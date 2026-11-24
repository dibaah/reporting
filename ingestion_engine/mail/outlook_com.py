"""Desktop Outlook source (Windows, pywin32). Read-only.

Outlook COM does NOT work reliably from a Windows service. Run the live importer as a scheduled
task "when I log on" in your own session, with Outlook installed and signed in.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from typing import Any, Iterator

from .base import MailAttachment, MailConfigError, MailMessage, MailSource, MailSourceError


def _items(collection: Any) -> Iterator[Any]:
    for i in range(1, int(collection.Count) + 1):  # COM collections are 1-based
        yield collection.Item(i)


class OutlookComSource(MailSource):
    def __init__(self, folder: str, *, application: Any = None):
        if not folder:
            raise MailConfigError("Outlook folder is not set (app setting outlook_folder, for example 'Inbox/Reports')")
        self.folder_path = folder
        self._app = application

    def describe(self) -> str:
        return f"desktop Outlook, folder {self.folder_path}"

    # ------------------------------------------------------------ connection and folder lookup
    def _namespace(self) -> Any:
        try:
            if self._app is None:
                import win32com.client  # type: ignore[import-not-found]  # only exists on Windows with pywin32

                self._app = win32com.client.Dispatch("Outlook.Application")
            return self._app.GetNamespace("MAPI")
        except ImportError:
            raise MailConfigError("pywin32 is not installed (pip install pywin32) or this is not Windows")
        except Exception as e:
            raise MailSourceError(f"cannot connect to Outlook (is it installed and signed in?): {e}")

    def _find_folder(self, ns: Any) -> Any:
        parts = [p.strip() for p in re.split(r"[\\/]", self.folder_path) if p.strip()]
        roots = list(_items(ns.Folders))

        def walk(folder: Any, rest: list[str]) -> Any | None:
            if not rest:
                return folder
            for child in _items(folder.Folders):
                if child.Name.lower() == rest[0].lower():
                    hit = walk(child, rest[1:])
                    if hit is not None:
                        return hit
            return None

        def search(folder: Any, name: str, depth: int) -> Any | None:
            for child in _items(folder.Folders):
                if child.Name.lower() == name.lower():
                    return child
                if depth < 3:
                    hit = search(child, name, depth + 1)
                    if hit is not None:
                        return hit
            return None

        for root in roots:
            hit = walk(root, parts) if len(parts) > 1 else search(root, parts[0], 0)
            if hit is None and len(parts) > 1 and root.Name.lower() == parts[0].lower():
                hit = walk(root, parts[1:])  # path starts with the mailbox name
            if hit is not None:
                return hit
        raise MailConfigError(f"Outlook folder not found: {self.folder_path}")

    # ------------------------------------------------------------ messages
    @staticmethod
    def _utc(dt: datetime) -> datetime:
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.astimezone().astimezone(timezone.utc)  # naive = local time

    def messages(self, since: datetime | None) -> Iterator[MailMessage]:
        folder = self._find_folder(self._namespace())
        try:
            items = folder.Items
            items.Sort("[ReceivedTime]", True)  # newest first, so we can stop at `since`
            collected = []
            for i in range(1, int(items.Count) + 1):
                item = items.Item(i)
                try:
                    received = self._utc(item.ReceivedTime)
                except Exception:
                    continue  # not a mail item (calendar request, report, ...)
                if since is not None and received < since:
                    break
                collected.append((received, item))
        except MailConfigError:
            raise
        except Exception as e:
            raise MailSourceError(f"error reading the Outlook folder: {e}")
        for received, item in reversed(collected):  # oldest first
            try:
                if int(item.Attachments.Count) == 0:
                    continue
                atts = []
                for att in _items(item.Attachments):
                    if getattr(att, "Type", 1) != 1:  # 1 = a real file attached to the mail
                        continue
                    atts.append(MailAttachment(str(att.FileName), getattr(att, "Size", None),
                                               lambda a=att: self._save_and_read(a)))
                yield MailMessage(str(item.EntryID), str(item.Subject or ""), received,
                                  str(getattr(item, "SenderEmailAddress", "") or ""), atts)
            except Exception as e:
                raise MailSourceError(f"error reading an Outlook message: {e}")

    @staticmethod
    def _save_and_read(att: Any) -> bytes:
        tmp = tempfile.mkdtemp(prefix="outlook_att_")
        try:
            path = os.path.join(tmp, "attachment.bin")
            att.SaveAsFile(path)
            with open(path, "rb") as f:
                return f.read()
        except Exception as e:
            raise MailSourceError(f"cannot save an Outlook attachment: {e}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
