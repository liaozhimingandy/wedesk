"""ORM models.

Tables reused from the wedesk hbbs schema (same database):
  * ``peer``            - device registration / sysinfo (read + write ``info``)
  * ``connection_log``  - audit events

Tables owned by the admin service (prefixed ``admin_``).
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    String,
    Boolean,
    SmallInteger,
    DateTime,
    Text,
    BigInteger,
    Integer,
    JSON,
    ForeignKey,
    LargeBinary,
    func,
    UniqueConstraint,
)

from .database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Reused hbbs tables
# ---------------------------------------------------------------------------
class Peer(Base):
    __tablename__ = "peer"

    guid = Column(LargeBinary, primary_key=True)
    id = Column(String(100), unique=True, nullable=False, index=True)
    uuid = Column(LargeBinary, nullable=False)
    pk = Column(LargeBinary, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    user = Column(LargeBinary)
    status = Column(SmallInteger)
    note = Column(String(300))
    info = Column(Text, nullable=False, server_default="")


class ConnectionLog(Base):
    __tablename__ = "connection_log"
    __table_args__ = {"sqlite_autoincrement": True}

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    ts = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    event = Column(String(32), nullable=False)
    from_id = Column(String(253))
    from_ip = Column(String(64))
    to_id = Column(String(253))
    to_ip = Column(String(64))
    via_relay = Column(Boolean)
    detail = Column(Text)


# ---------------------------------------------------------------------------
# Admin tables
# ---------------------------------------------------------------------------
class AdminUser(Base):
    __tablename__ = "admin_users"

    guid = Column(String(36), primary_key=True, default=_uuid)
    name = Column(String(100), unique=True, nullable=False, index=True)
    email = Column(String(255), default="")
    note = Column(String(300), default="")
    password_hash = Column(String(255), default="")
    status = Column(SmallInteger, default=1)  # 1 normal, 0 disabled
    is_admin = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AdminSession(Base):
    __tablename__ = "admin_sessions"

    token = Column(String(64), primary_key=True)
    user_guid = Column(String(36), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=True)


class AdminDeviceGroup(Base):
    __tablename__ = "admin_device_groups"

    id = Column(String(36), primary_key=True, default=_uuid)
    name = Column(String(100), unique=True, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AdminUserGroup(Base):
    __tablename__ = "admin_user_groups"
    __table_args__ = (UniqueConstraint("user_guid", "group_id", name="uq_user_group"),)

    user_guid = Column(
        String(36), ForeignKey("admin_users.guid"), primary_key=True
    )
    group_id = Column(
        String(36), ForeignKey("admin_device_groups.id"), primary_key=True
    )


class AdminAb(Base):
    __tablename__ = "admin_abs"

    guid = Column(String(36), primary_key=True, default=_uuid)
    name = Column(String(255), nullable=False)
    owner_guid = Column(String(36), nullable=False)
    is_shared = Column(Boolean, default=False)
    sharer = Column(String(255), default="")
    note = Column(String(300), default="")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AdminAbPeer(Base):
    __tablename__ = "admin_ab_peers"
    __table_args__ = (UniqueConstraint("ab_guid", "peer_id", name="uq_ab_peer"),)

    ab_guid = Column(String(36), ForeignKey("admin_abs.guid"), primary_key=True)
    peer_id = Column(String(100), primary_key=True)
    hostname = Column(String(255), default="")
    alias = Column(String(255), default="")
    tags = Column(JSON, default=lambda: [])
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AdminAbTag(Base):
    __tablename__ = "admin_ab_tags"
    __table_args__ = (UniqueConstraint("ab_guid", "name", name="uq_ab_tag"),)

    ab_guid = Column(String(36), ForeignKey("admin_abs.guid"), primary_key=True)
    name = Column(String(255), primary_key=True)
    color = Column(String(32), default="")


class AdminAbRule(Base):
    __tablename__ = "admin_ab_rules"

    guid = Column(String(36), primary_key=True, default=_uuid)
    ab_guid = Column(String(36), nullable=False, index=True)
    user_guid = Column(String(36), nullable=False)
    rule = Column(SmallInteger, default=1)  # 1 read, 2 readWrite, 3 fullControl
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AdminPeerStatus(Base):
    __tablename__ = "admin_peer_status"

    id = Column(String(100), primary_key=True)  # peer id
    last_heartbeat = Column(DateTime(timezone=True), nullable=True)
    conns = Column(Integer, default=0)
    sysinfo_version = Column(String(64), default="")
    modified_at = Column(BigInteger, default=0)
    strategy = Column(JSON, default=lambda: {})


class AdminPeerGroup(Base):
    __tablename__ = "admin_peer_groups"

    peer_id = Column(String(100), primary_key=True)
    group_name = Column(String(100), default="")


class AdminStrategy(Base):
    __tablename__ = "admin_strategies"

    id = Column(String(36), primary_key=True, default=_uuid)
    name = Column(String(255), nullable=False)
    config_options = Column(JSON, default=lambda: {})
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AdminSwitchGrant(Base):
    __tablename__ = "admin_switch_grants"

    switch_code_verifier = Column(String(255), primary_key=True)
    device_id = Column(String(100), default="")
    granted = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AdminDeployToken(Base):
    __tablename__ = "admin_deploy_tokens"

    token = Column(String(64), primary_key=True)
    owner_guid = Column(String(36), nullable=False)
    presets = Column(JSON, default=lambda: {})
    used = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AdminOidcState(Base):
    __tablename__ = "admin_oidc_states"

    state = Column(String(64), primary_key=True)
    nonce = Column(String(64), default="")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_aware(dt: datetime | None) -> datetime | None:
    """Normalise a possibly-naive datetime to timezone-aware (UTC).

    SQLite discards timezone metadata on read, so values written as aware UTC
    come back naive. Comparing naive vs aware raises TypeError; this helper makes
    comparisons safe across both backends.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
