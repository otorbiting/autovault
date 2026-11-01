"""Read and validate a local AutoVault portable backup for hosted import."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import zipfile
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List

from fastapi import HTTPException

from .app import SAFE_ID, TYPES, now
from .hosted import safe_relative_path

MAX_COMPRESSED_BYTES = 200_000_000
MAX_EXPANDED_BYTES = 1_000_000_000
MAX_ARCHIVE_ENTRIES = 10_000
MAX_GIT_COMMITS = 20_000


def portable_backup_bytes(path: Path) -> bytes:
    if path.stat().st_size > MAX_COMPRESSED_BYTES:
        raise ValueError("Portable backup exceeds the 200 MB compressed limit.")
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as project:
            member = "portable-data/autovault-data-backup.tar.gz"
            try:
                info = project.getinfo(member)
            except KeyError:
                raise ValueError("Project ZIP does not contain portable-data/autovault-data-backup.tar.gz.") from None
            if info.file_size > MAX_COMPRESSED_BYTES:
                raise ValueError("Portable backup exceeds the 200 MB compressed limit.")
            return project.read(info)
    return path.read_bytes()


@contextmanager
def staged_backup(content: bytes):
    if len(content) > MAX_COMPRESSED_BYTES:
        raise ValueError("Portable backup exceeds the 200 MB compressed limit.")
    temporary = tempfile.TemporaryDirectory(prefix="autovault-hosted-import-")
    root = Path(temporary.name)
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
            members = archive.getmembers()
            if len(members) > MAX_ARCHIVE_ENTRIES:
                raise ValueError("Portable backup contains too many entries.")
            total = 0
            seen = set()
            for member in members:
                name = member.name.replace("\\", "/").rstrip("/")
                parts = name.split("/")
                if (
                    not name
                    or name.startswith("/")
                    or any(part in {"", ".", ".."} for part in parts)
                    or any(ord(char) < 32 for char in name)
                ):
                    raise ValueError("Portable backup contains an unsafe path.")
                if name in seen:
                    raise ValueError("Portable backup contains duplicate paths.")
                seen.add(name)
                if not (member.isfile() or member.isdir()):
                    raise ValueError("Portable backup contains an unsupported file type.")
                total += member.size
                if total > MAX_EXPANDED_BYTES:
                    raise ValueError("Portable backup exceeds the 1 GB expanded limit.")
            for member in members:
                name = member.name.replace("\\", "/").rstrip("/")
                target = root.joinpath(*PurePosixPath(name).parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError("Portable backup contains an unreadable file.")
                with source, target.open("wb") as destination:
                    shutil.copyfileobj(source, destination)
        if not (root / "autovault.sqlite3").is_file():
            raise ValueError("Portable backup is missing its database.")
        (root / "workflows").mkdir(exist_ok=True)
        yield root
    finally:
        temporary.cleanup()


def _read_sqlite(root: Path) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    path = root / "autovault.sqlite3"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Portable backup database failed its integrity check.")
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "workflows" not in tables:
            raise ValueError("Portable backup database has no workflow catalog.")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(workflows)")}
        required = {"id", "name", "kind"}
        if not required.issubset(columns):
            raise ValueError("Portable backup catalog is missing required columns.")

        workflows: List[Dict[str, Any]] = []
        for source in conn.execute("SELECT * FROM workflows ORDER BY id"):
            row = dict(source)
            wid = row["id"]
            if not isinstance(wid, str) or not SAFE_ID.fullmatch(wid):
                raise ValueError("Portable backup contains an invalid workflow id.")
            if row.get("kind") not in TYPES:
                raise ValueError(f"Portable backup workflow {wid} has an unsupported type.")
            directory = root / "workflows" / wid
            if not directory.is_dir():
                raise ValueError(f"Portable backup is missing workflow folder {wid}.")
            row.setdefault("description", "")
            row.setdefault("tags", "[]")
            row.setdefault("status", "Active")
            row.setdefault("created", now())
            row.setdefault("updated", row["created"])
            try:
                tags = json.loads(row["tags"] or "[]") if isinstance(row["tags"], str) else row["tags"]
            except (TypeError, json.JSONDecodeError):
                raise ValueError(f"Portable backup workflow {wid} has invalid tags.") from None
            if not isinstance(tags, list):
                raise ValueError(f"Portable backup workflow {wid} has invalid tags.")
            row["tags"] = [str(tag).strip()[:50] for tag in tags if str(tag).strip()][:50]
            if row["status"] not in {"Active", "Testing", "Maintenance", "Archived"}:
                row["status"] = "Active"
            workflows.append(row)

        known_ids = {row["id"] for row in workflows}
        notes: List[Dict[str, Any]] = []
        if "notes" in tables:
            for source in conn.execute("SELECT * FROM notes ORDER BY id"):
                row = dict(source)
                if row.get("workflow_id") not in known_ids:
                    raise ValueError("Portable backup contains a note for a missing workflow.")
                notes.append({
                    "workflow_id": row["workflow_id"],
                    "body": str(row.get("body", ""))[:5000],
                    "created": str(row.get("created") or now()),
                })
        runs: List[Dict[str, Any]] = []
        if "runs" in tables:
            for source in conn.execute("SELECT * FROM runs ORDER BY id"):
                row = dict(source)
                if row.get("workflow_id") not in known_ids:
                    raise ValueError("Portable backup contains a run for a missing workflow.")
                runs.append({
                    "workflow_id": row["workflow_id"],
                    "command": str(row.get("command", ""))[:1000],
                    "returncode": row.get("returncode"),
                    "stdout": str(row.get("stdout", ""))[-30000:],
                    "stderr": str(row.get("stderr", ""))[-30000:],
                    "error": str(row.get("error", ""))[:1000],
                    "started": str(row.get("started") or now()),
                    "duration_ms": max(0, int(row.get("duration_ms") or 0)),
                })
        return workflows, notes, runs
    finally:
        conn.close()


def _current_files(directory: Path, size_counter: List[int]) -> Dict[str, bytes]:
    files: Dict[str, bytes] = {}
    for root, dirs, names in os.walk(directory, followlinks=False):
        current = Path(root)
        dirs[:] = [name for name in dirs if name != ".git"]
        for name in dirs:
            if (current / name).is_symlink():
                raise ValueError("Portable workflow contains a symbolic link.")
        for name in names:
            path = current / name
            if path.is_symlink():
                raise ValueError("Portable workflow contains a symbolic link.")
            relative = path.relative_to(directory).as_posix()
            if ".git" in PurePosixPath(relative).parts:
                continue
            try:
                relative = safe_relative_path(relative)
            except HTTPException as exc:
                raise ValueError(f"Portable workflow contains an unsafe file path: {relative}") from exc
            content = path.read_bytes()
            size_counter[0] += len(content)
            if size_counter[0] > MAX_EXPANDED_BYTES:
                raise ValueError("Portable workflow content exceeds the 1 GB import limit.")
            files[relative] = content
    return files


def _git_snapshots(
    directory: Path,
    size_counter: List[int],
    object_contents: Dict[str, bytes],
) -> List[Dict[str, Any]]:
    git_dir = directory / ".git"
    if not git_dir.is_dir():
        return []
    if (git_dir / "objects" / "info" / "alternates").exists() or (git_dir / "commondir").exists():
        raise ValueError("Portable Git history uses an external object directory and cannot be imported.")
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", ""),
        "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "GIT_OPTIONAL_LOCKS": "0",
    }

    def run_git(*args: str) -> bytes:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-c", "core.pager=cat", "-C", str(directory), *args],
            check=False,
            capture_output=True,
            timeout=120,
            env=environment,
        )
        if result.returncode:
            raise ValueError("Could not read the Git snapshot history from the portable backup.")
        return result.stdout

    head = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-C", str(directory), "rev-parse", "--verify", "HEAD"],
        check=False,
        capture_output=True,
        timeout=30,
        env=environment,
    )
    if head.returncode:
        return []
    commits_raw = run_git("log", "--reverse", "--format=%H%x09%cI%x09%s", "HEAD")
    commits = commits_raw.decode("utf-8", errors="replace").splitlines()
    if len(commits) > MAX_GIT_COMMITS:
        raise ValueError("Portable workflow contains more than 20,000 Git snapshots.")
    snapshots: List[Dict[str, Any]] = []
    for line in commits:
        parts = line.split("\t", 2)
        if len(parts) != 3 or not re.fullmatch(r"[0-9a-f]{40,64}", parts[0]):
            raise ValueError("Portable backup contains an invalid Git snapshot.")
        commit, created, message = parts
        manifest: Dict[str, str] = {}
        tree = run_git("ls-tree", "-rz", "--full-tree", commit)
        for item in tree.split(b"\0"):
            if not item:
                continue
            try:
                metadata, path_bytes = item.split(b"\t", 1)
                mode, object_type, object_id = metadata.decode("ascii").split(" ", 2)
                relative = safe_relative_path(path_bytes.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise ValueError("Portable Git history contains an invalid file path.") from None
            if not re.fullmatch(r"[0-9a-f]{40,64}", object_id):
                raise ValueError("Portable Git history contains an invalid object reference.")
            if object_type != "blob" or mode == "120000":
                continue
            content = run_git("cat-file", "blob", object_id)
            size_counter[0] += len(content)
            if size_counter[0] > MAX_EXPANDED_BYTES:
                raise ValueError("Portable Git history exceeds the 1 GB import limit.")
            object_hash = hashlib.sha256(content).hexdigest()
            manifest[relative] = object_hash
            object_contents.setdefault(object_hash, content)
        snapshots.append({
            "revision": commit[:12],
            "created": created[:80],
            "message": message.replace("\t", " ").replace("\n", " ")[:200],
            "manifest": manifest,
        })
    return snapshots


def source_data(root: Path) -> Dict[str, Any]:
    workflows, notes, runs = _read_sqlite(root)
    size_counter = [0]
    prepared = []
    for workflow in workflows:
        wid = workflow["id"]
        directory = root / "workflows" / wid
        files = _current_files(directory, size_counter)
        object_contents: Dict[str, bytes] = {}
        for content in files.values():
            object_contents.setdefault(hashlib.sha256(content).hexdigest(), content)
        snapshots = _git_snapshots(directory, size_counter, object_contents)
        for snapshot in snapshots:
            if len(snapshot["revision"]) < 12:
                raise ValueError("Portable backup contains an invalid Git snapshot id.")
        current_manifest: Dict[str, str] = {}
        for path, content in files.items():
            object_hash = hashlib.sha256(content).hexdigest()
            current_manifest[path] = object_hash
            object_contents[object_hash] = content
        if not snapshots or snapshots[-1]["manifest"] != current_manifest:
            snapshots.append({
                "revision": uuid_revision(),
                "created": now(),
                "message": "Imported current state",
                "manifest": current_manifest,
            })
        prepared.append({
            "workflow": workflow,
            "files": files,
            "snapshots": snapshots,
            "directory": directory,
            "objects": object_contents,
        })
    return {"workflows": prepared, "notes": notes, "runs": runs}


def uuid_revision() -> str:
    return uuid.uuid4().hex[:12]


def import_source_into_database(source: Dict[str, Any], conn, put_blob) -> int:
    conn.execute("LOCK TABLE workflows IN EXCLUSIVE MODE")
    if conn.execute("SELECT 1 FROM workflows LIMIT 1").fetchone():
        raise ValueError("Hosted library is not empty; refusing to overwrite existing workflows.")

    workflow_ids = set()
    for item in source["workflows"]:
        workflow = item["workflow"]
        wid = workflow["id"]
        workflow_ids.add(wid)
        conn.execute(
            "INSERT INTO workflows(id,name,kind,description,tags,status,created,updated) "
            "VALUES(%s,%s,%s,%s,%s::jsonb,%s,%s,%s)",
            (
                wid,
                str(workflow["name"])[:120],
                workflow["kind"],
                str(workflow.get("description", ""))[:1000],
                json.dumps(workflow["tags"]),
                workflow["status"],
                str(workflow["created"])[:80],
                str(workflow["updated"])[:80],
            ),
        )
        for object_hash, content in item["objects"].items():
            pathname = f"workflows/{wid}/objects/{object_hash}"
            if put_blob is not None:
                put_blob(pathname, content)
            conn.execute(
                "INSERT INTO blob_objects(workflow_id,object_hash,pathname) VALUES(%s,%s,%s)",
                (wid, object_hash, pathname),
            )
        for path, content in item["files"].items():
            object_hash = hashlib.sha256(content).hexdigest()
            conn.execute(
                "INSERT INTO workflow_files(workflow_id,path,object_hash) VALUES(%s,%s,%s)",
                (wid, path, object_hash),
            )
        revisions = set()
        for snapshot in item["snapshots"]:
            revision = snapshot["revision"]
            if revision in revisions:
                raise ValueError(f"Portable workflow {wid} contains duplicate snapshot ids.")
            revisions.add(revision)
            conn.execute(
                "INSERT INTO workflow_snapshots(workflow_id,revision,created,message,manifest) "
                "VALUES(%s,%s,%s,%s,%s::jsonb)",
                (
                    wid,
                    revision,
                    snapshot["created"],
                    snapshot["message"],
                    json.dumps(snapshot["manifest"]),
                ),
            )

    if len(workflow_ids) != len(source["workflows"]):
        raise ValueError("Portable backup contains duplicate workflow ids.")
    for note in source["notes"]:
        conn.execute(
            "INSERT INTO notes(workflow_id,body,created) VALUES(%s,%s,%s)",
            (note["workflow_id"], note["body"], note["created"]),
        )
    for run in source["runs"]:
        conn.execute(
            "INSERT INTO runs(workflow_id,command,returncode,stdout,stderr,error,started,duration_ms) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                run["workflow_id"], run["command"], run["returncode"], run["stdout"],
                run["stderr"], run["error"], run["started"], run["duration_ms"],
            ),
        )
    return len(source["workflows"])
