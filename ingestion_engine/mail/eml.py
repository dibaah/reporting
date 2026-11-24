"""Folder of .eml files: for tests, for exports made by IT, and as a fallback."""
from __future__ import annotations

from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterator

from .base import MailAttachment, MailConfigError, MailMessage, MailSource


class EmlFolderSource(MailSource):
    def __init__(self, folder: str | Path, recursive: bool = False):
        if not str(folder or "").strip():
            raise MailConfigError("EML folder is not set (EML_FOLDER in .env, or --eml-folder)")
        self.folder = Path(folder)
        self.recursive = recursive
        if not self.folder.is_dir():
            raise MailConfigError(f"EML folder not found: {self.folder}")

    def describe(self) -> str:
        return f"eml folder {self.folder}"

    def messages(self, since: datetime | None) -> Iterator[MailMessage]:
        pattern = "**/*.eml" if self.recursive else "*.eml"
        found: list[MailMessage] = []
        for path in sorted(self.folder.glob(pattern)):
            try:
                with open(path, "rb") as f:
                    msg: EmailMessage = BytesParser(policy=policy.default).parse(f)  # type: ignore[assignment]
            except OSError:
                continue
            try:
                received = parsedate_to_datetime(str(msg["Date"]))
                if received.tzinfo is None:
                    received = received.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                received = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            received = received.astimezone(timezone.utc)
            if since is not None and received < since:
                continue
            atts = []
            for part in msg.iter_attachments():
                name = part.get_filename()
                if not name:
                    continue
                payload = part.get_payload(decode=True) or b""
                atts.append(MailAttachment(name, len(payload), lambda b=payload: b))
            mid = (msg["Message-ID"] or "").strip() or f"file:{path.name}"
            found.append(MailMessage(mid, str(msg["Subject"] or ""), received, str(msg["From"] or ""), atts))
        found.sort(key=lambda m: m.received_at)
        yield from found
