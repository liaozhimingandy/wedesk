"""End-to-end exercise of the device PKI routes.

Runs against SQLite + a throw-away PKI directory, so it needs nothing but the
admin requirements::

    pip install -r requirements.txt
    python -m pytest tests -q          # or:  python tests/test_cert_api.py

Covers the happy path (activate -> renew -> business call) and the failure
modes that matter: replay, forged cert, revoked cert, wrong cert type.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

# Environment must be set before the app module is imported (config is read at
# import time through an lru_cache).
_TMP = tempfile.mkdtemp(prefix="wedesk-pki-test-")
os.environ["DATABASE_URL"] = "sqlite:///" + Path(_TMP, "admin.db").as_posix().replace("\\", "/")
os.environ["PKI_DIR"] = str(Path(_TMP, "pki"))
os.environ["DEFAULT_ADMIN_NAME"] = "admin"
os.environ["DEFAULT_ADMIN_PASSWORD"] = "testpass"
os.environ["PKI_DEVICE_CERT_DAYS"] = "90"
os.environ["PKI_APP_CERT_DAYS"] = "180"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app import mtls, pki  # noqa: E402
from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec, ed25519  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

DEVICE_ID = "123456789012"
APP_ID = "app-0001"

_failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        _failures.append(label)


# ---------------------------------------------------------------------------
# key / CSR / signature helpers (stand-ins for the real device)
# ---------------------------------------------------------------------------

def new_key(kind: str = "ed25519"):
    return (
        ed25519.Ed25519PrivateKey.generate()
        if kind == "ed25519"
        else ec.generate_private_key(ec.SECP256R1())
    )


def make_csr(key, cn: str) -> str:
    builder = x509.CertificateSigningRequestBuilder().subject_name(
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    )
    signed = (
        builder.sign(key, None)
        if isinstance(key, ed25519.Ed25519PrivateKey)
        else builder.sign(key, hashes.SHA256())
    )
    return signed.public_bytes(serialization.Encoding.PEM).decode("utf-8")


def sign_bytes(key, message: bytes) -> str:
    sig = (
        key.sign(message)
        if isinstance(key, ed25519.Ed25519PrivateKey)
        else key.sign(message, ec.ECDSA(hashes.SHA256()))
    )
    return base64.b64encode(sig).decode("utf-8")


def pop_headers(key, method: str, path: str, body: bytes, nonce: str | None = None) -> dict:
    ts = str(int(time.time()))
    nonce = nonce or os.urandom(16).hex()
    message = mtls.canonical_string(method, path, ts, nonce, body).encode("utf-8")
    return {
        "X-WeDesk-Timestamp": ts,
        "X-WeDesk-Nonce": nonce,
        "X-WeDesk-Signature": sign_bytes(key, message),
    }


def mtls_headers(cert_pem: str) -> dict:
    """What an nginx in front of the app would forward (percent-encoded PEM)."""
    return {
        "X-SSL-Client-Cert": quote(cert_pem, safe=""),
        "X-SSL-Client-Verify": "SUCCESS",
    }


# ---------------------------------------------------------------------------
# scenario
# ---------------------------------------------------------------------------

def main() -> int:
    # ``with`` triggers the lifespan handler (create_all + admin bootstrap + PKI).
    with TestClient(app) as client:
        return _scenario(client)


def _scenario(client: TestClient) -> int:
    print("\n[0] service + CA bootstrap")
    health = client.get("/health").json()
    check(health.get("db") == "up", "database reachable")
    check(health.get("pki") is True, "PKI bootstrapped at startup")
    ca_info = client.get("/api/cert/ca").json()
    root_cert = x509.load_pem_x509_certificate(ca_info["root"].encode())
    root_cn = root_cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    check(root_cn == "WeDesk Root CA", f"ROOT subject is branded ({root_cn})")
    check(ca_info["chain"].count("BEGIN CERTIFICATE") == 2, "chain = CA + ROOT")

    print("\n[1] admin login + activation token")
    r = client.post("/api/login", json={"username": "admin", "password": "testpass"})
    check(r.status_code == 200, "admin login")
    admin = r.json()
    token = admin.get("access_token") or admin.get("token") or ""
    if not token and isinstance(admin.get("user"), dict):
        token = admin["user"].get("token", "")
    check(bool(token), "session token returned")
    auth_headers = {"Authorization": f"Bearer {token}"}

    r = client.post(
        "/api/cert/activation-tokens",
        json={"device_id": DEVICE_ID, "cert_type": "device", "ttl_hours": 24},
        headers=auth_headers,
    )
    check(r.status_code == 200, "mint activation token")
    act_token = r.json().get("token", "")

    print("\n[2] device activation (one-way HTTPS, ed25519 key)")
    key = new_key("ed25519")
    csr = make_csr(key, DEVICE_ID)
    ts = str(int(time.time()))
    nonce = os.urandom(16).hex()
    sig = sign_bytes(
        key, mtls.activation_canonical(DEVICE_ID, csr, ts, nonce).encode("utf-8")
    )
    body = {
        "device_id": DEVICE_ID,
        "csr": csr,
        "token": act_token,
        "timestamp": ts,
        "nonce": nonce,
        "signature": sig,
    }
    r = client.post("/api/device/activate", json=body)
    check(r.status_code == 200, f"activate -> 200 ({r.status_code} {r.text[:120]})")
    issued = r.json()
    device_cert_pem = issued.get("certificate", "")
    check(device_cert_pem.startswith("-----BEGIN"), "certificate returned")

    leaf = x509.load_pem_x509_certificate(device_cert_pem.encode())
    ca = pki.get()
    check(ca.verify_leaf(leaf), "leaf chains to our CA")
    try:
        leaf.verify_directly_issued_by(ca.ca_cert)  # cryptography >= 40
        chained = True
    except Exception:
        chained = ca.verify_leaf(leaf)
    check(chained, "leaf signature verified against CA")
    cn = leaf.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    check(cn == DEVICE_ID, f"CN == device id ({cn})")
    check(issued.get("fingerprint") == pki.spki_fingerprint(leaf), "fingerprint matches SPKI")

    print("\n[3] activation token is single use")
    r = client.post("/api/device/activate", json=body)
    check(r.status_code == 409, f"replay of activation token rejected ({r.status_code})")

    print("\n[4] app activation (one-way HTTPS, EC key)")
    r = client.post(
        "/api/cert/activation-tokens",
        json={"device_id": APP_ID, "cert_type": "app"},
        headers=auth_headers,
    )
    app_act_token = r.json().get("token", "")
    app_key = new_key("ec256")
    app_csr = make_csr(app_key, APP_ID)
    ts = str(int(time.time()))
    nonce = os.urandom(16).hex()
    sig = sign_bytes(
        app_key, mtls.activation_canonical(APP_ID, app_csr, ts, nonce).encode("utf-8")
    )
    r = client.post(
        "/api/app/activate",
        json={
            "device_id": APP_ID,
            "csr": app_csr,
            "token": app_act_token,
            "timestamp": ts,
            "nonce": nonce,
            "signature": sig,
        },
    )
    check(r.status_code == 200, f"app activate -> 200 ({r.status_code} {r.text[:120]})")
    app_cert_pem = r.json().get("certificate", "")
    check(app_cert_pem.startswith("-----BEGIN"), "app certificate returned")

    print("\n[5] business call over mTLS (device heartbeat)")
    payload = b'{"conns":2,"sysinfo_version":"1.0"}'
    headers = {**mtls_headers(device_cert_pem), **pop_headers(key, "POST", "/api/device/heartbeat", payload)}
    r = client.post("/api/device/heartbeat", content=payload, headers=headers)
    check(r.status_code == 200, f"heartbeat accepted ({r.status_code} {r.text[:120]})")
    check(r.json().get("id") == DEVICE_ID, "heartbeat echoes device id")

    print("\n[6] replay of the very same request is rejected")
    r2 = client.post("/api/device/heartbeat", content=payload, headers=headers)
    check(r2.status_code == 401, f"nonce replay rejected ({r2.status_code} {r2.text[:120]})")

    print("\n[7] wrong cert type is rejected")
    payload2 = b'{}'
    headers2 = {
        **mtls_headers(app_cert_pem),
        **pop_headers(app_key, "POST", "/api/device/heartbeat", payload2),
    }
    r = client.post("/api/device/heartbeat", content=payload2, headers=headers2)
    check(r.status_code == 403, f"app cert cannot call /api/device/* ({r.status_code})")

    print("\n[8] app query of device status")
    payload3 = b""
    r = client.get(
        f"/api/app/device/{DEVICE_ID}",
        headers={**mtls_headers(app_cert_pem), **pop_headers(app_key, "GET", f"/api/app/device/{DEVICE_ID}", payload3)},
    )
    check(r.status_code == 200, f"app status query ({r.status_code} {r.text[:120]})")

    print("\n[9] certificate status over mTLS")
    payload4 = b""
    r = client.get(
        "/api/cert/status",
        headers={**mtls_headers(device_cert_pem), **pop_headers(key, "GET", "/api/cert/status", payload4)},
    )
    check(r.status_code == 200, f"cert status ({r.status_code})")
    check(r.json().get("should_renew") is False, "fresh cert does not need renewal yet")

    print("\n[10] silent renewal with key rotation (ed25519 -> EC)")
    # The renewal request is signed with the *current* key: that is what proves
    # the caller owns the certificate it is presenting. The new CSR carries a
    # brand new key (rotation), which is fine — nothing forces reuse.
    new_key2 = new_key("ec256")
    new_csr = make_csr(new_key2, DEVICE_ID)
    payload5 = json.dumps({"csr": new_csr}).encode()
    r = client.post(
        "/api/cert/renew",
        content=payload5,
        headers={
            **mtls_headers(device_cert_pem),
            **pop_headers(key, "POST", "/api/cert/renew", payload5),
        },
    )
    check(r.status_code == 200, f"renew -> 200 ({r.status_code} {r.text[:160]})")
    renewed = r.json()
    new_cert_pem = renewed.get("certificate", "")
    check(new_cert_pem.startswith("-----BEGIN"), "renewed certificate returned")
    check(bool(renewed.get("previous_serial")), "renewal links back to the old serial")
    new_leaf = x509.load_pem_x509_certificate(new_cert_pem.encode())
    check(ca.verify_leaf(new_leaf), "renewed leaf chains to our CA")
    check(new_leaf.serial_number != leaf.serial_number, "renewal minted a new serial")

    print("\n[11] old certificate is superseded (no longer accepted)")
    payload6 = b"{}"
    r = client.post(
        "/api/device/heartbeat",
        content=payload6,
        headers={**mtls_headers(device_cert_pem), **pop_headers(key, "POST", "/api/device/heartbeat", payload6)},
    )
    check(r.status_code == 403, f"superseded cert rejected ({r.status_code})")

    print("\n[12] new certificate works")
    payload7 = b'{"conns":1}'
    r = client.post(
        "/api/device/heartbeat",
        content=payload7,
        headers={**mtls_headers(new_cert_pem), **pop_headers(new_key2, "POST", "/api/device/heartbeat", payload7)},
    )
    check(r.status_code == 200, f"renewed cert accepted ({r.status_code} {r.text[:120]})")

    print("\n[13] revocation takes effect immediately + shows up in the CRL")
    r = client.post(
        "/api/cert/revoke", json={"device_id": DEVICE_ID}, headers=auth_headers
    )
    check(r.status_code == 200, f"revoke ({r.status_code} {r.text[:120]})")
    payload8 = b"{}"
    r = client.post(
        "/api/device/heartbeat",
        content=payload8,
        headers={**mtls_headers(new_cert_pem), **pop_headers(new_key2, "POST", "/api/device/heartbeat", payload8)},
    )
    check(r.status_code == 403, f"revoked cert rejected ({r.status_code})")
    crl = client.get("/api/cert/crl").json()
    check(crl.get("crl", "").startswith("-----BEGIN X509 CRL"), "CRL is valid PEM")
    check(crl.get("count", 0) >= 1, "CRL lists the revoked cert")

    print("\n[14] forged / foreign certificate is rejected")
    rogue_key = new_key("ed25519")
    rogue_ca_key = new_key("ec256")
    rogue_ca = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Rogue CA")]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Rogue CA")]))
        .public_key(rogue_ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime(2020, 1, 1))
        .not_valid_after(dt.datetime(2040, 1, 1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(rogue_ca_key, hashes.SHA256())
    )
    rogue = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, DEVICE_ID)]))
        .issuer_name(rogue_ca.subject)
        .public_key(rogue_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime(2020, 1, 1))
        .not_valid_after(dt.datetime(2040, 1, 1))
        .sign(rogue_ca_key, hashes.SHA256())
    )
    rogue_pem = rogue.public_bytes(serialization.Encoding.PEM).decode()
    payload9 = b"{}"
    r = client.post(
        "/api/device/heartbeat",
        content=payload9,
        headers={**mtls_headers(rogue_pem), **pop_headers(rogue_key, "POST", "/api/device/heartbeat", payload9)},
    )
    check(r.status_code == 401, f"rogue cert rejected ({r.status_code})")

    print("\n[15] no client certificate at all")
    payload10 = b"{}"
    r = client.post(
        "/api/device/heartbeat",
        content=payload10,
        headers=pop_headers(key, "POST", "/api/device/heartbeat", payload10),
    )
    check(r.status_code == 401, f"missing cert rejected ({r.status_code})")

    print("\n" + "=" * 60)
    if _failures:
        print(f"FAILED {len(_failures)} check(s):")
        for f in _failures:
            print("  - " + f)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
