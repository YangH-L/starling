# 记忆服务第 1 期：多世界同时运行 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 一个看板服务进程同时打开多个世界、各有引擎、按世界寻址读写；去掉「活动世界」；注册表改存 SQLite；每个世界有自己的设置（建时从默认世界复制）和不可变标签。

**Architecture:** C++ `governance` 新增 `registry_db`（SQLite 注册表：世界、标签、设置、快照、统计缓存）与 `world_settings`（合并、继承、`endpoint_changed` 判定、摄入分区路径），现有 `world_store` 的快照、回滚、自动快照逻辑改为读写新注册表。Python 侧 `WorldManager` 改成「引擎池」：每个世界一个 `DashboardEngine`，租约与关停按世界；看板路由挂在 `/api/w/{world}/...`，旧路径指向默认世界。前端加世界选择器，所有取数带世界名。

**Tech Stack:** C++20 + SQLite（`persistence::Connection` / `Stmt`）+ nlohmann/json + GoogleTest；pybind11；FastAPI + anyio；SvelteKit（Svelte 5 runes）+ vitest。

**Spec:** `docs/superpowers/specs/2026-11-03-memory-service-design.md`（本计划只覆盖 §17 第 1 期；§4、§6、§9、§10.1 入口、§11、§13.1 客户端路由属于后续各期）

## Global Constraints

- 核心逻辑放 C++（`src/` + `include/starling/`），Python 只做适配；越界判据：换一种绑定语言是否需要重写该逻辑（仓库 `CLAUDE.md`「架构边界」）。服务端 Python 里出现「判断」（能不能删、能不能恢复、设置怎么合并、该不该关）就是越界（spec §15）。
- 写后/订阅者路径用 SAVEPOINT，不用 BEGIN（`CLAUDE.md`「写入纪律」）。
- 世界名：首字符是字母或数字，其余 `[A-Za-z0-9._-]`，长度 1–128，区分大小写（spec §5.3）。
- 新世界目录是 `<数据目录>/worlds/<dir_id>/`，`dir_id` 由 C++ 生成（随机十六进制）；导入的旧世界保留原目录名作为 `dir_id`（spec §5.3）。
- 不在每次提交时写 `registry.db`；开着的世界从引擎读计数，关闭世界时写缓存，开着的世界每 30 秒刷一次缓存（spec §5.5）。
- 设置：调用方给了哪项用哪项，没给的从默认世界的当前设置复制；复制不是引用；`vector_backend` 只允许 `sqlite`（spec §8.1、§8.2）。
- 密钥不进注册表、不进 HTTP 响应、不进日志；只给 `key_set: bool`（spec §8.3）。
- 继承的 provider 的 `base_url` 与世界里记的不同 → `endpoint_changed`，拒绝调用（spec §8.3）。
- 摄入分区：默认世界保留 `tenant-<tenant>` 目录，其他世界用 `world-<dir_id>`（spec §8.4）。
- 看板 API 按世界寻址：`/api/w/{world}/...`；旧路径（不带世界名）继续可用，指向默认世界（spec §13.1）。
- 错误响应形状 `{"detail": "<code>"}` 保持不变（前端按 `detail` 显示提示）；统一的 `{"code","transient","detail"}` 形状属于第 4 期的客户端路由。
- 标签建世界时一次写入，之后不能改（`labels_immutable`）（spec §5.2）。
- 默认世界的行为与今天一致：tick 30 秒、摄入开、自动快照按时间 24 小时；本机 hook 投递的对话写进默认世界（spec §8.5）。
- 恢复只停被恢复的世界的引擎（spec §7.2）。
- 提交门：全量 ctest + `pytest tests/python` 绿；前端 `npm run check` / `npx vitest run` / `npm run build`（在 `dashboard/web/`）；C++ 改动行跑 `scripts/ci_clang_tidy_diff.py`（clang-tidy 在 `/opt/homebrew/opt/llvm/bin`，不在 PATH；改动行上标识符至少 3 字符）。
- git：只用显式路径 `git add`；不用 `--no-verify` / `--amend`。提交信息末尾加 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`。
- 改 C++/绑定/迁移后刷新 `_core`：`cmake --build build && cmake --install build --prefix .venv/lib/python3.14/site-packages`（worktree 自建 venv，见执行说明）。

## Review Focus

1. **同一世界被并发打开两次**：两个请求同时首次访问一个关着的世界，必须只建一个引擎（同一库文件上两条写连接会互相截断 WAL）。测试加在 Task 5。
2. **关闭世界与在途请求交错**：空闲关闭或删除世界时，持租约的请求还在跑，必须等租约归零再丢引擎；之后新请求能重新打开。测试加在 Task 5。
3. **大小写只差一位的两个世界名**（`Run-A` 与 `run-a`）：在 macOS 默认不区分大小写的文件系统上必须是两个独立的目录、两个独立的库。测试加在 Task 1。
4. **默认世界被删除或改名**：任何接口都不能删掉默认世界，也不能让注册表处于没有默认世界的状态。测试加在 Task 1。
5. **`worlds.json` 导入一半崩溃**：导入中途失败，下次启动必须能重新导入，`worlds.json` 不能先被改名。测试加在 Task 2。

---

## 执行说明

- 在 worktree `/Users/jaredguo-mini/develop/memory/starling-wt-service` 上新建分支 `feat/memory-service-p1`（从 `docs/memory-service-design` 拉出，带着 spec 与本计划）。本期改 C++，按记忆里的「worktree C++ editable 构建」配方自建 venv：
  ```bash
  cd /Users/jaredguo-mini/develop/memory/starling-wt-service
  python3.14 -m venv .venv && source .venv/bin/activate
  pip install -q cmake==4.3.2 ninja scikit-build-core pybind11
  pip install -e ".[dev,dashboard]" --no-build-isolation --config-settings=build-dir=build
  ```
- C++ 测试：`cmake --build build && ctest --test-dir build -R <名字> --output-on-failure`。
- Python 测试：`pytest tests/python/<文件> -q`。
- 前端：`cd dashboard/web && npm install`（第一次），之后 `npx vitest run <文件>`。
- 现有 `tests/python/test_dashboard_worlds.py`（184 个用例、4672 行）大量围绕「激活/切换」。改动「活动世界」的任务里，删掉专测切换语义的用例，保留并改写快照、回滚、自动快照、注册表的用例；每删一个用例，在提交信息里列出被删的用例名和原因（「切换语义已移除」）。

---

### Task 1: SQLite 注册表（C++）

**Files:**
- Create: `include/starling/governance/registry_db.hpp`
- Create: `src/governance/registry_db.cpp`
- Modify: `src/governance/world_store.cpp:37-45`（`is_valid_world_name` 换新规则）
- Modify: `CMakeLists.txt` 或 `src/governance/CMakeLists.txt`（把新源文件加进 governance 目标；按现有写法）
- Test: `tests/cpp/test_registry_db.cpp`（加进 tests 的 CMake 列表）

**Interfaces:**
- Consumes: `persistence::Connection`、`persistence::Stmt`（`include/starling/persistence/`）；`WorldStoreError`、`SnapshotEntry`、`AutoSnapshotConfig`（`world_store.hpp`）。
- Produces（命名空间 `starling::governance`）：

```cpp
struct WorldRecord {
    std::string name, dir_id, tenant, db_path, snapshot_dir, label, created_at;
    bool seed = false, read_only = false, reclaimable = false;   // 第 3、6 期才写入，本期恒为 false
    std::string retention_group, fork_of_world, fork_of_snapshot;  // 同上，本期为空
    std::string settings_json;                                    // Task 3 的 WorldSettings 序列化
    std::map<std::string, std::string> labels;
    std::int64_t holders = 0, statements = 0;                     // 统计缓存
    std::string last_write_at, stats_at;
};
struct WorldPage { std::vector<WorldRecord> worlds; std::int64_t total = 0; };
struct WorldQuery {
    std::map<std::string, std::string> labels;   // 全部匹配（AND）
    std::int64_t offset = 0, limit = 50;         // limit 1–500
};

