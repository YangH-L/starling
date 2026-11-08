"""Dashboard runtime configuration.

Single source of truth is `~/.starling/starling.json` (0600, gitignored): it
holds the dashboard fields plus the dashboard bearer `token` and a multi-model
provider registry. The file is the persistent secret store — it is NEVER
committed to git, written to the SQLite memory DB, or logged. `GET /api/config`
never returns the token or full keys.

Model config (Phase 2a) is a named-provider registry + per-job role bindings:

    providers: { "<name>": {provider, model, base_url, api_key, dim?, max_tokens?, timeout_ms?, max_retries?, enable_thinking?} }
    roles:     { extraction|embedding|chat|consolidation: "<provider name>" }

`extraction` and `embedding` have live consumers today; `chat` gains one with
converse() (Phase 2c); `consolidation` is reserved (no LLM consumer yet). The
old single `{llm, embedder}` shape is auto-migrated on load (llm → provider
"default" bound to extraction; embedder → provider "embedder" bound to
embedding), so existing configs keep working with no manual edit.
"""
from __future__ import annotations

import json
import os
import secrets
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

_DEFAULT_DIR = Path.home() / ".starling"
_DEFAULT_CONFIG = _DEFAULT_DIR / "starling.json"
_DEFAULT_DB = _DEFAULT_DIR / "dashboard.db"
_DEFAULT_INGEST_SPOOL = _DEFAULT_DIR / "ingest-spool"

# Role keys. extraction/embedding are wired today; chat is consumed by
# converse() (2c); consolidation is reserved (replay uses no LLM yet).
ROLES = ("extraction", "embedding", "chat", "consolidation")

_SERIALIZABLE = (
    "db_path", "agent", "tenant", "token", "host", "port",
    "cors_origins", "providers", "roles", "tick_interval_s",
    "vector_backend", "vector_store_path", "gist_thresholds", "ingest_spool_path",
    "source_lane_enabled", "source_lane_render_time", "source_lane_tau_source",
    "source_lane_model", "source_lane_dim", "source_lane_k_source",
    "source_lane_chunk_byte_budget",
)
# starling-serve 不从 D/starling.json 取的键（load_service）：路径都在 D 之下，token 与监听地址来自命令行。
_SERVICE_IGNORED = frozenset({"db_path", "ingest_spool_path", "vector_store_path", "token", "host",
                              "port"})


