"""Config routes — multi-model provider registry (read masked, upsert, bind, test).

The registry is `providers` (named secret-bearing configs) + `roles` (which
provider each job uses). extraction/embedding are live; chat is consumed by
converse() (2c); consolidation (#38-C) judges NORM gists in offline replay.
Hot-swap is per affected role:
rebinding/editing the extraction provider re-swaps the LLM adapter (O(1));
the embedding provider triggers a re-embed (expensive). GET never leaks keys.

config 是默认世界设置的权威来源（记忆服务第 1 期，裁定 R27）：保存之后经 pool.sync_default_settings()
把它整段替换进注册表里默认世界的设置，开着的默认世界引擎随之换上新设置；适配器仍由这里按改动的角色
热换。这些路由只作用于默认世界（只挂在 /api 下）。

改动先过 C++ 校验再落盘（裁定 R31）：校验不过就把内存里的 config 恢复原样、回 400 invalid_settings，
starling.json 与注册表都不动（否则坏值落了盘，下次启动同步默认世界时就起不来）。

写配置的两个路由整段在一个工作线程里做完（裁定 R30）：取租约、改 config、校验、落盘、同步、热换适配器、
还租约。同步与热换要取默认世界引擎的锁，tick、对话、回放可能持着它几秒到几十秒，事件循环不能等。两个
写请求彼此串行（app.state.config_lock，线程锁，只在工作线程里取）：改 config 与落盘之间若有别的请求插进来，
两份改动会交错，校验失败时的恢复也会盖掉对方的改动。
"""
from __future__ import annotations

import copy

from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from starling import _core


class ProviderFields(BaseModel):
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    dim: int | None = None
    # 生成参数(可选,仅 OpenAI 兼容端点):未设置则沿用 C++ 默认。
    max_tokens: int | None = None
    timeout_ms: int | None = None
    max_retries: int | None = None
    enable_thinking: bool | None = None


class ConfigBody(BaseModel):
    # Partial registry update: upsert these providers and/or (re)bind these roles.
    providers: dict[str, ProviderFields] | None = None
    roles: dict[str, str] | None = None
    # #38-C v2 threshold surface: {min_holders, min_replay_count, min_confidence}.
    # None → unchanged; {} → reset to C++ defaults.
    gist_thresholds: dict | None = None


class TestBody(ProviderFields):
    name: str | None = None       # probe a stored provider by name (reuses its key)
    kind: str = "llm"             # "llm" | "embedder" — which live call to make


def _mask(d: dict) -> dict:
    out = {"provider": d.get("provider", "openai"),
           "model": d.get("model", ""), "base_url": d.get("base_url", ""),
           "key_set": bool(d.get("api_key"))}
    if "dim" in d:
        out["dim"] = d["dim"]
    for k in ("max_tokens", "timeout_ms", "max_retries", "enable_thinking"):
        if k in d:
            out[k] = d[k]
    return out


def _public(cfg) -> dict:
    return {"agent": cfg.agent, "tenant": cfg.tenant, "host": cfg.host, "port": cfg.port,
            "providers": {name: _mask(p) for name, p in cfg.providers.items()},
            "roles": dict(cfg.roles),
            "gist_thresholds": dict(cfg.gist_thresholds)}


def _merge_provider(dst: dict, body: ProviderFields) -> None:
    """Merge edits into a provider dict. Omitted/None fields are untouched, so an
    empty api_key on edit keeps the stored secret. Non-positive dim is ignored."""
    for k in ("provider", "model", "base_url", "api_key", "dim"):
        v = getattr(body, k, None)
        if v is None:
            continue
        if k == "api_key" and v == "":
            continue  # blank key on edit → keep existing secret
        if k == "dim" and (not isinstance(v, int) or v <= 0):
            continue
        dst[k] = v
    # 生成参数:max_tokens/timeout_ms 须为正数(非正数忽略);max_retries 允许 0(不重试)。
    for k in ("max_tokens", "timeout_ms"):
        v = getattr(body, k, None)
        if v is not None and v > 0:
            dst[k] = v
    if body.max_retries is not None and body.max_retries >= 0:
        dst["max_retries"] = body.max_retries
    if body.enable_thinking is not None:
        dst["enable_thinking"] = body.enable_thinking


# 这些路由改动的 config 字段：校验失败时按这份清单恢复。
_EDITED_FIELDS = ("providers", "roles", "gist_thresholds")