class RegistryDb {
public:
    // 打开或创建 <data_dir>/registry.db（WAL）。不存在时建表，并登记默认世界：
    //   name="default", tenant=default_tenant, db_path=default_db_path（原地，不受布局约束），
    //   dir_id="default", label="主世界"。
    static RegistryDb open(const std::filesystem::path& data_dir,
                           const std::filesystem::path& default_db_path,
                           const std::string& default_tenant);
    [[nodiscard]] std::string default_world() const;
    [[nodiscard]] WorldRecord get(const std::string& name) const;            // world_not_found
    [[nodiscard]] WorldPage list(const WorldQuery& query) const;             // 按 created_at 降序
    WorldRecord create(const std::string& name, const std::string& label,
                       const std::map<std::string, std::string>& labels,
                       const std::string& settings_json, std::string_view now_iso);
    void remove(const std::string& name);       // default → world_protected；删注册项与世界目录
    void set_labels(const std::string& name, const std::map<std::string, std::string>& labels);
                                                // 已有标签 → labels_immutable
    void update_settings(const std::string& name, const std::string& settings_json);
    void update_stats(const std::string& name, std::int64_t holders, std::int64_t statements,
                      const std::string& last_write_at, std::string_view now_iso);
    // 快照条目（与 SnapshotEntry 同字段），最新在前
    [[nodiscard]] std::vector<SnapshotEntry> snapshots(const std::string& world) const;
    void put_snapshot(const std::string& world, const SnapshotEntry& entry);
    void drop_snapshot(const std::string& world, const std::string& snapshot_id);
    [[nodiscard]] int snapshot_retention() const;                             // 默认 5
    [[nodiscard]] const std::filesystem::path& data_dir() const;
};
```

- [ ] **Step 1: 写失败的测试** `tests/cpp/test_registry_db.cpp`

```cpp
TEST(RegistryDb, OpenCreatesDefaultWorldOnce) {
    // 两次 open 同一 data_dir：default 只有一条，db_path 是传入的绝对路径，label "主世界"
}
TEST(RegistryDb, WorldNameRules) {
    EXPECT_TRUE(is_valid_world_name("rise.deploy-S1_a"));
    EXPECT_TRUE(is_valid_world_name("Run-A"));
    EXPECT_TRUE(is_valid_world_name(std::string(128, 'a')));
    EXPECT_FALSE(is_valid_world_name(std::string(129, 'a')));
    EXPECT_FALSE(is_valid_world_name(""));
    EXPECT_FALSE(is_valid_world_name(".hidden"));
    EXPECT_FALSE(is_valid_world_name("-x"));
    EXPECT_FALSE(is_valid_world_name("a/b"));
    EXPECT_FALSE(is_valid_world_name("a b"));
}
TEST(RegistryDb, CaseDistinctNamesGetDistinctDirs) {   // Review Focus 3
    // create("Run-A") 与 create("run-a") 都成功；dir_id 不同；db_path 不同；两个目录都存在
}
TEST(RegistryDb, CreateRejectsDuplicateAndBadName) {
    // 再建 "Run-A" → world_exists；建 ".x" → invalid_world_name；注册表不变
}
TEST(RegistryDb, DefaultWorldCannotBeRemoved) {        // Review Focus 4
    // remove("default") → world_protected；default_world() 仍是 "default"
}
TEST(RegistryDb, LabelsAreImmutable) {
    // create(..., labels={{"run_id","r1"}}) 之后 set_labels → labels_immutable；
    // 无标签的世界 set_labels 一次成功、第二次 labels_immutable
}
TEST(RegistryDb, ListFiltersByLabelsAndPages) {
    // 建 7 个世界：3 个 run_id=r1、4 个 run_id=r2；list({labels:{run_id:r1}}) → total 3；
    // list({limit:2, offset:2}) → 2 条，total 8（含 default）；按 created_at 降序
}
TEST(RegistryDb, RemoveDeletesWorldDirectoryOnlyUnderWorlds) {
    // remove 后目录不在；手改 db 把 dir_id 改成 "../x" → remove 抛 snapshot_failed，什么都不删
}
TEST(RegistryDb, StatsAndSnapshotsRoundTrip) {
    // update_stats 后 get 读回；put_snapshot 两条，snapshots() 最新在前；drop_snapshot 一条
}
```

- [ ] **Step 2: 运行，确认失败**

Run: `cmake --build build && ctest --test-dir build -R RegistryDb --output-on-failure`
Expected: 编译失败（`registry_db.hpp` 不存在）。

- [ ] **Step 3: 实现 `RegistryDb`**

表结构（建表 SQL 写在 `registry_db.cpp` 里，`meta.schema_version=1`）：`meta(key PRIMARY KEY, value)`、`worlds(name PRIMARY KEY, dir_id UNIQUE, tenant, db_path, snapshot_dir, label, created_at, seed, read_only, reclaimable, retention_group, fork_of_world, fork_of_snapshot, settings_json, holders, statements, last_write_at, stats_at)`、`world_labels(name, key, value, PRIMARY KEY(name,key))` + 索引 `(key,value)`、`snapshots(world, id, path, kind, reason, note, created_at, world_time, size_bytes, sessions_complete, session_id, content_mark, PRIMARY KEY(world,id))`。`dir_id` 用 16 位随机小写十六进制（与 `world_store.cpp:638` 的 `random_hex` 同源），撞了重抽。`remove` 复用 `world_store.cpp` 的 `remove_within` 与布局检查（比较对象是 `worlds/<dir_id>`）。每个写方法一个事务。`is_valid_world_name` 换成 Global Constraints 里的规则。

- [ ] **Step 4: 运行，确认通过**

Run: `ctest --test-dir build -R 'RegistryDb|WorldName' --output-on-failure`
Expected: 全部 PASS。`WorldName.AcceptsAndRejects`（`tests/cpp/test_world_store.cpp:69`）按新规则改断言。

- [ ] **Step 5: 提交**

```bash
git add include/starling/governance/registry_db.hpp src/governance/registry_db.cpp src/governance/world_store.cpp tests/cpp/test_registry_db.cpp tests/cpp/test_world_store.cpp <CMake 文件>
git commit -m "feat(world): SQLite 注册表 registry.db——世界、标签、快照、统计缓存；世界名放宽到 128 字符、目录用 dir_id"
```

---

### Task 2: `world_store` 改用 `RegistryDb`，导入 `worlds.json`（C++）

**Files:**
- Modify: `include/starling/governance/world_store.hpp`（快照、回滚、自动快照、会话清单、归档的签名从 `Registry&` 改为 `RegistryDb&`；删 `set_active`、`Registry::active`）
- Modify: `src/governance/world_store.cpp`
- Create: `src/governance/registry_import.cpp`、在 `registry_db.hpp` 里声明 `import_worlds_json`
- Test: `tests/cpp/test_world_store.cpp`（改写）、`tests/cpp/test_registry_import.cpp`

**Interfaces:**
- Consumes: Task 1 的 `RegistryDb`、`WorldRecord`。
- Produces：

```cpp
// 有 worlds.json 且没有 registry.db 时调用：在临时文件 registry.db.importing 里建库、写入全部世界与快照，
// 成功后 rename 成 registry.db，再把 worlds.json 改名为 worlds.json.imported-<compact_now>。
// 任一步失败：删掉 registry.db.importing，worlds.json 原样不动，抛 registry_corrupt。
void import_worlds_json(const std::filesystem::path& data_dir);
// 现有函数改签名（语义不变，见 world_store.hpp 现有注释）：
SnapshotEntry take_snapshot(RegistryDb& reg, const std::string& world, const SnapshotOptions& opts);
std::vector<std::string> apply_retention(RegistryDb& reg, const std::string& world);
void delete_snapshot(RegistryDb& reg, const std::string& world, const std::string& snapshot_id);
AutoSnapshotResult take_auto_snapshot(RegistryDb& reg, const std::string& world, const AutoTrigger& trigger);
SessionList list_sessions(const RegistryDb& reg, const std::string& world, std::int64_t limit);
void check_rollback(const RegistryDb& reg, const std::string& world, const std::string& snapshot_id, const RollbackOptions& opts);
RollbackResult rollback_world(const RegistryDb& reg, const std::string& world, const std::string& snapshot_id, const RollbackOptions& opts);
std::vector<ArchiveEntry> list_rollback_archives(const RegistryDb& reg, const std::string& world);
std::size_t purge_rollback_archives(const RegistryDb& reg, const std::string& world);
void set_auto_snapshot(RegistryDb& reg, const std::string& world, const AutoSnapshotConfig& config);
```

`registry_dir` 参数去掉，统一用 `reg.data_dir()`；世界 home 由 `dir_id` 推出（`checked_world_home` 改为按 `dir_id`）。自动快照配置从 `WorldRecord.settings_json` 的 `background.auto_snapshot` 读写（Task 3 定义该结构；本任务先按 `{"background":{"auto_snapshot":{...}}}` 读写这一个键，其余键原样保留）。

- [ ] **Step 1: 写失败的测试**

```cpp
TEST(RegistryImport, ImportsWorldsAndSnapshots) {
    // 用旧 API 先造一个 worlds.json：default + "sim-1"（含 2 份快照、auto_snapshot {true,0}）
    // import_worlds_json → registry.db 里两个世界；sim-1 的 dir_id == "sim-1"；快照 2 份、字段逐一相等；
    // worlds.json 不在，worlds.json.imported-* 在
}
TEST(RegistryImport, CrashMidImportLeavesJsonUntouched) {   // Review Focus 5
    // 注入失败（把 worlds.json 里一份快照的 path 改成越界路径）→ 抛 registry_corrupt；
    // worlds.json 仍在且字节不变；registry.db 与 registry.db.importing 都不存在；
    // 修好 worlds.json 后再 import 成功
}
TEST(WorldSnapshot, TakeAndRetentionOnRegistryDb) { /* 现有 take_snapshot/apply_retention 用例改用 RegistryDb */ }
TEST(WorldRollback, RollbackOnRegistryDb) { /* 现有 rollback_world 用例改用 RegistryDb，世界目录按 dir_id */ }
```

`tests/cpp/test_world_store.cpp` 里所有 `Registry` 用例改成 `RegistryDb`；`set_active`、`active` 相关用例删除（列进提交信息）。

- [ ] **Step 2: 运行，确认失败**

Run: `cmake --build build 2>&1 | tail -5`
Expected: 编译失败（签名不匹配）。

- [ ] **Step 3: 实现** 改签名、改实现；删除 `registry_to_json`/`registry_from_json`/`load_registry`/`save_registry`/`ensure_registry`/`set_active` 及其校验里只为 JSON 存在的部分（布局规则 1–3 保留，改为对 `RegistryDb` 的记录检查）。

- [ ] **Step 4: 运行，确认通过**

Run: `ctest --test-dir build -R 'World|Registry' --output-on-failure`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add include/starling/governance/world_store.hpp include/starling/governance/registry_db.hpp src/governance/world_store.cpp src/governance/registry_import.cpp tests/cpp/test_world_store.cpp tests/cpp/test_registry_import.cpp <CMake 文件>
git commit -m "refactor(world): 快照、回滚、自动快照改读写 registry.db；worlds.json 一次性导入；去掉活动世界"
```

