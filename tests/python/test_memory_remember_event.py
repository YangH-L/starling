"""Memory.remember_event 的三段写入与 EventConflict(迁移 0038 的 events 表写侧)。

rise 每轮对话写一条记忆事件,带调用方给的稳定 key。重放要幂等:同 key 同文本不会
再抽取一次、不会写重复语句;第三段之前崩溃后重调要按 engram 补齐成 committed。

离线:stub LLM、默认 stub 嵌入器,不发任何真实网络请求。
"""
import sqlite3

import pytest

import starling

# 与 tests/python/test_memory_facade.py 的 CANNED_JSON 相同:单元素 JSON 数组。
CANNED_JSON = (
    '[{"holder":"alice","holder_perspective":"FIRST_PERSON",'
    '"subject":"cog-bob","predicate":"responsible_for","object":"auth",'
    '"modality":"BELIEVES","polarity":"POS","nesting_depth":0}]'
)

KEY = "r1/deploy-m-s-0#3"          # 含 "/":Starling 不校验 key 字符集,原样存取


def _mem(tmp_path, **kw):
    return starling.Memory.open(str(tmp_path / "m.db"), agent="alice",
                                llm=starling.make_stub_llm(default_response=CANNED_JSON), **kw)


def _failing_llm():
    llm = starling.make_stub_llm(default_response="")
    llm.set_default_response("", False, "stub extraction failure")
    return llm


def _count(tmp_path, sql):
    with sqlite3.connect(str(tmp_path / "m.db")) as c:
        return c.execute(sql).fetchone()[0]


def test_new_key_commits_and_assigns_seq(tmp_path):
    mem = _mem(tmp_path)
    res = mem.remember_event(KEY, "bob:我负责鉴权模块", interlocutor="bob")
    assert res.outcome == "accepted" and not res.extraction_failed and res.statement_ids
    assert mem.next_seq() == 1 and mem.pending_events() == []
    with sqlite3.connect(str(tmp_path / "m.db")) as c:
        row = c.execute("SELECT state, seq, key FROM events").fetchone()
        payload = c.execute("SELECT payload_inline FROM engrams").fetchone()[0]
    assert row == ("committed", 0, KEY)
    assert payload.decode() == f"[{KEY}] bob:我负责鉴权模块"


def test_same_key_same_text_returns_stored_ids_without_extraction(tmp_path):
    mem = _mem(tmp_path)
    first = mem.remember_event(KEY, "bob:hi", interlocutor="bob")
    n = _count(tmp_path, "SELECT COUNT(*) FROM statements")
    mem._core.llm = _failing_llm()                 # 若再抽取就会 extraction_failed
    again = mem.remember_event(KEY, "bob:hi", interlocutor="bob")
    assert again.outcome == "idempotent" and not again.extraction_failed
    assert again.statement_ids == first.statement_ids
    assert _count(tmp_path, "SELECT COUNT(*) FROM statements") == n
    assert mem.next_seq() == 1


def test_same_key_different_text_conflicts(tmp_path):
    mem = _mem(tmp_path)
    mem.remember_event(KEY, "bob:hi", interlocutor="bob")
    with pytest.raises(starling.EventConflict):
        mem.remember_event(KEY, "bob:bye", interlocutor="bob")


def test_pending_retry_with_different_interlocutor_conflicts_and_leaves_row_untouched(tmp_path):
    # 同 key 同 text、换说话对象重试:语句会落进另一个 scope_parties,key 的契约是
    # "标识一次写入",所以拒绝而不是静默沿用任何一边。重入时 interlocutor 来自会话上下文,
    # 不是从库里读的,不靠并发就会遇到。
    mem = starling.Memory.open(str(tmp_path / "m.db"), agent="alice", llm=_failing_llm())
    assert mem.remember_event(KEY, "bob:hi", interlocutor="bob").extraction_failed
    mem._core.llm = starling.make_stub_llm(default_response=CANNED_JSON)
    with pytest.raises(starling.EventConflict, match="different interlocutor"):
        mem.remember_event(KEY, "bob:hi", interlocutor="carol")
    [p] = mem.pending_events()                       # 行没被改:仍是 bob、仍 pending、没另分配 seq
    assert (p.key, p.text, p.interlocutor, p.seq) == (KEY, "bob:hi", "bob", 0)
    assert mem.next_seq() == 1
    assert _count(tmp_path, "SELECT COUNT(*) FROM statements") == 0     # 被拒的重试没抽取


