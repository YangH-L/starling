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

新建的引擎一律先发布、再起后台线程（_publish，裁定 R23）：起线程失败时引擎已经在表上，不会变成一个
既没发布、也没登记成泄漏的活 Runtime。失败只记日志、标记 workers-lost，下一次租约（非阻塞取世界锁）
补起；补起之前引擎照常服务请求。

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
  3. engine._lock（RLock，DashboardEngine 内部）：由持租约的请求和后台线程持有。池只在一处拿它：fork_world
     分叉开着的来源时，先取来源的世界锁、在 pool._lock 内查表拿到引擎并放开 pool._lock，再取 engine._lock，持着它
     走引擎自己的连接复制（裁定 R5、R22 M5）；顺序与本表一致（世界锁 → engine._lock），且不持租约。别处不拿：quiesce
     在不持引擎锁的情况下 join 后台线程（线程分段拿锁，持锁 join 会卡满 5 秒误报 world_busy）。
  4. supervisor 互斥锁（C++ Runtime 内部）：只在持 engine._lock 时取（见 engine.py 的
     _sample_backpressure 一带），从不反向。

  即 pool._lock → pool._world_locks[name] → engine._lock → supervisor。实际代码里 1 在 2 之内只短暂取
  （查表、改表），从不在持 1 时去取 2。

  app.state.config_lock（threading.Lock，写配置的路由在工作线程里取，见 routes/config.py）排在这四把之前：持着它时
  会发默认世界的租约（补起 workers-lost 的后台线程时非阻塞取世界锁）、取默认世界的 engine._lock。所以别处要它时
  一律在取世界锁、engine._lock 之前取，不得在持世界锁或 engine._lock 时等它（重新确认端点只在取世界锁之前持它
  拷 provider 表，裁定 R59 N-3）。
  持租约的代码不得阻塞地等 _world_locks：关闭、恢复持着世界锁等租约归零，反过来就是死锁。持租约时取
  世界锁的地方只有两处，都不阻塞地等：
    - 首次建引擎（lease 先登记租约、放开 _lock，再取世界锁建引擎）：_acquire_for_build 以 50 毫秒为一段
      轮询，每段之间查该世界的关闭标志，标志已置就放弃（world_busy），租约随之归还，关闭的等待随即结束。
      关闭先取世界锁、再置标志、再等租约，所以构建的租约要么在关闭取世界锁之前已经建完（关闭等它用完），
      要么在轮询里看到标志后放弃，不会互等。
    - 补起 workers-lost 世界的后台线程（_retry_lost_workers，裁定 R23）：只试一次 acquire(blocking=False)，
      取不到这次就不补，下一次租约再试。
  这两处都可能在事件循环上运行：路由同步调 lease 时，惰性建引擎与补起线程（start_ingest_worker、起不来时的
  quiesce，最长约 5 秒）就在事件循环线程上做，与旧实现的惰性构建一样。

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

只读打开（spec §6.3、§6.4，第 3 期）
-----------------------------------
建引擎时问 C++ world_decide_open_mode：只读世界（含种子）与可回收世界只读打开（WorldContext.read_only），
库经 open_read_only 打开，不跑迁移；引擎不起 tick、摄入，池不给它挂按会话的自动快照钩子；写入类调用一律
world_read_only。分叉（fork_world）按来源选路、持锁（裁定 R5）：开着的来源持引擎锁、走引擎自己的连接，
关着的来源持它的世界锁，快照来源直接复制。

本期不做：没有空闲自动关闭（C++ 的空闲判定在第 4 期，裁定 R21）。世界开着直到 close_world、
delete_world 或关停（close_all），默认世界只在关停时关。

客户端会话与写占用（第 4 期，spec §6.4、§11）
---------------------------------------------
会话、句柄、写占用、在途调用的状态机全在 C++（_core.SessionTable，app 在 lifespan 里建好后 attach_sessions 注入）。池只做两件
事：把「此刻有没有客户端会话占着这个世界的写入」（SessionTable.world_write_held，含还有在途调用的已关闭、已过期句柄，裁定 R59）
交给 C++ 的判定，以及按判定的结果建、关引擎。时钟只有一个（裁定 R60）：SessionTable 的每次调用都从 now_ms()（注入的单调时钟，
缺省 time.monotonic 换算成毫秒）取 now_ms，不混用墙钟——过期是终态，一个超前的 now 会把会话永久判死。

  - 打开方式：world_decide_open_mode(rec, for_client_write=held)（裁定 R67）。可回收世界没人占着时看板只读打开，被客户端占着
    写入时建可写引擎；被占用的世界被关掉（close_world 照旧允许，spec §10.3）后，下一次租约重建的也是可写引擎。
  - 冻结：world_read_only_frozen(rec, held)（裁定 R41、R49 (c)）。immutable 连接不加锁，文件在它活着时被写就读到撕裂的数据，
    所以写占用期间一律普通 mode=ro。
  - 后台：可回收世界不论按哪种方式打开都不起 tick、摄入，不挂按会话的自动快照钩子（裁定 R68，world_effective_settings 带
    record=rec）。rollout 由客户端驱动。
  - 没有注入会话表的池（测试里直接建的）按没人占着写入处理。

世界锁内调用的 C++（不变式，裁定 R66）
------------------------------------
C++ 里有 7 处按记录、以默认 held=false 判冻结的只读打开（world_store.cpp 的 take_snapshot、rollback_world 回滚后算摘要、
session_known、current_content_mark、list_sessions、fork_world、upgrade_seed）。它们只在没有写者时才可能走 immutable，而「没有
写者」靠世界锁保证：建引擎持世界锁，所以持世界锁期间表上的引擎不会变；没有引擎就没有写连接，有可写引擎则库旁边有 -wal/-shm，
冻结退回普通 mode=ro。所以凡是调用下面这些 C++ 函数，调用方都必须持着该世界的世界锁：
    world_take_snapshot（take_snapshot）、world_take_auto_snapshot（_auto_snapshot；内含 session_known、current_content_mark）、
    world_rollback（_rollback_in_cpp，在 rollback 的关闭窗口里）、world_list_sessions（list_sessions）、
    world_fork（fork_world，来源世界的锁；以快照为来源时例外：快照文件登记之后不再被写）、world_upgrade_seed（upgrade_seed）。
