from __future__ import annotations

import os

from ingestion_engine.settings import get_setting

from .base import MailConfigError, MailSource


def make_source(kind: str | None = None, folder: str | None = None, eml_folder: str | None = None) -> MailSource:
    """Build the mail source chosen by MAIL_SOURCE (graph | outlook | eml) and the outlook_folder setting."""
    kind = (kind or os.environ.get("MAIL_SOURCE") or "graph").strip().lower()
    folder = folder or get_setting("outlook_folder", "")
    if kind == "eml":
        from .eml import EmlFolderSource

        return EmlFolderSource(eml_folder or os.environ.get("EML_FOLDER", ""))
    if kind == "outlook":
        from .outlook_com import OutlookComSource

        return OutlookComSource(folder)
    if kind == "graph":
        from .graph import GraphMailSource

        e = os.environ.get
        return GraphMailSource(e("GRAPH_TENANT_ID", ""), e("GRAPH_CLIENT_ID", ""), e("GRAPH_CLIENT_SECRET", ""),
                               e("GRAPH_MAILBOX", ""), folder)
    raise MailConfigError(f"unknown MAIL_SOURCE {kind!r} (use graph, outlook or eml)")
