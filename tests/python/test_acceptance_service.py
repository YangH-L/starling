"""第 4 期验收（spec §16 第 1、1b、6、7、8 条，另加 §12 的跨世界 GIL 与「日志与错误体里没有地址和密钥」）。

全部经真的 `starling-serve` 子进程（python -m starling.service，数据目录与 HOME 都在 tmp 下，环境里没有 OPENAI_*）
与 starling_client。抽取与嵌入走子进程里的本机 OpenAI 兼容假端点（fakes/openai_fake_server.py）：每个测试模块一个
随机 key，写进 D/starling.json 的 providers（抽取、嵌入各一个），base_url 指向假端点。

服务子进程按模块共用（test_7 自己起、自己的数据目录）；世界名带测试编号，互不重复。每个测试开始与结束时把假端点的
开关恢复缺省。所有等待都有上限，轮询状态而不是 sleep；子进程在夹具的 finally 里清掉。

本模块不设 NO_PROXY（裁定 R97）：starling_client 连回环地址时自己不走代理（Transport 的 trust_env=False），这台机器上配了
系统代理时，验收照样直连，所以验收也覆盖到这处。
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import threading
import time
import traceback

import httpx
import pytest

import starling
import starling_client
from starling import _core
from starling_client import errors as client_errors

from fakes.openai_fake_server import FakeOpenAI
from fakes.service_proc import ServiceFactory

DIM = 64
HOLDER = "alice"
EPOCH = "2026-01-01T00:00:00Z"
NOW = "2026-06-01T00:00:00Z"
API = "/api/v1/client"
_JOIN_S = 30.0
_EXTRACT_TIMEOUT_MS = 10_000


# ───────────────────────── 夹具 ─────────────────────────

@pytest.fixture(scope="module")
def fake_openai(tmp_path_factory):
    """子进程里的假端点（本模块共用），key 是这一次的随机值。"""
    root = tmp_path_factory.mktemp("fake-openai")
    fake = FakeOpenAI(api_key="sk-fake-" + secrets.token_hex(16), dim=DIM,
                      log_path=root / "fake-stderr.log")
    fake.start()
    try:
        yield fake
    finally:
        fake.stop()


@pytest.fixture(autouse=True)
def _fake_defaults(fake_openai):
    """每个测试前后把假端点的开关恢复缺省（不清计数）。"""
    fake_openai.reset()
    try:
        yield
    finally:
        fake_openai.reset()


def _write_config(data_dir, fake: FakeOpenAI) -> None:
    """D/starling.json：抽取、嵌入各一个 provider，指向假端点，api_key 是随机 key。后台 tick 关掉（摘要比较要求世界只被
    测试自己的调用改动）；左脑 lane 关掉（进程内 Memory 一侧没有它）。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "starling.json").write_text(json.dumps({
        "providers": {
            "fake-extract": {"provider": "openai", "model": "fake-extract", "base_url": fake.base_url,
                             "api_key": fake.api_key, "timeout_ms": _EXTRACT_TIMEOUT_MS,
                             "max_retries": 0},
            "fake-embed": {"provider": "openai", "model": "fake-embed", "base_url": fake.base_url,
                           "api_key": fake.api_key, "dim": DIM},
        },
        "roles": {"extraction": "fake-extract", "embedding": "fake-embed"},
        "source_lane_enabled": False,
        "tick_interval_s": 0,
    }), encoding="utf-8")


@pytest.fixture(scope="module")
def service(tmp_path_factory, fake_openai):
    """本模块共用的 starling-serve 子进程（就绪之后才交出）。返回 ServiceProc：.url、.token、.stderr()。"""
    root = tmp_path_factory.mktemp("service")
    factory = ServiceFactory(root, token=secrets.token_urlsafe(24))
    try:
        data_dir = root / "D"
        _write_config(data_dir, fake_openai)
        yield factory(data_dir).start_and_wait()
    finally:
        factory.kill_all()


@pytest.fixture
def own_services(tmp_path, fake_openai):
    """起服务的工厂（test_7 用：自己的数据目录、可以 SIGTERM）。用例结束时杀掉每一个还活着的子进程。"""
    factory = ServiceFactory(tmp_path, token=secrets.token_urlsafe(24))
    try:
        yield factory
    finally:
        factory.kill_all()


@contextlib.contextmanager
def _connected(url: str, token: str):
    client = starling_client.connect(url, token=token, timeout_s=30.0)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def client(service):
    with _connected(service.url, service.token) as connected:
        yield connected


