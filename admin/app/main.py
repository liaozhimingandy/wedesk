"""FastAPI entrypoint for the wedesk admin API service.

Run with:  uvicorn app.main:app --host 0.0.0.0 --port 5185

The service shares the *same* PostgreSQL database as the wedesk hbbs process:
it reuses the ``peer`` and ``connection_log`` tables (recreating them idempotently
on first start) and adds its own ``admin_*`` tables.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from .config import settings
from .database import engine, Base, get_db, manual_session
from . import models  # noqa: F401  (register all ORM models on Base.metadata)
from . import auth, pki
from .routers import account, device, audit, addressbook, users, peers, devices
from .routers import cert, mtls_api


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure all tables exist (peer/connection_log are reused from hbbs and are
    # created idempotently here so the admin service can run standalone).
    Base.metadata.create_all(bind=engine)

    # Bootstrap the first admin account if no users exist yet.
    with manual_session() as db:
        count = db.query(models.AdminUser).count()
        if count == 0 and settings.default_admin_name:
            db.add(
                models.AdminUser(
                    name=settings.default_admin_name,
                    email=settings.default_admin_email,
                    password_hash=auth.hash_password(settings.default_admin_password),
                    status=1,
                    is_admin=True,
                )
            )
            db.commit()

    import os

    os.makedirs(settings.record_dir, exist_ok=True)

    # PKI (ROOT -> CA -> leaf). Material already in PKI_DIR is used as-is and
    # never overwritten; only empty slots are filled, and only when
    # PKI_AUTO_CREATE is on. A misconfiguration here means every device
    # certificate would be issued under the wrong anchor, so fail loudly.
    try:
        ca = pki.get()
        app.state.pki_ready = True
        app.state.pki_info = ca.info()
        if ca.root_source == "generated" or ca.ca_source == "generated":
            print(
                "[pki] WARNING: using generated CA material "
                f"(root={ca.root_source}, ca={ca.ca_source}). "
                "Supply your own ROOT/CA in PKI_DIR for production."
            )
    except Exception as exc:  # pragma: no cover - misconfigured PKI_DIR
        app.state.pki_ready = False
        app.state.pki_error = str(exc)
        print(f"[pki] CA bootstrap FAILED: {exc}", flush=True)
        print(
            "[pki] certificate issuance and mTLS device APIs are disabled until "
            "PKI_DIR holds a usable ROOT/CA (or PKI_ROOT_CERT_FILE / "
            "PKI_CA_CERT_FILE point at yours).",
            flush=True,
        )

    yield


app = FastAPI(title="wedesk admin API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(account.router)
app.include_router(device.router)
app.include_router(audit.router)
app.include_router(addressbook.router)
app.include_router(users.router)
app.include_router(peers.router)
app.include_router(devices.router)
app.include_router(cert.router)
app.include_router(mtls_api.router)


@app.get("/health")
def health():
    try:
        with manual_session() as db:
            db.execute(text("select 1"))
        db_state = "up"
    except Exception as e:  # pragma: no cover
        db_state = f"down: {e}"
    return {
        "status": "ok" if db_state == "up" else "degraded",
        "db": db_state,
        "pki": getattr(app.state, "pki_ready", False),
        "pki_error": getattr(app.state, "pki_error", ""),
    }


@app.get("/")
def root():
    return {"service": "wedesk admin API", "version": "1.0.0"}
