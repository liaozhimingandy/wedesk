"""X.509 PKI engine for wedesk device identity.

Hierarchy
---------
::

    ROOT (self-signed, 20y, offline in production)
      └── CA  (intermediate "WeDesk Device CA", 10y, path_length=0)
            ├── leaf: device  (mTLS clientAuth, ~90d)
            └── leaf: app     (mTLS clientAuth, ~180d)

The admin service is the only component that ever touches the CA private key.
hbbs/hbbr and the clients are **not** modified: they keep using their existing
key pairs; this module only issues the extra certificate layer that later
phases (P2P end-to-end encryption) will consume.

Key material lives in ``PKI_DIR`` as PEM files (default ``/data/pki``)::

    root.key / root.crt    ROOT CA
    ca.key   / ca.crt      intermediate CA (signs every leaf)
    ca_chain.crt           ca.crt + root.crt — what clients must trust/install

On first start the ROOT and CA are generated automatically (dev convenience).
In production, mount a pre-generated ROOT/CA or replace the generated ROOT with
your offline one and keep ``root.key`` out of the container.

Security notes
--------------
* Extensions are **never** copied from the CSR. A device could otherwise ask
  for ``BasicConstraints CA:TRUE`` and mint its own subtree. The CA builds every
  extension from server-side policy.
* Serial numbers are random 128-bit and recorded in the database, so revocation
  and CRL generation are exact.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .config import settings

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

_UTC = dt.timezone.utc
_LOCK = threading.Lock()


class PkiError(Exception):
    """Raised for operator-facing PKI problems (bad CSR, missing key, ...)."""


def now() -> dt.datetime:
    return dt.datetime.now(_UTC)


def _naive_utc(value: dt.datetime) -> dt.datetime:
    """``cryptography`` >= 42 returns aware datetimes from the ``*_utc`` getters
    but still wants naive ones on the builder side; normalise to naive UTC."""
    if value.tzinfo is not None:
        return value.astimezone(_UTC).replace(tzinfo=None)
    return value


def cert_not_before(cert: x509.Certificate) -> dt.datetime:
    try:
        return cert.not_valid_before_utc
    except AttributeError:  # cryptography < 42
        return cert.not_valid_before.replace(tzinfo=_UTC)


def cert_not_after(cert: x509.Certificate) -> dt.datetime:
    try:
        return cert.not_valid_after_utc
    except AttributeError:  # cryptography < 42
        return cert.not_valid_after.replace(tzinfo=_UTC)


def cert_not_valid_before(cert: x509.Certificate):
    return _naive_utc(cert_not_before(cert))


def cert_not_valid_after(cert: x509.Certificate):
    return _naive_utc(cert_not_after(cert))


def spki_fingerprint(cert_or_csr) -> str:
    """SHA-256 (hex, lower) of the DER-encoded SubjectPublicKeyInfo.

    Stable across renewals as long as the device keeps its key pair — this is
    the value the mTLS layer uses to bind a connection to a database record
    independently of the certificate serial.
    """
    der = cert_or_csr.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(der).hexdigest()


# ---------------------------------------------------------------------------
# Key handling
# ---------------------------------------------------------------------------

def _new_key(kind: str):
    kind = (kind or "ec256").lower()
    if kind == "ed25519":
        return ed25519.Ed25519PrivateKey.generate()
    if kind in ("ec256", "ec", "p256", "prime256v1"):
        return ec.generate_private_key(ec.SECP256R1())
    if kind in ("ec384", "p384"):
        return ec.generate_private_key(ec.SECP384R1())
    if kind.startswith("rsa"):
        bits = int(kind[3:] or 2048)
        return rsa.generate_private_key(public_exponent=65537, key_size=bits)
    raise PkiError(f"unsupported PKI_KEY_TYPE: {kind}")


def _sign(builder, key):
    """Sign a builder, coping with Ed25519 (algorithm must be ``None``)."""
    if isinstance(key, ed25519.Ed25519PrivateKey):
        return builder.sign(private_key=key, algorithm=None)
    return builder.sign(private_key=key, algorithm=hashes.SHA256())


def _write_key(path: Path, key) -> None:
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    try:
        path.chmod(0o600)
    except OSError:  # Windows / exotic fs
        pass


def _write_cert(path: Path, cert: x509.Certificate) -> None:
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def _load_cert(path: Path) -> x509.Certificate | None:
    if not path.exists():
        return None
    return x509.load_pem_x509_certificate(path.read_bytes())


def _load_key(path: Path):
    if not path.exists():
        return None
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


# ---------------------------------------------------------------------------
# Issued leaf result
# ---------------------------------------------------------------------------

@dataclass
class IssuedCert:
    certificate_pem: str
    serial_hex: str
    serial_int: int
    fingerprint: str
    not_before: dt.datetime
    not_after: dt.datetime
    subject_cn: str


def _subject(cn: str) -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, cn),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "WeDesk"),
        ]
    )


# ---------------------------------------------------------------------------
# The CA
# ---------------------------------------------------------------------------

class Pki:
    """Loads (or creates) ROOT + intermediate CA and signs leaf certificates."""

    def __init__(self, root_key, root_cert: x509.Certificate, ca_key, ca_cert: x509.Certificate,
                 root_pem: str, ca_pem: str):
        self.root_key = root_key
        self.root_cert = root_cert
        self.ca_key = ca_key
        self.ca_cert = ca_cert
        self.root_pem = root_pem
        self.ca_pem = ca_pem

    # -- construction ------------------------------------------------------
    @classmethod
    def bootstrap(cls, directory: str | None = None) -> "Pki":
        directory = directory or settings.pki_dir
        base = Path(directory)
        base.mkdir(parents=True, exist_ok=True)

        root = cls._ensure_ca(
            base / "root.key",
            base / "root.crt",
            cn=settings.pki_root_cn,
            days=settings.pki_root_days,
            path_length=1,
            issuer=None,
        )
        ca = cls._ensure_ca(
            base / "ca.key",
            base / "ca.crt",
            cn=settings.pki_ca_cn,
            days=settings.pki_ca_days,
            path_length=0,
            issuer=root,
        )

        root_pem = (base / "root.crt").read_text(encoding="utf-8")
        ca_pem = (base / "ca.crt").read_text(encoding="utf-8")
        (base / "ca_chain.crt").write_text(ca_pem.strip() + "\n" + root_pem.strip() + "\n",
                                           encoding="utf-8")

        return cls(
            root_key=root[0], root_cert=root[1],
            ca_key=ca[0], ca_cert=ca[1],
            root_pem=root_pem, ca_pem=ca_pem,
        )

    @staticmethod
    def _ensure_ca(key_path: Path, crt_path: Path, *, cn: str, days: int,
                   path_length: int, issuer: tuple | None):
        key = _load_key(key_path)
        cert = _load_cert(crt_path)
        if key is not None and cert is not None:
            # Sanity: the on-disk key must match the on-disk certificate.
            try:
                same = (
                    key.public_key().public_bytes(
                        serialization.Encoding.DER,
                        serialization.PublicFormat.SubjectPublicKeyInfo,
                    )
                    == cert.public_key().public_bytes(
                        serialization.Encoding.DER,
                        serialization.PublicFormat.SubjectPublicKeyInfo,
                    )
                )
            except Exception:
                same = False
            if same:
                return key, cert

        key = _new_key(settings.pki_key_type)
        subject = _subject(cn)
        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer[1].subject if issuer else subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_naive_utc(now() - dt.timedelta(hours=1)))
            .not_valid_after(_naive_utc(now() + dt.timedelta(days=days)))
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=path_length), critical=True
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
            )
        )
        if issuer is not None:
            builder = builder.add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(
                    issuer[0].public_key()
                ),
                critical=False,
            )
        cert = _sign(builder, issuer[0] if issuer else key)

        _write_key(key_path, key)
        _write_cert(crt_path, cert)
        return key, cert

    # -- public material ----------------------------------------------------
    @property
    def chain_pem(self) -> str:
        """Intermediate + ROOT, in the order TLS clients expect."""
        return self.ca_pem.strip() + "\n" + self.root_pem.strip() + "\n"

    # -- CSR intake ---------------------------------------------------------
    @staticmethod
    def parse_csr(csr_pem: str) -> x509.CertificateSigningRequest:
        """Parse a PEM CSR, rejecting anything malformed or self-inconsistent."""
        if not csr_pem or not csr_pem.strip():
            raise PkiError("csr is required")
        text = csr_pem.strip()
        if "-----BEGIN" not in text:  # tolerate bare base64 DER
            import base64

            try:
                der = base64.b64decode(text)
            except Exception:
                raise PkiError("csr must be PEM (or base64 DER)")
            try:
                return x509.load_der_x509_csr(der)
            except Exception:
                raise PkiError("csr is not a valid PKCS#10 request")
        try:
            csr = x509.load_pem_x509_csr(text.encode("utf-8"))
        except Exception as exc:
            raise PkiError(f"csr is not a valid PKCS#10 request: {exc}")

        # ``is_signature_valid`` was a method until cryptography 42 and became a
        # property afterwards; support both so the pin can move freely.
        valid = csr.is_signature_valid
        if callable(valid):
            valid = valid()
        if not valid:
            raise PkiError("csr signature is invalid (key does not match request)")

        pub = csr.public_key()
        if isinstance(pub, rsa.RSAPublicKey) and pub.key_size < 2048:
            raise PkiError("RSA key too small (minimum 2048 bits)")
        return csr

    # -- leaf issuance ------------------------------------------------------
    def issue(self, csr: x509.CertificateSigningRequest, *, device_id: str,
              cert_type: str, days: int) -> IssuedCert:
        """Sign a leaf certificate.

        Every extension is server-side policy; nothing is taken from the CSR.
        """
        if cert_type not in ("device", "app"):
            raise PkiError(f"unknown cert_type: {cert_type}")

        serial = x509.random_serial_number()
        nb = _naive_utc(now() - dt.timedelta(minutes=5))  # small clock-skew slack
        na = _naive_utc(now() + dt.timedelta(days=days))

        cn = device_id
        eku = [ExtendedKeyUsageOID.CLIENT_AUTH]
        if settings.pki_leaf_eku_server_auth:
            eku.append(ExtendedKeyUsageOID.SERVER_AUTH)

        san: list[x509.GeneralName] = [
            x509.DNSName(f"{device_id}.{settings.pki_leaf_domain}"),
            x509.UniformResourceIdentifier(f"urn:wedesk:{cert_type}:{device_id}"),
        ]

        builder = (
            x509.CertificateBuilder()
            .subject_name(_subject(cn))
            .issuer_name(self.ca_cert.subject)
            .public_key(csr.public_key())
            .serial_number(serial)
            .not_valid_before(nb)
            .not_valid_after(na)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=not isinstance(csr.public_key(), ed25519.Ed25519PublicKey),
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ), critical=True)
            .add_extension(x509.ExtendedKeyUsage(eku), critical=False)
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(csr.public_key()), critical=False
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self.ca_key.public_key()),
                critical=False,
            )
        )
        if settings.pki_crl_url:
            builder = builder.add_extension(
                x509.CRLDistributionPoints(
                    [
                        x509.DistributionPoint(
                            full_name=[x509.UniformResourceIdentifier(settings.pki_crl_url)],
                            relative_name=None,
                            reasons=None,
                            crl_issuer=None,
                        )
                    ]
                ),
                critical=False,
            )

        cert = _sign(builder, self.ca_key)
        pem = cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
        return IssuedCert(
            certificate_pem=pem,
            serial_hex=format(serial, "x"),
            serial_int=serial,
            fingerprint=spki_fingerprint(cert),
            not_before=cert_not_before(cert),
            not_after=cert_not_after(cert),
            subject_cn=cn,
        )

    # -- verification -------------------------------------------------------
    def verify_leaf(self, cert: x509.Certificate) -> bool:
        """True when ``cert`` was really signed by our intermediate CA."""
        try:
            if cert.issuer != self.ca_cert.subject:
                return False
            pub = self.ca_cert.public_key()
            if isinstance(pub, ed25519.Ed25519PublicKey):
                pub.verify(cert.signature, cert.tbs_certificate_bytes)
            elif isinstance(pub, ec.EllipticCurvePublicKey):
                pub.verify(
                    cert.signature,
                    cert.tbs_certificate_bytes,
                    ec.ECDSA(cert.signature_hash_algorithm),
                )
            elif isinstance(pub, rsa.RSAPublicKey):
                pub.verify(
                    cert.signature,
                    cert.tbs_certificate_bytes,
                    padding.PKCS1v15(),
                    cert.signature_hash_algorithm,
                )
            else:
                return False
            return True
        except Exception:
            return False

    def revoked_serials_to_crl(self, entries) -> str:
        """Build a CRL (PEM) covering ``[(serial_hex, revoked_at_datetime)]``."""
        builder = (
            x509.CertificateRevocationListBuilder()
            .issuer_name(self.ca_cert.subject)
            .last_update(_naive_utc(now()))
            .next_update(_naive_utc(now() + dt.timedelta(days=settings.pki_crl_next_days)))
        )
        for serial_hex, revoked_at in entries:
            try:
                serial_int = int(str(serial_hex), 16)
            except (TypeError, ValueError):
                continue
            builder = builder.add_revoked_certificate(
                x509.RevokedCertificateBuilder()
                .serial_number(serial_int)
                .revocation_date(_naive_utc(revoked_at))
                .build()
            )
        crl = _sign(builder, self.ca_key)
        return crl.public_bytes(serialization.Encoding.PEM).decode("utf-8")

    def info(self) -> dict:
        return {
            "root": {
                "subject": self.root_cert.subject.rfc4514_string(),
                "not_after": cert_not_after(self.root_cert).isoformat(),
                "serial": format(self.root_cert.serial_number, "x"),
            },
            "ca": {
                "subject": self.ca_cert.subject.rfc4514_string(),
                "not_after": cert_not_after(self.ca_cert).isoformat(),
                "serial": format(self.ca_cert.serial_number, "x"),
            },
            "leaf_domain": settings.pki_leaf_domain,
            "device_cert_days": settings.pki_device_cert_days,
            "app_cert_days": settings.pki_app_cert_days,
            "renew_window_days": settings.pki_renew_window_days,
        }


_INSTANCE: Pki | None = None


def get() -> Pki:
    """Process-wide singleton; the first call generates ROOT/CA if missing."""
    global _INSTANCE
    if _INSTANCE is None:
        with _LOCK:
            if _INSTANCE is None:
                _INSTANCE = Pki.bootstrap()
    return _INSTANCE


def reset() -> None:  # test helper
    global _INSTANCE
    _INSTANCE = None