@contextlib.contextmanager
def _openai_key(key: str):
    """只在块内把 OPENAI_API_KEY 设成假端点的 key（C++ 适配器在构造时从环境读），退出时还原（原来没设就删掉）。"""
    previous = os.environ.get("OPENAI_API_KEY")
    os.environ["OPENAI_API_KEY"] = key
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = previous


def _text(speaker: str, utterance: str) -> str:
    return f"{speaker}：{utterance}"


def _remember_ok(result) -> None:
    assert result.outcome in ("accepted", "idempotent"), result
    assert result.extraction_failed is False, result


def _run_in_thread(fn):
    """在线程里跑 fn，返回 (thread, box)；box["value"] 或 box["error"] 是结果。"""
    box: dict = {}

    def runner():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 — 交给主线程断言
            box["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    return thread, box


def _join(thread: threading.Thread, box: dict):
    thread.join(_JOIN_S)
    assert not thread.is_alive(), "worker thread did not finish"
    if "error" in box:
        raise box["error"]
    return box.get("value")


# ───────────────────────── 第 1 条：三个世界互不可见 ─────────────────────────

def test_1_three_worlds_isolated(client):
    """一个服务进程开三个世界（同一个 holder），各自 remember_event + recall：每个世界召回到自己的语句，召回结果里
    从来没有别的世界的语句。"""
    worlds = {"t1-a": "Alice deploys the auth service", "t1-b": "Bruno tunes the billing cache",
              "t1-c": "Chen rotates the infra keys"}
    own: dict[str, set[str]] = {}
    handles = {}
    for world, utterance in worlds.items():
        client.create_world(world)
        mem = handles[world] = client.open(world, holder=HOLDER)
        result = mem.remember_event(f"{world}#0", _text("bob", utterance), interlocutor="bob")
        _remember_ok(result)
        assert len(result.statement_ids) == 1
        own[world] = set(mem.content_ids(result.statement_ids).values())
        assert mem.embed_pending(NOW).embedded >= 1
    assert len(set().union(*own.values())) == 3                     # 三条内容各不相同
    for world, mem in handles.items():
        for query in worlds.values():
            hits = {hit["content_id"] for hit in mem.recall(query, k=10)}
            assert hits <= own[world], (world, query, hits)           # 只召回本世界的
        assert own[world] <= {hit["content_id"] for hit in mem.recall(worlds[world], k=10)}
    for mem in handles.values():
        mem.close()


# ───────────────────────── 第 1b 条：跨世界不受影响 ─────────────────────────

_X_EVENTS = [(f"x#{i}", _text("bob", f"X fact number {i} about the ledger")) for i in range(4)]


def test_1b_snapshot_write_restore_y_while_x_writes(client, fake_openai):
    """X 开着句柄、一个线程正往 X 写一组固定的 4 条事件（第一条的抽取被假端点挂住，所以下面整段都发生在 X 写入途中）；
    同时对 Y 依次做快照、写入、关句柄、恢复，全部成功，恢复后 Y 的摘要等于快照时的 digest。另建对照世界 X'，写同样
    的 4 条、期间不做任何 Y 操作：digest(X) == digest(X')，X 的语句按内容 id 逐条等于 X' 的。最后给 Y 再开一个句柄，恢复 Y 返回 world_held。"""
    for world in ("t1b-x", "t1b-xc", "t1b-y"):
        client.create_world(world)
    mem_x = client.open("t1b-x", holder=HOLDER)
    mem_y = client.open("t1b-y", holder=HOLDER)
    _remember_ok(mem_y.remember_event("y#0", _text("bob", "Y before the snapshot"), interlocutor="bob"))

    fake_openai.configure(hold_keys=["x#0"])
    thread, box = _run_in_thread(lambda: [mem_x.remember_event(key, text, interlocutor="bob")
                                          for key, text in _X_EVENTS])
    try:
        fake_openai.wait_until(lambda state: state["held"] >= 1)
        snap = client.snapshot("t1b-y", note="t1b")
        _remember_ok(mem_y.remember_event("y#1", _text("bob", "Y after the snapshot"),
                                          interlocutor="bob"))
        assert client.digest("t1b-y") != snap["digest"]
        mem_y.close()
        assert client.restore("t1b-y", snap["snapshot_id"]) == snap["digest"]
        assert client.digest("t1b-y") == snap["digest"]
        assert thread.is_alive() and fake_openai.state()["held"] == 1     # 整段都在 X 写入途中
    finally:
        fake_openai.release()
    x_results = _join(thread, box)
    fake_openai.configure(hold_keys=[])
    for result in x_results:
        _remember_ok(result)

    mem_xc = client.open("t1b-xc", holder=HOLDER)
    xc_results = [mem_xc.remember_event(key, text, interlocutor="bob") for key, text in _X_EVENTS]
    for result in xc_results:
        _remember_ok(result)
    assert mem_x.digest() == mem_xc.digest()
    x_ids = [sid for result in x_results for sid in result.statement_ids]
    xc_ids = [sid for result in xc_results for sid in result.statement_ids]
    assert len(x_ids) == len(_X_EVENTS)
    x_content = [mem_x.content_ids(x_ids)[sid] for sid in x_ids]
    assert "missing" not in x_content
    assert x_content == [mem_xc.content_ids(xc_ids)[sid] for sid in xc_ids]   # X 的语句逐条同对照

    mem_y2 = client.open("t1b-y", holder=HOLDER)
    with pytest.raises(client_errors.WorldError) as held:
        client.restore("t1b-y", snap["snapshot_id"])
    assert held.value.code == "world_held"
    for mem in (mem_x, mem_xc, mem_y2):
        mem.close()


# ───────────────────────── 第 6 条：门面行为一致 ─────────────────────────

# 进程内 Memory 的异常没有 code：按 spec §13.2 的对应关系写成表（测试自己的，不借服务端的映射函数，否则那个函数映射错了
# 两边照样相等）。
_LOCAL_CODES = {"EventConflict": "event_conflict", "RecallDegradedError": "recall_degraded",
                "MemoryClosed": "handle_closed", "KeyError": "event_not_found"}


def _call(fn):
    """调 fn()，返回 ("ok", 值) 或 ("raise", 异常类名, code)。客户端异常自带 code；进程内的按 _LOCAL_CODES 取。"""
    try:
        return ("ok", fn())
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "code", None)
        if code is None:
            code = _LOCAL_CODES.get(type(exc).__name__)
        return ("raise", type(exc).__name__, code)