---

### Task 3: 每个世界的设置（C++）

**Files:**
- Create: `include/starling/governance/world_settings.hpp`、`src/governance/world_settings.cpp`
- Test: `tests/cpp/test_world_settings.cpp`

**Interfaces:**
- Consumes: 无（纯数据 + JSON）。
- Produces：

```cpp
struct EndpointSettings {
    std::string provider;        // "openai" | "anthropic"
    std::string provider_name;   // 引用 starling.json 的 provider；客户端显式给的为空
    std::string base_url, model;
    std::int64_t dim = 0, max_tokens = 0, timeout_ms = 0;
    std::optional<std::int64_t> max_retries;
    std::optional<bool> enable_thinking;
    std::string thinking_type;   // "" | "disabled" | ...
};
struct BackgroundSettings {
    double tick_interval_s = 30.0;
    bool ingest = true;
    AutoSnapshotConfig auto_snapshot{.on_session = false, .interval_hours = 24};
};
struct SourceLaneSettings {
    bool enabled = true, render_time = false;
    double tau_source = 0.56; std::string model = "text-embedding-v4";
    std::int64_t dim = 1024, k_source = 30, chunk_byte_budget = 32000;
};
struct WorldSettings {
    std::optional<EndpointSettings> extraction, embedding, chat;
    std::string vector_backend = "sqlite";
    SourceLaneSettings source_lane;
    std::map<std::string, double> gist_thresholds;
    BackgroundSettings background;
};
// 解析/序列化；未知键报 invalid_settings；vector_backend 不是 "sqlite" 报 snapshot_unsupported_backend
WorldSettings settings_from_json(const std::string& text);
std::string settings_to_json(const WorldSettings& settings);
// overrides 是调用方给的部分设置（JSON 对象，只含要覆盖的顶层键与子键）；没给的键从 base 复制。
// read_only=true 时 background 强制全关（tick 0、ingest false、auto_snapshot {false,0}）。
WorldSettings merge_settings(const WorldSettings& base, const std::string& overrides_json, bool read_only);
// 默认世界的 endpoint 改了 base_url 之后，继承来的世界调用前判定：
//   world 里的 provider_name 非空，且 current_provider_base_url != world 记的 base_url → 抛 endpoint_changed
void require_endpoint_unchanged(const EndpointSettings& world_endpoint,
                                const std::string& current_provider_base_url);
// 摄入分区目录名：默认世界 "tenant-" + 百分号编码(tenant)，其余 "world-" + dir_id
std::string ingest_partition(bool is_default_world, const std::string& tenant, const std::string& dir_id);
```

