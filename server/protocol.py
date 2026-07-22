"""
InstaSend WebSocket Protocol
============================

All control messages are UTF-8 JSON text frames.
File chunk data is sent as binary frames.

Binary frame layout
-------------------
  Bytes 0-31  : request_id (ASCII, right-padded with spaces to 32 bytes)
  Bytes 32+   : raw chunk data

Authentication handshake
------------------------
On every new WebSocket connection the server initiates a mutual
challenge-response handshake before any other traffic is exchanged:

  1. Server → client: challenge  {nonce, server_digest}
     server_digest = HMAC-SHA256(key=token, msg="server:" + nonce)

  2. Client verifies server_digest to confirm the server knows the shared
     token (protection against MitM / rogue servers).

  3. Client → server: register  {version, digest, client_id, hashes}
     digest = HMAC-SHA256(key=token, msg="client:" + nonce)

  4. Server verifies digest. The token itself is never transmitted.

Control message reference
-------------------------
Desktop → server:

  register   Sent immediately after the server's `challenge`. Authenticates
             the client and lists all currently stored hashes. The server
             validates ownership of each hash; any hash that is unowned or
             owned by a different client_id is retired and a new one is
             assigned via `assigned`.
             {type, version, digest, client_id, hashes: [{hash, filename}]}

  add        Sent when the user shares a new file. The server generates the
             hash, stores ownership, and replies with `assigned`.
             {type, filename}

  remove     Sent when the user removes a share. The hash is removed from
             the live registry but remains in the DB so it can never be
             reassigned to another client.
             {type, hash}

  response   Sent after receiving a `request`. Announces transfer metadata
             and signals that binary chunk frames will follow.
             {type, request_id, status, filename, size}

  error      Sent instead of `response` when the file cannot be served.
             {type, request_id, status}   (status: 404 | 503)

Server → desktop:

  challenge  Sent immediately after the WebSocket connection is accepted,
             before any client frame. The client must verify `server_digest`
             to confirm the server knows the shared token, then reply with
             a `register` frame containing the matching client digest.
             {type, nonce, server_digest}

  assigned   Sent in response to `add`, and also during `register` for any
             hash that could not be verified (unowned, or owned by a
             different client). The desktop must update its local store.
             {type, filename, hash}

  request    Sent when a downloader requests a file. The desktop must reply
             with `response` + binary chunks, or `error`.
             {type, request_id, hash}

  cancel     Sent when the downloader disconnects before the transfer
             finished. The desktop must stop streaming chunks for this
             request_id.
             {type, request_id}

Close codes
-----------
  4001  Authentication failed (bad or missing token / digest mismatch)
  4002  Protocol error (malformed message)
  4003  Protocol version mismatch
"""

import hashlib
import hmac
import json
from dataclasses import asdict, dataclass, field

# Increment whenever a breaking change is made to the protocol.
# Server closes with 4003 if the client sends a different version.
PROTOCOL_VERSION = 2

# Width of the request_id prefix in binary frames (bytes).
# request_ids are hex UUIDs without dashes (32 ASCII chars).
REQUEST_ID_BYTES = 32


# ---------------------------------------------------------------------------
# Control messages
# ---------------------------------------------------------------------------

@dataclass
class Challenge:
    nonce: str          # random hex string (64 chars / 32 bytes of entropy)
    server_digest: str  # HMAC-SHA256(key=token, msg="server:" + nonce)
    type: str = field(default="challenge", init=False)


@dataclass
class Register:
    version: int
    digest: str                 # HMAC-SHA256(key=token, msg="client:" + nonce)
    client_id: str              # stable per-device identity (random UUID, persisted locally)
    hashes: list[dict]          # [{"hash": "...", "filename": "..."}]
    type: str = field(default="register", init=False)


@dataclass
class Add:
    filename: str               # hash is assigned by the server, not the client
    type: str = field(default="add", init=False)


@dataclass
class Remove:
    hash: str
    type: str = field(default="remove", init=False)


@dataclass
class Assigned:
    filename: str
    hash: str
    type: str = field(default="assigned", init=False)


@dataclass
class Request:
    request_id: str
    hash: str
    type: str = field(default="request", init=False)


@dataclass
class Response:
    request_id: str
    status: int
    filename: str
    size: int
    type: str = field(default="response", init=False)


@dataclass
class Error:
    request_id: str
    status: int                 # 404 or 503
    type: str = field(default="error", init=False)


@dataclass
class Cancel:
    request_id: str
    type: str = field(default="cancel", init=False)


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

_TYPES = {
    "challenge": Challenge,
    "register":  Register,
    "add":       Add,
    "remove":    Remove,
    "assigned":  Assigned,
    "request":   Request,
    "response":  Response,
    "error":     Error,
    "cancel":    Cancel,
}

_FIELDS = {
    "challenge": ("nonce", "server_digest"),
    "register":  ("version", "digest", "client_id", "hashes"),
    "add":       ("filename",),
    "remove":    ("hash",),
    "assigned":  ("filename", "hash"),
    "request":   ("request_id", "hash"),
    "response":  ("request_id", "status", "filename", "size"),
    "error":     ("request_id", "status"),
    "cancel":    ("request_id",),
}


def encode(msg) -> str:
    """Serialise a control message dataclass to a JSON string."""
    return json.dumps(asdict(msg))


def decode(raw: str):
    """Parse a JSON string into the appropriate control message dataclass.

    Raises ValueError for unknown or malformed messages.
    """
    try:
        data = json.loads(raw)
        t = data["type"]
        cls = _TYPES[t]
        return cls(**{k: data[k] for k in _FIELDS[t]})
    except (KeyError, TypeError) as exc:
        raise ValueError(f"Malformed message: {exc}") from exc


# ---------------------------------------------------------------------------
# Authentication helper
# ---------------------------------------------------------------------------

def auth_digest(token: str, message: str) -> str:
    """HMAC-SHA256(key=token, msg=message) as a lowercase hex string."""
    return hmac.new(token.encode(), message.encode(), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Binary frame helpers
# ---------------------------------------------------------------------------

def encode_chunk(request_id: str, data: bytes) -> bytes:
    """Pack (request_id, chunk) into a binary WebSocket frame."""
    header = request_id.encode("ascii").ljust(REQUEST_ID_BYTES)[:REQUEST_ID_BYTES]
    return header + data


def decode_chunk(frame: bytes) -> tuple[str, bytes]:
    """Unpack a binary WebSocket frame into (request_id, chunk)."""
    request_id = frame[:REQUEST_ID_BYTES].rstrip(b" ").decode("ascii")
    chunk = frame[REQUEST_ID_BYTES:]
    return request_id, chunk
