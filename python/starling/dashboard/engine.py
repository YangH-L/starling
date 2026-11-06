"""DashboardEngine — a configurable, partially-hot-swappable engine stack.

The remember / recall / tick / working-set command logic lives in
`starling._memory_core.MemoryCore` (single implementation shared with
`starling.Memory`, so the two surfaces cannot drift). This engine keeps only
the dashboard-specific policy: the chat LLM and the embedder are built from
the world's settings (`WorldContext.settings`, merged in C++) plus the live
provider registry (`providers`, keys only), so the embedder used to WRITE vectors
(EmbeddingWorker) and READ them (SemanticRetriever) stay the same.

记忆服务（Task 5，spec §8、§10.3）：一个引擎服务一个世界。构造收 `WorldContext`（库、租户、已合并的
设置、摄入分区目录）与 `providers`（starling.json 的 provider 注册表，只用来按 provider_name 取
api_key 与当前 base_url）。直接传 `DashboardConfig` 的旧写法照样可用（裁定 R17）：经
`WorldContext.from_config` 适配，providers 取 `config.providers`。继承来的 endpoint 每次调用前
都经 C++ `world_require_endpoint_unchanged` 核对 provider 此刻的种类与地址（spec §8.3，裁定 R51），改了
就拒绝（endpoint_changed），不把密钥发到另一个地址。

The SQLite runtime/connection is built ONCE at construction (db_path is fixed)
and never rebuilt — config changes only swap the llm and/or rebuild the
embedder-dependent components, reusing the single writer connection (no WAL
double-writer). API keys are injected via a transient os.environ swap at
adapter-build time (the binding does not expose api_key; Config.from_env()
captures it at build), so chat and embedder may use different providers/keys.

Phase 5 (P3.c1): a backpressure sampler (HealthSampler + MetricsGatherer) runs
after each background tick.  The C++ sampler is pure; debounce + DRAINING-
suppress live in the host (_sample_backpressure).  Lock order (L8): engine-
RLock → supervisor-mutex; NEVER the reverse.
"""
from __future__ import annotations

import collections
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dataclasses import dataclass, field

from starling import _core
from starling import runtime as _runtime
from starling._memory_core import LLMNotConfigured, MemoryCore
from starling.dashboard.config import provider_endpoint
from starling.dashboard.ingest_filter import chunk_dialogue, clean_turns, clean_turns_ts


@dataclass(frozen=True)
class WorldContext:
    """一个世界打开引擎需要的全部输入，从注册表记录推出（WorldPool.context）。

    settings 是 C++ 补齐默认值后的完整 WorldSettings dict（字段见 world_settings.hpp），不含密钥。
    ingest_spool_dir 是 <摄入根目录>/<world_ingest_partition(...)>（裁定 R4）。

    vector_backend / vector_store_path 不从 settings 取（裁定 R26）：注册表里的设置只收 sqlite，默认世界
    「原地」沿用 config 的向量后端与路径（zvec 的默认世界也照常打开，只是拍快照会被 C++ 拒绝），其余世界
    一律 sqlite、没有路径。

    read_only（spec §6.3、§6.4）：C++ world_decide_open_mode 判为 "ro" 的世界（只读世界、种子、可回收世界）。
    引擎只读打开库（不跑迁移、不改日志模式），不起后台线程，写入类方法一律 world_read_only。"""
    name: str
    dir_id: str
    db_path: str
    tenant: str
    agent: str
    settings: dict = field(default_factory=dict)
    ingest_spool_dir: str = ""
    is_default: bool = True
    vector_backend: str = "sqlite"
    vector_store_path: str = ""
    read_only: bool = False

    @classmethod
    def from_config(cls, config) -> "WorldContext":
        """DashboardConfig → 默认世界的上下文（裁定 R17 的适配器：现有 DashboardEngine(cfg) 的写法）。
        只做字段搬运：设置取 config.world_settings() 再由 C++ 补默认值，分区目录名由 C++ 给。
        DashboardConfig.world_settings() 没写出的 auto_snapshot 由 C++ 补默认值；直接构造的引擎
        不挂自动快照钩子，用不到它。向量后端与路径直接取 config，不经设置（裁定 R26）。"""
        settings = _core.world_effective_settings(config.world_settings())
        spool = config.ingest_spool_root() / _core.world_ingest_partition(
            True, config.tenant, "default")
        return cls(name="default", dir_id="default", db_path=config.db_path,
                   tenant=config.tenant, agent=config.agent, settings=settings,
                   ingest_spool_dir=str(spool), is_default=True,
                   vector_backend=config.vector_backend,
                   vector_store_path=config.vector_store_path or "")


# ── Phase 5 backpressure sampler defaults (L1 / L2) ─────────────────────────
#
# c1 enables outbox_lag + runtime_event_loop_lag_ms ONLY.  The other 5 metrics
# stay DISABLED (zero ≠ healthy; the sampler skips them).
#
# Recommended defaults (documented here as the single source of truth):
#   outbox_lag.threshold              = 100  (sequences)
#   runtime_event_loop_lag_ms.threshold = 200 (ms; proxy for background-tick overload, L6)
_DEFAULT_OUTBOX_LAG_THRESHOLD: int = 100
_DEFAULT_LOOP_LAG_THRESHOLD_MS: float = 200.0
# Flapping damping (L3): how many CONSECUTIVE same-verdict evaluate() results
# must accumulate before note_health is called.
_DEFAULT_DEBOUNCE_TICKS: int = 3


def _build_default_sampler_config(
    *,
    outbox_lag_threshold: int = _DEFAULT_OUTBOX_LAG_THRESHOLD,
    loop_lag_threshold_ms: float = _DEFAULT_LOOP_LAG_THRESHOLD_MS,
) -> "_core.HealthSamplerConfig":
    """Construct a HealthSamplerConfig enabling outbox_lag + runtime_event_loop_lag_ms."""
    cfg = _core.HealthSamplerConfig()
    cfg.outbox_lag.enabled = True
    cfg.outbox_lag.threshold = outbox_lag_threshold
    cfg.runtime_event_loop_lag_ms.enabled = True
    cfg.runtime_event_loop_lag_ms.threshold = loop_lag_threshold_ms
    # subscriber_failure_rate, extraction_queue_depth, projection_lag_seconds,
    # vector_delete_lag, erased_evidence — all DISABLED in c1 (L1).
    return cfg


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


logger = logging.getLogger(__name__)


def _dead_letter_text(exc: BaseException) -> str:
    """摄入 job 进 failed/ 时 .error 旁车写的文本。endpoint_changed 只写码（裁定 R54，spec §16：不留地址）；
    其余照旧写异常文本的前 1000 个字符。"""
    if isinstance(exc, _core.WorldStoreError):
        code = _core.world_store_error_code(exc)
        if code == "endpoint_changed":
            return code
    return str(exc)[:1000]


def _tick_did_work(stats: dict) -> bool:
    """tick 这一轮有没有做事。stage_timings_ms 总在（9 项）、DEGRADED 时 stages_skipped 非空，两者都不算
    （否则每个空闲的 DEGRADED 轮都会误发心跳，3b L8）。"""
    return any(v for k, v in stats.items() if k not in ("stage_timings_ms", "stages_skipped"))


# SQLite 拒绝在 mode=ro 连接上写入时的报错（SQLITE_READONLY 的 sqlite3_errstr）。C++ 的 SqliteError 到 Python
# 是 RuntimeError，只有文本可认。
_SQLITE_READONLY_TEXT = "readonly database"


def _world_read_only(world: str) -> Exception:
    """只读世界上的写入（spec §6.3）：WorldStoreError("world_read_only")，路由回 409。只点名世界。"""
    return _core.WorldStoreError(f"world_read_only: world '{world}' is opened read-only")


# Routes run engine calls in worker threads (anyio to_thread), so the transient
# os.environ mutation below needs mutual exclusion or two concurrent adapter
# builds could capture each other's keys.
_ENV_SWAP_LOCK = threading.Lock()


@contextmanager
def _env_swap(api_key: str, base_url: str, *,
              key_env: str = "OPENAI_API_KEY", base_env: str = "OPENAI_BASE_URL"):
    """Temporarily set <key_env>/<base_env> so the adapter's from_env() captures them.

    Parametric over the env-var names so the Anthropic path can inject
    ANTHROPIC_API_KEY/ANTHROPIC_BASE_URL through the same transient swap.
    Serialized via _ENV_SWAP_LOCK — os.environ is process-global state.
    """
    with _ENV_SWAP_LOCK:
        saved = {k: os.environ.get(k) for k in (key_env, base_env)}
        try:
            os.environ[key_env] = api_key
            if base_url:
                os.environ[base_env] = base_url
            else:
                os.environ.pop(base_env, None)  # clear stale value
            yield
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def _service_openai_key() -> str | None:
    """服务进程自己的 OPENAI_API_KEY（裁定 R22）。在 _ENV_SWAP_LOCK 下读：别的线程正在 _env_swap 里给
    provider P 建适配器时，环境变量暂时是 P 的密钥，不持锁读会把它当成服务级密钥，发到客户端给的地址。
    显式端点的世界只用这个函数取密钥。"""
    with _ENV_SWAP_LOCK:
        return os.environ.get("OPENAI_API_KEY")


def _build_chat_adapter(llm_cfg: dict):
    """Provider factory: anthropic → AnthropicAdapter (native Messages API),
    everything else → OpenAIAdapter (OpenAI-compatible: openai/azure/ollama/
    groq/deepseek/openrouter/vllm/lmstudio/custom). Key injected via env-swap."""
    provider = (llm_cfg.get("provider") or "openai").lower()
    if provider == "anthropic":
        with _env_swap(llm_cfg["api_key"], llm_cfg.get("base_url", ""),
                       key_env="ANTHROPIC_API_KEY", base_env="ANTHROPIC_BASE_URL"):
            cfg = _core.AnthropicAdapterConfig.from_env()
            if llm_cfg.get("model"):
                cfg.model = llm_cfg["model"]
            if llm_cfg.get("base_url"):
                cfg.base_url = llm_cfg["base_url"]
            return _core.AnthropicAdapter(cfg)
    with _env_swap(llm_cfg["api_key"], llm_cfg.get("base_url", "")):
        cfg = _core.OpenAIAdapterConfig.from_env()
        if llm_cfg.get("model"):
            cfg.model = llm_cfg["model"]
        if llm_cfg.get("base_url"):
            cfg.base_url = llm_cfg["base_url"]
        # 生成参数(可选):未设置则保持 C++ 默认(max_tokens 4096 / 超时 60s / 重试 3)。
        for key in ("max_tokens", "timeout_ms"):
            if llm_cfg.get(key):
                setattr(cfg, key, int(llm_cfg[key]))
        if llm_cfg.get("max_retries") is not None:
            cfg.max_retries = int(llm_cfg["max_retries"])
        if llm_cfg.get("enable_thinking") is not None:
            cfg.enable_thinking = bool(llm_cfg["enable_thinking"])
        return _core.OpenAIAdapter(cfg)


def _build_embed_adapter(emb_cfg: dict):
    with _env_swap(emb_cfg["api_key"], emb_cfg.get("base_url", "")):
        cfg = _core.OpenAIEmbeddingConfig.from_env()
        if emb_cfg.get("model"):
            cfg.model = emb_cfg["model"]
        if emb_cfg.get("base_url"):
            cfg.base_url = emb_cfg["base_url"]
        if emb_cfg.get("dim"):
            cfg.dim = int(emb_cfg["dim"])
        return _core.OpenAIEmbeddingAdapter(cfg)