- [ ] **Step 1: 写失败的测试**

```cpp
TEST(WorldSettings, RoundTripAllFields) { /* 每个字段都设非默认值，to_json→from_json 相等 */ }
TEST(WorldSettings, MergeCopiesMissingKeysFromBase) {
    // base.extraction.model="m1"；overrides {"extraction":{"model":"m2"}} → model m2，base_url 沿用 base
    // overrides {} → 与 base 相等；改 base 之后已合并出的结果不变（值拷贝）
}
TEST(WorldSettings, ReadOnlyForcesBackgroundOff) {
    // overrides {"background":{"tick_interval_s":5}} + read_only → tick 0、ingest false、auto {false,0}
}
TEST(WorldSettings, RejectsZvecAndUnknownKeys) {
    // {"vector_backend":"zvec"} → snapshot_unsupported_backend；{"foo":1} → invalid_settings
}
TEST(WorldSettings, EndpointChanged) {
    // provider_name "p", base_url "http://a" ；current "http://a" → 不抛；"http://b" → endpoint_changed；
    // provider_name ""（客户端显式给的）→ 永不抛
}
TEST(WorldSettings, IngestPartition) {
    EXPECT_EQ(ingest_partition(true, "default", "x"), "tenant-default");
    EXPECT_EQ(ingest_partition(true, "a/b", "x"), "tenant-a%2Fb");
    EXPECT_EQ(ingest_partition(false, "default", "3f9a"), "world-3f9a");
}
```

- [ ] **Step 2: 运行，确认失败** — Run: `cmake --build build && ctest --test-dir build -R WorldSettings` — Expected: 编译失败。
- [ ] **Step 3: 实现** 百分号编码与 `python/starling/dashboard/ingest_spool.py:12` 的 `quote(tenant, safe='')` 逐字节一致（只保留 `[A-Za-z0-9_.~-]`）。
- [ ] **Step 4: 运行，确认通过** — Run: `ctest --test-dir build -R WorldSettings --output-on-failure` — Expected: PASS。
- [ ] **Step 5: 提交**

```bash
git add include/starling/governance/world_settings.hpp src/governance/world_settings.cpp tests/cpp/test_world_settings.cpp <CMake 文件>
git commit -m "feat(world): 每个世界的设置——合并继承、只读关后台、endpoint_changed、摄入分区"
```

---

### Task 4: 世界相关绑定改到新注册表

**Files:**
- Modify: `bindings/python/bind_15_world.cpp`
- Test: `tests/python/test_world_registry_bindings.py`（新）；`tests/python/test_dashboard_worlds.py` 里 `test_binding_*` 三个用例改写

**Interfaces:**
- Consumes: Task 1–3 的 C++ 接口。
- Produces（`starling._core`，所有长 I/O 释放 GIL；错误仍抛 `WorldStoreError`，`world_store_error_code` 取码）：

