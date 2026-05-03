import ipaddress
import os
from contextlib import asynccontextmanager

import aiosqlite
import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

DB_PATH = os.environ.get("DB_PATH", "shares.db")
INTERNAL_NETWORK = ipaddress.IPv4Network(
    os.environ.get("INTERNAL_NETWORK", "10.0.0.0/24")
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS shares (
                hash     TEXT PRIMARY KEY,
                client_ip TEXT NOT NULL,
                port     INTEGER NOT NULL,
                filename TEXT NOT NULL
            )
        """)
        await db.commit()
    yield


app = FastAPI(lifespan=lifespan)


# ---------------------------------------------------------------------------
# Internal-network guard (S4)
# ---------------------------------------------------------------------------

def require_internal(request: Request):
    """Dependency that rejects requests from outside INTERNAL_NETWORK."""
    raw = request.client.host if request.client else ""
    try:
        ip = ipaddress.IPv4Address(raw)
    except ValueError:
        raise HTTPException(status_code=403, detail="Forbidden")
    if ip not in INTERNAL_NETWORK:
        raise HTTPException(status_code=403, detail="Forbidden")


# ---------------------------------------------------------------------------
# Management endpoints (internal only)
# ---------------------------------------------------------------------------

class ShareIn(BaseModel):
    hash: str
    port: int
    filename: str


@app.post("/shares", status_code=201, dependencies=[Depends(require_internal)])
async def register_share(body: ShareIn, request: Request):
    """Register (or update) a share. Client IP is read from the connection."""
    client_ip = request.client.host
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO shares (hash, client_ip, port, filename)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(hash) DO UPDATE SET
                client_ip = excluded.client_ip,
                port      = excluded.port,
                filename  = excluded.filename
            """,
            (body.hash, client_ip, body.port, body.filename),
        )
        await db.commit()
    return {"hash": body.hash, "client_ip": client_ip}


@app.delete("/shares/{hash}", status_code=204, dependencies=[Depends(require_internal)])
async def delete_share(hash: str):
    """Remove a share entirely. Future GET requests for this hash return 404."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM shares WHERE hash = ?", (hash,))
        await db.commit()


# ---------------------------------------------------------------------------
# Public download endpoint (S7)
# ---------------------------------------------------------------------------

# Headers the upstream sends that we must not blindly forward to the client
# (they describe the upstream connection, not the proxied one).
_HOP_BY_HOP = {
    "connection", "keep-alive", "transfer-encoding",
    "te", "trailer", "upgrade", "proxy-authorization", "proxy-authenticate",
}

PROXY_TIMEOUT = httpx.Timeout(10.0, read=None)  # no read timeout for large files


@app.get("/{hash}")
async def download(hash: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT client_ip, port, filename FROM shares WHERE hash = ?", (hash,)
        ) as cursor:
            row = await cursor.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Not found")

    client_ip, port, filename = row
    upstream = f"http://{client_ip}:{port}/{hash}"

    async def stream_upstream():
        async with httpx.AsyncClient(timeout=PROXY_TIMEOUT) as client:
            async with client.stream("GET", upstream) as resp:
                async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                    yield chunk

    # Probe the upstream first to get status + headers, then stream the body.
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            head = await client.head(upstream)
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="File host unreachable")
    except httpx.TimeoutException:
        raise HTTPException(status_code=503, detail="File host timed out")

    if head.status_code == 404:
        raise HTTPException(status_code=404, detail="Not found")

    headers = {
        k: v for k, v in head.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }

    return StreamingResponse(
        stream_upstream(),
        status_code=200,
        headers=headers,
        media_type=head.headers.get("content-type", "application/octet-stream"),
    )
