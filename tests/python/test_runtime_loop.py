"""P2.o 运行时闭环回归 — 「写进去的记忆,无人工干预地变得可召回」。

被钉的根因(2026-06-12 实测):
1. 写后泵(投影/信念/再巩固/在线回放/策略)生产宿主缺失——泵只挂在
   Bus::write 尾部,而生产语句写经 StatementWriter,泵永不运行;
2. StatementWriter 出生 salience 硬编码 0.0,采样权重恒 0,Replay 永远
   采不到生产语句,volatile→consolidated 晋升被锁死;
3. tick 不含回放/投影/出箱,dashboard 无自动周期。

闭环 = remember(在线泵)→ tick(嵌入+idle 巩固+投影+出箱)→ recall 命中。
全部用 FakeLLMAdapter + 默认 stub embedder,零网络。
"""
import sqlite3
import time

import starling
from starling import _core
from starling.dashboard.config import DashboardConfig
from starling.dashboard.engine import DashboardEngine

CANNED_JSON = (
    '[{"holder":"self","holder_perspective":"FIRST_PERSON",'
    '"subject":"Bob","predicate":"responsible_for","object":"auth",'
    '"modality":"BELIEVES","polarity":"POS","nesting_depth":0}]'
)


def _states(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT consolidation_state, COUNT(*) FROM statements GROUP BY 1"
        ).fetchall()
        return dict(rows)
    finally:
        conn.close()


def test_remember_tick_recall_closes_loop(tmp_path):
    db = str(tmp_path / "loop.db")
    llm = starling.make_stub_llm(default_response=CANNED_JSON)
    mem = starling.Memory.open(db, agent="alice", llm=llm)
    try:
        assert mem.remember("Bob owns the auth module").outcome == "accepted"
        # 写入后语句尚未巩固(在线窗口要第 3 次写才触发)。
        assert _states(db).get("volatile", 0) == 1

        t = mem.tick()
        assert t.embedded >= 1
        assert t.consolidated >= 1       # idle 批扫掉积压 volatile
        assert t.dispatched >= 1         # 出箱 pending→delivered 收敛
        assert _states(db).get("volatile", 0) == 0

        # 闭环验收:巩固 + 嵌入之后,语义召回必须命中。
        assert len(mem.recall("who owns auth")) >= 1
    finally:
        mem.close()


def test_online_pump_consolidates_every_third_remember(tmp_path):
    db = str(tmp_path / "pump.db")
    llm = starling.make_stub_llm(default_response=CANNED_JSON)
    mem = starling.Memory.open(db, agent="alice", llm=llm)
    try:
        mem.remember("text one")
        mem.remember("text two")
        mem.remember("text three")
        # 第 3 次写触发在线采样窗(kOnlineTrigger=3),无 tick 也有巩固。
        assert _states(db).get("consolidated", 0) >= 1
    finally:
        mem.close()


def test_background_tick_scheduler_consolidates(tmp_path):
    cfg = DashboardConfig(db_path=str(tmp_path / "bg.db"), token="t")
    eng = DashboardEngine(cfg)
    fake = _core.FakeLLMAdapter()
    fake.set_default_response(CANNED_JSON, True, "")
    eng.llm = fake
    try:
        assert eng.remember("Bob owns auth")["outcome"] == "accepted"
        assert _states(cfg.db_path).get("volatile", 0) == 1

        ticked = []
        eng.start_background_tick(0.05, ticked.append)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if _states(cfg.db_path).get("volatile", 1) == 0:
                break
            time.sleep(0.05)
        assert _states(cfg.db_path).get("volatile", 0) == 0, \
            "后台调度线程在 5s 内未完成巩固"
        # on_tick 仅在有实际变化的轮次回调,且至少回调过一次。
        assert ticked and ticked[0]["consolidated"] >= 1
    finally:
        eng.close()
    assert eng._tick_thread is None  # close() 收掉调度线程


def test_lifespan_starts_and_stops_scheduler(tmp_path):
    from fastapi.testclient import TestClient
    from starling.dashboard import create_app

    cfg = DashboardConfig(db_path=str(tmp_path / "ls.db"), token="",
                          tick_interval_s=600.0)  # 不会在测试窗口内触发
    app = create_app(cfg)   # engine=None:lifespan 应即建默认世界的引擎并启动调度
    pool = app.state.pool
    with TestClient(app) as _:
        assert pool.open_worlds() == ["default"]
        with pool.lease("default", build=False) as eng:   # 只看,不建
            assert eng is not None
            tick = eng._tick_thread
        eng = None
        assert tick is not None and tick.is_alive()
    assert not tick.is_alive()                 # shutdown 收线程
    assert pool.open_worlds() == []            # 并关掉默认世界(释放 C++ 写连接)


def test_scheduler_disabled_when_interval_zero(tmp_path):
    cfg = DashboardConfig(db_path=str(tmp_path / "off.db"), token="t",
                          tick_interval_s=0)
    eng = DashboardEngine(cfg)
    try:
        eng.start_background_tick(cfg.tick_interval_s)
        assert eng._tick_thread is None
    finally:
        eng.close()
