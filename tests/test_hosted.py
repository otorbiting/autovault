import os
import sys
import io
import unittest
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from autovault.hosted import _add_tar_dir, _zip_files, app, safe_relative_path
import tarfile
from fastapi import HTTPException


class HostedAuthTests(unittest.TestCase):
    def setUp(self):
        os.environ["AUTOVAULT_ACCESS_PASSWORD"] = "a-private-password-123"
        os.environ["AUTOVAULT_SESSION_SECRET"] = "s" * 40
        self.client = TestClient(app, base_url="https://testserver")

    def tearDown(self):
        os.environ.pop("AUTOVAULT_ACCESS_PASSWORD", None)
        os.environ.pop("AUTOVAULT_SESSION_SECRET", None)

    def test_all_hosted_pages_and_data_routes_require_auth(self):
        page = self.client.get("/", follow_redirects=False)
        self.assertEqual(page.status_code, 303)
        self.assertEqual(page.headers["location"], "/login")
        self.assertEqual(self.client.get("/static/app.js", follow_redirects=False).status_code, 303)
        self.assertEqual(self.client.get("/api/health").status_code, 401)
        self.assertEqual(
            self.client.post(
                "/api/workflows/private/run",
                json={"approved": True, "path": "main.py"},
            ).status_code,
            401,
        )

    def test_login_sets_secure_session_cookie_and_hosted_execution_stays_disabled(self):
        denied = self.client.post("/api/auth/login", data={"password": "wrong"})
        self.assertEqual(denied.status_code, 401)

        response = self.client.post(
            "/api/auth/login",
            data={"password": "a-private-password-123"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("secure", cookie)
        self.assertIn("samesite=strict", cookie)

        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn('data-hosted="true"', home.text)
        run = self.client.post(
            "/api/workflows/private/run",
            json={"approved": True, "path": "main.py"},
        )
        self.assertEqual(run.status_code, 403)
        self.assertEqual(run.json()["error"], "Script execution is disabled in the hosted deployment.")

        logout = self.client.post("/api/auth/logout", json={})
        self.assertEqual(logout.status_code, 204)
        self.assertEqual(self.client.get("/api/health").status_code, 401)

    def test_auth_configuration_fails_closed(self):
        os.environ.pop("AUTOVAULT_ACCESS_PASSWORD", None)
        os.environ.pop("AUTOVAULT_SESSION_SECRET", None)
        response = self.client.post("/api/auth/login", data={"password": "anything"})
        self.assertEqual(response.status_code, 503)
        self.assertIn("AUTOVAULT_ACCESS_PASSWORD", response.json()["error"])

    def test_oversized_request_is_rejected(self):
        response = self.client.post(
            "/api/auth/login",
            content=b"x" * 3_500_001,
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(response.status_code, 413)

    def test_hosted_file_paths_are_confined(self):
        self.assertEqual(safe_relative_path("src/main.py"), "src/main.py")
        for path in ("../outside", "/absolute", "a//b", ".git/config", ".autovault-hosted-data/history.json"):
            with self.subTest(path=path):
                with self.assertRaises(HTTPException):
                    safe_relative_path(path)

    def test_zip_import_accepts_directory_entries_and_rejects_traversal(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("src/", "")
            zipped.writestr("src/main.py", "print('ok')")
        self.assertEqual(_zip_files(archive.getvalue()), {"src/main.py": b"print('ok')"})

        unsafe = io.BytesIO()
        with zipfile.ZipFile(unsafe, "w") as zipped:
            zipped.writestr("../outside.txt", "no")
        with self.assertRaises(HTTPException):
            _zip_files(unsafe.getvalue())

    def test_tar_backup_directories_are_real_tar_directories(self):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            _add_tar_dir(archive, "workflows/sample")
        with tarfile.open(fileobj=io.BytesIO(output.getvalue()), mode="r:gz") as archive:
            member = archive.getmembers()[0]
            self.assertTrue(member.isdir())


if __name__ == "__main__":
    unittest.main()
