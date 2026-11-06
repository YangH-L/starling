"""迁移 0038:events 表(按稳定 key 记一次写入的收据)与门面读侧 next_seq / pending_events。

离线:stub LLM,不发任何真实网络请求。
"""
import sqlite3

import pytest

import starling
from starling import runtime

# 与 tests/python/test_memory_facade.py 的 CANNED_JSON 相同:单元素 JSON 数组。
CANNED_JSON = (
    '[{"holder":"alice","holder_perspective":"FIRST_PERSON",'
    '"subject":"cog-bob","predicate":"responsible_for","object":"auth",'
    '"modality":"BELIEVES","polarity":"POS","nesting_depth":0}]'
)


def _mem(tmp_path, **kw):
    return starling.Memory.open(str(tmp_path / "m.db"), agent="alice",
                                llm=starling.make_stub_llm(default_response=CANNED_JSON), **kw)


def _insert(db_path, tenant, key, seq, state, *, interlocutor="bob", failure_category=""):
    with sqlite3.connect(str(db_path)) as c:
        c.execute(
            "INSERT INTO events(tenant_id,key,text,interlocutor,text_sha256,seq,state,"
            "failure_category) VALUES(?,?,?,?,?,?,?,?)",
            (tenant, key, f"text-{key}", interlocutor, "h", seq, state, failure_category))


def test_events_table_shape(tmp_path):
    rt = runtime._build_local_store_sqlite_runtime(tmp_path / "s.db"); rt.start()
    with sqlite3.connect(str(rt.adapter.db_path)) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(events)")}
        pk = [r[1] for r in sorted(c.execute("PRAGMA table_info(events)"), key=lambda r: r[5]) if r[5]]
        assert c.execute("SELECT name FROM schema_migrations WHERE version=38").fetchone() is not None
    # 0039 起按 holder 区分:多一列 holder_id,主键 (tenant_id, holder_id, key)。
    assert cols == {"tenant_id", "holder_id", "key", "text", "interlocutor", "text_sha256",
                    "engram_ref", "statement_ids_json", "seq", "state", "failure_category"}
    assert pk == ["tenant_id", "holder_id", "key"]


def test_events_state_check_rejects_unknown(tmp_path):
    rt = runtime._build_local_store_sqlite_runtime(tmp_path / "s.db"); rt.start()
    with sqlite3.connect(str(rt.adapter.db_path)) as c, pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO events(tenant_id,key,text,interlocutor,text_sha256,seq,state) "
                  "VALUES('default','k','t','bob','h',0,'done')")


def test_empty_library_next_seq_zero_and_no_pending(tmp_path):
    mem = _mem(tmp_path)
    assert mem.next_seq() == 0
    assert mem.pending_events() == []


def test_next_seq_counts_abandoned_rows_so_numbers_are_never_reused(tmp_path):
    mem = _mem(tmp_path)
    db = mem._rt.adapter.db_path
    _insert(db, "default", "a", 0, "committed")
    _insert(db, "default", "b", 1, "abandoned")
    assert mem.next_seq() == 2


def test_next_seq_is_scoped_to_the_facade_tenant(tmp_path):
    mem = _mem(tmp_path)
    db = mem._rt.adapter.db_path
    _insert(db, "other", "x", 7, "pending")
    assert mem.next_seq() == 0
    _insert(db, "default", "a", 0, "pending")
    assert mem.next_seq() == 1


def test_pending_events_ordered_by_seq_and_only_pending(tmp_path):
    mem = _mem(tmp_path)
    db = mem._rt.adapter.db_path
    _insert(db, "default", "late", 5, "pending", interlocutor="carol", failure_category="timeout")
    _insert(db, "default", "done", 2, "committed")
    _insert(db, "default", "gone", 3, "abandoned")
    _insert(db, "default", "early", 1, "pending")
    _insert(db, "other", "foreign", 0, "pending")
    got = mem.pending_events()
    assert got == [
        starling.PendingEvent(key="early", text="text-early", interlocutor="bob",
                              seq=1, failure_category=""),
        starling.PendingEvent(key="late", text="text-late", interlocutor="carol",
                              seq=5, failure_category="timeout"),
    ]


def test_pending_event_is_frozen():
    ev = starling.PendingEvent(key="k", text="t", interlocutor="bob", seq=0, failure_category="")
    with pytest.raises(AttributeError):
        ev.seq = 1  # type: ignore[misc]


def test_clear_tenant_removes_events_of_that_tenant_only(tmp_path):
    mem = _mem(tmp_path)
    db = mem._rt.adapter.db_path
    _insert(db, "default", "a", 0, "pending")
    _insert(db, "default", "b", 1, "committed")
    _insert(db, "other", "x", 0, "pending")
    out = mem._core.clear_tenant()
    assert out["deleted"]["events"] == 2
    with sqlite3.connect(str(db)) as c:
        left = c.execute("SELECT tenant_id, key FROM events").fetchall()
    assert left == [("other", "x")]
    assert mem.next_seq() == 0
    assert mem.pending_events() == []
