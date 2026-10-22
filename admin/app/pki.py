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

Bringing your own ROOT / CA
---------------------------
Put the four files into ``PKI_DIR`` (or point ``PKI_ROOT_CERT_FILE`` /
``PKI_ROOT_KEY_FILE`` / ``PKI_CA_CERT_FILE`` / ``PKI_CA_KEY_FILE`` at absolute
paths). They are used as-is and **never overwritten** — that is the whole point:
clients ship with your ROOT baked in, so silently replacing it would orphan
every deployed device.

Accepted layouts:
* separate key + cert files (typical);
* one combined PEM holding key and certificate (either file name works);
* ROOT **without** its private key (offline ROOT) — legal, and in fact the
  recommended production layout. The ROOT key is only needed to mint a new
  intermediate; as long as ``ca.key`` + ``ca.crt`` are supplied it is never
  touched.

If material is missing the service generates it (dev convenience), unless
``PKI_AUTO_CREATE=0``, in which case startup fails loudly with the exact files
it wants. Generation refuses to write over an existing file, so a half-mounted
secret can never be clobbered.

Security notes
--------------
* Extensions are **never** copied from the CSR. A device could otherwise ask
  for ``BasicConstraints CA:TRUE`` and mint its own subtree. The CA builds every
  extension from server-side policy.
* Serial numbers are random 128-bit and recorded in the database, so revocation
  and CRL generation are exact.
* A supplied CA is signature-checked against the supplied ROOT. A mismatch means
  the two files are not a pair; startup refuses rather than issuing leaves under
  the wrong anchor.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
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
    if path.exists():
        # Generation is only ever allowed into an empty slot; hitting this means
        # two processes raced, or an operator mounted a file after we looked.
        raise PkiError(f"refusing to overwrite existing private key {path}")
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
    if path.exists():
        raise PkiError(f"refusing to overwrite existing certificate {path}")
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


_PEM_BLOCK = re.compile(
    rb"-----BEGIN ([A-Z0-9 ]*PRIVATE KEY[A-Z0-9 ]*)-----\r?\n(.*?)\r?\n?-----END \1-----",
    re.DOTALL,
)
_PEM_CERT = re.compile(
    rb"-----BEGIN CERTIFICATE-----\r?\n(.*?)\r?\n?-----END CERTIFICATE-----", re.DOTALL
)


def _parse_pem_bundle(data: bytes) -> tuple[list[x509.Certificate], list]:
    """Split a PEM file into certificates and private keys.

    Handles the "one file holds both" layout that plenty of CAs hand out, so an
    operator does not have to guess which filename we expect.
    """
    certs: list[x509.Certificate] = []
    keys: list = []
    for match in _PEM_CERT.finditer(data):
        try:
            certs.append(
                x509.load_pem_x509_certificate(
                    b"-----BEGIN CERTIFICATE-----\n"
                    + match.group(1)
                    + b"\n-----END CERTIFICATE-----\n"
                )
            )
        except Exception:
            continue
    for match in _PEM_BLOCK.finditer(data):
        blob = match.group(0)
        try:
            keys.append(
                serialization.load_pem_private_key(
                    blob, password=settings.pki_key_password
                )
            )
        except TypeError:  # encrypted key but no password configured
            raise PkiError(
                "private key is encrypted; set PKI_KEY_PASSWORD (or re-export it "
                "without a passphrase)"
            )
        except Exception:
            continue
    return certs, keys


def _read_material(path: Path) -> tuple[list[x509.Certificate], list]:
    return _parse_pem_bundle(path.read_bytes())


def _load_cert(path: Path) -> x509.Certificate | None:
    if not path.exists():
        return None
    certs, _ = _read_material(path)
    return certs[0] if certs else None


def _load_key(path: Path):
    if not path.exists():
        return None
    _, keys = _read_material(path)
    return keys[0] if keys else None


def _key_matches_cert(key, cert: x509.Certificate) -> bool:
    try:
        return key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ) == cert.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    except Exception:
        return False


# Provenance marker. ``source`` in /api/cert/ca is what tells an operator
# whether the anchors in use are theirs or something this box invented on a
# first boot — and that must survive a restart, otherwise the second boot with
# the same volume would report "provided" and the warning would never fire
# again. It records the serial we generated; a file whose serial does not match
# is somebody else's material.
_ORIGIN_FILE = ".wedesk-pki-origin.json"


