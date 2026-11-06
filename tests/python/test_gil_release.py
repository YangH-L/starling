"""GIL 释放回归(2026-06-11 修复:全绑定层原本 0 处 gil_scoped_release)。

被钉的事实:LLM/embedder 网络调用对应的绑定在 C++ 阻塞期间必须释放 GIL,
否则 dashboard 把工作丢进 anyio 线程也没用——线程换了、锁没放,事件循环
照样冻结(用户可见:remember 进行中全站无响应)。

测法:FakeLLMAdapter.set_delay_ms 在 C++ extract() 里制造确定性阻塞窗口
(零网络);线程 A 调被守护的绑定阻塞其中,主线程同时累加计数器——
GIL 已释放 ⇒ 计数推进;未释放 ⇒ 主线程第一条字节码就卡住,计数为 0。
阈值给得很宽(700ms 窗口只要求 ≥5 次 5ms 步进),CI 抖动安全。
"""
import sqlite3
import sys
import threading
import time

import starling
from starling import _core

CANNED_JSON = (
    '[{"holder":"self","holder_perspective":"FIRST_PERSON",'
    '"subject":"Bob","predicate":"responsible_for","object":"auth",'
    '"modality":"BELIEVES","polarity":"POS","nesting_depth":0}]'
)


def _progress_while(blocking_call) -> int:
    """跑 blocking_call 于线程 A,返回其阻塞期间主线程完成的步进数。"""
    started = threading.Event()
    done = threading.Event()

    def runner():
        started.set()
        try:
            blocking_call()
        finally:
            done.set()

    t = threading.Thread(target=runner)
    t.start()
    started.wait()
    progress = 0
    while not done.is_set():
        time.sleep(0.005)
        progress += 1
    t.join()
    return progress


def test_memory_remember_releases_gil(tmp_path):
    # 生产入口:MemoryCore.remember → _core.memory_remember(bind_13 守护)。
    llm = starling.make_stub_llm(default_response=CANNED_JSON)
    llm.set_delay_ms(700)
    mem = starling.Memory.open(str(tmp_path / "gil.db"), agent="alice", llm=llm)
    try:
        progress = _progress_while(lambda: mem.remember("Bob owns the auth module"))
    finally:
        mem.close()
    assert progress >= 5, (
        f"memory_remember 阻塞 C++ 期间主线程仅推进 {progress} 步——GIL 未释放")


def test_extractor_run_releases_gil(tmp_path):
    # 直调路径:_core.Extractor.run(bind_06 守护,eval 脚本等使用)。
    from starling import runtime as rt
    r = rt._build_local_store_sqlite_runtime(tmp_path / "gil2.db")
    r.start()
    # 先经正常路径建好 engram(零延迟),再用带延迟的 run 复抽(noop 路径同样
    # 穿过 LLM 调用,阻塞窗口成立)。
    llm = starling.make_stub_llm(default_response=CANNED_JSON)
    mem = starling.Memory(r, agent="alice", tenant_id="default", llm=llm)
    engram_ref = mem.remember("Bob owns the auth module").engram_ref
    assert engram_ref

    llm.set_delay_ms(700)
    ex = _core.Extractor(r.adapter.connection(), llm)
    progress = _progress_while(
        lambda: ex.run(engram_ref, b"Bob owns the auth module", "alice", "default", {}, ""))
    mem.close()
    assert progress >= 5, (
        f"Extractor.run 阻塞 C++ 期间主线程仅推进 {progress} 步——GIL 未释放")


def test_memory_recall_releases_gil_while_embedding_the_query(tmp_path, monkeypatch):
    # 生产入口:Memory.recall(mode="semantic")→ SemanticRetriever.vector_recall(bind_10),
    # C++ 里先同步嵌入查询词。阻塞窗口:嵌入器指向一个只接受连接、从不回包的本机端口,
    # timeout_ms=700、不重试,embed 阻塞到超时后降级(degraded,不抛)。零外网。
    import socket

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(8)                                  # 内核完成握手,没人 accept、没人回
    port = silent.getsockname()[1]
    emb = starling.make_openai_embedder(model="silent", base_url=f"http://127.0.0.1:{port}/v1",
                                        dim=8, timeout_ms=700, max_retries=0)
    mem = starling.Memory.open(str(tmp_path / "gil3.db"), agent="alice",
                               llm=starling.make_stub_llm(default_response=CANNED_JSON),
                               embedder=emb)
    try:
        progress = _progress_while(lambda: mem.recall("auth", k=3))
    finally:
        mem.close()
        silent.close()
    assert progress >= 5, (
        f"vector_recall 嵌入查询词期间主线程仅推进 {progress} 步——GIL 未释放")


