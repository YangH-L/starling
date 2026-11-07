// bind_15_world — 世界注册表（registry.db）、快照、回滚与自动快照。
// 设计：docs/superpowers/specs/2026-11-03-memory-service-design.md
//
// 约定：
//  - WorldRegistry 包 C++ 的 RegistryDb（SQLite，内部互斥，可跨 Python 线程共享）。所有改动都在
//    C++ 里直接落库，Python 侧没有注册表 dict，也没有「改完再保存」这一步。
//  - 设置合并、首次打开的导入编排都在 C++（settings_for_new_world / update_world_settings /
//    RegistryDb::open_or_import），这里只转发。
//  - 凡碰文件系统或 SQLite 的调用都释放 GIL；py::dict 与 JSON 的互转只在持 GIL 时做。
//  - WorldStoreError 带稳定的 code（见 world_store.hpp）。I/O 失败（filesystem_error、runtime_error）
//    没有专门的 code，原样传播。

#include "bind_common.hpp"

#include <pybind11/stl.h>

#include <chrono>
#include <map>
#include <memory>
#include <optional>
#include <thread>
#include <vector>

#include "starling/governance/error_catalog.hpp"
#include "starling/governance/registry_db.hpp"
#include "starling/governance/session_table.hpp"
#include "starling/governance/world_settings.hpp"
#include "starling/governance/world_store.hpp"
#include "starling/persistence/connection.hpp"