def test_pending_retry_with_same_interlocutor_still_commits_under_that_interlocutor(tmp_path):
    mem = starling.Memory.open(str(tmp_path / "m.db"), agent="alice", llm=_failing_llm())
    mem.remember_event(KEY, "bob:hi", interlocutor="bob")
    mem._core.llm = starling.make_stub_llm(default_response=CANNED_JSON)
    with pytest.raises(starling.EventConflict):      # 先被拒一次,再用对的说话对象补齐
        mem.remember_event(KEY, "bob:hi", interlocutor="carol")
    ok = mem.remember_event(KEY, "bob:hi", interlocutor="bob")
    assert not ok.extraction_failed and ok.statement_ids
    assert mem.pending_events() == []
    assert _count(tmp_path, "SELECT COUNT(*) FROM events WHERE state='committed'") == 1
    n = len(ok.statement_ids)
    assert _count(tmp_path, "SELECT COUNT(*) FROM statements "
                            "WHERE scope_parties_json LIKE '%bob%'") == n
    assert _count(tmp_path, "SELECT COUNT(*) FROM statements "
                            "WHERE scope_parties_json LIKE '%carol%'") == 0


def test_event_conflict_messages_tell_the_three_causes_apart(tmp_path):
    mem = starling.Memory.open(str(tmp_path / "m.db"), agent="alice", llm=_failing_llm())
    mem.remember_event("k-text", "bob:hi", interlocutor="bob")
    mem.remember_event("k-abandon", "bob:hi", interlocutor="bob")
    mem.abandon_event("k-abandon")

    def message(*args, **kw):
        with pytest.raises(starling.EventConflict) as e:
            mem.remember_event(*args, **kw)
        return str(e.value)

    different_text = message("k-text", "bob:bye", interlocutor="bob")
    different_interlocutor = message("k-text", "bob:hi", interlocutor="carol")
    abandoned = message("k-abandon", "bob:hi", interlocutor="bob")
    assert "different text" in different_text and "interlocutor" not in different_text
    assert "different interlocutor" in different_interlocutor
    assert "already abandoned" in abandoned and "different" not in abandoned


def test_committed_key_returns_stored_result_whatever_the_interlocutor(tmp_path):
    # 已提交的行不抽取,说话对象对结果没有影响,所以不比对:取行里存的结果。
    mem = _mem(tmp_path)
    first = mem.remember_event(KEY, "bob:hi", interlocutor="bob")
    again = mem.remember_event(KEY, "bob:hi", interlocutor="carol")
    assert again.statement_ids == first.statement_ids


def test_extraction_failure_leaves_pending_with_category_then_retry_commits(tmp_path):
    mem = starling.Memory.open(str(tmp_path / "m.db"), agent="alice", llm=_failing_llm())
    res = mem.remember_event(KEY, "bob:hi", interlocutor="bob")
    assert res.extraction_failed
    [p] = mem.pending_events()
    assert (p.key, p.text, p.interlocutor, p.seq) == (KEY, "bob:hi", "bob", 0)
    assert p.failure_category == res.failure_category != ""
    mem._core.llm = starling.make_stub_llm(default_response=CANNED_JSON)
    ok = mem.remember_event(KEY, "bob:hi", interlocutor="bob")
    assert not ok.extraction_failed and ok.statement_ids
    assert mem.pending_events() == [] and mem.next_seq() == 1      # 重试不另分配 seq