def _facade_steps(mem, fake: FakeOpenAI) -> list:
    """同一组调用，对 Memory 与 RemoteMemory 一字不差地跑。结果规范化：statement_ids 换成 content_ids（UUID 两边不同），
    engram_ref 去掉（engram UUID），其余字段原样。"""
    def norm(result):
        fields = dict(vars(result))
        ids = fields.pop("statement_ids")
        fields.pop("engram_ref")
        mapping = mem.content_ids(ids) if ids else {}
        fields["content_ids"] = [mapping[sid] for sid in ids]
        return fields

    def remember(key, text):
        return norm(mem.remember_event(key, text, interlocutor="bob", epoch=EPOCH))

    record = []
    text1 = _text("bob", "Bob owns the auth module")
    first = _call(lambda: remember("e1", text1))
    assert first[0] == "ok" and first[1]["outcome"] in ("accepted", "idempotent"), first
    assert first[1]["extraction_failed"] is False and len(first[1]["content_ids"]) == 1, first
    record.append(("e1 first", first))

    # 1 同 key 同文本重入：结果相同，假端点的 chat_calls 不增加。
    before = fake.state()["chat_calls"]
    again = _call(lambda: remember("e1", text1))
    assert fake.state()["chat_calls"] == before
    assert again[0] == "ok" and again[1]["content_ids"] == first[1]["content_ids"], again
    record.append(("e1 replay", again))

    # 2 同 key 异文本：EventConflict。
    record.append(("e1 other text", _call(lambda: remember("e1", _text("bob", "something else")))))

    # 3 abandon 之后再写：EventConflict。先让抽取回 500，事件留在 pending，才能 abandon。
    text3 = _text("bob", "Bob owns the billing module")
    fake.configure(fail_keys=["e3"])
    record.append(("e3 failed extraction", _call(lambda: remember("e3", text3))))
    fake.configure(fail_keys=[])
    record.append(("e3 pending", [(e.key, e.seq, e.failure_category, e.holder)
                                  for e in mem.pending_events()]))
    record.append(("e3 abandon", _call(lambda: mem.abandon_event("e3"))))
    record.append(("e3 after abandon", _call(lambda: remember("e3", text3))))

    # 嵌入后一次正常召回（两边的向量来自同一个假端点）。
    stats = mem.embed_pending(NOW)
    record.append(("embed", (stats.embedded, stats.failed)))
    record.append(("recall", [(hit["content_id"], round(hit["score"], 6))
                              for hit in mem.recall("Bob auth", k=10, strict=True,
                                                    include_volatile=True)]))

    # 4 嵌入失败时 strict 召回：RecallDegradedError。
    fake.configure(fail_embeddings=True)
    record.append(("strict recall degraded",
                   _call(lambda: mem.recall("Bob auth", k=10, strict=True, include_volatile=True))))
    fake.configure(fail_embeddings=False)

    # 5 close 超时：一次抽取被假端点挂住，close(timeout=0.2) 抛 TimeoutError；放行之后的调用照常成功（句柄没被标成已关）。
    fake.configure(hold_keys=["e5"])
    thread, box = _run_in_thread(lambda: remember("e5", _text("bob", "Bob owns the infra module")))
    try:
        fake.wait_until(lambda state: state["held"] >= 1)
        record.append(("close timeout", _call(lambda: mem.close(timeout=0.2))))
    finally:
        fake.release()
    record.append(("e5 held write", ("ok", _join(thread, box))))
    fake.configure(hold_keys=[])
    record.append(("next_seq after close timeout", _call(mem.next_seq)))
    record.append(("pending", [(e.key, e.text, e.interlocutor, e.seq, e.failure_category, e.holder)
                               for e in mem.pending_events()]))
    return record


