# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

InstaSend is a file-sharing system with two independent components:

- **`desktop/`** — PySide6 GUI app. Drag a file onto the window to share it. Runs a built-in HTTP server; optionally registers shares with the public server.
- **`server/`** — FastAPI public relay server. Holds a hash→(IP, port) registry and reverse-proxies download requests to the desktop client.

## Running the server

```bash
make production    # build and start in Docker (production)
make stop          # stop
make logs          # tail logs
```

Or locally:
```bash
cd server
pip install -r requirements.txt
uvicorn main:app --reload
```

## Running the desktop app

```bash
cd desktop
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

## Building desktop executables

```bash
cd desktop
pip install -r requirements-dev.txt
python build.py
```

Output is in `desktop/dist/`. On macOS: `instasend.app`; on Windows: `instasend.exe`; on Linux: `instasend`.

## Architecture

### Desktop (`desktop/`)

Three source files:

- **`main.py`** — UI. `MainWindow` handles DnD for the whole window; `DropZone` is visual only. `ShareList` manages `ShareRow` widgets. `InstaSend` ties everything together.
- **`file_server.py`** — `ThreadingHTTPServer` in a daemon thread. Serves `GET /<hash>` and `HEAD /<hash>` directly from the original file path.
- **`shares.py`** — `ShareManager`: hash generation, `shares.json` persistence, server API sync (POST/DELETE run in background threads to never block the UI).

### Server (`server/`)

Single file `main.py` — FastAPI app with:
- `POST /shares` and `DELETE /shares/{hash}` — management endpoints, restricted to `INTERNAL_NETWORK` (default `10.0.0.0/24`)
- `GET /{hash}` — probes the desktop client with HEAD, then streams the file body back to the requester
- SQLite via aiosqlite; DB path set by `DB_PATH` env var

### Data flow

1. Desktop drops file → `ShareManager.add()` → background thread POSTs to server
2. Someone visits `https://dl.666.fail/<hash>` → server looks up IP+port → HEAD probe → streams response
3. When desktop removes a share → background thread DELETEs from server

## Configuration (`desktop/config.json`)

| Key | Default | Description |
|---|---|---|
| `file_server_port` | `8081` | Port the desktop HTTP server listens on |
| `server_url` | `""` | Internal URL to the relay server (e.g. `http://10.0.0.13:8000`) |
| `public_url` | `""` | Public-facing base URL shown in share links (e.g. `https://dl.666.fail`) |

## Server environment variables

| Variable | Default | Description |
|---|---|---|
| `DB_PATH` | `shares.db` | SQLite database path |
| `INTERNAL_NETWORK` | `10.0.0.0/24` | CIDR range allowed to call management endpoints |
