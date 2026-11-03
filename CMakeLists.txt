cmake_minimum_required(VERSION 3.27)
project(starling LANGUAGES CXX VERSION 0.0.1)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

include(cmake/StarlingOptions.cmake)

function(starling_dependency_hint name hint)
    message(STATUS "Starling dependency ${name}: ${hint}")
endfunction()

# OpenSSL discovery — Homebrew on Apple Silicon installs at /opt/homebrew.
# Provide a fallback hint before find_package so a bare `cmake -S . -B build`
# works without the caller passing -DOPENSSL_ROOT_DIR.
if(APPLE AND NOT DEFINED OPENSSL_ROOT_DIR)
    if(EXISTS "/opt/homebrew/opt/openssl@3")
        set(OPENSSL_ROOT_DIR "/opt/homebrew/opt/openssl@3")
    endif()
endif()
find_package(OpenSSL REQUIRED)
starling_dependency_hint("OpenSSL" "${OPENSSL_VERSION} include=${OPENSSL_INCLUDE_DIR}")

# SQLite3 — Homebrew on Apple Silicon installs at /opt/homebrew/opt/sqlite.
# The system framework on macOS is older than 3.46; prefer Homebrew when present.
if(APPLE AND NOT DEFINED SQLite3_ROOT)
    if(EXISTS "/opt/homebrew/opt/sqlite/lib/libsqlite3.dylib")
        set(SQLite3_ROOT "/opt/homebrew/opt/sqlite")
        list(PREPEND CMAKE_PREFIX_PATH "/opt/homebrew/opt/sqlite")
    endif()
endif()
find_package(SQLite3 3.46 REQUIRED)
starling_dependency_hint("SQLite3" "${SQLite3_VERSION} include=${SQLite3_INCLUDE_DIRS}")

find_package(CURL REQUIRED)
starling_dependency_hint("CURL" "${CURL_VERSION_STRING} include=${CURL_INCLUDE_DIRS}")
if(NOT APPLE)
    find_package(ICU REQUIRED COMPONENTS uc)
    starling_dependency_hint("ICU uc" "${ICU_VERSION} include=${ICU_INCLUDE_DIRS}")
endif()
find_package(nlohmann_json 3.11 QUIET)
if(nlohmann_json_FOUND)
    starling_dependency_hint("nlohmann_json" "package config")
else()
    include(FetchContent)
    if(DEFINED FETCHCONTENT_SOURCE_DIR_JSON)
        starling_dependency_hint("nlohmann_json" "local source ${FETCHCONTENT_SOURCE_DIR_JSON}")
    else()
        starling_dependency_hint("nlohmann_json" "FetchContent network fallback")
    endif()
    FetchContent_Declare(json
        GIT_REPOSITORY https://github.com/nlohmann/json.git
        GIT_TAG v3.11.3)
    FetchContent_MakeAvailable(json)
endif()