def _snapshot(cfg) -> dict:
    return {name: copy.deepcopy(getattr(cfg, name)) for name in _EDITED_FIELDS}


def _restore(cfg, saved: dict) -> None:
    """把改动的字段恢复成快照。providers 与 roles 原地恢复：引擎（及引擎池建引擎时）持有的是同一个
    providers dict（裁定 R17），换成新对象的话引擎就看不到之后的改动。"""
    for name in ("providers", "roles"):
        live = getattr(cfg, name)
        live.clear()
        live.update(saved[name])
    cfg.gist_thresholds = saved["gist_thresholds"]


def _validate(cfg) -> None:
    """让 C++ 校验 config 推出的默认世界设置（world_effective_settings 与同步用的整段替换走同一套
    apply_settings 规则），不写任何东西。不合法 → WorldStoreError(invalid_settings)。"""
    _core.world_effective_settings(cfg.world_settings())


def _persist(pool, cfg, eng) -> None:
    """工作线程里调用。写 starling.json（config 里没有活动世界了，db_path/tenant 就是默认世界的），再把默认
    世界的设置同步成新的 config（C++ 整段替换，开着的默认世界引擎换上新设置，要取引擎锁）。没有注册表
    （:memory: 等，裁定 R6）时默认世界的设置每次都由 config 现推，开着的引擎直接换上推出来的设置。"""
    cfg.save()
    if pool.registry is not None:
        pool.sync_default_settings()
    elif eng is not None:
        eng.set_world_settings(pool.context(pool.default_world()).settings)


def _commit(pool, cfg, saved: dict, eng) -> None:
    """校验 → 落盘 → 同步。校验不过：恢复 config，抛 WorldStoreError（world_scope 的异常处理器回 400
    invalid_settings），什么都没写。"""
    try:
        _validate(cfg)
    except _core.WorldStoreError:
        _restore(cfg, saved)
        raise
    _persist(pool, cfg, eng)


async def _write_config(request: Request, edit):
    """在工作线程里持 config_lock 与默认世界的一份租约跑 edit(cfg, pool, eng)，返回它的结果。热更新要在同一个
    引擎上做完（改 config、落盘、同步设置、换适配器）。默认世界正在关闭 → 503 world_busy（world_scope 的
    异常处理器），先取租约再动 config，被拒时什么都没改。不建引擎：默认世界没开着时只改 config 与注册表里的
    设置，引擎建出来时自然读到新设置。工作线程跑完整段才返回（to_thread 不丢下线程）：请求被取消也照样
    还租约、放锁。"""
    state = request.app.state

    def run():
        with state.config_lock:
            pool = state.pool
            with pool.lease(pool.default_world(), build=False) as eng:
                try:
                    return edit(state.config, pool, eng)
                finally:
                    eng = None
    return await to_thread.run_sync(run)


