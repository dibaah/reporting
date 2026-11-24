"""Phase 5: the three mail sources (EML folder, Microsoft Graph with mocked HTTP, desktop Outlook with fakes)."""
import datetime as dt
import os
from email.message import EmailMessage
from email.utils import format_datetime

import httpx
import pytest

from ingestion_engine.mail.base import MailConfigError, MailSourceError
from ingestion_engine.mail.eml import EmlFolderSource
from ingestion_engine.mail.factory import make_source
from ingestion_engine.mail.graph import GraphMailSource
from ingestion_engine.mail.outlook_com import OutlookComSource

UTC = dt.timezone.utc


# ---------------------------------------------------------------- EML folder

def write_eml(folder, name, received, attachments, subject="Report", message_id=None):
    m = EmailMessage()
    m["Subject"], m["From"], m["To"], m["Date"] = subject, "ops@example.com", "me@example.com", format_datetime(received)
    if message_id:
        m["Message-ID"] = message_id
    m.set_content("see attached")
    for fname, data in attachments:
        m.add_attachment(data, maintype="application", subtype="octet-stream", filename=fname)
    (folder / name).write_bytes(bytes(m))


def test_eml_source_orders_filters_and_reads_attachments(tmp_path):
    write_eml(tmp_path, "b.eml", dt.datetime(2999, 1, 2, 6, tzinfo=UTC), [("b.xlsx", b"BBB")], message_id="<b@x>")
    write_eml(tmp_path, "a.eml", dt.datetime(2999, 1, 1, 6, tzinfo=UTC), [("a.xlsx", b"AAA"), ("logo.png", b"PNG")])
    src = EmlFolderSource(tmp_path)
    msgs = list(src.messages(None))
    assert [m.attachments[0].name for m in msgs] == ["a.xlsx", "b.xlsx"]            # oldest first
    assert msgs[1].id == "<b@x>" and msgs[0].id == "file:a.eml"                       # Message-ID, else file name
    assert [a.read() for a in msgs[0].attachments] == [b"AAA", b"PNG"]
    assert [m.id for m in src.messages(dt.datetime(2999, 1, 2, tzinfo=UTC))] == ["<b@x>"]
    assert all(m.received_at.tzinfo is not None for m in msgs)


def test_eml_source_config_error(tmp_path):
    with pytest.raises(MailConfigError, match="not found"):
        EmlFolderSource(tmp_path / "missing")
    for unset in ("", "   ", None):                      # Path("") means "the current folder": must be an error instead
        with pytest.raises(MailConfigError, match="not set"):
            EmlFolderSource(unset)


# ---------------------------------------------------------------- Microsoft Graph (mocked HTTP)

class GraphServer:
    """Tiny fake of the Graph endpoints the importer uses."""

    def __init__(self):
        self.calls, self.token_calls, self.sleeps = [], 0, []
        self.fail_next: list[httpx.Response | Exception] = []
        self.valid_token = "tok-1"
        self.messages_params = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "login.microsoftonline.com":
            self.token_calls += 1
            if self.token_status:
                return httpx.Response(self.token_status, json={"error": "invalid_client"})
            self.valid_token = f"tok-{self.token_calls}"
            return httpx.Response(200, json={"access_token": self.valid_token, "expires_in": 3600})
        self.calls.append(str(url))
        if self.fail_next:
            nxt = self.fail_next.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        if request.headers.get("Authorization") != f"Bearer {self.valid_token}":
            return httpx.Response(401, json={})
        path = url.path
        if path.endswith("/mailFolders") and "childFolders" not in path:
            return httpx.Response(200, json={"value": [
                {"id": "f-inbox", "displayName": "Inbox", "childFolderCount": 1},
                {"id": "f-sent", "displayName": "Sent Items", "childFolderCount": 0}]})
        if path.endswith("/mailFolders/f-inbox/childFolders"):
            return httpx.Response(200, json={"value": [{"id": "f-rep", "displayName": "Reports", "childFolderCount": 0}]})
        if path.endswith("/mailFolders/f-rep/messages"):
            self.messages_params.append(dict(url.params))
            if url.params.get("page") == "2":
                return httpx.Response(200, json={"value": [
                    {"id": "m3", "subject": "Third", "receivedDateTime": "2999-01-03T06:00:00Z", "hasAttachments": True,
                     "from": {"emailAddress": {"address": "ops@example.com"}}}]})
            return httpx.Response(200, json={"@odata.nextLink": str(url.copy_with(query=None)) + "?page=2", "value": [
                {"id": "m1", "subject": "First", "receivedDateTime": "2999-01-01T06:00:00Z", "hasAttachments": True,
                 "from": {"emailAddress": {"address": "ops@example.com"}}},
                {"id": "m2", "subject": "No files", "receivedDateTime": "2999-01-02T06:00:00Z", "hasAttachments": False}]})
        if path.endswith("/messages/m1/attachments") or path.endswith("/messages/m3/attachments"):
            return httpx.Response(200, json={"value": [
                {"@odata.type": "#microsoft.graph.fileAttachment", "id": "a1", "name": "r.xlsx", "size": 5, "isInline": False},
                {"@odata.type": "#microsoft.graph.fileAttachment", "id": "a2", "name": "logo.png", "size": 9, "isInline": True},
                {"@odata.type": "#microsoft.graph.itemAttachment", "id": "a3", "name": "forwarded mail", "size": 9}]})
        if path.endswith("/attachments/a1/$value"):
            return httpx.Response(200, content=b"XLSXBYTES")
        return httpx.Response(404, json={"error": "nope"})

    token_status = None


