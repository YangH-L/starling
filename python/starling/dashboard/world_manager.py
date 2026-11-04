"""引擎池：每个世界一个 DashboardEngine，按世界发租约、关闭、恢复（记忆服务第 1 期 Task 5）。

注册表、设置合并、快照、保留策略、租户不变式、回滚的顺序与原子性、自动快照「拍不拍」都在 C++
（`_core.WorldRegistry`、`_core.world_*`）；这里只做 Python 才能做的事：按世界建引擎、停线程、
等在途请求、释放 C++ 写连接、恢复前后丢引擎与重开。没有「活动世界」：任何世界都可以同时开着。
设计：docs/superpowers/specs/2026-11-03-memory-service-design.md §5.5、§7.2、§8、§10.3
（旧的单世界切换见 docs/superpowers/specs/2026-10-25-world-scene-management-design.md §8）。

引擎租约（lease）
-----------------
每个用到某个世界的请求在整个生命周期里持该世界的一份租约：线程池排队、remember 的锁外抽取、
/ws/converse 的锁外生成、最后的提交，全在租约之内。关闭、删除、恢复一个世界先置该世界的
「正在关闭」标志（此后它的新租约一律拒绝，WorldSwitchError("world_busy")），再等它的在途租约
归零，然后才丢引擎。只等引擎锁是不够的：锁外阶段不持引擎锁，请求随后还会写进被丢掉的引擎，
旧 Runtime 也一直活着，重开同一个世界就是同一个库上两条写连接。别的世界的租约不受影响：
恢复与关闭只停被操作的世界（spec §7.2）。

租约按请求发放，不按 WebSocket 连接：长连接的对话 socket 在两轮之间不持租约，不挡关闭。
后台 tick / 摄入线程不是租约，关闭时用 quiesce 停它们（不持引擎锁 join）。

本进程里打开某个世界库文件的每条路径，从打开之前到关闭之后都持该世界的一份租约（只读的也一样）：
检视 SQL 路由用 lease(world, build=False)（不建引擎，租约照样计数）。恢复把同一路径上的 inode 换掉，
一条在替换之前打开了旧 inode、替换之后才第一次读的只读连接会按路径打开新库的 -shm，在同进程里认为
自己是第一个连接，把引擎正在用的 wal-index 截断重建。C++ 的连接探测（world_in_use）看不见这种
「打开了还没读」的连接，在 delete 模式的库上连读过库的闲置连接也看不见，它只是纵深防御，所以要靠
租约：恢复等该世界的租约归零才动文件。live_stats、list_sessions 与 30 秒统计刷新同样持一份
build=False 的租约读库。

释放 C++ 写连接
---------------
DashboardEngine.close() 不释放 C++ SQLite 句柄，只有丢掉最后一个引用并 gc.collect() 才会。所以关闭
一个世界是：停线程 → 等租约归零 → 从表里摘掉引擎、丢掉本帧的引用 → gc，并用 Runtime 的弱引用确认
它已被回收（_RUNTIME_RELEASE_GRACE_S 内重试：刚返回的栈帧、异常回溯可能还短暂引用着它）。确认不了
就记 warning，把 (库路径, 弱引用) 记进 _leaked：这个 Runtime 还开着那个库的写连接。只要它还活着：
  - 该库的世界不能再建引擎（lease 回 world_busy）：同一个库文件上不能有两条写连接；
  - 该库不能被恢复替换（rollback 回 world_busy，库未动）：那条连接会继续写旧 inode 和它已被 unlink
    的 WAL，这些写入在恢复出来的库里没有，静默丢失，归档也可能被改动；
  - 该世界不能被删除（delete_world 回 world_busy）：进程仍在往已 unlink 的 inode 里写。
每次检查前先 gc 并摘掉已回收的条目，所以 Runtime 一旦被回收，这些操作就照常进行。

锁顺序（全局唯一，任何代码都不得反向持有）
------------------------------------------
  1. pool._lock（Lock，短）：只保护 _engines / _leases / _closing 三张表，以及只在表上做的簿记
     （_leaked、_world_locks、去抖状态、自动快照失败计数、后台线程句柄）。持有期间不做任何 I/O、
     不建引擎、不拿别的锁（起去抖计时器的 Timer.start 只等线程起来）。_drained 是建在它上面的
     Condition：等租约归零时 wait 放开 _lock，所以「等租约」从不持着 _lock。
  2. pool._world_locks[name]（每个世界一把 RLock）：建引擎、关引擎、快照、恢复、删除、自动快照、
     清理归档。可能被占几秒到几十秒（VACUUM、复制快照、等租约），所以事件循环上的代码从不拿它。
     C++ 要求按世界串行化快照、恢复、修剪，持这把锁即满足。不同世界的世界锁互不相干：一个世界建引擎
     或恢复时，别的世界照常打开、读写。后台调用方（会话钩子、调度线程）只非阻塞地取，取不到就是
     "busy"，从不等。
  3. engine._lock（RLock，DashboardEngine 内部）：由持租约的请求和后台线程持有。池从不拿它：quiesce
     在不持引擎锁的情况下 join 后台线程（线程分段拿锁，持锁 join 会卡满 5 秒误报 world_busy）。
  4. supervisor 互斥锁（C++ Runtime 内部）：只在持 engine._lock 时取（见 engine.py 的
     _sample_backpressure 一带），从不反向。

  即 pool._lock → pool._world_locks[name] → engine._lock → supervisor。实际代码里 1 在 2 之内只短暂取
  （查表、改表），从不在持 1 时去取 2。
  持租约的代码不得阻塞地等 _world_locks：关闭、恢复持着世界锁等租约归零，反过来就是死锁。首次建引擎
  是唯一要在持租约时取世界锁的地方（lease 先登记租约、放开 _lock，再取世界锁建引擎），所以它不阻塞地
  等：_acquire_for_build 以 50 毫秒为一段轮询，每段之间查该世界的关闭标志，标志已置就放弃
  （world_busy），租约随之归还，关闭的等待随即结束。关闭先取世界锁、再置标志、再等租约，所以构建的
  租约要么在关闭取世界锁之前已经建完（关闭等它用完），要么在轮询里看到标志后放弃，不会互等。

自动快照（W4）
--------------
两种触发共用 _auto_snapshot，要不要拍、拍什么全由 C++ take_auto_snapshot 判，这里只取锁、记失败。
按时间：调度线程 starling-auto-snapshot 每轮遍历注册表里的全部世界（开着的、关着的都算），逐个在该
世界的世界锁内调 C++（裁定 R19）。按会话：钩子挂在每个引擎上（attach_hooks），摄入线程在写入之前调用，
不持 engine._lock，所以没有 engine._lock → 世界锁的反向边。两者都只非阻塞取世界锁，取不到就是 "busy"：
钩子抛 SessionSnapshotDeferred 让摄入 job 放回 spool，调度线程等下一轮。失败只计数、记日志，不往外抛
（用户裁定：照常摄入）。

统计缓存（spec §5.5）
---------------------
提交时不写 registry.db。开着的世界的计数由 live_stats 从库里现读（C++ world_stats，持一份租约）；关闭时
（close_world、close_all）与 30 秒的后台刷新把它写进注册表的缓存列。引擎的 on_commit 由池设置：每个世界
每秒至多通知一次 on_world_changed（尾沿去抖：一秒内的一串写入，最后一次通知带最终计数）。

本期不做：没有空闲自动关闭（C++ 的空闲判定在第 4 期，裁定 R21）。世界开着直到 close_world、
delete_world 或关停（close_all），默认世界只在关停时关。
"""
from __future__ import annotations

