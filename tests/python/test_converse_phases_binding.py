"""三相绑定冒烟(MemoryCore 转发版 —— Task 3 落地后的正式版本):
MemoryCore.converse_prepare → generate_stream → converse_commit 组合的 dict
shape 须与单体 MemoryCore.converse 完全一致。经 `eng._core` 转发,不再直呼
`_core.memory_converse_prepare/...`(那是 Task 2 的绑定层过渡态,已被
_memory_core.py 的薄转发取代)。

夹具模式抄 tests/python/test_dashboard_commands.py / test_dashboard_converse.py
(FakeLLMAdapter + DashboardEngine)。
"""
from starling import _core
from starling.dashboard import DashboardConfig
from starling.dashboard.engine import DashboardEngine

_STUB_JSON = (
    '[{"holder":"self","holder_perspective":"FIRST_PERSON",'
    '"subject":"Bob","predicate":"responsible_for","object":"auth",'
    '"modality":"BELIEVES","polarity":"POS","nesting_depth":0}]'
)


def _core_handles(tmp_path):
    cfg = DashboardConfig(db_path=str(tmp_path / "b.db"), token="")
    eng = DashboardEngine(cfg)
    fake = _core.FakeLLMAdapter()
    fake.set_default_response(_STUB_JSON, True, "")
    eng.llm = fake                 # extraction adapter; chat falls back to it
    return eng._core, fake         # MemoryCore + 抽取/聊天两用 fake


def test_phased_binding_matches_monolith_shape(tmp_path):
    core, fake = _core_handles(tmp_path)
    now = "2026-07-05T10:00:00Z"
    message = "Bob owns auth"

    mono = core.converse(message, holder=None, interlocutor=None, k=6, now=now)

    prepared = core.converse_prepare(message, holder=None, interlocutor=None,
                                     k=6, now=now)
    assert prepared.prompt and "recalled_memory" in prepared.prompt

    gen = core.generate_stream(fake, prepared.prompt, None)
    assert gen.ok

    phased = core.converse_commit(message, prepared, gen, holder=None,
                                  interlocutor=None, k=6, now=now)

    assert set(phased) == set(mono)          # dict 键完全一致
    assert phased["ok"] and phased["reply"] == gen.raw_xml
    assert phased["remember_ok"] in (True, False)


def test_generate_stream_relays_tokens(tmp_path):
    core, fake = _core_handles(tmp_path)
    prepared = core.converse_prepare("hi", holder=None, interlocutor=None, k=6,
                                     now="2026-07-05T10:00:00Z")

    seen: list[str] = []
    gen = core.generate_stream(fake, prepared.prompt, seen.append)
    assert gen.ok and "".join(seen) == gen.raw_xml


def test_is_summary_request_binding_matches_native_rule():
    """绑定与 converse_prepare 用的是同一个 C++ 函数：总结请求豁免先引用后作答。"""
    assert _core.is_summary_request("Can you give me a summary of my progress?")
    assert _core.is_summary_request("RECAP the plan")
    assert not _core.is_summary_request("When is my dentist appointment?")
    # 中文经 pybind 以 UTF-8 传入，判定与 C++ 一致。
    assert _core.is_summary_request("帮我总结一下这周的进展")
    assert not _core.is_summary_request("我的牙医预约是哪天？")


def test_summary_override_default_and_forwarding(tmp_path, monkeypatch):
    core, _ = _core_handles(tmp_path)
    seen = []
    monkeypatch.setattr(_core, "memory_converse_prepare", lambda *a, **kw: seen.append(kw))
    core.converse_prepare("summary", now="2026-07-05T10:00:00Z")
    assert seen[-1]["summary_source_override"] is None
    override = _core.SummarySourceOverride(100, 128000)
    core.converse_prepare("summary", summary_source_override=override,
                          now="2026-07-05T10:00:00Z")
    assert seen[-1]["summary_source_override"] is override
    assert override.k_source == 100 and override.chunk_byte_budget == 128000


def test_summary_override_native_binding_and_none_default(tmp_path):
    import pytest
    core, _ = _core_handles(tmp_path)
    now = "2026-07-05T10:00:00Z"
    base = core.converse_prepare("summary", now=now)
    explicit = core.converse_prepare("summary", now=now, summary_source_override=None)
    override = core.converse_prepare("summary", now=now,
                                    summary_source_override=_core.SummarySourceOverride(100, 128000))
    assert base.context_pack == explicit.context_pack == override.context_pack
    assert base.prompt == explicit.prompt == override.prompt
    with pytest.raises(ValueError, match="source lane:.*summary"):
        core.converse_prepare("summary", now=now,
                              summary_source_override=_core.SummarySourceOverride(0, 128000))


def test_summary_override_above_k_max_is_value_error():
    import pytest
    adapter = _core.SqliteAdapter.open(":memory:")
    embedder = _core.StubEmbeddingAdapter(8)
    index = _core.SqliteBlobVectorIndex()
    source_index = _core.SqliteSourceVectorIndex()
    semantic = _core.SemanticRetriever(adapter, embedder, index)
    lane = _core.SourceLaneDeps(source_index, embedder, 0.56)
    for message in ("Summarize the vault", "What is the code?"):
        with pytest.raises(ValueError, match="summary.*k_source.*k_max"):
            _core.memory_converse_prepare(
                adapter, semantic, tenant_id="default", holder_id="user",
                interlocutor="", adapter_name="test", source_prefix="test-",
                created_at_iso8601="2026-07-05T10:00:00Z", message=message,
                recall_k=10, source_lane=lane,
                summary_source_override=_core.SummarySourceOverride(201, 128000))
