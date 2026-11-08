"""starling_client.connect / Client / RemoteMemory against a scripted service (httpx.MockTransport).

No real service and no Starling core: every response is replayed from the
`_Service` fake below, which mimics /api/v1/client (spec §11, §13.3).
"""
import inspect
import json
import threading
import time
import traceback

import httpx
import pytest

import starling_client
from starling_client import errors as E
from starling_client._transport import Transport
from starling_client.client import Client, _open_session
from starling_client.remote_memory import RemoteMemory
from starling_client.types import EmbedStats, PendingEvent, RememberResult, StatementRow

URL_SECRET = "10.9.8.7:4242/secret-path"
TOKEN_SECRET = "secret-tok"

ROW = {"id": "s1", "tenant_id": "w1", "holder_id": "carol", "holder_perspective": "first_person",
       "subject_kind": "cognizer", "subject_id": "cog-bob", "predicate": "responsible_for",
       "object_kind": "literal", "object_value": "auth", "canonical_object_hash": "h",
       "modality": "believes", "polarity": "pos", "confidence": 0.9, "observed_at": "t",
       "valid_from": "t", "valid_to": "", "consolidation_state": "volatile",
       "review_status": "none", "evidence_json": "[]", "affect_json": "{}",
       "semantic_claim_json": "", "source_spans_json": "", "a_future_field": 1}

REMEMBER = {"engram_ref": "e1", "statement_ids": ["s1"], "outcome": "accepted",
            "extraction_failed": False, "failure_category": "", "failure_detail": "",
            "catalog_version": "v", "source_preserved": False, "structured_claims_persisted": False,
            "accepted_by_predicate": {}, "rejected_by_predicate": {}}


def _error(code, status, transient=False):
    return httpx.Response(status, json={"code": code, "transient": transient, "detail": code})


class _Service:
    """Scripted /api/v1/client. `fail[path] = response` makes one route fail."""

    def __init__(self, heartbeat_ms=10_000):
        self.heartbeat_ms = heartbeat_ms
        self.seen = []
        self.fail = {}
        self.call_results = {}
        self.lock = threading.Lock()
        self.release_call = threading.Event()
        self.release_call.set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.split("/api/v1/client", 1)[1]   # base url has a path of its own
        body = json.loads(request.content or b"{}")
        with self.lock:
            self.seen.append((path, body, request.headers.get("authorization")))
        if path in self.fail:
            return self.fail[path]
        if path == "/connect":
            return httpx.Response(200, json={"session_id": "sess", "lease_ms": 30_000,
                                             "heartbeat_ms": self.heartbeat_ms,
                                             "info": {"protocol": 1}})
        if path == "/open":
            return httpx.Response(200, json={"handle_id": "h1"})
        if path == "/call":
            if body["method"] == "remember_event":
                self.release_call.wait(5)
            return httpx.Response(200, json={"result": self.call_results.get(body["method"])})
        if path.startswith("/admin/"):
            return httpx.Response(200, json=self.call_results.get(path, {}))
        return httpx.Response(200, json={})

    def paths(self):
        with self.lock:
            return [path for path, _body, _auth in self.seen]

    def bodies(self, path):
        with self.lock:
            return [body for seen_path, body, _auth in self.seen if seen_path == path]


def _client(service, **kw):
    transport = Transport(f"http://{URL_SECRET}", TOKEN_SECRET, 1.0,
                          _httpx_transport=httpx.MockTransport(service))
    return _open_session(transport, **kw)


@pytest.fixture
def svc():
    service = _Service()
    client = _client(service)
    yield service, client
    client.close()


def _assert_clean(exc):
    """url 与 token 不出现在异常及其 __cause__ / __context__ 两条链上（需求 5.5，修复轮 1 F5）。"""
    assert exc.__cause__ is None and exc.__context__ is None
    seen, todo = [], [exc]
    while todo:
        item = todo.pop()
        if item is None or any(item is other for other in seen):
            continue
        seen.append(item)
        todo += [item.__cause__, item.__context__]
        text = "".join(traceback.format_exception(type(item), item, item.__traceback__))
        for shown in (str(item), repr(item), text, repr(item.args)):
            assert URL_SECRET not in shown and TOKEN_SECRET not in shown


# ───────────────────────── signatures (spec §13.3, copied verbatim) ─────────────────────────