def build_config_router(require_token) -> APIRouter:
    router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])

    @router.get("/config")
    async def get_config(request: Request):
        return _public(request.app.state.config)

    @router.post("/config")
    async def post_config(body: ConfigBody, request: Request):
        return await _write_config(request, lambda cfg, pool, eng: _post_config(body, cfg, pool, eng))

    def _post_config(body: ConfigBody, cfg, pool, eng):
        saved = _snapshot(cfg)
        # Upsert providers (preserve stored keys on blank-key edits).
        for name, pf in (body.providers or {}).items():
            _merge_provider(cfg.providers.setdefault(name, {}), pf)
        # Bind roles (validate target provider exists).
        for role, name in (body.roles or {}).items():
            if name and name not in cfg.providers:
                _restore(cfg, saved)                 # 同一请求里已经改了的 provider 也撤回
                raise HTTPException(status_code=400, detail=f"unknown provider: {name}")
            cfg.roles[role] = name
        # #38-C v2 threshold surface: persist the gist knobs ({} resets to defaults).
        if body.gist_thresholds is not None:
            cfg.gist_thresholds = {
                k: body.gist_thresholds[k]
                for k in ("min_holders", "min_replay_count", "min_confidence",
                          "similarity_threshold", "entity_gist_enabled")
                if body.gist_thresholds.get(k) is not None
            }
        _commit(pool, cfg, saved, eng)               # 校验 → starling.json (0600) → 默认世界的设置
        # Hot-swap only the live roles whose resolved provider changed: a role
        # was (re)bound, or its currently-bound provider was edited (a key-only
        # edit changes no setting, so this is decided here, not by diffing settings).
        touched_providers = set(body.providers or {})
        rebound = set(body.roles or {})
        def _affected(role: str) -> bool:
            return role in rebound or cfg.roles.get(role) in touched_providers
        roles = {role for role in ("extraction", "embedding", "consolidation") if _affected(role)}
        # chat: rebound/edited, or unbound-and-extraction-changed (chat
        # falls back to the extraction provider).
        if _affected("chat") or (not cfg.roles.get("chat") and _affected("extraction")):
            roles.add("chat")
        warnings: list[str] = []
        if eng is not None:
            # 适配器按默认世界刚同步好的设置重建（与 PUT /api/worlds/{name}/settings 同一个
            # reload_adapters，裁定 R35）。嵌入换了会重嵌入；嵌入 provider 拒绝时返回提示（非致命，
            # 保存照常成功）而不是 500。
            warnings = eng.reload_adapters(roles, gist=body.gist_thresholds is not None)
        result = _public(cfg)
        if warnings:
            result["warnings"] = warnings
        return result

    @router.delete("/config/provider/{name}")
    async def delete_provider(name: str, request: Request):
        return await _write_config(request,
                                   lambda cfg, pool, eng: _delete_provider(name, cfg, pool, eng))

    def _delete_provider(name: str, cfg, pool, eng):
        saved = _snapshot(cfg)
        cfg.providers.pop(name, None)
        # Unbind any role pointing at the removed provider, then hot-swap it off.
        unbound = [r for r, n in cfg.roles.items() if n == name]
        for r in unbound:
            cfg.roles[r] = ""
        _commit(pool, cfg, saved, eng)
        if eng is not None and unbound:
            # 解绑的角色在同步后的设置里已清掉：extraction → None（remember 回 409 直到重新绑定），
            # chat 重新解析（可能回退到抽取或变 None），embedding → 占位嵌入（召回降级），
            # consolidation → None（离线回放退回确定性 gist）。
            roles = set(unbound) & {"extraction", "embedding", "chat", "consolidation"}
            if "extraction" in roles:
                roles.add("chat")
            eng.reload_adapters(roles)
        return _public(cfg)

    @router.post("/config/test")
    async def test_config(body: TestBody, request: Request):
        """Probe a provider config WITHOUT persisting it. Builds a transient
        adapter from (stored provider, by name, overlaid with inline fields) and
        issues one minimal live call. Never saves, never returns/logs the key.

        SECURITY: the stored key for provider `name` may only probe its own
        stored base_url. A base_url override (or a name with no stored key)
        requires a fresh api_key in the body — blocks credential exfiltration via
        a redirected probe. A caller-supplied key may go to a caller URL (their risk).
        """
        import time
        from starling.dashboard.engine import _build_chat_adapter, _build_embed_adapter
        cfg = request.app.state.config
        src = cfg.providers.get(body.name or "", {})
        probe = dict(src)
        for k in ("provider", "model", "base_url", "dim"):
            v = getattr(body, k, None)
            if v is None:
                continue
            if k == "dim" and (not isinstance(v, int) or v <= 0):
                continue
            probe[k] = v
        if body.api_key:
            probe["api_key"] = body.api_key
        else:
            overrides_endpoint = body.base_url is not None and body.base_url != src.get("base_url", "")
            if overrides_endpoint or not src.get("api_key"):
                return {"ok": False, "latency_ms": 0,
                        "detail": "需提供 api_key（改了 base_url 或未存密钥时不复用已存密钥）"}

        def _probe() -> tuple[bool, str]:
            if body.kind == "embedder":
                vec = _build_embed_adapter(probe).embed("ping")
                return True, f"dim={len(vec)}"
            r = _build_chat_adapter(probe).extract("ping", "")
            return bool(r.ok), ("ok" if r.ok else (r.error or "failed"))

        t0 = time.monotonic()
        try:
            ok, detail = await to_thread.run_sync(_probe)
            return {"ok": ok, "latency_ms": int((time.monotonic() - t0) * 1000), "detail": detail}
        except Exception as e:  # noqa: BLE001 — surface any build/probe failure as a failed test
            return {"ok": False, "latency_ms": int((time.monotonic() - t0) * 1000), "detail": str(e)}

    return router
