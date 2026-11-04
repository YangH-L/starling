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

#include "starling/governance/registry_db.hpp"
#include "starling/governance/world_settings.hpp"
#include "starling/governance/world_store.hpp"

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
// content_mark（str | None，只给内部与测试用，HTTP 白名单不外露）。
py::dict snap_out(const gov::SnapshotEntry& snap) {
    py::dict out("id"_a = snap.id, "path"_a = snap.path, "kind"_a = snap.kind,
                 "reason"_a = snap.reason, "note"_a = snap.note, "created_at"_a = snap.created_at,
                 "world_time"_a = snap.world_time, "size_bytes"_a = snap.size_bytes);
    out.attr("update")(py::dict(
        "sessions_complete"_a = snap.sessions_complete.has_value()
                                    ? py::object(py::int_(*snap.sessions_complete))
                                    : py::object(py::none()),
        "session_id"_a = text_or_none(snap.session_id),
        "content_mark"_a = text_or_none(snap.content_mark)));
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

// overrides：None → "{}"，否则 json.dumps（持 GIL）。
std::string overrides_in(const py::object& overrides) {
    return overrides.is_none() ? std::string("{}") : from_py(overrides);
}

[[noreturn]] void invalid_auto(const std::string& detail) {
    throw gov::WorldStoreError("invalid_auto_snapshot", detail);
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

    // 与 bind_07/bind_14 同一写法；what() 形如 "world_exists: sim-1"。
    py::register_exception<gov::WorldStoreError>(m, "WorldStoreError", PyExc_RuntimeError);

    // 取异常文本冒号前的部分，即 WorldStoreError 的 code。
    m.def("world_store_error_code", [](const py::object& exc) {
        const auto text = py::str(exc).cast<std::string>();
        const auto pos = text.find(':');
        return pos == std::string::npos ? text : text.substr(0, pos);
    }, py::arg("exc"));

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
        .def("create",
             // NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
             [](gov::RegistryDb& reg, const std::string& name, const std::string& label,
                const std::map<std::string, std::string>& labels, const py::object& overrides,
                const std::string& now_iso) {
                 const auto overrides_text = overrides_in(overrides);
                 gov::WorldRecord rec;
                 {
                     const py::gil_scoped_release release;
                     rec = reg.create_world(name, label, labels, overrides_text, now_iso);
                 }
                 return record_out(rec);
             }, py::arg("name"), py::arg("label"), py::arg("labels"), py::arg("overrides"),
             py::arg("now_iso"))
        // 递归删世界目录可能很大，释放 GIL。
        .def("remove", [](gov::RegistryDb& reg, const std::string& name) {
            const py::gil_scoped_release release;
            reg.remove(name);
        }, py::arg("name"))
        .def("update_settings",
             [](gov::RegistryDb& reg, const std::string& name, const py::object& overrides) {
                 const auto overrides_text = overrides_in(overrides);
                 std::string merged;
                 {
                     const py::gil_scoped_release release;
                     merged = reg.update_world_settings(name, overrides_text);
                 }
                 return to_py(merged);
             }, py::arg("name"), py::arg("overrides"))
        // 改设置 / 建世界的预演（裁定 R38）：同一套 C++ 合并，返回合并结果的 dict，什么都不写。
        .def("preview_settings",
             [](const gov::RegistryDb& reg, const std::string& name, const py::object& overrides) {
                 const auto overrides_text = overrides_in(overrides);
                 std::string merged;
                 {
                     const py::gil_scoped_release release;
                     merged = reg.preview_world_settings(name, overrides_text);
                 }
                 return to_py(merged);
             }, py::arg("name"), py::arg("overrides"))
        .def("preview_new_world_settings",
             [](const gov::RegistryDb& reg, const py::object& overrides) {
                 const auto overrides_text = overrides_in(overrides);
                 std::string merged;
                 {
                     const py::gil_scoped_release release;
                     merged = reg.preview_new_world_settings(overrides_text);
                 }
                 return to_py(merged);
             }, py::arg("overrides"))
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
    // settings_from_json（字段规则同一份）。provider_name 为空永不抛，否则 base_url 不同抛 endpoint_changed。
    m.def("world_require_endpoint_unchanged",
          [](const py::dict& world_endpoint, const std::string& current_base_url) {
              const auto parsed = gov::settings_from_json(
                  from_py(py::dict("extraction"_a = world_endpoint)));
              gov::require_endpoint_unchanged(*parsed.extraction, current_base_url);
          }, py::arg("world_endpoint"), py::arg("current_base_url"));

    // 一份（可能不完整的）设置 dict → C++ 补齐默认值后的完整设置 dict（settings_from_json →
    // settings_to_json）。默认值只在 C++：引擎池建引擎前用它得到实际生效的设置。非法输入同 settings_from_json。
    m.def("world_effective_settings", [](const py::dict& settings) {
        const auto text = from_py(settings);
        std::string normalized;
        {
            const py::gil_scoped_release release;
            normalized = gov::settings_to_json(gov::settings_from_json(text));
        }
        return to_py(normalized);
    }, py::arg("settings"));

    m.def("world_ingest_partition", &gov::ingest_partition, py::arg("is_default"),
          py::arg("tenant"), py::arg("dir_id"));

    // 只读打开，busy_timeout 最长 5 秒，所以释放 GIL。键名见 WorldStats（holders / last_write_at 是
    // 引擎池写统计缓存用的两项）。库文件存在但读不出时抛 snapshot_failed；只有文件不存在才是全 0。
    m.def("world_stats",
          [](const std::string& db_path, const std::string& tenant) {
              gov::WorldStats stats;
              {
                  const py::gil_scoped_release release;
                  stats = gov::world_stats(db_path, tenant);
              }
              return py::dict("db_bytes"_a = stats.db_bytes, "live_bytes"_a = stats.live_bytes,
                              "statements"_a = stats.statements, "engrams"_a = stats.engrams,
                              "world_time"_a = stats.world_time, "holders"_a = stats.holders,
                              "last_write_at"_a = stats.last_write_at);
          }, py::arg("db_path"), py::arg("tenant"));

    // 租户不变式（spec §2.1）：只读扫一遍库，有别的租户的行抛 world_tenant_mismatch。
    // 2.6GB 的生产库约 90ms，释放 GIL。库文件不存在时直接通过。
    m.def("world_verify_tenant",
          // NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
          [](const std::string& db_path, const std::string& tenant) {
              const py::gil_scoped_release release;
              gov::verify_world_tenant(db_path, tenant);
          }, py::arg("db_path"), py::arg("tenant"));

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
    // integrity_check 可能要几秒，释放 GIL。C++ 的测试钩子（free_space、before_replace）不暴露。
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

    m.def("world_rollback",
          // NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
          [](const gov::RegistryDb& reg, const std::string& world, const std::string& snapshot_id,
             const std::string& vector_backend) {
              gov::RollbackOptions opts;
              opts.vector_backend = vector_backend;
              gov::RollbackResult result;
              {
                  const py::gil_scoped_release release;
                  result = gov::rollback_world(reg, world, snapshot_id, opts);
              }
              return py::dict("archive_path"_a = result.archive_path,
                              "restored_bytes"_a = result.restored_bytes);
          }, py::arg("registry"), py::arg("world"), py::arg("snapshot_id"), py::kw_only(),
          py::arg("vector_backend") = "sqlite");

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
}

}  // namespace starling::bindings
