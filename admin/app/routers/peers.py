"""Peer list and device-group endpoints (admin console).

  GET /api/peers                  -> {total, data:[PeerPayload]}
  GET /api/device-group/accessible -> {total, data:[{name}]}

Both honor the same pagination contract the client uses (current / pageSize),
and return ``total`` + ``data`` so the client's paged fetch loop terminates.
"""

import json
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from .. import models, auth
from ..models import as_aware
from ..config import settings

router = APIRouter()

_HEARTBEAT_ONLINE_WINDOW = timedelta(seconds=30)


def _peer_payload(peer: models.Peer, status_row, group_name: str) -> dict:
    try:
        info = json.loads(peer.info) if peer.info else {}
    except Exception:
        info = {}
    if not isinstance(info, dict):
        info = {}
    # The client's PeerPayload.toPeer() reads info['device_name'] for the host
    # name, while the sysinfo payload carries 'hostname'. Mirror it so the UI
    # shows the host name correctly.
    if "device_name" not in info and info.get("hostname"):
        info = {**info, "device_name": info["hostname"]}

    online = False
    if status_row is not None and status_row.last_heartbeat is not None:
        online = as_aware(status_row.last_heartbeat) >= datetime.now(timezone.utc) - _HEARTBEAT_ONLINE_WINDOW

    return {
        "id": peer.id,
        "info": info,
        "status": 1 if online else 0,
        "user": "",
        "user_name": "",
        "device_group_name": group_name or "",
        "note": peer.note or "",
    }


@router.get("/api/peers")
def list_peers(
    current: int = 1,
    pageSize: int = 100,
    accessible: str = "",
    status: str = "",
    user=Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    q = db.query(models.Peer)
    if status == "1":
        # Online only: join admin_peer_status and check recency.
        recent = datetime.now(timezone.utc) - _HEARTBEAT_ONLINE_WINDOW
        q = q.join(
            models.AdminPeerStatus,
            models.AdminPeerStatus.id == models.Peer.id,
        ).filter(models.AdminPeerStatus.last_heartbeat >= recent)
    total = q.count()
    rows = (
        q.order_by(models.Peer.id)
        .offset((max(current, 1) - 1) * pageSize)
        .limit(pageSize)
        .all()
    )

    group_map = {
        g.peer_id: g.group_name
        for g in db.query(models.AdminPeerGroup).all()
    }
    status_map = {s.id: s for s in db.query(models.AdminPeerStatus).all()}

    data = [
        _peer_payload(p, status_map.get(p.id), group_map.get(p.id, "")) for p in rows
    ]
    return {"total": total, "data": data}


@router.get("/api/device-group/accessible")
def device_groups(
    current: int = 1,
    pageSize: int = 100,
    user=Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    total = db.query(models.AdminDeviceGroup).count()
    rows = (
        db.query(models.AdminDeviceGroup)
        .order_by(models.AdminDeviceGroup.name)
        .offset((max(current, 1) - 1) * pageSize)
        .limit(pageSize)
        .all()
    )
    data = [{"name": g.name} for g in rows]
    return {"total": total, "data": data}
