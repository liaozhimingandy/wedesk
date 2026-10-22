"""Device / app certificate lifecycle (PKI: ROOT -> CA -> leaf).

TLS mode per endpoint
---------------------
==============================  ============  ==================================
endpoint                        TLS mode      purpose
==============================  ============  ==================================
POST /api/device/activate       one-way HTTPS first certificate for hardware
POST /api/app/activate          one-way HTTPS first certificate for the iOS app
POST /api/cert/renew            mTLS          silent renewal before expiry
GET  /api/cert/status           mTLS          "am I still valid / when to renew"
GET  /api/cert/ca               one-way HTTPS public ROOT + intermediate
GET  /api/cert/crl              one-way HTTPS revocation list
POST /api/cert/nonce            one-way HTTPS optional server-issued nonce
POST /api/cert/activation-token admin session mint a single-use activation token
GET  /api/cert/certs            admin session list issued certificates
POST /api/cert/revoke           admin session revoke by serial or device
GET  /api/cert/info             admin session CA status
==============================  ============  ==================================

Activation is deliberately *one-way* HTTPS: a device with no certificate yet
has nothing to present, so it authenticates with a single-use activation token
instead. Everything after that runs over mTLS and additionally requires a
proof-of-possession signature (see ``app/mtls.py``).
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .. import auth, models, mtls, pki, schemas
from ..config import settings
from ..database import get_db
from ..models import as_aware

router = APIRouter()

_ID_RE = re.compile(r"^[A-Za-z0-9._:@-]{4,100}$")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _cert_days(cert_type: str) -> int:
    return settings.pki_device_cert_days if cert_type == "device" else settings.pki_app_cert_days


def _cert_payload(record: models.AdminDeviceCert, *, ca: "pki.Pki",
                  include_ca: bool = True) -> dict:
    ca_obj = ca or pki.get()
    not_after = as_aware(record.not_after)
    renew_after = (
        not_after - timedelta(days=settings.pki_renew_window_days)
        if not_after
        else None
    )
    out = {
        "result": "OK",
        "certificate": record.certificate,
        "serial": record.serial,
        "fingerprint": record.spki_fingerprint,
        "device_id": record.device_id,
        "cert_type": record.cert_type,
        "status": record.status,
        "not_before": as_aware(record.not_before).isoformat() if record.not_before else "",
        "not_after": not_after.isoformat() if not_after else "",
        "renew_after": renew_after.isoformat() if renew_after else "",
    }
    if include_ca:
        out["ca_chain"] = ca_obj.chain_pem
        out["root"] = ca_obj.root_pem
    return out


def _active_cert(db: Session, device_id: str, cert_type: str,
                 utcnow: datetime) -> models.AdminDeviceCert | None:
    rows = (
        db.query(models.AdminDeviceCert)
        .filter(
            models.AdminDeviceCert.device_id == device_id,
            models.AdminDeviceCert.cert_type == cert_type,
        )
        .all()
    )
    for row in rows:
        if row.status in ("active", "superseded"):
            not_after = as_aware(row.not_after)
            if not_after is None or not_after > utcnow:
                return row
    return None


def _activate(request: Request, db: Session, body: schemas.ActivateRequest,
              cert_type: str) -> dict:
    ca = pki.get()
    utcnow = datetime.now(timezone.utc)

    device_id = (body.device_id or "").strip()
    if not _ID_RE.match(device_id):
        raise HTTPException(status_code=400, detail="invalid device_id")
    if not body.csr.strip():
        raise HTTPException(status_code=400, detail="csr is required")

    try:
        csr = ca.parse_csr(body.csr)
    except pki.PkiError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # --- activation token (single use, bound to this id + cert type) --------
    token = (body.token or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="activation token required")
    row = db.get(models.AdminActivationToken, token)
    if row is None:
        raise HTTPException(status_code=401, detail="invalid activation token")
    if row.used:
        raise HTTPException(status_code=409, detail="activation token already used")
    if row.device_id != device_id or row.cert_type != cert_type:
        raise HTTPException(status_code=403, detail="activation token does not match")
    expires = as_aware(row.expires_at)
    if expires is not None and utcnow > expires:
        raise HTTPException(status_code=401, detail="activation token expired")

    # --- do not silently re-issue for a device that already holds a live cert
    existing = _active_cert(db, device_id, cert_type, utcnow)
    if existing is not None and not row.allow_reissue:
        raise HTTPException(
            status_code=409,
            detail="device already holds a live certificate; revoke it or mint a "
                   "token with allow_reissue=true",
        )

    # --- proof of possession (optional but on by default) -------------------
    if settings.pki_require_pop:
        if not (body.timestamp and body.nonce and body.signature):
            raise HTTPException(
                status_code=401,
                detail="proof of possession required (timestamp, nonce, signature)",
            )
        try:
            ts = int(body.timestamp)
        except ValueError:
            raise HTTPException(status_code=401, detail="bad timestamp")
        if abs(int(utcnow.timestamp()) - ts) > settings.pki_pop_window_seconds:
            raise HTTPException(status_code=401, detail="timestamp outside window")
        if not mtls._consume_nonce(db, body.nonce, device_id):
            raise HTTPException(status_code=401, detail="nonce replayed")
        message = mtls.activation_canonical(
            device_id, body.csr, body.timestamp, body.nonce
        ).encode("utf-8")
        if not mtls.verify_pop(csr.public_key(), message, body.signature):
            raise HTTPException(status_code=401, detail="proof of possession mismatch")

    # --- issue --------------------------------------------------------------
    issued = ca.issue(
        csr, device_id=device_id, cert_type=cert_type, days=_cert_days(cert_type)
    )
    record = models.AdminDeviceCert(
        serial=issued.serial_hex,
        device_id=device_id,
        cert_type=cert_type,
        subject_cn=issued.subject_cn,
        spki_fingerprint=issued.fingerprint,
        certificate=issued.certificate_pem,
        status="active",
        not_before=issued.not_before,
        not_after=issued.not_after,
        created_by="activate",
    )
    db.add(record)

    if existing is not None:
        existing.status = "superseded"
        existing.superseded_by = issued.serial_hex

    row.used = True
    row.used_at = utcnow
    row.used_serial = issued.serial_hex
    db.commit()

    return _cert_payload(record, ca=ca)


# ---------------------------------------------------------------------------
# public (one-way HTTPS)
# ---------------------------------------------------------------------------

@router.get("/api/cert/ca")
def cert_ca():
    """Public trust anchors: what a client must install/verify against."""
    ca = pki.get()
    return {
        "result": "OK",
        "root": ca.root_pem,
        "ca": ca.ca_pem,
        "chain": ca.chain_pem,
        # Lets an operator confirm the anchors in use are *their* ROOT/CA and
        # not something the service generated on a first boot.
        "root_source": ca.root_source,
        "ca_source": ca.ca_source,
        "root_serial": format(ca.root_cert.serial_number, "x"),
        "ca_serial": format(ca.ca_cert.serial_number, "x"),
        "root_subject": ca.root_cert.subject.rfc4514_string(),
        "ca_subject": ca.ca_cert.subject.rfc4514_string(),
    }


@router.get("/api/cert/crl")
def cert_crl(db: Session = Depends(get_db)):
    """CRL covering every certificate revoked by this CA (PEM)."""
    ca = pki.get()
    rows = (
        db.query(models.AdminDeviceCert)
        .filter(models.AdminDeviceCert.status == "revoked")
        .all()
    )
    entries = [(r.serial, as_aware(r.revoked_at) or datetime.now(timezone.utc))
               for r in rows]
    return {"result": "OK", "count": len(entries), "crl": ca.revoked_serials_to_crl(entries)}


@router.post("/api/cert/nonce")
def cert_nonce(device_id: str = "", db: Session = Depends(get_db)):
    return mtls.issue_nonce(db, device_id)


# ---------------------------------------------------------------------------
# activation (one-way HTTPS)
# ---------------------------------------------------------------------------

@router.post("/api/device/activate")
async def device_activate(
    request: Request, body: schemas.ActivateRequest, db: Session = Depends(get_db)
):
    """First certificate for hardware. One-way HTTPS + activation token."""
    return _activate(request, db, body, "device")


@router.post("/api/app/activate")
async def app_activate(
    request: Request, body: schemas.ActivateRequest, db: Session = Depends(get_db)
):
    """First certificate for the mobile app. One-way HTTPS + activation token."""
    return _activate(request, db, body, "app")


# ---------------------------------------------------------------------------
# renewal (mTLS)
# ---------------------------------------------------------------------------

@router.post("/api/cert/renew")
async def cert_renew(
    body: schemas.RenewRequest,
    request: Request,
    identity: mtls.LeafIdentity = Depends(mtls.require_leaf()),
    db: Session = Depends(get_db),
):
    """Silent renewal: same identity, new certificate (key rotation allowed).

    The request is authenticated by the *current* certificate (mTLS) plus a
    proof-of-possession signature, so no activation token is needed. The new
    certificate inherits ``device_id`` and ``cert_type`` from the old one —
    renewal can never widen scope. The old record is marked ``superseded`` and
    keeps verifying until its own ``not_after``, giving clients a grace window
    to swap the file.
    """
    ca = pki.get()

    if not body.csr.strip():
        raise HTTPException(status_code=400, detail="csr is required")
    try:
        csr = ca.parse_csr(body.csr)
    except pki.PkiError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    old = identity.record
    issued = ca.issue(
        csr, device_id=old.device_id, cert_type=old.cert_type,
        days=_cert_days(old.cert_type),
    )
    record = models.AdminDeviceCert(
        serial=issued.serial_hex,
        device_id=old.device_id,
        cert_type=old.cert_type,
        subject_cn=issued.subject_cn,
        spki_fingerprint=issued.fingerprint,
        certificate=issued.certificate_pem,
        status="active",
        not_before=issued.not_before,
        not_after=issued.not_after,
        renewed_from=old.serial,
        renew_count=(old.renew_count or 0) + 1,
        created_by="renew",
    )
    db.add(record)

    old.status = "superseded"
    old.superseded_by = issued.serial_hex
    db.commit()

    payload = _cert_payload(record, ca=ca, include_ca=False)
    payload["previous_serial"] = old.serial
    return payload


@router.get("/api/cert/status")
def cert_status(
    identity: mtls.LeafIdentity = Depends(mtls.require_leaf()),
    db: Session = Depends(get_db),
):
    """Current certificate status + when the client should renew."""
    ca = pki.get()
    record = identity.record
    not_after = as_aware(record.not_after)
    remaining = (not_after - datetime.now(timezone.utc)).days if not_after else None
    return {
        "result": "OK",
        "device_id": record.device_id,
        "cert_type": record.cert_type,
        "serial": record.serial,
        "fingerprint": record.spki_fingerprint,
        "status": record.status,
        "not_after": not_after.isoformat() if not_after else "",
        "days_remaining": remaining,
        "should_renew": bool(remaining is not None
                             and remaining <= settings.pki_renew_window_days),
        "renew_window_days": settings.pki_renew_window_days,
        "ca_not_after": pki.cert_not_after(ca.ca_cert).isoformat(),
    }


# ---------------------------------------------------------------------------
# admin session endpoints
# ---------------------------------------------------------------------------

@router.post("/api/cert/activation-tokens")
async def create_activation_token(
    body: schemas.ActivationTokenRequest,
    request: Request,
    user=Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    """Mint a single-use activation token for a not-yet-enrolled device."""
    device_id = (body.device_id or "").strip()
    if not _ID_RE.match(device_id):
        raise HTTPException(status_code=400, detail="invalid device_id")
    cert_type = body.cert_type if body.cert_type in ("device", "app") else "device"
    ttl = max(1, min(int(body.ttl_hours or 24), 24 * 365))

    token = secrets.token_urlsafe(24)
    db.add(
        models.AdminActivationToken(
            token=token,
            device_id=device_id,
            cert_type=cert_type,
            allow_reissue=bool(body.allow_reissue),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=ttl),
            created_by=getattr(user, "guid", ""),
        )
    )
    db.commit()
    return {
        "result": "OK",
        "token": token,
        "device_id": device_id,
        "cert_type": cert_type,
        "ttl_hours": ttl,
        "activate_url": f"/api/{cert_type}/activate",
    }


@router.get("/api/cert/certs")
def list_certs(
    device_id: str = "",
    cert_type: str = "",
    status: str = "",
    limit: int = 100,
    user=Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    q = db.query(models.AdminDeviceCert)
    if device_id:
        q = q.filter(models.AdminDeviceCert.device_id == device_id)
    if cert_type:
        q = q.filter(models.AdminDeviceCert.cert_type == cert_type)
    if status:
        q = q.filter(models.AdminDeviceCert.status == status)
    rows = q.order_by(models.AdminDeviceCert.issued_at.desc()).limit(max(1, min(limit, 500))).all()
    return {
        "result": "OK",
        "total": len(rows),
        "certs": [
            {
                "serial": r.serial,
                "device_id": r.device_id,
                "cert_type": r.cert_type,
                "status": r.status,
                "fingerprint": r.spki_fingerprint,
                "not_before": as_aware(r.not_before).isoformat() if r.not_before else "",
                "not_after": as_aware(r.not_after).isoformat() if r.not_after else "",
                "renew_count": r.renew_count or 0,
                "last_seen_at": as_aware(r.last_seen_at).isoformat() if r.last_seen_at else "",
            }
            for r in rows
        ],
    }


@router.get("/api/cert/certs/{device_id}")
def certs_of_device(
    device_id: str,
    user=Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(models.AdminDeviceCert)
        .filter(models.AdminDeviceCert.device_id == device_id)
        .order_by(models.AdminDeviceCert.issued_at.desc())
        .all()
    )
    return {
        "result": "OK",
        "device_id": device_id,
        "certs": [
            {
                "serial": r.serial,
                "cert_type": r.cert_type,
                "status": r.status,
                "fingerprint": r.spki_fingerprint,
                "not_after": as_aware(r.not_after).isoformat() if r.not_after else "",
                "certificate": r.certificate,
            }
            for r in rows
        ],
    }


@router.post("/api/cert/revoke")
async def revoke_cert(
    body: schemas.RevokeRequest,
    request: Request,
    user=Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    """Revoke by serial, or every live certificate of a device.

    Revocation takes effect on the next mTLS request: the dependency rejects
    ``status="revoked"`` before the route body is even read.
    """
    q = db.query(models.AdminDeviceCert)
    if body.serial:
        q = q.filter(models.AdminDeviceCert.serial == body.serial.strip())
    elif body.device_id:
        q = q.filter(models.AdminDeviceCert.device_id == body.device_id.strip())
        if body.cert_type:
            q = q.filter(models.AdminDeviceCert.cert_type == body.cert_type)
    else:
        raise HTTPException(status_code=400, detail="serial or device_id required")

    rows = [r for r in q.all() if r.status in ("active", "superseded")]
    if not rows:
        raise HTTPException(status_code=404, detail="no live certificate matched")

    utcnow = datetime.now(timezone.utc)
    for r in rows:
        r.status = "revoked"
        r.revoked_at = utcnow
        r.revoke_reason = body.reason or "unspecified"
    db.commit()
    return {
        "result": "OK",
        "revoked": [r.serial for r in rows],
        "reason": body.reason or "unspecified",
        "revoked_at": utcnow.isoformat(),
    }


@router.get("/api/cert/info")
def cert_info(user=Depends(auth.get_current_user)):
    """CA status: subjects, expiry, policy. Useful right after deployment."""
    ca = pki.get()
    return {"result": "OK", **ca.info()}
