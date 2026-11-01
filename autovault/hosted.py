"""Vercel adapter for the local AutoVault interface."""
from __future__ import annotations

import base64
import datetime as dt
import difflib
import hashlib
import hmac
import io
import json
import mimetypes
import os
import re
import sqlite3
import uuid
import tarfile
import tempfile
import threading
import urllib.parse
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from psycopg.rows import dict_row

from .app import SAFE_ID, TYPES, new_id, now

STATIC = Path(__file__).parent / "static"
SESSION_COOKIE = "autovault_session"
SESSION_SECONDS = 8 * 60 * 60
MAX_REQUEST_BYTES = 3_500_000
SAFE_STATUS = {"Active", "Testing", "Maintenance", "Archived"}
_schema_ready = False
_schema_lock = threading.Lock()

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def _auth_settings() -> Tuple[str, bytes]:
    password = os.environ.get("AUTOVAULT_ACCESS_PASSWORD", "")
    secret = os.environ.get("AUTOVAULT_SESSION_SECRET", "")
    if len(password) < 16 or len(secret) < 32:
        raise RuntimeError(
            "Set AUTOVAULT_ACCESS_PASSWORD (at least 16 characters) and "
            "AUTOVAULT_SESSION_SECRET (at least 32 characters)."
        )
    return password, secret.encode("utf-8")