import gc
import logging
import os
import threading
import time
import traceback
import weakref
from datetime import datetime, timezone
from typing import Callable

from starling import _core
from starling.dashboard.engine import (DashboardEngine, SessionSnapshotDeferred,
                                       WorldContext)

logger = logging.getLogger(__name__)

__all__ = ["EngineFactory", "EngineLease", "WorldContext", "WorldPool", "WorldSwitchError",
           "engine_lease"]

# 关闭、恢复等在途请求（租约）归零的默认上限。超时：清标志、恢复后台线程、回 world_busy。
_LEASE_DRAIN_TIMEOUT_S = 30.0
# 丢掉引擎后等旧 Runtime 被回收的宽限：异常回溯、刚返回的栈帧可能还短暂引用着它。
_RUNTIME_RELEASE_GRACE_S = 1.0
# on_world_changed 的去抖窗口（秒）：每个世界每秒至多一次。
_CHANGE_DEBOUNCE_S = 1.0
# 统计缓存的后台刷新间隔（spec §5.5）。
_STATS_REFRESH_S = 30.0
_STATS_THREAD = "starling-world-stats"
# 调度线程：首轮之前的等待与每轮间隔（秒）。按时间的间隔以小时计，10 分钟一轮足够；首轮推迟
# 一分钟，不与启动时的首屏请求、tick 抢 VACUUM 的 I/O。
_SCHEDULER_POLL_S = 600.0
_SCHEDULER_FIRST_DELAY_S = 60.0
_SCHEDULER_THREAD = "starling-auto-snapshot"
# 默认世界在没有注册表（:memory: 等）时的名字。
_DEFAULT_NAME = "default"
# 注册表 list 的分页上限（RegistryDb::list 的 limit 1–500）。
_LIST_PAGE = 500

EngineFactory = Callable[[WorldContext, dict], DashboardEngine]


def _default_factory(ctx: WorldContext, providers: dict) -> DashboardEngine:
    return DashboardEngine(ctx, providers)


