# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

The desktop client for InstaSend. Drop files onto the popup window to share them. Files are served directly over a persistent WebSocket connection to the relay server — no local HTTP server is needed.

## Running

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

On first launch, a setup dialog prompts for the server URL and pre-shared token.
Config is saved to `~/.instasend/config.json`.

## Building executables

```bash
pip install -r requirements-dev.txt
python build.py
```

Output: `dist/instasend.app` (macOS), `dist/instasend.exe` (Windows), `dist/instasend` (Linux).

## Architecture

Four source files:

**`main.py`** — UI and application wiring
- `SetupDialog` — shown on first run when `server_url` or `token` is missing
- `StatusBar` — colored dot + label: Connected / Reconnecting… / No server configured
- `PopupWindow` — frameless `QWidget` (not Tool window, so DnD stays open during Finder drags). Footer shows `StatusBar` (left) and build version (right).
- `ShareList` / `ShareRow` — keyed by `local_id` (not server hash). Rows show "Registering…" until the server sends an `Assigned` frame.
- `InstaSend` — creates all components, runs the Qt event loop. WsClient is created lazily after setup dialog via `_maybe_create_ws_client()`. On macOS, `setContextMenu()` is intentionally NOT called on the tray icon (causes left-click interception); right-click is handled via `ActivationReason.Context`.

**`ws_client.py`** — Persistent WebSocket client
- Runs its own asyncio event loop in a daemon thread
- On connect: sends `Register` (all hashes), then `Add` for pending shares (hash=None)
- Handles `Assigned` frames → calls `ShareManager.assign()`
- Handles `Request` frames → reads file, sends `Response` + binary chunks via `asyncio.create_task` (concurrent, multiplexed by `request_id`)
- Exponential backoff reconnect: 1 s → 2 s → … → 60 s; config errors (4001/4003) back off 120 s
- Thread-safe `send_add(filename)` / `send_remove(hash)` via `asyncio.run_coroutine_threadsafe`

**`shares.py`** — Share management and persistence
- Internal key: `local_id` (UUID hex), stable across reconnects
- Secondary index: `_hash_index` maps server_hash → local_id
- `add(path)` — creates pending share with `hash=None`; no hash is generated locally
- `assign(filename, server_hash)` — matches pending share by filename, sets hash, fires `on_assigned(local_id, server_hash)`
- `remove(local_id)` — returns server hash (caller sends the WS `Remove` frame)
- Persists to `~/.instasend/shares.json` as a list; auto-migrates old dict-keyed format

**`protocol.py`** — Shared with `server/`. Dataclasses + encode/decode for all control frames and binary chunk framing. Do not edit independently — keep in sync with `server/protocol.py`.

## Per-device identity

`client_id` is a random UUID hex stored in `~/.instasend/client_id`, created on first run. It is sent in every `Register` frame so the server can validate hash ownership across reconnects.

## Configuration (`~/.instasend/config.json`)

| Key | Description |
|---|---|
| `server_url` | Relay server base URL (e.g. `https://dl.666.fail`) |
| `public_url` | Optional override for the URL shown in share links |
| `token` | Pre-shared secret — must match the server's `TOKEN` env var. Never bundled with the app. |

## Cross-thread UI updates

Background-thread callbacks (ws_client, ShareManager) must not touch Qt widgets directly. Route updates through `_Signals` (a `QObject` subclass with typed `Signal` fields). The signal→slot connection goes through Qt's event queue, which is thread-safe.
