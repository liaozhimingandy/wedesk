"""Audit endpoint.

  POST /api/audit/{typ}   typ in {conn, file, alarm, login, ...}

The client posts a free-form JSON body carrying id/uuid/ip/peer_id/conn_id/info
etc. We persist it into the shared ``connection_log`` table (reused from hbbs)
keyed by event type, so audit data is queryable alongside the server's own logs.
"""

import json

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..database import get_db
from .. import models

router = APIRouter()


@router.post("/api/audit/{typ}")
async def audit(typ: str, request: Request, db: Session = Depends(get_db)):
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {"raw": str(body)}

    detail = json.dumps(body, ensure_ascii=False)
    log = models.ConnectionLog(
        event=typ,
        from_id=body.get("id"),
        from_ip=body.get("ip"),
        to_id=body.get("peer_id") or body.get("to_id"),
        via_relay=body.get("via_relay"),
        detail=detail,
    )
    db.add(log)
    db.commit()
    return {}