def test_6_facade_parity(tmp_path, client, fake_openai):
    """同一组调用分别走进程内 Memory 与客户端（同一个 holder、一个新世界，两边都指向同一个假端点）：规范化之后逐项相等，
    最后两边摘要相等（R76：来源前缀 dash- 与 mem- 不进摘要）。"""
    with _openai_key(fake_openai.api_key):
        llm = starling.make_openai_llm(model="fake-extract", base_url=fake_openai.base_url,
                                       timeout_ms=_EXTRACT_TIMEOUT_MS, max_retries=0)
        embedder = starling.make_openai_embedder(model="fake-embed", base_url=fake_openai.base_url,
                                                 dim=DIM, timeout_ms=60_000, max_retries=0)
    local = starling.Memory.open(tmp_path / "local.db", agent=HOLDER, llm=llm, embedder=embedder)
    try:
        local_record = _facade_steps(local, fake_openai)
        local_digest = local.digest()
    finally:
        local.close()

    fake_openai.reset()
    client.create_world("t6-remote")
    remote = client.open("t6-remote", holder=HOLDER)
    try:
        remote_record = _facade_steps(remote, fake_openai)
        remote_digest = remote.digest()
    finally:
        remote.close()

    assert [name for name, _ in remote_record] == [name for name, _ in local_record]
    for (name, local_value), (_, remote_value) in zip(local_record, remote_record):
        assert remote_value == local_value, name
    expected = {
        "e1 other text": ("raise", "EventConflict", "event_conflict"),
        "e3 after abandon": ("raise", "EventConflict", "event_conflict"),
        "strict recall degraded": ("raise", "RecallDegradedError", "recall_degraded"),
        "close timeout": ("raise", "TimeoutError", None),
        "e3 abandon": ("ok", None),
    }
    record = dict(local_record)
    for name, value in expected.items():
        assert record[name] == value, name
    assert record["e3 failed extraction"][1]["extraction_failed"] is True
    assert record["e3 pending"] == [("e3", 1, "transport_failure", HOLDER)]
    assert record["recall"], "a working recall finds the statement"
    held_write = record["e5 held write"][1]
    assert held_write["outcome"] in ("accepted", "idempotent") and held_write["extraction_failed"] is False
    assert record["next_seq after close timeout"] == ("ok", 3)       # 句柄没被标成已关；e1、e3、e5 各占一个 seq
    assert record["pending"] == []
    assert remote_digest == local_digest


# ───────────────────────── 第 7 条：停止与重启 ─────────────────────────