def _read_origin(base: Path) -> dict:
    path = base / _ORIGIN_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_origin(base: Path, stem: str, serial: int) -> None:
    data = _read_origin(base)
    data[f"{stem}_serial"] = format(serial, "x")
    data[f"{stem}_generated_at"] = now().isoformat()
    (base / _ORIGIN_FILE).write_text(
        json.dumps(data, indent=2, sort_keys=True), encoding="utf-8"
    )


def _locate(explicit: str, base: Path, names: tuple[str, ...], what: str) -> Path | None:
    """Resolve one material file: explicit env path, else first name that exists.

    An explicitly configured path that does not exist is a hard error rather
    than a fallback — silently ignoring it would generate a brand new CA and
    invalidate every certificate the operator thought they had mounted.
    """
    if explicit:
        path = Path(explicit)
        if not path.exists():
            raise PkiError(
                f"{what} was configured at {path} but the file does not exist; "
                f"mount it or unset the variable"
            )
        return path
    for name in names:
        path = base / name
        if path.exists():
            return path
    return None


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


@dataclass
class CaMaterial:
    """One CA half (ROOT or intermediate) plus where it came from.

    ``source`` is surfaced through ``/api/cert/ca`` and ``/health`` so an
    operator can confirm at a glance that their own ROOT — not a generated
    one — is in use.
    """

    key: object | None  # None is legal for an offline ROOT (cert only)
    cert: x509.Certificate
    source: str  # "provided" | "generated"
    files: dict