class _TenantCleared(Exception):
    """remember 的 prepare 段发现租户在本次调用开始后被 clear_memory 清空。

    只用于 ingest worker:它在 claim 前快照 `_clear_generation`,逐块 remember 时
    带上;清空后的块不再写入(否则会把清空前就排队的旧会话重新灌回来)。"""


class ClearConfirmMismatch(ValueError):
    """clear_memory 的 confirm 与当前租户名不符:零写入,路由回 400。"""


class SessionSnapshotDeferred(Exception):
    """按会话的自动快照这一轮没拿到该世界的世界锁（WorldPool 的会话钩子抛出，W4）。

    钩子在写入之前调用，所以抛出时这个摄入 job 还什么都没写：摄入 worker 把它放回 spool，
    不计重试次数，下一轮再来。快照失败不抛这个（照常摄入，只记录失败）。"""


def _key_fingerprint(api_key: str | None) -> str | None:
    """密钥的 sha256 十六进制摘要（裁定 R52），没有密钥时 None。只在引擎内存里比较用：不记日志、不进注册表、
    不进 HTTP 响应。"""
    if not api_key:
        return None
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def endpoint_api_key(endpoint: dict, providers: dict) -> str | None:
    """一个世界 endpoint 调用时用的密钥（spec §8.3）：provider_name 非空时取 providers 里该 provider 当前
    保存的密钥，为空（客户端显式给的端点）时取服务进程的 OPENAI_API_KEY。引擎建适配器与世界详情的
    key_set 都经这里（引擎经 endpoint_key_from，传的是 provider 的快照），规则只有一处。"""
    name = endpoint.get("provider_name") or ""
    return endpoint_key_from(endpoint, providers.get(name) if name else None)


def endpoint_key_from(endpoint: dict, prov: dict | None) -> str | None:
    """同 endpoint_api_key，但 provider 条目由调用方给（引擎给的是 dict(prov) 快照，裁定 R57；None = provider 已删掉）。"""
    if endpoint.get("provider_name"):
        return (prov or {}).get("api_key")
    return _service_openai_key()


# 世界设置里 endpoint 与 _build_chat_adapter / _build_embed_adapter 读的 provider dict 同名的字段。
_ADAPTER_KEYS = ("provider", "model", "base_url", "dim", "max_tokens", "timeout_ms",
                 "max_retries", "enable_thinking", "thinking_type")
# 三个对话类角色的适配器在 MemoryCore 上的属性名（嵌入另走 set_embedder）。
_ROLE_ATTR = {"extraction": "llm", "chat": "chat_llm", "consolidation": "consolidation_llm"}
# 打开世界时核对没过的角色（裁定 R57）：适配器不建，_built_endpoints 记这个哨兵。它与任何 endpoint 都不等，所以下一次
# 用到该角色时走「建时的 endpoint 与设置不同」那条路：重新拍快照、核对，过了才建，没过就 endpoint_changed。只读，不改。
_REFUSED_ENDPOINT = {"refused_at_open": True}
# _key_stale 的 snap 缺省值：自己拍一份快照。
_FRESH = object()
# NORM-gist 阈值里绑定层要整数的几项（ReplayScheduler.run_sleep / run_idle 的签名）。世界设置把阈值
# 存成 double（C++ map<string,double>），所以 4.0 要还原成 4 才能传进去。
_INT_GIST_KEYS = ("min_holders", "min_replay_count", "entity_gist_enabled")


