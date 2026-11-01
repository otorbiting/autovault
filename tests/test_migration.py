import io
import json
import hashlib
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from autovault.migration import import_source_into_database, portable_backup_bytes, source_data, staged_backup
from autovault.app import restore_hosted_snapshots


class PortableMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "workflows" / "sample-workflow").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def make_backup(self):
        workflow = self.root / "workflows" / "sample-workflow"
        subprocess.run(["git", "-C", str(workflow), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(workflow), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(workflow), "config", "user.email", "test@example.invalid"], check=True)
        (workflow / "main.py").write_text("print('first')\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(workflow), "add", "main.py"], check=True)
        subprocess.run(["git", "-C", str(workflow), "commit", "-m", "First version", "-q"], check=True)
        (workflow / "main.py").write_text("print('second')\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(workflow), "add", "main.py"], check=True)
        subprocess.run(["git", "-C", str(workflow), "commit", "-m", "Second version", "-q"], check=True)

        database = self.root / "autovault.sqlite3"
        conn = sqlite3.connect(database)
        conn.executescript(
            """
            CREATE TABLE workflows (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
                description TEXT DEFAULT '', tags TEXT DEFAULT '[]', status TEXT DEFAULT 'Active',
                created TEXT NOT NULL, updated TEXT NOT NULL
            );
            CREATE TABLE notes (id INTEGER PRIMARY KEY, workflow_id TEXT, body TEXT, created TEXT);
            CREATE TABLE runs (
                id INTEGER PRIMARY KEY, workflow_id TEXT, command TEXT, returncode INTEGER,
                stdout TEXT, stderr TEXT, error TEXT, started TEXT, duration_ms INTEGER
            );
            """
        )
        conn.execute(
            "INSERT INTO workflows VALUES(?,?,?,?,?,?,?,?)",
            ("sample-workflow", "Sample", "python", "", json.dumps(["test"]), "Active", "2024-01-01", "2024-01-02"),
        )
        conn.execute("INSERT INTO notes VALUES(1,?,?,?)", ("sample-workflow", "Imported note", "2024-01-02"))
        conn.execute(
            "INSERT INTO runs VALUES(1,?,?,?,?,?,?,?,?)",
            ("sample-workflow", "python main.py", 0, "ok", "", "", "2024-01-02", 10),
        )
        conn.commit()
        conn.close()
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            archive.add(database, arcname="autovault.sqlite3")
            archive.add(self.root / "workflows", arcname="workflows")
        return output.getvalue()

    def test_import_reads_workflows_notes_runs_and_git_history(self):
        backup = self.make_backup()
        with staged_backup(backup) as root:
            source = source_data(root)

        self.assertEqual(len(source["workflows"]), 1)
        workflow = source["workflows"][0]
        self.assertEqual(workflow["workflow"]["tags"], ["test"])
        self.assertEqual(len(workflow["snapshots"]), 2)
        self.assertEqual(workflow["snapshots"][0]["message"], "First version")
        self.assertEqual(workflow["snapshots"][1]["message"], "Second version")
        self.assertEqual(len(workflow["objects"]), 2)
        self.assertEqual(source["notes"][0]["body"], "Imported note")
        self.assertEqual(source["runs"][0]["stdout"], "ok")

    def test_import_plan_uploads_objects_and_refuses_existing_catalog(self):
        with staged_backup(self.make_backup()) as root:
            source = source_data(root)

        class Result:
            def __init__(self, value=None):
                self.value = value

            def fetchone(self):
                return self.value

        class Connection:
            def __init__(self, existing=False):
                self.existing = existing
                self.statements = []

            def execute(self, statement, params=None):
                self.statements.append((statement, params))
                return Result(("present",) if self.existing and statement.startswith("SELECT 1") else None)

        connection = Connection()
        uploads = []
        imported = import_source_into_database(source, connection, lambda path, data: uploads.append((path, data)))
        self.assertEqual(imported, 1)
        self.assertEqual(len(uploads), 2)
        self.assertEqual(sum("INSERT INTO workflow_snapshots" in sql for sql, _ in connection.statements), 2)
        self.assertEqual(sum("INSERT INTO notes" in sql for sql, _ in connection.statements), 1)
        self.assertEqual(sum("INSERT INTO runs" in sql for sql, _ in connection.statements), 1)

        existing = Connection(existing=True)
        with self.assertRaisesRegex(ValueError, "not empty"):
            import_source_into_database(source, existing, lambda _path, _data: self.fail("unexpected upload"))
        self.assertEqual(len(existing.statements), 2)

    def test_project_zip_backup_is_read_without_extracting_project(self):
        backup = self.make_backup()
        project = self.root / "portable-project.zip"
        with zipfile.ZipFile(project, "w") as archive:
            archive.writestr("portable-data/autovault-data-backup.tar.gz", backup)
            archive.writestr("README.md", "Project export")
        self.assertEqual(portable_backup_bytes(project), backup)

    def test_empty_library_backup_is_valid(self):
        database = self.root / "autovault.sqlite3"
        with sqlite3.connect(database) as conn:
            conn.executescript(
                """
                CREATE TABLE workflows (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
                    description TEXT DEFAULT '', tags TEXT DEFAULT '[]', status TEXT DEFAULT 'Active',
                    created TEXT NOT NULL, updated TEXT NOT NULL
                );
                CREATE TABLE notes (id INTEGER PRIMARY KEY, workflow_id TEXT, body TEXT, created TEXT);
                CREATE TABLE runs (
                    id INTEGER PRIMARY KEY, workflow_id TEXT, command TEXT, returncode INTEGER,
                    stdout TEXT, stderr TEXT, error TEXT, started TEXT, duration_ms INTEGER
                );
                """
            )
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            archive.add(database, arcname="autovault.sqlite3")
        with staged_backup(output.getvalue()) as root:
            source = source_data(root)
        self.assertEqual(source["workflows"], [])
        self.assertEqual(source["notes"], [])
        self.assertEqual(source["runs"], [])

    def test_unsafe_tar_path_is_rejected(self):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            info = tarfile.TarInfo("../outside.txt")
            info.size = 3
            archive.addfile(info, io.BytesIO(b"bad"))
        with self.assertRaisesRegex(ValueError, "unsafe path"):
            with staged_backup(output.getvalue()):
                pass

    def test_local_restore_rebuilds_hosted_snapshot_history(self):
        workflow = self.root / "hosted"
        workflow.mkdir()
        history = workflow / ".autovault-hosted-data"
        objects = history / "objects"
        objects.mkdir(parents=True)
        first = b"print('first')\n"
        second = b"print('second')\n"
        first_hash = hashlib.sha256(first).hexdigest()
        second_hash = hashlib.sha256(second).hexdigest()
        (objects / first_hash).write_bytes(first)
        (objects / second_hash).write_bytes(second)
        (workflow / "main.py").write_bytes(second)
        (history / "history.json").write_text(
            json.dumps({
                "snapshots": [
                    {
                        "revision": "a" * 12,
                        "created": "2024-01-01T10:00:00+00:00",
                        "message": "First saved version",
                        "manifest": {"main.py": first_hash},
                    },
                    {
                        "revision": "b" * 12,
                        "created": "2024-01-02T10:00:00+00:00",
                        "message": "Second saved version",
                        "manifest": {"main.py": second_hash},
                    },
                ]
            }),
            encoding="utf-8",
        )

        self.assertEqual(restore_hosted_snapshots("hosted", workflow), 2)
        self.assertEqual((workflow / "main.py").read_bytes(), second)
        self.assertFalse(history.exists())
        log = subprocess.check_output(
            ["git", "-C", str(workflow), "log", "--format=%s"],
            text=True,
        ).splitlines()
        self.assertEqual(log, ["Second saved version", "First saved version"])


if __name__ == "__main__":
    unittest.main()
