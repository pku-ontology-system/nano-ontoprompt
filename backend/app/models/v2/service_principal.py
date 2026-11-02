"""Registered non-human principals and exact resource grants."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ServicePrincipal(Base):
    """A named service/system identity; this record is not an API credential."""

    __tablename__ = "v2_service_principals"

    id: Mapped[str] = mapped_column(String(100), primary_key=True, default=lambda: f"sp:{uuid.uuid4()}")
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="service")
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    owner_user_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        CheckConstraint("kind IN ('service','system')", name="ck_v2_service_principal_kind"),
        CheckConstraint("status IN ('active','suspended')", name="ck_v2_service_principal_status"),
        CheckConstraint("revision >= 1", name="ck_v2_service_principal_revision"),
        Index("ix_v2_service_principals_status_kind", "status", "kind"),
    )


class ServicePrincipalGrant(Base):
    """An exact, revocable capability grant to a registered principal."""

    __tablename__ = "v2_service_principal_grants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    service_principal_id: Mapped[str] = mapped_column(
        String(100), ForeignKey("v2_service_principals.id", ondelete="RESTRICT"), nullable=False
    )
    resource_type: Mapped[str] = mapped_column(String(40), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(200), nullable=False)
    permission: Mapped[str] = mapped_column(String(40), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("resource_type <> ''", name="ck_v2_service_principal_grant_resource_type"),
        CheckConstraint("resource_id <> ''", name="ck_v2_service_principal_grant_resource_id"),
        CheckConstraint("permission <> ''", name="ck_v2_service_principal_grant_permission"),
        Index(
            "ix_v2_service_principal_grants_lookup",
            "service_principal_id", "resource_type", "resource_id", "permission", "active",
        ),
    )


class ServicePrincipalAuditEvent(Base):
    """Append-only registry and grant changes, linked to the acting human."""

    __tablename__ = "v2_service_principal_audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    service_principal_id: Mapped[str] = mapped_column(
        String(100), ForeignKey("v2_service_principals.id", ondelete="RESTRICT"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    grant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("v2_service_principal_grants.id", ondelete="RESTRICT"), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    previous_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)

    __table_args__ = (
        UniqueConstraint("service_principal_id", "sequence", name="uq_v2_service_principal_event_sequence"),
        Index("ix_v2_service_principal_events_principal", "service_principal_id", "created_at"),
    )
