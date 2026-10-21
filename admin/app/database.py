"""PostgreSQL connection via a SQLAlchemy synchronous engine + QueuePool.

The synchronous engine is paired with a thread pool (QueuePool is the default
for non-SQLite backends). FastAPI runs synchronous `def` route handlers in a
threadpool, so every request borrows a connection from the pool without blocking
the event loop.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    sessionmaker,
    Session,
)
from contextlib import contextmanager

from .config import settings


# QueuePool is the default for PostgreSQL/psycopg2; set it explicitly so the
# "thread pool connection" requirement is unambiguous.
from sqlalchemy.pool import QueuePool  # noqa: E402

# The PostgreSQL-specific connect option is only valid for that dialect; for a
# SQLite (dev/test) URL we instead relax the single-thread check so the sync
# handlers running in FastAPI's threadpool can share the connection.
_connect_args: dict = {}
if settings.database_url.startswith("postgresql"):
    _connect_args["options"] = "-c timezone=utc"
elif settings.database_url.startswith("sqlite"):
    _connect_args["check_same_thread"] = False

engine = create_engine(
    settings.database_url,
    poolclass=QueuePool,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    pool_recycle=1800,
    connect_args=_connect_args,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


@contextmanager
def manual_session() -> Session:
    """Manual (non-dependency) session: used by startup/bootstrap and health."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db() -> Session:
    """FastAPI dependency: yields a session, commits on success, rolls back on error.

    Defined as a generator (``yield``) so FastAPI can use it directly as a
    dependency; ``manual_session`` is the equivalent context manager for code
    that is not inside a request.
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
