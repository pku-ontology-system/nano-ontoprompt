"""Owner-scoped access checks for saved external connections."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.v2.connection import Connection


def require_connection_access(
    db: Session,
    connection_or_id: Connection | str,
    user: User,
    *,
    write: bool = False,
) -> Connection:
    if write and user.role not in {"admin", "editor"}:
        raise HTTPException(403, "Editor role required")
    connection = connection_or_id if isinstance(connection_or_id, Connection) else db.get(Connection, str(connection_or_id))
    if connection is None:
        raise HTTPException(404, "Connection not found or inaccessible")
    if user.role == "admin" or str(connection.created_by or "") == str(user.id):
        return connection
    raise HTTPException(404, "Connection not found or inaccessible")
