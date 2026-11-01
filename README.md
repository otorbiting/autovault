# AutoVault

AutoVault is a local-first library for collecting and maintaining automation workflows. It runs on Python's standard library, stores workflow files in ordinary folders, keeps catalog metadata in SQLite, and uses a private local Git repository inside each workflow folder for snapshots and restore.

## Start

### Docker (recommended)

Requirements: Docker Desktop with Compose.

```sh
docker compose up --build -d
```

Open [http://127.0.0.1:8766](http://127.0.0.1:8766). On macOS, double-click `Start AutoVault.command` in the project folder (or the Desktop launcher) to start Docker Desktop if needed, build/start the app, wait for its health check, and open the browser. To make a portable project ZIP with the latest workflows and history, double-click `Export AutoVault Project.command` while AutoVault is running. The container restarts automatically and stores its SQLite database, workflow files, Git snapshots, run history, and backups in the persistent `autovault_data` volume. Compose publishes the service only on `127.0.0.1`. Check startup with `docker compose ps` and logs with `docker compose logs -f autovault`.

To stop the app while keeping its data, run `docker compose down`. To move the library to another device, run `Export AutoVault Project.command` to refresh `outputs/autovault-project-with-data.zip`. On the destination device, unzip it, start AutoVault, choose **RESTORE** in the library page, and select `portable-data/autovault-data-backup.tar.gz`. The backup includes workflow files, Git versions, metadata, notes, and run history. The live library is saved in Docker’s persistent volume; the ZIP is a point-in-time copy, so export again after changes before moving devices. To remove the stored library as well, run `docker compose down -v`.

Set `AUTOVAULT_PORT=9000` in the shell before `docker compose up` to use a different host port. The default is 8766 so it can run alongside an existing non-Docker AutoVault instance on 8765.

### Run directly

Requirements: Python 3.9+ and Git. JavaScript execution also requires Node.js. No Python package installation is needed.

```sh
python3 autovault/app.py
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). To choose another local port, run `python3 autovault/app.py --port 9000`. AutoVault refuses non-local bind addresses. Set `AUTOVAULT_HOME=/path/to/data` before starting to choose where the library and database live.

## What it supports

- Create and import n8n JSON, Python, JavaScript, shell, Docker Compose, AI-agent configuration, and generic files. Import a `.zip` project archive to keep its folder structure and files together.
- Search by name, description, and tags; filter by type or maintenance status.
- Edit files, save named snapshots, compare Git revisions, and restore a selected revision. Restore adds the resulting state as a new snapshot.
- Add maintenance notes and update the name, description, status, or tags.
- Make a downloadable compressed backup of SQLite metadata and workflow files; restore it through the UI.
- Explicit local runs for Python, JavaScript (`.js`, `.mjs`, `.cjs`) and shell scripts. Each run requires checking the approval box. Output, errors, exit codes, duration, and recent run history are shown in the UI, with a configurable timeout (up to five minutes).

Docker Compose, AI-agent and n8n workflows are stored and versioned, but AutoVault does not attempt to start or control those external runtimes. TypeScript is cataloged but not executed. Scripts run inside the local container as an unprivileged user with a reduced environment and can access the container's network. This is not a security sandbox; only run scripts you trust. The host port stays bound to localhost. Hosted Vercel deployments never execute scripts.

## Data layout

With Docker, the named volume `autovault_data` is mounted at `/data` and contains `autovault.sqlite3`, `workflows/<workflow-id>/`, Git histories, run records, and backups. Direct runs use `~/.autovault/` by default or the path in `AUTOVAULT_HOME`. Each workflow directory is a normal folder with its own `.git` history.

## Tests

The local app itself needs no Python package installation. The hosted backend tests need the development dependencies:

```sh
python3 -m pip install -r requirements-dev.txt
python3 -m unittest discover -s tests -v
```

The tests use temporary data directories and do not modify your real library.

## Vercel hosted deployment

The Vercel deployment uses Python 3.12 and FastAPI Functions. The local Docker and direct-run modes continue to use the standard-library HTTP server, SQLite, the filesystem, and Git. The hosted mode stores catalog, notes, run history, and version manifests in Neon Postgres; workflow file contents and portable backup exports live in a **private** Vercel Blob store.

1. Import `otorbiting/autovault` as a Vercel project.
2. Connect a Neon Postgres database through the Vercel Marketplace and make its `DATABASE_URL` available to the project.
3. Create and connect a **private** Vercel Blob store. The hosted runtime uses Vercel OIDC credentials supplied to the connected project.
4. Add these Vercel environment variables for Production and Preview:
   - `AUTOVAULT_ACCESS_PASSWORD`: shared login password, at least 16 characters.
   - `AUTOVAULT_SESSION_SECRET`: random signing secret, at least 32 characters. Keep it stable to preserve active sessions across deployments.
   - `DATABASE_URL`: Neon Postgres connection string (usually supplied by the Neon integration).
5. Deploy from Vercel. The app creates its Postgres schema on first authenticated data request.

For example, generate independent values locally and add them directly in Vercel project settings:

```sh
openssl rand -base64 32
openssl rand -hex 32
```

The app uses an eight-hour, `Secure`, `HttpOnly`, `SameSite=Strict` session cookie. The login form is the only unauthenticated page; other pages, static assets, API routes, and file/backup reads require a valid session. Hosted script execution always returns `403`, even if a caller sends an approval flag. Imported run history remains viewable. Hosted JSON requests are capped at 3.5 MB; text file imports are capped at 1 MB and ZIP imports at 2.5 MB compressed.

### One-time portable data import

The importer accepts either `portable-data/autovault-data-backup.tar.gz` or the whole project ZIP that contains it. It validates the SQLite database, archive paths and Git history, then uploads workflow objects and historical snapshots to the private Blob store while inserting catalog, notes and run records into Neon. It refuses to overwrite a non-empty hosted catalog. The backup is read directly from the supplied path and is not copied into Git or the deployment bundle.

Run the import from a machine that has Python 3.10 or later (3.12 recommended), Git, and the project dependencies. Use the Neon connection string and a Vercel Blob read-write token from your own account; never commit either value:

```sh
python3 -m pip install -r requirements.txt
export DATABASE_URL='...'
export BLOB_READ_WRITE_TOKEN='...'
python3 scripts/import_portable_backup.py /path/to/autovault-project-with-data.zip
```

The Vercel Blob token is only needed by this one-time local importer; the deployed app uses the connected private store's Vercel OIDC credentials. The command uploads data only when explicitly run. Hosted restore through the browser is disabled; use this importer only against an empty Neon database. Re-running after a successful import will stop rather than overwrite existing workflows.

Hosted backup downloads contain a local-compatible SQLite catalog, the current workflow files, and stored version snapshots. Restoring one in the local app rebuilds its Git history. Local script execution remains available only in the local Docker/direct-run modes.

## Project structure

```text
autovault/app.py          Local Python HTTP server, SQLite, Git and execution API
autovault/hosted.py       Authenticated Vercel Functions API with Neon/Blob storage
autovault/migration.py    Safe portable backup validation and hosted migration
autovault/static/         Browser interface and hero artwork
Dockerfile                Local app image with Python, Git and Node.js
compose.yaml              Loopback-only service and persistent data volume
index.py                  Vercel FastAPI entrypoint
scripts/import_portable_backup.py  Deliberate one-time hosted data import
vercel.json               Vercel function configuration and data exclusions
Start AutoVault.command    macOS one-click Docker startup and browser launcher
Export AutoVault Project.command  Refreshes a portable ZIP with the latest saved data
portable-data/             Local portable data snapshot; excluded from Git
tests/                    Local and hosted backend tests
```

## Limitations

The browser editor edits text files up to 500 KB one file at a time. Local ZIP import is limited to 500 files and 100 MB expanded; hosted ZIP imports additionally have a 2.5 MB compressed upload limit. Hosted restore through the browser is disabled in favor of the explicit one-time migration tool. Generic files can be stored and versioned without being executable.