def test_crash_before_phase_three_is_completed_from_engram(tmp_path):
    # 模拟"② 落了语句、③ 没写成":先留一条 pending 行,再用裸 remember 写同一 payload。
    mem = starling.Memory.open(str(tmp_path / "m.db"), agent="alice", llm=_failing_llm())
    mem.remember_event(KEY, "bob:hi", interlocutor="bob")
    mem._core.llm = starling.make_stub_llm(default_response=CANNED_JSON)
    landed = mem.remember(f"[{KEY}] bob:hi", interlocutor="bob",
                          now="2026-01-01T00:00:00Z")
    assert landed.statement_ids
    n = _count(tmp_path, "SELECT COUNT(*) FROM statements")
    mem._core.llm = _failing_llm()                 # 补齐不得再抽取
    res = mem.remember_event(KEY, "bob:hi", interlocutor="bob")
    assert not res.extraction_failed
    assert sorted(res.statement_ids) == sorted(landed.statement_ids)
    assert _count(tmp_path, "SELECT COUNT(*) FROM statements") == n
    assert mem.pending_events() == []


def test_pending_without_engram_reextracts_from_stored_text(tmp_path):
    mem = starling.Memory.open(str(tmp_path / "m.db"), agent="alice", llm=_failing_llm())
    mem.remember_event(KEY, "bob:hi", interlocutor="bob")
    with sqlite3.connect(str(tmp_path / "m.db")) as c:   # python sqlite3 默认不开外键
        c.execute("DELETE FROM engrams")
    mem._core.llm = starling.make_stub_llm(default_response=CANNED_JSON)
    res = mem.remember_event(KEY, "bob:hi", interlocutor="bob")
    assert res.statement_ids and not res.extraction_failed
    assert _count(tmp_path, "SELECT COUNT(*) FROM engrams") == 1


def test_event_time_derives_from_seq(tmp_path):
    mem = _mem(tmp_path)
    mem.remember_event("a#0", "bob:one", interlocutor="bob")
    mem.remember_event("a#1", "bob:two", interlocutor="bob")
    with sqlite3.connect(str(tmp_path / "m.db")) as c:
        times = [r[0] for r in c.execute("SELECT created_at FROM engrams ORDER BY created_at")]
    assert times == ["2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z"]


def test_not_stored_outcome_leaves_pending_with_category(tmp_path):
    # 未入库(这里用非 ISO 的 epoch 触发 rejected)不能标 committed:标了的话这个 key
    # 以后每次都回 idempotent + 空语句,这条记忆就静默丢了(设计源 §9 禁止静默丢失)。
    mem = _mem(tmp_path)
    res = mem.remember_event(KEY, "bob:hi", interlocutor="bob", epoch="not-a-timestamp")
    assert res.outcome == "rejected" and not res.extraction_failed
    assert not res.statement_ids
    [p] = mem.pending_events()
    assert (p.key, p.seq) == (KEY, 0)
    assert p.failure_category == res.failure_category == "not_stored:rejected"
    with sqlite3.connect(str(tmp_path / "m.db")) as c:
        assert c.execute("SELECT state FROM events").fetchone() == ("pending",)
    # 重试(这次 epoch 正常)照原 seq 提交,记忆没丢。
    ok = mem.remember_event(KEY, "bob:hi", interlocutor="bob")
    assert ok.statement_ids and not ok.extraction_failed
    assert mem.pending_events() == [] and mem.next_seq() == 1


def test_new_key_extracts_instead_of_adopting_another_keys_statements(tmp_path):
    # 补齐分支只许重试已有的 pending 行走。新 key 若也去按 content_hash 认领语句,
    # 就会把别人(别的 key、或裸 remember)写的语句当成自己的结果,而自己的收据行
    # 指向的 engram 压根没建过。钉住 remember_event 里的 retrying_pending 这道门。
    mem = _mem(tmp_path)
    landed = mem.remember(f"[{KEY}] bob:hi", interlocutor="bob",
                          now="2026-01-01T00:00:00Z")
    assert landed.statement_ids
    mem._core.llm = _failing_llm()      # 新 key 必须自己抽取一次,所以这里会失败
    res = mem.remember_event(KEY, "bob:hi", interlocutor="bob")
    assert res.extraction_failed        # 认领的话就不会抽取、也就不会失败
    assert res.statement_ids != landed.statement_ids


