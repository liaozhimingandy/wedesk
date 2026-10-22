"""Regression tests for bring-your-own ROOT/CA handling.

The failure this guards against is destructive and silent: an operator mounts
their real ROOT (often *without* the private key, which is the whole point of
an offline ROOT) and the service happily writes a freshly generated
self-signed ROOT over the top of it. Every device that pinned the real ROOT
then stops trusting anything the service issues.

Each case builds its own PKI directory and re-runs ``Pki.bootstrap`` with
``settings`` mutated, which is enough because bootstrap reads settings at call
time.

Run::

    python tests/test_pki_external.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="wedesk-pki-ext-")
os.environ["DATABASE_URL"] = "sqlite:///" + Path(_TMP, "admin.db").as_posix().replace("\\", "/")
os.environ["PKI_DIR"] = str(Path(_TMP, "unused-pki"))
os.environ["DEFAULT_ADMIN_NAME"] = "admin"
os.environ["DEFAULT_ADMIN_PASSWORD"] = "testpass"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import pki  # noqa: E402
from app.config import settings  # noqa: E402
from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import (  # noqa: E402
    ec,
    ed25519,
    padding,
    rsa,
)
from cryptography.x509.oid import NameOID  # noqa: E402

_failures: list[str] = []
_case = ""


def check(cond: bool, label: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        _failures.append(f"{_case}: {label}")


def _pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _key_pem(key) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _sign(builder, key):
    if isinstance(key, ed25519.Ed25519PrivateKey):
        return builder.sign(private_key=key, algorithm=None)
    return builder.sign(private_key=key, algorithm=hashes.SHA256())


def make_ca(cn: str, *, issuer=None, path_length: int | None = 1,
            key=None, days: int = 3650) -> tuple:
    """Self-signed when ``issuer`` is None, otherwise signed by ``issuer``."""
    key = key or ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer[1].subject if issuer else subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(
            (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).replace(tzinfo=None)
        )
        .not_valid_after(
            (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).replace(tzinfo=None)
        )
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=path_length), critical=True
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=True,
                crl_sign=True, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
    )
    cert = _sign(builder, issuer[0] if issuer else key)
    return key, cert


def make_leaf_csr(key, cn: str) -> x509.CertificateSigningRequest:
    return (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
        .sign(key, hashes.SHA256() if not isinstance(key, ed25519.Ed25519PrivateKey) else None)
    )


def chained(leaf: x509.Certificate, ca: x509.Certificate, root: x509.Certificate) -> bool:
    """leaf <- ca <- root, by real signature verification."""
    if leaf.issuer != ca.subject or ca.issuer != root.subject:
        return False
    pub = ca.public_key()
    try:
        if isinstance(pub, ed25519.Ed25519PublicKey):
            pub.verify(leaf.signature, leaf.tbs_certificate_bytes)
        elif isinstance(pub, ec.EllipticCurvePublicKey):
            pub.verify(leaf.signature, leaf.tbs_certificate_bytes,
                       ec.ECDSA(leaf.signature_hash_algorithm))
        elif isinstance(pub, rsa.RSAPublicKey):
            pub.verify(leaf.signature, leaf.tbs_certificate_bytes,
                       padding.PKCS1v15(), leaf.signature_hash_algorithm)
        else:
            return False
    except Exception:
        return False
    return True


def fresh_dir(name: str) -> Path:
    base = Path(_TMP, name)
    base.mkdir(parents=True, exist_ok=True)
    return base


def reset_settings(**over) -> None:
    """Restore the defaults every case expects, then apply overrides."""
    defaults = dict(
        pki_root_cn="WeDesk Root CA",
        pki_ca_cn="WeDesk Device CA",
        pki_root_days=7300,
        pki_ca_days=3650,
        pki_key_type="ec256",
        pki_auto_create=True,
        pki_allow_unchained_ca=False,
        pki_root_key_file="",
        pki_root_cert_file="",
        pki_ca_key_file="",
        pki_ca_cert_file="",
        pki_key_password=None,
        pki_crl_url="",
        pki_leaf_eku_server_auth=False,
        pki_leaf_domain="device.wedesk.alsoapp.com",
        pki_device_cert_days=90,
    )
    for k, v in defaults.items():
        setattr(settings, k, v)
    for k, v in over.items():
        setattr(settings, k, v)
    pki.reset()


def boot(pki_dir: Path, **over):
    reset_settings(**over)
    settings.pki_dir = str(pki_dir)
    return pki.Pki.bootstrap(str(pki_dir))


def expect_error(pki_dir: Path, needle: str, **over):
    try:
        boot(pki_dir, **over)
    except pki.PkiError as exc:
        return str(exc)
    except Exception as exc:  # wrong exception type is still a failure
        return f"<unexpected {type(exc).__name__}: {exc}>"
    return "<no error raised>"


# ---------------------------------------------------------------------------
# A. offline ROOT (cert only) + supplied CA  -> used as-is, never rewritten
# ---------------------------------------------------------------------------
_case = "A 离线 ROOT + 自带 CA"
print(f"\n[{_case}]")
root_key, root_cert = make_ca("Customer Offline Root")
ca_key, ca_cert = make_ca("Customer Device CA", issuer=(root_key, root_cert),
                          path_length=0)

d = fresh_dir("A")
(d / "root.crt").write_bytes(_pem(root_cert))          # no root.key on purpose
(d / "ca.key").write_bytes(_key_pem(ca_key))
(d / "ca.crt").write_bytes(_pem(ca_cert))
root_before = (d / "root.crt").read_bytes()
ca_before = (d / "ca.crt").read_bytes()

ca_obj = boot(d)
check(ca_obj.root_source == "provided", "ROOT 标记为 provided（未被重新生成）")
check(ca_obj.ca_source == "provided", "CA 标记为 provided（未被重新生成）")
check(ca_obj.root_key is None, "离线 ROOT 无私钥仍可用")
check((d / "root.crt").read_bytes() == root_before, "root.crt 字节未被改写")
check((d / "ca.crt").read_bytes() == ca_before, "ca.crt 字节未被改写")
check(not (d / "root.key").exists(), "没有凭空生成 root.key")
check(ca_obj.root_cert.subject.rfc4514_string().count("Customer Offline Root") == 1,
      "ROOT 主题仍是客户自己的")
check((d / "ca_chain.crt").exists(), "派生文件 ca_chain.crt 已生成")

leaf_key = ec.generate_private_key(ec.SECP256R1())
issued = ca_obj.issue(make_leaf_csr(leaf_key, "123456789012"), device_id="123456789012",
                      cert_type="device", days=90)
leaf = x509.load_pem_x509_certificate(issued.certificate_pem.encode())
check(chained(leaf, ca_obj.ca_cert, ca_obj.root_cert),
      "签发的叶子证书能链回客户的 ROOT")

# A second boot must be a no-op, not a re-mint.
ca_obj2 = boot(d)
check(ca_obj2.ca_cert.serial_number == ca_obj.ca_cert.serial_number,
      "二次启动证书序列号不变（幂等）")

# ---------------------------------------------------------------------------
# B. PKI_AUTO_CREATE=0 and nothing supplied -> loud failure
# ---------------------------------------------------------------------------
_case = "B 禁止自动生成"
print(f"\n[{_case}]")
d = fresh_dir("B")
msg = expect_error(d, "PKI_AUTO_CREATE", pki_auto_create=False)
check("PKI_AUTO_CREATE" in msg, f"空目录 + AUTO_CREATE=0 明确报错：{msg[:80]}")
check(not any((d / n).exists() for n in ("root.crt", "root.key", "ca.crt", "ca.key")),
      "失败时未留下半套生成的文件")

# ---------------------------------------------------------------------------
# C. ROOT without key, no CA supplied -> cannot mint an intermediate
# ---------------------------------------------------------------------------
_case = "C 离线 ROOT 但缺 CA"
print(f"\n[{_case}]")
d = fresh_dir("C")
(d / "root.crt").write_bytes(_pem(root_cert))
before = (d / "root.crt").read_bytes()
msg = expect_error(d, "root.key")
check("root.key" in msg or "ca.crt" in msg or "ca.key" in msg,
      f"提示需要自备 CA 或临时挂载 root.key：{msg[:90]}")
check((d / "root.crt").read_bytes() == before, "ROOT 未被覆盖")
check(not (d / "ca.crt").exists(), "没有用错误的根签发 CA")

# ---------------------------------------------------------------------------
# D. key/cert mismatch -> refuse, never regenerate over them
# ---------------------------------------------------------------------------
_case = "D 密钥与证书不匹配"
print(f"\n[{_case}]")
d = fresh_dir("D")
other_key = ec.generate_private_key(ec.SECP256R1())
(d / "ca.key").write_bytes(_key_pem(other_key))     # wrong key for ca_cert
(d / "ca.crt").write_bytes(_pem(ca_cert))
(d / "root.crt").write_bytes(_pem(root_cert))
ca_bytes = (d / "ca.crt").read_bytes()
msg = expect_error(d, "does not match")
check("does not match" in msg, f"不匹配时报错而非重建：{msg[:80]}")
check((d / "ca.crt").read_bytes() == ca_bytes, "不匹配的证书文件原样保留")

# ---------------------------------------------------------------------------
# E. explicitly configured path that does not exist -> hard error
# ---------------------------------------------------------------------------
_case = "E 显式路径不存在"
print(f"\n[{_case}]")
d = fresh_dir("E")
missing = str(Path(_TMP, "nope", "my-root.crt"))
msg = expect_error(d, "does not exist", pki_root_cert_file=missing)
check("does not exist" in msg, f"显式路径缺失直接失败：{msg[:80]}")
check(not (d / "root.crt").exists(), "没有退回自动生成")

# ---------------------------------------------------------------------------
# F. CA that is not issued by the supplied ROOT -> rejected by default
# ---------------------------------------------------------------------------
_case = "F CA 与 ROOT 不成链"
print(f"\n[{_case}]")
d = fresh_dir("F")
rogue_root_key, rogue_root = make_ca("Someone Elses Root")
foreign_key, foreign_ca = make_ca("Foreign CA", issuer=(rogue_root_key, rogue_root),
                                  path_length=0)
(d / "root.crt").write_bytes(_pem(root_cert))
(d / "ca.crt").write_bytes(_pem(foreign_ca))
(d / "ca.key").write_bytes(_key_pem(foreign_key))
msg = expect_error(d, "not issued by")
check("not issued by" in msg, f"不成链默认拒绝：{msg[:80]}")
ok = True
try:
    boot(d, pki_allow_unchained_ca=True)
except Exception as exc:
    ok = False
    print("   ", exc)
check(ok, "PKI_ALLOW_UNCHAINED_CA=1 可显式放行")

# ---------------------------------------------------------------------------
# G. combined PEM (key + cert in one file) is recognised as supplied
# ---------------------------------------------------------------------------
_case = "G 合并 PEM"
print(f"\n[{_case}]")
d = fresh_dir("G")
(d / "root.pem").write_bytes(_key_pem(root_key) + _pem(root_cert))
(d / "ca.pem").write_bytes(_key_pem(ca_key) + _pem(ca_cert))
obj = boot(d)
check(obj.root_source == "provided", "root.pem 里的证书被识别为 provided")
check(obj.ca_source == "provided", "ca.pem 里的证书被识别为 provided")
check(obj.ca_key is not None, "ca.pem 里的私钥被识别")
check(obj.ca_cert.serial_number == ca_cert.serial_number, "用的是提供的 CA 而非新签发的")

# ---------------------------------------------------------------------------
# H. empty dir with auto-create still self-bootstraps (dev path) and is idempotent
# ---------------------------------------------------------------------------
_case = "H 空目录自举仍可用"
print(f"\n[{_case}]")
d = fresh_dir("H")
obj = boot(d)
check(obj.root_source == "generated", "空目录自动生成 ROOT")
check(obj.ca_source == "generated", "空目录自动生成 CA")
first_serial = obj.ca_cert.serial_number
obj2 = boot(d)
check(obj2.ca_cert.serial_number == first_serial, "再次启动不会重新生成（覆盖防护生效）")
check(obj2.root_source == "generated", "二次启动仍标记为 generated（来源如实上报）")

# ---------------------------------------------------------------------------
# I. a leaf-only (non-CA) certificate cannot be used as the intermediate
# ---------------------------------------------------------------------------
_case = "I 非 CA 证书冒充中间 CA"
print(f"\n[{_case}]")
d = fresh_dir("I")
leaf_key2 = ec.generate_private_key(ec.SECP256R1())
ee = (
    x509.CertificateBuilder()
    .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "not a CA")]))
    .issuer_name(root_cert.subject)
    .public_key(leaf_key2.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before((dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).replace(tzinfo=None))
    .not_valid_after((dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=365)).replace(tzinfo=None))
    .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
)
(d / "root.crt").write_bytes(_pem(root_cert))
(d / "ca.crt").write_bytes(_pem(_sign(ee, root_key)))
(d / "ca.key").write_bytes(_key_pem(leaf_key2))
msg = expect_error(d, "not a CA")
check("not a CA" in msg, f"拒绝用终端证书当 CA：{msg[:80]}")


# ---------------------------------------------------------------------------
# J. provenance marker only claims material we actually generated
# ---------------------------------------------------------------------------
_case = "J 来源标记可信度"
print(f"\n[{_case}]")
d = fresh_dir("J")
obj = boot(d)
check(obj.root_source == "generated", "首次生成后标记为 generated")
marker_path = d / ".wedesk-pki-origin.json"
check(marker_path.exists(), "写入了来源标记文件")
marker = json.loads(marker_path.read_text(encoding="utf-8"))
marker["root_serial"] = "deadbeef"          # as if the file were swapped
marker_path.write_text(json.dumps(marker), encoding="utf-8")
obj = boot(d)
check(obj.root_source == "provided", "标记对不上时不冒认 generated，如实报 provided")


# ---------------------------------------------------------------------------
# K. CA supplied but no ROOT -> refuse up front, write nothing
# ---------------------------------------------------------------------------
_case = "K 只给 CA 不给 ROOT"
print(f"\n[{_case}]")
d = fresh_dir("K")
(d / "ca.crt").write_bytes(_pem(ca_cert))
(d / "ca.key").write_bytes(_key_pem(ca_key))
msg = expect_error(d, "no ROOT")
check("no ROOT" in msg, f"提示缺少 ROOT：{msg[:80]}")
check(not (d / "root.crt").exists(), "没有先生成一套临时 ROOT 再失败")
check(not (d / "root.key").exists(), "没有留下无主的 root.key")


print("\n" + "=" * 60)
if _failures:
    print(f"{len(_failures)} 项失败：")
    for f in _failures:
        print("  -", f)
    sys.exit(1)
print("全部通过：自带 ROOT/CA 不会被覆盖、缺失/错配/不成链均明确报错")
