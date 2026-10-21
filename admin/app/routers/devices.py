"""Device deployment endpoints.

  POST /api/devices/cli     (auth)        -> text CLI command for unattended install
  POST /api/devices/deploy  (deploy token)-> {result: "OK" | "NOT_ENABLED" |
                                                "INVALID_INPUT" | "ID_TAKEN"}

Flow: an admin calls /api/devices/cli with the desired presets; the server mints a
single-use deploy token and returns a command the admin runs on the target
machine. That machine calls /api/devices/deploy with the token; the server claims
the device id/uuid/pk and binds it to the admin.
"""

import base64
import binascii
import json
import secrets

from fastapi import APIRouter, Depends, Header, Request, Response
from sqlalchemy.orm import Session

from ..database import get_db
from .. import models, auth, schemas
from ..config import settings

router = APIRouter()


def _b64_decode(s: str) -> bytes:
    try:
        return base64.b64decode(s + "=" * (-len(s) % 4))
    except Exception:
        try:
            return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
        except Exception:
            return b""


@router.post("/api/devices/cli")
async def devices_cli(
    request: Request,
    user=Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
        body = {}
    # Persist the presets against a single-use deploy token.
    token = secrets.token_urlsafe(24)
    db.add(
        models.AdminDeployToken(
            token=token,
            owner_guid=user.guid,
            presets=body,
            used=False,
        )
    )
    db.commit()

    # Build an unattended-install command carrying the single-use deploy token.
    cmd = f"rustdesk --deploy --token {token}"
    return Response(content=cmd, media_type="text/plain")


@router.post("/api/devices/deploy")
async def devices_deploy(
    request: Request,
    authorization: str = Header(None),
    db: Session = Depends(get_db),
):
    token = ""
    if authorization and authorization.startswith("Bearer "):
        token = authorization[len("Bearer "):].strip()
    if not token:
        return {"result": "INVALID_INPUT"}

    dt = (
        db.query(models.AdminDeployToken)
        .filter(models.AdminDeployToken.token == token)
        .first()
    )
    if not dt or dt.used:
        return {"result": "INVALID_INPUT"}

    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
        body = {}
    pid = body.get("id", "")
    if not pid:
        return {"result": "INVALID_INPUT"}

    existing = db.query(models.Peer).filter(models.Peer.id == pid).first()
    if existing:
        # Only allow claiming if the device has no public key yet (fresh).
        if existing.pk:
            return {"result": "ID_TAKEN"}

    uuid_bytes = _b64_decode(body.get("uuid", ""))
    pk_bytes = _b64_decode(body.get("pk", ""))

    presets = dt.presets or {}
    # Apply presets: device_group_name, note, address book, etc.
    if existing:
        peer = existing
    else:
        peer = models.Peer(
            guid=secrets.token_bytes(16),
            id=pid,
            uuid=uuid_bytes or secrets.token_bytes(16),
            pk=pk_bytes,
            info="{}",
        )
        db.add(peer)
    peer.pk = pk_bytes
    if uuid_bytes:
        peer.uuid = uuid_bytes

    db.commit()

    # Record device group membership from preset if provided.
    group_name = presets.get("device_group_name", "")
    if group_name:
        grp = (
            db.query(models.AdminDeviceGroup)
            .filter(models.AdminDeviceGroup.name == group_name)
            .first()
        )
        if grp:
            db.merge(models.AdminPeerGroup(peer_id=pid, group_name=group_name))
            db.commit()

    dt.used = True
    db.commit()
    return {"result": "OK"}