def test_remember_event_requires_llm(tmp_path):
    mem = starling.Memory.open(str(tmp_path / "m.db"), agent="alice")
    with pytest.raises(starling._memory_core.LLMNotConfigured):
        mem.remember_event(KEY, "bob:hi", interlocutor="bob")
    assert mem.next_seq() == 0                      # 没写 pending 行


def test_abandon_pending_then_same_key_conflicts(tmp_path):
    mem = starling.Memory.open(str(tmp_path / "m.db"), agent="alice", llm=_failing_llm())
    mem.remember_event(KEY, "bob:hi", interlocutor="bob")
    mem.abandon_event(KEY)
    assert mem.pending_events() == []
    assert _count(tmp_path, "SELECT COUNT(*) FROM events WHERE state='abandoned'") == 1
    assert mem.next_seq() == 1                       # 放弃不回收 seq
    mem._core.llm = starling.make_stub_llm(default_response=CANNED_JSON)
    with pytest.raises(starling.EventConflict):
        mem.remember_event(KEY, "bob:hi", interlocutor="bob")
    with pytest.raises(starling.EventConflict):
        mem.remember_event(KEY, "bob:bye", interlocutor="bob")


def test_conflict_on_abandoned_key_leaves_the_row_abandoned(tmp_path):
    """放弃是终局:之后的 remember_event 抛 EventConflict,且一个字节都不许改回去。

    这里测的是"两次调用之间放弃"这一半:第一次写抽取失败 → 行留 pending,放弃,第二次
    写(LLM 正常)在**第 1 段**就冲突。真正的"第 2 段在途中被放弃"竞态在 Python 里表达
    不出来(FakeLLMAdapter 没有响应回调,绑定也没给 LLMAdapter 装 trampoline,Python 子类
    覆盖不了 extract),由 tests/cpp/test_memory_events.cpp 的
    AbandonDuringExtractIsNotResurrectedByPhaseThree 钉住第 3 段的围栏。
    """
    mem = starling.Memory.open(str(tmp_path / "m.db"), agent="alice", llm=_failing_llm())
    mem.remember_event(KEY, "bob:hi", interlocutor="bob")
    mem.abandon_event(KEY)
    mem._core.llm = starling.make_stub_llm(default_response=CANNED_JSON)

    with pytest.raises(starling.EventConflict):
        mem.remember_event(KEY, "bob:hi", interlocutor="bob")

    with sqlite3.connect(str(tmp_path / "m.db")) as c:
        state, category = c.execute("SELECT state, failure_category FROM events").fetchone()
    assert state == "abandoned"                  # 没被翻回 committed
    assert category != ""                        # 放弃前记的原因原样留着
    assert mem.pending_events() == []            # 终态不进待处理清单
    assert mem.next_seq() == 1                   # 冲突没分配新序号


def test_abandon_committed_is_refused(tmp_path):
    mem = _mem(tmp_path)
    mem.remember_event(KEY, "bob:hi", interlocutor="bob")
    with pytest.raises(starling.EventConflict):
        mem.abandon_event(KEY)
    assert _count(tmp_path, "SELECT state FROM events") == "committed"


def test_abandon_unknown_key_raises_keyerror_and_twice_is_noop(tmp_path):
    mem = starling.Memory.open(str(tmp_path / "m.db"), agent="alice", llm=_failing_llm())
    with pytest.raises(KeyError):
        mem.abandon_event("nope#0")
    mem.remember_event(KEY, "bob:hi", interlocutor="bob")
    mem.abandon_event(KEY)
    mem.abandon_event(KEY)                           # 已是终态,幂等