```python
class WorldRegistry:                      # pybind11 包 RegistryDb，线程安全由 C++ 内部互斥保证
    @staticmethod
    def open(data_dir: str, default_db_path: str, default_tenant: str) -> "WorldRegistry": ...
    def default_world(self) -> str: ...
    def get(self, name: str) -> dict: ...         # WorldRecord 字段；settings 已解析成 dict；labels dict
    def list(self, labels: dict[str, str] | None = None, offset: int = 0, limit: int = 50) -> dict: ...
                                                   # {"worlds": [dict], "total": int}
    def create(self, name: str, label: str, labels: dict[str, str], overrides: dict | None,
               now_iso: str) -> dict: ...          # 设置 = merge_settings(默认世界设置, overrides, False)
    def remove(self, name: str) -> None: ...
    def update_settings(self, name: str, overrides: dict) -> dict: ...
    def update_stats(self, name: str, holders: int, statements: int, last_write_at: str, now_iso: str) -> None: ...
    def snapshots(self, world: str) -> list[dict]: ...
    data_dir: str                                  # 只读属性

def world_import_json(data_dir: str) -> None: ...
def world_require_endpoint_unchanged(world_endpoint: dict, current_base_url: str) -> None: ...
def world_ingest_partition(is_default: bool, tenant: str, dir_id: str) -> str: ...
# 原有 world_take_snapshot / world_take_auto_snapshot / world_list_sessions / world_check_rollback /
# world_rollback / world_list_archives / world_purge_archives / world_delete_snapshot /
# world_set_auto_snapshot / world_stats / world_verify_tenant：第一个参数从 registry dict 改为 WorldRegistry，
# 去掉 reg_dir 参数，返回值不变（不再返回 "registry" 键）。
# 删除：world_ensure_registry / world_load_registry / world_save_registry / world_create / world_delete / world_set_active
```

- [ ] **Step 1: 写失败的测试** `tests/python/test_world_registry_bindings.py`

```python
def test_open_create_list_roundtrip(tmp_path): ...       # create 两个世界带标签；list 过滤；get 读回 settings
def test_create_inherits_default_settings_by_copy(tmp_path):
    # 默认世界 update_settings({"extraction": {"model": "m1", ...}})；create("w1", overrides=None)
    # → w1 的 extraction.model == "m1"；再把默认世界改成 "m2"；w1 仍是 "m1"
def test_error_codes(tmp_path):
    # get("nope") → world_not_found；create("default") → world_exists；remove("default") → world_protected
def test_snapshot_and_rollback_on_registry(tmp_path): ...   # 取代原 test_binding_snapshot_and_stats
def test_registry_calls_release_gil(tmp_path): ...          # 照 tests/python/test_gil_release.py 的 _progress_while 写法测 take_snapshot
```

- [ ] **Step 2: 运行，确认失败** — Run: `pytest tests/python/test_world_registry_bindings.py -q` — Expected: `AttributeError: module 'starling._core' has no attribute 'WorldRegistry'`。
- [ ] **Step 3: 实现** 绑定；`settings` 在 C++ 里 JSON 与 dict 互转（沿用文件里 `to_py`/`from_py`）。
- [ ] **Step 4: 刷新 `_core` 并运行** — Run: `cmake --build build && cmake --install build --prefix .venv/lib/python3.14/site-packages && pytest tests/python/test_world_registry_bindings.py -q` — Expected: PASS。
- [ ] **Step 5: 提交**

```bash
git add bindings/python/bind_15_world.cpp tests/python/test_world_registry_bindings.py tests/python/test_dashboard_worlds.py
git commit -m "feat(bindings): WorldRegistry 绑定与快照、回滚接口改用 registry.db"
```

---

### Task 5: 引擎池（Python）

**Files:**
- Modify: `python/starling/dashboard/world_manager.py`（重写为引擎池；保留模块文档里锁顺序一节并改写为按世界）
- Modify: `python/starling/dashboard/engine.py:174-256`（`DashboardEngine.__init__` 收一个 `WorldContext`）
- Modify: `python/starling/dashboard/config.py`（`DashboardConfig` 增加 `data_dir`；`world_settings()` 把当前 provider/roles/lane/gist/tick 转成 Task 3 的设置 dict）
- Test: `tests/python/test_world_pool.py`（新）

**Interfaces:**
- Consumes: Task 4 的 `_core.WorldRegistry`、`world_ingest_partition`、`world_require_endpoint_unchanged`。
- Produces：

```python
@dataclass(frozen=True)
class WorldContext:            # 一个世界打开引擎需要的全部输入，从注册表记录推出
    name: str; dir_id: str; db_path: str; tenant: str; agent: str
    settings: dict             # 已合并的 WorldSettings dict
    ingest_spool_dir: str      # <data_dir>/ingest-spool/<world_ingest_partition(...)>
    is_default: bool

class DashboardEngine:
    def __init__(self, ctx: WorldContext, providers: dict) -> None: ...
    # providers：starling.json 的 provider 注册表（含密钥），只用来按 provider_name 取 api_key 与当前 base_url；
    # 调用前对每个继承来的 endpoint 调 world_require_endpoint_unchanged

    on_commit: Callable[[], None] | None   # 每个写方法（remember、forget、clear_memory、做了事的 tick、
                                           # 摄入一个 job）成功后调用一次；由 WorldPool 设置

EngineFactory = Callable[[WorldContext, dict], DashboardEngine]   # 测试注入 stub llm 用

class WorldPool:
    def __init__(self, registry: "_core.WorldRegistry", config: DashboardConfig, *, clock=None,
                 engine_factory: EngineFactory | None = None,
                 on_world_changed: Callable[[str, dict], None] | None = None) -> None: ...
        # on_world_changed(world, {"statements": n, "last_write_at": t})：由 engine.on_commit 触发，
        # 每个世界每秒至多一次（去抖），app 用它往 /ws 广播
    def lease(self, world: str, *, build: bool = True) -> EngineLease: ...
        # 世界不存在 → WorldSwitchError("world_not_found")；正在关闭/恢复 → WorldSwitchError("world_busy")
        # build=False 且没开着 → lease.engine is None（只读检视路由用，直接只读打开库文件）
        # 不论 build 与否都计入该世界的租约数：恢复与关闭等它们归零
    def live_stats(self, world: str) -> dict | None: ...   # 开着的世界返回 {"holders","statements","last_write_at"}，关着返回 None
    def sync_default_settings(self) -> None: ...            # registry.update_settings(默认世界, config.world_settings())；
                                                            # 启动时与每次 POST /api/config 保存后调用
    def close_world(self, world: str, timeout_s: float = 30.0) -> None: ...   # 等该世界租约归零、停线程、丢引擎、写统计缓存
    def close_all(self, timeout_s: float = 30.0) -> None: ...                  # 关停时调用
    def open_worlds(self) -> list[str]: ...
    def context(self, world: str) -> WorldContext: ...
    def rollback(self, world: str, snapshot_id: str) -> dict: ...              # 只停这个世界
    def take_snapshot(self, world: str, note: str) -> dict: ...
    def delete_world(self, world: str) -> None: ...                            # 先 close_world
    def start_background(self) -> None: ...   # 每个开着的世界按自己的 settings.background 起 tick/摄入；起自动快照调度与 30 秒统计刷新线程
    def stop_background(self) -> None: ...
```

