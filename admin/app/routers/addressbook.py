"""Address book endpoints.

  POST /api/ab/settings                 -> {max_peer_one_ab}
  POST /api/ab/personal                 -> personal AB profile (one per user)
  GET  /api/ab/shared/profiles          -> {data, total}
  GET  /api/ab/peers?ab=<guid>          -> {data, total}
  GET  /api/ab/tags/<guid>              -> {data, total}
  POST /api/ab/peer/add/<guid>
  PUT  /api/ab/peer/update/<guid>
  DELETE /api/ab/peer/<guid>
  POST /api/ab/shared/add
  PUT  /api/ab/shared/update/profile
  DELETE /api/ab/shared
  GET  /api/ab/rules?ab=<guid>
  POST /api/ab/rule
  PATCH /api/ab/rule
  DELETE /api/ab/rules
"""

import json

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..database import get_db
from .. import models, auth, schemas

router = APIRouter()


def _personal_ab(db: Session, user: models.AdminUser) -> models.AdminAb:
    ab = (
        db.query(models.AdminAb)
        .filter(
            models.AdminAb.owner_guid == user.guid,
            models.AdminAb.is_shared == False,  # noqa: E712
            models.AdminAb.name == "personal",
        )
        .first()
    )
    if not ab:
        ab = models.AdminAb(
            name="personal",
            owner_guid=user.guid,
            is_shared=False,
        )
        db.add(ab)
        db.commit()
        db.refresh(ab)
    return ab


def _profile(ab: models.AdminAb, owner_name: str, rule: int = 0) -> dict:
    return {
        "guid": ab.guid,
        "name": ab.name,
        "owner": owner_name,
        "note": ab.note or "",
        "rule": rule,
        "info": None,
    }


@router.post("/api/ab/settings")
def ab_settings(
    req: schemas.AbSettingsRequest,
    user=Depends(auth.get_current_user),
):
    # 0 means unlimited peers per address book.
    return {"max_peer_one_ab": 0}


