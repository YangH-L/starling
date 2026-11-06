"""验收 1a(设计 §16):一个世界两个 holder。

同一个库、同一个 Runtime 上开两个句柄绑 A、B,交替写 6 条事件(其中一对同 key 同文本)。
每个 holder 用自己的 stub LLM,抽取结果确定且互不相同。离线,不发网络请求。
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import starling
from starling import runtime as rt

EPOCH = "2026-01-01T00:00:00Z"
SAME_KEY, SAME_TEXT = "shared#0", "bob:同一条消息"

# (holder, key, text, 该 holder 的抽取结果里的 object)
WRITES = [
    ("A", SAME_KEY, SAME_TEXT, "a-auth"),
    ("B", SAME_KEY, SAME_TEXT, "b-auth"),          # 与上一条同 key 同文本,换 holder
    ("A", "a#1", "bob:A 的第二件事", "a-deploy"),
    ("B", "b#1", "bob:B 的第二件事", "b-deploy"),
    ("A", "a#2", "bob:A 的第三件事", "a-review"),
    ("B", "b#2", "bob:B 的第三件事", "b-review"),
]


def _canned(holder, obj):
    return (
        f'[{{"holder":"{holder}","holder_perspective":"FIRST_PERSON",'
        f'"subject":"cog-bob","predicate":"responsible_for","object":"{obj}",'
        '"modality":"BELIEVES","polarity":"POS","nesting_depth":0}]'
    )


def _llm(holder, obj):
    return starling.make_stub_llm(default_response=_canned(holder, obj))


def _open(path):
    r = rt._build_local_store_sqlite_runtime(path)
    r.start()
    return r


def _stmt_rows(path, ids):
    q = ",".join("?" * len(ids))
    with sqlite3.connect(str(path)) as c:
        return c.execute(
            f"SELECT id, holder_id, object_value, observed_at FROM statements WHERE id IN ({q})",
            list(ids)).fetchall()


def _event_seq(path, holder, key):
    with sqlite3.connect(str(path)) as c:
        return c.execute("SELECT seq FROM events WHERE holder_id = ? AND key = ?",
                         (holder, key)).fetchone()[0]


def _iso(seq):
    t = datetime.fromisoformat(EPOCH.replace("Z", "+00:00")) + timedelta(seconds=seq)
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_1a_two_holders_in_one_world(tmp_path):
    path = tmp_path / "w.db"
    r = _open(path)
    mems = {n: starling.Memory(r, agent=n, tenant_id="default", llm=_llm(n, "unused"))
            for n in ("A", "B")}
    results = {}
    try:
        for holder, key, text, obj in WRITES:
            mems[holder]._core.llm = _llm(holder, obj)      # 每次写给出确定的抽取
            res = mems[holder].remember_event(key, text, interlocutor="bob", epoch=EPOCH)
            assert res.outcome == "accepted" and not res.extraction_failed
            assert res.statement_ids
            results[(holder, key)] = res

        # 同 key 同文本:B 的结果是新写入,不是 A 的幂等回放,语句 id 与 A 的不相交
        a_same, b_same = results[("A", SAME_KEY)], results[("B", SAME_KEY)]
        assert b_same.outcome == "accepted"
        assert not set(b_same.statement_ids) & set(a_same.statement_ids)

        # observed_at:每个 holder 取自己事件的 epoch + seq
        seqs = {(h, k): _event_seq(path, h, k) for h, k, _, _ in WRITES}
        assert len(set(seqs.values())) == len(WRITES)       # 世界级序号,各不相同
        for holder, key, _, obj in WRITES:
            rows = _stmt_rows(path, results[(holder, key)].statement_ids)
            assert [(row[1], row[2]) for row in rows] == [(holder, obj)]
            assert rows[0][3] == _iso(seqs[(holder, key)])

        # recall:各看各的
        mems["A"].embed_pending("2026-01-01T00:01:00Z")
        for name, other in (("A", "B"), ("B", "A")):
            hits = mems[name].recall("responsible_for", k=20, include_volatile=True)
            got = {(h["row"].holder_id, h["row"].object_value) for h in hits}
            expected = {(n, o) for n, _, _, o in WRITES if n == name}
            assert got and {g[0] for g in got} == {name}
            assert not any(g[0] == other for g in got)
            assert got <= expected

        # pending_events:两人写完后都已 committed;留 pending 的用失败抽取另测
        assert mems["A"].pending_events() == []
        digest_both = mems["A"].digest()
    finally:
        for m in mems.values():
            m.close()

    # 去掉 B 的写入,按同样的 key、文本重建一个只有 A 的库:摘要不同
    path_a = tmp_path / "only_a.db"
    ra = _open(path_a)
    only_a = starling.Memory(ra, agent="A", tenant_id="default", llm=_llm("A", "unused"))
    try:
        for holder, key, text, obj in WRITES:
            if holder == "A":
                only_a._core.llm = _llm("A", obj)
                only_a.remember_event(key, text, interlocutor="bob", epoch=EPOCH)
        assert only_a.digest() != digest_both
    finally:
        only_a.close()


def test_1a_pending_events_list_both_holders_with_holder(tmp_path):
    r = _open(tmp_path / "p.db")
    failing = starling.make_stub_llm(default_response="")
    failing.set_default_response("", False, "stub extraction failure")
    a = starling.Memory(r, agent="A", tenant_id="default", llm=failing)
    b = starling.Memory(r, agent="B", tenant_id="default", llm=failing)
    try:
        a.remember_event(SAME_KEY, SAME_TEXT, interlocutor="bob", epoch=EPOCH)
        b.remember_event(SAME_KEY, SAME_TEXT, interlocutor="bob", epoch=EPOCH)
        a.remember_event("a#1", "bob:A 的第二件事", interlocutor="bob", epoch=EPOCH)
        for mem in (a, b):
            pend = mem.pending_events()
            assert sorted((p.holder, p.key) for p in pend) == [
                ("A", "a#1"), ("A", SAME_KEY), ("B", SAME_KEY)]
    finally:
        a.close()
