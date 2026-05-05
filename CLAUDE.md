# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

InstaSend is a file-sharing system with two components:

- **`desktop/`** — PySide6 macOS menu-bar app. Drop a file onto the popup to share it. Maintains a persistent WebSocket connection to the relay server; files are streamed directly over that connection to downloaders.
- **`server/`** — FastAPI public relay server. Routes download requests to the connected desktop client over WebSocket. No local file server on the desktop is needed.

## Running the server

```bash
make production    # build and start in Docker
make stop
make logs
```

Or locally:
```bash
cd server
pip install -r requirements.txt
TOKEN=my-secret uvicorn main:app --reload
```

## Running the desktop app

```bash
cd desktop
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

On first launch a setup dialog prompts for the server URL and token; these are saved to `~/.instasend/config.json`.

## Building desktop executables

```bash
cd desktop
pip install -r requirements-dev.txt
python build.py
```

Output is in `desktop/dist/`. On macOS: `instasend.app`.

## Architecture

### Data flow

1. Desktop connects to `wss://server/ws`, sends a `Register` frame with all known hashes and `client_id`.
2. User drops a file → desktop sends `Add(filename)` → server responds with `Assigned(filename, hash)` → desktop stores the server-assigned hash.
3. Someone visits `https://server/<hash>` → server sends `Request(request_id, hash)` to desktop over WebSocket → desktop streams `Response` + binary chunks → server pipes them to the HTTP client as a `StreamingResponse`.
4. Multiple concurrent downloads are multiplexed on the single WebSocket connection using `request_id`.

### Desktop (`desktop/`)

Four source files:

- **`main.py`** — UI and wiring. `PopupWindow` is a frameless `QWidget` (not `Tool` window, so DnD works). `ShareList` manages `ShareRow` widgets keyed by `local_id`. `InstaSend` creates all components and drives the Qt event loop. On first run (no token), shows `SetupDialog`. Status bar in popup footer shows `Connected / Reconnecting… / No server configured`.
- **`ws_client.py`** — `WsClient` runs its own asyncio event loop in a daemon thread. Sends `Register` on connect, `Add` for pending shares, handles incoming `Assigned` and `Request` frames. Serves files concurrently via `asyncio.create_task`. Reconnects with exponential backoff (1 s → 60 s). Thread-safe `send_add()` / `send_remove()` API for the Qt thread.
- **`shares.py`** — `ShareManager` owns the share dict keyed by `local_id` (a UUID), with a secondary `_hash_index` (server_hash → local_id). `add()` creates a pending share with `hash=None`; `assign(filename, server_hash)` wires it up when the server responds. `remove(local_id)` returns the server hash for the WS `Remove` frame. Persists to `~/.instasend/shares.json` (list format; auto-migrates old dict format).
- **`protocol.py`** — shared with `server/`. Dataclasses + encode/decode for all control frames and binary chunk framing.

### Server (`server/`)

Single file `main.py` — FastAPI app with:
- `GET /ws` — WebSocket endpoint. Validates token and protocol version on the first `Register` frame. Handles `Add` / `Remove` frames to keep the live registry up to date. Generates server-side hashes (8-char alphanumeric, checked against the ownership DB so retired hashes are never reused). Sends `Assigned` when a hash is created.
- `GET /{hash}` — HTTP download endpoint. Sends a `Request` frame to the desktop, awaits `Response` metadata, then streams binary chunks back to the HTTP client as a `StreamingResponse`. Concurrent downloads are multiplexed by `request_id`.
- `shares(hash, client_id, filename)` — SQLite ownership table (via aiosqlite). Hashes are never deleted from the DB; they stay retired forever so they can't be reassigned to a different `client_id`.

### Per-device identity

Each desktop generates a random `client_id` on first run and stores it in `~/.instasend/client_id`. The server uses `client_id` to validate hash ownership during reconnect — a reconnecting client can reclaim its own hashes; no other client can.

## Desktop configuration (`~/.instasend/config.json`)

| Key | Default | Description |
|---|---|---|
| `server_url` | `""` | Relay server base URL (e.g. `https://dl.666.fail`) |
| `public_url` | `""` | Optional override for the public-facing URL shown in share links |
| `token` | `""` | Pre-shared secret — must match the server's `TOKEN` env var. Never bundled with the app. |

## Server environment variables (`.env`)

| Variable | Description |
|---|---|
| `TOKEN` | Pre-shared secret the desktop must present in the `Register` frame |
| `DB_PATH` | SQLite database path (default `shares.db`; use `/data/shares.db` in Docker) |
| `FORWARDED_ALLOW_IPS` | IP of the reverse proxy whose `X-Forwarded-For` headers are trusted |

Copy `server/.env.dist` to `server/.env` and fill in values before starting.