def test_two_holders_share_a_world_each_sees_own_statements(tmp_path):
    # 同一个库文件、同一 tenant、同一个 Runtime,两个 holder 交替写事件:各看各的语句。
    from starling import runtime as rt
    r = rt._build_local_store_sqlite_runtime(tmp_path / "w.db")
    r.start()
    llm = starling.make_stub_llm(default_response=CANNED_JSON)
    a = starling.Memory(r, agent="A", tenant_id="default", llm=llm)
    b = starling.Memory(r, agent="B", tenant_id="default", llm=llm)
    try:
        a.remember_event("k#0", "bob:我负责鉴权模块", interlocutor="bob")
        digest_only_a = a.digest()
        b.remember_event("k#0", "bob:我负责鉴权模块", interlocutor="bob")   # 同 key 同文本
        a.remember_event("k#1", "bob:另一件事", interlocutor="bob")
        b.remember_event("k#2", "bob:B 独有的事", interlocutor="bob")

        a.embed_pending("2026-01-01T00:01:00Z")
        for mem, name in ((a, "A"), (b, "B")):
            hits = mem.recall("我负责鉴权模块", k=20, include_volatile=True)
            assert hits and {h["row"].holder_id for h in hits} == {name}
        assert a.digest() != digest_only_a                 # B 写过之后库内容变了
    finally:
        a.close()


def test_pending_events_lists_every_holder_with_its_holder(tmp_path):
    # 中途失败的事件留在 pending;pending_events 列出世界里每个 holder 的,每项带 holder。
    from starling import runtime as rt
    r = rt._build_local_store_sqlite_runtime(tmp_path / "p.db")
    r.start()
    llm = _failing_llm()
    a = starling.Memory(r, agent="A", tenant_id="default", llm=llm)
    b = starling.Memory(r, agent="B", tenant_id="default", llm=llm)
    try:
        a.remember_event("k#0", "bob:hi", interlocutor="bob")
        b.remember_event("k#0", "bob:hi", interlocutor="bob")
        pend = a.pending_events()
        assert sorted(p.holder for p in pend) == ["A", "B"]
        assert all(p.key == "k#0" for p in pend)
    finally:
        a.close()


def test_abandon_from_another_holder_raises_keyerror_naming_the_owner(tmp_path):
    # rise 的世界级短连接以 agent "world" 打开;事件记在 A 名下。放弃要在 A 的句柄上做,
    # 从 "world" 放弃抛 KeyError,消息点名主人,行不动。
    from starling import runtime as rt_mod
    runtime = rt_mod._build_local_store_sqlite_runtime(tmp_path / "o.db")
    runtime.start()
    llm = _failing_llm()
    owner = starling.Memory(runtime, agent="A", tenant_id="default", llm=llm)
    world = starling.Memory(runtime, agent="world", tenant_id="default", llm=llm)
    try:
        owner.remember_event("ep1#3@A", "bob:hi", interlocutor="bob")
        with pytest.raises(KeyError) as err:
            world.abandon_event("ep1#3@A")
        assert "recorded under holder(s) A, not 'world'" in str(err.value)
        pending = [(evt.key, evt.holder) for evt in world.pending_events()]
        assert pending == [("ep1#3@A", "A")]
        owner.abandon_event("ep1#3@A")                   # 主人的句柄上放弃照常
        assert world.pending_events() == []
    finally:
        owner.close()


def test_empty_holder_is_rejected_with_valueerror_before_any_write(tmp_path):
    # '' 是 0039 无主旧行的标记,不能当 holder 用(裁定 R15):C++ 抛 invalid_argument,
    # pybind 译成 ValueError,库里不留行。
    mem = starling.Memory.open(str(tmp_path / "m.db"), agent="",
                               llm=starling.make_stub_llm(default_response=CANNED_JSON))
    try:
        with pytest.raises(ValueError):
            mem.remember_event(KEY, "bob:hi", interlocutor="bob")
        with pytest.raises(ValueError):
            mem.abandon_event(KEY)
        assert mem.next_seq() == 0
        assert mem.pending_events() == []
    finally:
        mem.close()
    assert _count(tmp_path, "SELECT COUNT(*) FROM events") == 0
