"""One interface for every way of reading the Outlook folder that receives the reports."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterator


class MailSourceError(Exception):
    """Temporary problem (network, Outlook not reachable, throttling). The next cycle retries."""


class MailConfigError(Exception):
    """Permanent problem that needs a human: missing settings, folder not found, no permission."""


@dataclass
class MailAttachment:
    name: str
    size: int | None
    _fetch: Callable[[], bytes] = field(repr=False)  # downloaded only when needed

    def read(self) -> bytes:
        return self._fetch()


@dataclass
class MailMessage:
    id: str  # stable id of the mail in its source (Graph id, Outlook EntryID, Message-ID)
    subject: str
    received_at: datetime  # timezone-aware
    sender: str
    attachments: list[MailAttachment]


class MailSource(ABC):
    @abstractmethod
    def messages(self, since: datetime | None) -> Iterator[MailMessage]:
        """Messages received at or after `since` (None = all), OLDEST FIRST."""

    def close(self) -> None:  # pragma: no cover - nothing to release by default
        pass

    def describe(self) -> str:
        return type(self).__name__