def test_7_sigterm_restart_and_second_server(own_services, tmp_path, fake_openai):
    """通过客户端写 3 条；服务开着时在同一个 D 上起第二个实例：退出码 3，stderr 有 data_dir_in_use。SIGTERM：30 秒内退出码 0。
    同一个 D 重启：3 条都在（next_seq() == 3，pending_events() 为空，content_ids 没有 "missing"，摘要等于停之前的）。"""
    data_dir = tmp_path / "D"
    _write_config(data_dir, fake_openai)
    first = own_services(data_dir).start_and_wait()
    statement_ids = []
    with _connected(first.url, first.token) as cli:
        cli.create_world("t7-w")
        mem = cli.open("t7-w", holder=HOLDER)
        for i, utterance in enumerate(("Bob owns auth", "Bob owns billing", "Bob owns infra")):
            result = mem.remember_event(f"t7#{i}", _text("bob", utterance), interlocutor="bob")
            _remember_ok(result)
            statement_ids += result.statement_ids
        assert len(statement_ids) == 3
        digest_before = mem.digest()

        second = own_services(data_dir)
        assert second.proc.wait(30) == 3
        assert "data_dir_in_use" in second.stderr()
        mem.close()

    started = time.monotonic()
    assert first.terminate(timeout_s=30.0) == 0
    assert time.monotonic() - started < 30.0

    again = own_services(data_dir).start_and_wait()
    with _connected(again.url, again.token) as cli:
        mem = cli.open("t7-w", holder=HOLDER)
        assert mem.next_seq() == 3
        assert mem.pending_events() == []
        mapping = mem.content_ids(statement_ids)
        assert set(mapping) == set(statement_ids)
        assert "missing" not in mapping.values()
        assert mem.digest() == digest_before
        mem.close()


# ───────────────────────── 第 8 条：版本 ─────────────────────────

def test_8_info_commit_equals_install_commit(client):
    """info().commit 等于安装进来的 _core 编进去的提交（R88）：40 位小写十六进制。不是从 git checkout 构建的（"unknown"）时跳过。"""
    commit = _core.build_commit()
    if commit == "unknown":
        pytest.skip("_core was not built from a git checkout (build_commit() == 'unknown')")
    info = client.info()
    assert info["commit"] == commit
    assert re.fullmatch(r"[0-9a-f]{40}", info["commit"])


# ───────────────────────── §12：跨世界不阻塞 ─────────────────────────

def test_cross_world_gil(client, fake_openai):
    """世界 A 的抽取在假端点阻塞 700 ms（chat_delay_s），同时世界 B 的召回在 200 ms 内返回。B 先打开、预热一次召回；
    等假端点报告有抽取请求正在处理，再给 B 的 recall 计时。嵌入不加延迟。"""
    client.create_world("tgil-a")
    client.create_world("tgil-b")
    mem_b = client.open("tgil-b", holder=HOLDER)
    _remember_ok(mem_b.remember_event("b#0", _text("bob", "Bob keeps the release notes"),
                                      interlocutor="bob"))
    mem_b.embed_pending(NOW)
    assert mem_b.recall("release notes", k=5)                       # 预热
    mem_a = client.open("tgil-a", holder=HOLDER)

    fake_openai.configure(chat_delay_s=0.7)
    thread, box = _run_in_thread(lambda: mem_a.remember_event("a#0", _text("bob", "Bob drafts the plan"),
                                                              interlocutor="bob"))
    fake_openai.wait_until(lambda state: state["chat_in_flight"] >= 1)
    started = time.monotonic()
    hits = mem_b.recall("release notes", k=5)
    elapsed = time.monotonic() - started
    a_still_extracting = thread.is_alive()
    print(f"world B recall: {elapsed * 1000:.1f} ms while world A was extracting")
    result = _join(thread, box)
    assert hits
    assert a_still_extracting                                      # 计时的召回确实落在 A 的阻塞里
    assert elapsed < 0.2, f"world B recall took {elapsed * 1000:.0f} ms while world A was extracting"
    _remember_ok(result)
    mem_a.close()
    mem_b.close()


# ───────────────────────── 日志与错误体里没有地址和密钥 ─────────────────────────

def _exception_texts(exc: BaseException) -> list[str]:
    texts = [str(exc), repr(exc), repr(exc.__cause__), repr(exc.__context__),
             "".join(traceback.format_exception(exc))]
    for linked in (exc.__cause__, exc.__context__):
        if linked is not None:
            texts += [str(linked), "".join(traceback.format_exception(linked))]
    return texts