def graph(server: GraphServer, folder="Inbox/Reports", **kw):
    client = httpx.Client(transport=httpx.MockTransport(server.handler))
    return GraphMailSource("tenant", "cid", "secret", "box@x.com", folder, client=client,
                           sleep=server.sleeps.append, max_retries=3, **kw)


def test_graph_lists_messages_pages_and_lazy_attachments():
    srv = GraphServer()
    msgs = list(graph(srv).messages(dt.datetime(2999, 1, 1, tzinfo=UTC)))
    assert [m.id for m in msgs] == ["m1", "m3"]                                       # no-attachment mail skipped, 2 pages followed
    assert [a.name for a in msgs[0].attachments] == ["r.xlsx"]                          # inline image and item attachment skipped
    assert msgs[0].received_at == dt.datetime(2999, 1, 1, 6, tzinfo=UTC) and msgs[0].sender == "ops@example.com"
    first_params = srv.messages_params[0]
    assert first_params["$filter"] == "receivedDateTime ge 2999-01-01T00:00:00Z"
    assert first_params["$orderby"] == "receivedDateTime asc"
    assert not any(c.endswith("$value") for c in srv.calls)                            # nothing downloaded yet
    assert msgs[0].attachments[0].read() == b"XLSXBYTES"
    assert sum(c.endswith("a1/$value") for c in srv.calls) == 1


@pytest.mark.parametrize("folder", ["Inbox/Reports", "Inbox\\Reports", "Reports"])
def test_graph_finds_folder_by_path_or_by_name(folder):
    srv = GraphServer()
    assert len(list(graph(srv, folder).messages(None))) == 2


def test_graph_folder_missing_is_a_config_error():
    with pytest.raises(MailConfigError, match="not found"):
        list(graph(GraphServer(), "Inbox/Nope").messages(None))
    with pytest.raises(MailConfigError, match="not found"):
        list(graph(GraphServer(), "Nope").messages(None))


def test_graph_token_is_cached_and_refreshed_once_on_401():
    srv = GraphServer()
    src = graph(srv)
    list(src.messages(None))
    assert srv.token_calls == 1                                                        # one token for many calls
    srv.valid_token = "rotated"                                                        # server stops accepting ours
    src._folder_id = None
    list(src.messages(None))
    assert srv.token_calls == 2


def test_graph_retries_throttling_and_gives_up_on_persistent_errors():
    srv = GraphServer()
    srv.fail_next = [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(503)]
    assert len(list(graph(srv).messages(None))) == 2
    assert srv.sleeps[0] == 7 and len(srv.sleeps) == 2                                 # Retry-After honoured, then back-off

    srv = GraphServer()
    srv.fail_next = [httpx.Response(503)] * 10
    with pytest.raises(MailSourceError, match="retries"):
        list(graph(srv).messages(None))

    srv = GraphServer()
    srv.fail_next = [httpx.ConnectError("down")] * 10
    with pytest.raises(MailSourceError, match="network"):
        list(graph(srv).messages(None))


def test_graph_permission_and_credential_problems_are_config_errors():
    srv = GraphServer()
    srv.fail_next = [httpx.Response(403, json={})]
    with pytest.raises(MailConfigError, match="Mail.Read"):
        list(graph(srv).messages(None))
    srv = GraphServer()
    srv.token_status = 401
    with pytest.raises(MailConfigError, match="credentials"):
        list(graph(srv).messages(None))


def test_graph_requires_all_settings():
    with pytest.raises(MailConfigError, match="GRAPH_CLIENT_SECRET"):
        GraphMailSource("t", "c", "", "box@x.com", "Inbox/Reports")
    with pytest.raises(MailConfigError, match="outlook_folder"):
        GraphMailSource("t", "c", "s", "box@x.com", "")


# ---------------------------------------------------------------- desktop Outlook (fake COM objects)

class FakeAttachment:
    def __init__(self, name, data=b"DATA", type_=1, fail=False):
        self.FileName, self.Size, self.Type, self._data, self._fail = name, len(data), type_, data, fail

    def SaveAsFile(self, path):
        if self._fail:
            raise OSError("disk full")
        with open(path, "wb") as f:
            f.write(self._data)


class FakeCollection:
    def __init__(self, items):
        self._items = list(items)
        self.Count = len(self._items)

    def Item(self, i):
        return self._items[i - 1]


