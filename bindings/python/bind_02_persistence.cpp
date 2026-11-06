// bind_02_persistence — M0.2: BusEvent / compute_idempotency_key / SqliteAdapter
// Split verbatim from bindings/python/module.cpp (original lines 300-365).
// Registration order across bind_01..bind_12 mirrors the original file and is
// load-bearing (pybind11 requires base classes registered before derived).

#include "bind_common.hpp"

#include <string_view>

#include "starling/bus/bus_event.hpp"
#include "starling/bus/outbox_writer.hpp"
#include "starling/persistence/connection.hpp"
#include "starling/persistence/migration_runner.hpp"
#include "starling/persistence/sqlite_adapter.hpp"

namespace starling::bindings {

void bind_02_persistence(pybind11::module_& m) {
    using namespace pybind11::literals;

    // ----- M0.2: bus + persistence bindings -----

    py::class_<starling::bus::BusEvent>(m, "BusEvent")
        .def(py::init<>())
        .def_readwrite("event_id",        &starling::bus::BusEvent::event_id)
        .def_readwrite("tenant_id",       &starling::bus::BusEvent::tenant_id)
        .def_readwrite("event_type",      &starling::bus::BusEvent::event_type)
        .def_readwrite("primary_id",      &starling::bus::BusEvent::primary_id)
        .def_readwrite("aggregate_id",    &starling::bus::BusEvent::aggregate_id)
        .def_readwrite("outbox_sequence", &starling::bus::BusEvent::outbox_sequence)
        .def_readwrite("causation_chain", &starling::bus::BusEvent::causation_chain)
        .def_readwrite("idempotency_key", &starling::bus::BusEvent::idempotency_key)
        .def_readwrite("payload_json",    &starling::bus::BusEvent::payload_json)
        .def_readwrite("created_at",      &starling::bus::BusEvent::created_at)
        .def_readwrite("version",         &starling::bus::BusEvent::version);

    m.def("compute_idempotency_key",
          [](std::string event_type, std::string aggregate_id,
             std::string canonical_key, std::string causation_root,
             std::string window_bucket) {
              // Take by value so we own stable storage; pass string_views into
              // the C++ formula. Avoids any lifetime question on the Python
              // str → string_view conversion path.
              return starling::bus::compute_idempotency_key(
                  event_type, aggregate_id, canonical_key,
                  causation_root, window_bucket);
          },
          py::arg("event_type"),
          py::arg("aggregate_id"),
          py::arg("canonical_key"),
          py::arg("causation_root"),
          py::arg("window_bucket"));

    // SqliteAdapter is non-copyable + non-movable (Adapter base deletes both).
    // The unique_ptr holder lets pybind take ownership from open()'s factory.
    py::class_<starling::persistence::SqliteAdapter,
               std::unique_ptr<starling::persistence::SqliteAdapter>>(m, "SqliteAdapter")
        .def_static("open", [](const std::string& path) {
            return starling::persistence::SqliteAdapter::open(path);
        }, py::arg("db_path"))
        .def_static("open_read_only", [](const std::string& path) {
            return starling::persistence::SqliteAdapter::open_read_only(path);
        }, py::arg("db_path"), py::call_guard<py::gil_scoped_release>(),
           "Open an existing database read-only (mode=ro): no migrations, no "
           "journal_mode change, no directories. write_admitted() is always False.")
        .def_property_readonly("read_only",
             &starling::persistence::SqliteAdapter::read_only)
        .def("schema_version",
             &starling::persistence::SqliteAdapter::schema_version,
             py::call_guard<py::gil_scoped_release>(),
             "MAX(version) of schema_migrations; 0 when the table does not exist.")
        .def("declare_capability",
             &starling::persistence::SqliteAdapter::declare_capability)
        .def("has_index",
             [](starling::persistence::SqliteAdapter& adapter, std::string_view name) {
                 return adapter.has_index(name);
             },
             py::arg("name"),
             "Returns whether a named SQLite index exists (legacy-compat probe: "
             "proves existence only, NOT tenant-isolation semantics).")
        .def("check_final_query",
             &starling::persistence::SqliteAdapter::check_final_query,
             py::arg("sql"))
        .def_property_readonly("db_path", [](const starling::persistence::SqliteAdapter& a) {
            return a.db_path().string();
        })
        // append_event_unsafe is a TEST-ONLY shortcut: it wraps OutboxWriter in
        // a self-contained transaction, bypassing the producer's domain write.
        // Real producers must share their own transaction with OutboxWriter —
        // this binding exists solely so TC-NEW-OUTBOX-IDEMP can seed events
        // without duplicating the schema layer in Python. The CI static scan
        // refuses any prod-entrypoint reference to this name.
        // NOLINT(starling-testing-isolation): definition site of the test-only
        // binding; prod call sites are still rejected by the scanner.
        .def("append_event_unsafe", [](starling::persistence::SqliteAdapter& a,
                                        starling::bus::BusEvent& ev) {
            starling::persistence::TransactionGuard g(a.connection());
            starling::bus::OutboxWriter w(a.connection());
            w.append(ev);
            g.commit();
        }, py::arg("event"))
        .def("close", &starling::persistence::SqliteAdapter::close,
             py::call_guard<py::gil_scoped_release>(),
             "Merge the WAL into the main file and release the sqlite handle. "
             "Idempotent. connection() raises RuntimeError afterwards. The caller "
             "must have no call in flight (starling.Memory.close ensures this).")
        .def("write_admitted",
             &starling::persistence::SqliteAdapter::write_admitted,
             "Returns True when the adapter's write gate allows writes "
             "(no hook set → always True; DRAINING → False).")
        .def("connection",
             &starling::persistence::SqliteAdapter::connection,
             py::return_value_policy::reference_internal);

    m.def("latest_schema_version",
          &starling::persistence::MigrationRunner::latest_embedded_version,
          "Highest migration version embedded in this build.");
}

}  // namespace starling::bindings