@router.post("/api/ab/personal")
def ab_personal(user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    ab = _personal_ab(db, user)
    return _profile(ab, user.name)


@router.get("/api/ab/shared/profiles")
def ab_shared_profiles(
    user=Depends(auth.get_current_user), db: Session = Depends(get_db)
):
    rows = db.query(models.AdminAb).filter(models.AdminAb.is_shared == True).all()  # noqa: E712
    data = []
    for ab in rows:
        owner = (
            db.query(models.AdminUser)
            .filter(models.AdminUser.guid == ab.owner_guid)
            .first()
        )
        rule_row = (
            db.query(models.AdminAbRule)
            .filter(
                models.AdminAbRule.ab_guid == ab.guid,
                models.AdminAbRule.user_guid == user.guid,
            )
            .first()
        )
        data.append(_profile(ab, owner.name if owner else "", rule_row.rule if rule_row else 0))
    return {"data": data, "total": len(data)}


@router.get("/api/ab/peers")
def ab_peers(ab: str = "", user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    rows = db.query(models.AdminAbPeer).filter(models.AdminAbPeer.ab_guid == ab).all()
    data = [
        {
            "id": p.peer_id,
            "hostname": p.hostname,
            "alias": p.alias,
            "tags": p.tags or [],
        }
        for p in rows
    ]
    return {"data": data, "total": len(data)}


@router.get("/api/ab/tags/{ab_guid}")
def ab_tags(ab_guid: str, user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    rows = db.query(models.AdminAbTag).filter(models.AdminAbTag.ab_guid == ab_guid).all()
    data = [{"name": t.name, "color": t.color} for t in rows]
    return {"data": data, "total": len(data)}


@router.post("/api/ab/peer/add/{ab_guid}")
async def ab_peer_add(
    ab_guid: str, request: Request, user=Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
        body = {}
    peers = body.get("peers", [])
    for p in peers:
        pid = p.get("id") or p.get("peer_id")
        if not pid:
            continue
        existing = (
            db.query(models.AdminAbPeer)
            .filter(
                models.AdminAbPeer.ab_guid == ab_guid,
                models.AdminAbPeer.peer_id == pid,
            )
            .first()
        )
        if existing:
            continue
        db.add(
            models.AdminAbPeer(
                ab_guid=ab_guid,
                peer_id=pid,
                hostname=p.get("hostname", ""),
                alias=p.get("alias", ""),
                tags=p.get("tags", []),
            )
        )
    db.commit()
    return {}


@router.put("/api/ab/peer/update/{ab_guid}")
async def ab_peer_update(
    ab_guid: str, request: Request, user=Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
        body = {}
    for p in body.get("peers", []):
        pid = p.get("id") or p.get("peer_id")
        if not pid:
            continue
        row = (
            db.query(models.AdminAbPeer)
            .filter(
                models.AdminAbPeer.ab_guid == ab_guid,
                models.AdminAbPeer.peer_id == pid,
            )
            .first()
        )
        if not row:
            continue
        if "alias" in p:
            row.alias = p["alias"]
        if "hostname" in p:
            row.hostname = p["hostname"]
        if "tags" in p:
            row.tags = p["tags"]
    db.commit()
    return {}


@router.delete("/api/ab/peer/{ab_guid}")
async def ab_peer_delete(
    ab_guid: str, request: Request, user=Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    try:
        body = json.loads(await request.body() or b"[]")
    except Exception:
        body = []
    ids = body if isinstance(body, list) else body.get("peer_ids", [])
    for pid in ids:
        db.query(models.AdminAbPeer).filter(
            models.AdminAbPeer.ab_guid == ab_guid,
            models.AdminAbPeer.peer_id == pid,
        ).delete()
    db.commit()
    return {}


@router.post("/api/ab/shared/add")
async def ab_shared_add(
    request: Request, user=Depends(auth.get_current_user), db: Session = Depends(get_db)
):
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
        body = {}
    name = body.get("name") or "shared"
    ab = models.AdminAb(
        name=name,
        owner_guid=user.guid,
        is_shared=True,
        note=body.get("note", ""),
    )
    db.add(ab)
    db.commit()
    db.refresh(ab)
    return _profile(ab, user.name)


@router.put("/api/ab/shared/update/profile")
async def ab_shared_update(
    request: Request, user=Depends(auth.get_current_user), db: Session = Depends(get_db)
):
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
        body = {}
    guid = body.get("guid") or body.get("ab")
    ab = db.query(models.AdminAb).filter(models.AdminAb.guid == guid).first()
    if not ab:
        return {"error": "address book not found"}
    if "name" in body:
        ab.name = body["name"]
    if "note" in body:
        ab.note = body["note"]
    db.commit()
    return _profile(ab, user.name)


@router.delete("/api/ab/shared")
async def ab_shared_delete(
    request: Request, user=Depends(auth.get_current_user), db: Session = Depends(get_db)
):
    try:
        body = json.loads(await request.body() or b"[]")
    except Exception:
        body = []
    guids = body if isinstance(body, list) else body.get("ab_guids", [])
    for g in guids:
        db.query(models.AdminAb).filter(models.AdminAb.guid == g).delete()
    db.commit()
    return {}


# ---------------------------------------------------------------------------
# AB sharing rules
# ---------------------------------------------------------------------------
@router.get("/api/ab/rules")
def ab_rules(ab: str = "", user=Depends(auth.get_current_user), db: Session = Depends(get_db)):
    q = db.query(models.AdminAbRule)
    if ab:
        q = q.filter(models.AdminAbRule.ab_guid == ab)
    rows = q.all()
    data = [
        {
            "guid": r.guid,
            "ab": r.ab_guid,
            "user": r.user_guid,
            "rule": r.rule,
        }
        for r in rows
    ]
    return {"data": data, "total": len(data)}


@router.post("/api/ab/rule")
async def ab_rule_add(
    request: Request, user=Depends(auth.get_current_user), db: Session = Depends(get_db)
):
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
        body = {}
    ab_guid = body.get("ab") or body.get("ab_guid")
    users = body.get("users", [])
    rule = int(body.get("rule", 1))
    for u in users:
        uid = u.get("guid") if isinstance(u, dict) else u
        if not uid:
            continue
        db.add(models.AdminAbRule(ab_guid=ab_guid, user_guid=uid, rule=rule))
    db.commit()
    return {}


@router.patch("/api/ab/rule")
async def ab_rule_update(
    request: Request, user=Depends(auth.get_current_user), db: Session = Depends(get_db)
):
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
        body = {}
    ab_guid = body.get("ab") or body.get("ab_guid")
    users = body.get("users", [])
    rule = int(body.get("rule", 1))
    for u in users:
        uid = u.get("guid") if isinstance(u, dict) else u
        if not uid:
            continue
        r = (
            db.query(models.AdminAbRule)
            .filter(
                models.AdminAbRule.ab_guid == ab_guid,
                models.AdminAbRule.user_guid == uid,
            )
            .first()
        )
        if r:
            r.rule = rule
    db.commit()
    return {}


@router.delete("/api/ab/rules")
async def ab_rules_delete(
    request: Request, user=Depends(auth.get_current_user), db: Session = Depends(get_db)
):
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
        body = {}
    ab_guid = body.get("ab") or body.get("ab_guid")
    guids = body.get("rule_guids", [])
    q = db.query(models.AdminAbRule)
    if ab_guid:
        q = q.filter(models.AdminAbRule.ab_guid == ab_guid)
    if guids:
        q = q.filter(models.AdminAbRule.guid.in_(guids))
    q.delete()
    db.commit()
    return {}
