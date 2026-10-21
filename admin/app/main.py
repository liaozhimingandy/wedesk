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
from . import auth
from .routers import account, device, audit, addressbook, users, peers, devices


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


@app.get("/health")
def health():
    try:
        with manual_session() as db:
            db.execute(text("select 1"))
        return {"status": "ok", "db": "up"}
    except Exception as e:  # pragma: no cover
        return {"status": "degraded", "db": "down", "error": str(e)}


@app.get("/")
def root():
    return {"service": "wedesk admin API", "version": "1.0.0"}