class DashboardEngine:
    def __init__(self, ctx, providers: dict | None = None) -> None:
        # Embedded facade runs the reduced capability set (testing_helper_marker
        # test-only; engram_per_record_key deferred M0.4+KMS); the C++ supervisor
        # is built with embedded=True (in _build_local_store_sqlite_runtime), so
        # preflight reaches READY without any Python-side global relaxation.
        # Serializes all engine entry points: routes call them from worker
        # threads (anyio to_thread) and SQLite has a single writer connection.
        # RLock because working_set() re-enters recall().
        self._lock = threading.RLock()
        # 旧写法 DashboardEngine(config)（裁定 R17）：经 WorldContext.from_config 适配，providers 取
        # config.providers（同一个 dict，不拷贝，改 provider 立刻可见）。_cfg 只留给测试与 create_app
        # 的注入校验读；引擎内部一律读 _ctx / _settings / _providers。
        config = None
        if not isinstance(ctx, WorldContext):
            config = ctx
            # 左脑 lane：开启但未标定阈值即拒绝构造（spec §4.4 硬约束），在打开库之前。
            validate = getattr(config, "validate_source_lane", None)
            if callable(validate):
                validate()
            ctx = WorldContext.from_config(config)
            if providers is None:
                providers = config.providers
        if not ctx.tenant:
            raise ValueError("world tenant must not be empty")
        if not ctx.ingest_spool_dir:
            # 空串会变成 Path("")，摄入线程就去轮询当前目录。
            raise ValueError("world ingest spool directory must not be empty")
        self._cfg = config
        self._ctx = ctx
        # 已合并的设置（WorldPool.sync_default_settings 会经 set_world_settings 换成新的）。只读引用：
        # 换的时候整体替换，不原地改。
        self._settings = ctx.settings
        self._providers = providers if providers is not None else {}
        self._db_path = ctx.db_path
        # 写方法成功之后调用一次（WorldPool 设置，用于统计去抖广播）。在引擎锁之外调用。
        self.on_commit = None
        self._embedder_live = False             # rebuild_embedder 设置
        # 裁定 R54：最近一次 tick 的嵌入端点是否被拒（endpoint_changed → 只跳过嵌入阶段），与后台循环上次记日志时
        # 的状态。只在 tick（持 self._lock）里写；后台循环按两者是否不同决定记不记一次日志。
        self.embedding_refused = False
        self._embedding_refusal_logged = False
        # 裁定 R41：每个角色的适配器是按哪个 endpoint 建的（建与重建时记下；None = 不是从设置建的，例如测试
        # 直接 eng.llm = fake，或角色没有 endpoint）。用到适配器之前核对的是这个，不是 _settings 里的：设置
        # 被换了却没重建适配器时，按旧 endpoint 建的适配器（带着密钥指着旧地址）不能被新设置的核对放行。
        self._built_endpoints: dict[str, dict | None] = {}
        # 裁定 R52：每个角色的适配器建时用的密钥的指纹（_key_fingerprint）。与 _built_endpoints 同时记、同时丢。
        # 地址核对通过之后再比 provider 此刻的密钥指纹，不同（换了密钥、删了密钥、新加了密钥）就重建该角色。
        self._built_key_fps: dict[str, str | None] = {}
        # 只读世界（ctx.read_only，spec §6.3）：库经 open_read_only 打开，不跑迁移；schema 落后照常打开。
        # 落后与否在打开时记一次（只读库不迁移，可写库刚迁移到最新，之后都不会变），列表与详情据此标出
        # schema_behind，不必再碰这条连接。
        self.read_only = bool(ctx.read_only)
        self.schema_behind = False
        build = (_runtime._build_read_only_sqlite_runtime if self.read_only
                 else _runtime._build_local_store_sqlite_runtime)
        rt = build(Path(ctx.db_path))
        try:
            self.schema_behind = rt.adapter.schema_behind     # 判定在 C++（裁定 R31 M5）
            rt.start()
            self._rt = rt   # the live Runtime (holds the C++ RuntimeSupervisor); the
                            # host drain reaches the supervisor through begin_drain().
            self._init_core(rt)
        except BaseException as exc:
            # 构造失败时不能让 Runtime（C++ 写连接）挂在异常的回溯上活着：调用方（引擎池）可能留着
            # 这个异常，同时为同一个库再建一个引擎，就是一个库上两条写连接。已经返回的栈帧清掉局部
            # 变量，本帧手动丢引用。
            import traceback
            traceback.clear_frames(exc.__traceback__)
            self._core = None
            self._rt = None
            rt = None
            raise

    def _init_core(self, rt) -> None:
        settings = self._settings
        lane = settings["source_lane"]
        self._core = MemoryCore(rt, agent=self._ctx.agent, tenant_id=self._ctx.tenant,
                                adapter_name="dashboard", source_prefix="dash-",
                                vector_backend=self._ctx.vector_backend,
                                vector_store_path=(self._ctx.vector_store_path or None))
        # 左脑 lane 读写两端：必须在 rebuild_embedder 之前登记，embedder 一到位
        # 就按它是否与标定一致决定 lane 是否生效（不一致时读写都不碰原文）。
        if lane["enabled"]:
            self._core.enable_source_lane(
                tau_source=lane["tau_source"], model=lane["model"], dim=lane["dim"],
                k_source=lane["k_source"], chunk_byte_budget=lane["chunk_byte_budget"])
        # P2.o 后台维护 tick(写→读闭环的离线半边);start_background_tick 启动。
        self._tick_thread: threading.Thread | None = None
        self._tick_stop: threading.Event | None = None
        # Phase 5 — backpressure sampler (L1/L2): created once, process-lifetime.
        # outbox_lag + runtime_event_loop_lag_ms ONLY in c1 (other 5 DISABLED).
        # Debounce state (L3): a deque of the last `_debounce_ticks` verdicts.
        # Lock order (L8): engine-RLock → supervisor-mutex (via note_health). NEVER reverse.
        self._sampler = _core.HealthSampler(_build_default_sampler_config())
        self._gatherer = _core.MetricsGatherer()
        self._debounce_ticks: int = _DEFAULT_DEBOUNCE_TICKS
        self._debounce_window: collections.deque = collections.deque(
            maxlen=self._debounce_ticks)
        # Roles → adapters, from the world's own settings (copied from the default
        # world at creation, spec §8.2) + the live provider registry for keys.
        # No endpoint in the settings → {} → no api_key → None / stub, the same as
        # an unbound role today (裁定 R15: no fallback to the default world).
        # consolidation 是世界设置的第四个 endpoint（裁定 R25），与另外三个同样建、同样核对地址。
        # 裁定 R57：每个角色按一份 provider 快照建，建之前对这份快照核对；没过的角色不建（_open_role）。
        self._open_role("extraction")
        self._open_role("chat")                       # 没绑 chat 时按抽取的 endpoint 建
        self._open_role("consolidation")                                     # #38-C role consumer
        self.set_gist_thresholds(self._settings.get("gist_thresholds") or {})  # #38-C v2 threshold surface
        self._open_role("embedding")
        # dogfood sub-project A (Task 3): spool ingest worker — drains this
        # world's partition (WorldContext.ingest_spool_dir; jobs written by the
        # SessionEnd hook via scripts/ingest_session.py) into remember() calls.
        # Tests inject a tmp spool dir via `eng._ingest_spool = tmp_path / "spool"`.
        self._ingest_spool = Path(self._ctx.ingest_spool_dir)
        self._ingest_thread: threading.Thread | None = None
        self._ingest_stop: threading.Event | None = None
        # W4 按会话的自动快照：hook(session_id)，某个会话第一次写入之前调用。由 WorldPool 在
        # 起摄入线程之前挂上（attach_hooks）；没有注册表（:memory: 等）时保持 None。
        # 返回即可继续写入；抛 SessionSnapshotDeferred 表示这一轮没拿到世界锁、job 要放回 spool。
        self.session_snapshot_hook = None
        # 最近一次 quiesce() 没停下的线程名（"ingest" / "tick"），供调用方在
        # ingest_busy 里点名；每次 quiesce 开头重置，所以不会读到上一轮的陈旧值。
        self.quiesce_stuck: tuple[str, ...] = ()
        self._ingest_remember_ms_total = 0      # observability: see _ingest_drain_once
        self._ingest_throttle_s = 5.0           # gap after each processed job (dashboard breathing room)
        self._ingest_max_attempts = 5           # bounded retry before dead-lettering to failed/
        # clear_memory 每次 +1。ingest worker 在 claim 前快照,逐块 remember 时带上;
        # 代数变了 = 清空发生在这个 job 被 claim 之后,该 job 作废(见 _TenantCleared)。
        self._clear_generation = 0

        # dogfood 子项 B:embed 深度采样序列。HOST 独立 sqlite(唯一写者=采样器,
        # 天然单写者;与 core 的 dashboard.db 无共享写连接)。非记忆 schema,不进
        # C++ MigrationRunner。
        self._metrics_db_path = Path(self._db_path).parent / "metrics.db"
        self._metrics_retention_days = 30

    # ── 世界设置 → 适配器配置（只做字段搬运；继承判定在 C++） ─────────────────

    @property
    def world(self) -> str:
        return self._ctx.name

    @property
    def tenant(self) -> str:
        return self._ctx.tenant

    def _require_writable(self) -> None:
        """写入类方法的第一步：只读引擎（spec §6.3）一律 world_read_only，什么都不做。"""
        if self.read_only:
            raise _world_read_only(self._ctx.name)

    @contextmanager
    def _read_only_rejections(self):
        """读方法在只读引擎上万一碰到写入（C++ 写门的 WriteGateRejected，或 mode=ro 连接拒绝写入的 SQLite 错误），
        统一报 world_read_only，不让它变成 500。可写引擎原样放行。"""
        if not self.read_only:
            yield
            return
        try:
            yield
        except _core.WriteGateRejected:
            raise _world_read_only(self._ctx.name) from None
        except RuntimeError as exc:
            if _SQLITE_READONLY_TEXT not in str(exc):
                raise
            raise _world_read_only(self._ctx.name) from None

    def _provider_snapshot(self, ep: dict | None) -> dict | None:
        """裁定 R57：ep 引用的 provider 条目此刻的一份拷贝（dict(prov)）；ep 不引用 provider（客户端显式给的端点、没有
        endpoint）或 provider 已删掉时 None。POST /api/config 原地改 provider 条目（依次写种类、地址、密钥，只持
        config_lock，不持引擎锁），所以一次建 / 重建里的地址核对、密钥指纹、适配器配置一律读这一份，不各自去读共享的
        dict：否则核对读到旧种类、密钥读到新密钥，新密钥就按旧种类建出来，发到旧种类的默认地址。dict(...) 在 C 里持
        GIL 一次拷完。"""
        name = (ep or {}).get("provider_name") or ""
        if not name:
            return None
        prov = self._providers.get(name)
        return dict(prov) if prov is not None else None

    def _endpoint_cfg(self, role: str, prov=_FRESH) -> dict:
        """role（extraction / embedding / chat / consolidation）的适配器该按的 endpoint（_source_endpoint：chat 没绑定时是
        抽取的）→ _build_*_adapter 读的 provider dict。密钥取 prov（provider 条目的快照，缺省现拍一份，见
        _provider_snapshot）里的；provider_name 为空（客户端显式给的端点）时取服务进程的 OPENAI_API_KEY（spec §8.3，
        与 make_openai_llm 一致）。没有 endpoint 返回 {}。这里不核对：核对在 _build_role（建之前，对同一份快照）与
        每次调用之前（_require_endpoint_unchanged）。"""
        ep = self._source_endpoint(role)
        if not ep:
            return {}
        if prov is _FRESH:
            prov = self._provider_snapshot(ep)
        cfg = {key: ep[key] for key in _ADAPTER_KEYS if key in ep}
        key = endpoint_key_from(ep, prov)
        if key:
            cfg["api_key"] = key
        return cfg

    def _check_endpoint(self, ep: dict | None, role: str, prov: dict | None) -> None:
        """一个 endpoint（provider_name 非空时）对 provider 条目的快照 prov（_provider_snapshot；None = provider 已删掉）
        的种类与 base_url 做 C++ 核对（world_require_endpoint_unchanged，裁定 R51）：provider 不在、或种类与地址任一
        不同就抛 WorldStoreError("endpoint_changed")。比较全在 C++，这里只把快照归一成 {provider, base_url}。"""
        if not ep or not ep.get("provider_name"):
            return
        current = provider_endpoint(prov) if prov is not None else None
        _core.world_require_endpoint_unchanged(ep, current, role)

    def _source_endpoint(self, role: str) -> dict | None:
        """role 的适配器此刻该按哪个 endpoint 建：_settings[role]；chat 没绑定时回退到抽取的（与构造时一致）。"""
        if role == "chat":
            return self._settings.get("chat") or self._settings.get("extraction")
        return self._settings.get(role)

    def _require_endpoint_unchanged(self, role: str, prov=_FRESH) -> None:
        """用到 role 的适配器之前调（持 self._lock）。prov 是调用方已拍的 provider 快照（缺省现拍一份）；下面的核对、
        密钥指纹、重建都用这一份（裁定 R57）。

        裁定 R41：核对的是这个适配器建时的 endpoint，不是 _settings 里的。两者不同（设置被换了、适配器没重建，或打开
        世界时核对没过、没建）时按 _settings 的 endpoint 重建（_build_role：先对快照核对，过了才建）；没过就
        endpoint_changed，不重建。两者相同时核对一次。适配器不是从设置建的（测试直接装的替身，见 llm 的 setter）时按
        _settings 核对。调用方在这之后再读适配器（可能刚换过）。"""
        ep = self._source_endpoint(role)
        if prov is _FRESH:
            prov = self._provider_snapshot(ep)
        if role in self._built_endpoints and self._built_endpoints[role] != ep:
            self._build_role(role, prov)             # 新设置的地址也得对，才按它重建
            return
        self._check_endpoint(ep, role, prov)
        # 裁定 R52（spec §8.3「调用时用该 provider 当前保存的密钥」）：地址没变，但密钥换了或删了，就按快照里的密钥重建。
        # 删了密钥时重建出来的是 None（嵌入是占位），调用方照常报 llm_not_configured。
        if self._key_stale(role, prov):
            self._build_role(role, prov)

    def _key_stale(self, role: str, prov=_FRESH) -> bool:
        """role 的适配器建时的密钥指纹与 provider 快照 prov（缺省现拍一份）里密钥的指纹不同（裁定 R52）。不是从设置建的
        适配器（测试替身）没有记录，永不算过期。"""
        if role not in self._built_key_fps:
            return False
        ep = self._source_endpoint(role)
        # 与 _endpoint_cfg 同一取法：没有 endpoint 的角色不带密钥（不取服务进程的 OPENAI_API_KEY）。
        if not ep:
            current = None
        else:
            if prov is _FRESH:
                prov = self._provider_snapshot(ep)
            current = endpoint_key_from(ep, prov)
        return self._built_key_fps[role] != _key_fingerprint(current)

    def _build_role(self, role: str, prov=_FRESH, *, reembed: bool = False) -> str | None:
        """按 _settings 建 / 重建 role 的适配器（持 self._lock）。全部建法（构造、用到时重建、reload_adapters、
        apply_world_settings、重嵌入之前）都经这里（裁定 R57）：provider 条目只拍一份快照（prov，缺省现拍），适配器
        配置与密钥指纹都从它来，紧挨着建适配器之前再对这同一份快照做一次 C++ 核对，没过就 endpoint_changed、什么都
        不换。chat 没绑定时按抽取的 endpoint 建。reembed 只对嵌入有意义（rebuild_embedder），返回它的提示。"""
        ep = self._source_endpoint(role)
        if prov is _FRESH:
            prov = self._provider_snapshot(ep)
        cfg = self._endpoint_cfg(role, prov)
        self._check_endpoint(ep, role, prov)
        if role == "extraction":
            self.set_llm(cfg)
        elif role == "chat":
            self.set_chat(cfg)
        elif role == "consolidation":
            self.set_consolidation(cfg)
        else:
            return self.rebuild_embedder(cfg, reembed=reembed)
        return None

    def _open_role(self, role: str) -> None:
        """构造时建 role 的适配器（_build_role）。核对没过（provider 在世界关着时换了种类或地址、或被删掉）就不建：
        世界照样打开、照样能检视，抽取类角色为 None、嵌入用占位，建时的 endpoint 记成 _REFUSED_ENDPOINT，下一次用到
        时按 _settings 重新核对（还是没过就 endpoint_changed，tick 只跳过嵌入阶段）。"""
        try:
            self._build_role(role)
        except _core.WorldStoreError as exc:
            if _core.world_store_error_code(exc) != "endpoint_changed":
                raise
            if role == "embedding":
                self._embedder_live = False
                self._core.set_embedder(_core.StubEmbeddingAdapter(8))
            else:
                setattr(self._core, _ROLE_ATTR[role], None)
            self._built_endpoints[role] = _REFUSED_ENDPOINT
            self._built_key_fps[role] = None

    def _prepare_role(self, role: str):
        """用到 role（extraction / chat / consolidation）的世界适配器之前调（持 self._lock），返回此刻该用的适配器
        （可能为 None：没有可用的密钥）。provider 快照只拍一份，判断与核对、重建都用它（裁定 R57）。

        - 适配器建时的 endpoint 与 _settings 的不同（设置换了、没重建）：不论现在的适配器是不是 None，都先按
          _settings 核对，过了就重建（裁定 R41；修复轮 3 小项 2：从没绑定换成带密钥的 endpoint 时，下一次用到就
          建出来，而不是报 llm_not_configured）。
        - 相同：有适配器才核对（没有适配器就没有密钥会被发出去，照旧由调用方报 llm_not_configured）。
        - 核对失败抛 endpoint_changed，不重建。"""
        attr = _ROLE_ATTR[role]
        ep = self._source_endpoint(role)
        prov = self._provider_snapshot(ep)
        stale = role in self._built_endpoints and self._built_endpoints[role] != ep
        # 密钥指纹变了（裁定 R52）也要走一遍：没有适配器、provider 刚加上密钥时，按新密钥建出来。
        if stale or getattr(self._core, attr) is not None or self._key_stale(role, prov):
            self._require_endpoint_unchanged(role, prov)
        return getattr(self._core, attr)

    def _require_embedding_unchanged(self) -> None:
        """用到嵌入的调用（召回、tick、工作集、检索规划、对话的召回段）之前调。占位嵌入（没有密钥）不发
        请求，不核对；但设置已换成另一个 endpoint 时照样按新设置核对并重建（裁定 R41）。快照只拍一份（裁定 R57）。"""
        ep = self._settings.get("embedding")
        prov = self._provider_snapshot(ep)
        stale = ("embedding" in self._built_endpoints
                 and self._built_endpoints["embedding"] != ep)
        if self._embedder_live or stale or self._key_stale("embedding", prov):
            self._require_endpoint_unchanged("embedding", prov)

    def set_world_settings(self, settings: dict) -> None:
        """WorldPool.sync_default_settings 之后换上新合并的设置（默认世界）。只换引用；适配器由
        调用方（POST /api/config）按改动的角色热换（reload_adapters），这里不重建，与改 config 之前的
        行为一致。"""
        with self._lock:
            self._settings = settings

    def reload_adapters(self, roles, *, gist: bool = False, reembed: bool = True) -> list[str]:
        """按当前设置（self._settings）与 provider 注册表重建 roles 里的适配器（extraction / chat /
        consolidation / embedding），gist 为真时重新装上阈值；与构造时（_init_core）同一套取法（_build_role）。POST
        /api/config（按改动的角色，含只改了密钥的 provider）与 PUT /api/worlds/{name}/settings
        （apply_world_settings，按改动的段）都经这里。reembed 为假时换了嵌入也只换适配器，向量不动（重新确认端点，
        裁定 R58）。返回重嵌入的提示（嵌入 provider 拒绝时，非致命）。"""
        roles = set(roles)
        warnings: list[str] = []
        with self._lock:
            # 裁定 R57：每个角色只拍一份 provider 快照，下面的核对与建都用它。
            snaps = {role: self._provider_snapshot(self._source_endpoint(role)) for role in roles}
            # 裁定 R38：从设置重建的每个适配器先核对它将按哪个 endpoint 建（_source_endpoint；chat 没绑定时是
            # 抽取的），任何一个地址改了就抛 endpoint_changed，什么都不换。建适配器本身不发请求，但嵌入一换就会
            # 重嵌入、用这个密钥发出去。
            for role in roles:
                self._check_endpoint(self._source_endpoint(role), role, snaps[role])
            # 没绑 chat 的世界用抽取的 endpoint 对话（与构造时一致，_source_endpoint）。
            for role in ("extraction", "chat", "consolidation", "embedding"):
                if role in roles:
                    warn = self._build_role(role, snaps[role], reembed=reembed)
                    if warn:
                        warnings.append(warn)
            if gist:
                self.set_gist_thresholds(self._settings.get("gist_thresholds") or {})
        return warnings

    def apply_world_settings(self, settings: dict, *, reconfirm: bool = False) -> list[str]:
        """换上一个世界的新设置（PUT /api/worlds/{name}/settings 之后，C++ 已合并好），并按与旧设置相比
        变了的段重建适配器：不留旧 endpoint 的适配器（继承来的地址核对按新设置做，见
        _require_endpoint_unchanged）。chat 没绑定时跟着抽取变。background 只换引用：tick 间隔与摄入开关
        在下一次打开这个世界时生效，自动快照的配置池每次从注册表现读。返回重嵌入的提示。

        reconfirm（重新确认端点，裁定 R58）：嵌入段变了时，只有嵌入的模型或维度真的变了才重嵌入，判定在 C++
        world_embedding_needs_reembed。只换了地址就只重建适配器，向量原样留着；嵌入角色被解绑（provider 删掉了）换成
        占位嵌入，不重嵌入，向量也留着（召回降级到重新绑定为止）。为假（PUT 设置）时嵌入段一变就重嵌入，与之前相同。"""
        with self._lock:
            old, self._settings = self._settings, settings
            changed = {role for role in ("extraction", "embedding", "chat", "consolidation")
                       if old.get(role) != settings.get(role)}
            if "extraction" in changed and not settings.get("chat"):
                changed.add("chat")
            gist = old.get("gist_thresholds") != settings.get("gist_thresholds")
            reembed = (not reconfirm
                       or _core.world_embedding_needs_reembed(old.get("embedding"),
                                                              settings.get("embedding")))
            try:
                return self.reload_adapters(changed, gist=gist, reembed=reembed)
            except _core.WorldStoreError:
                # 新设置里有继承地址已变的 endpoint（裁定 R38，核对在换任何适配器之前）：换回旧设置，
                # 引擎原样。C++ 在写注册表时已于同一次持锁内核对过（裁定 R40），这里是纵深防御。
                self._settings = old
                raise

    def _notify_commit(self) -> None:
        """写方法成功之后通知 WorldPool（统计去抖）。回调异常只记日志：写已经成功了。"""
        callback = self.on_commit
        if callback is None:
            return
        try:
            callback()
        except Exception:  # noqa: BLE001 — 通知失败不能把成功的写变成失败
            logger.exception("on_commit callback failed")

    # `engine.llm` stays read/write: tests and offline harnesses inject a
    # FakeLLMAdapter directly (`eng.llm = fake`).
    @property
    def llm(self):
        return self._core.llm

    @llm.setter
    def llm(self, value) -> None:
        # 直接装进来的适配器（测试替身）不是从设置建的：不记 endpoint，核对按 _settings 做（裁定 R41）。
        with self._lock:
            self._core.llm = value
            self._built_endpoints.pop("extraction", None)
            self._built_key_fps.pop("extraction", None)

    def _record_built(self, role: str, cfg: dict) -> None:
        """记下 role 的适配器刚按哪个 endpoint 建（_source_endpoint 的此刻值，见 _built_endpoints）与建时用的密钥的
        指纹（cfg 的 api_key，见 _built_key_fps）。"""
        self._built_endpoints[role] = self._source_endpoint(role)
        self._built_key_fps[role] = _key_fingerprint(cfg.get("api_key"))

    def set_llm(self, llm_cfg: dict) -> None:
        with self._lock:
            self._core.llm = (_build_chat_adapter(llm_cfg)
                              if llm_cfg.get("api_key") else None)
            self._record_built("extraction", llm_cfg)

    def set_chat(self, chat_cfg: dict) -> None:
        """Build the chat-role adapter (converse). Unbound chat falls back to
        the extraction provider's config (DashboardConfig.chat()), so this is
        O(1) reference swap like set_llm — no re-embed."""
        with self._lock:
            self._core.chat_llm = (_build_chat_adapter(chat_cfg)
                                   if chat_cfg.get("api_key") else None)
            self._record_built("chat", chat_cfg)       # 没绑 chat 时记下的是抽取的 endpoint

    def set_consolidation(self, cons_cfg: dict) -> None:
        """#38-C: build the consolidation-role adapter that judges NORM gists in
        offline replay. Unbound (no api_key) → None → deterministic gist (no LLM
        judge). O(1) reference swap; the NORM-gist prompt + orchestration live in
        the C++ core, this only injects the adapter."""
        with self._lock:
            self._core.consolidation_llm = (_build_chat_adapter(cons_cfg)
                                            if cons_cfg.get("api_key") else None)
            self._record_built("consolidation", cons_cfg)

    def set_gist_thresholds(self, thresholds: dict) -> None:
        """#38-C v2 threshold surface: override the NORM-gist K / T / confidence floor /
        semantic similarity_threshold (0 = off) / entity_gist_enabled (1 = on) used by
        offline replay (run_idle/run_sleep). Empty/missing keys → the C++ defaults. O(1)
        value swap; the clustering + gating logic lives in the C++ core — this only
        injects the knobs. 世界设置把阈值存成 double（C++ map<string,double>），整数类的几项
        （_INT_GIST_KEYS）在这里还原成 int：ReplayScheduler 的绑定只收整数。"""
        thresholds = {k: (int(v) if k in _INT_GIST_KEYS and isinstance(v, float)
                          and v.is_integer() else v)
                      for k, v in (thresholds or {}).items()}
        with self._lock:
            self._core.gist_thresholds = {
                k: thresholds[k]
                for k in ("min_holders", "min_replay_count", "min_confidence",
                          "similarity_threshold", "entity_gist_enabled")
                if thresholds.get(k) is not None
            } if thresholds else {}

    def rebuild_embedder(self, emb_cfg: dict, *, reembed: bool = True) -> str | None:
        """Swap the embedder; if reembed, re-process the backlog. Returns a
        human-readable warning when the immediate re-embed was rejected (the
        embedder is still swapped + rows deferred to the background tick), else
        None — so the config-save handler can tell the user instead of silently
        succeeding with broken embeddings."""
        with self._lock:
            # 带密钥 = 真适配器（会把密钥发往某个地址），之后用到嵌入的调用要先核对继承来的地址。
            self._embedder_live = bool(emb_cfg.get("api_key"))
            emb = (_build_embed_adapter(emb_cfg) if emb_cfg.get("api_key")
                   else _core.StubEmbeddingAdapter(8))
            self._core.set_embedder(emb)
            self._record_built("embedding", emb_cfg)
            if reembed:
                return self._reembed()
        return None

    def _replace_sampler(self, *,
                          outbox_lag_threshold: int = _DEFAULT_OUTBOX_LAG_THRESHOLD,
                          loop_lag_threshold_ms: float = _DEFAULT_LOOP_LAG_THRESHOLD_MS,
                          debounce_ticks: int = _DEFAULT_DEBOUNCE_TICKS) -> None:
        """Test seam: replace the sampler + gatherer with a custom config.

        Called from tests to inject low thresholds without rebuilding the engine.
        Not thread-safe (call before start_background_tick).
        """
        self._sampler = _core.HealthSampler(
            _build_default_sampler_config(
                outbox_lag_threshold=outbox_lag_threshold,
                loop_lag_threshold_ms=loop_lag_threshold_ms,
            )
        )
        self._gatherer = _core.MetricsGatherer()
        self._debounce_ticks = debounce_ticks
        self._debounce_window = collections.deque(maxlen=debounce_ticks)

    def _sample_backpressure(self, tick_started_at: float,
                              scheduled_interval: float) -> None:
        """Run the backpressure sample cycle under the engine lock (L3/L6/L7/L8).

        Steps (LOCKED block authoritative):
          L7 suppress  — skip entirely if health is DRAINING or UNREADY.
          gather        — read outbox_lag_sequence from the live DB.  On failure,
                          log + return (leave health unchanged; no spurious transition).
          L6 tick-delay — set snapshot.runtime_event_loop_lag_ms to the actual
                          elapsed time since the tick was scheduled (a host-overload
                          proxy; NOT asyncio loop lag — L6).
          evaluate      — pure C++ sampler produces a HealthDecision.
          L3 debounce   — enqueue the verdict; call note_health ONLY when the last
                          `_debounce_ticks` consecutive verdicts agree on the same
                          target_status.  Resets when the verdict changes.

        Lock order (L8): self._lock (engine-RLock) → _rt._sup mutex (via note_health).
        The reverse order NEVER occurs: note_health/begin_drain hold no engine lock.
        """
        with self._lock:
            # L7: suppress when DRAINING or UNREADY — don't fight the state machine.
            current_health = self._rt.health()
            if (current_health == _core.RuntimeHealth.DRAINING
                    or current_health == _core.RuntimeHealth.UNREADY):
                return

            # gather — wrap in try/except (DB busy, missing table, etc.).
            try:
                snapshot = self._gatherer.gather(self._rt.adapter)
            except Exception:  # noqa: BLE001 — gather failure leaves health unchanged
                logger.exception("backpressure gather failed; health unchanged")
                return

            # L6: background-tick-delay as the loop-lag proxy.
            # actual_elapsed = now - tick_started_at (seconds); convert to ms.
            # Subtract scheduled_interval to get the *over* delay (clamped >=0).
            # Example: if interval=60s and tick fires after 60.5s, delay=500ms.
            now_mono = time.monotonic()
            actual_elapsed_ms = (now_mono - tick_started_at) * 1000.0
            loop_lag_ms = max(0.0, actual_elapsed_ms - scheduled_interval * 1000.0)
            snapshot.runtime_event_loop_lag_ms = loop_lag_ms

            # evaluate — pure C++ function (no I/O, no mutex).
            decision = self._sampler.evaluate(snapshot)

            # L3 debounce: track last N verdicts; fire only when all N agree.
            verdict = decision.target_status
            # Reset deque if the verdict changed (maxlen handles window trimming).
            if self._debounce_window and self._debounce_window[-1] != verdict:
                self._debounce_window.clear()
            self._debounce_window.append(verdict)
            if (len(self._debounce_window) == self._debounce_ticks
                    and all(v == verdict for v in self._debounce_window)):
                # N consecutive same verdicts → apply to the supervisor.
                self._rt.note_health(decision)

    def sample_embed_depth(self) -> None:
        """采一个 embed 队列深度样本 → metrics.db(host,append-only + retention)。
        由后台 tick `_loop` 每轮无条件调用(紧邻 _sample_backpressure,不经
        did_work 门控——backlog/embedded 是绝对状态快照,空闲/卡死轮也要采,
        否则恰好在「backlog 卡住/增长」这个要回答的场景丢样本)。异常吞掉记
        log,绝不杀 tick(保活)。"""
        # closing() 而非裸 `with sqlite3.connect(...) as c`:Connection.__exit__ 只
        # 提交/回滚事务,**不 close 连接** —— fd 一直挂着。本方法每轮 tick 无条件跑,
        # 于是每 tick 泄漏 3 个 fd(db + wal + 偶尔 shm),约 3 小时撑满 launchd 的 256
        # fd 上限 → accept() 得 EMFILE → 新连接被内核 RST,而事件循环照常轮询(表面
        # 是「进程活着、端口在听,却 Connection reset」)。观测器实测 66 次真实卡死、
        # fd 峰值 255 坐实此因。同仓库 queries.open_ro 与本文件 _reembed 都是 close 的,
        # 唯独此处漏了。
        try:
            # 只读 dashboard.db 算 backlog(未 embed 的 statement 数)+ embedded 数
            with closing(sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)) as ro:
                backlog = ro.execute(
                    "SELECT COUNT(*) FROM statements s LEFT JOIN statement_vectors v "
                    "ON v.stmt_id=s.id AND v.tenant_id=s.tenant_id "
                    "WHERE s.tenant_id=? AND v.stmt_id IS NULL",
                    (self._core.tenant,)).fetchone()[0]
                embedded = ro.execute(
                    "SELECT COUNT(*) FROM statement_vectors WHERE tenant_id=? AND status='embedded'",
                    (self._core.tenant,)).fetchone()[0]
            with closing(sqlite3.connect(str(self._metrics_db_path))) as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS embed_depth_samples ("
                    " ts TEXT NOT NULL, backlog INTEGER NOT NULL, embedded INTEGER NOT NULL)")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_embed_depth_ts ON embed_depth_samples(ts)")
                conn.execute("INSERT INTO embed_depth_samples(ts,backlog,embedded) VALUES(?,?,?)",
                             (_now_iso(), backlog, embedded))
                # cutoff 必须与插入的 ts 同一格式(isoformat + 微秒)——之前用
                # strftime 整秒会在同一墙钟秒内和 _now_iso() 的微秒串做字典序
                # 比较（"."排在"Z"之前），把边界行提前删掉一点点。
                cutoff = (datetime.now(timezone.utc)
                          - timedelta(days=self._metrics_retention_days)
                          ).isoformat().replace("+00:00", "Z")
                conn.execute("DELETE FROM embed_depth_samples WHERE ts < ?", (cutoff,))
                conn.commit()
        except Exception:  # noqa: BLE001 — 保活:采样失败不杀 tick
            logger.exception("embed-depth sampler failed")

    def _reembed(self) -> str | None:
        """Clear this tenant's stored vectors (dim/space changed) and re-embed.

        The re-embed makes live embedding-provider calls. A rejected/unreachable
        provider (e.g. a chat model bound to the embedding role → permanent_400)
        must NOT fail the config save with a 500: the new embedder is already
        swapped in, and the cleared rows stay in the embedding backlog for the
        background tick (with its own retry/failure tracking) to re-process once
        a valid embedding role is configured. So a failure here is logged and
        swallowed, mirroring the background tick's own non-fatal handling — and
        returned as a warning string so the save can surface it (silent success
        with broken embeddings is worse than a clear heads-up)."""
        # 裁定 R38：在删任何向量之前核对嵌入的继承地址；改了就抛 endpoint_changed，一行不删。
        self._require_embedding_unchanged()
        if self.read_only:
            # 只读世界（spec §6.3）：库只读打开，向量一行不删；换上的嵌入只用于查询。
            return "re-embed skipped: the world is opened read-only; vectors NOT cleared"
        if not self._rt.adapter.write_admitted():
            # 前台写门 follow-up(P3.c write-gate):这个 config-save 重嵌是绕过 C++ 核心的
            # 裸 sqlite 写(DELETE statement_vectors + tick_one_batch),核心门管不到 → host-side
            # gate,复用 PR #45 的 adapter 钩子。DRAINING/UNREADY 时跳过(不 DELETE),返回
            # warning(贴合本方法 str|None 契约,config-save 展示)。
            return ("re-embed skipped: runtime is draining; vectors NOT cleared "
                    "(writes are rejected during shutdown)")
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("DELETE FROM statement_vectors WHERE tenant_id = ?",
                         (self._core.tenant,))
            conn.commit()
        finally:
            conn.close()
        try:
            self._core.worker.tick_one_batch(_now_iso())
        except Exception as exc:
            logger.exception("config re-embed failed; deferred to the background tick")
            return (f"embedding provider rejected the config ({exc}); embeddings "
                    "will not generate until a valid embedding model is bound to "
                    "the embedding role (embedding models differ from chat models)")
        return None

    @property
    def llm_configured(self) -> bool:
        return self._core.llm is not None

    def _resolve_extraction(self, provider: str | None):
        """锁内调用:解析本轮抽取 adapter 为局部引用(锁外 extract 段使用)。
        取代旧 _role_override('llm', …) 的全局 slot 换入换出——拆锁后换全局
        slot 会被并发轮/后台 tick 读到错误模型(同 _resolve_chat)。"""
        if provider:
            # 裁定 R57：一份快照，种类、地址、密钥出自同一时刻（POST /api/config 原地改这个条目）。
            cfg = dict(self._providers.get(provider) or {})
            if not cfg.get("api_key"):
                raise _LLMNotConfigured(
                    f"selected provider '{provider}' is not configured")
            return _build_chat_adapter(cfg)
        # 用世界自己的抽取适配器：先核对继承来的地址（endpoint_changed）。没有适配器时不核对，由调用方
        # 报 llm_not_configured（没有适配器就没有密钥会被发出去）。
        return self._prepare_role("extraction")              # 可能按新设置重建了（裁定 R41）

    def _check_clear_generation(self, generation: int) -> None:
        """代数过期（claim 之后发生了 clear_memory）抛 _TenantCleared。短暂持 self._lock 读代数。"""
        with self._lock:
            if generation != self._clear_generation:
                raise _TenantCleared(
                    "tenant memory was cleared after this job was claimed")

    def _retain_transcript_turns(self, lines, *, job: dict, project: str,
                                 generation: int) -> int:
        """把 transcript 的逐话轮写进左脑来源通道（形态 S §6.3）。

        这是**通用摄入 API 的第一个消费者**：``retain_source_turns`` 本身不绑定
        transcript，任何可信后端按 v3 契约提供逐话轮数组即可。

        说话人映射必须与既有 ``remember`` 的 holder 约定一致
        （holder="self"、interlocutor="claude-code:<project>"）——否则左脑写下的
        在场集合与右脑抽取的语句归属对不上，可见性会被静默放宽或收窄。

        返回写入的 engram 数；clear 语义与 remember 一致：代数过期即作废。

        代数检查与 ``retain_source_turns`` 在同一个 ``with self._lock:`` 块里（Codex C1，W4 之前就有的
        竞态）：clear_memory 也持这把锁清库、前移代数，所以它要么在检查之前（代数过期 →
        _TenantCleared，一行不写），要么在写入之后（清空把刚写的来源行一并删掉）。检查与写入之间
        放开锁，清空就可能落在两者之间，清空前的 transcript 会在清空之后写回库里。
        ``retain_source_turns`` 只做本地校验与写行（向量行写 pending，嵌入线程之后再补），没有网络
        调用，持锁的代价与 remember 的 prepare/commit 段相同。

        按会话的自动快照（W4，设计稿 §4.3）：第一次代数检查通过之后、上面那个持锁块之前调
        ``session_snapshot_hook(session_id)``，此时这个 job 还什么都没写，快照里没有这个会话。钩子
        抛 ``SessionSnapshotDeferred`` 时原样外抛，``_ingest_drain_once`` 把 job 放回 spool。只有
        lane 开启时才走到这里，所以 lane 关闭时没有按会话的快照（设计如此）。
        """
        turns = clean_turns_ts(lines)
        if not turns:
            return 0
        session_id = job.get("session_id") or ""
        hook = self.session_snapshot_hook
        if hook is not None and session_id:
            # 提前查一次：钩子之前已经清空过，就不必为一个注定作废的 job 拍快照。持锁块里那次
            # 才是权威的检查。
            self._check_clear_generation(generation)
            # 锁顺序：钩子（非阻塞）取该世界的世界锁，它排在 engine._lock 之前，所以调钩子时不得持有
            # self._lock——上面的提前检查已经放开了它，下面的持锁块在钩子返回之后才取锁。
            hook(session_id)

        speaker_of = {"user": self._ctx.agent or "self",
                      "assistant": f"claude-code:{project}"}
        listeners = sorted(set(speaker_of.values()))
        payload = [
            {"speaker": speaker_of.get(role, role),
             "text": text,
             "session_id": session_id,
             "turn_index": turn_index,
             "audience": listeners,        # 对话双方都在场；说话人必在听众里
             "provenance": "source_time",
             "turn_id": None,
             "observed_at": event_time}
            for role, text, event_time, turn_index in turns
        ]
        turns_json = json.dumps(payload, ensure_ascii=False)
        # 代数检查与写入同一把锁、中间不放开（见 docstring）。快照可能要几秒（VACUUM INTO），这期间的
        # 清空也由这次检查让 job 作废。
        with self._lock:
            self._check_clear_generation(generation)   # RLock：同一线程重入
            out = _core.retain_source_turns(
                self._rt.adapter, self._ctx.tenant, listeners, turns_json,
                payload[0]["observed_at"])
        return len(json.loads(out).get("engram_refs") or ())

    def remember(self, text: str, *, holder=None, interlocutor=None, now=None,
                 provider: str | None = None, clear_generation: int | None = None) -> dict:
        """三相落锁(方案2 + option B 收尾):belief+gf+episodic extraction 段全在
        锁外,prepare/commit 持锁。episodic LLM 已出锁(commit 内 episodic 只做纯
        DB persist)。provider 解析为局部 adapter 传入 extract/commit(避拆锁后全局
        slot 竞态,同 _converse_phased)。

        clear_generation:ingest worker 传 claim 时的 `_clear_generation` 快照;
        与当前值不同 = 该 job 被 claim 后发生过 clear_memory,抛 _TenantCleared,
        不再写入。锁外抽取期间被清空的情况由 C++ commit 守卫兜底(source_missing)。"""
        self._require_writable()
        now = now or datetime.now(timezone.utc)
        with self._lock:                                    # ① 短:写门 + engram
            if clear_generation is not None and clear_generation != self._clear_generation:
                raise _TenantCleared("tenant memory was cleared after this job was claimed")
            extraction = self._resolve_extraction(provider)
            if extraction is None:   # #1:fail-fast 在写 engram 前(provider 未给且抽取角色未绑)
                raise _LLMNotConfigured(
                    "remember requires an llm adapter (bind the extraction role "
                    "or pass a configured provider)")
            bundle = self._core.remember_prepare(
                text, holder=holder, interlocutor=interlocutor, now=now)
        extracted = self._core.remember_extract(bundle, llm=extraction)  # ② 锁外 LLM
        with self._lock:                                    # ③ persist + episodic + 写
            out = self._core.remember_commit(bundle, extracted, llm=extraction)
        self._notify_commit()                               # 锁外：统计去抖（WorldPool）
        return out

    def _resolve_chat(self, provider: str | None):
        """锁内调用:解析本轮 chat 适配器为局部引用(锁外使用)。取代旧
        _role_override('chat_llm', …) 的全局 slot 换入换出——锁外生成期间
        换 slot 会被并发轮读到错误模型。"""
        # prepare 的召回要嵌入查询词（embedding），回合末的抽取走世界自己的 extraction（converse_commit 用
        # self._core.llm）：不论本轮选没选别的 provider，这两个都还是世界的适配器，都先核对（裁定 R43：选了
        # provider 的分支以前直接返回，世界的抽取与嵌入不经核对就被用到）。
        self._prepare_role("extraction")
        self._require_embedding_unchanged()
        if provider:
            # 裁定 R57：一份快照，种类、地址、密钥出自同一时刻（POST /api/config 原地改这个条目）。
            cfg = dict(self._providers.get(provider) or {})
            if not cfg.get("api_key"):
                raise _LLMNotConfigured(
                    f"selected provider '{provider}' is not configured")
            return _build_chat_adapter(cfg)
        # 生成走世界的 chat（没有就回退到抽取）。
        return self._prepare_role("chat") or self._core.llm

    def _converse_phased(self, message: str, *, on_token=None, on_status=None,
                         holder=None, interlocutor=None, k: int = 6, now=None,
                         provider: str | None = None) -> dict:
        """converse/converse_stream 共享实现:三段化落锁,与 remember(也在
        LLM 调用期间持锁)不同——生成段(网络/延时)现在在锁外运行,只有
        prepare(recall + 组 prompt)和 commit(抽取 + 写)持锁,后台 tick 等
        并发调用不再被一次生成段整体阻塞。C++ 侧 no-write-txn-across-generate
        的结构保证不变(remember 的写事务仍是 commit 阶段,在 generate 之后)。

        prepare 与 commit 必须共用同一时刻(幂等键/召回哈希含 created_at)。"""
        self._require_writable()                            # 回合末要抽取、写入
        now = now or datetime.now(timezone.utc)
        with self._lock:                                    # ① 短:读 + prompt
            chat = self._resolve_chat(provider)
            prepared = self._core.converse_prepare(
                message, holder=holder, interlocutor=interlocutor, k=k, now=now)
        if on_status is not None and prepared.grounded:
            # 先引用后作答：模型先写一段用户看不到的证据块，流式过滤器会把它挡住，
            # 用户那边是一段空白等待。先告诉前端「正在核对记忆」，让这段等待看得见。
            on_status("grounding")
        gen_resp = self._core.generate_stream(                               # ② 锁外
            chat, prepared.prompt, on_token, grounded=prepared.grounded)
        with self._lock:                                    # ③ 抽取 + 写
            out = self._core.converse_commit(
                message, prepared, gen_resp, holder=holder,
                interlocutor=interlocutor, k=k, now=now)
        if out.get("statement_ids"):
            self._notify_commit()
        return out

    def converse(self, message: str, *, holder=None, interlocutor=None,
                 k: int = 6, now=None, provider: str | None = None) -> dict:
        return self._converse_phased(message, holder=holder,
                                     interlocutor=interlocutor, k=k, now=now,
                                     provider=provider)

    def converse_stream(self, message: str, *, on_token, on_status=None, holder=None,
                        interlocutor=None, k: int = 6, now=None,
                        provider: str | None = None) -> dict:
        """Streaming converse: on_token(delta:str) fires per token during the
        generation phase (WS token stream, running outside the lock). Returns
        the same dict shape as converse() (reply / statement_ids / remember_ok /
        gen_* cost); on_token only relays the reply incrementally.

        on_token runs on the C++ worker thread (the binding re-acquires the GIL
        per delta), so it must be cheap and thread-safe — the WS bridge hands
        each delta to the event loop via call_soon_threadsafe and never touches
        the DB or socket from inside the callback.

        on_status(status:str), if given, fires at most once, before generation:
        "grounding" when this turn writes a hidden evidence block first (the
        stream stays silent until the block closes). Same threading rules as
        on_token — it runs off the event loop."""
        return self._converse_phased(message, on_token=on_token, on_status=on_status,
                                     holder=holder, interlocutor=interlocutor, k=k,
                                     now=now, provider=provider)

    def recall(self, query: str, *, perspective="first_person", k=10, mode="semantic",
               holder=None) -> list:
        with self._lock, self._read_only_rejections():
            self._require_embedding_unchanged()   # 语义召回要嵌入查询词
            return self._core.recall(query, perspective=perspective, k=k, mode=mode,
                                     holder=holder)

    def tick(self, now: str) -> dict:
        """后台维护一轮。嵌入阶段走 embedding 端点，先核对继承来的地址（占位嵌入不核对）。核对不过
        （endpoint_changed，裁定 R54）只跳过嵌入阶段（C++ tick_all 的 skip_embed，stages_skipped 记 "embed"），
        承诺触发、grounding、回放、投影、出箱照常跑；是否被拒记在 embedding_refused（后台循环据此按状态变化记一次
        日志）。做了事（任一计数非零）就通知一次 on_commit（与 _loop 的 did_work 同一判据）。只读引擎
        world_read_only（维护要写库）。"""
        self._require_writable()
        with self._lock:
            skip_embed = not self._embedding_usable()
            stats = self._core.tick(now, skip_embed=skip_embed)
        if _tick_did_work(stats):
            self._notify_commit()
        return stats

    def _embedding_usable(self) -> bool:
        """持 self._lock。嵌入端点此刻能不能用：_require_embedding_unchanged 抛 endpoint_changed → False，其余照常
        抛出。结果记在 embedding_refused。"""
        try:
            self._require_embedding_unchanged()
        except _core.WorldStoreError as exc:
            if _core.world_store_error_code(exc) != "endpoint_changed":
                raise
            self.embedding_refused = True
            return False
        self.embedding_refused = False
        return True

    def _log_embedding_state(self) -> None:
        """后台循环每轮 tick 之后调：embedding_refused 与上次记日志时不同才记一次（变为被拒记 warning，恢复记
        info），不逐轮记。只点名世界，不带地址与密钥（spec §16）。"""
        refused = self.embedding_refused
        if refused == self._embedding_refusal_logged:
            return
        self._embedding_refusal_logged = refused
        if refused:
            logger.warning("world %s: embedding endpoint changed (endpoint_changed); background "
                           "embedding is paused until the endpoint is re-confirmed, other "
                           "maintenance continues", self._ctx.name)
        else:
            logger.info("world %s: embedding endpoint usable again; background embedding resumed",
                        self._ctx.name)

    def forget(self, ids, *, now=None) -> dict:
        self._require_writable()
        with self._lock:
            out = self._core.forget(ids, now=now)
        self._notify_commit()
        return out

    def clear_memory(self, *, confirm: str) -> dict:
        """设置页「危险区」:物理清空当前租户的全部记忆(保留 self 认知体),并把该
        租户 spool 里尚未 claim 的会话 job 移到 discarded/(不删,手动移回即可重新摄入)。

        confirm 必须等于当前租户名(前端要求手输),否则 ClearConfirmMismatch,零写入。
        三件事在同一把引擎锁内完成:核心清库、代数 +1、丢弃 pending job。ingest
        worker 的 claim 也在这把锁内快照代数(见 _ingest_drain_once),所以任何 job
        要么在清空前被 claim(代数不符 → 作废),要么已被这里移走,不会把清空前排队
        的旧会话重新灌回来。"""
        self._require_writable()
        if confirm != self._ctx.tenant:
            raise ClearConfirmMismatch("confirm_mismatch")
        with self._lock:
            # 先清库:写门关闭时核心抛 WriteGateRejected,此时代数与 spool 都不动,
            # 在途 job 不会因一次没发生的清空被作废。
            out = self._core.clear_tenant()
            self._clear_generation += 1
            out["spool_discarded"] = self._ingest_discard_pending()
        out["tenant"] = self._ctx.tenant
        self._notify_commit()
        return out

    # 片 6 干预集:全经 self._lock 串行(对单写者 + 后台 tick)。
    def approve_review(self, stmt_id: str, *, now=None) -> dict:
        self._require_writable()
        with self._lock:
            return self._core.approve_review(stmt_id, now=now)

    def fulfill_commitment(self, stmt_id: str, *, now=None) -> dict:
        self._require_writable()
        with self._lock:
            return self._core.fulfill_commitment(stmt_id, now=now)

    def withdraw_commitment(self, stmt_id: str, *, now=None) -> dict:
        self._require_writable()
        with self._lock:
            return self._core.withdraw_commitment(stmt_id, now=now)

    def run_replay(self, mode: str, *, now=None) -> dict:
        self._require_writable()
        with self._lock:
            self._prepare_role("consolidation")             # 离线回放用 consolidation 判 gist
            if not self._rt.adapter.write_admitted():
                # host-side gate(P3.c write-gate follow-up):手动 replay 是重 DB 写。
                # DRAINING/UNREADY 时拒(不跑 ReplayScheduler)。返回带标记 dict —— route
                # (replay_trigger)只透传广播/返回,不访问 dict 的 key,故安全。
                return {"mode": mode, "rejected": "draining"}
            return self._core.run_replay(mode, now=now)

    def request_reconsolidation(self, stmt_id: str, *, request_id: str, now=None) -> dict:
        self._require_writable()
        with self._lock:
            return self._core.request_reconsolidation(stmt_id, request_id=request_id, now=now)

    def plan_query(self, text: str, *, intent: str, perspective=None,
                   target=None, k: int = 10) -> dict:
        """P3.a1 检索规划(9 意图 + 8 标签 + 拒答);JSON-able 摘要给路由层。"""
        with self._lock, self._read_only_rejections():
            self._require_embedding_unchanged()   # 规划里的语义召回要嵌入查询词
            r = self._core.plan_query(text, intent=intent,
                                      perspective=perspective, target=target, k=k)
            rc = r.receipt
            cc = rc.candidate_counts
            # Existing 6 keys stay byte-identical (the interact page depends on
            # them — regression-pinned). Everything below is purely additive:
            # the attribution/receipt fields that were previously dropped. Only
            # fields that are actually populated are surfaced — `runtime_health`
            # is deliberately omitted (callers never set q.runtime_health, so it
            # is always the default, and showing it would violate the live-vs-
            # roadmap honesty rule).
            # 左脑原文条目：dashboard 是扁平结构（与 memory.py 的 {"row": ...}
            # 不同，spec §4.6 已写明两处结构本就不同）。语句条目补
            # kind="statement"；lane 关闭时 results 与原结构等价。
            out = {
                "results": [{"kind": "statement",
                             "subject": e.row.subject_id,
                             "predicate": e.row.predicate,
                             "object": e.row.object_value,
                             "score": e.score, "label": e.label.name}
                            for e in r.entries],
                "context_pack": r.context_pack,
                "abstained": r.abstained,
                "abstention_reason": rc.abstention_reason,
                "plan_steps": [{"step": s.step, "detail": s.detail}
                               for s in rc.plan_steps],
                "scopes_searched": list(rc.scopes_searched),
                # 左脑原文条目：扁平结构与 results 一致，用 kind 区分。
                # lane 关闭时恒为空列表。
                "source_results": [{"kind": "source",
                                    "engram_ref": s.engram_ref,
                                    "clause_id": s.clause_id,
                                    "speaker": s.speaker,
                                    "session_id": s.session_id,
                                    "observed_at": s.observed_at,
                                    "text": s.text,
                                    "provenance": s.provenance,
                                    "score": s.score,
                                    "rrf_score": s.rrf_score,
                                    "rrf_rank": s.rrf_rank}
                                   for s in getattr(r, "source_entries", ())],
            }
            out["receipt"] = {
                "trace_id": rc.trace_id,
                "query_id": rc.query_id,
                "sufficiency_status": getattr(rc.sufficiency_status, "name",
                                              str(rc.sufficiency_status)),
                "filters_applied": [{"name": f.name, "value": f.value}
                                    for f in rc.filters_applied],
                "candidate_counts": {
                    "fetched": cc.fetched,
                    "returned": cc.returned,
                    "dropped_by_review": cc.dropped_by_review,
                    "dropped_by_state": cc.dropped_by_state,
                    "dropped_by_time_anchor": cc.dropped_by_time_anchor,
                    "dropped_by_evidence_erasure": cc.dropped_by_evidence_erasure,
                },
                "frontier_masked_count": rc.frontier_masked_count,
                # 左脑 lane 计数（lane 关闭时全为 0）。
                "source_candidates": getattr(rc, "source_candidates", 0),
                "source_selected": getattr(rc, "source_selected", 0),
                "source_rejected": getattr(rc, "source_rejected", 0),
                "source_no_time": getattr(rc, "source_no_time", 0),
                "source_truncated": getattr(rc, "source_truncated", 0),
                "frontier_visible_engrams": getattr(rc, "frontier_visible_engrams", 0),
                "evidence_erased_count": rc.evidence_erased_count,
                "projection_lag_events": rc.projection_lag_events,
                "degraded_paths": [{"path": d.path, "reason": d.reason,
                                    "fallback": d.fallback}
                                   for d in rc.degraded_paths],
                "score_breakdown": [{"statement_id": s.statement_id,
                                     "base": s.base, "recency": s.recency,
                                     "salience": s.salience, "activation": s.activation,
                                     "affect_consistency": s.affect_consistency,
                                     "temporal_penalty": s.temporal_penalty,
                                     "final_score": s.final_score}
                                    for s in rc.score_breakdown],
                "skipped_scopes": [{"scope": s.scope, "reason": s.reason}
                                   for s in rc.skipped_scopes],
                "stop_reason": rc.stop_reason,
            }
            return out

    def start_background_tick(self, interval_s: float, on_tick=None) -> None:
        """周期维护线程(P2.o 运行时闭环):每 interval_s 秒跑一次 tick
        (嵌入 → 承诺触发 → grounding → 回放巩固 → 投影兜底 → 出箱收敛),
        让 remember 的内容无人工干预地变得可召回。

        - 首次 tick 在一个完整间隔之后(避免启动时与首屏请求抢引擎锁);
        - tick 持引擎 RLock,期间 UI 命令请求排队——与手动 tick 同语义;
        - on_tick(stats) 仅在本轮有实际变化(任一计数非零)时回调,调用方
          用它做 WS 广播;
        - sample_embed_depth()/_sample_backpressure() 每轮都跑,不受 on_tick
          的 did_work 门控(它们采的是绝对状态快照,空闲/卡死轮也要采);
        - 异常记日志后继续循环(网络嵌入失败不杀线程);
        - interval_s <= 0 或已启动时为 no-op；只读引擎（spec §6.3）同样 no-op。
        """
        if interval_s <= 0 or self._tick_thread is not None or self.read_only:
            return
        stop = threading.Event()

        def _loop() -> None:
            while not stop.wait(interval_s):
                # Record when this iteration actually started (L6: tick-delay
                # measurement).  The scheduled wake was `interval_s` after the
                # previous iteration; any surplus is the background-tick overload.
                tick_started_at = time.monotonic()
                try:
                    stats = self.tick(_now_iso())
                except Exception:  # noqa: BLE001 — 保活:单轮失败不终结调度
                    logger.exception("background tick failed")
                    continue
                # 裁定 R54：嵌入端点被拒只跳过嵌入阶段（tick 不抛），按状态变化记一次日志，不逐轮记。
                self._log_embedding_state()
                # stage_timings_ms is always present (9 entries) and stages_skipped is
                # non-empty under DEGRADED — exclude both from the work-check, else
                # every idle DEGRADED tick spuriously fires a WS heartbeat (3b L8).
                did_work = _tick_did_work(stats)
                if on_tick is not None and did_work:
                    try:
                        on_tick(stats)
                    except Exception:  # noqa: BLE001
                        logger.exception("background tick on_tick callback failed")
                # dogfood 子项 B: embed-depth 采样——UNCONDITIONAL, not did_work-gated
                # (review fix: previously only ran from app.py's did_work-gated
                # on_tick, which silently dropped exactly the "embedder stalled /
                # backlog stuck" samples this series exists to show). backlog/
                # embedded are absolute state snapshots, independent of whether
                # this round's maintenance stack did anything — sample every
                # round, same rationale as _sample_backpressure below.
                # sample_embed_depth already keeps its own try/except keepalive;
                # wrapped again here so it can never take the loop down either.
                try:
                    self.sample_embed_depth()
                except Exception:  # noqa: BLE001
                    logger.exception("embed-depth sample failed")
                # Phase 5: backpressure sample after each tick (L3/L6/L7).
                # tick_started_at was recorded before tick() so the delay includes
                # tick duration; that is intentional — a slow tick means the loop
                # is overloaded.  Failures are caught inside _sample_backpressure.
                try:
                    self._sample_backpressure(tick_started_at, interval_s)
                except Exception:  # noqa: BLE001
                    logger.exception("backpressure sample failed")

        # 先 start 再挂句柄：句柄一旦可见，stop_* / quiesce 就会 join 它；join 一个还没启动
        # 的线程抛 RuntimeError。start() 返回之后线程已经启动，join 总是合法的。
        thread = threading.Thread(target=_loop, name="starling-bg-tick", daemon=True)
        thread.start()
        self._tick_stop = stop
        self._tick_thread = thread

    def stop_background_tick(self, timeout_s: float = 5.0) -> None:
        if self._tick_thread is None:
            return
        self._tick_stop.set()
        # 有界等待:线程可能正阻塞在真模型嵌入网络调用里;daemon 线程随
        # 进程退出,这里只为测试与显式 close 提供确定性收尾。
        self._tick_thread.join(timeout=timeout_s)
        self._tick_thread = None
        self._tick_stop = None

    # ── dogfood 子项 A(Task 3):spool ingest worker ──────────────────────
    #
    # `scripts/ingest_session.py`(Task 2)把 SessionEnd hook 的 transcript
    # 摄入 job 原子落到 `<spool>/tenant-<encoded>/*.json`;这里在对应租户的
    # dashboard 进程内只轮询该分区:
    # claim(rename → .json.processing)→ 读 transcript → clean_turns/
    # chunk_dialogue(ingest_filter,Task 1)→ 逐块 self.remember()(持锁,见
    # 下方 docstring)→ done/ 或 failed/,可重试的失败留在 spool 根下等下一
    # 轮重试(有界,见 _ingest_max_attempts;opus review 2026-07-12 fix wave)。

    def ingest_status(self) -> dict:
        """Spool queue depths for the ingest worker — pure directory listing,
        no engine lock needed (pending/processing/done/failed are just glob
        counts; ingest_remember_ms_total is an in-memory counter, see
        _ingest_drain_once)."""
        sp = self._ingest_spool
        cnt = lambda p, pat: len(list(p.glob(pat))) if p.exists() else 0
        return {"pending": cnt(sp, "*.json"),
                "processing": cnt(sp, "*.json.processing"),
                "done": cnt(sp / "done", "*.json"),
                "failed": cnt(sp / "failed", "*.json"),
                "ingest_remember_ms_total": self._ingest_remember_ms_total}

    def _ingest_reap_stale_processing(self) -> None:
        """Startup reaper (opus review Hazard — crash strands the claim): a
        single worker means any `*.json.processing` file found here was
        claimed by a PRIOR process that died before routing it to
        done/failed/spool (claim-rename and done/failed/spool-rename are the
        only ways a `.processing` file is created or removed, and both
        happen inside _ingest_drain_once in this same process). Rename it
        back to `*.json` so the very next poll re-claims and reprocesses it
        — restores the "no data loss on crash" property. Must run once,
        BEFORE the poll loop starts (see start_ingest_worker), so it never
        races the live worker's own in-flight claim."""
        sp = self._ingest_spool
        if not sp.exists():
            return
        for stale in sp.glob("*.json.processing"):
            stale.rename(stale.with_suffix(""))    # strip the trailing ".processing"

    def _ingest_discard_pending(self) -> int:
        """clear_memory 专用(调用方持 self._lock):把本租户 spool 根下尚未 claim 的
        `*.json` 移到 discarded/。不删除——手动移回 spool 根即可重新摄入。
        明确属于别的租户的 job 原样留下,与 _ingest_drain_once 的租户过滤一致。
        返回移走的个数。"""
        sp = self._ingest_spool
        if not sp.exists():
            return 0
        moved = 0
        for job_path in sorted(sp.glob("*.json")):
            try:
                job = json.loads(job_path.read_text())
            except Exception:  # noqa: BLE001 — 损坏的 job 也在本分区,一并移走
                job = None
            if isinstance(job, dict) and "tenant" in job and job.get("tenant") != self._ctx.tenant:
                continue
            (sp / "discarded").mkdir(exist_ok=True)
            try:
                job_path.rename(sp / "discarded" / job_path.name)
            except OSError:
                continue   # 已被别处移走;worker 的 claim 同样持锁,正常不会发生
            moved += 1
        return moved

    def _requeue_unless_cleared(self, claimed: Path, job_path: Path, generation: int,
                                job: dict | None = None) -> str:
        """把已 claim 的 job 放回 spool 根，除非 claim 之后发生过 clear_memory（整支审查 I-1）。

        _ingest_discard_pending 只看 spool 根下的 ``*.json``，看不到正在处理的 ``.processing``。
        claim 之后、放回之前若有一次清空，原样放回就会让下一轮 drain 用新代数重新 claim，把清空前
        的 transcript 再写进库。所以比较代数与放回都在 ``self._lock`` 内做：clear_memory 也在这把锁内
        前移代数、丢弃 pending job，两者因此互斥——要么清空在先（代数过期 → 移进 discarded/，与
        _TenantCleared 同一归宿，返回 "done"），要么放回在先（job 回到 spool 根，清空随即把它当
        pending 丢弃）。

        ``job`` 给出时（通用重试分支，attempts 已 +1）先把它写回再放回；推迟分支不传，job 原样不变。
        只在 drain 的 except 分支里调用，此时不持别的锁、钩子已经返回，短暂取 ``self._lock`` 与 claim
        时取它的方式一致，不跨任何钩子调用。"""
        sp = self._ingest_spool
        with self._lock:
            if generation != self._clear_generation:
                (sp / "discarded").mkdir(exist_ok=True)
                claimed.rename(sp / "discarded" / job_path.name)
                return "done"
            if job is not None:
                claimed.write_text(json.dumps(job))
            claimed.rename(job_path)
            return "deferred"

    def _ingest_drain_once(self) -> str:
        """Drain ONE spool job: claim (atomic rename to .json.processing) ->
        read transcript -> clean/chunk -> self.remember() each chunk -> done/
        on success or once retries are exhausted, or back to the spool root
        (unclaimed, attempts incremented) on a still-retryable failure so a
        LATER poll retries it with no data loss. Returns "empty" (nothing to
        claim), "done" (the job left the spool this round — succeeded or was
        dead-lettered), or "deferred" (left back in the spool for a later
        poll — never hot-reclaimed within this call). Callers (see
        start_ingest_worker) must not busy-loop on "deferred"/"empty".

        Lock discipline: this calls the ENGINE-wrapped `self.remember(...)`,
        so the shared facade/adapter state remains serialized with every
        other engine operation. Core now splits remember into short prepare
        and atomic commit transactions with all LLM extraction between them;
        no SQLite transaction spans the network phase. The engine lock still
        covers the whole facade call, so ingest cannot race provider swaps or
        another operation using the same adapter. `_ingest_throttle_s` leaves
        service windows between backlog jobs.

        Failure classification (opus review Hazard 2, "zero statements ==
        failure" — WRONG): a legitimate no-facts chunk (most pure-coding
        Claude Code turns: "run the tests" / "ok" / "done") extracts zero
        statements via a successful `ok=True` empty `[]` response —
        indistinguishable, at the old binding surface, from a systemic
        extraction outage. `RememberOutcome.extraction_failed`
        (src/memory/memory_ops.cpp:78, derived from Extractor::run's
        Status::FAILED — set only when the LLM adapter/JSON-parse failed on
        EVERY retry attempt, never for a successful-but-empty extraction) is
        now surfaced through the Core-owned `memory_remember_all` binding and
        passed through by MemoryCore.remember(). The returned flag merges the
        belief and general-fact channel outcomes. Episodic extraction remains
        explicitly best-effort: an empty/invalid episodic response contributes
        no event statements but does not fail an otherwise durable remember.

        A chunk with extraction_failed=False is success REGARDLESS of
        statement count (an all-chitchat job legitimately produces zero
        statements and is NOT an error). extraction_failed=True on any
        chunk, or ANY raised exception (bad job/transcript data,
        WriteGateRejected, LLMNotConfigured, network errors, a same-
        connection race, …), is a retryable failure: no more keyword-
        guessing about which errors are "transient" — every failure gets
        the same bounded number of chances (_ingest_max_attempts, tracked
        via an `attempts` counter persisted on the job JSON). Below the
        cap it is left back in the spool for a later poll; at or above it,
        the job is dead-lettered to failed/ with a `.error` sidecar
        recording the last error.
        """
        sp = self._ingest_spool
        jobs = sorted(sp.glob("*.json")) if sp.exists() else []
        if not jobs:
            return "empty"
        job_path = None
        for candidate in jobs:
            try:
                candidate_job = json.loads(candidate.read_text())
            except Exception:
                candidate_job = None
            if (candidate_job is None or "tenant" not in candidate_job
                    or candidate_job.get("tenant") == self._ctx.tenant):
                job_path = candidate
                break
        if job_path is None:
            return "deferred"
        claimed = job_path.with_suffix(".json.processing")
        try:
            # claim 与代数快照在同一把锁内,clear_memory 也持这把锁:要么它先把这个
            # pending job 移进 discarded/(这里 rename 失败 → deferred),要么它后发生
            # (代数不符 → remember 抛 _TenantCleared → 作废)。清空前排队的旧会话
            # 因此不会被重新灌回来。
            with self._lock:
                generation = self._clear_generation
                job_path.rename(claimed)           # 原子 claim
        except OSError:
            return "deferred"                       # 被别的消费者抢走或已被清空丢弃,下轮再试
        try:
            job = json.loads(claimed.read_text())
        except Exception:                           # noqa: BLE001 — 损坏的 job 文件按空 job 处理,下方统一走有界重试
            job = {}
        if "tenant" in job and job.get("tenant") != self._ctx.tenant:
            claimed.rename(job_path)
            return "deferred"
        try:
            tp = Path(job["transcript_path"])
            lines = tp.read_text(errors="replace").splitlines()
            project = os.path.basename((job.get("cwd") or "").rstrip("/")) or "unknown"
            # 形态 S §6.3：transcript 是来源通道的第一个消费者。开关关闭时
            # 完全走原有路径（clean_turns → chunk_dialogue → remember），
            # 不写任何来源表；开启时先用带时间的版本把逐话轮写入左脑。
            # 写入以配置为准、而非以 lane 是否生效为准：原文是记忆本身，不该因为
            # 一时配错了 embedder 就丢掉。未生效时 source_unit_vectors 留 pending，
            # 换回标定模型后 worker 会补嵌入（模型变更的行由 worker 重置）。
            if self._settings["source_lane"]["enabled"]:
                self._retain_transcript_turns(
                    lines, job=job, project=project, generation=generation)
            chunks = chunk_dialogue(clean_turns(lines))
            extraction_failed = False
            for chunk in chunks:
                t0 = time.monotonic()
                out = self.remember(chunk, holder="self",
                                    interlocutor=f"claude-code:{project}", now=None,
                                    clear_generation=generation)
                self._ingest_remember_ms_total += int((time.monotonic() - t0) * 1000)
                if out.get("outcome") == "source_missing":
                    # 锁外抽取期间被清空:C++ commit 已零写入,整个 job 作废。
                    raise _TenantCleared("tenant memory was cleared during extraction")
                if out.get("extraction_failed"):
                    extraction_failed = True
            if extraction_failed:
                raise RuntimeError(
                    f"extraction_failed: extraction LLM failed on one or "
                    f"more of {len(chunks)} chunk(s)")
            (sp / "done").mkdir(exist_ok=True)
            claimed.rename(sp / "done" / job_path.name)
            # 摄入一个 job 算一次提交（来源通道的写入不经 remember，不会自己通知）。每块 remember 也各
            # 通知一次，WorldPool 按世界去抖，多发几次无害。
            self._notify_commit()
            return "done"
        except SessionSnapshotDeferred:
            # 按会话的自动快照这一轮没拿到该世界的世界锁（恢复、关闭、手动快照正持着它）。钩子在任何
            # 写入之前调用，这个 job 什么都没写：原样放回 spool，不计 attempts（不是失败），下一轮再来。
            # 钩子期间若发生了清空，放回就会把清空前的会话重新灌回来：改进 discarded/（整支审查 I-1）。
            return self._requeue_unless_cleared(claimed, job_path, generation)
        except _TenantCleared:
            # 清空发生在本 job claim 之后:不重试、不进 failed/,与清空时尚未 claim 的
            # job 一样归档到 discarded/(手动移回 spool 根即可重新摄入)。清空前已提交的
            # 块随清空一起被删,清空后的块一律没写,所以库里不留这个 job 的半截。
            (sp / "discarded").mkdir(exist_ok=True)
            claimed.rename(sp / "discarded" / job_path.name)
            return "done"
        except Exception as exc:                    # noqa: BLE001 — classify + route, never crash the worker
            attempts = int(job.get("attempts") or 0) + 1
            if attempts < self._ingest_max_attempts:
                job["attempts"] = attempts
                # 留 spool,下轮重扫自动重试(本次不 hot-reclaim);claim 之后发生过清空则改进
                # discarded/、不写回 attempts(整支审查 I-1,W4 之前就有的同类缺口)。
                return self._requeue_unless_cleared(claimed, job_path, generation, job)
            (sp / "failed").mkdir(exist_ok=True)
            claimed.rename(sp / "failed" / job_path.name)
            (sp / "failed" / (job_path.name + ".error")).write_text(_dead_letter_text(exc))
            return "done"

    def start_ingest_worker(self, poll_interval_s: float = 2.0) -> None:
        """Background poller (mirrors start_background_tick's thread
        template, engine.py:559): first reaps any crash-stranded
        `.processing` claim (see _ingest_reap_stale_processing), then drains
        the spool every poll_interval_s. The inner loop keeps going ONLY
        while jobs keep completing ("done") so a burst doesn't wait a full
        poll interval between jobs — throttled by `_ingest_throttle_s`
        between each, since "done" means this worker just held `self._lock`
        across a remember() call and an unthrottled hot loop could pin the
        rest of the dashboard behind a large backlog. A "deferred"/"empty"
        result falls straight through to the normal poll_interval_s sleep —
        no busy-spin on a persistently-retryable job. daemon thread; an
        exception from one job never kills the loop (caught + logged, next
        poll continues). 只读引擎（spec §6.3）不起摄入：no-op。"""
        if self._ingest_thread is not None or self.read_only:
            return
        self._ingest_reap_stale_processing()
        stop = threading.Event()

        def _loop() -> None:
            while not stop.wait(poll_interval_s):
                try:
                    while self._ingest_drain_once() == "done":
                        if stop.wait(self._ingest_throttle_s):
                            break
                except Exception:  # noqa: BLE001 — 保活:单个 job 失败不终结调度
                    logger.exception("ingest worker failed")

        # 先 start 再挂句柄，理由同 start_background_tick。
        thread = threading.Thread(target=_loop, name="starling-ingest", daemon=True)
        thread.start()
        self._ingest_stop = stop
        self._ingest_thread = thread

    def stop_ingest_worker(self, timeout_s: float = 5.0) -> None:
        if self._ingest_thread is None:
            return
        self._ingest_stop.set()
        self._ingest_thread.join(timeout=timeout_s)
        self._ingest_thread = None
        self._ingest_stop = None

    def quiesce(self, timeout_s: float = 5.0) -> bool:
        """停后台 tick 与摄入线程（世界切换前用）。两个线程都确认退出返回 True；
        任一线程 join 超时仍存活返回 False——调用方据此拒绝切换（ingest_busy）。
        stop_* 不管 join 成败都会清句柄，所以先留住线程引用，事后再判 is_alive。

        失败时除了句柄，还要把停止事件一并放回并撤销（clear）：stop_* 已经 set 了它，
        卡住的线程一旦脱困就会看到停止信号而退出；而句柄非空会让调用方回滚时的
        start_* 直接 no-op，于是摄入/维护悄悄停摆。撤销后线程脱困会继续正常循环。

        timeout_s 是两个线程合计的上限：先停的线程用掉多少，后停的就只剩多少（两次 join
        串行，各给满 timeout_s 的话最坏是 2×timeout_s，与契约的「5 秒停不下就拒绝」不符）。"""
        self.quiesce_stuck = ()
        ingest, tick = self._ingest_thread, self._tick_thread
        ingest_stop, tick_stop = self._ingest_stop, self._tick_stop
        deadline = time.monotonic() + timeout_s
        self.stop_ingest_worker(timeout_s=timeout_s)
        self.stop_background_tick(timeout_s=max(0.0, deadline - time.monotonic()))
        ingest_alive = ingest is not None and ingest.is_alive()
        tick_alive = tick is not None and tick.is_alive()
        if ingest_alive:
            self._ingest_thread, self._ingest_stop = ingest, ingest_stop
            ingest_stop.clear()
        if tick_alive:
            self._tick_thread, self._tick_stop = tick, tick_stop
            tick_stop.clear()
        self.quiesce_stuck = tuple(name for name, alive in (("ingest", ingest_alive),
                                                            ("tick", tick_alive)) if alive)
        return not (ingest_alive or tick_alive)

    def working_set(self, interlocutor, *, goal=None, token_budget=2000, holder=None) -> dict:
        """Like Memory.render_working_set but returns a JSON-able dict (API shape)."""
        with self._lock, self._read_only_rejections():
            self._require_embedding_unchanged()   # relevant_memories 走语义召回
            cb = self._core.build_working_set(interlocutor, goal=goal,
                                              token_budget=token_budget, holder=holder)
            return {"render": cb.render(),
                    "blocks": [{"label": b.label, "content": b.content,
                                "tokens": b.token_estimate} for b in cb.blocks],
                    "truncated": cb.truncated}

    def health(self):
        """Current RuntimeHealth (read-only; forwards to the live supervisor)."""
        return self._rt.health()

    def source_lane_status(self) -> dict:
        """左脑 lane 是否真正生效。配置开着不等于生效：embedder 与标定模型不一致时
        lane 静默不工作，这里把原因摆出来，免得「开了却没用」无从察觉。"""
        with self._lock:
            core = self._core
            return {"configured": bool(self._settings["source_lane"]["enabled"]),
                    "active": core.source_lane is not None,
                    "inactive_reason": core.source_lane_inactive_reason}

    def events(self) -> list:
        """Snapshot of the supervisor transition log (read-only passthrough)."""
        return self._rt.events()

    def begin_drain(self, trigger: str = "admin_drain") -> None:
        """OV-5 / D-P2-6: enter DRAINING on host shutdown. Forwards to the live
        C++ supervisor through the Runtime handle. Acquires NOTHING extra — the
        supervisor self-locks (D3); taking self._lock here would let a slow
        in-flight converse/tick block shutdown drain."""
        self._rt.begin_drain(trigger)

    def close(self) -> None:
        self.stop_ingest_worker()
        self.stop_background_tick()
        self._core.close()


# Back-compat alias: routes and tests import the underscore name from here.
_LLMNotConfigured = LLMNotConfigured
