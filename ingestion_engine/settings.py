"""Read and write app_settings (the same table the Settings screen will edit)."""
from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert

from database.db import session_scope
from database.models import AppSetting


def get_setting(key: str, default: str = "") -> str:
    with session_scope() as s:
        row = s.get(AppSetting, key)
        return row.value if row is not None and row.value != "" else default


def set_setting(key: str, value: str) -> None:
    with session_scope() as s:
        stmt = insert(AppSetting).values(key=key, value=value)
        s.execute(stmt.on_conflict_do_update(index_elements=["key"], set_={"value": value}))