@dataclass
class DashboardConfig:
    db_path: str = ""
    agent: str = "self"
    tenant: str = "default"
    token: str = ""
    host: str = "127.0.0.1"
    port: int = 8787
    cors_origins: list[str] = field(default_factory=list)
    # Named provider registry + role bindings (see module docstring).
    providers: dict = field(default_factory=dict)
    roles: dict = field(default_factory=dict)
    # P2.o 运行时闭环:后台维护 tick 间隔秒数(嵌入/巩固/投影/出箱收敛)。
    # 0 = 禁用(回到纯手动 tick)。
    tick_interval_s: float = 30.0
    # P3.b1 phase 5: 向量后端选型。sqlite(默认)=暴力 cosine + SQL scope。
    vector_backend: str = "sqlite"
    vector_store_path: str = ""
    # Root directory only. DashboardEngine adds a tenant-specific partition.
    ingest_spool_path: str = ""
    # #38-C v2 threshold surface: NORM-gist tuning knobs. Empty keys → C++ defaults
    # (min_holders=3, min_replay_count=1, min_confidence=0.6). Threaded into
    # run_idle/run_sleep via MemoryCore.gist_thresholds.
    gist_thresholds: dict = field(default_factory=dict)
    # 形态 S 左脑来源 lane。**默认开启**（用户 2026-10-23 裁定，依据
    # docs/eval/2026-10-23-lane-tradeoff-results.md：BEAM-500K 留出集上可答题
    # +0.361，弃答错误 2→10/58，C1/C2 过、C3 过不了——带着已知代价开启）。
    # tau_source 只对标定时的嵌入模型有效：source_lane_model/dim 记下它，
    # 当前 embedder 与之不符时 lane 不生效（见 MemoryCore.enable_source_lane）。
    # render_time 从属于 enabled——单独打开它没有意义（没有原文可渲染）。
    source_lane_enabled: bool = True
    source_lane_render_time: bool = False
    source_lane_tau_source: float = 0.56          # 阶段 A 标定（LongMemEval），阶段 B 沿用
    source_lane_model: str = "text-embedding-v4"
    source_lane_dim: int = 1024
    source_lane_k_source: int = 30                # 500K 扫描选定（k/预算耦合）
    source_lane_chunk_byte_budget: int = 32000
    config_path: str = ""  # not serialized; where load()/save() persist
    # 以下两项不序列化，只由 load_service 设（starling-serve，spec §10.1）：data_root 非空时就是数据目录 D
    # （data_dir 不再看 STARLING_DATA_DIR）；persist_token 为 False 时 save() 不写 token。
    data_root: str = ""
    persist_token: bool = True

    # ----- role → provider resolution -----
    def resolve_role(self, role: str) -> dict | None:
        """The provider config dict bound to `role`, or None when unbound /
        unconfigured. Callers treat None as 'no adapter for this job'."""
        name = self.roles.get(role)
        if not name:
            return None
        return self.providers.get(name)

    def extraction(self) -> dict | None:
        return self.resolve_role("extraction")

    def embedding(self) -> dict | None:
        return self.resolve_role("embedding")

    def chat(self) -> dict | None:
        # converse falls back to the extraction provider when chat is unbound.
        return self.resolve_role("chat") or self.resolve_role("extraction")

    def resolve_provider(self, name: str | None) -> dict | None:
        """The provider config dict for a registry name, or None when the name is
        empty/absent. Distinct from resolve_role (which goes through a role binding):
        this powers per-turn model selection on the converse / interact pages, where
        the operator picks a registry provider directly for one request."""
        return self.providers.get(name) if name else None

    @classmethod
    def from_env(cls) -> "DashboardConfig":
        origins = os.environ.get("STARLING_DASH_CORS_ORIGINS", "")
        return cls(
            db_path=os.environ.get("STARLING_DASH_DB", "starling_dashboard.db"),
            agent=os.environ.get("STARLING_DASH_AGENT", "self"),
            tenant=os.environ.get("STARLING_DASH_TENANT", "default"),
            token=os.environ.get("STARLING_DASH_TOKEN", ""),
            host=os.environ.get("STARLING_DASH_HOST", "127.0.0.1"),
            port=int(os.environ.get("STARLING_DASH_PORT", "8787")),
            cors_origins=[o.strip() for o in origins.split(",") if o.strip()],
            tick_interval_s=float(os.environ.get("STARLING_DASH_TICK_INTERVAL", "30")),
            ingest_spool_path=os.environ.get(
                "STARLING_DASH_INGEST_SPOOL", str(_DEFAULT_INGEST_SPOOL)),
        )

    @classmethod
    def load(cls, path: str | None = None) -> "DashboardConfig":
        """Load defaults -> file (+ legacy migration) -> env, then ensure token."""
        cfg = cls(db_path=str(_DEFAULT_DB))
        p = Path(path or os.environ.get("STARLING_CONFIG") or _DEFAULT_CONFIG).expanduser()
        cfg.config_path = str(p)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            for k in _SERIALIZABLE:
                if k in data and data[k] is not None:
                    setattr(cfg, k, data[k])
            _migrate_legacy_model_config(cfg, data)
        _env_overlay(cfg)
        cfg.db_path = str(Path(cfg.db_path).expanduser())
        if cfg.ingest_spool_path:
            cfg.ingest_spool_path = str(Path(cfg.ingest_spool_path).expanduser())
        if not cfg.token:
            cfg.token = secrets.token_urlsafe(24)
            cfg.save()
        return cfg

    @classmethod
    def load_service(cls, data_dir: str, token: str) -> "DashboardConfig":
        """starling-serve 的配置（spec §10.1）：所有路径都在数据目录 D 之下，不读 ~/.starling、不看环境变量里的路径。

        默认值 → D/starling.json（可以没有；旧格式照常迁移）→ 没有抽取 provider 时从 OPENAI_* 补一个（与 load 同一条
        规则）。文件里的路径键（db_path、ingest_spool_path、vector_store_path）与 token、host、port 不用：默认世界的库是
        D/dashboard.db，摄入目录 D/ingest-spool，zvec 的向量目录 D/vectors；token 由调用方给（--token-file 或
        STARLING_SERVICE_TOKEN），save() 从不写它；host、port 取命令行。STARLING_DASH_* 环境变量一律不看。"""
        root = Path(data_dir)
        cfg = cls(db_path=str(root / "dashboard.db"))
        path = root / "starling.json"
        cfg.config_path = str(path)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            for key in _SERIALIZABLE:
                if key not in _SERVICE_IGNORED and key in data and data[key] is not None:
                    setattr(cfg, key, data[key])
            _migrate_legacy_model_config(cfg, data)
        _seed_openai_extraction(cfg)
        cfg.ingest_spool_path = str(root / "ingest-spool")
        cfg.vector_store_path = str(root / "vectors") if cfg.vector_backend == "zvec" else ""
        cfg.token = token
        cfg.data_root = str(root)
        cfg.persist_token = False
        return cfg

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in _SERIALIZABLE}

    def save(self, path: str | None = None) -> None:
        p = Path(path or self.config_path or _DEFAULT_CONFIG).expanduser()
        p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = self.to_dict()
        if not self.persist_token:
            data.pop("token", None)          # starling-serve：token 不落盘（spec §10.1）
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".starling-", suffix=".tmp")
        try:
            os.chmod(fd, 0o600)  # set perms before any secret bytes are written
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(data, indent=2, ensure_ascii=False))
            os.replace(tmp, p)   # atomic replace; never leaves a partial file
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self.config_path = str(p)

    def validate_source_lane(self) -> None:
        """左脑 lane 的开关一致性。

        ``tau_source`` 未经标定不得开启（spec §4.4 硬约束）——用一个拍脑袋的
        阈值上线，等于用一个没有依据的分界线决定"记不记得"。
        ``render_time`` 从属于 ``enabled``：没有原文可渲染时单独打开它没有意义。
        """
        if self.source_lane_enabled:
            if not (self.source_lane_tau_source > 0):
                raise RuntimeError(
                    "source_lane_enabled requires a calibrated "
                    "source_lane_tau_source (> 0)")
        elif self.source_lane_render_time:
            raise RuntimeError(
                "source_lane_render_time requires source_lane_enabled")

    def validate_bind(self) -> None:
        """Refuse to expose a tokenless service on a non-loopback interface."""
        if self.host not in ("127.0.0.1", "localhost", "::1") and not self.token:
            raise RuntimeError(
                "refusing to bind dashboard to non-loopback host without a token"
            )

    def ingest_spool_root(self) -> Path:
        configured = self.ingest_spool_path or os.environ.get(
            "STARLING_DASH_INGEST_SPOOL", str(_DEFAULT_INGEST_SPOOL))
        return Path(configured).expanduser()

    # ----- 记忆服务：数据目录与默认世界的设置（spec §8、§10.1） -----
    @property
    def data_dir(self) -> str:
        """注册表（registry.db）与新世界目录 worlds/ 所在的目录：环境变量 STARLING_DATA_DIR，
        没设就是默认世界库文件所在的目录（今天 ~/.starling）。db_path 没有父目录（:memory:、空串、
        裸文件名）时为空串：没有地方放注册表，引擎池只服务默认世界（裁定 R6）。这个判断先于环境
        变量：否则测试里的 :memory: 会被登记成某个目录下的默认世界。"""
        db_path = self.db_path
        if not db_path or db_path == ":memory:" or not os.path.dirname(db_path):
            return ""
        if self.data_root:
            return self.data_root            # starling-serve 的 D（load_service）
        env = os.environ.get("STARLING_DATA_DIR")
        if env:
            return str(Path(env).expanduser())
        return os.path.dirname(os.path.abspath(db_path))

    def world_settings(self) -> dict:
        """把当前 provider / roles / lane / gist / tick 转成世界设置的部分 dict（字段见 C++
        world_settings.hpp）。只做字段改名，不做判断：合并、校验、补默认值都在 C++
        （registry.update_settings）。不含密钥：endpoint 只记 provider_name 与当时的 base_url，
        密钥在调用时按 provider_name 从 providers 取（spec §8.3）。不带 background.auto_snapshot
        （裁定 R16），所以同步默认世界时保留注册表里按世界存的自动快照配置。

        没绑定的角色（或绑到不存在的 provider）写 null，gist_thresholds 为空时写 null：合并本身删不掉
        键，null 让 C++ 删掉注册表里的旧绑定、把阈值重置为 {}（裁定 R24）。chat 没绑定时按
        DashboardConfig.chat() 的回退取抽取的 provider。vector_backend 永远写 sqlite（裁定 R26）：
        注册表里的设置只收 sqlite，zvec 不进存着的、被继承的设置；默认世界的引擎直接按 config 的后端建。"""
        out: dict = {}
        for role, name in (("extraction", self.roles.get("extraction")),
                           ("embedding", self.roles.get("embedding")),
                           ("chat", self.roles.get("chat") or self.roles.get("extraction")),
                           ("consolidation", self.roles.get("consolidation"))):
            prov = self.providers.get(name) if name else None
            out[role] = _endpoint_settings(name, prov) if prov is not None else None
        out["vector_backend"] = "sqlite"
        out["source_lane"] = {
            "enabled": bool(self.source_lane_enabled),
            "render_time": bool(self.source_lane_render_time),
            "tau_source": float(self.source_lane_tau_source),
            "model": self.source_lane_model,
            "dim": int(self.source_lane_dim),
            "k_source": int(self.source_lane_k_source),
            "chunk_byte_budget": int(self.source_lane_chunk_byte_budget),
        }
        gist = {k: v for k, v in (self.gist_thresholds or {}).items() if v is not None}
        out["gist_thresholds"] = gist or None
        out["background"] = {"tick_interval_s": float(self.tick_interval_s)}
        return out


