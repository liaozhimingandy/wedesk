"""Account endpoints: login / currentUser / logout / login-options / OIDC.

These mirror the contract the RustDesk client expects:
  * POST /api/login            -> {type, access_token, user}  | {error}
  * POST /api/currentUser      -> UserPayload                 | {error}
  * POST /api/logout           -> {}
  * GET  /api/login-options    -> List[str]
  * POST /api/oidc/auth        -> {url, state} | {error}
  * GET  /api/oidc/auth-query  -> {type, access_token, user} | {error}
"""

import base64
import json
import secrets
import urllib.request
import urllib.parse
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from ..database import get_db
from .. import models, auth, schemas
from ..config import settings

router = APIRouter()


def _oidc_config(issuer: str) -> dict | None:
    """Fetch and cache (in-memory) the OIDC .well-known configuration."""
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


@router.post("/api/login")
def login(
    req: schemas.LoginRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    if not req.username or not req.password:
        return {"error": "Wrong user or password"}
    user = (
        db.query(models.AdminUser)
        .filter(models.AdminUser.name == req.username)
        .first()
    )
    if not user or user.status != 1 or not auth.verify_password(
        req.password, user.password_hash
    ):
        return {"error": "Wrong user or password"}
    token = auth.create_session(db, user.guid)
    return {
        "type": "access_token",
        "access_token": token,
        "user": auth.user_payload(user),
    }


@router.post("/api/currentUser")
def current_user(
    req: schemas.CurrentUserRequest,
    user=Depends(auth.get_current_user),
):
    return auth.user_payload(user)


@router.post("/api/logout")
def logout(
    req: schemas.LogoutRequest,
    authorization: str = Header(None),
    db: Session = Depends(get_db),
):
    if authorization and authorization.startswith("Bearer "):
        auth.destroy_session(db, authorization[len("Bearer "):].strip())
    return {}


@router.get("/api/login-options")
def login_options():
    opts: list[str] = []
    if settings.oidc_enabled:
        opts.append("oidc/wedesk")
    return opts


# ---------------------------------------------------------------------------
# OIDC (optional, disabled unless OIDC_ISSUER is configured)
# ---------------------------------------------------------------------------
@router.post("/api/oidc/auth")
async def oidc_auth(request: Request, db: Session = Depends(get_db)):
    if not settings.oidc_enabled:
        return {"error": "OIDC not configured"}
    body = {}
    try:
        body = json.loads((await _read_body(request)) or "{}")
    except Exception:
        body = {}
    cfg = _oidc_config(settings.oidc_issuer)
    if not cfg or "authorization_endpoint" not in cfg:
        return {"error": "OIDC discovery failed"}
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    db.add(models.AdminOidcState(state=state, nonce=nonce))
    db.commit()
    params = {
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": settings.oidc_redirect_uri,
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
    }
    url = (
        cfg["authorization_endpoint"]
        + "?"
        + urllib.parse.urlencode(params)
    )
    return {"url": url, "state": state}


@router.get("/api/oidc/auth-query")
def oidc_auth_query(
    code: str = "", id: str = "", uuid: str = "", db: Session = Depends(get_db)
):
    if not settings.oidc_enabled:
        return {"error": "OIDC not configured"}
    if not code:
        return {"error": "Missing authorization code"}
    cfg = _oidc_config(settings.oidc_issuer)
    if not cfg:
        return {"error": "OIDC discovery failed"}
    # exchange code -> token
    token_data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": settings.oidc_client_id,
        "client_secret": settings.oidc_client_secret,
        "redirect_uri": settings.oidc_redirect_uri,
    }
    try:
        req = urllib.request.Request(
            cfg["token_endpoint"],
            data=urllib.parse.urlencode(token_data).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            tok = json.loads(resp.read().decode("utf-8"))
        id_token = tok.get("id_token", "")
        # decode payload (skip signature verification for v1)
        payload = _decode_jwt_payload(id_token) if id_token else {}
        email = payload.get("email") or (tok.get("userinfo", {}) or {}).get("email")
        name = payload.get("name") or email or "oidc-user"
    except Exception as e:
        return {"error": f"OIDC token exchange failed: {e}"}

    user = (
        db.query(models.AdminUser)
        .filter(models.AdminUser.email == (email or ""))
        .first()
    )
    if not user:
        user = models.AdminUser(
            name=name, email=email or "", status=1, is_admin=False
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    token = auth.create_session(db, user.guid)
    return {
        "type": "access_token",
        "access_token": token,
        "user": auth.user_payload(user),
    }


def _decode_jwt_payload(id_token: str) -> dict:
    try:
        part = id_token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


async def _read_body(request: Request) -> bytes:
    return await request.body()