锁顺序（写进模块文档）：`pool._lock`（短，只保护 `_engines` / `_leases` / `_closing` 三张表）→ `pool._world_locks[name]`（每世界一把 RLock：建引擎、关引擎、快照、恢复）→ `engine._lock` → supervisor。持租约的代码不得拿 `_world_locks`。

- [ ] **Step 1: 写失败的测试** `tests/python/test_world_pool.py`

```python
def test_two_worlds_open_at_once_and_isolated(tmp_path): ...
    # 建 w1、w2；各自 lease → remember（stub llm）→ recall；互相召回不到；open_worlds() == 3（含 default）
def test_concurrent_first_lease_builds_one_engine(tmp_path, monkeypatch):    # Review Focus 1
    # monkeypatch DashboardEngine.__init__ 计数并 sleep 0.2；两个线程同时 lease("w1") → 构造次数 1，两份租约拿到同一个对象
def test_close_world_waits_for_in_flight_lease(tmp_path):                    # Review Focus 2
    # 线程 A 持 w1 租约 sleep 0.5；主线程 close_world("w1") 返回时间 ≥ A 释放时间；之后 lease("w1") 重新建引擎（新对象）
def test_lease_while_closing_returns_world_busy(tmp_path): ...
def test_rollback_only_stops_that_world(tmp_path):
    # w1 拍快照、写入；w2 线程持续 remember；rollback("w1") 成功；w2 的引擎对象不变、写入全部成功
def test_inherited_endpoint_changed_refuses(tmp_path):
    # 默认世界 provider "p" base_url a；建 w1（继承）；把 provider "p" 的 base_url 改成 b；lease("w1").engine.remember → WorldSwitchError/RuntimeError 码 endpoint_changed
def test_ingest_partitions_are_per_world(tmp_path):
    # default 的 spool 目录 == <data_dir>/ingest-spool/tenant-default；w1 的 == .../world-<dir_id>
def test_engine_counts_track_writes_and_cache_on_close(tmp_path):
    # w1 写 2 条 → live_stats("w1")["statements"] == 2 → close_world → live_stats 为 None，
    # registry.get("w1")["statements"] == 2 且 last_write_at 非空
def test_on_world_changed_is_debounced(tmp_path):
    # 1 秒内对 w1 连写 5 条 → on_world_changed 收到 1–2 次，最后一次的 statements == 5
def test_default_settings_follow_config_and_new_worlds_copy_them(tmp_path):
    # config 的 extraction model 改成 m2 → sync_default_settings → registry.get(default) 是 m2；
    # 之前建的 w1 仍是旧值，之后建的 w2 是 m2
```

- [ ] **Step 2: 运行，确认失败** — Run: `pytest tests/python/test_world_pool.py -q` — Expected: `ImportError: cannot import name 'WorldPool'`。
- [ ] **Step 3: 实现** `WorldPool` 与 `DashboardEngine(ctx, providers)`。`DashboardEngine` 内部所有原先读 `config.*` 的地方改读 `ctx.settings`（字段名对应 Task 3）；`ingest_spool` 用 `ctx.ingest_spool_dir`。旧的「切换窗口」「泄漏 Runtime 登记」「惰性构建补线程」逻辑按世界保留在 `WorldPool` 里（同一库文件上不能有两条写连接这一条仍成立）。
- [ ] **Step 4: 运行，确认通过** — Run: `pytest tests/python/test_world_pool.py tests/python/test_dashboard_engine.py -q` — Expected: PASS（`test_dashboard_engine.py` 按新构造签名改写）。
- [ ] **Step 5: 提交**

```bash
git add python/starling/dashboard/world_manager.py python/starling/dashboard/engine.py python/starling/dashboard/config.py tests/python/test_world_pool.py tests/python/test_dashboard_engine.py
git commit -m "feat(dashboard): 引擎池——多个世界同时开、按世界租约与关闭、恢复只停单个世界"
```

---

### Task 6: 按世界寻址的看板路由

**Files:**
- Modify: `python/starling/dashboard/app.py`（`create_app` 建 `WorldRegistry` + `WorldPool`；lifespan 调 `start_background`/`stop_background`/`close_all`；`/ws/converse` 首帧请求增加可选 `world`）
- Modify: `python/starling/dashboard/routes/inspect.py`、`routes/commands.py`、`routes/worlds.py`、`routes/config.py`
- Modify: `python/starling/dashboard/realtime.py`（广播消息增加 `world` 字段）
- Modify: `scripts/run_dashboard.py`（不再单独建引擎；`data_dir` = 配置文件所在目录）
- Test: `tests/python/test_dashboard_world_routes.py`（新）；改写 `test_dashboard_worlds.py`、`test_dashboard_inspect.py`、`test_dashboard_commands.py` 等依赖 `app.state.engine` / `app.state.config` 的用例