# provider dict 里与 C++ EndpointSettings 同名的非密钥字段。api_key 永不写出。
_ENDPOINT_TEXT_KEYS = ("base_url", "model", "thinking_type")
_ENDPOINT_INT_KEYS = ("dim", "max_tokens", "timeout_ms", "max_retries")


def _endpoint_settings(name: str, prov: dict) -> dict:
    """一个 provider（starling.json 的一项）→ 世界设置里的 endpoint dict。None 与缺省的字段不写出，
    由 C++ 补默认值；provider 缺省按 openai 写出（与 _build_chat_adapter 的缺省一致）。"""
    ep: dict = {"provider_name": name, "provider": prov.get("provider") or "openai"}
    for key in _ENDPOINT_TEXT_KEYS:
        if prov.get(key) is not None:
            ep[key] = str(prov[key])
    for key in _ENDPOINT_INT_KEYS:
        if prov.get(key) is not None:
            ep[key] = int(prov[key])
    if prov.get("enable_thinking") is not None:
        ep["enable_thinking"] = bool(prov["enable_thinking"])
    return ep


def provider_endpoint_settings(name: str, prov: dict) -> dict:
    """一个 provider → 世界设置里的 endpoint dict（不含密钥），与 world_settings() 建 endpoint 同一套字段规则。
    重新确认端点（裁定 R53）把它交给 C++。"""
    return _endpoint_settings(name, prov)