add_library(starling_core STATIC)
set_target_properties(starling_core PROPERTIES POSITION_INDEPENDENT_CODE ON)
target_include_directories(starling_core PUBLIC include)
target_sources(starling_core PRIVATE
    src/preflight.cpp
    src/final_query_assertion.cpp
    src/runtime_health.cpp
    src/schema/canonicalize.cpp
    src/schema/common_ground_scope.cpp
    src/schema/normalize_theme.cpp
    src/schema/statement_enums.cpp
    src/schema/enums.cpp
    src/persistence/migration_runner.cpp
    src/persistence/connection.cpp
    src/bus/bus_event.cpp
    src/bus/outbox_writer.cpp
    src/bus/consumer_state.cpp
    src/bus/outbox_dispatcher.cpp
    src/bus/pipeline_ledger.cpp
    src/bus/statement_writer.cpp
    src/bus/normalized_interval.cpp
    src/bus/canonical_scope.cpp
    src/bus/conflict_key.cpp
    src/bus/conflict_probe.cpp
    src/bus/conflict_key_backfill.cpp
    src/persistence/sqlite_adapter.cpp
    src/store/sqlite_graph_store.cpp
    src/store/sqlite_statement_store.cpp
    src/store/episodic_event_store.cpp
    src/store/perception_state_store.cpp
    src/store/sqlite_meta_store.cpp
    src/store/store_bundle.cpp
    src/evidence/engram.cpp
    src/evidence/ingest_policy_resolver.cpp
    src/evidence/evidence_validator.cpp
    src/evidence/engram_store.cpp
    src/bus/bus.cpp
    src/net/http_post_json.cpp
    src/extractor/extraction_span_key.cpp
    src/extractor/fake_llm_adapter.cpp
    src/extractor/openai_adapter.cpp
    src/extractor/anthropic_adapter.cpp
    src/extractor/sse_stream.cpp
    src/extractor/json_parser.cpp
    src/extractor/claim_contract.cpp
    src/extractor/claim_scope.cpp
    src/extractor/structured_output.cpp
    src/extractor/statement_validator.cpp
    src/extractor/extractor.cpp
    src/extractor/episodic_extractor.cpp
    src/retrieval/basic_retriever.cpp
    src/retrieval/claim_evidence.cpp
    src/retrieval/temporal_evidence.cpp
    src/retrieval/structured_claim_retriever.cpp
    src/retrieval/semantic_retriever.cpp
    src/retrieval/affect_reranker.cpp
    src/retrieval/abstention.cpp
    src/retrieval/context_pack.cpp
    src/retrieval/retrieval_planner.cpp
    src/retrieval/source_retriever.cpp
    src/retrieval/source_selection.cpp
    src/retrieval/evidence_answer.cpp
    src/cognizer/uuid5.cpp
    src/cognizer/alias_normalizer.cpp
    src/cognizer/name_resolver.cpp
    src/cognizer/cognizer_hub.cpp
    src/cognizer/knowledge_frontier.cpp
    src/cognizer/perception_reconstructor.cpp
    src/tom/tom_engine.cpp
    src/tom/common_ground.cpp
    src/tom/common_ground_writer.cpp
    src/tom/common_ground_subscriber.cpp
    src/tom/persona_subscriber.cpp
    src/tom/nesting_depth_writer.cpp
    src/tom/rate_limiter.cpp
    src/tom/depth_estimator.cpp
    src/tom/limiting.cpp
    src/tom/second_order.cpp
    src/hippocampus/affect_buffer.cpp
    src/tom/mentalizing_believe.cpp
    src/tom/mentalizing_fauxpas.cpp
    src/tom/mentalizing_appraise.cpp
    src/tom/mentalizing_know.cpp
    src/tom/mentalizing_think.cpp
    src/tom/mentalizing_chain.cpp
    src/tom/mentalizing_common_knowledge.cpp
    src/tom/mentalizing_misalign.cpp
    src/tom/mentalizing_shared.cpp
    src/tom/mentalizing_more.cpp
    src/tom/mentalizing_profile.cpp
    src/tom/belief_tracker.cpp
    src/tom/belief_tracker_handlers.cpp
    src/replay/forgetting_curve.cpp
    src/replay/swr_sampler.cpp
    src/replay/consolidation_ops.cpp
    src/replay/gist_clustering.cpp
    src/replay/gist_prompt.cpp
    src/replay/gist_writer.cpp
    src/replay/replay_scheduler.cpp
    src/reconsolidation/plastic_window.cpp
    src/reconsolidation/arbitration.cpp
    src/reconsolidation/reconsolidation_engine.cpp
    src/neocortex/persona_container.cpp
    src/neocortex/common_ground_container.cpp
    src/hippocampus/working_set.cpp
    src/memory/memory_ops.cpp
    src/memory/memory_events.cpp
    src/memory/memory_digest.cpp
    src/projection/projection_maintainer.cpp
    src/bus/subscriber_pump.cpp
    src/vector/vector_math.cpp
    src/vector/pattern_separator.cpp
    src/vector/sqlite_blob_vector_index.cpp
    src/vector/sqlite_source_vector_index.cpp
    src/embedding/stub_embedding_adapter.cpp
    src/embedding/openai_embedding_adapter.cpp
    src/embedding/embedding_worker.cpp
    src/affect/affect_vector.cpp
    src/prospective/action_guard.cpp
    src/prospective/commitment_engine.cpp
    src/prospective/trigger.cpp
    src/prospective/policy_engine.cpp
    src/retrieval/pattern_completor.cpp
    src/governance/capability_policy.cpp
    src/governance/runtime_supervisor.cpp
    src/governance/write_gate.cpp
    src/governance/pipeline_run_store.cpp
    src/governance/scoped_work_gate.cpp
    src/governance/restart_guard.cpp
    src/governance/health_sampler.cpp
    src/governance/metrics_gatherer.cpp
    src/governance/world_store.cpp
    src/governance/registry_db.cpp
)
target_compile_options(starling_core PRIVATE ${STARLING_WARNING_FLAGS})
if(TARGET SQLite3::SQLite3)
    set(STARLING_SQLITE_TARGET SQLite3::SQLite3)