namespace starling::bindings {

namespace {

namespace gov = starling::governance;
using namespace pybind11::literals;

py::object to_py(const std::string& text) {
    return py::module_::import("json").attr("loads")(text);
}

std::string from_py(const py::object& obj) {
    return py::module_::import("json").attr("dumps")(obj, py::arg("ensure_ascii") = false)
        .cast<std::string>();
}

py::object text_or_none(const std::string& text) {
    return text.empty() ? py::object(py::none()) : py::object(py::str(text));
}

// 单份快照的 dict（注册表 snapshots / world_take_snapshot / world_take_auto_snapshot 返回的那份）。
// W4 的三个字段总是在，缺省为 None：sessions_complete（int | None）、session_id（str | None）、
// content_mark（str | None，只给内部与测试用，HTTP 白名单不外露）。digest（第 3 期）：快照里的 library
// 摘要，旧条目或缺表的库为 None。
py::dict snap_out(const gov::SnapshotEntry& snap) {
    py::dict out("id"_a = snap.id, "path"_a = snap.path, "kind"_a = snap.kind,
                 "reason"_a = snap.reason, "note"_a = snap.note, "created_at"_a = snap.created_at,
                 "world_time"_a = snap.world_time, "size_bytes"_a = snap.size_bytes);
    out.attr("update")(py::dict(
        "sessions_complete"_a = snap.sessions_complete.has_value()
                                    ? py::object(py::int_(*snap.sessions_complete))
                                    : py::object(py::none()),
        "session_id"_a = text_or_none(snap.session_id),
        "content_mark"_a = text_or_none(snap.content_mark),
        "digest"_a = text_or_none(snap.digest)));
    return out;
}

// WorldRecord 的全部字段；settings 是解析后的 dict（原文 settings_json 不外露），labels 是 dict。
py::dict record_out(const gov::WorldRecord& rec) {
    py::dict labels;
    for (const auto& [key, value] : rec.labels) { labels.attr("setdefault")(py::str(key), py::str(value)); }
    return py::dict("name"_a = rec.name, "dir_id"_a = rec.dir_id, "tenant"_a = rec.tenant,
                    "db_path"_a = rec.db_path, "snapshot_dir"_a = rec.snapshot_dir,
                    "label"_a = rec.label, "created_at"_a = rec.created_at, "seed"_a = rec.seed,
                    "read_only"_a = rec.read_only, "reclaimable"_a = rec.reclaimable,
                    "retention_group"_a = rec.retention_group,
                    "fork_of_world"_a = rec.fork_of_world,
                    "fork_of_snapshot"_a = rec.fork_of_snapshot,
                    "settings"_a = to_py(rec.settings_json), "labels"_a = labels,
                    "holders"_a = rec.holders, "statements"_a = rec.statements,
                    "last_write_at"_a = rec.last_write_at, "stats_at"_a = rec.stats_at);
}

// 一个 provider 此刻的端点身份（裁定 R51）：{"provider": 种类, "base_url": 地址}，缺的键或 None 按空串。
// 只做类型归一；比较在 C++ require_endpoint_unchanged。
gov::ProviderEndpoint provider_endpoint_in(const py::handle& obj) {
    const auto dict = py::reinterpret_borrow<py::dict>(obj);
    const auto field = [&dict](const char* key) {
        const py::object val = dict.attr("get")(key);
        return val.is_none() ? std::string() : val.cast<std::string>();
    };
    return gov::ProviderEndpoint{.provider = field("provider"), .base_url = field("base_url")};
}

// {provider 名: {"provider", "base_url"}} → ProviderEndpoints；None → nullopt（不核对）。
std::optional<gov::ProviderEndpoints> provider_endpoints_in(const py::object& obj) {
    if (obj.is_none()) { return std::nullopt; }
    gov::ProviderEndpoints out;
    for (const auto& [name, endpoint] : obj.cast<py::dict>()) {
        if (!py::isinstance<py::dict>(endpoint)) {
            throw py::type_error("provider_endpoints values must be dicts");
        }
        out.emplace(name.cast<std::string>(), provider_endpoint_in(endpoint));
    }
    return out;
}

// overrides：None → "{}"，否则 json.dumps（持 GIL）。
std::string overrides_in(const py::object& overrides) {
    return overrides.is_none() ? std::string("{}") : from_py(overrides);
}

[[noreturn]] void invalid_auto(const std::string& detail) {
    throw gov::WorldStoreError("invalid_auto_snapshot", detail);
}

// 分叉的 opts dict → ForkOptions（只做类型归一）。键：snapshot_id、labels、retention_group、reclaimable、
// seed、settings（None 或部分设置 dict，json.dumps 成 overrides）、now（调用方的时刻）、provider_endpoints
// （None 或 {provider 名: {"provider", "base_url"}}，同 WorldRegistry.create；核对在 C++）、vector_backend（来源的
// 向量后端，是否支持由 C++ 判）。未知键抛
// invalid_settings；缺的键取 ForkOptions 的默认值。持 GIL 调用。
gov::ForkOptions fork_options_in(const py::dict& opts) {
    gov::ForkOptions out;
    for (const auto& [raw_key, value] : opts) {
        const auto key = raw_key.cast<std::string>();
        if (key == "snapshot_id") {
            out.snapshot_id = value.is_none() ? std::string() : value.cast<std::string>();
        } else if (key == "labels") {
            if (!value.is_none()) { out.labels = value.cast<std::map<std::string, std::string>>(); }
        } else if (key == "retention_group") {
            out.retention_group = value.is_none() ? std::string() : value.cast<std::string>();
        } else if (key == "reclaimable") {
            out.reclaimable = value.cast<bool>();
        } else if (key == "seed") {
            out.seed = value.cast<bool>();
        } else if (key == "settings") {
            out.settings_overrides_json =
                value.is_none() ? std::string() : from_py(py::reinterpret_borrow<py::object>(value));
        } else if (key == "now") {
            out.now_iso = value.cast<std::string>();
        } else if (key == "vector_backend") {
            out.vector_backend = value.cast<std::string>();
        } else if (key == "provider_endpoints") {
            out.provider_endpoints =
                provider_endpoints_in(py::reinterpret_borrow<py::object>(value));
        } else {
            throw gov::WorldStoreError("invalid_settings", "unknown fork option '" + key + "'");
        }
    }
    return out;
}

py::dict fork_out(const gov::ForkResult& result) {
    return py::dict("world"_a = record_out(result.world), "digest"_a = result.digest);
}

// 绑定层只做类型归一：Python 的 int（bool 不算）→ int64，放不下 int64 的按越界报。
// 取值范围（interval_hours 0–8760、limit 1–1000）由 C++ 校验。类型不对与越界同一个码
// invalid_auto_snapshot，路由只映射一个 400。
std::int64_t int64_in(const py::handle& value, const std::string& field) {
    if (!py::isinstance<py::int_>(value) || py::isinstance<py::bool_>(value)) {
        invalid_auto(field + " must be an integer");
    }
    int overflow = 0;
    const auto number = PyLong_AsLongLongAndOverflow(value.ptr(), &overflow);
    if (overflow != 0) { invalid_auto(field + " is out of range"); }
    if (PyErr_Occurred() != nullptr) { throw py::error_already_set(); }
    return static_cast<std::int64_t>(number);
}

// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
gov::AutoSnapshotConfig auto_config_in(const py::handle& on_session, const py::handle& hours) {
    if (!py::isinstance<py::bool_>(on_session)) { invalid_auto("on_session must be a bool"); }
    return gov::AutoSnapshotConfig{.on_session = on_session.cast<bool>(),
                                   .interval_hours = int64_in(hours, "interval_hours"),};
}

}  // namespace

// NOLINTNEXTLINE(readability-identifier-length) — `m` matches all sibling bind_NN units
void bind_15_world(pybind11::module_& m) {
    using namespace pybind11::literals;

    // Python 类型 WorldStoreError（基类 RuntimeError），str(exc) 即 what()，形如 "world_exists: sim-1"。自定义翻译器
    // 只多做一件事：把 C++ 的 replaced()（rollback_world 替换库之后才失败，裁定 R31 M5、R32）放到异常对象的属性
    // replaced 上。Python 侧自己构造的 WorldStoreError（引擎的 world_read_only）没有这个属性，读时按 False。
    PYBIND11_CONSTINIT static py::gil_safe_call_once_and_store<py::object> world_store_error;
    world_store_error.call_once_and_store_result([&m] {
        return py::object(py::exception<gov::WorldStoreError>(m, "WorldStoreError", PyExc_RuntimeError));
    });
    // 签名由 pybind11 定死（ExceptionTranslator = void (*)(std::exception_ptr)），不能改成 const 引用。
    // NOLINTNEXTLINE(performance-unnecessary-value-param)
    py::register_exception_translator([](std::exception_ptr thrown) {
        try {
            if (thrown) { std::rethrow_exception(thrown); }
        } catch (const gov::WorldStoreError& err) {
            const py::object& type = world_store_error.get_stored();
            const py::object instance = type(py::str(err.what()));
            instance.attr("replaced") = py::bool_(err.replaced());
            py::set_error(type, instance);
        }
    });

    // 取异常文本冒号前的部分，即 WorldStoreError 的 code。
    m.def("world_store_error_code", [](const py::object& exc) {
        const auto text = py::str(exc).cast<std::string>();
        const auto pos = text.find(':');
        return pos == std::string::npos ? text : text.substr(0, pos);
    }, py::arg("exc"));

    // 错误码目录（spec §13.2）：[{code, transient, http_status}]，顺序固定。
    m.def("error_catalog", [] {
        py::list out;
        for (const auto& info : gov::error_catalog()) {
            py::dict item("code"_a = std::string(info.code), "transient"_a = info.transient,
                          "http_status"_a = info.http_status);
            out.append(std::move(item));
        }
        return out;
    });

    m.def("world_is_valid_name", &gov::is_valid_world_name, py::arg("name"));

    // ---- 注册表 ----
    py::class_<gov::RegistryDb>(m, "WorldRegistry")
        // 首次打开的编排（worlds.json 一次性导入 → open → validate_registry）在 C++ open_or_import。
        .def_static("open",
                    // NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
                    [](const std::string& data_dir, const std::string& default_db_path,
                       const std::string& default_tenant) {
                        const py::gil_scoped_release release;
                        return std::make_unique<gov::RegistryDb>(gov::RegistryDb::open_or_import(
                            data_dir, default_db_path, default_tenant));
                    }, py::arg("data_dir"), py::arg("default_db_path"), py::arg("default_tenant"))
        .def("default_world", [](const gov::RegistryDb& reg) {
            const py::gil_scoped_release release;
            return reg.default_world();
        })
        .def("get", [](const gov::RegistryDb& reg, const std::string& name) {
            gov::WorldRecord rec;
            {
                const py::gil_scoped_release release;
                rec = reg.get(name);
            }
            return record_out(rec);
        }, py::arg("name"))
        .def("list",
             [](const gov::RegistryDb& reg,
                const std::optional<std::map<std::string, std::string>>& labels,
                std::int64_t offset, std::int64_t limit) {
                 gov::WorldQuery query;
                 if (labels) { query.labels = *labels; }
                 query.offset = offset;
                 query.limit = limit;
                 gov::WorldPage page;
                 {
                     const py::gil_scoped_release release;
                     page = reg.list(query);
                 }
                 py::list worlds;
                 for (const auto& rec : page.worlds) { worlds.append(record_out(rec)); }
                 return py::dict("worlds"_a = worlds, "total"_a = page.total);
             }, py::arg("labels") = py::none(), py::arg("offset") = 0, py::arg("limit") = 50)
        // 新世界设置 = 默认世界当前设置 + overrides 的值拷贝（C++ create_world，一次持锁）。
        // provider_endpoints（裁定 R40、R51）：{provider 名: {"provider": 种类, "base_url": 地址}}。给出时 C++
        // 在同一次持锁内合并、核对继承来的 endpoint、再写；endpoint_changed 时什么都不写。None：不核对。
        // seed=True（spec §6.2）：建空种子，蕴含只读、后台强制关（C++ create_world 的 WorldFlags）。
        .def("create",
             // NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
             [](gov::RegistryDb& reg, const std::string& name, const std::string& label,
                const std::map<std::string, std::string>& labels, const py::object& overrides,
                const std::string& now_iso, const py::object& provider_endpoints, bool seed) {
                 const auto overrides_text = overrides_in(overrides);
                 const auto endpoints = provider_endpoints_in(provider_endpoints);
                 gov::WorldRecord rec;
                 {
                     const py::gil_scoped_release release;
                     rec = reg.create_world(name, label, labels, overrides_text, now_iso, endpoints,
                                            gov::WorldFlags{.seed = seed});
                 }
                 return record_out(rec);
             }, py::arg("name"), py::arg("label"), py::arg("labels"), py::arg("overrides"),
             py::arg("now_iso"), py::arg("provider_endpoints") = py::none(), py::kw_only(),
             py::arg("seed") = false)
        // 递归删世界目录可能很大，释放 GIL。
        .def("remove", [](gov::RegistryDb& reg, const std::string& name) {
            const py::gil_scoped_release release;
            reg.remove(name);
        }, py::arg("name"))
        .def("update_settings",
             [](gov::RegistryDb& reg, const std::string& name, const py::object& overrides,
                const py::object& provider_endpoints) {
                 const auto overrides_text = overrides_in(overrides);
                 const auto endpoints = provider_endpoints_in(provider_endpoints);
                 std::string merged;
                 {
                     const py::gil_scoped_release release;
                     merged = reg.update_world_settings(name, overrides_text, endpoints);
                 }
                 return to_py(merged);
             }, py::arg("name"), py::arg("overrides"), py::arg("provider_endpoints") = py::none())
        // 重新确认端点（裁定 R53）：current 是 {provider 名: endpoint dict（不含密钥，config.provider_endpoint_settings）}，
        // 每个 dict 经 C++ settings_from_json 的同一套字段规则解析（带 api_key 等未知键 → invalid_settings）。
        // 替换规则全在 C++ RegistryDb::reconfirm_world_endpoints。返回新的设置 dict。
        .def("reconfirm_endpoints",
             [](gov::RegistryDb& reg, const std::string& name, const py::dict& current) {
                 std::map<std::string, gov::EndpointSettings> endpoints;
                 for (const auto& [provider, endpoint] : current) {
                     const auto parsed = gov::settings_from_json(
                         from_py(py::dict("extraction"_a = endpoint)));
                     if (!parsed.extraction) {
                         throw gov::WorldStoreError("invalid_settings",
                                                    "provider endpoint must be an object");
                     }
                     endpoints.emplace(provider.cast<std::string>(), *parsed.extraction);
                 }
                 std::string text;
                 {
                     const py::gil_scoped_release release;
                     text = reg.reconfirm_world_endpoints(name, endpoints);
                 }
                 return to_py(text);
             }, py::arg("name"), py::arg("current"))
        // 权威来源同步（裁定 R27）：出现的段整段替换、background 逐叶合并、null 清除。返回新的设置 dict。
        .def("replace_settings_sections",
             [](gov::RegistryDb& reg, const std::string& name, const py::object& sections) {
                 const auto sections_text = overrides_in(sections);
                 std::string replaced;
                 {
                     const py::gil_scoped_release release;
                     replaced = reg.replace_world_sections(name, sections_text);
                 }
                 return to_py(replaced);
             }, py::arg("name"), py::arg("sections"))
        .def("update_stats",
             // NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
             [](gov::RegistryDb& reg, const std::string& name, std::int64_t holders,
                std::int64_t statements, const std::string& last_write_at,
                const std::string& now_iso) {
                 const py::gil_scoped_release release;
                 reg.update_stats(name, holders, statements, last_write_at, now_iso);
             }, py::arg("name"), py::arg("holders"), py::arg("statements"),
             py::arg("last_write_at"), py::arg("now_iso"))
        .def("snapshots", [](const gov::RegistryDb& reg, const std::string& world) {
            std::vector<gov::SnapshotEntry> entries;
            {
                const py::gil_scoped_release release;
                entries = reg.snapshots(world);
            }
            py::list out;
            for (const auto& entry : entries) { out.append(snap_out(entry)); }
            return out;
        }, py::arg("world"))
        // 每个世界保留的快照份数（meta.snapshot_retention，默认 5）。
        .def("snapshot_retention", [](const gov::RegistryDb& reg) {
            const py::gil_scoped_release release;
            return reg.snapshot_retention();
        })
        .def_property_readonly("data_dir", [](const gov::RegistryDb& reg) {
            return reg.data_dir().string();
        });

    // 一次性导入旧 worlds.json（无 worlds.json 或 registry.db 已在时什么都不做）。
    m.def("world_import_json", [](const std::string& data_dir) {
        const py::gil_scoped_release release;
        gov::import_worlds_json(data_dir);
    }, py::arg("data_dir"));

    // world_endpoint 是 settings 里 extraction / embedding / chat / consolidation 之一的 dict；解析走 C++
    // settings_from_json（字段规则同一份）。current 是该 provider 此刻的 {"provider", "base_url"}，provider
    // 已被删掉时 None（裁定 R51）。provider_name 为空永不抛；否则 provider 不存在、或种类与 base_url 任一不同
    // 抛 endpoint_changed（消息只点名 provider 与 role，裁定 R54）。
    m.def("world_require_endpoint_unchanged",
          [](const py::dict& world_endpoint, const py::object& current, const std::string& role) {
              const auto parsed = gov::settings_from_json(
                  from_py(py::dict("extraction"_a = world_endpoint)));
              const std::optional<gov::ProviderEndpoint> now_at =
                  current.is_none() ? std::nullopt
                                    : std::optional<gov::ProviderEndpoint>(
                                          provider_endpoint_in(current.cast<py::dict>()));
              gov::require_endpoint_unchanged(*parsed.extraction, now_at, role);
          }, py::arg("world_endpoint"), py::arg("current"), py::arg("role"));

    // 嵌入 endpoint 从 before 换成 after（各为设置里 embedding 段的 dict，None = 没绑定）时要不要重嵌入（裁定 R58）。
    // 解析走 settings_from_json（字段规则同一份），规则在 C++ embedding_needs_reembed。
    m.def("world_embedding_needs_reembed",
          [](const py::object& before, const py::object& after) {
              const auto parse = [](const py::object& endpoint)
                  -> std::optional<gov::EndpointSettings> {
                  if (endpoint.is_none()) { return std::nullopt; }
                  return gov::settings_from_json(
                             from_py(py::dict("embedding"_a = endpoint.cast<py::dict>())))
                      .embedding;
              };
              return gov::embedding_needs_reembed(parse(before), parse(after));
          }, py::arg("before"), py::arg("after"));

    // 一份（可能不完整的）设置 dict → C++ 补齐默认值后的完整设置 dict（settings_from_json →
    // settings_to_json）。默认值只在 C++：引擎池建引擎前用它得到实际生效的设置。非法输入同 settings_from_json。
    // record（注册表记录 dict，只读 seed、read_only、reclaimable 三个键）给出时按看板的打开方式算（C++
    // effective_world_settings，裁定 R33）：只读打开的世界后台全关。
    m.def("world_effective_settings", [](const py::dict& settings, const py::object& record) {
        gov::WorldRecord rec;
        rec.settings_json = from_py(settings);
        const bool by_open_mode = !record.is_none();
        if (by_open_mode) {
            const auto flags = py::reinterpret_borrow<py::dict>(record);
            const auto flag = [&flags](const char* key) {
                return flags.attr("get")(key, false).cast<bool>();
            };
            rec.seed = flag("seed");
            rec.read_only = flag("read_only");
            rec.reclaimable = flag("reclaimable");
        }
        std::string normalized;
        {
            const py::gil_scoped_release release;
            normalized = gov::settings_to_json(by_open_mode
                                                   ? gov::effective_world_settings(rec)
                                                   : gov::settings_from_json(rec.settings_json));
        }
        return to_py(normalized);
    }, py::arg("settings"), py::kw_only(), py::arg("record") = py::none());

    m.def("world_ingest_partition", &gov::ingest_partition, py::arg("is_default"),
          py::arg("tenant"), py::arg("dir_id"));

    // 只读打开，busy_timeout 最长 5 秒，所以释放 GIL。键名见 WorldStats（holders / last_write_at 是
    // 引擎池写统计缓存用的两项）。库文件存在但读不出时抛 snapshot_failed；只有文件不存在才是全 0。
    // frozen 默认 False（Shared）；已登记世界传 world_read_only_frozen(record)（裁定 R36）。
    m.def("world_stats",
          [](const std::string& db_path, const std::string& tenant, bool frozen) {
              gov::WorldStats stats;
              {
                  const py::gil_scoped_release release;
                  stats = gov::world_stats(db_path, tenant,
                                           frozen ? starling::persistence::ReadOnlyFreeze::Frozen
                                                  : starling::persistence::ReadOnlyFreeze::Shared);
              }
              return py::dict("db_bytes"_a = stats.db_bytes, "live_bytes"_a = stats.live_bytes,
                              "statements"_a = stats.statements, "engrams"_a = stats.engrams,
                              "world_time"_a = stats.world_time, "holders"_a = stats.holders,
                              "last_write_at"_a = stats.last_write_at);
          }, py::arg("db_path"), py::arg("tenant"), py::kw_only(), py::arg("frozen") = false);

    // 租户不变式（spec §2.1）：只读扫一遍库，有别的租户的行抛 world_tenant_mismatch。
    // 2.6GB 的生产库约 90ms，释放 GIL。库文件不存在时直接通过。
    m.def("world_verify_tenant",
          // NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
          [](const std::string& db_path, const std::string& tenant, bool frozen) {
              const py::gil_scoped_release release;
              gov::verify_world_tenant(db_path, tenant,
                                       frozen ? starling::persistence::ReadOnlyFreeze::Frozen
                                              : starling::persistence::ReadOnlyFreeze::Shared);
          }, py::arg("db_path"), py::arg("tenant"), py::kw_only(), py::arg("frozen") = false);

    // 快照会 VACUUM 整库，可能要几秒，期间释放 GIL。
    // take_snapshot 先清扫上次崩溃的残留、写快照文件、登记条目（失败时 C++ 自己删掉新文件），最后才
    // 执行保留策略；保留策略失败由 C++ 吞掉：成功的快照一定返回条目。抛异常只表示快照本身没拍成
    // （磁盘不足、库损坏、kind 非法、vector_backend 不是 sqlite 等），此时磁盘上没有新文件。
    // vector_backend 由调用方按当前配置传入；拒绝与否由 C++ 判（snapshot_unsupported_backend）。
    // 条目已写进 registry.db，Python 侧没有保存或补偿步骤。
    m.def("world_take_snapshot",
          // NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
          [](gov::RegistryDb& reg, const std::string& world, const std::string& kind,
             const std::string& reason, const std::string& note,
             const std::string& vector_backend, int test_before_vacuum_sleep_ms) {
              gov::SnapshotOptions opts;
              opts.kind = kind;
              opts.reason = reason;
              opts.note = note;
              opts.vector_backend = vector_backend;
              if (test_before_vacuum_sleep_ms > 0) {
                  // 仅测试用：在 VACUUM 之前睡一会儿（GIL 已释放），造出确定的阻塞窗口。
                  opts.before_vacuum = [test_before_vacuum_sleep_ms] {
                      std::this_thread::sleep_for(
                          std::chrono::milliseconds(test_before_vacuum_sleep_ms));
                  };
              }
              gov::SnapshotEntry snap;
              {
                  const py::gil_scoped_release release;
                  snap = gov::take_snapshot(reg, world, opts);
              }
              return py::dict("snapshot"_a = snap_out(snap));
          }, py::arg("registry"), py::arg("world"), py::arg("kind") = "manual",
          py::arg("reason") = "manual", py::arg("note") = "",
          py::kw_only(), py::arg("vector_backend") = "sqlite",
          py::arg("_test_before_vacuum_sleep_ms") = 0);

    // 删一份快照（注册项 + 文件）。快照不存在抛 snapshot_not_found。
    m.def("world_delete_snapshot",
          [](gov::RegistryDb& reg, const std::string& world, const std::string& snapshot_id) {
              const py::gil_scoped_release release;
              gov::delete_snapshot(reg, world, snapshot_id);
          }, py::arg("registry"), py::arg("world"), py::arg("snapshot_id"));

    // ---- 自动快照与会话清单（W4） ----
    // 参数先在持 GIL 时转换好，C++ 调用释放 GIL。

    // 改一个世界的自动快照配置（写 registry.db）。world_not_found；类型不对或 interval_hours 不在
    // 0–8760 抛 invalid_auto_snapshot。
    m.def("world_set_auto_snapshot",
          // NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
          [](gov::RegistryDb& reg, const std::string& world, const py::object& on_session,
             const py::object& interval_hours) {
              const auto config = auto_config_in(on_session, interval_hours);
              const py::gil_scoped_release release;
              gov::set_auto_snapshot(reg, world, config);
          }, py::arg("registry"), py::arg("world"), py::arg("on_session"),
          py::arg("interval_hours"));

    // 按触发判断要不要拍一份自动快照（规则全在 C++ take_auto_snapshot）。trigger 是 "session" |
    // "interval"；now_iso 必须是 YYYY-MM-DDTHH:MM:SSZ；session_id 只给 "session" 触发。
    // 返回 {"snapshot", "skipped", "refused"}：拍了时 snapshot 是条目、skipped 与 refused 为 None；
    // 没拍时 snapshot 为 None、skipped 是原因（disabled | known_session | unchanged | not_due | empty |
    // unsupported_backend）。修剪卡住而拒绝拍时 refused 是原因，调用方计为失败（码 snapshot_failed）。
    // 注册表的改动（登记、部分修剪）C++ 已落库。
    m.def("world_take_auto_snapshot",
          // NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
          [](gov::RegistryDb& reg, const std::string& world, const std::string& trigger,
             const std::string& now_iso, const std::string& session_id,
             const std::string& vector_backend) {
              const gov::AutoTrigger auto_trigger{
                  .kind = trigger, .session_id = session_id, .now_iso = now_iso,};
              gov::SnapshotOptions base;
              base.vector_backend = vector_backend;
              gov::AutoSnapshotResult result;
              {
                  const py::gil_scoped_release release;
                  result = gov::take_auto_snapshot(reg, world, auto_trigger, base);
              }
              py::object snapshot = py::none();
              if (result.taken.has_value()) { snapshot = snap_out(*result.taken); }
              return py::dict("snapshot"_a = snapshot, "skipped"_a = text_or_none(result.skipped),
                              "refused"_a = text_or_none(result.refused));
          }, py::arg("registry"), py::arg("world"), py::arg("trigger"), py::arg("now_iso"),
          py::arg("session_id") = "", py::kw_only(), py::arg("vector_backend") = "sqlite");

    // 会话清单（设计稿 §7），只读打开该世界的库。返回 {"total", "null_observed", "truncated",
    // "sessions": [{session_id, units, speakers, first_at, last_at, anchor}]}；first_at / last_at
    // 没有时间时为 None，anchor 为 {"snapshot_id", "created_at"} 或 None。limit 不是整数或不在
    // 1–1000 抛 invalid_auto_snapshot，世界不存在抛 world_not_found，库读不出抛 snapshot_failed。
    m.def("world_list_sessions",
          [](const gov::RegistryDb& reg, const std::string& world, const py::object& limit) {
              const auto max_rows = int64_in(limit, "limit");
              gov::SessionList listed;
              {
                  const py::gil_scoped_release release;
                  listed = gov::list_sessions(reg, world, max_rows);
              }
              py::list sessions;
              for (const auto& row : listed.rows) {
                  py::object anchor = py::none();
                  if (!row.anchor_snapshot_id.empty()) {
                      anchor = py::dict("snapshot_id"_a = row.anchor_snapshot_id,
                                        "created_at"_a = row.anchor_created_at);
                  }
                  sessions.append(py::dict("session_id"_a = row.session_id, "units"_a = row.units,
                                           "speakers"_a = row.speakers,
                                           "first_at"_a = text_or_none(row.first_at),
                                           "last_at"_a = text_or_none(row.last_at),
                                           "anchor"_a = anchor));
              }
              return py::dict("total"_a = listed.total, "null_observed"_a = listed.null_observed,
                              "truncated"_a = listed.truncated, "sessions"_a = sessions);
          }, py::arg("registry"), py::arg("world"), py::arg("limit") = 200);

    // ---- 回滚与归档（W3） ----
    // 回滚不改注册表。世界 home 与归档目录由 C++ 从 RegistryDb 推出（不再传 registry_dir）。
    // C++ 的连接探测（world_in_use）只是纵深防御；进程内的安全由调用方保证。复制快照 +
    // integrity_check 可能要几秒，释放 GIL。C++ 的测试钩子 free_space、before_replace 不暴露；after_replace_for_test
    // 经 world_rollback 的 _test_fail_after_replace 暴露（只能注入一次固定的失败）。
    // 回滚预检：world_rollback 第 1–4 步的只读检查（同顺序、同错误码），什么都不动。
    m.def("world_check_rollback",
          // NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
          [](const gov::RegistryDb& reg, const std::string& world, const std::string& snapshot_id,
             const std::string& vector_backend) {
              gov::RollbackOptions opts;
              opts.vector_backend = vector_backend;
              const py::gil_scoped_release release;
              gov::check_rollback(reg, world, snapshot_id, opts);
          }, py::arg("registry"), py::arg("world"), py::arg("snapshot_id"), py::kw_only(),
          py::arg("vector_backend") = "sqlite");

    // 抛出的 WorldStoreError 带属性 replaced（bool）：True = 库已经换成快照的内容之后才失败（restore_digest_mismatch
    // 恒为 True），调用方按「库已替换」处理；码不变。_test_fail_after_replace（仅测试用）：rename 之后、算摘要之前
    // 抛 snapshot_failed，模拟「替换之后才失败」。
    m.def("world_rollback",
          // NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
          [](const gov::RegistryDb& reg, const std::string& world, const std::string& snapshot_id,
             const std::string& vector_backend, bool test_fail_after_replace) {
              gov::RollbackOptions opts;
              opts.vector_backend = vector_backend;
              if (test_fail_after_replace) {
                  opts.after_replace_for_test = [] {
                      throw std::runtime_error("injected failure after the replace (test only)");
                  };
              }
              gov::RollbackResult result;
              {
                  const py::gil_scoped_release release;
                  result = gov::rollback_world(reg, world, snapshot_id, opts);
              }
              return py::dict("archive_path"_a = result.archive_path,
                              "restored_bytes"_a = result.restored_bytes,
                              "digest"_a = text_or_none(result.digest));
          }, py::arg("registry"), py::arg("world"), py::arg("snapshot_id"), py::kw_only(),
          py::arg("vector_backend") = "sqlite", py::arg("_test_fail_after_replace") = false);

    m.def("world_list_archives", [](const gov::RegistryDb& reg, const std::string& world) {
        std::vector<gov::ArchiveEntry> listed;
        {
            const py::gil_scoped_release release;
            listed = gov::list_rollback_archives(reg, world);
        }
        py::list out;
        for (const auto& item : listed) {
            out.append(py::dict("name"_a = item.name, "size_bytes"_a = item.size_bytes));
        }
        return out;
    }, py::arg("registry"), py::arg("world"));

    // 归档是整份旧库，删除可能较慢，释放 GIL。返回删掉的文件数。
    m.def("world_purge_archives", [](const gov::RegistryDb& reg, const std::string& world) {
        const py::gil_scoped_release release;
        return gov::purge_rollback_archives(reg, world);
    }, py::arg("registry"), py::arg("world"));

    // 打开方式（spec §6.4）：record 是注册表记录 dict（WorldRegistry.get / list 的一行），只读 seed、read_only、
    // reclaimable 三个键（缺的键按 False）。规则在 C++ decide_open_mode。返回 "rw" | "ro"。
    m.def("world_decide_open_mode",
          [](const py::dict& record, bool for_client_write) {
              const auto flag = [&record](const char* key) {
                  const py::object val = record.attr("get")(key, false);
                  return val.cast<bool>();
              };
              gov::WorldRecord rec;
              rec.seed = flag("seed");
              rec.read_only = flag("read_only");
              rec.reclaimable = flag("reclaimable");
              return gov::decide_open_mode(rec, for_client_write) == gov::OpenMode::ReadOnly ? "ro"
                                                                                           : "rw";
          }, py::arg("record"), py::arg("for_client_write") = false);

    // 已登记世界的只读连接能不能冻结（裁定 R36、R41、R49 (c)，C++ read_only_freeze_for）：record 同
    // world_decide_open_mode。True 只在只读打开的世界（只读、种子、可回收）上、且 held_for_write 为 False 时；
    // held_for_write 传 SessionTable.world_write_held(world, now)（客户端写入期间一律 False）。Python 把结果传给
    // read_only_uri / open_read_only / world_stats 等的 frozen，自己不判。
    // 不释放 GIL（审查 M3）：判定是亚微秒级的纯函数（三个 bool），没有 I/O、不加锁、不会阻塞；spec §12 要求释放的是
    // 会阻塞的调用。读 record 的三个键本身要持 GIL，释放再取回只会更慢。
    m.def("world_read_only_frozen",
          [](const py::dict& record, bool held_for_write) {
              const auto flag = [&record](const char* key) {
                  return record.attr("get")(key, false).cast<bool>();
              };
              gov::WorldRecord rec;
              rec.seed = flag("seed");
              rec.read_only = flag("read_only");
              rec.reclaimable = flag("reclaimable");
              return gov::read_only_freeze_for(rec, held_for_write) ==
                     starling::persistence::ReadOnlyFreeze::Frozen;
          }, py::arg("record"), py::arg("held_for_write") = false);

    // ---- 分叉、种子与升级种子（spec §6.1、§6.2） ----
    // 规则、校验与错误码全在 C++（world_store.hpp fork_world / upgrade_seed）。opts 见 fork_options_in。
    // 复制、算摘要可能要几百毫秒，释放 GIL。返回 {"world": <记录 dict>, "digest": str}。
    // 关着的世界或快照为来源。前提（裁定 R5）：调用方持着来源世界（WorldPool 的世界锁），分叉期间没有
    // 写连接打开它。
    m.def("world_fork",
          // NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
          [](gov::RegistryDb& reg, const std::string& source, const std::string& new_name,
             const py::dict& opts, int test_after_copy_sleep_ms) {
              auto options = fork_options_in(opts);
              if (test_after_copy_sleep_ms > 0) {
                  // 仅测试用：复制完之后睡一会儿（GIL 已释放），造出确定的阻塞窗口。
                  options.after_copy_for_test = [test_after_copy_sleep_ms](const std::filesystem::path&) {
                      std::this_thread::sleep_for(std::chrono::milliseconds(test_after_copy_sleep_ms));
                  };
              }
              gov::ForkResult result;
              {
                  const py::gil_scoped_release release;
                  result = gov::fork_world(reg, source, new_name, options);
              }
              return fork_out(result);
          }, py::arg("registry"), py::arg("source"), py::arg("new_name"), py::arg("opts"),
          py::kw_only(), py::arg("_test_after_copy_sleep_ms") = 0);

    // 开着的世界为来源：从它的引擎连接复制（读到最近一次提交）。opts 里不能有 snapshot_id（快照为来源用
    // world_fork），否则 ValueError。复制途中来源有写入抛 world_busy（瞬态，重试）。
    m.def("world_fork_from_connection",
          // NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
          [](gov::RegistryDb& reg, starling::persistence::Connection& conn,
             const std::string& source, const std::string& new_name, const py::dict& opts) {
              const auto options = fork_options_in(opts);
              gov::ForkResult result;
              {
                  const py::gil_scoped_release release;
                  result = gov::fork_world_from_connection(reg, conn, source, new_name, options);
              }
              return fork_out(result);
          }, py::arg("registry"), py::arg("connection"), py::arg("source"), py::arg("new_name"),
          py::arg("opts"));

    // 复制旧种子 → 副本上跑迁移 → 登记为新种子；旧种子原样保留。来源不是种子抛 world_not_seed。
    m.def("world_upgrade_seed",
          // NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
          [](gov::RegistryDb& reg, const std::string& source, const std::string& new_name,
             const std::string& now_iso) {
              gov::ForkResult result;
              {
                  const py::gil_scoped_release release;
                  result = gov::upgrade_seed(reg, source, new_name, now_iso);
              }
              return fork_out(result);
          }, py::arg("registry"), py::arg("source"), py::arg("new_name"), py::arg("now"));

    // ---- 会话、句柄、写占用、空闲判定与写入准入（spec §11，Task 4.2；本期新增的都在这个区块，裁定 R44） ----
    // 状态机与规则全在 C++（session_table.hpp、registry_db.hpp require_writable），这里只转发。now_ms 由调用方传
    // （单调时钟的毫秒），C++ 不读墙钟。SessionTable 的每个方法释放 GIL（spec §12）。错误：world_held /
    // session_expired / handle_closed / world_read_only 抛 WorldStoreError（带 code）；参数错误抛 ValueError；
    // 没有配对 call_enter 的 call_exit 抛 RuntimeError（std::logic_error）。
    py::class_<gov::HandleInfo>(m, "HandleInfo")
        .def_readonly("session_id", &gov::HandleInfo::session_id)
        .def_readonly("world", &gov::HandleInfo::world)
        .def_readonly("holder", &gov::HandleInfo::holder)
        .def_readonly("read_only", &gov::HandleInfo::read_only);

    using GilFree = py::call_guard<py::gil_scoped_release>;
    const gov::SessionConfig session_defaults;
    py::class_<gov::SessionTable>(m, "SessionTable")
        // 构造只建空表，不做 I/O；pybind11 的工厂构造要在持 GIL 时登记实例，所以这里不释放。
        .def(py::init([](std::int64_t lease_ms, std::int64_t idle_close_ms) {
                 return std::make_unique<gov::SessionTable>(
                     gov::SessionConfig{.lease_ms = lease_ms, .idle_close_ms = idle_close_ms});
             }),
             py::kw_only(), py::arg("lease_ms") = session_defaults.lease_ms,
             py::arg("idle_close_ms") = session_defaults.idle_close_ms)
        .def("connect", &gov::SessionTable::connect, py::arg("now_ms"), GilFree())
        .def("heartbeat", &gov::SessionTable::heartbeat, py::arg("session_id"), py::arg("now_ms"),
             GilFree())
        .def("disconnect", &gov::SessionTable::disconnect, py::arg("session_id"), GilFree())
        .def("open_handle", &gov::SessionTable::open_handle, py::arg("session_id"),
             py::arg("world"), py::arg("holder"), py::arg("read_only"), py::arg("now_ms"),
             GilFree())
        .def("close_handle", &gov::SessionTable::close_handle, py::arg("handle_id"), GilFree())
        .def("resolve", &gov::SessionTable::resolve, py::arg("handle_id"), py::arg("now_ms"),
             GilFree())
        .def("call_enter", &gov::SessionTable::call_enter, py::arg("handle_id"), py::arg("now_ms"),
             GilFree())
        .def("call_exit", &gov::SessionTable::call_exit, py::arg("handle_id"), py::arg("now_ms"),
             GilFree())
        .def("in_flight", &gov::SessionTable::in_flight, py::arg("handle_id"), GilFree())
        .def("expire", &gov::SessionTable::expire, py::arg("now_ms"), GilFree())
        .def("touch_world", &gov::SessionTable::touch_world, py::arg("world"), py::arg("now_ms"),
             GilFree())
        .def("world_has_live_handles", &gov::SessionTable::world_has_live_handles,
             py::arg("world"), py::arg("now_ms"), GilFree())
        .def("world_write_held", &gov::SessionTable::world_write_held, py::arg("world"),
             py::arg("now_ms"), GilFree())
        .def("world_write_held_by_other", &gov::SessionTable::world_write_held_by_other,
             py::arg("world"), py::arg("session_id"), py::arg("now_ms"), GilFree())
        .def("may_close_world", &gov::SessionTable::may_close_world, py::arg("world"),
             py::arg("is_default"), py::arg("now_ms"), GilFree())
        // 裁定 R54：删世界之后调；还有活句柄或在途调用时抛 WorldStoreError（world_in_use）。
        .def("forget_world", &gov::SessionTable::forget_world, py::arg("world"), GilFree());

    // 写入准入（裁定 R51，C++ require_writable）：record 同 world_decide_open_mode（另读 name，只用于错误文本）。
    // held_by_other 传 SessionTable.world_write_held_by_other(world, session_id, now)（看板传 ""）。
    // 通过返回 None；不通过抛 WorldStoreError（world_held 优先于 world_read_only）。
    m.def("world_require_writable",
          [](const py::dict& record, bool for_client_write, bool held_by_other) {
              const auto flag = [&record](const char* key) {
                  return record.attr("get")(key, false).cast<bool>();
              };
              gov::WorldRecord rec;
              rec.name = record.attr("get")("name", "").cast<std::string>();
              rec.seed = flag("seed");
              rec.read_only = flag("read_only");
              rec.reclaimable = flag("reclaimable");
              const py::gil_scoped_release release;
              gov::require_writable(rec, for_client_write, held_by_other);
          }, py::arg("record"), py::arg("for_client_write") = false,
          py::arg("held_by_other") = false);
}

}  // namespace starling::bindings
