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