def _sig(fn):
    """Parameters as (name, kind, default) with the leading `self` dropped. Annotations are not compared:
    the modules use `from __future__ import annotations`, so they are strings."""
    params = list(inspect.signature(fn).parameters.values())
    if params and params[0].name == "self":
        params = params[1:]
    return [(p.name, p.kind.name, None if p.default is inspect.Parameter.empty else p.default)
            for p in params]


POS, KW = "POSITIONAL_OR_KEYWORD", "KEYWORD_ONLY"


def test_signatures_match_the_spec():
    assert _sig(starling_client.connect) == [("url", POS, None), ("token", KW, None), ("timeout_s", KW, 30.0)]
    assert _sig(Client.open) == [("world", POS, None), ("holder", KW, None), ("read_only", KW, False),
                                 ("settings", KW, None)]
    assert _sig(RemoteMemory.recall) == [("query", POS, None), ("k", KW, 10), ("mode", KW, "semantic"),
                                         ("strict", KW, True), ("include_volatile", KW, True)]
    assert _sig(RemoteMemory.remember_event) == [("key", POS, None), ("text", POS, None),
                                                 ("interlocutor", KW, None),
                                                 ("epoch", KW, "2026-01-01T00:00:00Z")]
    assert _sig(RemoteMemory.abandon_event) == [("key", POS, None)]
    assert _sig(RemoteMemory.content_ids) == [("ids", POS, None)]
    for name in ("digest", "pending_events", "next_seq"):
        assert _sig(getattr(RemoteMemory, name)) == [], name
    assert _sig(RemoteMemory.embed_pending) == [("now", POS, None)]
    assert _sig(RemoteMemory.close) == [("timeout", POS, None)]
    assert _sig(Client.info) == []
    assert _sig(Client.create_world) == [("name", POS, None), ("labels", KW, None), ("settings", KW, None),
                                         ("seed", KW, False)]
    assert _sig(Client.fork) == [("source", POS, None), ("name", POS, None), ("snapshot_id", KW, None),
                                 ("labels", KW, None), ("retention_group", KW, None),
                                 ("reclaimable", KW, False), ("seed", KW, False), ("settings", KW, None)]
    assert _sig(Client.upgrade_seed) == [("source", POS, None), ("name", POS, None)]
    assert _sig(Client.snapshot) == [("world", POS, None), ("note", POS, "")]
    assert _sig(Client.restore) == [("world", POS, None), ("snapshot_id", POS, None)]
    assert _sig(Client.delete_snapshot) == [("world", POS, None), ("snapshot_id", POS, None)]
    for name in ("digest", "pending_events", "close_world"):
        assert _sig(getattr(Client, name)) == [("world", POS, None)], name
    assert _sig(Client.list_worlds) == [("labels", POS, None), ("page", POS, 1), ("page_size", POS, 50),
                                        ("with_digest", POS, False)]
    assert _sig(Client.close) == []
    assert {"connect", "Client", "RemoteMemory"} <= set(starling_client.__all__)


# ───────────────────────── connect / open / calls ─────────────────────────

def test_connect_checks_the_protocol_and_sends_the_token(svc):
    service, client = svc
    path, body, auth = service.seen[0]
    assert (path, body, auth) == ("/connect", {"protocol": 1}, f"Bearer {TOKEN_SECRET}")
    assert (client.lease_ms, client.heartbeat_ms) == (30_000, 10_000)


def test_protocol_mismatch_raises_and_closes_the_transport():
    service = _Service()
    service.fail["/connect"] = _error("protocol_mismatch", 426)
    with pytest.raises(E.ProtocolMismatch) as err:
        _client(service)
    _assert_clean(err.value)