def _fail_unchained(root: x509.Certificate, ca: x509.Certificate, why: str) -> None:
    message = (
        f"supplied intermediate CA {ca.subject.rfc4514_string()} is not issued by "
        f"the supplied ROOT {root.subject.rfc4514_string()} ({why})"
    )
    if settings.pki_allow_unchained_ca:
        print(f"[pki][warn] {message} — continuing because PKI_ALLOW_UNCHAINED_CA=1")
        return
    raise PkiError(
        message + "; devices trusting that ROOT will reject every certificate "
        "issued under this CA. Set PKI_ALLOW_UNCHAINED_CA=1 to override."
    )


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
                 root_pem: str, ca_pem: str, *, root_source: str = "generated",
                 ca_source: str = "generated", root_files: dict | None = None,
                 ca_files: dict | None = None):
        self.root_key = root_key
        self.root_cert = root_cert
        self.ca_key = ca_key
        self.ca_cert = ca_cert
        self.root_pem = root_pem
        self.ca_pem = ca_pem
        self.root_source = root_source
        self.ca_source = ca_source
        self.root_files = root_files or {}
        self.ca_files = ca_files or {}

    # -- construction ------------------------------------------------------
    @classmethod
    def bootstrap(cls, directory: str | None = None) -> "Pki":
        directory = directory or settings.pki_dir
        base = Path(directory)
        base.mkdir(parents=True, exist_ok=True)

        # Fail before touching disk: a supplied CA with no ROOT would make us
        # generate a ROOT first (leaving stray files behind) and only then
        # discover it cannot vouch for that CA.
        if (
            _locate(settings.pki_ca_cert_file, base,
                    ("ca.crt", "ca.pem", "ca.cert.pem"), "ca.crt")
            is not None
            and _locate(settings.pki_root_cert_file, base,
                        ("root.crt", "root.pem", "root.cert.pem"), "root.crt")
            is None
        ):
            raise PkiError(
                f"a CA certificate was found in {base} but no ROOT certificate; "
                f"clients need both halves of the chain. Add root.crt (or set "
                f"PKI_ROOT_CERT_FILE) — no files were written"
            )

        root = cls._load_or_create(
            base,
            stem="root",
            key_file=settings.pki_root_key_file,
            cert_file=settings.pki_root_cert_file,
            cn=settings.pki_root_cn,
            days=settings.pki_root_days,
            path_length=1,
            issuer=None,
        )
        ca = cls._load_or_create(
            base,
            stem="ca",
            key_file=settings.pki_ca_key_file,
            cert_file=settings.pki_ca_cert_file,
            cn=settings.pki_ca_cn,
            days=settings.pki_ca_days,
            path_length=0,
            issuer=root,
        )

        # The intermediate is the only thing that ever signs a leaf, so it must
        # be usable; a cert-only CA (key kept in an HSM elsewhere) cannot issue.
        cls._assert_usable_ca(ca)

        # Both supplied -> they must actually be a pair, or we would be handing
        # out leaves that no client can chain to the ROOT it trusts.
        if root.cert is not None and ca.cert is not None:
            if ca.source == "provided" and root.source == "generated":
                # We just minted a ROOT, so of course it did not sign their CA.
                # Say that plainly instead of "not issued by the supplied ROOT",
                # which reads as if the ROOT were the problem.
                raise PkiError(
                    f"a CA was supplied ({ca.files.get('cert')}) but no ROOT "
                    f"certificate was found, so a throwaway ROOT was generated "
                    f"that cannot vouch for it. Add your root.crt to "
                    f"{base} (or set PKI_ROOT_CERT_FILE); nothing was overwritten"
                )
            cls._assert_chained(root.cert, ca.cert)

        root_pem = cls._pem(root.cert)
        ca_pem = cls._pem(ca.cert)
        # Derived file: rewritten whenever the inputs change, never authoritative.
        chain_path = base / "ca_chain.crt"
        chain_text = ca_pem.strip() + "\n" + root_pem.strip() + "\n"
        if not chain_path.exists() or chain_path.read_text(encoding="utf-8") != chain_text:
            chain_path.write_text(chain_text, encoding="utf-8")

        return cls(
            root_key=root.key, root_cert=root.cert,
            ca_key=ca.key, ca_cert=ca.cert,
            root_pem=root_pem, ca_pem=ca_pem,
            root_source=root.source, ca_source=ca.source,
            root_files=root.files, ca_files=ca.files,
        )

    @staticmethod
    def _pem(cert: x509.Certificate) -> str:
        return cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")

    @staticmethod
    def _assert_usable_ca(ca: "CaMaterial") -> None:
        if ca.key is None:
            raise PkiError(
                f"intermediate CA {ca.files.get('cert', '?')} has no usable private "
                f"key ({ca.files.get('key', 'ca.key')} missing or unreadable); "
                f"the CA key is required to issue and renew device certificates"
            )
        cert = ca.cert
        if cert is None:  # pragma: no cover - _load_or_create always yields one
            raise PkiError("intermediate CA certificate is missing")
        name = cert.subject.rfc4514_string()
        try:
            bc = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
        except Exception:
            raise PkiError(
                f"intermediate CA {name} has no BasicConstraints extension; an "
                f"end-entity certificate cannot sign device certificates"
            )
        if not bc.ca:
            raise PkiError(
                f"intermediate CA {name} is not a CA (BasicConstraints CA=FALSE)"
            )
        try:
            ku = cert.extensions.get_extension_for_class(x509.KeyUsage).value
        except Exception:
            raise PkiError(
                f"intermediate CA {name} has no KeyUsage extension; refusing to "
                f"assume keyCertSign"
            )
        if not ku.key_cert_sign:
            raise PkiError(
                f"intermediate CA {name} lacks keyCertSign and cannot issue "
                f"certificates"
            )
        not_after = cert_not_after(cert)
        if not_after < now():
            raise PkiError(
                f"intermediate CA expired at {not_after.isoformat()}; renew or "
                f"replace ca.crt before starting the service"
            )

    @staticmethod
    def _assert_chained(root: x509.Certificate, ca: x509.Certificate) -> None:
        """Real signature check that ``ca`` was issued by ``root``."""
        if ca.issuer != root.subject:
            _fail_unchained(root, ca, "issuer name does not match the ROOT subject")
        pub = root.public_key()
        try:
            if isinstance(pub, ed25519.Ed25519PublicKey):
                pub.verify(ca.signature, ca.tbs_certificate_bytes)
            elif isinstance(pub, ec.EllipticCurvePublicKey):
                pub.verify(
                    ca.signature, ca.tbs_certificate_bytes,
                    ec.ECDSA(ca.signature_hash_algorithm),
                )
            elif isinstance(pub, rsa.RSAPublicKey):
                pub.verify(
                    ca.signature, ca.tbs_certificate_bytes,
                    padding.PKCS1v15(), ca.signature_hash_algorithm,
                )
            else:
                _fail_unchained(root, ca, "unsupported ROOT key type")
        except Exception:
            _fail_unchained(root, ca, "signature does not verify against the ROOT key")

    @classmethod
    def _load_or_create(cls, base: Path, *, stem: str, key_file: str, cert_file: str,
                        cn: str, days: int, path_length: int,
                        issuer: "CaMaterial | None") -> "CaMaterial":
        """Use the operator's material when present; only ever fill empty slots.

        Ordering matters: a certificate on disk wins unconditionally. The old
        implementation regenerated whenever key+cert did not pair up, which
        silently replaced an offline ROOT (cert mounted, key deliberately
        withheld) with a brand new self-signed one — invalidating every client
        that had the real ROOT pinned.
        """
        cert_path = _locate(
            cert_file, base, (f"{stem}.crt", f"{stem}.pem", f"{stem}.cert.pem"),
            f"{stem}.crt",
        )
        key_path = _locate(
            key_file, base, (f"{stem}.key", f"{stem}.key.pem"), f"{stem}.key"
        )

        cert = _load_cert(cert_path) if cert_path else None
        key = _load_key(key_path) if key_path else None
        # Combined PEM: one file carrying both halves.
        if cert is None and key_path is not None:
            certs, _ = _read_material(key_path)
            if certs:
                cert = certs[0]
        if key is None and cert_path is not None:
            _, keys = _read_material(cert_path)
            if keys:
                key = keys[0]

        if cert is not None:
            if key is not None and not _key_matches_cert(key, cert):
                # Never "fix" this by regenerating — that is how real key
                # material gets destroyed. Refuse and let the operator look.
                raise PkiError(
                    f"{stem}: the private key at {key_path} does not match the "
                    f"certificate at {cert_path}; both are left untouched. Point "
                    f"PKI_{stem.upper()}_KEY_FILE / PKI_{stem.upper()}_CERT_FILE at "
                    f"a matching pair"
                )
            origin = _read_origin(base)
            mine = origin.get(f"{stem}_serial") == format(cert.serial_number, "x")
            files = {
                "cert": str(cert_path) if cert_path else str(key_path),
                "key": str(key_path) if key_path else "",
            }
            return CaMaterial(
                key=key, cert=cert,
                source="generated" if mine else "provided",
                files=files,
            )

        if not settings.pki_auto_create:
            raise PkiError(
                f"{stem}: no certificate found in {base} and PKI_AUTO_CREATE=0. "
                f"Supply {stem}.crt (and {stem}.key for the CA) or point "
                f"PKI_{stem.upper()}_CERT_FILE at your file"
            )
        if issuer is not None and issuer.key is None:
            raise PkiError(
                f"{stem}: the ROOT certificate was supplied without its private "
                f"key, so a new intermediate cannot be signed. Supply ca.key + "
                f"ca.crt yourself (PKI_CA_KEY_FILE / PKI_CA_CERT_FILE), or mount "
                f"root.key temporarily for a one-time CA mint"
            )

        key = key or _new_key(settings.pki_key_type)
        crt_target = Path(cert_file) if cert_file else base / f"{stem}.crt"
        key_target = Path(key_file) if key_file else base / f"{stem}.key"
        if crt_target.exists() or key_target.exists():  # pragma: no cover - race
            raise PkiError(f"{stem}: refusing to generate over existing files")

        subject = _subject(cn)
        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer.cert.subject if issuer else subject)
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
                    issuer.key.public_key()
                ),
                critical=False,
            )
        cert = _sign(builder, issuer.key if issuer else key)

        _write_key(key_target, key)
        _write_cert(crt_target, cert)
        _write_origin(base, stem, cert.serial_number)
        return CaMaterial(
            key=key, cert=cert, source="generated",
            files={"cert": str(crt_target), "key": str(key_target)},
        )

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
                "source": self.root_source,
                "files": self.root_files,
                "has_private_key": self.root_key is not None,
            },
            "ca": {
                "subject": self.ca_cert.subject.rfc4514_string(),
                "not_after": cert_not_after(self.ca_cert).isoformat(),
                "serial": format(self.ca_cert.serial_number, "x"),
                "source": self.ca_source,
                "files": self.ca_files,
                "has_private_key": self.ca_key is not None,
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