def provider_endpoint(prov: dict) -> dict:
    """一个 provider 此刻的端点身份 {"provider", "base_url"}（裁定 R51），按 world_settings() 写 endpoint 的同一套
    规则归一（种类缺省 openai、base_url 缺省空串），所以与世界里记的 endpoint 可以逐字段比较。比较在 C++。"""
    ep = _endpoint_settings("", prov)
    return {"provider": ep["provider"], "base_url": ep.get("base_url", "")}


def provider_endpoints(providers: dict) -> dict[str, dict]:
    """{provider 名: provider_endpoint(...)}，交给 C++ 核对客户端写的设置（裁定 R40、R51）。不含密钥。
    先拍一份快照再遍历：/api/config 的 _post_config（setdefault）与 _restore（clear + update）可能同时在改这个
    dict，直接遍历会抛「dictionary changed size during iteration」。list(...) 由 C 实现、持 GIL 一次拷完。"""
    return {name: provider_endpoint(prov or {}) for name, prov in list(providers.items())}


def _migrate_legacy_model_config(cfg: DashboardConfig, data: dict) -> None:
    """Old `{llm, embedder}` files → providers/roles (one-way, on load).

    Only fires when the file predates the registry (no `providers`/`roles`
    keys). llm → provider "default" bound to extraction; embedder → provider
    "embedder" bound to embedding. Idempotent: re-saving persists the new shape,
    so this runs at most once per file.
    """
    if "providers" in data or "roles" in data:
        return
    legacy_llm = data.get("llm")
    legacy_emb = data.get("embedder")
    if legacy_llm and legacy_llm.get("api_key"):
        cfg.providers["default"] = dict(legacy_llm)
        cfg.roles["extraction"] = "default"
    if legacy_emb and legacy_emb.get("api_key"):
        cfg.providers["embedder"] = dict(legacy_emb)
        cfg.roles["embedding"] = "embedder"