新增这些函数的调用点时照此取锁；等世界锁时手里不能持租约（见上面的锁顺序）。
"""
from __future__ import annotations

import contextlib
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
from starling.dashboard.config import provider_endpoint_settings, provider_endpoints
from starling.dashboard.engine import (DashboardEngine, SessionSnapshotDeferred,
                                       WorldContext)

logger = logging.getLogger(__name__)

__all__ = ["EngineFactory", "EngineLease", "WorldContext", "WorldPool", "WorldSwitchError",
           "engine_lease"]

# 关闭、恢复等在途请求（租约）归零的默认上限。超时：清标志、恢复后台线程、回 world_busy。
_LEASE_DRAIN_TIMEOUT_S = 30.0
# 丢掉引擎后等旧 Runtime 被回收的宽限：异常回溯、刚返回的栈帧可能还短暂引用着它。
_RUNTIME_RELEASE_GRACE_S = 1.0
# 句柄 close(timeout) 等在途调用归零的轮询间隔（秒）。
_HANDLE_CLOSE_POLL_S = 0.02
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
# 默认世界经 PUT /api/worlds/default/settings 能改的设置（裁定 R35）：其余段（endpoint、阈值、来源通道、
# tick 间隔）归 /api/config，下一次 sync_default_settings 会整段覆盖它们。auto_snapshot 与 ingest 不在
# config 里（裁定 R16），同步时保留。
_DEFAULT_WORLD_EDITABLE = {"background": {"auto_snapshot", "ingest"}}

EngineFactory = Callable[[WorldContext, dict], DashboardEngine]


def _default_factory(ctx: WorldContext, providers: dict) -> DashboardEngine:
    return DashboardEngine(ctx, providers)


class WorldSwitchError(Exception):
    """code: "world_not_found" | "world_busy" | "world_protected" | "worlds_unavailable"
    | "world_open_failed" | "rollback_reopen_failed" """

    # 恢复的出口：库已经换成快照的内容（裁定 R32），路由据此广播 world_rolled_back。只有 rollback_reopen_failed 会是 True。
    pool_replaced = False

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


def _replaced_by_rollback(exc: BaseException) -> bool:
    """world_rollback 抛出时库是不是已经换掉了：C++ 的标记（WorldStoreError.replaced，裁定 R31 M5、R32），不看错误码、
    不看文件。不是 C++ 抛的异常（没有这个属性）按 False。"""
    return bool(getattr(exc, "replaced", False))


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


def _monotonic_ms() -> int:
    """会话表的缺省时钟：time.monotonic 换算成毫秒（裁定 R60：一个进程只用这一个时钟）。"""
    return int(time.monotonic() * 1000)


class WorldPool:
    def __init__(self, registry, config, *, clock=None,
                 engine_factory: EngineFactory | None = None,
                 on_world_changed: Callable[[str, dict], None] | None = None,
                 monotonic_ms: Callable[[], int] | None = None) -> None:
        self._registry = registry                     # _core.WorldRegistry | None（裁定 R6）
        self._config = config
        self._clock = clock or _now_iso
        # 会话表（_core.SessionTable，attach_sessions 注入）与喂给它的唯一时钟（裁定 R60）。没有会话表时一律按没人占着写入。
        self._sessions = None
        self._monotonic_ms = monotonic_ms or _monotonic_ms
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
        self._vector_warned = False                   # 非 sqlite 的 config 只提醒一次（裁定 R26）
        # 发布时没起来后台线程的世界（裁定 R23）：下一次租约在世界锁内补起，补起之前引擎照常服务请求。
        self._workers_lost: set[str] = set()

    # ───────────────────────── 注册表与上下文 ─────────────────────────

    @property
    def registry(self):
        """_core.WorldRegistry；库路径没有父目录（:memory: 等）时为 None（裁定 R3、R6）。"""
        return self._registry

    # ───────────────────────── 客户端会话表（第 4 期） ─────────────────────────

    def attach_sessions(self, sessions) -> None:
        """注入客户端会话表（_core.SessionTable）。app 在 lifespan 里建好后、开始服务请求之前调一次。之后池的冻结、打开方式、
        空闲关闭、恢复与删除的检查都问它，now_ms 一律取 now_ms()（裁定 R60）。"""
        self._sessions = sessions

    @property
    def sessions(self):
        """注入的 _core.SessionTable；没注入（测试里直接建的池）时为 None。"""
        return self._sessions

    def now_ms(self) -> int:
        """会话表的唯一时钟（注入的单调时钟，毫秒；裁定 R60）。SessionTable 的每次调用都从这里取 now_ms。"""
        return int(self._monotonic_ms())

    def _write_held(self, world: str) -> bool:
        """此刻有没有客户端会话占着 world 的写入（C++ SessionTable.world_write_held：活的可写句柄，或还有在途调用的可写句柄，
        裁定 R59）。没有会话表时 False。"""
        sessions = self._sessions
        return bool(sessions is not None and sessions.world_write_held(world, self.now_ms()))

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
        （<config.ingest_spool_root()>/<world_ingest_partition(...)>，裁定 R4）。默认世界的向量后端与路径
        取 config（裁定 R26，默认世界「原地」），其余世界 sqlite、没有路径。

        三个判定都在 C++，这里只传 held（此刻有没有客户端会话占着它的写入，_write_held）：
          - 打开方式 world_decide_open_mode(rec, for_client_write=held)（spec §6.4，裁定 R67）：只读世界、种子只读；可回收世界
            没人占着时只读，被客户端占着写入时可写（客户端的写要能落库）；其余可写。
          - 冻结 world_read_only_frozen(rec, held)（裁定 R41、R49 (c)）：只读打开且没人占着写入时才冻结。
          - 设置 world_effective_settings(..., record=rec)（裁定 R68）：只读打开的世界与可回收世界后台全关，可回收世界切成可写
            引擎也不起 tick、摄入（rollout 由客户端驱动）。"""
        rec = self._record(world)
        held = self._write_held(world)
        is_default = world == self.default_world()
        partition = _core.world_ingest_partition(is_default, rec["tenant"], rec["dir_id"])
        backend, store_path = self._vector_store(world, is_default)
        return WorldContext(
            name=world, dir_id=rec["dir_id"], db_path=rec["db_path"], tenant=rec["tenant"],
            agent=self._config.agent,
            settings=self._effective_settings(rec),
            ingest_spool_dir=str(self._config.ingest_spool_root() / partition),
            is_default=is_default, vector_backend=backend, vector_store_path=store_path,
            read_only=_core.world_decide_open_mode(rec, for_client_write=held) == "ro",
            frozen=_core.world_read_only_frozen(rec, held))

    @staticmethod
    def _effective_settings(rec: dict, settings: dict | None = None) -> dict:
        """给引擎的设置：C++ 按记录算的生效设置（裁定 R33、R68）。settings 缺省取记录里存着的。"""
        return _core.world_effective_settings(rec["settings"] if settings is None else settings,
                                              record=rec)

    def _vector_store(self, world: str, is_default: bool | None = None) -> tuple[str, str]:
        """(向量后端, 路径)：默认世界取 config（裁定 R26），其余世界 ("sqlite", "")。快照、恢复、自动快照
        把这个后端交给 C++，zvec 的默认世界由 C++ 拒绝（snapshot_unsupported_backend）。"""
        if is_default is None:
            is_default = world == self.default_world()
        if is_default:
            return (self._config.vector_backend or "sqlite",
                    self._config.vector_store_path or "")
        return "sqlite", ""

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
            lost = world in self._workers_lost
        lease = EngineLease(self, world, None)
        try:
            if engine is None and build:
                engine = self._build(world)
            elif engine is not None and lost:
                self._retry_lost_workers(world)
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
            return self._open_and_publish(world)
        finally:
            lock.release()

    def _open_and_publish(self, world: str) -> DashboardEngine:
        """持世界锁调用（表上没有它的引擎）：按此刻的 context 建引擎并发布，返回引擎。_build 与 admit_client_handle 共用。"""
        ctx = self.context(world)
        self._refuse_leaked(ctx.db_path)              # 旧 Runtime 还开着这个库：不开第二条写连接
        if not ctx.is_default:
            self._verify_tenant(ctx)                  # 裁定 R36：在打开库之前，拒开时什么都没建
        engine = self._open_engine(world, ctx)
        if not self._publish(world, engine):
            box, engine = [engine], None              # 交出唯一的引用：回收证明要求本帧不再钉住它
            self._discard_unpublished(box, ctx.db_path)
            raise WorldSwitchError("world_busy", "the pool is shutting down")
        return engine

    # ───────────────────────── 客户端句柄（给 4.5 的路由，裁定 R61） ─────────────────────────

    def admit_client_handle(self, world: str, session_id: str, holder: str, read_only: bool,
                            now_ms: int) -> str:
        """客户端的每一次开句柄（只读、可写都一样）都经这里，在 world 的世界锁内执行（裁定 R61），返回 handle_id。

        世界锁以 50 毫秒为一段轮询着取（同首次建引擎）：世界处在关闭窗口里（关闭、恢复、删除），或等锁时进了关闭窗口，或池已
        关停，就回 world_busy（瞬态），什么都不登记。所以关闭、恢复、删除与开句柄互相排斥。调用方在工作线程里调，不持租约。

        可写打开：
          1. C++ world_require_writable(rec, for_client_write=True, held_by_other=False)：种子、只读世界 world_read_only。两个
             客户端之间的写占用冲突由 SessionTable.open_handle 抛 world_held。
          2. 池里已有可写引擎：写者已经在了（库旁边有 -wal/-shm，冻结退回普通 mode=ro），不用等，直接登记。
          3. 池里没有可写引擎（没有引擎，或只有只读引擎）：进关闭窗口，走与 close_world 相同的路径等该世界的全部租约（build=True
             与 build=False）归零、把只读引擎完整关掉并确认它的 Runtime 已回收（裁定 R30、R37、R53）；然后先登记写占用
             （open_handle），再按此刻的 context 建可写引擎（held 已为真，C++ 判 rw、不冻结，裁定 R67）；最后退出窗口。窗口里
             谁也打不开这个世界的库，所以任何检视连接要么在登记之前已经关掉，要么在登记之后才打开（冻结判定给 Shared）。
             建引擎失败：撤销这个句柄（不留没有引擎的写占用），原样抛出。旧 Runtime 回收不了：world_busy，不登记。
        只读打开：不切换引擎。池里没有引擎时按看板的方式打开（此刻没人占着写入就是 decide_open_mode(rec, false)），然后登记。

        会话未知或已过期：SessionTable 抛 session_expired。now_ms 必须来自池的时钟（now_ms()，裁定 R60）。"""
        sessions = self._sessions
        if sessions is None:
            raise WorldSwitchError("worlds_unavailable", "the pool has no client session table")
        rec = self._record(world)                     # world_not_found：未知世界名不建世界锁
        lock = self._acquire_for_build(world)         # 关闭窗口里 → world_busy
        try:
            with self._lock:
                closing = world in self._closing or self._shut
                engine = self._engines.get(world)
            if closing:
                raise WorldSwitchError("world_busy", world)
            has_writer = engine is not None and not getattr(engine, "read_only", False)
            has_engine = engine is not None
            engine = None
            if read_only:
                if not has_engine:
                    self._open_and_publish(world)
                return sessions.open_handle(session_id, world, holder, True, now_ms)
            _core.world_require_writable(rec, for_client_write=True, held_by_other=False)
            if has_writer:
                return sessions.open_handle(session_id, world, holder, False, now_ms)
            return self._admit_first_writer(world, rec, session_id, holder, now_ms)
        finally:
            lock.release()

    def _admit_first_writer(self, world: str, rec: dict, session_id: str, holder: str,
                            now_ms: int) -> str:
        """持世界锁调用（池里没有 world 的可写引擎）：关闭窗口里排干全部租约、关掉只读引擎，登记写占用，建可写引擎。"""
        sessions = self._sessions
        with _ClosingWindow(self, world):
            if not self._close_engine(world, _LEASE_DRAIN_TIMEOUT_S):
                raise WorldSwitchError(
                    "world_busy", "the read-only engine's runtime is still referenced")
            self._refuse_leaked(rec["db_path"])
            handle = sessions.open_handle(session_id, world, holder, False, now_ms)
            try:
                self._open_and_publish(world)
            except BaseException:
                sessions.close_handle(handle)
                raise
            return handle

    def require_dashboard_writable(self, world: str) -> None:
        """看板写入类命令的准入（裁定 R51）：C++ world_require_writable(rec, for_client_write=False,
        held_by_other=world_write_held(world, now))。被客户端占着写入的世界 world_held（可回收世界也是 world_held，不是
        world_read_only）；没人占着的只读打开的世界 world_read_only。改内容的命令路由与 /ws/converse 在动引擎之前调。
        只读检视与召回不调。"""
        _core.world_require_writable(self._record(world), for_client_write=False,
                                     held_by_other=self._write_held(world))

    def _refuse_while_held(self, world: str) -> None:
        """改设置的三条路由（PUT 设置、重新确认端点、改自动快照）的准入（裁定 R73），持世界锁调用（与 admit_client_handle
        互斥）：只看写占用（C++ SessionTable.world_write_held），不判只读——它们写的是 registry.db，不碰世界库（R38 不变）；
        但会重新配置客户端正在用的那个引擎，所以被占着时回 world_held，什么都不写。"""
        if self._write_held(world):
            raise WorldSwitchError("world_held", f"{world} is held for write by a client session")

    def close_client_handle(self, handle_id: str, timeout_s: float) -> None:
        """句柄的 close(timeout)（spec §11）：等这个句柄的在途调用（SessionTable.in_flight）归零再关。超时回 world_busy，句柄保持
        打开。未知或已关闭的句柄直接返回（幂等）。短轮询（20 毫秒），不取世界锁、不持租约。"""
        sessions = self._sessions
        if sessions is None:
            raise WorldSwitchError("worlds_unavailable", "the pool has no client session table")
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while sessions.in_flight(handle_id) > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorldSwitchError(
                    "world_busy", f"{sessions.in_flight(handle_id)} call(s) still in flight")
            time.sleep(min(_HANDLE_CLOSE_POLL_S, remaining))
        sessions.close_handle(handle_id)

    @staticmethod
    def _verify_tenant(ctx: WorldContext) -> None:
        """租户不变式（世界设计 §2.1，裁定 R36）：非默认世界第一次建引擎之前，C++ world_verify_tenant 只读
        扫一遍库，有别的租户的行就拒绝（world_tenant_mismatch），判断全在 C++。库读不出按「打不开」报
        world_open_failed。默认世界（原地的 dashboard.db）早于世界管理，既有部署可按租户过滤地存多个租户，
        不核对（与旧的启动校验一致）。"""
        try:
            _core.world_verify_tenant(ctx.db_path, ctx.tenant, frozen=ctx.frozen)
        except _core.WorldStoreError as exc:
            code = "world_tenant_mismatch" if _store_code(exc) == "world_tenant_mismatch" \
                else "world_open_failed"
            raise WorldSwitchError(code, str(exc)) from exc

    def _publish(self, world: str, engine) -> bool:
        """持世界锁调用：先把引擎放进表，再按后台开关起线程（裁定 R23）。顺序不能反：起线程失败时引擎若还
        不在表上，它的 Runtime（已打开写连接，起了一半的 tick 线程还引用着它）既没发布也没登记成泄漏，
        下一次租约会在同一个库上再建一条写连接。起线程失败只记日志、标记 workers-lost，下一次租约补起；
        这个引擎照常服务请求。

        池已关停（close_all 置了 _shut）时不放进表、返回 False，由调用方关掉这个引擎（_discard_unpublished）。
        close_all 只关它列出那一刻表上的世界：一个已经持着世界锁、正在首次打开的世界不在表上，它若在关停之后
        才发布，写连接就一直开着。判断与放进表在同一次持 _lock 内，所以要么在 close_all 列表之前已经在表上
        （close_all 取世界锁等它建完再关），要么看到 _shut、不发布。"""
        with self._lock:
            if self._shut:
                return False
            self._engines[world] = engine
            background = self._background
        if background:
            self._start_workers_or_mark_lost(world, engine)
        return True

    def _discard_unpublished(self, box: list, db_path: str) -> bool:
        """关掉一个没能发布的引擎（池已关停）：摘掉池的回调、关引擎、等旧 Runtime 回收（回收不了照常登记成
        泄漏），返回是否已回收。没起过线程（_publish 没走到起线程那一步）。

        引擎装在 box（只有一个元素的列表）里交进来，这里取出后清空它：调用方的局部变量若还指着引擎，
        回收证明永远等不到（等满宽限期、登记成泄漏，此后这个库一直 world_busy）。调用方交出之前先把
        自己的局部变量置 None。"""
        engine = box.pop()
        rt_ref = weakref.ref(engine._rt)
        engine.on_commit = None
        engine.session_snapshot_hook = None
        engine.close()
        engine = None
        return self._release_runtime(rt_ref, db_path)

    def _start_workers_or_mark_lost(self, world: str, engine) -> bool:
        """持世界锁调用。起 tick / 摄入线程；任一步失败就把已经起来的停掉（不留半套线程，补起时整套重来），
        记日志，标记 workers-lost，返回 False。成功时清掉标记，返回 True。"""
        try:
            self._start_workers(world, engine)
        except Exception as exc:  # noqa: BLE001 — 线程起不来不能连带租约失败（与旧 _restart_lost_workers 相同）
            # 只记格式化好的文本：留着 LogRecord 的 handler（caplog、队列、内存 handler）若拿到 exc_info 或
            # 异常对象本身（在 args 里），就经 __traceback__ 钉住持有引擎的栈帧（正在执行的帧 clear_frames
            # 清不掉），之后关闭就证明不了 Runtime 已回收（世界一直 world_busy，直到 gc）。
            detail = f"{type(exc).__name__}: {exc}"
            traceback.clear_frames(exc.__traceback__)
            exc.__traceback__ = None
            exc = None
            logger.error("cannot start background workers of world %s; retrying on the next "
                         "lease: %s", world, detail)
            if not engine.quiesce():
                logger.warning("background threads of world %s did not stop after a failed start: %s",
                               world, "/".join(engine.quiesce_stuck))
            with self._lock:
                self._workers_lost.add(world)
            return False
        with self._lock:
            self._workers_lost.discard(world)
        return True

    def _retry_lost_workers(self, world: str) -> None:
        """租约发现该世界 workers-lost：非阻塞取世界锁补起线程。世界锁被占（快照、关闭、恢复）就这次不补，
        下一次租约再试；持租约时不能阻塞地等世界锁（见模块文档锁顺序）。补起失败同样只记日志。"""
        lock = self._world_lock(world)
        if not lock.acquire(blocking=False):
            return
        try:
            with self._lock:
                engine = self._engines.get(world)
                still_lost = world in self._workers_lost
                closing = world in self._closing or self._shut
            if engine is None or not still_lost or closing or not self._background:
                return
            self._start_workers_or_mark_lost(world, engine)
        finally:
            engine = None
            lock.release()

    def workers_lost_worlds(self) -> list[str]:
        """发布时没起来后台线程、等下一次租约补起的世界。"""
        with self._lock:
            return sorted(self._workers_lost)

    def _open_engine(self, world: str, ctx: WorldContext):
        """建引擎并挂上池的回调。失败抛 world_open_failed：清掉回溯里的栈帧，半建的 Runtime 不能被异常
        钉住（调用方可能留着这个异常，同时为同一个库再建一个引擎）。"""
        try:
            engine = self._factory(ctx, self._config.providers)
        except Exception as exc:
            traceback.clear_frames(exc.__traceback__)
            raise WorldSwitchError("world_open_failed", str(exc)) from exc
        self._wire(world, engine)
        return engine

    def _wire(self, world: str, engine) -> None:
        """挂上池的回调：统计去抖广播（on_commit）与按会话的自动快照钩子（有注册表时）。钩子只挂给可写打开、且不可回收的世界
        （spec §6.3、裁定 R68）：只读引擎与可回收世界（切成可写引擎也一样）不起摄入，没有调用钩子的地方。"""
        engine.on_commit = lambda: self._note_change(world)
        if self._registry is None or getattr(engine, "read_only", False):
            return
        if self._record(world).get("reclaimable"):
            return
        self.attach_hooks(world, engine)

    def seed_engine(self, world: str, engine) -> None:
        """把一个已经建好的引擎作为 world 的引擎放进池（create_app(engine=...)：测试与嵌入式调用方注入默认
        世界的引擎）。不建引擎：换上池给这个世界的设置、挂上池的回调，然后与新建的引擎一样发布（_publish）。
        create_app 在 lifespan 之前调用，后台线程还没开，所以什么都不起；之后 start_background 按世界设置起。

        world 不存在 → world_not_found；池已关停、该世界正在关闭、已经有引擎，或之前的关闭留下的 Runtime
        还开着这个库 → world_busy（同一个库上不能有两条写连接）。引擎打开的库与租户是否就是这个世界的，
        由调用方先核对（app._require_engine_matches）。"""
        try:
            ctx = self.context(world)                 # world_not_found
            with self._world_lock(world):
                with self._lock:
                    busy = self._shut or world in self._closing or world in self._engines
                if busy:
                    raise WorldSwitchError("world_busy", world)
                self._refuse_leaked(ctx.db_path)
                engine.set_world_settings(ctx.settings)
                self._wire(world, engine)
                if not self._publish(world, engine):  # 判断与放进表之间池关停了
                    raise WorldSwitchError("world_busy", world)
        except BaseException:
            # 异常的回溯持有本帧：调用方留着异常时，engine 参数会钉住被拒的引擎（同 _close_engine）。
            engine = None
            raise

    # ───────────────────────── 只读查询 ─────────────────────────

    def open_worlds(self) -> list[str]:
        with self._lock:
            return sorted(self._engines)

    def closing_worlds(self) -> list[str]:
        with self._lock:
            return sorted(self._closing)

    def list_worlds(self, labels: dict | None = None, offset: int = 0, limit: int = 50) -> dict:
        """注册表的一页 {"worlds": [记录], "total"}（C++ RegistryDb::list：标签全部匹配；写过的世界按最近写入
        last_write_at 降序在前，没写过的在后，同序再按 created_at 降序、name 升序，spec §14；分页参数由 C++ 校验，
        invalid_world_query）。只读注册表，不开任何世界的库；开着的世界的计数由调用方
        经 live_stats 现读。"""
        reg = self._require_registry()
        return reg.list(labels=dict(labels or {}), offset=offset, limit=limit)

    def record(self, world: str) -> dict:
        """注册表记录（world_not_found；没有注册表时 worlds_unavailable）。"""
        self._require_registry()
        return self._record(world)

    def db_stats(self, world: str) -> dict:
        """世界库的完整统计（C++ world_stats：db_bytes、live_bytes、statements、engrams、holders、
        last_write_at 等），持一份不建引擎的租约只读打开库（恢复要等它归还）。库文件不在时全 0；读不出时
        C++ 抛 snapshot_failed。"""
        self._require_registry()
        with self.lease(world, build=False):
            ctx = self.context(world)
            return _core.world_stats(ctx.db_path, ctx.tenant, frozen=ctx.frozen)

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
            stats = _core.world_stats(db_path, tenant, frozen=self._frozen(world))
        return {"holders": stats["holders"], "statements": stats["statements"],
                "last_write_at": stats["last_write_at"]}

    def schema_behind(self, world: str, *, open_only: bool = False) -> bool | None:
        """世界库的 schema 是否落后于本构建（spec §6.3，C++ SqliteAdapter::schema_behind）。开着的世界
        取引擎打开时记下的值，不碰它的连接。关着的世界：open_only 时返回 None（列表不打开库，裁定 R17）；否则持
        一份不建引擎的租约，短暂只读打开库（不迁移、不改日志模式）读一次。库不在或读不出时 None。"""
        try:
            lease = self.lease(world, build=False)
        except WorldSwitchError as exc:
            if exc.code == "world_busy":
                return None
            raise
        with lease as engine:
            if engine is not None:
                behind = getattr(engine, "schema_behind", None)
                engine = None
                return behind
            if open_only:
                return None
            ctx = self.context(world)
            db_path = ctx.db_path
            if not os.path.exists(db_path):
                return None
            try:
                # 只持 build=False 租约，不持世界锁：可写世界随时可能被写，按记录选冻结（C++ 判，裁定 R36）。
                adapter = _core.SqliteAdapter.open_read_only(db_path, frozen=ctx.frozen)
                try:
                    return adapter.schema_behind               # 判定在 C++（裁定 R31 M5）
                finally:
                    adapter.close()
            except Exception:  # noqa: BLE001 — 坏库只是没有这个标记，详情照常给出
                logger.warning("cannot read the schema version of world %s", world)
                return None

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

    def _frozen(self, world: str) -> bool:
        """这个世界的只读连接能不能冻结（C++ world_read_only_frozen，裁定 R36、R41）：带上此刻的写占用。读不到记录时 False
        （普通 mode=ro）。"""
        try:
            return _core.world_read_only_frozen(self._record(world), self._write_held(world))
        except Exception:  # noqa: BLE001 — 拿不准就不冻结
            return False

    def _write_stats_cache(self, world: str, db_path: str, tenant: str) -> None:
        """把库里的当前计数写进注册表缓存列。失败只记日志（缓存过期无害，下次刷新再写）。"""
        if self._registry is None:
            return
        try:
            stats = _core.world_stats(db_path, tenant, frozen=self._frozen(world))
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
        """registry.replace_settings_sections(默认世界, config.world_settings())：启动时与每次 POST
        /api/config 保存后调用。config 是默认世界设置的权威来源，所以用整段替换而不是逐叶合并（裁定 R27）：
        改绑到另一个 provider 之后不留旧 provider 的叶子，gist 少了的键同步后也没了。background 仍逐叶合并，
        config 不带 auto_snapshot、ingest（裁定 R16）。替换在 C++ 的注册表锁内一次完成。开着的默认世界引擎
        换上新设置（endpoint 核对按新地址做）；适配器由保存配置的路由按改动的角色热换，与改之前一样。"""
        reg = self._require_registry()
        default = reg.default_world()
        merged = reg.replace_settings_sections(default, self._config.world_settings())
        backend = self._config.vector_backend or "sqlite"
        if backend != "sqlite":
            with self._lock:
                warn, self._vector_warned = not self._vector_warned, True
            if warn:
                # 裁定 R26：世界设置只收 sqlite。默认世界照常按 config 的后端打开，新世界一律 sqlite，
                # 快照、恢复由 C++ 拒绝（snapshot_unsupported_backend）。
                logger.warning("vector_backend=%r applies to the default world only; new worlds use "
                               "sqlite, and snapshots of the default world are refused", backend)
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
                    self._start_workers_or_mark_lost(world, engine)
                engine = None
        if self._registry is not None:
            self._start_thread(_SCHEDULER_THREAD, self._scheduler_loop,
                               scheduler_poll_s, scheduler_first_delay_s)
            self._start_thread(_STATS_THREAD, self._stats_loop, stats_interval_s, stats_interval_s)

    def stop_background(self, timeout_s: float = 5.0) -> None:
        """停调度与统计线程（任何锁之外 join），再停每个开着的世界的 tick / 摄入线程。此后新建的引擎
        不起线程。

        每个世界持它的世界锁 quiesce（与 start_background 一样）：正在首次打开的世界在 _publish 里读到后台
        开着、放进表之后、起线程之前，世界锁一直在它手里；不取世界锁的话，这里可能先 quiesce 了一个还没有
        线程的引擎，随后它才把线程起来，关停之后线程还在跑。"""
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
            with self._world_lock(world):
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

    def begin_drain(self) -> None:
        """关停的第一步（D-P2-6）：每个开着的世界的引擎进 DRAINING，写门转为拒绝，之后才停摄入、停 tick、
        关引擎（顺序见 app 的 lifespan）。每个世界持一份不建引擎的租约调用，正在关闭的世界跳过。引擎的
        begin_drain 不拿引擎锁，在途的长请求挡不住它。关停路径：一个世界出错只记日志，不挡别的世界。"""
        for world in self.open_worlds():
            try:
                with self.lease(world, build=False) as engine:
                    if engine is not None:
                        engine.begin_drain()
                    engine = None
            except WorldSwitchError:
                continue
            except Exception:  # noqa: BLE001 — 关停路径
                logger.exception("cannot drain world %s at shutdown", world)

    def close_world(self, world: str, timeout_s: float | None = None) -> None:
        """关一个世界：等该世界租约归零、停线程、丢引擎、写统计缓存（Review Focus 2）。之后的 lease 会重新
        建引擎。默认世界不在这里关（world_protected；只在关停时由 close_all 关）。没开着时什么都不做。
        超时（缺省 _LEASE_DRAIN_TIMEOUT_S，调用时读，与恢复、删除一致）或线程停不下 → world_busy，引擎
        原样留着。"""
        if world == self.default_world():
            raise WorldSwitchError("world_protected", "the default world closes only at shutdown")
        self._record(world)
        with self._world_lock(world), _ClosingWindow(self, world):
            self._close_engine(world, _LEASE_DRAIN_TIMEOUT_S if timeout_s is None else timeout_s)

    def close_all(self, timeout_s: float | None = None) -> None:
        """关停：此后 lease 一律 world_busy；停后台线程，逐个关闭全部世界（含默认世界）。幂等。"""
        if timeout_s is None:
            timeout_s = _LEASE_DRAIN_TIMEOUT_S
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
            self._workers_lost.discard(world)
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
        """关闭没成（world_busy）、引擎留在表上：把停掉的线程补回来。补不起来就标记 workers-lost，
        下一次租约再试；不能盖掉 world_busy。"""
        if self._background:
            self._start_workers_or_mark_lost(world, engine)

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
        # 裁定 R40：客户端给了设置（或旧字段 auto_snapshot）时，provider 地址表交给 C++，合并、核对继承来的
        # endpoint、写在同一次持注册表锁内完成（spec §8.3）；地址不同就 endpoint_changed、什么都不写。同步默认
        # 世界设置取同一把锁，插不进中间。没给设置时照旧只继承默认世界（它由同步与 config 保持一致）。
        return reg.create(name, label, dict(labels or {}), merged or None, self._clock(),
                          provider_endpoints=self._provider_endpoints() if merged else None)

    def fork_world(self, source: str, new_name: str, *, snapshot_id: str | None = None,
                   labels: dict | None = None, retention_group: str | None = None,
                   reclaimable: bool = False, seed: bool = False,
                   settings: dict | None = None) -> dict:
        """分叉（spec §6.1，C++ world_fork / world_fork_from_connection）：复制、校验、摘要比较、登记全在 C++，
        返回 {"world": 新世界的注册表记录, "digest"}。settings 是部分设置，经 C++ 与建世界同一套合并
        （merge_settings）叠在来源的设置上；继承来的 endpoint 照旧在调用时核对（spec §8.3）。不打开新世界。

        这里只按来源的状态选路、持对的锁（裁定 R5）：
          - 快照为来源：快照文件建好之后不再写，直接 world_fork。
          - 来源开着（有引擎）：world_fork_from_connection 用引擎自己的连接复制（读到它最近一次提交），全程持
            引擎锁，没有在途的写入。
          - 来源关着：全程持来源的世界锁（这期间不会有引擎在它上面建起来），world_fork 只读打开来源库。
        两种世界来源都在世界锁内判断开没开着：建引擎也持世界锁，判断之后状态不会变。世界锁 → 引擎锁是模块
        文档里的锁顺序；这里不持租约（持租约的代码不得阻塞地等世界锁）。之前的关闭留下的 Runtime 还开着来源
        库时 world_busy（关着的来源只读打开可能走 immutable，看不见它的写入）。"""
        reg = self._require_registry()
        # 裁定 R31 M3：与 create_world 同一种传法。客户端给了设置时 provider 地址表交给 C++，在登记的那次持注册表锁内
        # 核对合并后设置里继承来的 endpoint；地址变了就 endpoint_changed，不留文件、不登记。没给设置时不核对。
        opts = {"labels": dict(labels or {}), "retention_group": retention_group or "",
                "reclaimable": bool(reclaimable), "seed": bool(seed), "settings": settings,
                "now": self._clock(),
                "provider_endpoints": self._provider_endpoints() if settings else None,
                # 裁定 R31 M4：来源的向量后端（与快照路由同一取法），是否支持由 C++ 判。
                "vector_backend": self._vector_backend(source)}
        if snapshot_id:
            return _core.world_fork(reg, source, new_name, {**opts, "snapshot_id": snapshot_id})
        rec = self._record(source)                    # world_not_found：未知世界名不建世界锁
        with self._world_lock(source):
            with self._lock:
                engine = self._engines.get(source)
            if engine is None:
                self._refuse_leaked(rec["db_path"])
                return _core.world_fork(reg, source, new_name, opts)
            try:
                with engine._lock:
                    return _core.world_fork_from_connection(
                        reg, engine._rt.adapter.connection(), source, new_name, opts)
            finally:
                engine = None

    def upgrade_seed(self, source: str, new_name: str) -> dict:
        """升级种子（spec §6.2，C++ world_upgrade_seed）：复制旧种子、在副本上跑迁移、登记成新种子，旧种子不动。
        返回 {"world", "digest"}。来源不是种子 world_not_seed（C++ 判）。持来源的世界锁：种子只会被只读打开，
        持锁挡住的是同一世界上的恢复、删除与快照修剪。"""
        reg = self._require_registry()
        self._record(source)                          # world_not_found
        with self._world_lock(source):
            return _core.world_upgrade_seed(reg, source, new_name, self._clock())

    def _provider_endpoints(self) -> dict[str, dict]:
        """{provider 名: {"provider": 种类, "base_url": 地址}}（starling.json 的 provider 注册表），交给 C++ 核对客户端
        写的设置里继承来的 endpoint（裁定 R40、R51）。只给当前值，比较在 C++；不含密钥。遍历前先拍快照（见
        config.provider_endpoints）。"""
        return provider_endpoints(self._config.providers)

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
            # 裁定 R60：删除之前先问会话表。有活句柄（可写或只读）或在途调用时 C++ forget_world 抛 world_in_use，什么都
            # 不删、引擎不动；通过了就清掉它的最后活动与墓碑（同名世界重建后从「没有记录」开始）。窗口里 admit_client_handle
            # 一律 world_busy，所以从这里到删完不会有新句柄。
            if self._sessions is not None:
                self._sessions.forget_world(world, self.now_ms())
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
        """会话清单（C++ world_list_sessions 只读打开该世界的库）。先阻塞取世界锁、再取一份不建引擎的租约，在锁内调 C++
        （裁定 R66，见模块文档「世界锁内调用的 C++」）：C++ 按记录判冻结（held=false），持世界锁期间没有别的路径能在这个库上
        建起写连接。等锁时手里没有租约，不与关闭、恢复（持世界锁等租约）互等。路由在工作线程里调。"""
        reg = self._require_registry()
        self._record(world)                           # world_not_found：未知世界名不建世界锁
        with self._world_lock(world), self.lease(world, build=False):
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

    def update_settings(self, world: str, overrides: dict) -> tuple[dict, list[str]]:
        """把部分设置 overrides 合并进 world 的设置（C++ update_world_settings：逐叶合并、null 清除、校验、核对
        继承来的 endpoint 地址（裁定 R40），读—合并—核对—写在注册表锁内；非法或 endpoint_changed 时什么都不写），
        返回 (实际生效的完整设置, 重嵌入提示)。世界开着时引擎
        换上新设置，并按变了的 endpoint 段重建适配器（engine.apply_world_settings，裁定 R35）。

        默认世界只收 _DEFAULT_WORLD_EDITABLE 里的键，其余 → invalid_settings，什么都不写：那些段归 config，
        下一次同步会覆盖。

        持世界锁（可能等几秒，调用方在工作线程里调）：建引擎（读注册表里的设置）也持世界锁，所以一个正在
        首次打开的世界要么在合并之前建完（这里换上新设置），要么在合并之后才读到设置，不会建出一个带着旧
        设置、又没被这里看见的引擎。关闭同样持世界锁，持锁期间表上的引擎不会被丢。这里不持租约（持租约的
        代码不得阻塞地等世界锁）。"""
        reg = self._require_registry()
        self._record(world)                           # world_not_found：未知世界名不建世界锁
        if world == reg.default_world():
            self._require_default_world_editable(overrides)
        with self._world_lock(world):
            self._refuse_while_held(world)            # 裁定 R73：被客户端占着写入时 world_held，什么都不写
            # 裁定 R40：合并、核对继承来的 endpoint、写都在 C++ 的同一次持锁内；endpoint_changed 时什么都不写。
            merged = reg.update_settings(world, overrides,
                                         provider_endpoints=self._provider_endpoints())
            # 按记录算生效的设置（裁定 R68）：可回收世界的引擎后台照旧全关，与详情同一口径（R33）。
            settings = self._effective_settings(reg.get(world), merged)
            with self._lock:
                engine = self._engines.get(world)
            warnings: list[str] = []
            if engine is not None:
                warnings = engine.apply_world_settings(settings)
            engine = None
        return settings, warnings

    def reconfirm_endpoints(self, world: str, *, config_lock=None) -> tuple[dict, list[str]]:
        """重新确认端点（裁定 R53）：把 starling.json 里各 provider 此刻的 endpoint（不含密钥，与 world_settings 同一套
        字段规则）交给 C++ reconfirm_world_endpoints，世界里引用某个 provider 的角色整段换成它此刻的 endpoint，
        provider 已被删掉的角色解绑。返回 (实际生效的完整设置, 重嵌入提示)。开着的世界按变了的角色热换适配器
        （apply_world_settings，与 PUT 设置同一条路，裁定 R35），但只在嵌入的模型或维度变了时重嵌入（裁定 R58）：只换了
        地址的嵌入只重建适配器，解绑的嵌入换成占位，两者都不删向量。

        默认世界拒绝，码与 PUT 改它的 endpoint 相同（invalid_settings）：它的端点归 config，同步时自动跟上。
        持世界锁，理由同 update_settings。

        config_lock（app.state.config_lock，裁定 R59 N-3）：各 provider 此刻的 endpoint 表在这把锁内拷出。POST /api/config
        持着它原地改 provider 条目（种类、地址、密钥依次写），不持锁拷就可能拷到改了一半的条目（新种类配旧地址）。锁顺序见
        模块文档：config_lock 排在世界锁之前，所以这里只在取世界锁之前短暂持它，拷完即放，不在持世界锁时取。"""
        reg = self._require_registry()
        self._record(world)                           # world_not_found
        if world == reg.default_world():
            raise WorldSwitchError(
                "invalid_settings", "the default world's endpoints come from the dashboard config")
        with config_lock if config_lock is not None else contextlib.nullcontext():
            current = {name: provider_endpoint_settings(name, prov or {})
                       for name, prov in list(self._config.providers.items())}
        with self._world_lock(world):
            self._refuse_while_held(world)            # 裁定 R73
            merged = reg.reconfirm_endpoints(world, current)
            settings = self._effective_settings(reg.get(world), merged)
            with self._lock:
                engine = self._engines.get(world)
            warnings: list[str] = []
            if engine is not None:
                # 裁定 R58：只有嵌入的模型或维度变了才重嵌入；只换地址、或嵌入角色被解绑，向量原样留着。
                warnings = engine.apply_world_settings(settings, reconfirm=True)
            engine = None
        return settings, warnings

    @staticmethod
    def _require_default_world_editable(overrides: dict) -> None:
        for section, value in overrides.items():
            allowed = _DEFAULT_WORLD_EDITABLE.get(section)
            if allowed is None or not isinstance(value, dict) or set(value) - allowed:
                raise WorldSwitchError(
                    "invalid_settings",
                    f"the default world's {section!r} settings come from the dashboard config")

    def set_auto_snapshot(self, world: str, on_session: bool, interval_hours: int) -> dict:
        """改一个世界的自动快照配置（C++ 在注册表锁内合并），返回生效的配置 {"on_session", "interval_hours"}（按看板的
        打开方式算，与列表行、详情同一口径，裁定 R38：只读打开的世界恒为全关，存着的值照写）。
        world_not_found / invalid_auto_snapshot 由 C++ 抛。开着的引擎换上新的设置引用。"""
        reg = self._require_registry()
        self._record(world)                           # world_not_found：未知世界名不建世界锁
        # 裁定 R42：持世界锁，与 update_settings 串行。否则它可能落在 update_settings 写完注册表、还没把新设置
        # 交给引擎之间，先把合并后的设置（含新 endpoint）换上引擎，apply_world_settings 随后看不出变化、不重建
        # 适配器。路由在工作线程里调，不持租约，按锁顺序可以等世界锁。
        with self._world_lock(world):
            self._refuse_while_held(world)            # 裁定 R73
            _core.world_set_auto_snapshot(reg, world, on_session, interval_hours)
            rec = reg.get(world)
            settings = _core.world_effective_settings(rec["settings"], record=rec)
            with self._lock:
                engine = self._engines.get(world)
            if engine is not None:
                engine.set_world_settings(settings)
            engine = None
        return dict(settings["background"]["auto_snapshot"])

    def auto_snapshot_status(self) -> dict:
        """{"failures", "last_error", "last_error_at"}：自池建起以来所有世界的自动快照失败次数、最近一次
        失败的错误码与时刻。busy 与跳过不算失败。"""
        with self._lock:
            return {"failures": self._auto_failures, "last_error": self._auto_last_error,
                    "last_error_at": self._auto_last_error_at}

    def _vector_backend(self, world: str) -> str:
        self._record(world)                           # world_not_found
        return self._vector_store(world)[0]

    # ───────────────────────── 恢复（只停这一个世界，spec §7.2） ─────────────────────────

    def rollback(self, world: str, snapshot_id: str) -> dict:
        """把 world 恢复到快照 snapshot_id（丢掉快照之后的一切）。替换、校验、归档都在 C++
        （world_rollback）；被替换的库留在 <世界目录>/archive/。返回 {"world", "snapshot_id", "archived",
        "reopened", "digest"}（reopened：恢复前开着、恢复后已重新打开；digest：C++ 在恢复后的库上算的摘要，裁定 R26，
        缺表的库为 None）。

        只停被恢复的世界：持它的世界锁、置它的关闭标志、等它的租约（含只读检视）归零、停它的线程、丢它
        的引擎并确认 Runtime 已回收，然后才调 C++；别的世界的租约与写入照常（需求 §7 第 1b 条）。

        抛错：C++ 的码是 _core.WorldStoreError，按库有没有被替换分两类（C++ rollback_world 第 12 步）：
          - 库原样不动：snapshot_not_found / snapshot_corrupt / world_tenant_mismatch / insufficient_disk /
            snapshot_unsupported_backend / world_in_use，以及替换之前的 snapshot_failed；
          - 库已替换（C++ 的 WorldStoreError.replaced 为 True，裁定 R31 M5）：快照文件登记之后被改过
            （restore_digest_mismatch），或第 12 步「库已替换、算不出摘要」的 snapshot_failed。照替换成功的出口刷
            统计缓存、重开引擎，再原样抛出（异常上另挂 pool_replaced=True，路由据此广播）；重开失败报
            rollback_reopen_failed（链到 C++ 的错误，同样挂 pool_replaced=True）。
        另有 world_busy（在途请求 _LEASE_DRAIN_TIMEOUT_S 内不结束、线程停不下、Runtime 回收不了或之前的关闭
        留下的 Runtime 还开着这个库，库未动）；world_open_failed（库未动，但重开引擎失败）；
        rollback_reopen_failed（库已替换，重开失败）。后两种引擎不在表上，下一次 lease 重新建。
        动引擎之前先跑 C++ 的只读预检（world_check_rollback）：不改输入重试也过不了的错误直接抛出，引擎、
        线程、库都不动，也不进关闭窗口。"""
        reg = self._require_registry()
        rec = self._record(world)
        backend = self._vector_backend(world)
        _core.world_check_rollback(reg, world, snapshot_id, vector_backend=backend)
        with self._world_lock(world), _ClosingWindow(self, world):
            # 裁定 R25、R61 (3)：有活句柄（可写或只读，C++ world_has_live_handles）时 world_held，在进 C++ 回滚、动引擎之前；
            # 在途调用由下面的租约排干等到（每次客户端调用全程持一份租约，裁定 R63）。窗口里不会有新句柄。
            if self._sessions is not None and self._sessions.world_has_live_handles(world, self.now_ms()):
                raise WorldSwitchError("world_held", f"{world} has open client handles")
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
                result = self._rollback_in_cpp(reg, world, snapshot_id, backend)
            except Exception as exc:
                if not _replaced_by_rollback(exc):
                    self._reopen_after_rollback(world, was_open, exc)   # 库未动；原样抛出
                    raise
                # 库已换成快照文件此刻的内容（摘要对不上或算不出）：与成功出口一样刷缓存、在新库上重开，再
                # 把 C++ 的错误原样抛出（硬错误，spec §7.2）。重开失败时 rollback_reopen_failed 链到它。两种出口都
                # 在异常上挂 pool_replaced=True：路由按它广播 world_rolled_back（裁定 R32），不按错误码列表。
                exc.pool_replaced = True
                self._write_stats_cache(world, rec["db_path"], rec["tenant"])
                self._reopen_after_rollback(world, was_open, None, replaced_by=exc)
                raise
            # 审查 T5-1：库已换成快照，注册表里的统计缓存还是回滚前（关闭时写的）的计数。照关闭的写法刷一次，
            # 关着的世界在列表上立刻显示回滚后的计数。失败只记日志（缓存是尽力而为）。
            self._write_stats_cache(world, rec["db_path"], rec["tenant"])
            reopened = self._reopen_after_rollback(world, was_open, None)
        return {"world": world, "snapshot_id": snapshot_id,
                "archived": bool(result["archive_path"]), "reopened": reopened,
                "digest": result["digest"]}

    @staticmethod
    def _rollback_in_cpp(reg, world: str, snapshot_id: str, backend: str) -> dict:
        """调 C++ world_rollback 的唯一一处（测试替换它来注入 C++ 的失败）。"""
        return _core.world_rollback(reg, world, snapshot_id, vector_backend=backend)

    def _reopen_after_rollback(self, world: str, was_open: bool, cause: Exception | None, *,
                               replaced_by: Exception | None = None) -> bool:
        """恢复的每个出口（持世界锁、在关闭窗口内）：恢复前开着就按注册表重建引擎、起线程（打开时会跑
        迁移，旧 schema 的快照会补齐）。返回世界此刻是否开着（按实际结果，不按恢复前的状态）：恢复前关着、
        或恢复途中池关停了（建好的引擎不发布、关掉），都是 False。重建失败：引擎不在表上，下一次 lease
        重新建。码按库有没有被替换来分：cause 为 None（库已替换：成功，或 replaced_by 给出的「替换之后才
        失败」）→ rollback_reopen_failed（有 replaced_by 时链到它）；其余出口 → world_open_failed（链到 cause）。
        重建成功则返回，由调用方抛 cause / replaced_by 或返回结果。"""
        if not was_open:
            return False
        try:
            engine = self._open_engine(world, self.context(world))
        except Exception as exc:
            if cause is None:
                detail = f"rollback applied, but the world cannot be reopened: {exc}"
                logger.error("world rollback (%s): %s", world, detail)
                reopen_failed = WorldSwitchError("rollback_reopen_failed", detail)
                reopen_failed.pool_replaced = True          # 库已替换（裁定 R32）
                raise reopen_failed from (replaced_by or exc)
            detail = f"{cause}; reopening the world also failed: {exc}"
            logger.error("world rollback (%s): %s", world, detail)
            raise WorldSwitchError("world_open_failed", detail) from cause
        # 起线程失败只标记 workers-lost（裁定 R23）。恢复途中池关停了：不发布，关掉它（世界留在关着的状态，
        # 恢复本身照常算成功或照常抛 cause）。
        if self._publish(world, engine):
            engine = None
            return True
        box, engine = [engine], None
        self._discard_unpublished(box, self.context(world).db_path)
        return False

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
            # 只把格式化好的文本交给日志：留着 LogRecord 的 handler 拿到异常对象（在 args 里）就经它的回溯钉住
            # 栈帧，帧的 f_back 一路通到调用钩子的引擎方法（self 就是引擎），之后关闭这个世界就等不到旧
            # Runtime 回收（同 _start_workers_or_mark_lost）。非领域异常照旧带回溯。
            domain = isinstance(exc, (_core.WorldStoreError, WorldSwitchError))
            code = (exc.code if isinstance(exc, WorldSwitchError)
                    else _store_code(exc) if domain else "internal_error")
            logger.warning("auto snapshot (%s, %s) failed: %s", world, trigger_kind, str(exc),
                           exc_info=not domain)
            exc = None
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
