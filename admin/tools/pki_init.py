#!/usr/bin/env python
"""Offline ROOT/CA initialiser for the wedesk device PKI.

Deployment model this tool produces
-----------------------------------
::

    ROOT  ── certificate only ──> baked into every device (the trust anchor)
           └─ private key ──────> offline safe, NEVER enters a container

    CA    ── certificate + private key ──> the admin (FastAPI) service only;
                                           it is what signs every device cert

The service never needs the ROOT private key: the ROOT key is only required to
mint a new intermediate, and minting happens here, once, by hand. That is why
``root.key`` is written to ``offline/`` and the copy handed to the server is
certificate-only.

Usage
-----
Generate a brand new hierarchy::

    python admin/tools/pki_init.py --out-dir ./pki-out

Sign a CA under a ROOT you already have (the ROOT key is needed once, here,
and can be deleted afterwards)::

    python admin/tools/pki_init.py --out-dir ./pki-out \
        --root-cert ./my-root.crt --root-key ./my-root.key

Verify a directory that is about to be mounted as ``PKI_DIR``::

    python admin/tools/pki_init.py --check ./pki-out/server

Nothing is ever overwritten: re-running into a non-empty output directory
fails unless ``--force`` is given.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.x509.oid import NameOID

DEFAULT_ROOT_CN = "WeDesk Root CA"
DEFAULT_CA_CN = "WeDesk Device CA"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _naive(value: dt.datetime) -> dt.datetime:
    return value.astimezone(dt.timezone.utc).replace(tzinfo=None)


def _new_key(kind: str):
    kind = (kind or "ec256").lower()
    if kind == "ed25519":
        return ed25519.Ed25519PrivateKey.generate()
    if kind in ("ec256", "ec", "p256", "prime256v1"):
        return ec.generate_private_key(ec.SECP256R1())
    if kind in ("ec384", "p384"):
        return ec.generate_private_key(ec.SECP384R1())
    if kind.startswith("rsa"):
        return rsa.generate_private_key(public_exponent=65537, key_size=int(kind[3:] or 2048))
    raise SystemExit(f"unsupported --key-type: {kind}")


def _sign(builder, key):
    if isinstance(key, ed25519.Ed25519PrivateKey):
        return builder.sign(private_key=key, algorithm=None)
    return builder.sign(private_key=key, algorithm=hashes.SHA256())


def _pem(obj) -> bytes:
    if isinstance(obj, x509.Certificate):
        return obj.public_bytes(serialization.Encoding.PEM)
    return obj.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _ca_builder(subject_cn: str, issuer_name: x509.Name, key, *, days: int,
                path_length: int | None) -> x509.CertificateBuilder:
    """Policy mirrored from ``app/pki.py`` — BasicConstraints + keyCertSign +
    crlSign, nothing else. Keep the two in sync when policy changes."""
    return (
        x509.CertificateBuilder()
        .subject_name(x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, subject_cn),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "WeDesk"),
        ]))
        .issuer_name(issuer_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_naive(_utcnow() - dt.timedelta(hours=1)))
        .not_valid_after(_naive(_utcnow() + dt.timedelta(days=days)))
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
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
    )


def _fingerprint(cert: x509.Certificate) -> str:
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def _write(path: Path, data: bytes, *, force: bool, secret: bool = False) -> None:
    if path.exists() and not force:
        raise SystemExit(
            f"refusing to overwrite {path}; delete it or pass --force"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if secret:
        try:
            path.chmod(0o600)
        except OSError:  # Windows / exotic fs
            pass


def _describe(cert: x509.Certificate, label: str) -> str:
    try:
        na = cert.not_valid_after_utc
    except AttributeError:  # cryptography < 42
        na = cert.not_valid_after.replace(tzinfo=dt.timezone.utc)
    return (
        f"  {label}\n"
        f"    subject : {cert.subject.rfc4514_string()}\n"
        f"    issuer  : {cert.issuer.rfc4514_string()}\n"
        f"    serial  : {format(cert.serial_number, 'x')}\n"
        f"    valid   : until {na.date().isoformat()}\n"
        f"    sha256  : {_fingerprint(cert)}\n"
        f"    key     : {type(cert.public_key()).__name__}"
        + (
            f" {cert.public_key().key_size} bit"
            if isinstance(cert.public_key(), rsa.RSAPublicKey)
            else ""
        )
    )


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------

def do_generate(args: argparse.Namespace) -> int:
    out = Path(args.out_dir).resolve()
    device_dir = out / "device"      # what ships inside the client
    server_dir = out / "server"      # what goes into PKI_DIR
    offline_dir = out / "offline"    # what must never see a container

    # --- ROOT -------------------------------------------------------------
    if args.root_cert:
        root_cert_path = Path(args.root_cert)
        if not root_cert_path.exists():
            raise SystemExit(f"--root-cert {root_cert_path} does not exist")
        root_cert = x509.load_pem_x509_certificate(root_cert_path.read_bytes())
        root_key = None
        if not args.root_key:
            raise SystemExit(
                "--root-cert requires --root-key as well: signing a new "
                "intermediate needs the ROOT private key exactly once, right "
                "here. It is not needed again afterwards."
            )
        root_key_path = Path(args.root_key)
        if not root_key_path.exists():
            raise SystemExit(f"--root-key {root_key_path} does not exist")
        root_key = serialization.load_pem_private_key(
            root_key_path.read_bytes(), password=None
        )
        reuse = True
    else:
        root_key = _new_key(args.root_key_type)
        subject = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, args.root_cn),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "WeDesk"),
        ])
        builder = _ca_builder(args.root_cn, subject, root_key, days=args.root_days,
                              path_length=1)
        root_cert = _sign(builder, root_key)
        reuse = False

    # --- CA (signed by the ROOT, this is the last time the ROOT key is used) --
    ca_key = _new_key(args.ca_key_type)
    ca_builder = (
        _ca_builder(args.ca_cn, root_cert.subject, ca_key, days=args.ca_days,
                    path_length=0)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(
                root_cert.public_key()
            ),
            critical=False,
        )
    )
    ca_cert = _sign(ca_builder, root_key)

    # --- layout -----------------------------------------------------------
    # The ROOT key stays out of server/ entirely; that directory is meant to be
    # mounted read-only into the container.
    if not reuse:
        _write(offline_dir / "root.key", _pem(root_key), force=args.force, secret=True)
    _write(device_dir / "root.crt", _pem(root_cert), force=args.force)
    _write(server_dir / "root.crt", _pem(root_cert), force=args.force)
    _write(server_dir / "ca.crt", _pem(ca_cert), force=args.force)
    _write(server_dir / "ca.key", _pem(ca_key), force=args.force, secret=True)
    _write(
        server_dir / "ca_chain.crt",
        _pem(ca_cert).strip() + b"\n" + _pem(root_cert).strip() + b"\n",
        force=args.force,
    )

    print("\n生成完成：\n")
    print(_describe(root_cert, "ROOT（信任锚）"))
    print(_describe(ca_cert, "CA（签发者）"))
    print("\n文件去向：\n")
    print(f"  {device_dir / 'root.crt'}")
    print("      → 内置到设备端/客户端，作为唯一信任锚（建议同时做证书固定 pinning）")
    print(f"  {server_dir / 'root.crt'}")
    print(f"  {server_dir / 'ca.crt'}")
    print(f"  {server_dir / 'ca.key'}")
    print("      → 三个一起放进容器的 PKI_DIR（或分别用")
    print("        PKI_ROOT_CERT_FILE / PKI_CA_CERT_FILE / PKI_CA_KEY_FILE 指定）")
    if not reuse:
        print(f"  {offline_dir / 'root.key'}")
        print("      → 离线保管，【绝不】进容器/镜像/备份到公共位置。")
        print("        只有将来要签新的中间 CA 时才需要它。")
    print("\n服务端建议一并设置：\n")
    print("  PKI_AUTO_CREATE=0     # 材料齐全就好；缺了就停机报错，不自己造")
    print(f"  ROOT 指纹（写进客户端做固定）：{_fingerprint(root_cert)}")
    print("\n下一步：\n")
    print(f"  python admin/tools/pki_init.py --check {server_dir}")
    return 0


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------

def do_check(args: argparse.Namespace) -> int:
    base = Path(args.check).resolve()
    if not base.exists():
        raise SystemExit(f"{base} does not exist")

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app import pki  # noqa: E402  (config is env-driven; nothing is written)
    from app.config import settings  # noqa: E402

    settings.pki_dir = str(base)
    settings.pki_auto_create = False
    settings.pki_root_key_file = ""
    settings.pki_root_cert_file = ""
    settings.pki_ca_key_file = ""
    settings.pki_ca_cert_file = ""
    pki.reset()

    problems: list[str] = []
    try:
        ca = pki.Pki.bootstrap(str(base))
    except pki.PkiError as exc:
        print(f"\n[FAIL] {base} 不能作为 PKI_DIR：\n  {exc}\n")
        return 1

    print(f"\n[OK] {base} 可以作为 PKI_DIR\n")
    print(_describe(ca.root_cert, "ROOT"))
    print(_describe(ca.ca_cert, "CA"))
    print(f"\n  ROOT 来源: {ca.root_source}   私钥在场: {ca.root_key is not None}")
    print(f"  CA   来源: {ca.ca_source}   私钥在场: {ca.ca_key is not None}\n")

    # The deployment model: ROOT cert only, CA cert + key.
    if ca.root_key is not None:
        problems.append(
            "ROOT 私钥也在这个目录里 —— 按部署模型它应该离线保存，"
            "只把 root.crt 挂进来（服务端不需要 ROOT 私钥）"
        )
    if ca.ca_key is None:
        problems.append("CA 私钥缺失 —— 没有它签不了设备证书")

    # Clients pin the ROOT; the service must hand out exactly that ROOT.
    served = (base / "root.crt").read_bytes()
    print(f"  下发链 ca_chain.crt 首项 CN: "
          f"{ca.ca_cert.subject.rfc4514_string()}")
    print(f"  root.crt 大小: {len(served)} 字节\n")

    if problems:
        print("需要注意：")
        for p in problems:
            print(f"  - {p}")
        print()
        return 1
    print("符合部署模型：ROOT 仅证书（私钥离线）+ CA 证书与私钥配套。\n")
    return 0


# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="离线生成/校验 wedesk 设备证书体系的 ROOT 与 CA",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--out-dir", default="./pki-out",
                        help="输出根目录（默认 ./pki-out）")
    parser.add_argument("--root-cn", default=DEFAULT_ROOT_CN)
    parser.add_argument("--ca-cn", default=DEFAULT_CA_CN)
    parser.add_argument("--root-key-type", default="ec384",
                        help="ROOT 密钥类型，默认 ec384（ROOT 用强一档的密钥）")
    parser.add_argument("--ca-key-type", default="ec256",
                        help="CA 密钥类型，默认 ec256")
    parser.add_argument("--root-days", type=int, default=7300)
    parser.add_argument("--ca-days", type=int, default=3650)
    parser.add_argument("--root-cert", default="",
                        help="用已有 ROOT 证书（需同时给 --root-key）")
    parser.add_argument("--root-key", default="", help="已有 ROOT 的私钥（只用一次）")
    parser.add_argument("--force", action="store_true", help="允许覆盖已存在的文件")
    parser.add_argument("--check", metavar="DIR", default="",
                        help="校验某目录能否直接作为 PKI_DIR 挂载")
    args = parser.parse_args(argv)

    if args.check:
        return do_check(args)
    return do_generate(args)


if __name__ == "__main__":
    raise SystemExit(main())
