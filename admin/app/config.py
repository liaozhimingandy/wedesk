import os
from functools import lru_cache


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _normalize_db_url(url: str) -> str:
    """SQLAlchemy requires the ``postgresql`` dialect; the wedesk convention uses
    the shorter ``postgres://`` prefix (which sqlx/Rust accepts but SQLAlchemy
    does not). Normalise it to ``postgresql+psycopg2://``."""
    if url.startswith("postgres://"):
        return "postgresql+psycopg2://" + url[len("postgres://"):]
    return url


class Settings:
    """Runtime configuration, sourced from environment variables.

    The admin service intentionally shares the *same* PostgreSQL database as the
    wedesk hbbs process: it reuses the `peer` and `connection_log` tables and
    adds its own tables under the `admin_` prefix.
    """

    def __init__(self) -> None:
        self.pg_host = _env("PG_HOST", "postgres")
        self.pg_port = _env("PG_PORT", "5432")
        self.pg_user = _env("PG_USER", "rustdesk")
        self.pg_password = _env("PG_PASSWORD", "changeme")
        self.pg_db = _env("PG_DB", "rustdesk")

        url = _env("DATABASE_URL", "")
        if url:
            self.database_url = _normalize_db_url(url)
        else:
            self.database_url = _normalize_db_url(
                f"postgres://{self.pg_user}:{self.pg_password}"
                f"@{self.pg_host}:{self.pg_port}/{self.pg_db}"
            )

        self.host = _env("ADMIN_HOST", "0.0.0.0")
        self.port = int(_env("ADMIN_PORT", "5185"))

        origins = _env("CORS_ORIGINS", "*")
        if origins.strip() == "*":
            self.cors_origins = ["*"]
        else:
            self.cors_origins = [o.strip() for o in origins.split(",") if o.strip()]

        self.default_admin_name = _env("DEFAULT_ADMIN_NAME", "admin")
        self.default_admin_password = _env("DEFAULT_ADMIN_PASSWORD", "changeme")
        self.default_admin_email = _env("DEFAULT_ADMIN_EMAIL", "admin@wedesk.local")

        self.record_dir = _env("RECORD_DIR", "/data/records")

        # ------------------------------------------------------------------
        # PKI (ROOT -> CA -> device/app leaf). See app/pki.py for the model.
        # ------------------------------------------------------------------
        self.pki_dir = _env("PKI_DIR", "/data/pki")
        self.pki_root_cn = _env("PKI_ROOT_CN", "WeDesk Root CA")
        self.pki_ca_cn = _env("PKI_CA_CN", "WeDesk Device CA")
        # ed25519 | ec256 | ec384 | rsa2048 | rsa4096
        self.pki_key_type = _env("PKI_KEY_TYPE", "ec256").lower()
        self.pki_root_days = int(_env("PKI_ROOT_DAYS", "7300"))
        self.pki_ca_days = int(_env("PKI_CA_DAYS", "3650"))
        self.pki_device_cert_days = int(_env("PKI_DEVICE_CERT_DAYS", "90"))
        self.pki_app_cert_days = int(_env("PKI_APP_CERT_DAYS", "180"))
        self.pki_leaf_domain = _env("PKI_LEAF_DOMAIN", "device.wedesk.alsoapp.com")
        self.pki_leaf_eku_server_auth = _env("PKI_LEAF_EKU_SERVER_AUTH", "0") not in (
            "0",
            "",
            "false",
            "False",
        )
        self.pki_crl_url = _env("PKI_CRL_URL", "")
        self.pki_crl_next_days = int(_env("PKI_CRL_NEXT_DAYS", "7"))
        # 续期窗口：仅用于下发 renew_after 建议值，不强制。
        self.pki_renew_window_days = int(_env("PKI_RENEW_WINDOW_DAYS", "30"))

        # Proof of possession: every mTLS request must carry a signature made
        # with the leaf private key. This is what makes forwarded client-cert
        # headers safe to trust.
        self.pki_require_pop = _env("PKI_REQUIRE_POP", "1") not in (
            "0",
            "",
            "false",
            "False",
        )
        self.pki_pop_window_seconds = int(_env("PKI_POP_WINDOW_SECONDS", "300"))

        # Headers set by the TLS-terminating reverse proxy (nginx/Caddy names).
        self.mtls_header_cert = _env("PKI_MTLS_HEADER_CERT", "X-SSL-Client-Cert")
        self.mtls_header_verify = _env("PKI_MTLS_HEADER_VERIFY", "X-SSL-Client-Verify")
        self.mtls_header_serial = _env("PKI_MTLS_HEADER_SERIAL", "X-SSL-Client-Serial")
        self.mtls_header_dn = _env("PKI_MTLS_HEADER_DN", "X-SSL-Client-DN")
        self.mtls_header_fingerprint = _env(
            "PKI_MTLS_HEADER_FINGERPRINT", "X-SSL-Client-Fingerprint"
        )

        # OIDC (optional)
        self.oidc_issuer = _env("OIDC_ISSUER", "")
        self.oidc_client_id = _env("OIDC_CLIENT_ID", "")
        self.oidc_client_secret = _env("OIDC_CLIENT_SECRET", "")
        self.oidc_redirect_uri = _env("OIDC_REDIRECT_URI", "")

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_client_id)


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
