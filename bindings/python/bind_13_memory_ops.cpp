// bind_13_memory_ops — remember/tick_all 管线编排绑定(2026-06-11 边界归位)。
//
// 两个入口都用 gil_scoped_release:remember 内含 LLM 网络调用(真模型
// 20-110s+ 重试退避)、tick_all 内含批量嵌入网络调用——绑定层若持 GIL,
// dashboard 把工作丢进 anyio 线程也救不了事件循环(线程换了、GIL 没放,
// 其余请求与 WS 心跳照样冻结)。释放期间只执行纯 C++,不触碰 Python 对象。

#include "bind_common.hpp"

#include <cstdint>
#include <filesystem>
#include <map>
#include <optional>
#include <stdexcept>
#include <string>
#include <system_error>
#include <vector>

#include "starling/extractor/statement_validator.hpp"
#include "starling/memory/memory_digest.hpp"
#include "starling/memory/memory_events.hpp"
#include "starling/memory/memory_ops.hpp"
#include "starling/persistence/connection.hpp"
#include "starling/store/episodic_event_store.hpp"

namespace starling::bindings {

namespace {

// Build a TokenSink that relays deltas to a Python callback, or an empty sink
// when on_token is None. Extracted verbatim (incl. every comment) from the
// memory_converse binding body so memory_generate_stream can share the exact
// same noexcept PyGILState relay logic — behavior-neutral extraction, not a
// rewrite.
//
// Lifetime contract (unchanged): the sink captures on_token BY REFERENCE, so
// the caller must keep on_token alive across every sink invocation — both
// call sites (memory_converse's and memory_generate_stream's lambdas) satisfy
// this because on_token is a function parameter that outlives the C++ call
// the sink is invoked from (converse()/generate_stream() call the sink
// synchronously on this thread, inside the gil_scoped_release block the
// caller wraps around it).
starling::extractor::TokenSink make_token_sink(const py::object& on_token) {
    // 流式回调:非空 Python on_token → C++ TokenSink。converse 在 GIL
    // 释放下运行(下方 release 块),sink 每个 delta 重新 acquire GIL 再
    // 回调 Python(标准 pybind「从已释放 GIL 回调 Python」模式)。
    // on_token=None → 空 sink,converse 走非流式路径(等价旧行为)。
    // sink 在 release 块外声明,其析构(decref on_token)在 GIL 重持后发生。
    starling::extractor::TokenSink sink;
    if (!on_token.is_none()) {
        // Capture on_token by reference: converse() calls the sink
        // synchronously on THIS thread (inside the gil_scoped_release
        // block below), so the param outlives every sink invocation,
        // and a reference capture keeps the closure trivially
        // destructible (no py::object copy/move/dtor to reason about).
        sink = [&on_token](std::string_view delta) noexcept {
            // Relay one delta to the Python callback. Pure C-API + raw
            // PyGILState ensure/release — all noexcept — so the sink
            // PROVABLY cannot throw: nothing can escape into the C++
            // converse across the released GIL (a thrown pybind call or
            // a non-noexcept gil RAII ctor would). The C-API sets PyErr
            // instead of throwing; a failed relay just drops the delta
            // (the reply is still generated + persisted). Runs on the
            // C++ worker thread, so PyGILState_Ensure (not a held GIL)
            // is the correct acquire.
            const PyGILState_STATE gstate = PyGILState_Ensure();
            PyObject* arg = PyUnicode_FromStringAndSize(
                delta.data(), static_cast<Py_ssize_t>(delta.size()));
            if (arg != nullptr) {
                Py_XDECREF(PyObject_CallOneArg(on_token.ptr(), arg));
                Py_DECREF(arg);
            }
            if (PyErr_Occurred() != nullptr) {
                PyErr_Clear();
            }
            PyGILState_Release(gstate);
        };
    }
    return sink;
}

}  // namespace

void bind_13_memory_ops(pybind11::module_& m) {
    using namespace pybind11::literals;

    m.def("memory_remember",
          [](starling::persistence::SqliteAdapter& adapter,
             starling::extractor::LLMAdapter& llm,
             const std::string& prompt_template,
             const std::string& tenant_id, const std::string& holder_id,
             const std::string& interlocutor, const std::string& adapter_name,
             const std::string& source_prefix, const std::string& created_at_iso8601,
             const py::bytes& payload,
             starling::extractor::ValidationPolicy policy) {
              starling::memoryops::RememberParams p;
              p.tenant_id         = tenant_id;
              p.holder_id         = holder_id;
              p.interlocutor      = interlocutor;
              p.adapter_name      = adapter_name;
              p.source_prefix     = source_prefix;
              p.created_at_iso8601 = created_at_iso8601;
              {   // bytes → vector 必须在持 GIL 时完成
                  const std::string raw = payload;
                  p.payload.assign(raw.begin(), raw.end());
              }
              starling::memoryops::RememberOutcome r;
              {
                  py::gil_scoped_release release;
                  r = starling::memoryops::remember(adapter, llm, prompt_template, p, policy);
              }
              return py::dict("engram_ref"_a = r.engram_ref,
                              "statement_ids"_a = r.statement_ids,
                              "outcome"_a = r.outcome,
                              "extraction_failed"_a = r.extraction_failed,
                              "failure_category"_a = r.failure_category,
                              "failure_detail"_a = r.failure_detail,
                              "catalog_version"_a = r.catalog_version,
                              "source_preserved"_a = r.source_preserved,
                              "structured_claims_persisted"_a = r.structured_claims_persisted,
                              "accepted_by_predicate"_a = r.accepted_by_predicate,
                              "rejected_by_predicate"_a = r.rejected_by_predicate);
          },
          py::arg("adapter"), py::arg("llm"), py::arg("prompt_template"),
          py::arg("tenant_id"), py::arg("holder_id"), py::arg("interlocutor"),
          py::arg("adapter_name"), py::arg("source_prefix"),
          py::arg("created_at_iso8601"), py::arg("payload"),
          py::arg("policy") = starling::extractor::ValidationPolicy{});

    // Phase 2c: chat-with-memory turn. chat_llm + extraction_llm may be the same
    // object (chat role falls back to extraction). GIL released around the whole
    // C++ converse (recall + generate network + remember); the adapters are C++
    // objects so their virtuals run lock-free during the release.
    m.def("memory_converse",
          [](starling::persistence::SqliteAdapter& adapter,
             starling::extractor::LLMAdapter& chat_llm,
             starling::extractor::LLMAdapter& extraction_llm,
             starling::retrieval::SemanticRetriever& semantic,
             const std::string& extraction_prompt,
             const std::string& tenant_id, const std::string& holder_id,
             const std::string& interlocutor, const std::string& adapter_name,
             const std::string& source_prefix, const std::string& created_at_iso8601,
             const std::string& message, int recall_k,
             const starling::extractor::ValidationPolicy& policy,
             const py::object& on_token,
             std::optional<starling::retrieval::SourceLaneDeps> source_lane) {
              starling::memoryops::ConverseParams p;
              p.tenant_id          = tenant_id;
              p.holder_id          = holder_id;
              p.interlocutor       = interlocutor;
              p.adapter_name       = adapter_name;
              p.source_prefix      = source_prefix;
              p.created_at_iso8601 = created_at_iso8601;
              p.message            = message;
              p.recall_k           = recall_k;
              // 形态 S：左脑 lane（None = 关闭）。与 memory_converse_prepare 同约定：
              // 只存指针，调用方须持有 index/embedder。
              p.source_lane        = std::move(source_lane);
              auto sink = make_token_sink(on_token);
              starling::memoryops::ConverseOutcome r;
              {
                  py::gil_scoped_release release;
                  r = starling::memoryops::converse(adapter, chat_llm, extraction_llm,
                                                    semantic, extraction_prompt, p, policy, sink);
              }
              return py::dict("reply"_a = r.reply, "ok"_a = r.ok, "error"_a = r.error,
                              "context_pack"_a = r.context_pack, "abstained"_a = r.abstained,
                              "statement_ids"_a = r.statement_ids,
                              "remember_ok"_a = r.remember_ok,
                              "remember_error"_a = r.remember_error,
                              "gen_prompt_tokens"_a = r.gen_prompt_tokens,
                              "gen_completion_tokens"_a = r.gen_completion_tokens,
                              "gen_total_tokens"_a = r.gen_total_tokens,
                              "gen_latency_ms"_a = r.gen_latency_ms);
          },
          py::arg("adapter"), py::arg("chat_llm"), py::arg("extraction_llm"),
          py::arg("semantic"), py::arg("extraction_prompt"),
          py::arg("tenant_id"), py::arg("holder_id"), py::arg("interlocutor"),
          py::arg("adapter_name"), py::arg("source_prefix"),
          py::arg("created_at_iso8601"), py::arg("message"), py::arg("recall_k") = 6,
          py::arg("policy") = starling::extractor::ValidationPolicy{},
          py::arg("on_token") = py::none(),
          py::arg("source_lane") = py::none());

    // 三相拆分(2026-07-05 锁外生成):host 在 prepare/commit 段持引擎锁、
    // generate 段释放——三个绑定共享单体的语义(C++ 内联同三相)。
    py::class_<starling::memoryops::ConversePrepared>(m, "ConversePrepared")
        .def_readonly("prompt",       &starling::memoryops::ConversePrepared::prompt)
        .def_readonly("context_pack", &starling::memoryops::ConversePrepared::context_pack)
        .def_readonly("abstained",    &starling::memoryops::ConversePrepared::abstained)
        .def_readonly("created_at_iso8601",
                       &starling::memoryops::ConversePrepared::created_at_iso8601)
        // 先引用、后作答：为 true 时回复开头有 <evidence> 块，流式回调须经
        // memory_generate_stream(grounded=True) 过滤，commit 会自行剥掉。
        .def_readonly("grounded",     &starling::memoryops::ConversePrepared::grounded);

    // 返回 None = 证据块未闭合（截断在摘录阶段，无回复）。
    m.def("strip_evidence_block", &starling::memoryops::strip_evidence_block,
          py::arg("reply"));
    // 总结类请求豁免先引用后作答的判定（与 converse_prepare 同一函数）。
    m.def("is_summary_request", &starling::memoryops::is_summary_request,
          py::arg("message"));

    m.def("memory_converse_prepare",
          [](starling::persistence::SqliteAdapter& adapter,
             starling::retrieval::SemanticRetriever& semantic,
             const std::string& tenant_id, const std::string& holder_id,
             const std::string& interlocutor, const std::string& adapter_name,
             const std::string& source_prefix, const std::string& created_at_iso8601,
             const std::string& message, int recall_k,
             std::optional<starling::retrieval::SourceLaneDeps> source_lane) {
              starling::memoryops::ConverseParams params;
              params.tenant_id          = tenant_id;
              params.holder_id          = holder_id;
              params.interlocutor       = interlocutor;
              params.adapter_name       = adapter_name;
              params.source_prefix      = source_prefix;
              params.created_at_iso8601 = created_at_iso8601;
              params.message            = message;
              params.recall_k           = recall_k;
              // 形态 S：左脑 lane（None = 关闭）。只存指针，调用方须持有 index/embedder。
              params.source_lane        = std::move(source_lane);
              starling::memoryops::ConversePrepared prep;
              {
                  py::gil_scoped_release release;   // 内含 query-embed 网络
                  prep = starling::memoryops::converse_prepare(adapter, semantic, params);
              }
              return prep;
          },
          py::arg("adapter"), py::arg("semantic"), py::arg("tenant_id"),
          py::arg("holder_id"), py::arg("interlocutor"), py::arg("adapter_name"),
          py::arg("source_prefix"), py::arg("created_at_iso8601"),
          py::arg("message"), py::arg("recall_k") = 6,
          py::arg("source_lane") = py::none());

    m.def("memory_generate_stream",
          [](starling::extractor::LLMAdapter& chat_llm, const std::string& prompt,
             const py::object& on_token, bool grounded) {
              auto sink = make_token_sink(on_token);
              starling::extractor::LLMResponse resp;
              if (!grounded || !sink) {
                  py::gil_scoped_release release;   // 纯网络段
                  resp = chat_llm.generate_stream(prompt, sink);
                  return resp;
              }
              // 证据块不进流式回调：与单体 converse 同一个过滤器。raw_xml 仍是完整
              // 输出（含块），由 converse_commit 剥掉——commit 是回复的唯一权威。
              starling::memoryops::EvidenceStreamFilter filter(sink);
              {
                  py::gil_scoped_release release;
                  resp = chat_llm.generate_stream(
                      prompt, [&filter](std::string_view delta) { filter(delta); });
                  filter.finish();
              }
              return resp;
          },
          py::arg("chat_llm"), py::arg("prompt"), py::arg("on_token") = py::none(),
          py::arg("grounded") = false);

    m.def("memory_converse_commit",
          [](starling::persistence::SqliteAdapter& adapter,
             starling::extractor::LLMAdapter& extraction_llm,
             const std::string& extraction_prompt,
             const std::string& tenant_id, const std::string& holder_id,
             const std::string& interlocutor, const std::string& adapter_name,
             const std::string& source_prefix, const std::string& created_at_iso8601,
             const std::string& message, int recall_k,
             const starling::memoryops::ConversePrepared& prepared,
             const starling::extractor::LLMResponse& gen_resp,
             const starling::extractor::ValidationPolicy& policy) {
              starling::memoryops::ConverseParams params;
              params.tenant_id          = tenant_id;
              params.holder_id          = holder_id;
              params.interlocutor       = interlocutor;
              params.adapter_name       = adapter_name;
              params.source_prefix      = source_prefix;
              params.created_at_iso8601 = created_at_iso8601;
              params.message            = message;
              params.recall_k           = recall_k;
              starling::memoryops::ConverseOutcome outcome;
              {
                  py::gil_scoped_release release;   // 内含 extraction 网络
                  outcome = starling::memoryops::converse_commit(
                      adapter, extraction_llm, extraction_prompt, params, prepared,
                      gen_resp, policy);
              }
              return py::dict("reply"_a = outcome.reply, "ok"_a = outcome.ok,
                              "error"_a = outcome.error,
                              "context_pack"_a = outcome.context_pack,
                              "abstained"_a = outcome.abstained,
                              "statement_ids"_a = outcome.statement_ids,
                              "remember_ok"_a = outcome.remember_ok,
                              "remember_error"_a = outcome.remember_error,
                              "gen_prompt_tokens"_a = outcome.gen_prompt_tokens,
                              "gen_completion_tokens"_a = outcome.gen_completion_tokens,
                              "gen_total_tokens"_a = outcome.gen_total_tokens,
                              "gen_latency_ms"_a = outcome.gen_latency_ms);
          },
          py::arg("adapter"), py::arg("extraction_llm"), py::arg("extraction_prompt"),
          py::arg("tenant_id"), py::arg("holder_id"), py::arg("interlocutor"),
          py::arg("adapter_name"), py::arg("source_prefix"),
          py::arg("created_at_iso8601"), py::arg("message"), py::arg("recall_k") = 6,
          py::arg("prepared"), py::arg("gen_resp"),
          py::arg("policy") = starling::extractor::ValidationPolicy{});

    // 方案2 三相(2026-07-12 remember extraction 出锁):host 在 prepare/commit
    // 段持引擎锁、extract 段释放——写事务不再跨 extraction 网络。opaque 句柄只在
    // 三段间传递,Python 不解构(照 converse 三相绑定范式)。
    py::class_<starling::memoryops::RememberPrepared>(m, "RememberPrepared")
        .def_readonly("engram_ref",     &starling::memoryops::RememberPrepared::engram_ref)
        .def_readonly("outcome",        &starling::memoryops::RememberPrepared::outcome)
        .def_readonly("should_extract", &starling::memoryops::RememberPrepared::should_extract)
        .def_readonly("created_at_iso8601",   // #6:Python(episodic)从此读,不再自持 created_iso
                       &starling::memoryops::RememberPrepared::created_at_iso8601);

    // 纯句柄:extract→commit 之间传递,无成员导出。
    // NOLINTNEXTLINE(bugprone-unused-raii) -- pybind 类型注册是有意的副作用,无需命名对象
    py::class_<starling::extractor::ExtractionLlmResult>(m, "ExtractionLlmResult");
    py::class_<starling::memoryops::RememberLlmBundle>(m, "RememberLlmBundle");

    m.def("memory_remember_bundle_receipt",
          &starling::memoryops::remember_bundle_receipt,
          py::arg("extracted"));

    m.def("memory_remember_holders",
          [](starling::persistence::SqliteAdapter& adapter,
             starling::extractor::LLMAdapter& llm,
             const std::string& belief_prompt,
             const std::string& episodic_prompt,
             const std::string& general_fact_prompt,
             const py::list& holder_inputs,
             const starling::extractor::ValidationPolicy& policy) {
              std::vector<starling::memoryops::RememberParams> params;
              params.reserve(holder_inputs.size());
              for (const auto& item : holder_inputs) {
                  const py::dict input = item.cast<py::dict>();
                  starling::memoryops::RememberParams p;
                  p.tenant_id = input["tenant_id"].cast<std::string>();
                  p.holder_id = input["holder_id"].cast<std::string>();
                  p.interlocutor = input["interlocutor"].cast<std::string>();
                  p.adapter_name = input["adapter_name"].cast<std::string>();
                  p.source_prefix = input["source_prefix"].cast<std::string>();
                  p.created_at_iso8601 = input["created_at_iso8601"].cast<std::string>();
                  const std::string raw = input["payload"].cast<py::bytes>();
                  p.payload.assign(raw.begin(), raw.end());
                  params.push_back(std::move(p));
              }
              const starling::memoryops::RememberPrompts prompts{
                  belief_prompt, episodic_prompt, general_fact_prompt};
              std::vector<starling::memoryops::RememberOutcome> outcomes;
              {
                  py::gil_scoped_release release;
                  outcomes = starling::memoryops::remember_holders(adapter, llm, prompts, params, policy);
              }
              py::list result;
              for (const auto& outcome : outcomes) {
                  result.append(py::dict(
                      "holder_id"_a = outcome.holder_id,
                      "engram_ref"_a = outcome.engram_ref,
                      "statement_ids"_a = outcome.statement_ids,
                      "outcome"_a = outcome.outcome,
                      "extraction_failed"_a = outcome.extraction_failed,
                      "failure_category"_a = outcome.failure_category,
                      "failure_detail"_a = outcome.failure_detail,
                      "catalog_version"_a = outcome.catalog_version,
                      "source_preserved"_a = outcome.source_preserved,
                      "structured_claims_persisted"_a = outcome.structured_claims_persisted,
                      "accepted_by_predicate"_a = outcome.accepted_by_predicate,
                      "rejected_by_predicate"_a = outcome.rejected_by_predicate,
                      "receipt"_a = outcome.receipt));
              }
              return result;
          },
          py::arg("adapter"), py::arg("llm"),
          py::arg("belief_prompt"), py::arg("episodic_prompt"),
          py::arg("general_fact_prompt"), py::arg("holder_inputs"),
          py::arg("policy") = starling::extractor::ValidationPolicy{});

    m.def("memory_remember_prepare",
          [](starling::persistence::SqliteAdapter& adapter,
             const std::string& tenant_id, const std::string& holder_id,
             const std::string& interlocutor, const std::string& adapter_name,
             const std::string& source_prefix, const std::string& created_at_iso8601,
             const py::bytes& payload) {
              starling::memoryops::RememberParams params;
              params.tenant_id          = tenant_id;
              params.holder_id          = holder_id;
              params.interlocutor       = interlocutor;
              params.adapter_name       = adapter_name;
              params.source_prefix      = source_prefix;
              params.created_at_iso8601 = created_at_iso8601;
              {   // bytes → vector 须持 GIL
                  const std::string raw = payload;
                  params.payload.assign(raw.begin(), raw.end());
              }
              starling::memoryops::RememberPrepared prep;
              {
                  py::gil_scoped_release release;
                  prep = starling::memoryops::remember_prepare(adapter, params);
              }
              return prep;
          },
          py::arg("adapter"), py::arg("tenant_id"), py::arg("holder_id"),
          py::arg("interlocutor"), py::arg("adapter_name"), py::arg("source_prefix"),
          py::arg("created_at_iso8601"), py::arg("payload"));

    m.def("memory_extract_llm",
          [](starling::persistence::SqliteAdapter& adapter,
             starling::extractor::LLMAdapter& llm,
             const std::string& prompt_template, const std::string& holder_id,
             const py::bytes& payload,
             const starling::extractor::ValidationPolicy& policy) {
              starling::memoryops::RememberParams params;
              params.holder_id = holder_id;
              {
                  const std::string raw = payload;
                  params.payload.assign(raw.begin(), raw.end());
              }
              starling::extractor::ExtractionLlmResult out;
              {
                  py::gil_scoped_release release;   // 纯 LLM 段
                  out = starling::memoryops::extract_llm(
                      adapter, llm, prompt_template, params, policy);
              }
              return out;
          },
          py::arg("adapter"), py::arg("llm"), py::arg("prompt_template"),
          py::arg("holder_id"), py::arg("payload"),
          py::arg("policy") = starling::extractor::ValidationPolicy{});

    m.def("memory_remember_commit",
          // #6:不收 created_at_iso8601——commit 以 prepared.created_at_iso8601 为权威
          // (防 prepare/commit 时戳漂移,镜像 converse_commit)。
          [](starling::persistence::SqliteAdapter& adapter,
             starling::extractor::LLMAdapter& llm,
             const std::string& tenant_id, const std::string& holder_id,
             const std::string& interlocutor,
             const starling::memoryops::RememberPrepared& prepared,
             const starling::extractor::ExtractionLlmResult& llm_result,
             const starling::extractor::ValidationPolicy& policy) {
              starling::memoryops::RememberParams params;
              params.tenant_id    = tenant_id;
              params.holder_id    = holder_id;
              params.interlocutor = interlocutor;
              // params.created_at_iso8601 留空:commit 用 prepared 的权威时戳。
              starling::memoryops::RememberOutcome result;
              {
                  py::gil_scoped_release release;   // 内含 persist txn 写 + 写后泵
                  result = starling::memoryops::remember_commit(
                      adapter, llm, params, prepared, llm_result, policy);
              }
              return py::dict("engram_ref"_a = result.engram_ref,
                              "statement_ids"_a = result.statement_ids,
                              "outcome"_a = result.outcome,
                              "extraction_failed"_a = result.extraction_failed,
                              "failure_category"_a = result.failure_category,
                              "failure_detail"_a = result.failure_detail,
                              "catalog_version"_a = result.catalog_version,
                              "source_preserved"_a = result.source_preserved,
                              "structured_claims_persisted"_a = result.structured_claims_persisted,
                              "accepted_by_predicate"_a = result.accepted_by_predicate,
                              "rejected_by_predicate"_a = result.rejected_by_predicate);
          },
          py::arg("adapter"), py::arg("llm"), py::arg("tenant_id"), py::arg("holder_id"),
          py::arg("interlocutor"),
          py::arg("prepared"), py::arg("llm_result"),
          py::arg("policy") = starling::extractor::ValidationPolicy{});

    m.def("memory_remember_extract_all",
          [](starling::persistence::SqliteAdapter& adapter,
             starling::extractor::LLMAdapter& llm,
             const std::string& belief_prompt,
             const std::string& episodic_prompt,
             const std::string& general_fact_prompt,
             const std::string& holder_id,
             const py::bytes& payload,
             const starling::extractor::ValidationPolicy& policy) {
              starling::memoryops::RememberParams params;
              params.holder_id = holder_id;
              {
                  const std::string raw = payload;
                  params.payload.assign(raw.begin(), raw.end());
              }
              starling::memoryops::RememberPrompts prompts{
                  belief_prompt, episodic_prompt, general_fact_prompt};
              starling::memoryops::RememberLlmBundle result;
              {
                  py::gil_scoped_release release;
                  result = starling::memoryops::remember_extract_all(
                      adapter, llm, params, prompts, policy);
              }
              return result;
          },
          py::arg("adapter"), py::arg("llm"), py::arg("belief_prompt"),
          py::arg("episodic_prompt"), py::arg("general_fact_prompt"),
          py::arg("holder_id"), py::arg("payload"),
          py::arg("policy") = starling::extractor::ValidationPolicy{});

    m.def("memory_remember_commit_all",
          [](starling::persistence::SqliteAdapter& adapter,
             starling::extractor::LLMAdapter& llm,
             const std::string& tenant_id,
             const std::string& holder_id,
             const std::string& interlocutor,
             const starling::memoryops::RememberPrepared& prepared,
             const starling::memoryops::RememberLlmBundle& extracted,
             const starling::extractor::ValidationPolicy& policy) {
              starling::memoryops::RememberParams params;
              params.tenant_id = tenant_id;
              params.holder_id = holder_id;
              params.interlocutor = interlocutor;
              starling::memoryops::RememberOutcome result;
              {
                  py::gil_scoped_release release;
                  result = starling::memoryops::remember_commit_all(
                      adapter, llm, params, prepared, extracted, policy);
              }
              return py::dict("engram_ref"_a = result.engram_ref,
                              "statement_ids"_a = result.statement_ids,
                              "outcome"_a = result.outcome,
                              "extraction_failed"_a = result.extraction_failed,
                              "failure_category"_a = result.failure_category,
                              "failure_detail"_a = result.failure_detail,
                              "catalog_version"_a = result.catalog_version,
                              "source_preserved"_a = result.source_preserved,
                              "structured_claims_persisted"_a = result.structured_claims_persisted,
                              "accepted_by_predicate"_a = result.accepted_by_predicate,
                              "rejected_by_predicate"_a = result.rejected_by_predicate);
          },
          py::arg("adapter"), py::arg("llm"), py::arg("tenant_id"),
          py::arg("holder_id"), py::arg("interlocutor"), py::arg("prepared"),
          py::arg("extracted"),
          py::arg("policy") = starling::extractor::ValidationPolicy{});

    m.def("memory_remember_all",
          [](starling::persistence::SqliteAdapter& adapter,
             starling::extractor::LLMAdapter& llm,
             const std::string& belief_prompt,
             const std::string& episodic_prompt,
             const std::string& general_fact_prompt,
             const std::string& tenant_id, const std::string& holder_id,
             const std::string& interlocutor, const std::string& adapter_name,
             const std::string& source_prefix, const std::string& created_at_iso8601,
             const py::bytes& payload,
             const starling::extractor::ValidationPolicy& policy) {
              starling::memoryops::RememberParams params;
              params.tenant_id = tenant_id;
              params.holder_id = holder_id;
              params.interlocutor = interlocutor;
              params.adapter_name = adapter_name;
              params.source_prefix = source_prefix;
              params.created_at_iso8601 = created_at_iso8601;
              {
                  const std::string raw = payload;
                  params.payload.assign(raw.begin(), raw.end());
              }
              const starling::memoryops::RememberPrompts prompts{
                  belief_prompt, episodic_prompt, general_fact_prompt};
              starling::memoryops::RememberOutcome result;
              {
                  py::gil_scoped_release release;
                  result = starling::memoryops::remember_all(
                      adapter, llm, params, prompts, policy);
              }
              return py::dict("engram_ref"_a = result.engram_ref,
                              "statement_ids"_a = result.statement_ids,
                              "outcome"_a = result.outcome,
                              "extraction_failed"_a = result.extraction_failed,
                              "failure_category"_a = result.failure_category,
                              "failure_detail"_a = result.failure_detail,
                              "catalog_version"_a = result.catalog_version,
                              "source_preserved"_a = result.source_preserved,
                              "structured_claims_persisted"_a = result.structured_claims_persisted,
                              "accepted_by_predicate"_a = result.accepted_by_predicate,
                              "rejected_by_predicate"_a = result.rejected_by_predicate);
          },
          py::arg("adapter"), py::arg("llm"), py::arg("belief_prompt"),
          py::arg("episodic_prompt"), py::arg("general_fact_prompt"),
          py::arg("tenant_id"), py::arg("holder_id"), py::arg("interlocutor"),
          py::arg("adapter_name"), py::arg("source_prefix"),
          py::arg("created_at_iso8601"), py::arg("payload"),
          py::arg("policy") = starling::extractor::ValidationPolicy{});

    m.def("memory_tick_all",
          [](starling::persistence::SqliteAdapter& adapter,
             starling::embedding::EmbeddingWorker& worker,
             starling::prospective::PolicyEngine& policy,
             const std::string& now_iso,
             starling::RuntimeHealth health,
             bool skip_embed) {
              starling::memoryops::TickOutcome t;
              {
                  py::gil_scoped_release release;
                  t = starling::memoryops::tick_all(adapter, worker, policy, now_iso, health,
                                                    skip_embed);
              }
              py::list stages;
              for (const auto& timing : t.stage_timings_ms) {
                  stages.append(py::dict("stage"_a = timing.stage,
                                         "ms"_a = timing.duration_ms));
              }
              return py::dict("embedded"_a = t.embedded, "fired"_a = t.fired,
                              "broken"_a = t.broken, "auto_withdrawn"_a = t.auto_withdrawn,
                              "replay_sampled"_a = t.replay_sampled,
                              "consolidated"_a = t.consolidated,
                              "ttl_archived"_a = t.ttl_archived,
                              "projected"_a = t.projected,
                              "dispatched"_a = t.dispatched,
                              "stage_timings_ms"_a = stages,
                              "stages_skipped"_a = t.stages_skipped);
          },
          py::arg("adapter"), py::arg("worker"), py::arg("policy"), py::arg("now_iso"),
          py::arg("health") = starling::RuntimeHealth::READY,
          // 裁定 R54:嵌入端点不可用时只跳过 Embed 阶段(stages_skipped 记 "embed")。
          py::arg("skip_embed") = false);

    // 只跑嵌入阶段(不做承诺/回放/投影/出箱维护):循环在 C++ memoryops::embed_pending。
    // 嵌入逐批走网络,call_guard 释放 GIL;lambda 返回纯 C++ map,call_guard 析构
    // (GIL 回来)之后才由 pybind11 转成 dict,释放期间不碰任何 Python 对象。
    m.def("memory_embed_pending",
          [](starling::persistence::SqliteAdapter& adapter,
             starling::embedding::EmbeddingWorker& worker,
             const std::string& now_iso) {
              const auto out = starling::memoryops::embed_pending(adapter, worker, now_iso);
              return std::map<std::string, int>{
                  {"embedded", out.embedded},
                  {"failed", out.failed},
              };
          },
          py::arg("adapter"), py::arg("worker"), py::arg("now_iso"),
          py::call_guard<py::gil_scoped_release>());

    m.def("memory_forget",
          [](starling::persistence::SqliteAdapter& adapter,
             const std::string& tenant, const std::vector<std::string>& ids,
             const std::string& now_iso) {
              int n = 0;
              {
                  py::gil_scoped_release release;
                  n = starling::memoryops::forget(adapter, tenant, ids, now_iso);
              }
              return n;
          },
          py::arg("adapter"), py::arg("tenant"), py::arg("ids"), py::arg("now_iso"));

    // 设置页「危险区」:单事务物理清空一个租户的全部记忆(保留 kind='self' 认知体)。
    // 门前抛 WriteGateRejected(零写);空 tenant 抛 ValueError。index 只给向量存在
    // SQLite 之外的后端(zvec);None = 不碰外部索引。
    m.def("memory_clear_tenant",
          [](starling::persistence::SqliteAdapter& adapter, const std::string& tenant,
             starling::vector::VectorIndex* index) {
              starling::memoryops::ClearTenantOutcome out;
              {
                  py::gil_scoped_release release;
                  out = starling::memoryops::clear_tenant(adapter, tenant, index);
              }
              return py::dict("deleted"_a = out.deleted, "total"_a = out.total,
                              "vectors_removed"_a = out.vectors_removed,
                              "vector_remove_failed"_a = out.vector_remove_failed);
          },
          py::arg("adapter"), py::arg("tenant"), py::arg("index") = py::none());

    // 片 6 干预集:人工审批 review_requested → approved(守卫幂等、tenant-scoped)。
    // 返回转换计数(1=已批准,0=非 review_requested 行 no-op)。reject 不在此(=memory_forget)。
    m.def("memory_approve_review",
          [](starling::persistence::SqliteAdapter& adapter,
             const std::string& tenant, const std::string& stmt_id,
             const std::string& now_iso) {
              int n = 0;
              {
                  py::gil_scoped_release release;
                  n = starling::memoryops::approve_review(adapter, tenant, stmt_id, now_iso);
              }
              return n;
          },
          py::arg("adapter"), py::arg("tenant"), py::arg("stmt_id"), py::arg("now_iso"));

    // sub-project A phase 6 Task 6.1:暴露 EpisodicEventStore::latest_event_location。
    // 主题 theme 的最高 seq 事件的 location(地面真值「当前所在」),无事件返回 ""。
    // 纯本地 SQLite 读(无网络),薄绑定转发到核心层单属主 store。
    m.def("latest_event_location",
          [](starling::persistence::Connection& conn, const std::string& tenant,
             const std::string& theme) {
              starling::store::EpisodicEventStore store(conn);
              return store.latest_event_location(tenant, theme);
          },
          py::arg("connection"), py::arg("tenant"), py::arg("theme"));

    // 迁移 0038 events 表的读侧,纯本地 SQLite 读(无网络),不放 GIL,薄转发到
    // memoryops::next_seq / pending_events。查询在 C++,这里只把行转成 dict。
    m.def("memory_next_seq",
          [](starling::persistence::Connection& conn, const std::string& tenant) {
              return starling::memoryops::next_seq(conn, tenant);
          },
          py::arg("connection"), py::arg("tenant"));

    m.def("memory_pending_events",
          [](starling::persistence::Connection& conn, const std::string& tenant) {
              py::list out;
              for (const auto& row : starling::memoryops::pending_events(conn, tenant)) {
                  out.append(py::dict("key"_a = row.key, "text"_a = row.text,
                                      "interlocutor"_a = row.interlocutor, "seq"_a = row.seq,
                                      "failure_category"_a = row.failure_category));
              }
              return out;
          },
          py::arg("connection"), py::arg("tenant"));

    // 同一个 key 要写另一段文本、重试 pending 行时换了 interlocutor,或该 key 已
    // abandoned → starling.EventConflict(三种情形的消息各不相同)。
    py::register_exception<starling::memoryops::EventConflict>(
        m, "EventConflict", PyExc_RuntimeError);

    // events 表的写侧(迁移 0038):按 key 三段写入。第 2 段含 LLM 抽取(真模型几十秒
    // 起),故整段释放 GIL——释放期间只执行纯 C++,不触碰 Python 对象。
    m.def("memory_remember_event",
          [](starling::persistence::SqliteAdapter& adapter,
             starling::extractor::LLMAdapter& llm,
             const std::string& belief_prompt,
             const std::string& episodic_prompt,
             const std::string& general_fact_prompt,
             const std::string& tenant_id, const std::string& holder_id,
             const std::string& interlocutor, const std::string& adapter_name,
             const std::string& source_prefix, const std::string& key,
             const std::string& text, const std::string& epoch_iso,
             const starling::extractor::ValidationPolicy& policy) {
              starling::memoryops::RememberParams base;
              base.tenant_id     = tenant_id;
              base.holder_id     = holder_id;
              base.interlocutor  = interlocutor;
              base.adapter_name  = adapter_name;
              base.source_prefix = source_prefix;
              // payload 与 created_at_iso8601 由 remember_event 从 key+text 与
              // epoch+seq 推出,这里不填。
              const starling::memoryops::RememberPrompts prompts{
                  .belief = belief_prompt,
                  .episodic = episodic_prompt,
                  .general_fact = general_fact_prompt,
              };
              starling::memoryops::RememberOutcome result;
              {
                  py::gil_scoped_release release;
                  result = starling::memoryops::remember_event(
                      adapter, llm, base, prompts, key, text, epoch_iso, policy);
              }
              return py::dict("engram_ref"_a = result.engram_ref,
                              "statement_ids"_a = result.statement_ids,
                              "outcome"_a = result.outcome,
                              "extraction_failed"_a = result.extraction_failed,
                              "failure_category"_a = result.failure_category,
                              "failure_detail"_a = result.failure_detail,
                              "catalog_version"_a = result.catalog_version,
                              "source_preserved"_a = result.source_preserved,
                              "structured_claims_persisted"_a = result.structured_claims_persisted,
                              "accepted_by_predicate"_a = result.accepted_by_predicate,
                              "rejected_by_predicate"_a = result.rejected_by_predicate);
          },
          py::arg("adapter"), py::arg("llm"), py::arg("belief_prompt"),
          py::arg("episodic_prompt"), py::arg("general_fact_prompt"),
          py::arg("tenant_id"), py::arg("holder_id"), py::arg("interlocutor"),
          py::arg("adapter_name"), py::arg("source_prefix"), py::arg("key"),
          py::arg("text"), py::arg("epoch_iso"),
          py::arg("policy") = starling::extractor::ValidationPolicy{});

    // 放弃一个 key(events 表,迁移 0038):一个短本地事务,无网络,不放 GIL。
    // committed → starling.EventConflict(上面已注册);未知 key → C++ 抛
    // std::out_of_range。pybind11 对 out_of_range 的默认译法是 IndexError,不是
    // KeyError,所以这里显式改译:调用方拼错 key 该得到 KeyError。
    m.def("memory_abandon_event",
          [](starling::persistence::SqliteAdapter& adapter, const std::string& tenant,
             const std::string& key) {
              try {
                  starling::memoryops::abandon_event(adapter, tenant, key);
              } catch (const std::out_of_range& e) {
                  throw py::key_error(e.what());
              }
          },
          py::arg("adapter"), py::arg("tenant"), py::arg("key"));

    // library 的逻辑摘要与语句的内容 id(memoryops::digest / content_ids)。规范化与
    // 哈希都在 C++,这里只转发。纯本地 SQLite 读,无网络,不放 GIL。
    //
    // memory_content_ids 收一批 id 一次查完:召回命中补 content_id 走它,逐条查会把
    // 一次召回变成 k 次 SQL。
    m.def("memory_digest",
          [](starling::persistence::Connection& conn, const std::string& tenant) {
              return starling::memoryops::digest(conn, tenant);
          },
          py::arg("connection"), py::arg("tenant"));

    m.def("memory_content_ids",
          [](starling::persistence::Connection& conn, const std::string& tenant,
             const std::vector<std::string>& stmt_ids) {
              return starling::memoryops::content_ids(conn, tenant, stmt_ids);
          },
          py::arg("connection"), py::arg("tenant"), py::arg("statement_ids"));

    // 给 library 留一份自洽的单文件副本(memoryops::backup_to,VACUUM INTO)。VACUUM INTO
    // 随库大小线性变长,所以跑的时候放掉 GIL;释放期间只执行纯 C++,不触碰 Python 对象。
    //
    // 目标已存在时 C++ 抛 std::filesystem::filesystem_error(errc::file_exists)。pybind11
    // 把它当 std::exception 译成 RuntimeError,调用方想要的是 FileExistsError,所以在这里
    // 译:先在释放 GIL 的块里把错误抓成 (errno, 路径),块结束 GIL 回来之后再构造 Python
    // 异常。这就是为什么不用 call_guard:call_guard 在整个 lambda 期间不持 GIL,而构造
    // Python 异常必须持 GIL。
    //
    // 异常按 errno 构造 OSError(errno, strerror, filename):CPython 的 OSError 构造器按
    // errno 选子类(EEXIST → FileExistsError,EACCES → PermissionError ...),并带上
    // .errno 与 .filename。
    // 只译 filesystem_error;完整性检查失败(runtime_error)与 SQLite 层错误
    // (persistence::SqliteError,runtime_error 的子类)照常走默认的 RuntimeError。
    m.def("memory_backup_to",
          [](starling::persistence::SqliteAdapter& adapter, const std::string& dest) {
              struct FsFailure {
                  std::error_code code;
                  std::string path;
              };
              std::optional<FsFailure> failure;
              {
                  py::gil_scoped_release release;
                  try {
                      starling::memoryops::backup_to(adapter, dest);
                  } catch (const std::filesystem::filesystem_error& e) {
                      failure = FsFailure{.code = e.code(), .path = e.path1().string()};
                  }
              }
              if (failure) {
                  const py::object exc = py::reinterpret_borrow<py::object>(PyExc_OSError)(
                      failure->code.value(), failure->code.message(), failure->path);
                  py::set_error(py::type::of(exc), exc);
                  throw py::error_already_set();
              }
          },
          py::arg("adapter"), py::arg("dest"));
}

}  // namespace starling::bindings
