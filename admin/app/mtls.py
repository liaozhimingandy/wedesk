"""mTLS identity for ``/api/device/*``, ``/api/app/*`` and ``/api/cert/renew``.

Why this module exists
----------------------
Neither uvicorn nor FastAPI publishes the peer certificate on the ASGI scope,
and in production TLS is terminated by a reverse proxy (nginx / Caddy / Traefik)
which *does* verify the client certificate but only forwards it as an HTTP
header. Trusting a header blindly is a remote-identity-spoofing hole: anybody
who can reach the admin port can set ``X-SSL-Client-Cert``.

So identity here rests on three independent checks, and the header is only the
*transport* hint:

1. the presented certificate really chains to our intermediate CA
   (``pki.verify_leaf`` — a real signature check, not a string compare);
2. its serial **and** public-key fingerprint exist in ``admin_device_certs``
   with ``status="active"`` and are inside the validity window — so revocation
   and expiry are enforced server-side, not by the proxy;
3. the request carries a proof-of-possession signature made with the leaf
   private key over ``method + path + timestamp + nonce + sha256(body)``.

Check (3) is what makes the forwarded header safe: an attacker who forges the
header still cannot produce the signature.

Deployment
----------
nginx (the part that matters)::

    ssl_client_certificate /data/pki/ca_chain.crt;   # trust our CA only
    ssl_verify_client       on;
    proxy_set_header X-SSL-Client-Cert      $ssl_client_escaped_cert;
    proxy_set_header X-SSL-Client-Verify    $ssl_client_verify;
    proxy_set_header X-SSL-Client-Serial    $ssl_client_serial;
    proxy_set_header X-SSL-Client-DN        $ssl_client_s_dn;

Header names are overridable through ``PKI_MTLS_HEADER_*``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from fastapi import Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models, pki
from .config import settings
from .database import get_db
from .models import as_aware

HDR_TIMESTAMP = "X-WeDesk-Timestamp"
HDR_NONCE = "X-WeDesk-Nonce"
HDR_SIGNATURE = "X-WeDesk-Signature"

# Optional: a server that *does* publish the peer cert on the ASGI scope.
_SCOPE_KEYS = ("tls_client_cert", "ssl_client_cert", "client_cert")


class MtlsError(HTTPException):
    """Uniform 401/403 for every mTLS failure mode."""


@dataclass
class LeafIdentity:
    """An authenticated leaf: the certificate plus its database record."""

    cert: x509.Certificate
    record: models.AdminDeviceCert

    @property
    def device_id(self) -> str:
        return self.record.device_id

    @property
    def cert_type(self) -> str:
        return self.record.cert_type

    @property
    def serial(self) -> str:
        return self.record.serial


# ---------------------------------------------------------------------------
# Certificate extraction
# ---------------------------------------------------------------------------

def _clean_pem(raw: str) -> str:
    """Undo whatever escaping the proxy applied to the PEM.

    nginx ``$ssl_client_escaped_cert`` is percent-encoded; ``$ssl_client_cert``
    replaces the newlines with tabs on some builds. Handle both.
    """
    text = raw.strip()
    for candidate in (text, unquote(text), unquote(text).replace("\t", "\n")):
        if "-----BEGIN CERTIFICATE-----" in candidate:
            return candidate
    return text


def _load(pem: str) -> x509.Certificate | None:
    cleaned = _clean_pem(pem)
    try:
        return x509.load_pem_x509_certificate(cleaned.encode("utf-8"))
    except Exception:
        return None


def extract_client_cert(request: Request) -> x509.Certificate | None:
    """Find the peer certificate: proxy headers first, then the ASGI scope."""
    headers = request.headers
    for key in _SCOPE_KEYS:
        raw = request.scope.get(key)
        if isinstance(raw, x509.Certificate):
            return raw
        if isinstance(raw, (str, bytes)):
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            cert = _load(raw)
            if cert is not None:
                return cert

    raw = headers.get(settings.mtls_header_cert)
    if not raw:
        return None

    verify = headers.get(settings.mtls_header_verify, "")
    if verify and verify.upper() not in ("SUCCESS", "OK", "TRUE", "1"):
        return None
    return _load(raw)


# ---------------------------------------------------------------------------
# Proof of possession
# ---------------------------------------------------------------------------

def canonical_string(method: str, path: str, timestamp: str, nonce: str, body: bytes) -> str:
    return "\n".join(
        [
            method.upper(),
            path,
            str(timestamp),
            nonce,
            hashlib.sha256(body or b"").hexdigest(),
        ]
    )


def activation_canonical(device_id: str, csr_pem: str, timestamp: str, nonce: str) -> str:
    """Canonical string for the *activation* proof of possession.

    Activation is one-way HTTPS and carries the signature inside the JSON body,
    so the payload cannot be hashed as a whole (the signature would have to
    cover itself). We sign over the two things that matter instead: which
    identity is claimed and which key is being certified.
    """
    return "\n".join(
        [
            device_id,
            hashlib.sha256((csr_pem or "").strip().encode("utf-8")).hexdigest(),
            str(timestamp),
            nonce,
        ]
    )


def _decode_signature(text: str) -> bytes | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return base64.b64decode(text + "=" * (-len(text) % 4), validate=False)
    except (binascii.Error, ValueError):
        pass
    try:
        return bytes.fromhex(text)
    except ValueError:
        return None


def verify_pop(public_key, message: bytes, signature: str) -> bool:
    sig = _decode_signature(signature)
    if not sig:
        return False
    try:
        if isinstance(public_key, ed25519.Ed25519PublicKey):
            public_key.verify(sig, message)
            return True
        if isinstance(public_key, ec.EllipticCurvePublicKey):
            public_key.verify(sig, message, ec.ECDSA(hashes.SHA256()))
            return True
        if isinstance(public_key, rsa.RSAPublicKey):
            try:
                public_key.verify(sig, message, padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.MAX_LENGTH,
                ), hashes.SHA256())
                return True
            except InvalidSignature:
                public_key.verify(sig, message, padding.PKCS1v15(), hashes.SHA256())
                return True
    except Exception:
        return False
    return False


def _consume_nonce(db: Session, nonce: str, subject: str = "") -> bool:
    """Claim a nonce; ``False`` means replay or already-consumed.

    Two flavours of nonce are accepted:

    * server-issued (``/api/cert/nonce``) — the row already exists and is
      unused, so claiming it means flipping ``used``;
    * client-generated — the row does not exist yet, so claiming it means
      inserting it already marked used. A replay then finds ``used=True``.
    """
    if not nonce or len(nonce) < 8:
        return False
    utcnow = datetime.now(timezone.utc)

    row = db.get(models.AdminCertNonce, nonce)
    if row is None:
        db.add(
            models.AdminCertNonce(
                nonce=nonce,
                subject=subject or "",
                used=True,
                expires_at=utcnow + timedelta(seconds=settings.pki_pop_window_seconds * 2),
            )
        )
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            return False
    else:
        if row.used:
            return False
        expires = as_aware(row.expires_at)
        if expires is not None and utcnow > expires:
            return False
        row.used = True
        db.flush()

    # Opportunistic purge keeps the table small without a background job.
    if secrets.randbelow(50) == 0:
        db.query(models.AdminCertNonce).filter(
            models.AdminCertNonce.expires_at < utcnow
        ).delete(synchronize_session=False)
        db.flush()
    return True


def issue_nonce(db: Session, subject: str = "") -> dict:
    """Mint a server-issued nonce (optional; clients may generate their own)."""
    nonce = secrets.token_hex(16)
    expires = datetime.now(timezone.utc) + timedelta(
        seconds=settings.pki_pop_window_seconds * 4
    )
    db.add(models.AdminCertNonce(nonce=nonce, subject=subject or "", expires_at=expires))
    db.commit()
    return {
        "result": "OK",
        "nonce": nonce,
        "expires_at": expires.isoformat(),
        "window_seconds": settings.pki_pop_window_seconds,
    }


def check_pop(
    request: Request, db: Session, public_key, *, subject: str = "", body: bytes = b""
) -> None:
    """Verify the request signature; raise 401 on any failure."""
    timestamp = request.headers.get(HDR_TIMESTAMP, "")
    nonce = request.headers.get(HDR_NONCE, "")
    signature = request.headers.get(HDR_SIGNATURE, "")

    if not (timestamp and nonce and signature):
        raise MtlsError(
            status_code=401,
            detail=f"missing proof-of-possession headers ({HDR_TIMESTAMP}, "
                   f"{HDR_NONCE}, {HDR_SIGNATURE})",
        )
    try:
        ts = int(timestamp)
    except ValueError:
        raise MtlsError(status_code=401, detail="bad timestamp")
    skew = abs(int(datetime.now(timezone.utc).timestamp()) - ts)
    if skew > settings.pki_pop_window_seconds:
        raise MtlsError(status_code=401, detail="timestamp outside the freshness window")

    if not _consume_nonce(db, nonce, subject):
        raise MtlsError(status_code=401, detail="nonce replayed")

    message = canonical_string(
        request.method, request.url.path, timestamp, nonce, body
    ).encode("utf-8")

    if not verify_pop(public_key, message, signature):
        raise MtlsError(status_code=401, detail="proof-of-possession signature mismatch")


# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------

def require_leaf(kind: str | None = None):
    """Dependency factory for mTLS routes.

    ``kind`` restricts the accepted certificate type (``"device"`` for
    ``/api/device/*``, ``"app"`` for ``/api/app/*``, ``None`` for either).
    """

    async def dependency(request: Request, db: Session = Depends(get_db)) -> LeafIdentity:
        body = await request.body()  # cached; the route handler re-reads it
        cert = extract_client_cert(request)
        if cert is None:
            raise MtlsError(
                status_code=401,
                detail="client certificate required (mTLS); none presented or "
                       "the proxy did not forward it",
            )

        ca = pki.get()
        if not ca.verify_leaf(cert):
            raise MtlsError(status_code=401, detail="certificate is not issued by this CA")

        serial = format(cert.serial_number, "x")
        fingerprint = pki.spki_fingerprint(cert)

        record = (
            db.query(models.AdminDeviceCert)
            .filter(
                models.AdminDeviceCert.serial == serial,
                models.AdminDeviceCert.spki_fingerprint == fingerprint,
            )
            .first()
        )
        if record is None:
            raise MtlsError(status_code=401, detail="unknown certificate serial/key")
        if record.status == "revoked":
            raise MtlsError(status_code=403, detail="certificate revoked")
        if record.status == "superseded":
            raise MtlsError(status_code=403, detail="certificate superseded by a renewal")

        now_dt = datetime.now(timezone.utc)
        not_after = as_aware(record.not_after)
        not_before = as_aware(record.not_before)
        if not_after is not None and now_dt > not_after:
            record.status = "expired"
            db.commit()
            raise MtlsError(status_code=403, detail="certificate expired")
        if not_before is not None and now_dt < not_before - timedelta(hours=1):
            raise MtlsError(status_code=403, detail="certificate not yet valid")

        if kind and record.cert_type != kind:
            raise MtlsError(
                status_code=403,
                detail=f"endpoint requires a {kind} certificate, got {record.cert_type}",
            )

        if settings.pki_require_pop:
            check_pop(
                request, db, cert.public_key(), subject=record.device_id, body=body
            )

        record.last_seen_at = now_dt
        record.last_seen_ip = request.client.host if request.client else ""
        db.commit()
        return LeafIdentity(cert=cert, record=record)

    return dependency