def _env_overlay(cfg: DashboardConfig) -> None:
    """env > file for dashboard fields; seed an extraction provider from OPENAI_*
    when no extraction provider is configured (preserves the zero-config-file,
    OPENAI_API_KEY-only quickstart)."""
    e = os.environ.get
    if e("STARLING_DASH_DB"): cfg.db_path = e("STARLING_DASH_DB")
    if e("STARLING_DASH_AGENT"): cfg.agent = e("STARLING_DASH_AGENT")
    if e("STARLING_DASH_TENANT"): cfg.tenant = e("STARLING_DASH_TENANT")
    if e("STARLING_DASH_TOKEN"): cfg.token = e("STARLING_DASH_TOKEN")
    if e("STARLING_DASH_HOST"): cfg.host = e("STARLING_DASH_HOST")
    if e("STARLING_DASH_PORT"): cfg.port = int(e("STARLING_DASH_PORT"))
    if e("STARLING_DASH_TICK_INTERVAL"): cfg.tick_interval_s = float(e("STARLING_DASH_TICK_INTERVAL"))
    if e("STARLING_DASH_VECTOR_BACKEND"): cfg.vector_backend = e("STARLING_DASH_VECTOR_BACKEND")
    if e("STARLING_DASH_VECTOR_STORE_PATH"): cfg.vector_store_path = e("STARLING_DASH_VECTOR_STORE_PATH")
    if e("STARLING_DASH_INGEST_SPOOL"): cfg.ingest_spool_path = e("STARLING_DASH_INGEST_SPOOL")
    if e("STARLING_DASH_CORS_ORIGINS"):
        cfg.cors_origins = [o.strip() for o in e("STARLING_DASH_CORS_ORIGINS").split(",") if o.strip()]
    _seed_openai_extraction(cfg)


def _seed_openai_extraction(cfg: DashboardConfig) -> None:
    """没有抽取 provider、但设了 OPENAI_API_KEY：从 OPENAI_* 补一个 provider "default" 并绑到抽取（load 与
    load_service 共用这条规则）。"""
    e = os.environ.get
    if cfg.resolve_role("extraction") is None and e("OPENAI_API_KEY"):
        cfg.providers["default"] = {
            "provider": "openai",
            "model": e("OPENAI_MODEL", ""),
            "base_url": e("OPENAI_BASE_URL", ""),
            "api_key": e("OPENAI_API_KEY"),
        }
        cfg.roles["extraction"] = "default"