def test_logs_and_errors_have_no_address_or_key(service, client, fake_openai):
    """主动触发几类错误：抽取 500（fail_keys）、嵌入失败的 strict 召回、world_not_found、handle_closed，外加 invalid_request
    与没带 token 的 401。收集所有响应体（直接发 HTTP）与客户端异常的 str / repr / __cause__ / __context__ / 回溯，再读服务的
    完整 stderr（与写访问日志的 stdout）：里面都没有假端点的 127.0.0.1:<port> 与随机 key。"""
    secrets_ = (fake_openai.address, fake_openai.api_key)
    texts: list[str] = []
    bodies: list[httpx.Response] = []
    client.create_world("tlog-w")
    fake_openai.configure(fail_keys=["log#0"])

    # 直接发 HTTP，拿原始响应体。
    # 直接发 HTTP 的这两个 httpx.Client 是测试自己的工具，不是被测的客户端：trust_env=False，不经这台机器的系统代理。
    with httpx.Client(base_url=service.url, timeout=30.0, trust_env=False,
                      headers={"Authorization": f"Bearer {service.token}"}) as raw:
        session = raw.post(f"{API}/connect", json={"protocol": 1}).json()["session_id"]
        bodies.append(raw.post(f"{API}/open", json={"session_id": session, "world": "tlog-missing",
                                                    "holder": HOLDER}))
        handle = raw.post(f"{API}/open", json={"session_id": session, "world": "tlog-w",
                                               "holder": HOLDER}).json()["handle_id"]
        bodies.append(raw.post(f"{API}/call", json={
            "handle_id": handle, "method": "remember_event",
            "args": {"key": "log#0", "text": _text("bob", "Bob owns the log"), "interlocutor": "bob"}}))
        bodies.append(raw.post(f"{API}/call", json={"handle_id": handle, "method": "pending_events",
                                                    "args": {}}))
        fake_openai.configure(fail_embeddings=True)
        bodies.append(raw.post(f"{API}/call", json={"handle_id": handle, "method": "recall",
                                                    "args": {"query": "Bob log"}}))
        fake_openai.configure(fail_embeddings=False)
        bodies.append(raw.post(f"{API}/call", json={"handle_id": handle, "method": "nope", "args": {}}))
        bodies.append(raw.post(f"{API}/info", json={}))
        assert raw.post(f"{API}/close", json={"handle_id": handle}).status_code == 200
        bodies.append(raw.post(f"{API}/call", json={"handle_id": handle, "method": "next_seq",
                                                    "args": {}}))
        raw.post(f"{API}/disconnect", json={"session_id": session})
    with httpx.Client(base_url=service.url, timeout=30.0, trust_env=False) as anon:
        bodies.append(anon.post(f"{API}/connect", json={"protocol": 1}))
    codes = {resp.json().get("code") for resp in bodies if resp.status_code >= 400}
    assert {"world_not_found", "recall_degraded", "handle_closed", "invalid_request"} <= codes, codes
    failed = bodies[1].json()["result"]
    assert failed["extraction_failed"] is True and failed["failure_category"] == "transport_failure"
    texts += [resp.text for resp in bodies]

    # 经客户端：同样几类，收集异常的全部文本。
    fake_openai.configure(fail_keys=["log#1"])
    mem = client.open("tlog-w", holder=HOLDER)
    result = mem.remember_event("log#1", _text("bob", "Bob owns the second log"), interlocutor="bob")
    assert result.extraction_failed is True
    texts.append(repr(result))
    raised = []
    with pytest.raises(client_errors.WorldError) as missing:
        client.open("tlog-missing", holder=HOLDER)
    raised.append(missing.value)
    fake_openai.configure(fail_embeddings=True)
    with pytest.raises(client_errors.RecallDegradedError) as degraded:
        mem.recall("Bob log")
    raised.append(degraded.value)
    fake_openai.configure(fail_embeddings=False)
    mem.close()
    with pytest.raises(client_errors.MemoryClosed) as closed:
        mem.next_seq()
    raised.append(closed.value)
    for exc in raised:
        texts += _exception_texts(exc)

    # 服务的完整日志：stderr（应用日志、uvicorn 启停）与 stdout（uvicorn 的访问日志默认写 stdout）。
    stdout = "".join(service.stdout)
    assert "POST /api/v1/client/call" in stdout                       # 确实读到了这些请求的日志
    texts += [service.stderr(), stdout]
    for text in texts:
        for secret in secrets_:
            assert secret not in text, f"{secret!r} leaked into: {text[:500]!r}"
    assert fake_openai.state()["auth_failures"] == 0                  # 服务确实带着这个 key 调了假端点
