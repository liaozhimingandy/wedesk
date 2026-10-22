"""Business endpoints that ride on the device PKI.

===================  =====  ==================================================
endpoint             mode   purpose
===================  =====  ==================================================
POST /api/device/heartbeat      mTLS  device liveness + connection count
POST /api/device/report         mTLS  device telemetry / sysinfo push
GET  /api/device/commands       mTLS  device pulls pending commands
POST /api/device/command-result mTLS  device reports a command outcome
GET  /api/app/devices           mTLS  app lists devices it may control
GET  /api/app/device/{id}       mTLS  app queries one device's status
POST /api/app/control           mTLS  app enqueues a control command
===================  =====  ==================================================

These are the *reference* shapes for the mTLS surface: every route gets its
identity from ``mtls.require_leaf(kind)``, which enforces (a) chain-of-trust
against our CA, (b) a live, non-revoked database record, and (c) a
proof-of-possession signature. Adding a new business endpoint is therefore just
``Depends(mtls.require_leaf("device"))`` away — no bespoke auth code.

hbbs/hbbr and the clients are untouched by this module; it is purely the admin
side of the future P2P end-to-end encryption path.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from .. import models, mtls
from ..config import settings
from ..database import get_db
from ..models import as_aware

router = APIRouter()

device_leaf = mtls.require_leaf("device")
app_leaf = mtls.require_leaf("app")


class HeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    conns: int = 0
    sysinfo_version: str = ""
    modified_at: int = 0


class CommandResultRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    guid: str = ""
    status: str = "done"  # done | failed
    result: str = ""


class ControlRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    device_id: str = ""
    cmd: str = ""
    payload: dict = {}


def _touch(db: Session, device_id: str, **fields) -> models.AdminPeerStatus:
    row = db.get(models.AdminPeerStatus, device_id)
    if row is None:
        row = models.AdminPeerStatus(id=device_id)
        db.add(row)
    for key, value in fields.items():
        if value is not None:
            setattr(row, key, value)
    db.flush()
    return row


# ---------------------------------------------------------------------------
# /api/device/*  -- hardware, mTLS with a device certificate
# ---------------------------------------------------------------------------

@router.post("/api/device/heartbeat")
async def device_heartbeat(
    body: HeartbeatRequest,
    identity: mtls.LeafIdentity = Depends(device_leaf),
    db: Session = Depends(get_db),
):
    utcnow = datetime.now(timezone.utc)
    row = _touch(
        db,
        identity.device_id,
        last_heartbeat=utcnow,
        conns=body.conns,
        sysinfo_version=body.sysinfo_version or None,
        modified_at=body.modified_at or None,
    )
    db.commit()
    return {
        "result": "OK",
        "id": identity.device_id,
        "server_time": utcnow.isoformat(),
        "last_heartbeat": as_aware(row.last_heartbeat).isoformat(),
        "cert_days_remaining": _days_remaining(identity.record),
        "should_renew": _should_renew(identity.record),
    }


@router.post("/api/device/report")
async def device_report(
    request: Request,
    identity: mtls.LeafIdentity = Depends(device_leaf),
    db: Session = Depends(get_db),
):
    """Free-form telemetry push; the payload is echoed back for confirmation."""
    try:
        payload = json.loads(await request.body() or b"{}")
    except Exception:
        payload = {}
    _touch(db, identity.device_id, last_heartbeat=datetime.now(timezone.utc))
    db.commit()
    return {"result": "OK", "id": identity.device_id, "received": payload}


@router.get("/api/device/commands")
def device_commands(
    identity: mtls.LeafIdentity = Depends(device_leaf),
    db: Session = Depends(get_db),
):
    """Long-poll style pull of commands queued by the app."""
    rows = (
        db.query(models.AdminDeviceCommand)
        .filter(
            models.AdminDeviceCommand.device_id == identity.device_id,
            models.AdminDeviceCommand.status == "pending",
        )
        .order_by(models.AdminDeviceCommand.created_at.asc())
        .limit(50)
        .all()
    )
    for row in rows:
        row.status = "sent"
        row.updated_at = datetime.now(timezone.utc)
    db.commit()
    return {
        "result": "OK",
        "commands": [
            {
                "guid": r.guid,
                "cmd": r.cmd,
                "payload": r.payload or {},
                "created_at": as_aware(r.created_at).isoformat() if r.created_at else "",
            }
            for r in rows
        ],
    }


@router.post("/api/device/command-result")
async def device_command_result(
    body: CommandResultRequest,
    identity: mtls.LeafIdentity = Depends(device_leaf),
    db: Session = Depends(get_db),
):
    row = db.get(models.AdminDeviceCommand, body.guid)
    if row is None or row.device_id != identity.device_id:
        raise HTTPException(status_code=404, detail="command not found")
    row.status = body.status if body.status in ("done", "failed") else "done"
    row.result = body.result or ""
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    return {"result": "OK", "guid": row.guid, "status": row.status}


# ---------------------------------------------------------------------------
# /api/app/*  -- mobile app, mTLS with an app certificate
# ---------------------------------------------------------------------------

@router.get("/api/app/devices")
def app_devices(
    identity: mtls.LeafIdentity = Depends(app_leaf),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(models.AdminPeerStatus)
        .order_by(models.AdminPeerStatus.last_heartbeat.desc())
        .limit(200)
        .all()
    )
    out = []
    for r in rows:
        peer = db.query(models.Peer).filter(models.Peer.id == r.id).first()
        out.append(
            {
                "id": r.id,
                "online": _online(r),
                "conns": r.conns or 0,
                "last_heartbeat": as_aware(r.last_heartbeat).isoformat()
                if r.last_heartbeat
                else "",
                "hostname": _hostname(peer),
            }
        )
    return {"result": "OK", "app_id": identity.device_id, "devices": out}


@router.get("/api/app/device/{device_id}")
def app_device_status(
    device_id: str,
    identity: mtls.LeafIdentity = Depends(app_leaf),
    db: Session = Depends(get_db),
):
    status = db.get(models.AdminPeerStatus, device_id)
    peer = db.query(models.Peer).filter(models.Peer.id == device_id).first()
    certs = (
        db.query(models.AdminDeviceCert)
        .filter(
            models.AdminDeviceCert.device_id == device_id,
            models.AdminDeviceCert.cert_type == "device",
        )
        .order_by(models.AdminDeviceCert.issued_at.desc())
        .all()
    )
    return {
        "result": "OK",
        "id": device_id,
        "registered": peer is not None,
        "online": _online(status) if status else False,
        "conns": status.conns if status else 0,
        "last_heartbeat": as_aware(status.last_heartbeat).isoformat()
        if status and status.last_heartbeat
        else "",
        "certificates": [
            {
                "serial": c.serial,
                "status": c.status,
                "not_after": as_aware(c.not_after).isoformat() if c.not_after else "",
            }
            for c in certs
        ],
    }


@router.post("/api/app/control")
async def app_control(
    body: ControlRequest,
    request: Request,
    identity: mtls.LeafIdentity = Depends(app_leaf),
    db: Session = Depends(get_db),
):
    """Enqueue a control command; the target device picks it up on its next pull."""
    device_id = (body.device_id or "").strip()
    if not device_id:
        raise HTTPException(status_code=400, detail="device_id required")
    if not body.cmd.strip():
        raise HTTPException(status_code=400, detail="cmd required")
    if not db.query(models.Peer).filter(models.Peer.id == device_id).first():
        raise HTTPException(status_code=404, detail="device not registered")

    row = models.AdminDeviceCommand(
        device_id=device_id,
        cmd=body.cmd.strip(),
        payload=body.payload or {},
        created_by=identity.device_id,
        status="pending",
    )
    db.add(row)
    db.commit()
    return {"result": "OK", "guid": row.guid, "device_id": device_id, "cmd": row.cmd}


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------

def _days_remaining(record: models.AdminDeviceCert):
    not_after = as_aware(record.not_after)
    if not_after is None:
        return None
    return (not_after - datetime.now(timezone.utc)).days


def _should_renew(record: models.AdminDeviceCert) -> bool:
    remaining = _days_remaining(record)
    return remaining is not None and remaining <= settings.pki_renew_window_days


def _hostname(peer: models.Peer | None) -> str:
    """``peer.info`` is a JSON blob written by the client's sysinfo push."""
    if peer is None or not peer.info:
        return ""
    try:
        info = json.loads(peer.info)
    except Exception:
        return ""
    return str(info.get("hostname", "") or info.get("device_name", "") or "")


def _online(status: models.AdminPeerStatus | None) -> bool:
    if status is None or status.last_heartbeat is None:
        return False
    hb = as_aware(status.last_heartbeat)
    if hb is None:
        return False
    return (datetime.now(timezone.utc) - hb).total_seconds() < 180
