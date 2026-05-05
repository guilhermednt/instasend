# InstaSend

Drop a file, get a link. InstaSend is a self-hosted file-sharing tool with two parts:

- **Desktop app** — a macOS/Linux/Windows menu-bar app. Drop a file onto the popup to share it. The app streams files directly over a persistent WebSocket connection to the relay server; no port-forwarding or local HTTP server is needed.
- **Relay server** — a lightweight FastAPI server that routes download requests to whichever desktop client owns the requested file. It never stores file data.

```
Browser ──GET /abc12345──► Relay server ──Request WS frame──► Desktop app
                                ◄── Response + binary chunks ──────────────
```

---

## Quick start

### 1. Deploy the server

```bash
cp server/.env.dist server/.env
# Edit server/.env: set TOKEN to a strong random secret
# TOKEN=$(openssl rand -hex 32)

make production   # docker compose up --build -d
make logs         # tail logs
make stop         # bring down
```

The server expects a TLS-terminating reverse proxy in front (nginx, Caddy, etc.) forwarding
to `localhost:8000`. Set `FORWARDED_ALLOW_IPS` in `.env` to the proxy's IP so that
`X-Forwarded-For` headers are trusted for rate limiting.

### 2. Install the desktop app

**From source:**

```bash
cd desktop
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

**Build a distributable executable:**

```bash
pip install -r requirements-dev.txt
python build.py
# Output: dist/instasend.app  (macOS)
#         dist/instasend.exe  (Windows)
#         dist/instasend      (Linux)
```

On first launch a setup dialog prompts for the server URL and token.

---

## Configuration

### Server (`server/.env`)

| Variable | Required | Description |
|---|---|---|
| `TOKEN` | Yes | Pre-shared secret. Must match the desktop's token. Generate with `openssl rand -hex 32`. |
| `DB_PATH` | No | SQLite database path (default: `/data/shares.db`). |
| `FORWARDED_ALLOW_IPS` | No | Comma-separated list of trusted reverse-proxy IPs (default: `127.0.0.1`). |

### Desktop (`~/.instasend/config.json`)

Managed via the setup dialog. Edit directly if needed:

| Key | Description |
|---|---|
| `server_url` | Relay server base URL, e.g. `https://files.example.com`. Must be `https://`. |
| `public_url` | Optional override for the URL shown in share links (e.g. a CDN alias). |
| `token` | Pre-shared secret — must match `TOKEN` on the server. |

---

## How it works

1. The desktop connects to `wss://server/ws` and sends a `Register` frame containing its `client_id` and all known share hashes.
2. The server validates hash ownership against a SQLite database. Valid hashes are re-registered; stale or foreign ones are retired and replaced. An `Assigned` frame confirms every accepted hash.
3. When a user drops a file, the desktop sends `Add(filename)`. The server generates an 8-character alphanumeric hash, stores ownership, and replies with `Assigned(filename, hash)`.
4. A downloader visits `https://server/<hash>`. The server sends a `Request` frame to the desktop, which streams `Response` metadata followed by binary chunks. The server pipes them to the HTTP client as a `StreamingResponse`.
5. Multiple concurrent downloads are multiplexed on the single WebSocket using a `request_id` per request.

Per-device identity (`client_id`) is a random UUID stored in `~/.instasend/client_id`. It lets a reconnecting desktop reclaim its own hashes; no other client can claim them.

---

## Limits

| Limit | Value |
|---|---|
| Active shares per desktop connection | 20 |
| Concurrent WebSocket connections per IP | 3 |
| Concurrent downloads per desktop connection | 20 |
| Download rate limit | 30 requests / minute / IP |
| Hash length | 8 alphanumeric characters (62⁸ ≈ 218 trillion combinations) |

---

## Security

Authentication uses a mutual HMAC-SHA256 challenge-response handshake — the shared token
is never transmitted. Hashes are never reused across clients; a retired hash stays in the
ownership database permanently.

**Important:** Always deploy the server behind a TLS-terminating reverse proxy. The desktop
app should be configured with an `https://` server URL.

---

## Development

The protocol is defined in `desktop/protocol.py` and mirrored in `server/protocol.py` —
keep them in sync. Increment `PROTOCOL_VERSION` in both files for any breaking change; the
server closes mismatched clients with code `4003`.

Run the server locally for development:

```bash
cd server
pip install -r requirements.txt
TOKEN=<your-token> uvicorn main:app --reload
```
