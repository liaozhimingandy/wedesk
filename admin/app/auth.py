"""Authentication helpers: password hashing, session tokens, current-user dep."""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from .database import get_db
from . import models
from .models import as_aware


# ---------------------------------------------------------------------------
# Password hashing (PBKDF2-HMAC-SHA256, stdlib only)
# ---------------------------------------------------------------------------
def hash_password(password: str, salt: bytes | None = None) -> str:
    if salt is None:
        salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return f"pbkdf2_sha256$100000${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    try:
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters)
        )
    except ValueError:
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


# ---------------------------------------------------------------------------
# Sessions (DB-backed random token)
# ---------------------------------------------------------------------------
def create_session(db: Session, user_guid: str, ttl_hours: int = 24 * 30) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
    db.add(models.AdminSession(token=token, user_guid=user_guid, expires_at=expires))
    db.commit()
    return token


def destroy_session(db: Session, token: str) -> None:
    db.query(models.AdminSession).filter(
        models.AdminSession.token == token
    ).delete()
    db.commit()


def get_user_by_token(db: Session, token: str):
    sess = (
        db.query(models.AdminSession)
        .filter(models.AdminSession.token == token)
        .first()
    )
    if not sess:
        return None
    if sess.expires_at and as_aware(sess.expires_at) < datetime.now(timezone.utc):
        db.delete(sess)
        db.commit()
        return None
    user = (
        db.query(models.AdminUser)
        .filter(models.AdminUser.guid == sess.user_guid)
        .first()
    )
    if not user or user.status != 1:
        return None
    return user


def get_current_user(
    authorization: str = Header(None),
    db: Session = Depends(get_db),
):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    token = authorization[len("Bearer "):].strip()
    user = get_user_by_token(db, token)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return user


def require_admin(user=Depends(get_current_user)):
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin required!")
    return user


# ---------------------------------------------------------------------------
# User payload (matches client UserPayload shape)
# ---------------------------------------------------------------------------
def user_payload(user: models.AdminUser) -> dict:
    return {
        "name": user.name,
        "display_name": user.name,
        "avatar": "",
        "email": user.email or "",
        "note": user.note or "",
        "status": user.status,
        "info": "",
        "is_admin": bool(user.is_admin),
        "third_auth_type": "",
    }