**Interfaces:**
- Consumes: Task 5 的 `WorldPool`、`EngineFactory`。
- Produces（Python）：`create_app(config: DashboardConfig, *, engine: object | None = None, engine_factory: EngineFactory | None = None) -> FastAPI`。`engine` 保留原意：作为默认世界的引擎预先放进池（`_require_engine_matches` 照旧校验）；`app.state.pool` 是 `WorldPool`；`app.state.engine`、`app.state.worlds` 删除。现有测试里 27 处 `app.state.engine = X`（3 个文件）改成 `create_app(cfg, engine=X)`；`POST /api/config` 保存后调 `pool.sync_default_settings()`。
- Produces（HTTP）：
  - 检视与命令路由同时挂在 `/api/...`（默认世界）与 `/api/w/{world}/...`。实现方式：路由函数收 `world: str` 依赖项，前缀路由从路径取，旧路由取 `pool.registry.default_world()`。
  - `GET /api/worlds?label.<key>=<value>&offset=&limit=` → `{"default": str, "total": int, "worlds": [row]}`；`row` 去掉 `active`，增加 `labels`、`open: bool`、`holders`、`last_write_at`。开着的世界的计数取 `pool.live_stats`，关着的取注册表缓存；列表不再逐个打开库（`db_bytes`、`live_bytes`、`engrams` 只在 `GET /api/worlds/{name}` 详情里给）。
  - `POST /api/worlds` body 增加 `labels: dict[str,str] = {}`、`settings: dict | None`；原 `auto_snapshot` 字段保留，映射到 `settings.background.auto_snapshot`。
  - `PUT /api/worlds/{name}/settings` body 为部分设置 → 新的完整设置（不含密钥，endpoint 只给 `key_set`）。
  - `POST /api/worlds/{name}/close` → `{"closed": name}`。
  - 删除：`POST /api/worlds/{name}/activate`。
  - WebSocket `/ws` 广播：`{"type": ..., "world": <name>, "payload": ...}`；`pool.on_world_changed` 触发时（去抖后）广播 `{"type": "world_changed", "world": w, "payload": {"statements": n, "last_write_at": t}}`。
  - `_STATUS` 增加 `world_busy: 503`、`labels_immutable: 409`、`endpoint_changed: 409`、`invalid_settings: 400`；去掉 `world_switching`。

- [ ] **Step 1: 写失败的测试** `tests/python/test_dashboard_world_routes.py`（`with TestClient(create_app(cfg, engine_factory=stub_factory))` 进 lifespan；`stub_factory(ctx, providers)` 建 `DashboardEngine` 后把 `engine.llm` 换成 `make_stub_llm(...)`，与现有 `test_dashboard_worlds.py:107` 的注入方式相同）

```python
def test_inspect_routes_by_world(client):
    # POST /api/worlds {name:"w1"}；POST /api/w/w1/remember；GET /api/w/w1/statements 有 1 条；GET /api/statements（默认世界）0 条
def test_legacy_paths_hit_default_world(client): ...
def test_unknown_world_404(client):
    # GET /api/w/nope/statements → 404 {"detail":"world_not_found"}
def test_list_worlds_filters_and_pages(client):
    # 建 3 个 labels {run_id:r1}、2 个 {run_id:r2}；GET /api/worlds?label.run_id=r1 → total 3；?limit=2&offset=0 → 2 条
def test_activate_route_removed(client):
    # POST /api/worlds/w1/activate → 404 或 405
def test_settings_update_and_no_secret_leak(client):
    # PUT /api/worlds/w1/settings {"extraction":{"model":"m2"}} → 返回里 extraction.model == m2，无 api_key 字段，有 key_set
def test_ws_broadcast_carries_world(client):
    # 连 /ws；POST /api/w/w1/remember → 收到 type world_changed、world "w1"
def test_close_world_route(client): ...
```

- [ ] **Step 2: 运行，确认失败** — Run: `pytest tests/python/test_dashboard_world_routes.py -q` — Expected: 404（路由不存在）。
- [ ] **Step 3: 实现** 路由与 `create_app`。`run_dashboard.py` 的启动形状：`cfg = DashboardConfig.load(args.config)`；`app = create_app(cfg)`；`uvicorn.run(app, ...)`（引擎由 lifespan 里的 `WorldPool` 建）。
- [ ] **Step 4: 跑全量 Python** — Run: `pytest tests/python -q -x` — Expected: 全绿。被删的用例（激活、切换窗口、`world_switching`）在提交信息里列出。
- [ ] **Step 5: 提交**

```bash
git add python/starling/dashboard/app.py python/starling/dashboard/realtime.py python/starling/dashboard/routes/inspect.py python/starling/dashboard/routes/commands.py python/starling/dashboard/routes/worlds.py python/starling/dashboard/routes/config.py scripts/run_dashboard.py tests/python/test_dashboard_world_routes.py <改写过的测试文件>
git commit -m "feat(dashboard): 看板 API 按世界寻址 /api/w/{world}；去掉激活；广播带世界名"
```

---

### Task 7: 看板前端按世界取数

**Files:**
- Create: `dashboard/web/src/lib/world.svelte.ts`（当前页面选中的世界：读写 URL 查询参数 `world`，缺省为注册表的默认世界）
- Modify: `dashboard/web/src/lib/api.ts`（`worldPath(p)`：`/api/x` → `/api/w/<world>/x`，世界是默认世界时保持 `/api/x`）
- Modify: 所有调 `/api/...` 检视与命令路由的页面（`routes/*/+page.svelte`，约 20 个，见 `grep -rn "'/api/" src/routes`）改用 `worldPath`
- Modify: `dashboard/web/src/routes/+layout.svelte`（顶栏世界选择器）
- Modify: `dashboard/web/src/lib/worlds.ts`（去掉 `activate`、`WORLD_NAME_RE` 换新规则、`world_switching` 文案换成 `world_busy`）、`lib/ws.ts`、`lib/feed.ts`（`world_switched` 去掉；按 `world` 字段过滤；`world_changed` 触发当前页 `refetch`）
- Modify: `dashboard/web/src/routes/worlds/+page.svelte`、`lib/components/WorldRollbackDrawer.svelte`、`routes/sessions/+page.svelte`（去掉「当前」徽标与激活按钮；列表加标签列与按标签过滤、分页）
- Test: `dashboard/web/src/lib/world.test.ts`、`lib/api.test.ts`、`lib/worlds.test.ts`、`lib/feed.test.ts`

**Interfaces:**
- Consumes: Task 6 的 HTTP 形状。
- Produces：

```ts
// world.svelte.ts
export function currentWorld(): string;              // URL ?world=，没有则 defaultWorld()
export function setWorld(name: string): void;        // 改 URL 查询参数（goto，replaceState），不发任何请求
export function defaultWorld(): string;              // 来自 GET /api/worlds 的 default，启动时取一次
// api.ts
export function worldPath(path: string, world?: string): string;
```

- [ ] **Step 1: 写失败的测试**

