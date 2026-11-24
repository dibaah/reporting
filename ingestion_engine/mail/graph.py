"""Microsoft Graph source (read-only).

Needs an app registration made by IT with the APPLICATION permission Mail.Read (ideally
restricted to this one mailbox with an Exchange application access policy). The importer never
changes the mailbox: "already processed" is tracked in our own database (file hash and mail id).
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterator
from urllib.parse import quote

import httpx

from .base import MailAttachment, MailConfigError, MailMessage, MailSource, MailSourceError

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"


class GraphMailSource(MailSource):
    def __init__(self, tenant_id: str, client_id: str, client_secret: str, mailbox: str, folder: str, *,
                 client: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep,
                 max_retries: int = 5):
        missing = [n for n, v in (("GRAPH_TENANT_ID", tenant_id), ("GRAPH_CLIENT_ID", client_id),
                                  ("GRAPH_CLIENT_SECRET", client_secret), ("GRAPH_MAILBOX", mailbox)) if not v]
        if missing:
            raise MailConfigError("Microsoft Graph settings missing: " + ", ".join(missing))
        if not folder:
            raise MailConfigError("Outlook folder is not set (app setting outlook_folder, for example 'Inbox/Reports')")
        self.tenant, self.client_id, self.secret = tenant_id, client_id, client_secret
        self.mailbox, self.folder = mailbox, folder
        self._http = client or httpx.Client(timeout=60)
        self._sleep, self._max_retries = sleep, max_retries
        self._token: str | None = None
        self._token_expires = 0.0
        self._folder_id: str | None = None

    def describe(self) -> str:
        return f"Microsoft Graph mailbox {self.mailbox}, folder {self.folder}"

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------ http plumbing
    def _get_token(self, force: bool = False) -> str:
        if self._token and not force and time.time() < self._token_expires:
            return self._token
        try:
            r = self._http.post(
                f"{LOGIN}/{self.tenant}/oauth2/v2.0/token",
                data={"grant_type": "client_credentials", "client_id": self.client_id,
                      "client_secret": self.secret, "scope": "https://graph.microsoft.com/.default"},
            )
        except httpx.TransportError as e:
            raise MailSourceError(f"cannot reach Microsoft login: {e}")
        if r.status_code in (400, 401):
            raise MailConfigError(f"Microsoft login refused the app credentials ({r.status_code}): {r.text[:200]}")
        if r.status_code >= 400:
            raise MailSourceError(f"Microsoft login error {r.status_code}")
        body = r.json()
        self._token = body["access_token"]
        self._token_expires = time.time() + int(body.get("expires_in", 3600)) - 60
        return self._token

    def _request(self, url: str, params: dict | None = None) -> httpx.Response:
        refreshed = False
        for attempt in range(self._max_retries + 1):
            try:
                r = self._http.get(url, params=params, headers={"Authorization": f"Bearer {self._get_token()}"})
            except httpx.TransportError as e:
                if attempt >= self._max_retries:
                    raise MailSourceError(f"network error talking to Graph: {e}")
                self._sleep(min(60, 2 ** attempt))
                continue
            if r.status_code == 401 and not refreshed:
                self._get_token(force=True)
                refreshed = True
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                if attempt >= self._max_retries:
                    raise MailSourceError(f"Graph still failing after retries ({r.status_code})")
                try:
                    wait = float(r.headers.get("Retry-After", ""))
                except ValueError:
                    wait = min(60, 2 ** attempt)
                self._sleep(min(wait, 120))
                continue
            if r.status_code == 403:
                raise MailConfigError("Graph says access denied: check the Mail.Read application permission, "
                                      "admin consent and any mailbox access policy")
            if r.status_code == 404:
                raise MailConfigError(f"Graph: not found ({url.split('?')[0]}). Check GRAPH_MAILBOX and the folder name")
            if r.status_code >= 400:
                raise MailSourceError(f"Graph error {r.status_code}: {r.text[:200]}")
            return r
        raise MailSourceError("Graph request failed")  # pragma: no cover

    def _paged(self, url: str, params: dict | None = None) -> Iterator[dict[str, Any]]:
        while url:
            body = self._request(url, params).json()
            yield from body.get("value", [])
            url, params = body.get("@odata.nextLink"), None  # the next link already contains the query

    # ------------------------------------------------------------ folder lookup
    def _user(self) -> str:
        return f"{GRAPH}/users/{quote(self.mailbox)}"

    def _children(self, folder_id: str | None) -> list[dict]:
        url = f"{self._user()}/mailFolders" if folder_id is None else f"{self._user()}/mailFolders/{folder_id}/childFolders"
        return list(self._paged(url, {"$top": 100}))

    def _resolve_folder(self) -> str:
        if self._folder_id:
            return self._folder_id
        parts = [p for p in re.split(r"[\\/]", self.folder) if p.strip()]
        if len(parts) > 1:  # exact path, for example Inbox/Reports
            current: str | None = None
            for part in parts:
                match = next((f for f in self._children(current) if f["displayName"].lower() == part.strip().lower()), None)
                if match is None:
                    raise MailConfigError(f"Outlook folder not found: {self.folder} (no folder named {part!r})")
                current = match["id"]
            self._folder_id = current
            return current  # type: ignore[return-value]
        name = parts[0].strip().lower() if parts else ""
        queue: list[tuple[str | None, int]] = [(None, 0)]  # a single name: search the tree, 4 levels deep
        while queue:
            parent, depth = queue.pop(0)
            for f in self._children(parent):
                if f["displayName"].lower() == name:
                    self._folder_id = f["id"]
                    return f["id"]
                if depth < 3 and f.get("childFolderCount", 0):
                    queue.append((f["id"], depth + 1))
        raise MailConfigError(f"Outlook folder not found: {self.folder}")

    # ------------------------------------------------------------ messages
    def messages(self, since: datetime | None) -> Iterator[MailMessage]:
        folder_id = self._resolve_folder()
        params = {"$select": "id,subject,receivedDateTime,from,hasAttachments", "$orderby": "receivedDateTime asc", "$top": 50}
        if since is not None:
            params["$filter"] = "receivedDateTime ge " + since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for m in self._paged(f"{self._user()}/mailFolders/{folder_id}/messages", params):
            if not m.get("hasAttachments"):
                continue
            received = datetime.fromisoformat(m["receivedDateTime"].replace("Z", "+00:00")).astimezone(timezone.utc)
            sender = ((m.get("from") or {}).get("emailAddress") or {}).get("address", "")
            atts = []
            for a in self._paged(f"{self._user()}/messages/{m['id']}/attachments",
                                 {"$select": "id,name,size,isInline"}):
                if a.get("@odata.type", "#microsoft.graph.fileAttachment") != "#microsoft.graph.fileAttachment" or a.get("isInline"):
                    continue
                url = f"{self._user()}/messages/{m['id']}/attachments/{a['id']}/$value"
                atts.append(MailAttachment(a["name"], a.get("size"), lambda u=url: self._request(u).content))
            yield MailMessage(m["id"], m.get("subject") or "", received, sender, atts)
