"""Read and write app_settings (the same table the Settings screen will edit)."""
from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from database.db import session_scope
from database.models import AppSetting


def get_setting(key: str, default: str = "", session: Session | None = None) -> str:
    """Value of a setting; `default` when missing or empty. Pass `session` to read inside a transaction."""
    if session is not None:
        row = session.get(AppSetting, key)
        return row.value if row is not None and row.value != "" else default
    with session_scope() as s:
        return get_setting(key, default, s)


def set_setting(key: str, value: str, session: Session | None = None) -> None:
    if session is not None:
        stmt = insert(AppSetting).values(key=key, value=value)
        session.execute(stmt.on_conflict_do_update(index_elements=["key"], set_={"value": value}))
        session.expire_all()
        return
    with session_scope() as s:
        set_setting(key, value, s)