def test_open_and_call_shapes(svc):
    service, client = svc
    mem = client.open("w1", holder="carol", settings={"background": {"ingest": False}})
    assert service.bodies("/open")[-1] == {"session_id": "sess", "world": "w1", "holder": "carol",
                                           "read_only": False,
                                           "settings": {"background": {"ingest": False}}}
    service.call_results.update({
        "recall": [{"content_id": "c" * 64, "score": 0.5, "row": ROW}],
        "remember_event": REMEMBER, "content_ids": {"s1": "c" * 64}, "digest": "d" * 64,
        "pending_events": [{"key": "k", "text": "t", "interlocutor": "bob", "seq": 0,
                            "failure_category": "", "holder": "carol"}],
        "next_seq": 3, "embed_pending": {"embedded": 2, "failed": 0}})
    hits = mem.recall("who owns auth")
    assert service.bodies("/call")[-1] == {"handle_id": "h1", "method": "recall", "args": {
        "query": "who owns auth", "k": 10, "mode": "semantic", "strict": True, "include_volatile": True}}
    row = hits[0]["row"]
    assert isinstance(row, StatementRow) and row.holder_id == "carol" and hits[0]["content_id"] == "c" * 64
    with pytest.raises(Exception):
        row.subject_id = "x"                                       # frozen
    res = mem.remember_event("k1", "Bob owns auth", interlocutor="bob")
    assert isinstance(res, RememberResult) and res.statement_ids == ["s1"]
    assert service.bodies("/call")[-1]["args"] == {"key": "k1", "text": "Bob owns auth",
                                                   "interlocutor": "bob", "epoch": "2026-01-01T00:00:00Z"}
    assert mem.content_ids(["s1"]) == {"s1": "c" * 64}
    assert mem.digest() == "d" * 64 and mem.next_seq() == 3
    assert mem.pending_events() == [PendingEvent("k", "t", "bob", 0, "", "carol")]
    assert mem.embed_pending("2026-01-01T00:00:00Z") == EmbedStats(2, 0)
    mem.abandon_event("k1")
    assert service.bodies("/call")[-1]["args"] == {"key": "k1"}
    mem.close(timeout=1.0)
    assert service.bodies("/close")[-1]["handle_id"] == "h1"
    with pytest.raises(E.MemoryClosed):
        mem.digest()


def test_admin_shapes(svc):
    service, client = svc
    service.call_results["/admin/restore"] = {"digest": "r" * 64}
    service.call_results["/admin/digest"] = {"digest": "d" * 64}
    service.call_results["/admin/pending_events"] = {"events": []}
    service.call_results["/admin/list_worlds"] = {"worlds": [], "total": 0, "page": 2, "page_size": 5}
    client.create_world("s1", seed=True)
    assert service.bodies("/admin/create_world")[-1] == {"name": "s1", "labels": None,
                                                         "settings": None, "seed": True}
    client.fork("w1", "r1", reclaimable=True)
    assert service.bodies("/admin/fork")[-1]["reclaimable"] is True
    client.snapshot("w1", note="n")
    assert client.restore("w1", "snap") == "r" * 64
    assert client.digest("w1") == "d" * 64 and client.pending_events("w1") == []
    assert client.list_worlds(labels={"kind": "x"}, page=2, page_size=5, with_digest=True)["page"] == 2
    assert service.bodies("/admin/list_worlds")[-1] == {"labels": {"kind": "x"}, "page": 2,
                                                        "page_size": 5, "with_digest": True}


# ───────────────────────── heartbeat ─────────────────────────

def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_heartbeat_runs_every_heartbeat_ms_and_stops_after_close():
    service = _Service(heartbeat_ms=20)
    client = _client(service)
    assert _wait_for(lambda: service.paths().count("/heartbeat") >= 3)
    assert service.bodies("/heartbeat")[0] == {"session_id": "sess"}
    client.close()
    assert not client._heartbeat.is_alive()
    count = service.paths().count("/heartbeat")
    time.sleep(0.1)
    assert service.paths().count("/heartbeat") == count
    assert service.paths()[-1] == "/disconnect"
    assert not any(t.name == "starling-client-heartbeat" and t.is_alive() for t in threading.enumerate()
                   if t is client._heartbeat)


def test_heartbeat_failures_are_only_logged(caplog):
    service = _Service(heartbeat_ms=20)
    service.fail["/heartbeat"] = httpx.Response(503, text="down")
    client = _client(service)
    with caplog.at_level("WARNING", logger="starling_client.client"):
        assert _wait_for(lambda: service.paths().count("/heartbeat") >= 2)
    assert client._heartbeat.is_alive()                          # keeps trying
    for record in caplog.records:
        assert URL_SECRET not in record.getMessage() and TOKEN_SECRET not in record.getMessage()
    client.close()


# ───────────────────────── session_expired: no automatic reopen ─────────────────────────

def test_session_expired_raises_and_never_reopens(svc):
    service, client = svc
    mem = client.open("w1", holder="carol")
    service.fail["/call"] = _error("session_expired", 401)
    with pytest.raises(E.SessionExpired) as err:
        mem.digest()
    _assert_clean(err.value)
    del service.fail["/call"]                                    # even if the service would answer now
    before = service.paths()
    with pytest.raises(E.SessionExpired):
        mem.digest()
    with pytest.raises(E.SessionExpired):
        client.open("w1", holder="carol")
    assert service.paths() == before                              # no request: not reopened, not reconnected


