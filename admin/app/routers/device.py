"""Device sync endpoints called by every client session:

  * POST /api/heartbeat       - liveness ping; may push strategy / force sysinfo
  * POST /api/sysinfo         - device hardware/os snapshot (stored in peer.info)
  * GET  /api/sysinfo_ver     - server-side sysinfo version marker
  * POST /api/record          - session recording chunk upload
  * POST /api/switch-grant    - device switch grant registration

These endpoints are intentionally unauthenticated (the device id/uuid is the
identity), matching the upstream RustDesk design.
"""

import base64
import json
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from ..database import get_db
from .. import models
from ..config import settings

router = APIRouter()


def _now():
    return datetime.now(timezone.utc)


def _b64_decode(s: str) -> bytes:
    try:
        return base64.b64decode(s + "=" * (-len(s) % 4))
    except Exception:
        try:
            return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
        except Exception:
            return b""


@router.post("/api/heartbeat")
async def heartbeat(request: Request, db: Session = Depends(get_db)):
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
        body = {}
    pid = body.get("id", "")
    if pid:
        row = (
            db.query(models.AdminPeerStatus)
            .filter(models.AdminPeerStatus.id == pid)
            .first()
        )
        if not row:
            row = models.AdminPeerStatus(id=pid)
            db.add(row)
        row.last_heartbeat = _now()
        row.conns = int(body.get("conns", 0) or 0)
        try:
            row.modified_at = int(body.get("modified_at", 0) or 0)
        except Exception:
            row.modified_at = 0
        db.commit()
    # Default: nothing to do. Optionally the server can push `strategy`,
    # force `sysinfo` re-upload, request `disconnect`, or bump `modified_at`.
    return {}


@router.post("/api/sysinfo")
async def sysinfo(request: Request, db: Session = Depends(get_db)):
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
        body = {}
    pid = body.get("id", "")
    if not pid:
        return Response(content="ID_NOT_FOUND", media_type="text/plain")

    uuid_bytes = _b64_decode(body.get("uuid", ""))
    info_json = json.dumps(body, ensure_ascii=False)

    peer = db.query(models.Peer).filter(models.Peer.id == pid).first()
    if peer is None:
        # Device may not yet be registered with hbbs; create a minimal row so it
        # shows up in the peer list. pk/guid are filled in at registration time.
        guid = os.urandom(16)
        peer = models.Peer(
            guid=guid,
            id=pid,
            uuid=uuid_bytes or os.urandom(16),
            pk=b"",
            info=info_json,
        )
        db.add(peer)
    else:
        peer.info = info_json
        if uuid_bytes:
            peer.uuid = uuid_bytes

    # Remember the sysinfo version hash for incremental uploads.
    status = (
        db.query(models.AdminPeerStatus)
        .filter(models.AdminPeerStatus.id == pid)
        .first()
    )
    if not status:
        status = models.AdminPeerStatus(id=pid)
        db.add(status)
    status.sysinfo_version = body.get("version", "")
    status.last_heartbeat = _now()
    db.commit()
    return Response(content="SYSINFO_UPDATED", media_type="text/plain")


@router.get("/api/sysinfo_ver")
def sysinfo_ver():
    # Empty string: the client only uses this for change detection; we always
    # accept full uploads so no server-side version is tracked.
    return Response(content="", media_type="text/plain")


@router.post("/api/record")
async def record(request: Request, db: Session = Depends(get_db)):
    params = dict(request.query_params)
    typ = params.get("type", "")
    filename = params.get("file", "")
    if not filename:
        return {}
    safe_name = os.path.basename(filename)
    os.makedirs(settings.record_dir, exist_ok=True)
    path = os.path.join(settings.record_dir, safe_name)
    body = await request.body()

    if typ == "new":
        open(path, "wb").close()
    elif typ in ("part", "tail"):
        offset = int(params.get("offset", 0) or 0)
        mode = "r+b" if os.path.exists(path) else "wb"
        with open(path, mode) as f:
            f.seek(offset)
            f.write(body)
    elif typ == "remove":
        if os.path.exists(path):
            os.remove(path)
    return {}


@router.post("/api/switch-grant")
async def switch_grant(request: Request, db: Session = Depends(get_db)):
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
        body = {}
    verifier = body.get("switch_code_verifier", "")
    device_id = body.get("id", "")
    signature_b64 = body.get("signature", "")
    timestamp = body.get("timestamp", "")

    if not verifier:
        return {"accepted": False}

    # Verify the Ed25519 signature over:
    #   "switch-grant\0<id>\0<verifier>\0<timestamp>"
    # using the device's public key (stored as peer.pk).
    msg = b"switch-grant\0" + device_id.encode() + b"\0" + verifier.encode() + b"\0" + timestamp.encode()
    valid = False
    try:
        peer = db.query(models.Peer).filter(models.Peer.id == device_id).first()
        if peer and peer.pk:
            from nacl.signing import VerifyKey

            vk = VerifyKey(bytes(peer.pk))
            vk.verify(msg, base64.b64decode(signature_b64))
            valid = True
    except Exception:
        valid = False

    if not valid:
        # Return server_time so the client retries once with the corrected clock.
        return {"accepted": False, "server_time": int(_now().timestamp())}

    # Enforce a small time window to avoid replay.
    try:
        ts = int(timestamp)
    except Exception:
        ts = 0
    if abs(ts - int(_now().timestamp())) > 300:
        return {"accepted": False, "server_time": int(_now().timestamp())}

    row = (
        db.query(models.AdminSwitchGrant)
        .filter(models.AdminSwitchGrant.switch_code_verifier == verifier)
        .first()
    )
    if not row:
        row = models.AdminSwitchGrant(
            switch_code_verifier=verifier, device_id=device_id, granted=True
        )
        db.add(row)
        db.commit()
    return {"accepted": True}