def _valid_session(token: Optional[str]) -> bool:
    if not token:
        return False
    try:
        password, secret = _auth_settings()
        expires_text, signature = token.split(".", 1)
        expires = int(expires_text)
    except (RuntimeError, ValueError):
        return False
    if expires < int(dt.datetime.now(dt.timezone.utc).timestamp()):
        return False
    signing_key = hashlib.sha256(secret + b"\0" + password.encode("utf-8")).digest()
    expected = hmac.new(signing_key, expires_text.encode("ascii"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def _session_token() -> str:
    password, secret = _auth_settings()
    expires_text = str(int(dt.datetime.now(dt.timezone.utc).timestamp()) + SESSION_SECONDS)
    signing_key = hashlib.sha256(secret + b"\0" + password.encode("utf-8")).digest()
    signature = hmac.new(signing_key, expires_text.encode("ascii"), hashlib.sha256).hexdigest()
    return expires_text + "." + signature


@app.middleware("http")
async def require_hosted_auth(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_REQUEST_BYTES:
        return JSONResponse({"error": "Request exceeds the hosted 3.5 MB limit"}, status_code=413)
    public_paths = {"/login", "/api/auth/login"}
    if request.url.path not in public_paths and not _valid_session(request.cookies.get(SESSION_COOKIE)):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        return RedirectResponse("/login", status_code=303)

    origin = request.headers.get("origin")
    if origin and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        parsed = urllib.parse.urlsplit(origin)
        if parsed.netloc.lower() != request.headers.get("host", "").lower():
            return JSONResponse({"error": "Cross-origin request rejected"}, status_code=403)

    try:
        response = await call_next(request)
    except RuntimeError as exc:
        if "AUTOVAULT_ACCESS_PASSWORD" in str(exc) or "AUTOVAULT_SESSION_SECRET" in str(exc):
            return JSONResponse({"error": str(exc)}, status_code=503)
        raise
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.exception_handler(HTTPException)
async def http_error_handler(_request: Request, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, str) else "Request failed"
    return JSONResponse({"error": detail}, status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request: Request, _exc: RequestValidationError):
    return JSONResponse({"error": "Invalid request"}, status_code=400)


def db_url() -> str:
    value = os.environ.get("DATABASE_URL", "").strip()
    if not value:
        raise RuntimeError("Set DATABASE_URL to the Neon Postgres connection string.")
    return value


def connection():
    import psycopg

    return psycopg.connect(db_url(), row_factory=dict_row)


def ensure_schema() -> None:
    global _schema_ready
    try:
        db_url()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from None
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        statements = [
            """
                CREATE TABLE IF NOT EXISTS workflows (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
                    status TEXT NOT NULL DEFAULT 'Active',
                    created TEXT NOT NULL,
                    updated TEXT NOT NULL
                )
            """,
            """
                CREATE TABLE IF NOT EXISTS notes (
                    id BIGSERIAL PRIMARY KEY,
                    workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
                    body TEXT NOT NULL,
                    created TEXT NOT NULL
                )
            """,
            """
                CREATE TABLE IF NOT EXISTS runs (
                    id BIGSERIAL PRIMARY KEY,
                    workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
                    command TEXT NOT NULL,
                    returncode INTEGER,
                    stdout TEXT NOT NULL DEFAULT '',
                    stderr TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    started TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL DEFAULT 0
                )
            """,
            """
                CREATE TABLE IF NOT EXISTS blob_objects (
                    workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
                    object_hash TEXT NOT NULL,
                    pathname TEXT NOT NULL,
                    PRIMARY KEY (workflow_id, object_hash)
                )
            """,
            """
                CREATE TABLE IF NOT EXISTS workflow_files (
                    workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
                    path TEXT NOT NULL,
                    object_hash TEXT NOT NULL,
                    PRIMARY KEY (workflow_id, path),
                    FOREIGN KEY (workflow_id, object_hash)
                        REFERENCES blob_objects(workflow_id, object_hash)
                )
            """,
            """
                CREATE TABLE IF NOT EXISTS workflow_snapshots (
                    workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
                    revision TEXT NOT NULL,
                    id BIGSERIAL UNIQUE,
                    created TEXT NOT NULL,
                    message TEXT NOT NULL,
                    manifest JSONB NOT NULL,
                    PRIMARY KEY (workflow_id, revision)
                )
            """,
            "CREATE INDEX IF NOT EXISTS workflow_updated_idx ON workflows(updated DESC)",
            "CREATE INDEX IF NOT EXISTS notes_workflow_idx ON notes(workflow_id, id DESC)",
            "CREATE INDEX IF NOT EXISTS runs_workflow_idx ON runs(workflow_id, id DESC)",
            """
                CREATE INDEX IF NOT EXISTS snapshots_workflow_idx
                    ON workflow_snapshots(workflow_id, id DESC)
            """,
            """
                CREATE TABLE IF NOT EXISTS backup_exports (
                    id TEXT PRIMARY KEY,
                    workflow_id TEXT,
                    pathname TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    created TEXT NOT NULL
                )
            """,
        ]
        with connection() as conn:
            for statement in statements:
                conn.execute(statement)
        _schema_ready = True


def blob_api():
    try:
        from vercel import blob
    except ImportError as exc:
        raise RuntimeError("Install the Vercel Blob Python SDK from requirements.txt.") from exc

    return blob


def safe_relative_path(raw: str) -> str:
    normalized = str(raw).replace("\\", "/")
    parts = normalized.split("/")
    if (
        not normalized
        or normalized.startswith("/")
        or any(part in {"", ".", "..", ".git", ".autovault-hosted-data"} for part in parts)
        or any(ord(char) < 32 for char in normalized)
    ):
        raise HTTPException(400, "Invalid file path")
    return str(PurePosixPath(*parts))


def _workflow_id(wid: str) -> str:
    if not SAFE_ID.fullmatch(wid):
        raise HTTPException(400, "Invalid workflow id")
    return wid


async def json_body(request: Request) -> Dict[str, Any]:
    raw = await request.body()
    if len(raw) > MAX_REQUEST_BYTES:
        raise HTTPException(413, "Request exceeds the hosted 3.5 MB limit")
    try:
        value = json.loads(raw or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(400, "Invalid JSON request") from None
    if not isinstance(value, dict):
        raise HTTPException(400, "JSON request must be an object")
    return value


def _tags(value: Any) -> List[str]:
    if not isinstance(value, list):
        raise HTTPException(400, "Tags must be a list")
    return [str(tag).strip()[:50] for tag in value if str(tag).strip()][:50]


def _workflow(row: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(row)
    tags = result.get("tags", [])
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except json.JSONDecodeError:
            tags = []
    result["tags"] = tags if isinstance(tags, list) else []
    return result


def _workflow_row(conn, wid: str) -> Optional[Dict[str, Any]]:
    return conn.execute("SELECT * FROM workflows WHERE id=%s", (wid,)).fetchone()


def _snapshot(conn, wid: str, message: str, created: Optional[str] = None) -> str:
    rows = conn.execute(
        "SELECT path, object_hash FROM workflow_files WHERE workflow_id=%s ORDER BY path",
        (wid,),
    ).fetchall()
    manifest = {row["path"]: row["object_hash"] for row in rows}
    revision = uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO workflow_snapshots(workflow_id,revision,created,message,manifest) "
        "VALUES(%s,%s,%s,%s,%s::jsonb)",
        (wid, revision, created or now(), str(message)[:200], json.dumps(manifest)),
    )
    return revision


async def _put_object(conn, wid: str, content: bytes) -> str:
    object_hash = hashlib.sha256(content).hexdigest()
    existing = conn.execute(
        "SELECT pathname FROM blob_objects WHERE workflow_id=%s AND object_hash=%s",
        (wid, object_hash),
    ).fetchone()
    if existing:
        return object_hash
    pathname = f"workflows/{wid}/objects/{object_hash}"
    await blob_api().put_blob(
        pathname,
        content,
        access="private",
        content_type="application/octet-stream",
        allow_overwrite=True,
    )
    conn.execute(
        "INSERT INTO blob_objects(workflow_id,object_hash,pathname) VALUES(%s,%s,%s)",
        (wid, object_hash, pathname),
    )
    return object_hash


async def _write_files(conn, wid: str, files: Dict[str, bytes]) -> None:
    for raw_path, content in files.items():
        path = safe_relative_path(raw_path)
        object_hash = await _put_object(conn, wid, content)
        conn.execute(
            """
            INSERT INTO workflow_files(workflow_id,path,object_hash) VALUES(%s,%s,%s)
            ON CONFLICT(workflow_id,path) DO UPDATE SET object_hash=EXCLUDED.object_hash
            """,
            (wid, path, object_hash),
        )


def _zip_files(data: bytes) -> Dict[str, bytes]:
    files: Dict[str, bytes] = {}
    expanded = 0
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        with archive:
            for info in archive.infolist():
                raw_name = info.filename.replace("\\", "/")
                if info.is_dir():
                    raw_name = raw_name.rstrip("/")
                parts = raw_name.split("/")
                mode = (info.external_attr >> 16) & 0o170000
                if (
                    raw_name.startswith("/")
                    or not raw_name
                    or any(part in {"", ".", ".."} for part in parts)
                    or mode == 0o120000
                ):
                    raise HTTPException(400, "Archive contains an unsafe path")
                if ".git" in parts:
                    continue
                if info.is_dir():
                    continue
                path = safe_relative_path(raw_name)
                if path in files:
                    raise HTTPException(400, "Archive contains duplicate file paths")
                expanded += info.file_size
                if len(files) >= 500 or expanded > 100_000_000:
                    raise HTTPException(413, "Archive exceeds the 500-file or 100 MB expanded limit")
                files[path] = archive.read(info)
    except (zipfile.BadZipFile, OSError, RuntimeError):
        raise HTTPException(400, "Invalid ZIP archive") from None
    if not files:
        raise HTTPException(400, "Archive contains no importable files")
    return files


async def _create_workflow(
    name: str,
    kind: str,
    description: str,
    tags: List[str],
    files: Dict[str, bytes],
    status: str = "Active",
    snapshot_message: str = "Initial version",
) -> str:
    if kind not in TYPES:
        raise HTTPException(400, "Unsupported workflow type")
    name = str(name).strip()[:120]
    if not name:
        raise HTTPException(400, "Workflow name is required")
    if status not in SAFE_STATUS:
        raise HTTPException(400, "Invalid workflow status")
    wid = new_id(name)
    created = now()
    ensure_schema()
    with connection() as conn:
        conn.execute(
            "INSERT INTO workflows(id,name,kind,description,tags,status,created,updated) "
            "VALUES(%s,%s,%s,%s,%s::jsonb,%s,%s,%s)",
            (wid, name, kind, str(description)[:1000], json.dumps(tags), status, created, created),
        )
        await _write_files(conn, wid, files)
        _snapshot(conn, wid, snapshot_message)
    return wid


async def _file_bytes(wid: str, path: str) -> bytes:
    ensure_schema()
    with connection() as conn:
        row = conn.execute(
            """
            SELECT o.pathname FROM workflow_files f
            JOIN blob_objects o ON o.workflow_id=f.workflow_id AND o.object_hash=f.object_hash
            WHERE f.workflow_id=%s AND f.path=%s
            """,
            (wid, path),
        ).fetchone()
    if not row:
        raise HTTPException(404, "File not found")
    result = await blob_api().get_blob(row["pathname"], access="private")
    return result.body


def _revision_row(conn, wid: str, selector: str) -> Optional[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT revision,created,message,manifest FROM workflow_snapshots "
        "WHERE workflow_id=%s ORDER BY id DESC",
        (wid,),
    ).fetchall()
    if selector == "HEAD":
        return rows[0] if rows else None
    parent = re.fullmatch(r"HEAD~([0-9]+)", selector)
    if parent:
        index = int(parent.group(1))
        return rows[index] if index < len(rows) else None
    matches = [row for row in rows if row["revision"].startswith(selector)]
    return matches[0] if len(matches) == 1 else None


async def _manifest_files(wid: str, manifest: Dict[str, str]) -> Dict[str, str]:
    ensure_schema()
    with connection() as conn:
        objects = {
            row["object_hash"]: row["pathname"]
            for row in conn.execute(
                "SELECT object_hash,pathname FROM blob_objects WHERE workflow_id=%s",
                (wid,),
            ).fetchall()
        }
    result: Dict[str, str] = {}
    for path, object_hash in manifest.items():
        pathname = objects.get(object_hash)
        if not pathname:
            raise RuntimeError("Snapshot references a missing stored file object.")
        data = await blob_api().get_blob(pathname, access="private")
        result[path] = data.body.decode("utf-8", errors="replace")
    return result


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return HTMLResponse(
        """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in — AutoVault</title><body style="font:16px system-ui;max-width:28rem;margin:10vh auto;padding:2rem">
<h1>AutoVault</h1><p>Sign in to your private workflow library.</p>
<form action="/api/auth/login" method="post"><label for="password">App password</label>
<input id="password" name="password" type="password" required autocomplete="current-password"
style="display:block;width:100%;box-sizing:border-box;padding:.8rem;margin:.5rem 0 1rem">
<button style="padding:.7rem 1rem">Sign in</button></form></body></html>"""
    )


@app.post("/api/auth/login")
async def login(request: Request):
    try:
        configured_password, _ = _auth_settings()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from None
    body = await request.body()
    if len(body) > MAX_REQUEST_BYTES:
        raise HTTPException(413, "Request exceeds the hosted 3.5 MB limit")
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            values = json.loads(body or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(400, "Invalid login request") from None
    else:
        values = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"))
        values = {key: items[-1] for key, items in values.items()}
    password = values.get("password", "") if isinstance(values, dict) else ""
    if not isinstance(password, str) or not hmac.compare_digest(password, configured_password):
        raise HTTPException(401, "Incorrect password")
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        _session_token(),
        max_age=SESSION_SECONDS,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    return response


@app.post("/api/auth/logout")
async def logout():
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    return response


@app.get("/")
async def home():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(page.replace("<body>", '<body data-hosted="true">', 1))


@app.get("/static/{asset:path}")
async def static_asset(asset: str):
    path = (STATIC / asset).resolve()
    if STATIC.resolve() not in path.parents or not path.is_file():
        raise HTTPException(404, "Asset not found")
    return FileResponse(path, media_type=mimetypes.guess_type(str(path))[0])


@app.get("/api/health")
async def health():
    ensure_schema()
    return {"ok": True, "storage": "managed"}


@app.get("/api/workflows")
async def list_workflows(q: str = "", type: str = "", status: str = ""):
    ensure_schema()
    with connection() as conn:
        rows = conn.execute("SELECT * FROM workflows ORDER BY updated DESC").fetchall()
    values = [_workflow(row) for row in rows]
    term = q.strip().lower()
    values = [
        row
        for row in values
        if (not term or term in (row["name"] + " " + row["description"] + " " + " ".join(row["tags"])).lower())
        and (not type or row["kind"] == type)
        and (not status or row["status"] == status)
    ]
    return {"workflows": values, "home": "Managed storage"}


@app.post("/api/workflows", status_code=201)
async def create_workflow(request: Request):
    data = await json_body(request)
    kind = str(data.get("kind", "generic"))
    name = str(data.get("name", "New workflow")).strip()[:120]
    filename = str(data.get("filename") or "workflow" + (TYPES.get(kind, [".md"])[0] if TYPES.get(kind) else ".md"))
    filename = filename.replace("\\", "/").split("/")[-1]
    if not filename or filename.startswith("."):
        filename = "workflow.md"
    wid = await _create_workflow(
        name,
        kind,
        str(data.get("description", "")),
        _tags(data.get("tags", [])),
        {filename: str(data.get("content", "")).encode("utf-8")},
    )
    return {"id": wid}


@app.post("/api/import", status_code=201)
async def import_workflow(request: Request):
    data = await json_body(request)
    filename = str(data.get("filename", "workflow.json")).replace("\\", "/").split("/")[-1]
    if not filename:
        raise HTTPException(400, "Invalid import")
    extension = Path(filename).suffix.lower()
    kind = {
        ".py": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
        ".ts": "javascript", ".sh": "shell", ".command": "shell", ".json": "n8n",
        ".yml": "docker-compose", ".yaml": "docker-compose",
    }.get(extension, "generic")
    if data.get("kind") in TYPES:
        kind = data["kind"]
    name = str(data.get("name") or Path(filename).stem).strip()[:120]
    if data.get("archive"):
        encoded = data["archive"]
        if not isinstance(encoded, str):
            raise HTTPException(400, "Invalid archive encoding")
        try:
            archive = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            raise HTTPException(400, "Invalid archive encoding") from None
        if len(archive) > 2_500_000:
            raise HTTPException(413, "Compressed import exceeds the hosted 2.5 MB limit")
        files = _zip_files(archive)
    else:
        content = str(data.get("content", ""))
        if len(content.encode("utf-8")) > 1_000_000:
            raise HTTPException(413, "Text import exceeds 1 MB")
        files = {filename: content.encode("utf-8")}
    wid = await _create_workflow(
        name,
        kind,
        str(data.get("description", "")),
        _tags(data.get("tags", [])),
        files,
        snapshot_message="Imported workflow",
    )
    return {"id": wid, "kind": kind}


@app.get("/api/workflows/{wid}")
async def workflow_detail(wid: str):
    wid = _workflow_id(wid)
    ensure_schema()
    with connection() as conn:
        workflow = _workflow_row(conn, wid)
        if not workflow:
            raise HTTPException(404, "Workflow not found")
        files = [
            row["path"]
            for row in conn.execute(
                "SELECT path FROM workflow_files WHERE workflow_id=%s ORDER BY path", (wid,)
            ).fetchall()
        ]
        history = conn.execute(
            "SELECT revision,created,message FROM workflow_snapshots "
            "WHERE workflow_id=%s ORDER BY id DESC LIMIT 100",
            (wid,),
        ).fetchall()
    commits = [f'{row["revision"]}\t{row["created"][:10]}\t{row["message"]}' for row in history]
    return {"workflow": _workflow(workflow), "files": files, "history": commits}


@app.get("/api/workflows/{wid}/notes")
async def workflow_notes(wid: str):
    wid = _workflow_id(wid)
    ensure_schema()
    with connection() as conn:
        if not _workflow_row(conn, wid):
            raise HTTPException(404, "Workflow not found")
        notes = conn.execute(
            "SELECT id,workflow_id,body,created FROM notes WHERE workflow_id=%s ORDER BY id DESC",
            (wid,),
        ).fetchall()
    return {"notes": notes}


@app.get("/api/workflows/{wid}/runs")
async def workflow_runs(wid: str):
    wid = _workflow_id(wid)
    ensure_schema()
    with connection() as conn:
        if not _workflow_row(conn, wid):
            raise HTTPException(404, "Workflow not found")
        runs = conn.execute(
            "SELECT id,workflow_id,command,returncode,stdout,stderr,error,started,duration_ms "
            "FROM runs WHERE workflow_id=%s ORDER BY id DESC LIMIT 50",
            (wid,),
        ).fetchall()
    return {"runs": runs}


@app.get("/api/workflows/{wid}/diff")
async def workflow_diff(wid: str, a: str = "HEAD~1", b: str = "HEAD"):
    wid = _workflow_id(wid)
    ensure_schema()
    with connection() as conn:
        if not _workflow_row(conn, wid):
            raise HTTPException(404, "Workflow not found")
        rev_a = _revision_row(conn, wid, a)
        rev_b = _revision_row(conn, wid, b)
    if not rev_a or not rev_b:
        raise HTTPException(400, "Snapshot not found")
    left = await _manifest_files(wid, rev_a["manifest"])
    right = await _manifest_files(wid, rev_b["manifest"])
    output: List[str] = []
    for path in sorted(set(left) | set(right)):
        output.extend(
            difflib.unified_diff(
                left.get(path, "").splitlines(keepends=True),
                right.get(path, "").splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
        )
    return {"diff": "".join(output), "error": ""}


@app.get("/api/file/{wid}/{rel:path}")
async def read_file(wid: str, rel: str):
    wid = _workflow_id(wid)
    path = safe_relative_path(rel)
    data = await _file_bytes(wid, path)
    return {"path": path, "content": data.decode("utf-8", errors="replace")[:500_000]}


@app.post("/api/workflows/{wid}/update")
async def update_workflow(wid: str, request: Request):
    wid = _workflow_id(wid)
    data = await json_body(request)
    fields: Dict[str, Any] = {}
    if "name" in data:
        name = str(data["name"]).strip()[:120]
        if not name:
            raise HTTPException(400, "Workflow name is required")
        fields["name"] = name
    if "description" in data:
        fields["description"] = str(data["description"])[:1000]
    if "status" in data:
        if data["status"] not in SAFE_STATUS:
            raise HTTPException(400, "Invalid workflow status")
        fields["status"] = data["status"]
    if "tags" in data:
        fields["tags"] = json.dumps(_tags(data["tags"]))
    if not fields:
        raise HTTPException(400, "No fields to update")
    ensure_schema()
    with connection() as conn:
        if not _workflow_row(conn, wid):
            raise HTTPException(404, "Workflow not found")
        assignments = [f"{column}=%s" + ("::jsonb" if column == "tags" else "") for column in fields]
        values = list(fields.values()) + [now(), wid]
        conn.execute(
            f"UPDATE workflows SET {', '.join(assignments)},updated=%s WHERE id=%s",
            values,
        )
    return {"ok": True}


@app.post("/api/workflows/{wid}/save")
async def save_file(wid: str, request: Request):
    wid = _workflow_id(wid)
    data = await json_body(request)
    path = safe_relative_path(str(data.get("path", "")))
    content = str(data.get("content", "")).encode("utf-8")
    if len(content) > 1_000_000:
        raise HTTPException(413, "File exceeds the 1 MB editor limit")
    ensure_schema()
    with connection() as conn:
        if not _workflow_row(conn, wid):
            raise HTTPException(404, "Workflow not found")
        existing = conn.execute(
            "SELECT 1 FROM workflow_files WHERE workflow_id=%s AND path=%s", (wid, path)
        ).fetchone()
        if not existing and data.get("newFile") is not True:
            raise HTTPException(400, "Choose an existing workflow file")
        if existing and data.get("newFile") is True:
            raise HTTPException(409, "A file with that name already exists")
        await _write_files(conn, wid, {path: content})
        revision = _snapshot(conn, wid, str(data.get("message", "Update workflow")))
        conn.execute("UPDATE workflows SET updated=%s WHERE id=%s", (now(), wid))
    return {"ok": True, "commit": revision}


@app.post("/api/workflows/{wid}/notes", status_code=201)
async def add_note(wid: str, request: Request):
    wid = _workflow_id(wid)
    data = await json_body(request)
    text = str(data.get("body", "")).strip()
    if not text:
        raise HTTPException(400, "Note cannot be empty")
    ensure_schema()
    with connection() as conn:
        if not _workflow_row(conn, wid):
            raise HTTPException(404, "Workflow not found")
        conn.execute(
            "INSERT INTO notes(workflow_id,body,created) VALUES(%s,%s,%s)",
            (wid, text[:5000], now()),
        )
    return {"ok": True}


@app.post("/api/workflows/{wid}/restore")
async def restore_snapshot(wid: str, request: Request):
    wid = _workflow_id(wid)
    data = await json_body(request)
    revision = str(data.get("revision", ""))
    if not re.fullmatch(r"[0-9a-f]{4,40}", revision):
        raise HTTPException(400, "Invalid revision")
    ensure_schema()
    with connection() as conn:
        if not _workflow_row(conn, wid):
            raise HTTPException(404, "Workflow not found")
        selected = _revision_row(conn, wid, revision)
        if not selected:
            raise HTTPException(400, "Snapshot not found")
        _snapshot(conn, wid, "Before restore")
        conn.execute("DELETE FROM workflow_files WHERE workflow_id=%s", (wid,))
        manifest = selected["manifest"]
        for path, object_hash in manifest.items():
            safe_path = safe_relative_path(path)
            conn.execute(
                "INSERT INTO workflow_files(workflow_id,path,object_hash) VALUES(%s,%s,%s)",
                (wid, safe_path, object_hash),
            )
        new_revision = _snapshot(conn, wid, "Restore snapshot " + selected["revision"])
        conn.execute("UPDATE workflows SET updated=%s WHERE id=%s", (now(), wid))
    return {"ok": True, "commit": new_revision}


@app.post("/api/workflows/{wid}/run")
async def disabled_script_execution(wid: str):
    _workflow_id(wid)
    raise HTTPException(403, "Script execution is disabled in the hosted deployment.")


def _add_tar_file(archive: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o600
    archive.addfile(info, io.BytesIO(data))


def _add_tar_dir(archive: tarfile.TarFile, name: str) -> None:
    info = tarfile.TarInfo(name.rstrip("/") + "/")
    info.type = tarfile.DIRTYPE
    info.mode = 0o700
    archive.addfile(info)


async def _make_workflow_backup(wid: str) -> bytes:
    detail = await workflow_detail(wid)
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for path in detail["files"]:
            content = await _file_bytes(wid, path)
            _add_tar_file(archive, f"{wid}/{path}", content)
        await _add_snapshot_bundle(archive, wid, wid)
    return output.getvalue()


async def _add_snapshot_bundle(archive: tarfile.TarFile, wid: str, root: str) -> None:
    ensure_schema()
    with connection() as conn:
        snapshots = conn.execute(
            "SELECT revision,created,message,manifest FROM workflow_snapshots "
            "WHERE workflow_id=%s ORDER BY id",
            (wid,),
        ).fetchall()
        objects = {
            row["object_hash"]: row["pathname"]
            for row in conn.execute(
                "SELECT object_hash,pathname FROM blob_objects WHERE workflow_id=%s",
                (wid,),
            ).fetchall()
        }
    if not snapshots:
        return
    history_root = f"{root}/.autovault-hosted-data"
    payload = json.dumps(
        {
            "snapshots": [
                {
                    "revision": row["revision"],
                    "created": row["created"],
                    "message": row["message"],
                    "manifest": row["manifest"],
                }
                for row in snapshots
            ]
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    _add_tar_file(archive, f"{history_root}/history.json", payload)
    hashes = {
        object_hash
        for snapshot in snapshots
        for object_hash in snapshot["manifest"].values()
    }
    for object_hash in sorted(hashes):
        pathname = objects.get(object_hash)
        if not pathname:
            raise RuntimeError("Hosted snapshot references a missing file object.")
        stored = await blob_api().get_blob(pathname, access="private")
        _add_tar_file(archive, f"{history_root}/objects/{object_hash}", stored.body)


async def _make_full_backup() -> bytes:
    ensure_schema()
    with connection() as conn:
        workflows = [_workflow(row) for row in conn.execute("SELECT * FROM workflows ORDER BY id").fetchall()]
        notes = conn.execute("SELECT id,workflow_id,body,created FROM notes ORDER BY id").fetchall()
        runs = conn.execute(
            "SELECT id,workflow_id,command,returncode,stdout,stderr,error,started,duration_ms FROM runs ORDER BY id"
        ).fetchall()
        file_rows = conn.execute(
            "SELECT workflow_id,path FROM workflow_files ORDER BY workflow_id,path"
        ).fetchall()

    with tempfile.TemporaryDirectory(prefix="autovault-export-") as directory:
        database_path = Path(directory) / "autovault.sqlite3"
        db_conn = sqlite3.connect(database_path)
        db_conn.executescript(
            """
            CREATE TABLE workflows (id TEXT PRIMARY KEY,name TEXT NOT NULL,kind TEXT NOT NULL,
                description TEXT DEFAULT '',tags TEXT DEFAULT '',status TEXT DEFAULT 'Active',
                created TEXT NOT NULL,updated TEXT NOT NULL);
            CREATE TABLE notes (id INTEGER PRIMARY KEY,workflow_id TEXT NOT NULL,body TEXT NOT NULL,
                created TEXT NOT NULL);
            CREATE TABLE runs (id INTEGER PRIMARY KEY,workflow_id TEXT NOT NULL,command TEXT NOT NULL,
                returncode INTEGER,stdout TEXT DEFAULT '',stderr TEXT DEFAULT '',error TEXT DEFAULT '',
                started TEXT NOT NULL,duration_ms INTEGER NOT NULL);
            """
        )
        db_conn.executemany(
            "INSERT INTO workflows VALUES(?,?,?,?,?,?,?,?)",
            [
                (w["id"], w["name"], w["kind"], w["description"], json.dumps(w["tags"]), w["status"], w["created"], w["updated"])
                for w in workflows
            ],
        )
        db_conn.executemany(
            "INSERT INTO notes VALUES(?,?,?,?)",
            [(n["id"], n["workflow_id"], n["body"], n["created"]) for n in notes],
        )
        db_conn.executemany(
            "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)",
            [
                (r["id"], r["workflow_id"], r["command"], r["returncode"], r["stdout"], r["stderr"], r["error"], r["started"], r["duration_ms"])
                for r in runs
            ],
        )
        db_conn.commit()
        db_conn.close()

        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            _add_tar_file(archive, "autovault.sqlite3", database_path.read_bytes())
            _add_tar_dir(archive, "workflows")
            for workflow in workflows:
                wid = workflow["id"]
                _add_tar_dir(archive, f"workflows/{wid}")
                await _add_snapshot_bundle(archive, wid, f"workflows/{wid}")
            for row in file_rows:
                data = await _file_bytes(row["workflow_id"], row["path"])
                _add_tar_file(archive, f'workflows/{row["workflow_id"]}/{row["path"]}', data)
        return output.getvalue()


async def _save_backup_export(content: bytes, workflow_id: Optional[str]) -> Dict[str, str]:
    export_id = uuid.uuid4().hex
    filename = (
        f"{workflow_id}-backup.tar.gz"
        if workflow_id
        else "autovault-backup-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S") + ".tar.gz"
    )
    pathname = f"backups/{export_id}/{filename}"
    await blob_api().put_blob(
        pathname,
        content,
        access="private",
        content_type="application/gzip",
        allow_overwrite=False,
    )
    ensure_schema()
    with connection() as conn:
        conn.execute(
            "INSERT INTO backup_exports(id,workflow_id,pathname,filename,created) VALUES(%s,%s,%s,%s,%s)",
            (export_id, workflow_id, pathname, filename, now()),
        )
    return {"id": export_id}


@app.post("/api/backup")
async def create_backup():
    result = await _make_full_backup()
    return await _save_backup_export(result, None)


@app.get("/api/backup/download")
async def download_backup(id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", id):
        raise HTTPException(400, "Invalid backup id")
    ensure_schema()
    with connection() as conn:
        row = conn.execute(
            "SELECT pathname,filename FROM backup_exports WHERE id=%s AND workflow_id IS NULL",
            (id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Backup not found")
    from contextlib import AsyncExitStack
    from starlette.background import BackgroundTask

    stack = AsyncExitStack()
    download = await stack.enter_async_context(blob_api().stream_blob(row["pathname"], access="private"))
    return StreamingResponse(
        download,
        media_type=download.metadata.content_type or "application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{row["filename"]}"',
            "X-Content-Type-Options": "nosniff",
        },
        background=BackgroundTask(stack.aclose),
    )


@app.post("/api/workflows/{wid}/backup")
async def create_workflow_backup(wid: str):
    wid = _workflow_id(wid)
    result = await _make_workflow_backup(wid)
    return await _save_backup_export(result, wid)


@app.get("/api/workflows/{wid}/backup/download")
async def download_workflow_backup(wid: str, id: str):
    wid = _workflow_id(wid)
    if not re.fullmatch(r"[0-9a-f]{32}", id):
        raise HTTPException(400, "Invalid backup id")
    ensure_schema()
    with connection() as conn:
        row = conn.execute(
            "SELECT pathname,filename FROM backup_exports WHERE id=%s AND workflow_id=%s",
            (id, wid),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Backup not found")
    from contextlib import AsyncExitStack
    from starlette.background import BackgroundTask

    stack = AsyncExitStack()
    download = await stack.enter_async_context(blob_api().stream_blob(row["pathname"], access="private"))
    return StreamingResponse(
        download,
        media_type=download.metadata.content_type or "application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{row["filename"]}"',
            "X-Content-Type-Options": "nosniff",
        },
        background=BackgroundTask(stack.aclose),
    )


@app.post("/api/restore")
async def disabled_hosted_restore():
    raise HTTPException(501, "Use scripts/import_portable_backup.py for a deliberate hosted migration.")


@app.delete("/api/workflows/{wid}")
async def delete_workflow(wid: str):
    wid = _workflow_id(wid)
    ensure_schema()
    with connection() as conn:
        if not _workflow_row(conn, wid):
            raise HTTPException(404, "Workflow not found")
        objects = conn.execute(
            "SELECT pathname FROM blob_objects WHERE workflow_id=%s", (wid,)
        ).fetchall()
        conn.execute("DELETE FROM workflows WHERE id=%s", (wid,))
    paths = [row["pathname"] for row in objects]
    client = blob_api()
    for start in range(0, len(paths), 1000):
        await client.delete_blob(paths[start:start + 1000])
    return {"ok": True}


@app.get("/{unmatched:path}")
async def not_found(unmatched: str):
    del unmatched
    raise HTTPException(404, "Not found")