class FakeItems(FakeCollection):
    def Sort(self, prop, descending):
        assert prop == "[ReceivedTime]" and descending is True
        self._items.sort(key=lambda it: getattr(it, "_sortkey", dt.datetime.min), reverse=True)


class FakeMail:
    def __init__(self, entry, received, *atts, subject="Report"):
        self.EntryID, self.ReceivedTime, self.Subject = entry, received, subject
        self.SenderEmailAddress = "ops@example.com"
        self.Attachments = FakeCollection(atts)
        self._sortkey = received.replace(tzinfo=None) if received.tzinfo else received


class FakeNotMail:  # a meeting request: no ReceivedTime
    _sortkey = dt.datetime.max

    @property
    def ReceivedTime(self):
        raise AttributeError("ReceivedTime")


class FakeFolder:
    def __init__(self, name, items=(), children=()):
        self.Name, self.Items, self.Folders = name, FakeItems(items), FakeCollection(children)


class FakeApp:
    def __init__(self, *roots):
        self.roots = roots

    def GetNamespace(self, kind):
        assert kind == "MAPI"
        return type("NS", (), {"Folders": FakeCollection(self.roots)})()


def outlook_tree(*mails):
    return FakeApp(FakeFolder("ops@company.com", children=[
        FakeFolder("Inbox", children=[FakeFolder("Reports", items=mails)]), FakeFolder("Sent Items")]))


def test_outlook_reads_folder_oldest_first_and_skips_non_mail_and_non_file_attachments():
    mails = [
        FakeMail("e2", dt.datetime(2999, 1, 2, 6, tzinfo=UTC), FakeAttachment("b.xlsx", b"BBB")),
        FakeMail("e1", dt.datetime(2999, 1, 1, 6, tzinfo=UTC), FakeAttachment("a.xlsx", b"AAA"), FakeAttachment("ole", type_=6)),
        FakeNotMail(),
        FakeMail("e0", dt.datetime(2999, 1, 1, 5, tzinfo=UTC)),                           # no attachments
    ]
    src = OutlookComSource("Inbox/Reports", application=outlook_tree(*mails))
    msgs = list(src.messages(None))
    assert [m.id for m in msgs] == ["e1", "e2"]
    assert [a.name for a in msgs[0].attachments] == ["a.xlsx"] and msgs[0].attachments[0].read() == b"AAA"
    assert [m.id for m in src.messages(dt.datetime(2999, 1, 2, tzinfo=UTC))] == ["e2"]   # stops at `since`


def test_outlook_finds_folder_by_name_and_with_mailbox_prefix():
    mails = [FakeMail("e1", dt.datetime(2999, 1, 1, tzinfo=UTC), FakeAttachment("a.xlsx"))]
    for path in ("Reports", "ops@company.com/Inbox/Reports"):
        assert len(list(OutlookComSource(path, application=outlook_tree(*mails)).messages(None))) == 1


def test_outlook_naive_times_are_local_time():
    naive = dt.datetime(2999, 1, 1, 6, 0)
    m = list(OutlookComSource("Reports", application=outlook_tree(
        FakeMail("e1", naive, FakeAttachment("a.xlsx")))).messages(None))[0]
    assert m.received_at == naive.astimezone().astimezone(UTC)


def test_outlook_errors_are_classified():
    with pytest.raises(MailConfigError, match="not found"):
        list(OutlookComSource("Nope", application=outlook_tree()).messages(None))
    bad = FakeMail("e1", dt.datetime(2999, 1, 1, tzinfo=UTC), FakeAttachment("a.xlsx", fail=True))
    att = list(OutlookComSource("Reports", application=outlook_tree(bad)).messages(None))[0].attachments[0]
    with pytest.raises(MailSourceError, match="cannot save"):
        att.read()

    class Broken:
        def GetNamespace(self, kind):
            raise RuntimeError("RPC server unavailable")

    with pytest.raises(MailSourceError, match="cannot connect"):
        list(OutlookComSource("Reports", application=Broken()).messages(None))
    with pytest.raises(MailConfigError, match="pywin32"):          # this test machine is not Windows
        list(OutlookComSource("Reports").messages(None))
    with pytest.raises(MailConfigError, match="outlook_folder"):
        OutlookComSource("")


# ---------------------------------------------------------------- factory

def test_factory_chooses_source_from_settings(tmp_path, monkeypatch):
    assert type(make_source("eml", eml_folder=str(tmp_path))).__name__ == "EmlFolderSource"
    monkeypatch.delenv("GRAPH_TENANT_ID", raising=False)
    with pytest.raises(MailConfigError, match="GRAPH_TENANT_ID"):
        make_source("graph", folder="Inbox/Reports")
    with pytest.raises(MailConfigError, match="unknown MAIL_SOURCE"):
        make_source("pigeon")
    monkeypatch.setenv("MAIL_SOURCE", "eml")
    monkeypatch.setenv("EML_FOLDER", str(tmp_path))
    assert type(make_source()).__name__ == "EmlFolderSource"