# --- 事件与摘要绑定(bind_13):读/写在大库上要持续一段时间,期间必须放 GIL ---

def _sql(path, sql):
    with sqlite3.connect(str(path)) as c:
        c.execute(sql)


def _seed_statements(path, total):
    """把库里唯一一条语句按行克隆到 total 条(只换 id/object_value/canonical_object_hash),
    直接走 SQL,比 total 次 remember 快几个数量级。"""
    with sqlite3.connect(str(path)) as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(statements)")]
        sel = ",".join(
            {"id": "'s'||n", "object_value": "'o'||n", "canonical_object_hash": "'h'||n"}.get(col, col)
            for col in cols)
        c.execute(
            f"WITH RECURSIVE g(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM g WHERE n<{total}) "
            f"INSERT INTO statements({','.join(cols)}) SELECT {sel} FROM statements, g")


def _event_mem(tmp_path, name):
    llm = starling.make_stub_llm(default_response=CANNED_JSON)
    return starling.Memory.open(str(tmp_path / name), agent="alice", llm=llm)


def test_memory_digest_releases_gil(tmp_path):
    # 计划写「约 2000 条」,实测 2000 条 digest 只要 ~40ms,窗口太短;4 万条约 0.8s。
    mem = _event_mem(tmp_path, "dg.db")
    try:
        mem.remember_event("e#0", "bob:我负责鉴权模块", interlocutor="bob")
        _seed_statements(tmp_path / "dg.db", 40000)
        t0 = time.monotonic()
        progress = _progress_while(mem.digest)
        elapsed = time.monotonic() - t0
    finally:
        mem.close()
    assert elapsed > 0.3, f"digest 只跑了 {elapsed:.2f}s,窗口太短,测不出 GIL"
    assert progress >= 5, (
        f"memory_digest 阻塞 C++ 期间主线程仅推进 {progress} 步——GIL 未释放")


def test_memory_pending_events_releases_gil(tmp_path):
    # 30 万行 pending:C++ 取行与 Python 建 dict 各占一段,放 GIL 的是前一段。
    llm = starling.make_stub_llm(default_response="")
    mem = starling.Memory.open(str(tmp_path / "pe.db"), agent="alice", llm=llm)
    try:
        _sql(tmp_path / "pe.db",
             "WITH RECURSIVE g(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM g WHERE n<300000) "
             "INSERT INTO events(tenant_id,holder_id,key,text,interlocutor,text_sha256,seq,state) "
             "SELECT 'default','alice','k'||n,'t','bob','x',n,'pending' FROM g")
        # 调大切换间隔:否则 Python 侧建 dict 时解释器每 5ms 也会让出 GIL,
        # 绑定没放 GIL 照样"有进展",测不出差别。
        old_interval = sys.getswitchinterval()
        sys.setswitchinterval(30)
        try:
            t0 = time.monotonic()
            progress = _progress_while(mem.pending_events)
            elapsed = time.monotonic() - t0
        finally:
            sys.setswitchinterval(old_interval)
    finally:
        mem.close()
    assert elapsed > 0.3, f"pending_events 只跑了 {elapsed:.2f}s,窗口太短,测不出 GIL"
    assert progress >= 5, (
        f"memory_pending_events 阻塞 C++ 期间主线程仅推进 {progress} 步——GIL 未释放")


def test_memory_abandon_event_releases_gil(tmp_path):
    # 另一条连接持有写锁,abandon 的写事务在 busy_timeout 里等,这段等待就是阻塞窗口。
    mem = _event_mem(tmp_path, "ab.db")
    mem.remember_event("e#0", "bob:hi", interlocutor="bob")
    _sql(tmp_path / "ab.db", "UPDATE events SET state='pending'")
    locker = sqlite3.connect(str(tmp_path / "ab.db"), isolation_level=None,
                             check_same_thread=False)
    try:
        locker.execute("BEGIN IMMEDIATE")
        release = threading.Timer(0.7, lambda: locker.execute("ROLLBACK"))
        release.start()
        progress = _progress_while(lambda: mem.abandon_event("e#0"))
        release.join()
    finally:
        locker.close()
        mem.close()
    assert progress >= 5, (
        f"memory_abandon_event 阻塞 C++ 期间主线程仅推进 {progress} 步——GIL 未释放")


def test_memory_abandon_event_still_raises_keyerror_for_unknown_key(tmp_path):
    # 释放 GIL 后异常要在 GIL 回来之后再构造:未知 key 仍是 KeyError(不是 IndexError)。
    import pytest
    mem = _event_mem(tmp_path, "ab2.db")
    try:
        with pytest.raises(KeyError):
            mem.abandon_event("nope#0")
    finally:
        mem.close()