elseif(TARGET SQLite::SQLite3)
    set(STARLING_SQLITE_TARGET SQLite::SQLite3)
else()
    message(FATAL_ERROR "SQLite3 target not found after find_package(SQLite3). Pass -DSQLite3_INCLUDE_DIR and -DSQLite3_LIBRARY if CMake cannot locate SQLite >= 3.46.")
endif()
target_link_libraries(starling_core PUBLIC OpenSSL::Crypto ${STARLING_SQLITE_TARGET}
    CURL::libcurl
    nlohmann_json::nlohmann_json)
if(APPLE)
    target_link_libraries(starling_core PUBLIC "-framework CoreFoundation")
else()
    target_link_libraries(starling_core PUBLIC ICU::uc)
endif()

# P3.b1 phase 5: zvec 向量后端(STARLING_VECTOR_ZVEC=ON)。declare ExternalProject
# 编译 zvec + 合并 2-bundle + zvec_backend INTERFACE 库;ZvecVectorIndex 加入
# starling_core 并链接 zvec_backend(force_load plugins + main + STARLING_HAS_ZVEC)。
if(STARLING_VECTOR_ZVEC)
    include(cmake/StarlingZvec.cmake)
    target_sources(starling_core PRIVATE src/vector/zvec_vector_index.cpp)
    target_link_libraries(starling_core PUBLIC zvec_backend)
    add_dependencies(starling_core zvec_ext)
endif()

# Embed migration SQL into a header. Re-run cmake configure when files change.
file(GLOB STARLING_MIGRATION_FILES
    LIST_DIRECTORIES false
    RELATIVE "${CMAKE_SOURCE_DIR}/migrations"
    "${CMAKE_SOURCE_DIR}/migrations/*.sql")
list(SORT STARLING_MIGRATION_FILES)
set(STARLING_MIGRATIONS_INC "${CMAKE_BINARY_DIR}/generated/starling/migrations.inc")
file(MAKE_DIRECTORY "${CMAKE_BINARY_DIR}/generated/starling")
set(_mig_body "")
foreach(mig ${STARLING_MIGRATION_FILES})
    set(_mig_path "${CMAKE_SOURCE_DIR}/migrations/${mig}")
    file(READ "${_mig_path}" _mig_sql)
    string(REGEX MATCH "^([0-9]+)_(.+)\\.sql$" _ "${mig}")
    set(_mig_version_raw "${CMAKE_MATCH_1}")
    math(EXPR _mig_version "${_mig_version_raw}")  # strips leading zeros, base 10
    set(_mig_name "${CMAKE_MATCH_2}")
    string(APPEND _mig_body
        "  { ${_mig_version}, \"${_mig_name}\", R\"STARLING_MIG(${_mig_sql})STARLING_MIG\" },\n")
    set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${_mig_path}")
endforeach()
file(WRITE "${STARLING_MIGRATIONS_INC}"
    "// Auto-generated by CMakeLists.txt. Do not edit.\n"
    "// Each row: { version, name, raw_sql }.\n"
    "namespace starling::persistence::detail {\n"
    "struct EmbeddedMigration { int version; const char* name; const char* sql; };\n"
    "inline constexpr EmbeddedMigration kEmbeddedMigrations[] = {\n"
    "${_mig_body}"
    "};\n"
    "}\n")
target_include_directories(starling_core PRIVATE "${CMAKE_BINARY_DIR}/generated")

# Testing-only translation unit lives in its own target so prod can omit it.
# Links starling_core because mark_consolidated drives the same OutboxWriter +
# TransactionGuard primitives Bus uses — it just lives outside the prod link
# graph (CI scanner + preflight enforce that prod profiles never pull this
# target in).
add_library(starling_testing_marker STATIC src/testing/testing_marker.cpp)
set_target_properties(starling_testing_marker PROPERTIES POSITION_INDEPENDENT_CODE ON)
target_include_directories(starling_testing_marker PUBLIC include)
target_link_libraries(starling_testing_marker PUBLIC starling_core)

option(STARLING_BUILD_PYTHON "Build pybind11 bindings" ON)
if(STARLING_BUILD_PYTHON)
    add_subdirectory(bindings/python)
endif()

enable_testing()
option(STARLING_BUILD_TESTS "Build C++ tests" ON)
if(STARLING_BUILD_TESTS)
    add_subdirectory(tests/cpp)
endif()