def test_heartbeat_seeing_session_expired_marks_the_session_dead():
    service = _Service(heartbeat_ms=20)
    client = _client(service)
    mem = client.open("w1", holder="carol")
    service.fail["/heartbeat"] = _error("session_expired", 401)
    assert _wait_for(lambda: not client._heartbeat.is_alive())
    with pytest.raises(E.SessionExpired):
        mem.digest()
    assert "/open" not in service.paths()[2:]
    client.close()
    assert service.paths()[-1] != "/disconnect"


# ───────────────────────── close(timeout) ─────────────────────────

def test_close_waits_locally_and_times_out_with_the_handle_open(svc):
    service, client = svc
    mem = client.open("w1", holder="carol")
    service.call_results["remember_event"] = REMEMBER
    service.release_call.clear()
    worker = threading.Thread(target=lambda: mem.remember_event("k", "t", interlocutor="bob"))
    worker.start()
    assert _wait_for(lambda: mem._in_flight == 1)
    with pytest.raises(TimeoutError):
        mem.close(timeout=0.05)
    assert "/close" not in service.paths()                        # never asked the service
    service.release_call.set()
    worker.join(5)
    service.call_results["next_seq"] = 5
    assert mem.next_seq() == 5                                    # handle accepts calls again
    mem.close(timeout=1.0)


def test_close_service_side_world_busy_is_timeout_and_handle_stays_open(svc):
    service, client = svc
    mem = client.open("w1", holder="carol")
    service.fail["/close"] = _error("world_busy", 503, transient=True)
    with pytest.raises(TimeoutError) as err:
        mem.close(timeout=0.5)
    _assert_clean(err.value)
    assert service.bodies("/close")[-1]["timeout_s"] <= 0.5
    del service.fail["/close"]
    service.call_results["digest"] = "d" * 64
    assert mem.digest() == "d" * 64                               # still open
    mem.close(timeout=0.5)


# ───────────────────────── every code the routes return maps to a named class (R46) ─────────────────────────

_NAMED = {"recall_degraded": E.RecallDegradedError, "event_conflict": E.EventConflict,
          "handle_closed": E.MemoryClosed, "protocol_mismatch": E.ProtocolMismatch,
          "session_expired": E.SessionExpired, "event_not_found": E.EventNotFound}

# (code, http_status, transient) for every code the /api/v1/client routes return: the catalog values
# (pinned against the C++ catalog by tests/python/test_client_routes.py::ROUTE_CODES).
_ROUTE_CODES = [
    ("protocol_mismatch", 426, False), ("session_expired", 401, False), ("handle_closed", 409, False),
    ("world_held", 409, False), ("world_busy", 503, True), ("world_read_only", 409, False),
    ("world_not_found", 404, False), ("world_settings_mismatch", 409, False),
    ("invalid_settings", 400, False), ("invalid_request", 400, False), ("recall_degraded", 503, True),
    ("event_conflict", 409, False), ("event_not_found", 404, False), ("llm_not_configured", 409, False),
    ("worlds_unavailable", 503, True), ("world_open_failed", 500, False), ("endpoint_changed", 409, False),
    ("endpoint_key_missing", 409, False),
    ("world_exists", 409, False), ("invalid_world_name", 400, False), ("world_schema_behind", 409, False),
    ("world_not_seed", 409, False), ("snapshot_not_found", 404, False), ("snapshot_corrupt", 409, False),
    ("restore_digest_mismatch", 500, False), ("fork_digest_mismatch", 500, False),
    ("insufficient_disk", 507, False), ("invalid_world_query", 400, False), ("world_protected", 409, False),
    ("labels_immutable", 409, False), ("invalid_labels", 400, False), ("snapshot_failed", 500, False),
    ("internal_error", 500, False),
]


@pytest.mark.parametrize("code,status,transient", _ROUTE_CODES, ids=[c for c, _s, _t in _ROUTE_CODES])
def test_every_route_code_raises_its_class_through_a_call(svc, code, status, transient):
    service, client = svc
    mem = client.open("w1", holder="carol")
    service.fail["/call"] = _error(code, status, transient)
    with pytest.raises(E.StarlingClientError) as err:
        mem.digest()
    want = _NAMED.get(code) or (E.ServiceError if status >= 500 else E.WorldError)
    assert type(err.value) is want
    assert (err.value.code, err.value.transient) == (code, transient)
    _assert_clean(err.value)
    if code == "event_not_found":
        assert isinstance(err.value, KeyError)
    if code == "handle_closed":
        assert isinstance(err.value, RuntimeError)
