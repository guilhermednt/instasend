# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

The desktop client for InstaSend. Drop files onto the window to share them. Each file gets a 6-character alphanumeric hash and is served by a built-in HTTP server. Optionally registers shares with the InstaSend server (`server/`) so they're accessible via a public short URL.

## Running

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

## Building executables

```bash
pip install -r requirements-dev.txt
python build.py
```

| Platform | Output | config.json location |
|---|---|---|
| macOS | `dist/instasend.app` | same folder as the `.app` bundle |
| Windows | `dist\instasend.exe` | same folder as `.exe` |
| Linux | `dist/instasend` | same folder as the binary |

## Configuration

`config.json` (next to the executable, or next to the `.app` on macOS):

| Key | Default | Description |
|---|---|---|
| `file_server_port` | `8081` | Port the built-in HTTP server listens on |
| `server_url` | `""` | Public InstaSend server URL (e.g. `https://dl.666.fail`). If empty, the displayed URL uses the local IP. |

## Architecture

Three source files:

**`main.py`** — UI and wiring
- `MainWindow(QMainWindow)` handles drag-and-drop for the entire window; `DropZone` is visual only
- `ShareList` manages a scrollable list of `ShareRow` widgets (one per active share)
- `InstaSend` ties everything together: loads config, starts `FileServer`, loads `ShareManager`, populates UI
- Cross-thread UI updates (download counts from the HTTP server thread) go through `_Signals`

**`file_server.py`** — HTTP server
- `FileServer` runs a `ThreadingHTTPServer` in a daemon thread
- Serves `GET /<hash>` and `HEAD /<hash>`; calls `share_manager.record_download()` after each completed GET
- Files are served directly from their original path — never copied

**`shares.py`** — Share management
- `ShareManager` owns the shares dict (hash → `{path, filename, downloads}`)
- Persists to `shares.json`; stale entries (file no longer on disk) are dropped on load
- Server API calls (`POST /shares`, `DELETE /shares/<hash>`) run in background threads so they never block the UI
- `on_download` callback notifies the UI when a download count changes
- `startup_sync()` re-registers all persisted shares with the server after a restart