```ts
// api.test.ts
expect(worldPath('/api/statements', 'default')).toBe('/api/statements');   // defaultWorld() 返回 'default' 时
expect(worldPath('/api/statements?q=1', 'Run-A')).toBe('/api/w/Run-A/statements?q=1');
expect(worldPath('/api/statements', 'a b')).toBe('/api/w/a%20b/statements');
expect(worldPath('/api/worlds', 'Run-A')).toBe('/api/worlds');             // 管理路由不加前缀
// worlds.test.ts
expect(isValidWorldName('Run-A.1')).toBe(true); expect(isValidWorldName('.x')).toBe(false);
expect(isValidWorldName('a'.repeat(129))).toBe(false);
// feed.test.ts：world_changed 只对当前世界触发 refetch；别的世界的消息忽略
```

- [ ] **Step 2: 运行，确认失败** — Run: `cd dashboard/web && npx vitest run src/lib/api.test.ts src/lib/worlds.test.ts src/lib/feed.test.ts src/lib/world.test.ts` — Expected: FAIL。
- [ ] **Step 3: 实现**；管理路由前缀白名单：`/api/worlds`、`/api/config`、`/api/eval`、`/health`、`/api/ping`。
- [ ] **Step 4: 前端三门** — Run: `npm run check && npx vitest run && npm run build` — Expected: 全绿。
- [ ] **Step 5: 提交**

```bash
git add dashboard/web/src/lib/world.svelte.ts dashboard/web/src/lib/api.ts dashboard/web/src/lib/worlds.ts dashboard/web/src/lib/ws.ts dashboard/web/src/lib/feed.ts dashboard/web/src/routes <具体改过的 +page.svelte 与组件> dashboard/web/src/lib/*.test.ts
git commit -m "feat(web): 顶栏选世界、所有页面按世界取数、去掉激活；世界列表按标签过滤与分页"
```

---

### Task 8: 端到端验证与 8787 迁移演练

**Files:**
- Create: `tests/python/test_multi_world_e2e.py`
- Modify: `docs/superpowers/specs/2026-10-25-world-scene-management-design.md`（文首状态行加一句：§8.1 被 `2026-11-03-memory-service-design.md` 取代）

- [ ] **Step 1: 写测试** `tests/python/test_multi_world_e2e.py`

```python
def test_three_worlds_isolated_through_http(tmp_path):        # 需求 §7 第 1 条的看板版
    # create_app 进 lifespan；建 w1 w2；三个世界各 POST remember（stub）；各自 recall 只见自己的
def test_rollback_one_world_while_another_writes(tmp_path):     # 需求 §7 第 1b 条的看板版
def test_restart_reads_imported_registry(tmp_path):
    # 先用 Task 2 之前格式造 worlds.json（default + sim-1 带快照）放进 data_dir；create_app → 两个世界都在、快照都在；
    # 关停再起，registry.db 复用，不再导入
```

- [ ] **Step 2: 运行** — Run: `pytest tests/python/test_multi_world_e2e.py -q` — Expected: PASS。
- [ ] **Step 3: 迁移演练（不碰真实数据）**

```bash
COPY=$CLAUDE_JOB_DIR/tmp/starling-copy && mkdir -p $COPY
cp ~/.starling/starling.json ~/.starling/worlds.json $COPY/
cp -cR ~/.starling/worlds $COPY/worlds                 # APFS 克隆，不占额外空间（真实快照约 660 MB）
sqlite3 ~/.starling/dashboard.db ".backup '$COPY/dashboard.db'"
# worlds.json 里的绝对路径（db_path、snapshot_dir、每份快照的 path）全部从 ~/.starling 改指 $COPY；
# 副本 starling.json：db_path=$COPY/dashboard.db、port=8797、ingest_spool_path=$COPY/ingest-spool（防止读走真实摄入目录）
env -u OPENAI_API_KEY -u OPENAI_BASE_URL -u OPENAI_MODEL python scripts/run_dashboard.py --config $COPY/starling.json &
curl -s -H "Authorization: Bearer <副本里的 token>" http://127.0.0.1:8797/api/worlds | python -m json.tool | head -30
curl -s -H "Authorization: Bearer <副本里的 token>" http://127.0.0.1:8797/api/worlds/default/snapshots | python -m json.tool | head
```
Expected: `default` 在列表里，语句数等于真实库的语句数；5 份快照都在；`$COPY/worlds.json` 已改名为 `worlds.json.imported-*`；`~/.starling` 下的文件 mtime 不变。演练完停掉进程、删掉 `$COPY`。

真实 8787 的部署（重启 launchd 服务）不在本计划里：合并之后由用户决定何时重启，按记忆里「部署走用户 `!` 脚本」的方式做。
- [ ] **Step 4: 全量提交门** — Run: `python scripts/configure_build.py --build --test` 之后 `pytest tests/python -q`，`cd dashboard/web && npm run check && npx vitest run && npm run build`，`python scripts/ci_clang_tidy_diff.py`（按脚本用法给 base）。Expected: 全绿。
- [ ] **Step 5: 提交**

```bash
git add tests/python/test_multi_world_e2e.py docs/superpowers/specs/2026-10-25-world-scene-management-design.md
git commit -m "test(world): 多世界端到端——互不可见、单世界回滚不影响别的世界、worlds.json 导入后重启"
```

---

## 自查记录

- **spec 覆盖（第 1 期）**：§5 注册表 → Task 1、2；§5.3 世界名与目录 → Task 1；§5.4 导入 → Task 2、8；§5.5 统计缓存 → Task 1（存储）、Task 5（刷新与关闭时写）；§7.2「只停一个世界」→ Task 5；§8 设置、密钥、摄入分区 → Task 3、5、6；§10.3 引擎池 → Task 5；§13.1 按世界寻址 → Task 6；§14 看板 → Task 7（实时推送的提交后广播 → Task 6）；标签不可变 → Task 1、6。
- **不在本期**：迁移 0039 与多 holder（第 2 期）、分叉与只读（第 3 期）、`starling-serve` 与客户端与句柄（第 4 期）、自动清理（第 6 期）、看板的占用与只读提示（依赖第 3、4 期，放第 5 期）。
- **名字一致性**：`WorldRegistry`（Python）包 `RegistryDb`（C++）；`WorldPool.lease(world)`；`WorldContext`；`worldPath`。