class WorldSwitchError(Exception):
    """code: "world_not_found" | "world_busy" | "world_protected" | "worlds_unavailable"
    | "world_open_failed" | "rollback_reopen_failed" """

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _now_iso() -> str:
    """秒精度 UTC（YYYY-MM-DDTHH:MM:SSZ）。C++ 的 now_iso 只认这个形状：engine.py 与
    routes/commands.py 里 isoformat 的版本带微秒，交给自动快照会一律 invalid_auto_snapshot。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _same_db(left: str, right: str) -> bool:
    """两个库路径是不是同一个文件（按真实路径比：符号链接目录、相对路径）。"""
    return os.path.realpath(left) == os.path.realpath(right)


def _store_code(exc: BaseException) -> str | None:
    return _core.world_store_error_code(exc) if isinstance(exc, _core.WorldStoreError) else None


class EngineLease:
    """一个世界的一份引擎租约。`with lease as engine:` 或手动 `release()`；release 幂等。

    release 先丢掉自己对引擎的引用、再归还计数：计数一归零，关闭就会丢引擎并断言旧 Runtime 已回收，
    租约对象本身（可能还挂在某个栈帧或异常回溯上）不能再钉住它。调用方自己的局部变量要在 release
    之前清掉（见 /ws/converse、/api/config）。build=False 且世界没开着时 engine 为 None。"""

    __slots__ = ("_owner", "world", "engine")

    def __init__(self, owner, world: str, engine) -> None:
        self._owner = owner          # WorldPool
        self.world = world
        self.engine = engine

    def release(self) -> None:
        owner, self._owner = self._owner, None
        self.engine = None
        if owner is not None:
            owner._return_lease(self.world)

    def __enter__(self):
        return self.engine

    def __exit__(self, *_exc) -> None:
        self.release()


def engine_lease(state, world: str | None = None, *, build: bool = True) -> EngineLease:
    """路由取引擎的入口（Task 6 改写路由时用）：app.state.pool 上按世界发租约，world 为 None
    时取默认世界。"""
    pool = state.pool
    return pool.lease(world or pool.default_world(), build=build)


class _Debounce:
    """一个世界的去抖状态（只在 pool._lock 下读写）。timer 非 None = 窗口开着。"""

    __slots__ = ("pending", "timer", "last_fire")

    def __init__(self) -> None:
        self.pending = False
        self.timer: threading.Timer | None = None
        self.last_fire = float("-inf")


class _ClosingWindow:
    """置一个世界的关闭标志（此后它的新租约一律 world_busy），任何退出路径都清标志。调用方持该世界的
    世界锁，所以同一世界的关闭、删除、恢复互相串行，标志不会被别的窗口提前清掉。"""

    __slots__ = ("_pool", "_world")

    def __init__(self, pool: "WorldPool", world: str) -> None:
        self._pool, self._world = pool, world

    def __enter__(self) -> None:
        with self._pool._lock:
            self._pool._closing.add(self._world)

    def __exit__(self, *_exc) -> None:
        with self._pool._lock:
            self._pool._closing.discard(self._world)


class WorldPool:
    def __init__(self, registry, config, *, clock=None,
                 engine_factory: EngineFactory | None = None,
                 on_world_changed: Callable[[str, dict], None] | None = None) -> None:
        self._registry = registry                     # _core.WorldRegistry | None（裁定 R6）
        self._config = config
        self._clock = clock or _now_iso
        self._factory = engine_factory or _default_factory
        self.on_world_changed = on_world_changed
        # 后台 tick 的广播回调 on_tick(world, stats)。app 设置；引擎起 tick 线程时绑上世界名传进去。
        self.on_tick: Callable[[str, dict], None] | None = None
        self._lock = threading.Lock()
        self._drained = threading.Condition(self._lock)   # 某个世界的租约归零时 notify_all
        self._engines: dict[str, DashboardEngine] = {}
        self._leases: dict[str, int] = {}
        self._closing: set[str] = set()
        self._world_locks: dict[str, threading.RLock] = {}
        # 没能回收的旧 Runtime：[(它打开的库路径, 弱引用)]（见模块文档「释放 C++ 写连接」）。
        self._leaked: list[tuple[str, weakref.ref]] = []
        self._debounce: dict[str, _Debounce] = {}
        # 后台线程是否开着（start_background 之后）：此后新建的引擎按自己的设置起线程。
        self._background = False
        self._shut = False                            # close_all 之后为 True，不再发租约
        # 自动快照失败计数与最近一次错误（码与时刻），所有世界合计。
        self._auto_failures = 0
        self._auto_last_error: str | None = None
        self._auto_last_error_at: str | None = None
        self._threads: dict[str, tuple[threading.Thread, threading.Event]] = {}

    # ───────────────────────── 注册表与上下文 ─────────────────────────

    @property
    def registry(self):
        """_core.WorldRegistry；库路径没有父目录（:memory: 等）时为 None（裁定 R3、R6）。"""
        return self._registry

    def _require_registry(self):
        if self._registry is None:
            raise WorldSwitchError("worlds_unavailable")
        return self._registry

    def default_world(self) -> str:
        return self._registry.default_world() if self._registry is not None else _DEFAULT_NAME

    def _record(self, world: str) -> dict:
        """注册表记录；不存在 → WorldSwitchError("world_not_found")。无注册表时只有默认世界，记录由
        config 直接给出（裁定 R6）。"""
        if self._registry is None:
            if world != _DEFAULT_NAME:
                raise WorldSwitchError("world_not_found", world)
            return {"name": _DEFAULT_NAME, "dir_id": _DEFAULT_NAME,
                    "db_path": self._config.db_path, "tenant": self._config.tenant,
                    "settings": self._config.world_settings()}
        try:
            return self._registry.get(world)
        except _core.WorldStoreError as exc:
            if _store_code(exc) == "world_not_found":
                raise WorldSwitchError("world_not_found", world) from exc
            raise

    def context(self, world: str) -> WorldContext:
        """一个世界打开引擎需要的全部输入：库、租户、C++ 补齐默认值后的设置、摄入分区目录
        （<config.ingest_spool_root()>/<world_ingest_partition(...)>，裁定 R4）。"""
        rec = self._record(world)
        is_default = world == self.default_world()
        partition = _core.world_ingest_partition(is_default, rec["tenant"], rec["dir_id"])
        return WorldContext(
            name=world, dir_id=rec["dir_id"], db_path=rec["db_path"], tenant=rec["tenant"],
            agent=self._config.agent,
            settings=_core.world_effective_settings(rec["settings"]),
            ingest_spool_dir=str(self._config.ingest_spool_root() / partition),
            is_default=is_default)

    def _world_lock(self, world: str) -> threading.RLock:
        with self._lock:
            lock = self._world_locks.get(world)
            if lock is None:
                lock = self._world_locks[world] = threading.RLock()
            return lock

    # ───────────────────────── 租约 ─────────────────────────

    def lease(self, world: str, *, build: bool = True) -> EngineLease:
        """发 world 的一份租约（见模块文档）。

        world 不存在 → WorldSwitchError("world_not_found")；正在关闭、恢复、删除，或池已关停 →
        WorldSwitchError("world_busy")。build=False 且世界没开着时 lease.engine 为 None（只读检视
        路由直接只读打开库文件用）。不论 build 与否都计入该世界的租约数：恢复与关闭等它们归零。

        计数先于建引擎：关闭置标志后等租约归零，也就等到了正在进行的构建。建引擎在世界锁内、池锁
        之外（不同世界的首次打开互不排队）；拿到世界锁之后再查一次表，并发的两个首次打开只建一个
        引擎（Review Focus 1：同一库文件上两条写连接会互相截断 WAL）。"""
        self._record(world)                           # world_not_found（池锁之外读注册表）
        with self._lock:
            if self._shut or world in self._closing:
                raise WorldSwitchError("world_busy", world)
            self._leases[world] = self._leases.get(world, 0) + 1
            engine = self._engines.get(world)
        lease = EngineLease(self, world, None)
        try:
            if engine is None and build:
                engine = self._build(world)
            lease.engine = engine
        except BaseException:
            engine = None
            lease.release()
            raise
        return lease

    def _return_lease(self, world: str) -> None:
        with self._lock:
            left = self._leases.get(world, 0) - 1
            if left <= 0:
                self._leases.pop(world, None)
                self._drained.notify_all()
            else:
                self._leases[world] = left

    def _drain(self, world: str, timeout_s: float) -> None:
        """等 world 的在途租约归零（调用方已置该世界的关闭标志，不会再有新租约）。超时抛
        world_busy。Condition.wait 放开 _lock，不挡归还。"""
        deadline = time.monotonic() + timeout_s
        with self._lock:
            while self._leases.get(world, 0) > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorldSwitchError(
                        "world_busy", f"{self._leases.get(world, 0)} request(s) still in flight")
                self._drained.wait(remaining)

    def _acquire_for_build(self, world: str) -> threading.RLock:
        """持租约的首次建引擎取世界锁：关闭（持世界锁等这份租约）正在进行时不能阻塞地等它，那是互等。
        所以轮询：取得到就返回；取不到时该世界若在关闭窗口里，就 world_busy（构建作废，租约随之归还，
        关闭的等待随即结束）；否则（快照、自动快照等短操作持着锁）稍等再试。"""
        lock = self._world_lock(world)
        while not lock.acquire(timeout=0.05):
            with self._lock:
                closing = world in self._closing or self._shut
            if closing:
                raise WorldSwitchError("world_busy", world)
        return lock

    def _build(self, world: str) -> DashboardEngine:
        """持世界锁建引擎并发布（调用方持着 world 的一份租约，所以关闭等它）。"""
        lock = self._acquire_for_build(world)
        try:
            with self._lock:
                engine = self._engines.get(world)
                closing = world in self._closing or self._shut
            if engine is not None:
                return engine
            if closing:
                raise WorldSwitchError("world_busy", world)
            ctx = self.context(world)
            self._refuse_leaked(ctx.db_path)          # 旧 Runtime 还开着这个库：不开第二条写连接
            engine = self._open_engine(world, ctx)
            if self._background:
                self._start_workers(world, engine)
            with self._lock:
                self._engines[world] = engine
            return engine
        finally:
            lock.release()

    def _open_engine(self, world: str, ctx: WorldContext):
        """建引擎并挂上池的回调。失败抛 world_open_failed：清掉回溯里的栈帧，半建的 Runtime 不能被异常
        钉住（调用方可能留着这个异常，同时为同一个库再建一个引擎）。"""
        try:
            engine = self._factory(ctx, self._config.providers)
        except Exception as exc:
            traceback.clear_frames(exc.__traceback__)
            raise WorldSwitchError("world_open_failed", str(exc)) from exc
        engine.on_commit = lambda: self._note_change(world)
        if self._registry is not None:
            self.attach_hooks(world, engine)
        return engine

    # ───────────────────────── 只读查询 ─────────────────────────

    def open_worlds(self) -> list[str]:
        with self._lock:
            return sorted(self._engines)

    def closing_worlds(self) -> list[str]:
        with self._lock:
            return sorted(self._closing)

    def live_stats(self, world: str) -> dict | None:
        """开着的世界：{"holders", "statements", "last_write_at"}（C++ world_stats，持一份不建引擎的
        租约读库）；关着的返回 None（用注册表的缓存列）。正在关闭时同样 None。"""
        try:
            lease = self.lease(world, build=False)
        except WorldSwitchError as exc:
            if exc.code == "world_busy":
                return None
            raise
        with lease as engine:
            if engine is None:
                return None
            db_path, tenant = engine._db_path, engine.tenant
            engine = None
            stats = _core.world_stats(db_path, tenant)
        return {"holders": stats["holders"], "statements": stats["statements"],
                "last_write_at": stats["last_write_at"]}

    # ───────────────────────── 统计缓存与去抖广播 ─────────────────────────

    def _note_change(self, world: str) -> None:
        """引擎的 on_commit（写方法成功之后，引擎锁之外）：尾沿去抖，每个世界每秒至多一次。窗口没开着
        就立刻发并开一个窗口；窗口开着就记下 pending，窗口结束时由计时器再发一次（读那一刻的计数）。"""
        if self.on_world_changed is None:
            return
        with self._lock:
            deb = self._debounce.setdefault(world, _Debounce())
            if deb.timer is not None:
                deb.pending = True
                return
            deb.last_fire = time.monotonic()
            deb.timer = self._arm(world)
        self._emit_change(world)

    def _arm(self, world: str) -> threading.Timer:
        """持 _lock 调用：建一个 daemon 计时器并启动（Timer.start 只等线程起来，不碰我们的锁）。"""
        timer = threading.Timer(_CHANGE_DEBOUNCE_S, self._debounce_tick, args=(world,))
        timer.daemon = True
        timer.start()
        return timer

    def _debounce_tick(self, world: str) -> None:
        with self._lock:
            deb = self._debounce.get(world)
            if deb is None:
                return
            if not deb.pending:
                deb.timer = None                      # 窗口内没有新写入：关窗口
                return
            deb.pending = False
            deb.last_fire = time.monotonic()
            deb.timer = self._arm(world)              # 再开一个窗口吸收后续写入
        self._emit_change(world)

    def _emit_change(self, world: str) -> None:
        callback = self.on_world_changed
        if callback is None:
            return
        try:
            stats = self.live_stats(world)
            if stats is None:
                return
            callback(world, {"statements": stats["statements"],
                             "last_write_at": stats["last_write_at"]})
        except Exception:  # noqa: BLE001 — 通知失败不能连带写入或计时器线程
            logger.exception("on_world_changed failed for %s", world)

    def _cancel_debounce(self, world: str) -> None:
        with self._lock:
            deb = self._debounce.pop(world, None)
        if deb is not None and deb.timer is not None:
            deb.timer.cancel()

    def _write_stats_cache(self, world: str, db_path: str, tenant: str) -> None:
        """把库里的当前计数写进注册表缓存列。失败只记日志（缓存过期无害，下次刷新再写）。"""
        if self._registry is None:
            return
        try:
            stats = _core.world_stats(db_path, tenant)
            self._registry.update_stats(world, stats["holders"], stats["statements"],
                                        stats["last_write_at"], self._clock())
        except Exception:  # noqa: BLE001 — 统计缓存是尽力而为
            logger.warning("cannot write stats cache for %s", world, exc_info=True)

    def refresh_stats(self) -> None:
        """30 秒统计刷新的一轮：每个开着的世界，持一份不建引擎的租约读库、写缓存列。正在关闭的跳过
        （关闭自己会写）。"""
        for world in self.open_worlds():
            try:
                lease = self.lease(world, build=False)
            except WorldSwitchError:
                continue
            with lease as engine:
                if engine is None:
                    continue
                db_path, tenant = engine._db_path, engine.tenant
                engine = None
                self._write_stats_cache(world, db_path, tenant)

    # ───────────────────────── 设置 ─────────────────────────

    def sync_default_settings(self) -> None:
        """registry.update_settings(默认世界, config.world_settings())：启动时与每次 POST /api/config
        保存后调用。合并在 C++（不带 auto_snapshot，裁定 R16）。开着的默认世界引擎换上新合并的设置
        （endpoint 核对按新地址做）；适配器由保存配置的路由按改动的角色热换，与改之前一样。"""
        reg = self._require_registry()
        default = reg.default_world()
        merged = reg.update_settings(default, self._config.world_settings())
        with self._lock:
            engine = self._engines.get(default)
        if engine is not None:
            engine.set_world_settings(_core.world_effective_settings(merged))

    # ───────────────────────── 后台线程 ─────────────────────────

    def _start_workers(self, world: str, engine) -> None:
        """按该世界的 settings.background 起 tick / 摄入线程（钩子已在 _open_engine 挂好）。"""
        background = engine._settings["background"]
        interval = float(background["tick_interval_s"])
        if interval > 0:
            on_tick = self.on_tick
            engine.start_background_tick(
                interval, (lambda stats: on_tick(world, stats)) if on_tick is not None else None)
        if background["ingest"]:
            engine.start_ingest_worker()

    def start_background(self, *, stats_interval_s: float = _STATS_REFRESH_S,
                         scheduler_poll_s: float = _SCHEDULER_POLL_S,
                         scheduler_first_delay_s: float = _SCHEDULER_FIRST_DELAY_S) -> None:
        """每个开着的世界按自己的 settings.background 起 tick / 摄入；此后新建的引擎也起。起按时间的
        自动快照调度线程与 30 秒统计刷新线程（有注册表时）。已开着时是 no-op。"""
        with self._lock:
            if self._background or self._shut:
                return
            self._background = True
            worlds = list(self._engines)
        for world in worlds:
            with self._world_lock(world):             # 与关闭、恢复互斥：不给正在被丢的引擎起线程
                with self._lock:
                    engine = self._engines.get(world)
                if engine is not None:
                    self._start_workers(world, engine)
                engine = None
        if self._registry is not None:
            self._start_thread(_SCHEDULER_THREAD, self._scheduler_loop,
                               scheduler_poll_s, scheduler_first_delay_s)
            self._start_thread(_STATS_THREAD, self._stats_loop, stats_interval_s, stats_interval_s)

    def stop_background(self, timeout_s: float = 5.0) -> None:
        """停调度与统计线程（任何锁之外 join），再停每个开着的世界的 tick / 摄入线程。此后新建的引擎
        不起线程。"""
        with self._lock:
            self._background = False
            threads = list(self._threads.values())
            self._threads.clear()
            worlds = list(self._engines)
        for _thread, stop in threads:
            stop.set()
        for thread, _stop in threads:
            thread.join(timeout_s)
            if thread.is_alive():
                logger.warning("%s did not stop within %.1fs", thread.name, timeout_s)
        for world in worlds:
            with self._lock:
                engine = self._engines.get(world)
            if engine is not None and not engine.quiesce(timeout_s):
                logger.warning("background threads of world %s did not stop: %s",
                               world, "/".join(engine.quiesce_stuck))
            engine = None

    def _start_thread(self, name: str, target, poll_s: float, first_delay_s: float) -> None:
        stop = threading.Event()
        thread = threading.Thread(target=target, args=(stop, poll_s, first_delay_s),
                                  name=name, daemon=True)
        thread.start()
        with self._lock:
            self._threads[name] = (thread, stop)

    def _stats_loop(self, stop: threading.Event, poll_s: float, first_delay_s: float) -> None:
        if stop.wait(first_delay_s):
            return
        while True:
            try:
                self.refresh_stats()
            except Exception:  # noqa: BLE001 — 保活
                logger.exception("stats refresh round failed")
            if stop.wait(poll_s):
                return

    def _scheduler_loop(self, stop: threading.Event, poll_s: float, first_delay_s: float) -> None:
        if stop.wait(first_delay_s):
            return
        while True:
            try:
                self.auto_snapshot_round(stop)
            except Exception:  # noqa: BLE001 — 保活：单轮出错不终结调度
                logger.exception("auto snapshot scheduler round failed")
            if stop.wait(poll_s):
                return

    # ───────────────────────── 关闭 ─────────────────────────

    def close_world(self, world: str, timeout_s: float = _LEASE_DRAIN_TIMEOUT_S) -> None:
        """关一个世界：等该世界租约归零、停线程、丢引擎、写统计缓存（Review Focus 2）。之后的 lease 会重新
        建引擎。默认世界不在这里关（world_protected；只在关停时由 close_all 关）。没开着时什么都不做。
        超时或线程停不下 → world_busy，引擎原样留着。"""
        if world == self.default_world():
            raise WorldSwitchError("world_protected", "the default world closes only at shutdown")
        self._record(world)
        with self._world_lock(world), _ClosingWindow(self, world):
            self._close_engine(world, timeout_s)

    def close_all(self, timeout_s: float = _LEASE_DRAIN_TIMEOUT_S) -> None:
        """关停：此后 lease 一律 world_busy；停后台线程，逐个关闭全部世界（含默认世界）。幂等。"""
        with self._lock:
            self._shut = True
        self.stop_background()
        for world in self.open_worlds():
            try:
                with self._world_lock(world), _ClosingWindow(self, world):
                    self._close_engine(world, timeout_s)
            except Exception:  # noqa: BLE001 — 关停路径：一个世界关不掉不挡别的世界
                logger.exception("cannot close world %s at shutdown", world)
        with self._lock:
            worlds = list(self._debounce)
        for world in worlds:
            self._cancel_debounce(world)

    def _close_engine(self, world: str, timeout_s: float) -> bool:
        """关闭窗口内调用（持世界锁、标志已置）：停线程 → 等租约归零 → 再停一次线程 → 丢引擎、确认 Runtime
        已回收 → 写统计缓存。返回 Runtime 是否已回收（没开着时 True）。

        顺序是「先 quiesce，再等租约」：后台线程不是租约，quiesce 停它们，等租约的这段时间里就不会再有
        新的摄入 job 开始写。第二次 quiesce 针对此刻表上的引擎（建引擎在世界锁内，我们持着它，实际上就是
        同一个；无条件再停一次，正确性不靠这个推理，没有线程时 quiesce 直接返回 True）。

        超时或线程停不下：恢复后台线程（后台开着时）、引擎原样留在表上，抛 world_busy。"""
        # 每个 raise 之前先丢掉本帧对引擎的引用：异常的回溯持有本帧，调用方（路由、测试）留着异常时，
        # 本帧的 engine 局部变量会钉住这个引擎，下一次关闭就丢不掉它的 Runtime（同库第二条写连接）。
        with self._lock:
            engine = self._engines.get(world)
        if engine is not None and not engine.quiesce():
            busy = self._quiesce_failed(world, engine)
            engine = None
            raise busy
        try:
            self._drain(world, timeout_s)
        except WorldSwitchError as exc:
            if engine is not None:
                self._restart_after_failed_close(world, engine)
            engine = None
            raise exc from None
        with self._lock:
            engine = self._engines.get(world)
        if engine is None:
            self._cancel_debounce(world)
            return True
        if not engine.quiesce():
            busy = self._quiesce_failed(world, engine)
            engine = None
            raise busy
        db_path, tenant = engine._db_path, engine.tenant
        rt_ref = weakref.ref(engine._rt)
        engine.on_commit = None
        engine.session_snapshot_hook = None
        with self._lock:
            self._engines.pop(world, None)
        engine.close()
        engine = None
        self._cancel_debounce(world)
        released = self._release_runtime(rt_ref, db_path)
        self._write_stats_cache(world, db_path, tenant)
        return released

    def _quiesce_failed(self, world: str, engine) -> WorldSwitchError:
        """后台线程没停下：恢复线程、引擎原样留着，返回要抛的 world_busy（detail 点名卡住的线程）。"""
        stuck = "/".join(engine.quiesce_stuck) or "background"
        self._restart_after_failed_close(world, engine)
        return WorldSwitchError("world_busy", f"{stuck} thread did not stop in 5s")

    def _restart_after_failed_close(self, world: str, engine) -> None:
        if not self._background:
            return
        try:
            self._start_workers(world, engine)
        except Exception:  # noqa: BLE001 — 线程起不来不能盖掉 world_busy
            logger.exception("cannot restart background workers of world %s", world)

    def _release_runtime(self, rt_ref, db_path: str) -> bool:
        """丢掉引擎之后等旧 Runtime 被回收，返回是否已回收。宽限期过了仍活着就记 warning，并把
        (db_path, 弱引用) 记进 _leaked（见模块文档「释放 C++ 写连接」）。"""
        gc.collect()
        deadline = time.monotonic() + _RUNTIME_RELEASE_GRACE_S
        while rt_ref() is not None and time.monotonic() < deadline:
            time.sleep(0.02)
            gc.collect()
        if rt_ref() is None:
            return True
        logger.warning("runtime of %s still referenced after close; its write connection "
                       "stays open until it is collected", db_path)
        with self._lock:
            self._leaked = [(path, ref) for path, ref in self._leaked if ref() is not None]
            self._leaked.append((db_path, rt_ref))
        return False

    def _refuse_leaked(self, db_path: str) -> None:
        """之前的关闭留下的旧 Runtime 还开着 db_path 的写连接时抛 world_busy。先 gc 并摘掉已回收的条目。
        在动任何引擎或文件之前调用。"""
        with self._lock:
            if not self._leaked:
                return
        gc.collect()
        with self._lock:
            self._leaked = [(path, ref) for path, ref in self._leaked if ref() is not None]
            held = any(_same_db(path, db_path) for path, _ref in self._leaked)
        if held:
            raise WorldSwitchError(
                "world_busy", "an earlier close left a write connection open on this database")

    # ───────────────────────── 世界管理（Task 6 的路由调用这些） ─────────────────────────

    def create_world(self, name: str, label: str, *, labels: dict | None = None,
                     overrides: dict | None = None,
                     auto_snapshot: dict | None = None) -> dict:
        """建世界（C++ create_world：设置是默认世界当前设置的值拷贝，overrides 覆盖）。auto_snapshot
        {"on_session", "interval_hours"} 给出时放进 overrides 的 background.auto_snapshot（C++ 校验，
        invalid_auto_snapshot）。不打开引擎。返回注册表记录。"""
        reg = self._require_registry()
        merged = dict(overrides or {})
        if auto_snapshot is not None:
            background = dict(merged.get("background") or {})
            background["auto_snapshot"] = {"on_session": auto_snapshot["on_session"],
                                           "interval_hours": auto_snapshot["interval_hours"]}
            merged["background"] = background
        return reg.create(name, label, dict(labels or {}), merged or None, self._clock())

    def delete_world(self, world: str) -> None:
        """删世界：先关（等租约、停线程、丢引擎），旧 Runtime 回收不了就拒绝（world_busy，什么都不删），
        再由 C++ 删注册项与目录。默认世界由 C++ 拒绝（world_protected），引擎不动。全程持世界锁、处于
        关闭窗口：删的途中没有新租约能把它重新打开。"""
        reg = self._require_registry()
        if world == reg.default_world():
            reg.remove(world)                         # C++ 抛 world_protected
            return
        rec = self._record(world)
        with self._world_lock(world), _ClosingWindow(self, world):
            if not self._close_engine(world, _LEASE_DRAIN_TIMEOUT_S):
                raise WorldSwitchError(
                    "world_busy", "the closed world's runtime is still referenced")
            self._refuse_leaked(rec["db_path"])
            reg.remove(world)

    def take_snapshot(self, world: str, note: str) -> dict:
        """手动快照（C++ take_snapshot，作用于整个世界；vector_backend 取该世界的设置，是否支持由 C++ 判）。
        持世界锁：C++ 要求按世界串行化快照、恢复、修剪。"""
        reg = self._require_registry()
        backend = self._vector_backend(world)
        with self._world_lock(world):
            out = _core.world_take_snapshot(reg, world, "manual", "manual", note,
                                            vector_backend=backend)
        return out["snapshot"]

    def delete_snapshot(self, world: str, snapshot_id: str) -> None:
        reg = self._require_registry()
        self._record(world)
        with self._world_lock(world):
            _core.world_delete_snapshot(reg, world, snapshot_id)

    def snapshots(self, world: str) -> list[dict]:
        """该世界的快照条目，最新在前（注册表登记顺序）。"""
        reg = self._require_registry()
        self._record(world)
        return reg.snapshots(world)

    def list_sessions(self, world: str, limit: int = 200) -> dict:
        """会话清单（C++ 只读打开该世界的库）：持一份不建引擎的租约，恢复要等它归还。"""
        reg = self._require_registry()
        with self.lease(world, build=False):
            return _core.world_list_sessions(reg, world, limit)

    def archives(self, world: str) -> list[dict]:
        """回滚归档 [{"name", "size_bytes"}]，最新在前。只读，不开库，不持世界锁。"""
        reg = self._require_registry()
        self._record(world)
        return _core.world_list_archives(reg, world)

    def purge_archives(self, world: str) -> int:
        """删掉该世界的全部回滚归档，返回删掉的文件数。"""
        reg = self._require_registry()
        self._record(world)
        with self._world_lock(world):
            return _core.world_purge_archives(reg, world)

    def set_auto_snapshot(self, world: str, on_session: bool, interval_hours: int) -> dict:
        """改一个世界的自动快照配置（C++ 在注册表锁内合并），返回新配置 {"on_session", "interval_hours"}。
        world_not_found / invalid_auto_snapshot 由 C++ 抛。开着的引擎换上新的设置引用。"""
        reg = self._require_registry()
        _core.world_set_auto_snapshot(reg, world, on_session, interval_hours)
        settings = _core.world_effective_settings(reg.get(world)["settings"])
        with self._lock:
            engine = self._engines.get(world)
        if engine is not None:
            engine.set_world_settings(settings)
        return dict(settings["background"]["auto_snapshot"])

    def auto_snapshot_status(self) -> dict:
        """{"failures", "last_error", "last_error_at"}：自池建起以来所有世界的自动快照失败次数、最近一次
        失败的错误码与时刻。busy 与跳过不算失败。"""
        with self._lock:
            return {"failures": self._auto_failures, "last_error": self._auto_last_error,
                    "last_error_at": self._auto_last_error_at}

    def _vector_backend(self, world: str) -> str:
        return _core.world_effective_settings(self._record(world)["settings"])["vector_backend"]

    # ───────────────────────── 恢复（只停这一个世界，spec §7.2） ─────────────────────────

    def rollback(self, world: str, snapshot_id: str) -> dict:
        """把 world 恢复到快照 snapshot_id（丢掉快照之后的一切）。替换、校验、归档都在 C++
        （world_rollback）；被替换的库留在 <世界目录>/archive/。返回 {"world", "snapshot_id", "archived",
        "reopened"}（reopened：恢复前开着、恢复后已重新打开）。

        只停被恢复的世界：持它的世界锁、置它的关闭标志、等它的租约（含只读检视）归零、停它的线程、丢它
        的引擎并确认 Runtime 已回收，然后才调 C++；别的世界的租约与写入照常（需求 §7 第 1b 条）。

        抛错：C++ 的码（snapshot_not_found / snapshot_corrupt / world_tenant_mismatch / insufficient_disk /
        snapshot_failed / snapshot_unsupported_backend / world_in_use）是 _core.WorldStoreError，库原样不动；
        world_busy（在途请求 _LEASE_DRAIN_TIMEOUT_S 内不结束、线程停不下、Runtime 回收不了或之前的关闭
        留下的 Runtime 还开着这个库，库未动）；world_open_failed（库未动，但重开引擎失败）；
        rollback_reopen_failed（库已替换，重开失败）。后两种引擎不在表上，下一次 lease 重新建。
        动引擎之前先跑 C++ 的只读预检（world_check_rollback）：不改输入重试也过不了的错误直接抛出，引擎、
        线程、库都不动，也不进关闭窗口。"""
        reg = self._require_registry()
        rec = self._record(world)
        backend = self._vector_backend(world)
        _core.world_check_rollback(reg, world, snapshot_id, vector_backend=backend)
        with self._world_lock(world), _ClosingWindow(self, world):
            self._refuse_leaked(rec["db_path"])
            with self._lock:
                was_open = world in self._engines
            if not self._close_engine(world, _LEASE_DRAIN_TIMEOUT_S):
                busy = WorldSwitchError(
                    "world_busy", "old runtime still referenced; the database was not replaced")
                # 旧 Runtime 还活着：不能再开第二条写连接，所以不重开（下一次 lease 被 _refuse_leaked 挡住，
                # 直到它被回收）。
                raise busy
            try:
                result = _core.world_rollback(reg, world, snapshot_id, vector_backend=backend)
            except Exception as exc:
                self._reopen_after_rollback(world, was_open, exc)   # C++ 保证库未动；原样抛出
                raise
            self._reopen_after_rollback(world, was_open, None)
        return {"world": world, "snapshot_id": snapshot_id,
                "archived": bool(result["archive_path"]), "reopened": was_open}

    def _reopen_after_rollback(self, world: str, was_open: bool, cause: Exception | None) -> None:
        """恢复的每个出口（持世界锁、在关闭窗口内）：恢复前开着就按注册表重建引擎、起线程（打开时会跑
        迁移，旧 schema 的快照会补齐）。重建失败：引擎不在表上，下一次 lease 重新建。码按库有没有被替换来分：
        cause 为 None（替换成功之后）→ rollback_reopen_failed；其余出口 → world_open_failed（链到 cause）。
        重建成功则返回，由调用方抛 cause 或返回结果。"""
        if not was_open:
            return
        try:
            engine = self._open_engine(world, self.context(world))
        except Exception as exc:
            if cause is None:
                detail = f"rollback applied, but the world cannot be reopened: {exc}"
                logger.error("world rollback (%s): %s", world, detail)
                raise WorldSwitchError("rollback_reopen_failed", detail) from exc
            detail = f"{cause}; reopening the world also failed: {exc}"
            logger.error("world rollback (%s): %s", world, detail)
            raise WorldSwitchError("world_open_failed", detail) from cause
        if self._background:
            self._start_workers(world, engine)
        with self._lock:
            self._engines[world] = engine

    # ───────────────────────── 自动快照（W4） ─────────────────────────

    def attach_hooks(self, world: str, engine) -> None:
        """给引擎挂上按会话的自动快照钩子（在它起摄入线程之前）。"""
        engine.session_snapshot_hook = self._make_session_hook(world)

    def _make_session_hook(self, world: str):
        def hook(session_id: str) -> None:
            # 摄入线程上、写入之前、不持 engine._lock 时调用。busy → 放回 spool（_ingest_drain_once 接住）；
            # 拍了、跳过、失败都照常返回，摄入继续。最外层兜底：只有 SessionSnapshotDeferred 往外抛，
            # 钩子坏了不能变成摄入 job 的失败（用户裁定：快照失败照常摄入）。
            try:
                if self._session_trigger_off(world):
                    return
                if self._auto_snapshot(world, "session", session_id) == "busy":
                    raise SessionSnapshotDeferred(session_id)
            except SessionSnapshotDeferred:
                raise
            except Exception:  # noqa: BLE001 — 后台路径：只记录，不外抛
                try:
                    logger.exception("session auto snapshot hook failed (%s, %s)", world, session_id)
                except Exception:  # noqa: BLE001 — 日志本身也坏了：照样不能连带摄入
                    pass
        return hook

    def _session_trigger_off(self, world: str) -> bool:
        """钩子的提前退出：不取世界锁读一次注册表，该世界的 auto_snapshot.on_session 为 False 就返回 True，
        不取锁也不推迟摄入（否则每个摄入 job 都会因无关的管理操作持着世界锁而被放回 spool）。只决定
        「肯定不用拍」：读到开着就走持锁路径，C++ 按当时的配置重新判（被关掉时回 disabled）。读失败
        返回 False，同样走持锁路径。"""
        try:
            settings = _core.world_effective_settings(self._registry.get(world)["settings"])
            return not settings["background"]["auto_snapshot"]["on_session"]
        except Exception:  # noqa: BLE001 — 提前退出读不到不是失败，交给持锁路径
            return False

    def _auto_snapshot(self, world: str, trigger_kind: str, session_id: str = "") -> str:
        """两种触发共用的核心，返回 "taken:<id>" | "skipped:<原因>" | "busy" | "failed"。非阻塞取该世界的
        世界锁（关闭、恢复、手动快照持着它就 busy，从不等）。修剪卡住时 C++ 不拍、返回 refused，按
        snapshot_failed 计一次失败。任何异常都计为失败、记 warning、返回 "failed"，不往外抛：这是后台路径。"""
        lock = self._world_lock(world)
        if not lock.acquire(blocking=False):
            return "busy"
        now = None
        try:
            now = self._clock()
            out = _core.world_take_auto_snapshot(self._registry, world, trigger_kind, now, session_id,
                                                 vector_backend=self._vector_backend(world))
            if out.get("refused"):
                logger.warning("auto snapshot (%s, %s) refused: %s", world, trigger_kind,
                               out["refused"])
                self._count_auto_failure("snapshot_failed", now)
                return "failed"
            snap = out["snapshot"]
            if snap is None:
                return f"skipped:{out['skipped']}"
            return f"taken:{snap['id']}"
        except Exception as exc:  # noqa: BLE001 — 后台路径：只记录，不外抛（用户裁定）
            # 错误码进状态（GET /api/worlds 外露），全文只进日志：C++ 的 detail 里有服务器路径。
            domain = isinstance(exc, (_core.WorldStoreError, WorldSwitchError))
            code = (exc.code if isinstance(exc, WorldSwitchError)
                    else _store_code(exc) if domain else "internal_error")
            logger.warning("auto snapshot (%s, %s) failed: %s", world, trigger_kind, exc,
                           exc_info=not domain)
            self._count_auto_failure(code, now)
            return "failed"
        finally:
            lock.release()

    def _count_auto_failure(self, code: str, now: str | None) -> None:
        if now is None:
            now = _now_iso()        # 注入的时钟本身抛了：时刻退回真实时钟
        with self._lock:
            self._auto_failures += 1
            self._auto_last_error = code
            self._auto_last_error_at = now

    def auto_snapshot_round(self, stop: threading.Event | None = None) -> dict:
        """按时间的一轮（裁定 R19）：遍历注册表里的全部世界，逐个调 _auto_snapshot("interval")，要不要拍由
        C++ 判。返回 {世界名: 结果}。关着的世界不打开引擎（C++ 只读源库、VACUUM INTO）。stop 置位时提前结束。"""
        reg = self._require_registry()
        results: dict[str, str] = {}
        offset = 0
        while True:
            page = reg.list(offset=offset, limit=_LIST_PAGE)
            for rec in page["worlds"]:
                if stop is not None and stop.is_set():
                    return results
                results[rec["name"]] = self._auto_snapshot(rec["name"], "interval")
            offset += _LIST_PAGE
            if not page["worlds"] or offset >= page["total"]:
                return results
