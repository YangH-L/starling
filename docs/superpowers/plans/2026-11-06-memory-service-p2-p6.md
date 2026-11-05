# 记忆服务第 2–6 期实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把第 1 期的多世界看板服务做成 rise 能用的记忆服务。具体是五件事：一个世界多个 holder；分叉、种子与只读；`starling-serve` 加客户端会话与 `starling-client` 包；看板补齐状态提示；自动清理。

**Architecture:** 规则与状态机全在 C++（D12）。Python 只做 HTTP 转发、引擎建停和传入当前时间。客户端只做 HTTP 转发和异常映射。按 spec §17 分期，每期一个 PR，各自合并到 main；能并行的期放在不同 worktree 里同时做（见「执行编排」）。

**Tech Stack:** C++20 + SQLite + pybind11（`_core`）；Python 3.14 + FastAPI（看板服务）；`httpx`（客户端）；SvelteKit + Svelte 5 + vitest（前端）。

**Spec:** `docs/superpowers/specs/2026-11-03-memory-service-design.md`（§4、§6、§7、§9–§13、§16、§17）。第 1 期计划 `docs/superpowers/plans/2026-11-03-memory-service-p1-multi-world.md` 已执行完（PR #121，0447a86）。

## Global Constraints

- 核心逻辑放 C++：注册表、标签、设置合并、分叉、快照、只读、事件、句柄与租约状态机、空闲判定、清理规则、错误码目录（D12）。判断标准是换一种绑定语言要不要重写这段逻辑（`CLAUDE.md`「架构边界」）。
- 新增和改动的绑定一律释放 GIL；`memory_digest` 改为释放（§12）。
- 异常文字、日志、HTTP 响应里不出现密钥和端点地址（§2 反目标、§13.1）。
- 错误响应统一为 `{"code": "...", "transient": bool, "detail": "..."}`。`code` 与 `transient` 来自 C++ 错误码目录（§13.1，第 4 期起）。
- 世界名：首字符是字母或数字，其余 `[A-Za-z0-9._-]`，长度 1–128，区分大小写（§19）。
- 自动清理默认值：每组保留 200 份、14 天，每 10 分钟跑一次（§9）。
- 时长默认值：空闲关闭 10 分钟、心跳 10 秒、租约 30 秒、统计缓存 30 秒；都做成服务配置项（§18）。
- C++ 不读墙钟：会话、空闲、清理都由调用方传 `now`（§11）。
- 服务端不内置桩模式；测试用子进程里的本机 OpenAI 兼容假端点（§2 非目标、§16）。
- 客户端包 `clients/python/`，包名 `starling-client`，导入名 `starling_client`，只依赖标准库和 `httpx`（§13.3）。
- 测试不得读写 `~/.starling`、不得连 8787、不得联网：沿用 `tests/python/conftest.py` 的守卫（第 1 期 44c3099、2d3ec57）。
- 提交门：全量 ctest 加 `python -m pytest tests/python` 全绿；前端 `npm run check`、`npx vitest run`、`npm run build`；clang-tidy 差异门 `scripts/ci_clang_tidy_diff.py`；客户端包另跑自己的 pytest（§16）。
- 用 `python -m pytest`，不用裸 `pytest`（裸 pytest 的 sys.path 缺仓库根，`tests/python/test_configure_build.py` 收集失败）。
- clang-tidy：改动行上的名字至少 3 个字符（`readability-identifier-length`）。
- 提交信息用中文，按仓库现有风格，结尾 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`。`git add` 只写明确路径；不用 `--no-verify`、不用 `--amend`。

## Review Focus

1. **同一轮发言写进两个 holder**（同 key、同文本）：第二个 holder 必须得到自己的语句，`observed_at` 取自己事件的时刻，不能静默拿到第一个 holder 的语句 id（§2 反目标第 6 条）。由 Task 2.1、2.2 的测试钉住。
2. **旧库升级到 0039 之后的重入**：旧格式库里 pending、committed、abandoned 的事件，被第一个 holder 碰到时认领，重入、冲突、放弃的行为与升级前一致。由 Task 2.1 的测试钉住。
3. **看板点开一个可回收或只读世界**：不跑迁移，文件 sha256 不变（复盘副本不能因为升级 Starling 后点开一次就被改字节）。由 Task 3.2、3.3 的测试钉住。
4. **会话租约过期期间别的会话占了世界**：原会话下一次调用得到 `session_expired`，客户端不自动重开句柄；新会话可写打开成功。由 Task 4.2、4.5 的测试钉住。
5. **清理与正在打开的世界赛跑**：清理选中一个世界的同时它被 `open`，要么清理跳过（记录原因），要么 `open` 得到 `world_not_found`，不能删掉一个刚开出引擎的世界的文件。由 Task 6.1、6.2 的测试钉住。

---

## 执行编排

依赖关系：

```
P2 (holders) ─┐
              ├──► P4 (service + client) ──► P5 (dashboard) ──► P6 (cleanup)
P3 (fork/RO) ─┘
```

- **第 2 期与第 3 期并行**，两个 worktree 同时做。P2 改 `memory_events`、迁移 0039、门面；P3 改 `world_store` 分叉、`registry_db` 的 seed、read_only、fork 字段、只读打开路径和 `held` 快照。两期都会碰的文件只有三个：`bindings/python/bind_13_memory_ops.cpp`（P2）与 `bind_15_world.cpp`（P3）分开；`python/starling/dashboard/engine.py` 两期各改不同的方法；`CMakeLists.txt` 两期都可能加测试源。后合并的那一期负责 rebase 并解决冲突。
- **第 4 期**在 P2、P3 都合并之后开始。期内 Task 4.1（错误码目录，C++）与 Task 4.4（客户端包骨架与传输层，纯 Python）可以并行；4.4 只依赖 4.1 定下的码表，码表在本计划里写死。
- **第 5 期**在 P4 合并之后开始，纯前端，在不带 venv 的普通 worktree 里做（记忆「dashboard frontend is standalone npm」）。
- **第 6 期**的 C++ 部分（Task 6.1）只依赖 P3 的 `reclaimable`、`retention_group` 与 `held` 快照，所以可以在 P3 合并后与 P4 并行。Python 调度（Task 6.2）要等 P4 的空闲判定与句柄计数。
- 每期结束时做整支审查和全量提交门，然后开 PR、合并。部署由用户决定，每期合并后不自动部署。

### Worktree 配方（每期一个）

```bash
cd /Users/jaredguo-mini/develop/memory/starling
git worktree add ../starling-wt-p<N> -b feat/memory-service-p<N> origin/main
cd ../starling-wt-p<N>
python3.14 -m venv .venv && source .venv/bin/activate
pip install -q cmake==4.3.2 ninja scikit-build-core pybind11
pip install -e ".[dev,dashboard]" --no-build-isolation --config-settings=build-dir=build
cd dashboard/web && npm ci && cd -
```

- 改 C++、绑定、迁移后刷新 `_core`：`cmake --build build && cmake --install build --prefix .venv/lib/python3.14/site-packages`。
- C++ 测试：`ctest --test-dir build -R <名字> --output-on-failure`。gtest 的名字是驼峰，过滤用 `[Ww]orld|[Ee]vent` 这样的写法。
- 并行的两个 worktree 各自编译会争 CPU（10 核）：每个 worktree 的 `cmake --build` 加 `-j 5`。

---

## 第 2 期：一个世界多个 holder

### Task 2.1: 迁移 0039 与按 holder 的事件状态机（C++）

**Files:**
- Create: `migrations/0039_events_holder.sql`
- Modify: `include/starling/memory/memory_events.hpp`, `src/memory/memory_events.cpp`
- Test: `tests/cpp/test_memory_events.cpp`（已有，追加用例）

**Interfaces:**
- Produces:
  - `struct PendingEventRow { std::string key, text, interlocutor; std::int64_t seq; std::string failure_category; std::string holder; };`（新增 `holder`，放最后）
  - `remember_event(...)` 签名不变：holder 取 `base.holder_id`。`abandon_event(adapter, tenant, holder, key)` 多一个 `std::string_view holder` 参数，放在 `key` 前面。
  - `pending_events(conn, tenant)` 返回全世界的 pending 行（所有 holder），按 `seq` 升序。
  - `next_seq` 不变（世界一级）。

- [ ] **Step 1: 写迁移的失败测试** `TEST(MemoryEvents0039, LegacyRowsBackfillHolderFromStatements)`：在一份只跑到 0038 的库上插入三行事件，然后跑 0039。三行分别是：committed 且 `statement_ids_json` 指向 holder `alice` 的语句；pending 且没有语句；abandoned。断言回填后 holder 依次是 `alice`、`''`、`''`，主键变为 `(tenant_id, holder_id, key)`，`UNIQUE(tenant_id, seq)` 仍在（插同一个 seq 抛约束错误）。
- [ ] **Step 2: 写认领规则的失败测试**
  - `TEST(MemoryEvents0039, FirstHolderClaimsLegacyRow)`：一条 `('', K)` 的 pending 旧行，用 holder `bob` 调 `remember_event(K, 同文本)`，行的 holder 变成 `bob` 并按补齐分支收成 committed。之后 holder `carol` 写 K 是一条全新的事件（新 seq），不是重入。
  - 同理测 `abandon_event(bob, K)` 认领一条 `('', K)` 行。
  - 测 abandoned 旧行被认领后，`remember_event` 抛 `EventConflict`。
- [ ] **Step 3: 写同 key 两个 holder 的失败测试** `TEST(MemoryEventsHolder, SameKeySameTextGivesEachHolderOwnStatements)`：holder A 与 B 先后 `remember_event("ep1#3", "B：你好")`，两者都是 `outcome="accepted"`，语句 id 不相交，各自的语句 `holder_id` 等于自己；`pending_events` 在中途失败时列出两行，`holder` 字段分别是 A、B。
- [ ] **Step 4: 运行，确认失败** — Run: `cmake --build build -j 5 && ctest --test-dir build -R MemoryEvents --output-on-failure` — Expected: 新用例 FAIL（没有 0039、没有 holder 列）。
- [ ] **Step 5: 写 `migrations/0039_events_holder.sql`**。在一个事务里建新表 `events_new`，主键改为 `(tenant_id, holder_id, key)`，其余列和 `UNIQUE (tenant_id, seq)` 照旧；按回填规则从旧表复制（`json_each(statement_ids_json)` 取第一条语句的 `holder_id`，没有就是 `''`）；删旧表、改名；重建 `idx_events_state`，并加 `idx_events_holder_key ON events(tenant_id, holder_id, key)`。
- [ ] **Step 6: 在 `memory_events.cpp` 实现按 holder 读写和认领**。第 ① 段按 `(tenant, holder, key)` 读，读不到再读 `(tenant, '', key)`，读到就在同一个短事务里 `UPDATE holder_id=holder`，然后按原状态机走。`abandon_event` 同理。
- [ ] **Step 7: 运行，确认通过** — Run: 同 Step 4 — Expected: PASS，`MemoryEvents*` 既有用例也全绿。
- [ ] **Step 8: 提交** — `git add migrations/0039_events_holder.sql include/starling/memory/memory_events.hpp src/memory/memory_events.cpp tests/cpp/test_memory_events.cpp && git commit -m "feat(memory): 事件按 holder 区分（迁移 0039 与认领规则）"`

### Task 2.2: 原文按 holder 分开（C++）

**Files:**
- Modify: `src/memory/memory_events.cpp`（第 ② 段的 `params.source_prefix` 和补齐分支）
- Test: `tests/cpp/test_memory_events.cpp`

**Interfaces:**
- Consumes: Task 2.1 的按 holder 状态机。
- Produces: `remember_event` 生成 engram 时，`source_item_id` 前缀带上 holder，即 `base.source_prefix + "h:" + holder + ":"`。同一段话写进两个 holder 名下得到两个 engram。

- [ ] **Step 1: 写失败测试** `TEST(MemoryEventsHolder, EachHolderGetsOwnEngramAndObservedAt)`：A 用 seq 0 写 `("ep1#3","B：你好")`，再写一条无关事件（seq 1），之后 B 写同一 key 同文本（seq 2）。断言：
  - A、B 的语句 `evidence_json` 指向两个不同的 engram；
  - A 的语句 `observed_at == epoch+0s`，B 的 `== epoch+2s`；
  - 两个 engram 的 `content_hash` 相同（摘要按 content_hash 引用，不受影响）。
- [ ] **Step 2: 写补齐分支的失败测试** `TEST(MemoryEventsHolder, RetryCompletesFromOwnEngramNotOtherHolders)`：A 先成功写 K；B 写 K 时在第 ③ 段之前模拟崩溃（行仍是 pending，语句已落库），然后重试 B。断言 B 补齐拿到的是 B 自己 engram 上的语句，不是 A 的；全程 LLM 只被调用两次（A 一次、B 一次）。
- [ ] **Step 3: 运行，确认失败** — Run: `ctest --test-dir build -R MemoryEventsHolder --output-on-failure` — Expected: FAIL（两人共用一个 engram）。
- [ ] **Step 4: 实现**：前缀带 holder；补齐分支按 `source_item_id`（来源身份）找 engram，不再按 `content_hash`。
- [ ] **Step 5: 运行，确认通过**，再跑一遍 `MemoryDigest*` 确认摘要用例不变 — Expected: PASS。
- [ ] **Step 6: 提交** — `git commit -m "fix(memory): remember_event 的原文按 holder 分开，各取各的事件时刻"`（只 add 本任务改动的路径）

### Task 2.3: 绑定、门面与 GIL（Python）

**Files:**
- Modify: `bindings/python/bind_13_memory_ops.cpp`（`memory_pending_events`、`memory_abandon_event`、`memory_digest`、`memory_next_seq`）
- Modify: `python/starling/_memory_core.py`（`abandon_event` 传 holder）、`python/starling/memory.py`（`PendingEvent` 加 `holder: str = ""`，放最后，带默认值以保持位置构造兼容）
- Test: `tests/python/test_memory_events.py`（已有，追加）、`tests/python/test_gil_release.py`（追加）

**Interfaces:**
- Consumes: Task 2.1 的 `abandon_event(adapter, tenant, holder, key)`、`PendingEventRow::holder`。
- Produces: `_core.memory_abandon_event(adapter, tenant, holder, key)`；`_core.memory_pending_events(...)` 每项多 `holder` 键；`Memory.pending_events()` 返回的 `PendingEvent` 带 `holder`。门面 `Memory` 的 holder 就是 `open(..., agent=...)` 的 `agent`。

- [ ] **Step 1: 写失败测试** `test_two_holders_share_a_world_each_sees_own_statements`（`tests/python/test_memory_events.py`）：同一个库文件，两个 `MemoryCore`（agent `A`、`B`，同一 tenant，同一个 Runtime），交替写 3 条事件，其中一条同 key 同文本。断言：
  - A 的 `recall` 只返回 `holder_id=="A"` 的行，B 同理；
  - `digest()` 在两人都写过之后与只有 A 写时不同；
  - 中途失败时 `pending_events()` 每项有 `holder`。
- [ ] **Step 2: 写 GIL 失败测试** `test_memory_digest_releases_gil`（`tests/python/test_gil_release.py`，沿用该文件的写法）：在一个约 2000 条语句的库上跑 `digest()` 的同时，另一线程在 200 ms 内完成一次纯 Python 计数循环。另外两个用例对 `pending_events` 与 `abandon_event` 做同样检查。
- [ ] **Step 3: 运行，确认失败** — Run: `cmake --build build -j 5 && cmake --install build --prefix .venv/lib/python3.14/site-packages && python -m pytest tests/python/test_memory_events.py tests/python/test_gil_release.py -q` — Expected: FAIL。
- [ ] **Step 4: 实现绑定**：四个绑定在调用 C++ 前用 `py::gil_scoped_release`，在 release 之外把结果转成 Python 对象。`memory_abandon_event` 继续把 `std::out_of_range` 改译成 `KeyError`。
- [ ] **Step 5: 实现门面与引擎转发**：`MemoryCore.abandon_event(key)` 传 `self.agent`；`PendingEvent(**row)` 自动带上 `holder`。
- [ ] **Step 6: 运行，确认通过** — Run: 同 Step 3，再跑 `python -m pytest tests/python -q -k "memory or event or digest or gil"` — Expected: PASS。
- [ ] **Step 7: 提交** — `git commit -m "feat(memory): pending_events 带 holder，abandon_event 按 holder，事件与摘要绑定释放 GIL"`

### Task 2.4: 第 2 期验收用例（§16 第 1a 条）

**Files:**
- Create: `tests/python/test_acceptance_1a_two_holders.py`

**Interfaces:**
- Consumes: Task 2.3 的门面。

- [ ] **Step 1: 写验收测试** `test_1a_two_holders_in_one_world`：一个世界、两个 holder A 和 B，交替写 6 条（其中两条同 key 同文本），全部走 `make_stub_llm` 并按 holder 给出不同的确定性抽取。断言：
  - A 的 recall 只含 A，B 只含 B；
  - 两人各自拿到自己的语句，`observed_at` 各取各的事件时刻；
  - `digest()` 反映两人的写入：去掉 B 的写入后重建同一个库，摘要不同；
  - `pending_events` 列出两人的事件，带 `holder`。
- [ ] **Step 2: 运行** — Run: `python -m pytest tests/python/test_acceptance_1a_two_holders.py -v` — Expected: PASS（Task 2.1–2.3 之后）。
- [ ] **Step 3: 提交** — `git commit -m "test(memory): 验收 1a——一个世界两个 holder"`

---

## 第 3 期：分叉、种子与只读

### Task 3.1: 只读打开路径（C++）

**Files:**
- Modify: `include/starling/persistence/connection.hpp`, `src/persistence/connection.cpp`（`Connection::open_read_only(path)`：`mode=ro` URI，不设 `journal_mode`，不建目录）
- Modify: `include/starling/persistence/sqlite_adapter.hpp`, `src/persistence/sqlite_adapter.cpp`（`SqliteAdapter::open_read_only(path)`：不跑迁移，`write_admitted()` 恒为 false；`bool read_only() const noexcept`；`int schema_version() const` 读 `schema_migrations` 的 `MAX(version)`）
- Modify: `include/starling/persistence/migration_runner.hpp`, `src/persistence/migration_runner.cpp`（`static int latest_embedded_version()`）
- Modify: `bindings/python/bind_02_persistence.cpp`
- Test: `tests/cpp/test_sqlite_adapter_read_only.cpp`（新建，加进 `CMakeLists.txt` 的测试源）

**Interfaces:**
- Produces:
  - `static std::unique_ptr<SqliteAdapter> SqliteAdapter::open_read_only(const std::filesystem::path&)`；
  - `bool SqliteAdapter::read_only() const noexcept`；
  - `int SqliteAdapter::schema_version() const`；
  - `static int MigrationRunner::latest_embedded_version()`；
  - 绑定 `_core.SqliteAdapter.open_read_only(path)`、`.read_only`、`.schema_version()`、`_core.latest_schema_version()`。
  - 所有写入入口在只读适配器上抛 `governance::WriteGateRejected`，由引擎层译成 `world_read_only`（Task 3.3）。

- [ ] **Step 1: 写失败测试**
  - `TEST(SqliteAdapterReadOnly, OpenDoesNotMigrateOrChangeBytes)`：用旧代码建一个库，手工删掉最后一条 `schema_migrations` 记录来模拟 schema 落后；记下 sha256 后 `open_read_only`。断言 `schema_version() < latest_embedded_version()`，关闭后 sha256 不变，目录里没有新出现的 `-wal`、`-shm`。
  - `TEST(SqliteAdapterReadOnly, WritesAreRejected)`：在只读适配器上调用 `remember` 的写入口，断言抛 `WriteGateRejected`，文件不变。
- [ ] **Step 2: 运行，确认失败** — Run: `cmake --build build -j 5 && ctest --test-dir build -R SqliteAdapterReadOnly --output-on-failure` — Expected: 编译失败（没有 `open_read_only`）。
- [ ] **Step 3: 实现**（三个文件）。只读连接用 `sqlite3_open_v2(uri, SQLITE_OPEN_READONLY | SQLITE_OPEN_URI)`，uri 写成 `file:<path>?mode=ro`。适配器的写准入钩子固定返回拒绝。
- [ ] **Step 4: 运行，确认通过** — Expected: PASS。
- [ ] **Step 5: 提交** — `git commit -m "feat(persistence): 只读打开路径——不迁移、不改日志模式、拒绝写入"`

### Task 3.2: 分叉、种子、升级种子与 `held` 快照（C++）

**Files:**
- Modify: `include/starling/governance/world_store.hpp`, `src/governance/world_store.cpp`
- Modify: `include/starling/governance/registry_db.hpp`, `src/governance/registry_db.cpp`（`create` 接受 seed/read_only/reclaimable/retention_group/fork 字段；`kSchemaVersion` 不变，列已存在）
- Modify: `bindings/python/bind_15_world.cpp`
- Test: `tests/cpp/test_world_fork.cpp`（新建）、`tests/cpp/test_world_store.cpp`（`held` 用例追加）

**Interfaces:**
- Consumes: Task 3.1 的 `open_read_only`、`schema_version`、`latest_embedded_version`；`memoryops::digest(conn, tenant)`。
- Produces:
  ```cpp
  struct ForkOptions {
      std::string snapshot_id;                       // 空 = 以世界当前状态为来源
      std::map<std::string, std::string> labels;
      std::string retention_group;
      bool reclaimable = false, seed = false;
      std::string settings_overrides_json;           // 空 = 复制来源的设置
      std::string now_iso;                           // 调用方给的时刻
  };
  struct ForkResult { WorldRecord world; std::string digest; };
  ForkResult fork_world(RegistryDb& reg, const std::string& source,
                        const std::string& new_name, const ForkOptions& opts);
  ForkResult upgrade_seed(RegistryDb& reg, const std::string& source,
                          const std::string& new_name, const std::string& now_iso);
  ```
  - `SnapshotOptions::kind` 接受 `"held"`；`apply_retention` 跳过 `held`；`SnapshotEntry` 多一个 `std::string digest`；`take_snapshot` 填上这个值。
  - `rollback_world` 的结果 `RollbackResult` 多一个 `std::string digest`：恢复后算出的摘要必须等于快照条目的 digest（条目带 digest 时），不等抛 `WorldStoreError("restore_digest_mismatch")`。
  - 新错误码：`world_schema_behind`、`fork_digest_mismatch`、`restore_digest_mismatch`、`world_read_only`（用 `WorldStoreError`）。
  - `seed=true` 蕴含 `read_only=true`。
  - 绑定：`_core.world_fork(reg, source, new_name, opts_dict)`、`_core.world_upgrade_seed(reg, source, new_name, now)`，都释放 GIL，返回 `{world: <记录 dict>, digest}`。

- [ ] **Step 1: 写失败测试**（`tests/cpp/test_world_fork.cpp`）：
  - `ForkCopiesContentAndDigestMatches`：来源写 3 条语句后分叉。新世界的摘要等于来源，来源文件 sha256 不变，新世界的 `fork_of_world` 等于来源。
  - `ForkFromSnapshot`：以一份快照为来源分叉，新世界的摘要等于快照条目的 digest，`fork_of_snapshot` 已登记。
  - `ForkRefusesSchemaBehindSource`：来源 schema 落后时抛 `world_schema_behind`，不留目录，也不留注册项。
  - `ForkDigestMismatchLeavesNothing`：用测试钩子在 partial 上篡改一行，抛 `fork_digest_mismatch`，不留文件与注册项。
  - `ForkNameTakenIsWorldExists`。
  - `SeedImpliesReadOnly`：`seed=true` 时记录里 `read_only` 为 true。
  - `UpgradeSeedMigratesCopyAndKeepsOld`：旧种子 schema 落后，`upgrade_seed` 得到的新种子 schema 为最新，旧种子文件 sha256 不变。
  - `HeldSnapshotsSurviveRetention`（`test_world_store.cpp`）：保留 N=2，拍 3 份 auto 和 1 份 held，修剪后 held 还在。
  - `RollbackDigestMustMatch`：把快照文件改掉一行后恢复，抛 `restore_digest_mismatch`。
- [ ] **Step 2: 运行，确认失败** — Run: `cmake --build build -j 5 && ctest --test-dir build -R "WorldFork|WorldStore" --output-on-failure` — Expected: FAIL 或编译失败。
- [ ] **Step 3: 实现 `fork_world`**，按 spec §6.1 的步骤：
  1. 校验新名；
  2. 只读打开来源并检查 schema；
  3. 用只读连接 `VACUUM INTO` 到新目录的 `world.db.partial`；
  4. `integrity_check`；
  5. 在 partial 上算摘要，与来源比较；
  6. 改名为 `world.db`；
  7. 登记注册表。

  任一步失败都删掉新目录、不登记。分叉不持注册表锁，只在第 7 步短暂写注册表。
- [ ] **Step 4: 实现 `upgrade_seed`**：复制 → 在副本上 `SqliteAdapter::open`（会跑迁移）→ 关闭 → 登记为 `seed=true` 的新世界。
- [ ] **Step 5: 实现 `held` 与快照、恢复的 digest**。
- [ ] **Step 6: 运行，确认通过** — Expected: PASS；`WorldStore*` 既有用例全绿。
- [ ] **Step 7: 绑定，并在 `tests/python/test_world_registry_bindings.py` 加 `test_fork_binding_releases_gil_and_returns_digest`** — Run: `cmake --install ... && python -m pytest tests/python/test_world_registry_bindings.py -q` — Expected: PASS。
- [ ] **Step 8: 提交** — `git commit -m "feat(world): 分叉、种子、升级种子、held 快照，快照与恢复带摘要"`

### Task 3.3: 打开方式判定、只读引擎与 HTTP 路由（C++ + Python）

**Files:**
- Modify: `include/starling/governance/registry_db.hpp`, `src/governance/registry_db.cpp`（`OpenMode decide_open_mode(const WorldRecord&, bool for_client_write)`）
- Modify: `bindings/python/bind_15_world.cpp`
- Modify: `python/starling/dashboard/world_manager.py`（`WorldPool._build` 按判定选 `open` 或 `open_read_only`，只读世界不起后台线程）
- Modify: `python/starling/dashboard/engine.py`（只读引擎上写入类方法抛 `WorldStoreError("world_read_only")`）
- Modify: `python/starling/dashboard/routes/worlds.py`（`POST /api/worlds/{name}/fork`、`POST /api/worlds/{name}/upgrade_seed`；列表与详情多 `seed`、`read_only`、`reclaimable`、`retention_group`、`fork_of_world`、`fork_of_snapshot`、`schema_behind`）
- Modify: `python/starling/dashboard/routes/world_scope.py`（`_STATUS` 加 `world_read_only: 409`、`world_schema_behind: 409`、`fork_digest_mismatch: 500`、`restore_digest_mismatch: 500`）
- Test: `tests/cpp/test_registry_db.cpp`（追加）、`tests/python/test_dashboard_world_fork.py`（新建）

**Interfaces:**
- Consumes: Task 3.1、3.2。
- Produces:
  - `enum class OpenMode { ReadWrite, ReadOnly };`。规则（spec §6.4）：`read_only || reclaimable` → ReadOnly；否则 ReadWrite。第 4 期给它加「客户端占用」这一维。
  - 绑定 `_core.world_decide_open_mode(record_dict) -> "rw" | "ro"`。
  - HTTP：`POST /api/worlds/{name}/fork` 的 body 是 `{new_name, snapshot_id?, labels?, retention_group?, reclaimable?, seed?, settings?}`，返回 `{world, digest}`；`POST /api/worlds/{name}/upgrade_seed` 的 body 是 `{new_name}`。

- [ ] **Step 1: 写 C++ 失败测试** `TEST(RegistryDb, OpenModeRule)`：四种组合（seed、read_only、reclaimable、普通）各给出预期的模式。
- [ ] **Step 2: 写 Python 失败测试**（`tests/python/test_dashboard_world_fork.py`，用 `TestClient`）：
  - `test_fork_through_http_digest_matches`；
  - `test_reclaimable_world_opens_read_only_and_bytes_unchanged`：分叉一个 `reclaimable` 世界，记 sha256，读 `/api/w/{name}/statements` 与 `/recall`，sha256 不变，目录里没有新的 `-wal`；
  - `test_write_to_read_only_world_is_409_world_read_only`；
  - `test_seed_shows_flags_in_list`；
  - `test_schema_behind_world_opens_and_is_flagged`。
- [ ] **Step 3: 运行，确认失败**。
- [ ] **Step 4: 实现判定、只读引擎与路由**。只读引擎：`DashboardEngine` 构造时收到 `read_only=True`，就不建写路径（不起 tick、摄入、自动快照），写入类方法一律抛 `world_read_only`。
- [ ] **Step 5: 运行，确认通过**，再跑 `python -m pytest tests/python -q -k "world or dashboard"` — Expected: PASS。
- [ ] **Step 6: 提交** — `git commit -m "feat(world): 看板按世界类型只读打开，分叉与升级种子的 HTTP 路由"`

### Task 3.4: 第 3 期验收用例（§16 第 2、3 条与只读）

**Files:**
- Create: `tests/python/test_acceptance_2_3_fork_restore.py`

- [ ] **Step 1: 写验收测试**：
  - `test_2_seed_forks_share_digest_and_seed_bytes_unchanged`：种子分叉两个副本，三者摘要相同；写其中一个副本，另两个的摘要不变；种子 sha256 不变。
  - `test_3_snapshot_write_restore_digest`：快照 → 写入 → 恢复，摘要等于快照时的值。
  - `test_read_only_recall_keeps_sha256`。
- [ ] **Step 2: 运行** — Expected: PASS。
- [ ] **Step 3: 提交** — `git commit -m "test(world): 验收 2、3——分叉摘要、快照恢复、只读不改字节"`

---

## 第 4 期：服务与客户端

### Task 4.1: 错误码目录（C++）

**Files:**
- Create: `include/starling/governance/error_catalog.hpp`, `src/governance/error_catalog.cpp`
- Modify: `bindings/python/bind_15_world.cpp`
- Test: `tests/cpp/test_error_catalog.cpp`

**Interfaces:**
- Produces:
  - `struct ErrorInfo { std::string_view code; bool transient; int http_status; };`
  - `[[nodiscard]] std::optional<ErrorInfo> lookup_error(std::string_view code);`
  - `[[nodiscard]] const std::vector<ErrorInfo>& error_catalog();`
  - 绑定 `_core.error_catalog() -> list[dict]`。
  - 码表，与 spec §13.2 一致，再加第 1 期已在用的码：

| code | transient | http |
|---|---|---|
| world_not_found | false | 404 |
| world_exists | false | 409 |
| invalid_world_name | false | 400 |
| world_held | false | 409 |
| world_busy | true | 503 |
| world_read_only | false | 409 |
| world_schema_behind | false | 409 |
| world_settings_mismatch | false | 409 |
| labels_immutable | false | 409 |
| fork_digest_mismatch | false | 500 |
| restore_digest_mismatch | false | 500 |
| snapshot_not_found | false | 404 |
| snapshot_corrupt | false | 500 |
| insufficient_disk | false | 507 |
| event_conflict | false | 409 |
| event_not_found | false | 404 |
| recall_degraded | true | 503 |
| handle_closed | false | 409 |
| session_expired | false | 401 |
| endpoint_changed | false | 409 |
| endpoint_key_missing | false | 409 |
| protocol_mismatch | false | 426 |
| data_dir_in_use | false | 409 |
| world_protected | false | 409 |
| world_tenant_mismatch | false | 409 |
| world_in_use | false | 409 |
| invalid_settings / invalid_labels / invalid_world_query / invalid_auto_snapshot / invalid_retention / invalid_snapshot_kind | false | 400 |
| registry_corrupt / snapshot_failed / snapshot_unsupported_backend | false | 500 |
| worlds_unavailable | true | 503 |
| world_not_initialized | false | 409 |

- `world_switching`、`ingest_busy` 不进码表（§13.2 末段：随活动世界退役）。

- [ ] **Step 1: 写失败测试** `TEST(ErrorCatalog, EverySpecCodePresentWithTransientFlag)`：断言上表每一行都在，`world_busy` 与 `recall_degraded` 的 transient 为 true；`TEST(ErrorCatalog, EveryWorldStoreErrorCodeIsCataloged)`：遍历 `src/governance` 抛出的全部码（在测试里列出常量数组，与 grep 结果一致），断言每个都查得到。
- [ ] **Step 2: 运行，确认失败** → **Step 3: 实现** → **Step 4: 运行，确认通过**。
- [ ] **Step 5: Python 侧统一错误响应**：`python/starling/dashboard/routes/world_scope.py` 与 `routes/worlds.py` 的两份 `_STATUS` 换成从 `_core.error_catalog()` 建的表，响应体改成 `{"code","transient","detail"}`，并保留 `detail` 字段的老语义（等于 code），这样前端 `describeError` 不用改。加 `tests/python/test_error_shape.py::test_world_error_body_has_code_and_transient`。
- [ ] **Step 6: 提交** — `git commit -m "feat(service): C++ 错误码目录，HTTP 错误体带 code 与 transient"`

### Task 4.2: 会话、句柄、在途调用与空闲判定（C++）

**Files:**
- Create: `include/starling/governance/session_table.hpp`, `src/governance/session_table.cpp`
- Modify: `bindings/python/bind_15_world.cpp`
- Test: `tests/cpp/test_session_table.cpp`

**Interfaces:**
- Produces（线程安全，内部一把互斥锁；所有方法收 `now_ms`，C++ 不读墙钟）：
  ```cpp
  struct SessionConfig { std::int64_t lease_ms = 30000; std::int64_t idle_close_ms = 600000; };
  class SessionTable {
   public:
    explicit SessionTable(SessionConfig cfg);
    std::string connect(std::int64_t now_ms);                         // 返回 session_id
    void heartbeat(const std::string& session_id, std::int64_t now_ms);  // 过期抛 session_expired
    void disconnect(const std::string& session_id);
    std::string open_handle(const std::string& session_id, const std::string& world,
                            const std::string& holder, bool read_only, std::int64_t now_ms);
    void close_handle(const std::string& handle_id);
    HandleInfo resolve(const std::string& handle_id, std::int64_t now_ms);  // handle_closed / session_expired
    void call_enter(const std::string& handle_id, std::int64_t now_ms);
    void call_exit(const std::string& handle_id, std::int64_t now_ms);
    std::int64_t in_flight(const std::string& handle_id) const;
    std::vector<std::string> expire(std::int64_t now_ms);             // 返回被释放的 handle_id
    bool world_has_live_handles(const std::string& world, std::int64_t now_ms) const;
    bool world_write_held_by_other(const std::string& world, const std::string& session_id,
                                   std::int64_t now_ms) const;
    bool may_close_world(const std::string& world, bool is_default, std::int64_t now_ms) const;
  };
  struct HandleInfo { std::string session_id, world, holder; bool read_only; };
  ```
  - 写占用：任一时刻至多一个会话占用一个世界的写入；别的会话开可写句柄抛 `world_held`，只读句柄可以开（§11）。
  - `may_close_world`：没有活句柄、没有在途调用、最后活动超过 `idle_close_ms`、不是默认世界。
  - `decide_open_mode` 扩展一个参数：`world_write_held_by_other` 为真时，看板写入抛 `world_held`（Task 4.3 用）。
  - 绑定 `_core.SessionTable`，每个方法都释放 GIL。

- [ ] **Step 1: 写失败测试**，每条一个 TEST：
  - `ConnectHeartbeatExpire`：30 s 不心跳就过期，过期后 `heartbeat` 抛 `session_expired`。
  - `SecondWriterGetsWorldHeld`：第二个会话开可写句柄抛 `world_held`，开只读句柄成功。
  - `ExpiredSessionReleasesWriteHold`：原会话过期后，新会话可写打开成功，原会话 `resolve` 抛 `session_expired`（Review Focus 第 4 条）。
  - `CloseHandleWaitsInFlightIsCallersJob`：`in_flight` 计数正确；`close_handle` 之后 `resolve` 抛 `handle_closed`。
  - `MultipleHandlesSameWorldSameSession`：同一会话在同一世界开两个句柄、绑不同 holder，可以。
  - `MayCloseWorldRules`：四个条件逐一否定。
- [ ] **Step 2–4: 运行确认失败 → 实现 → 运行确认通过**。
- [ ] **Step 5: 提交** — `git commit -m "feat(service): C++ 会话、句柄、写占用与空闲判定"`

### Task 4.3: `starling-serve` 入口、数据目录锁、优雅停止

**Files:**
- Create: `python/starling/service/__init__.py`, `python/starling/service/__main__.py`, `python/starling/service/serve.py`
- Modify: `pyproject.toml`（`[project.scripts] starling-serve = "starling.service.serve:main"`）
- Modify: `scripts/run_dashboard.py`（改成调用 `serve.run(data_dir=~/.starling, token_from_config=True)`）
- Modify: `python/starling/dashboard/app.py`（lifespan 里建 `SessionTable`；`/health` 在注册表打开、默认世界引擎建好之后才返回就绪，之前所有 API 回 503；`app.state.sessions`）
- Modify: `python/starling/dashboard/world_manager.py`（空闲关闭轮询：每 30 s 问 `sessions.may_close_world`，可以就关）
- Test: `tests/python/test_service_entry.py`

**Interfaces:**
- Consumes: Task 4.2 的 `SessionTable`。
- Produces:
  - `serve.main(argv)`：参数 `--data-dir D --host 127.0.0.1 --port P [--token-file F]`；token 来自 `--token-file` 或环境变量 `STARLING_SERVICE_TOKEN`，不从命令行参数取，从不写回 `starling.json`（§10.1）。
  - 启动时对 `D/.lock` 加 `fcntl.flock(LOCK_EX | LOCK_NB)`，拿不到就打印 `data_dir_in_use`，退出码 3。
  - SIGTERM：停止接受新请求 → 等在途请求结束（上限 `--drain-timeout`，默认 30 s）→ 停各世界后台线程 → 逐个关世界 → 释放锁 → 退出 0。
  - 所有路径都在 `D` 之下：`D/starling.json`（可以没有；没有就从环境变量 `OPENAI_*` 取 provider）、`D/registry.db`、`D/worlds/`、`D/ingest-spool/`。

- [ ] **Step 1: 写失败测试**（子进程方式起服务，端口用 `0` 让系统分配，再从 stdout 的就绪行读回端口）：
  - `test_second_server_same_data_dir_exits_data_dir_in_use`：退出码 3，stderr 含 `data_dir_in_use`。
  - `test_token_not_accepted_from_argv`：`--token` 不是参数（argparse 报错）。
  - `test_sigterm_then_restart_keeps_committed_writes`：写入 3 条后 SIGTERM，在 30 s 内退出码 0；重启后 3 条都在（§16 第 7 条）。
  - `test_health_503_until_ready`。
  - `test_data_dir_is_the_only_root`：给一个空的 `D`，断言 `~/.starling` 的 mtime 不变（conftest 已重定向 HOME 相关默认值；这里另设 `HOME=tmp`）。
- [ ] **Step 2–4: 运行确认失败 → 实现 → 运行确认通过**。
- [ ] **Step 5: 回归** — Run: `python -m pytest tests/python -q -k "dashboard or service or world"` — Expected: PASS。`scripts/run_dashboard.py` 的行为不变：用户的 8787 仍从 `~/.starling/starling.json` 取 token。
- [ ] **Step 6: 提交** — `git commit -m "feat(service): starling-serve 入口、数据目录锁、SIGTERM 优雅停止"`

### Task 4.4: 客户端包骨架、传输层与异常（纯 Python，可与 4.1–4.3 并行）

**Files:**
- Create: `clients/python/pyproject.toml`, `clients/python/starling_client/__init__.py`, `clients/python/starling_client/_transport.py`, `clients/python/starling_client/errors.py`, `clients/python/starling_client/types.py`, `clients/python/starling_client/_build_info.py`（构建时生成，见 Step 4）
- Test: `clients/python/tests/test_transport.py`, `clients/python/tests/test_errors.py`

**Interfaces:**
- Produces:
  - 异常（`errors.py`）：每个都有 `code: str` 与 `transient: bool` 属性。

    | 异常 | 基类 | 对应的 code |
    |---|---|---|
    | `StarlingClientError` | `Exception` | 公共基类 |
    | `RecallDegradedError` | `StarlingClientError` | `recall_degraded` |
    | `EventConflict` | `StarlingClientError` | `event_conflict` |
    | `MemoryClosed` | `StarlingClientError`, `RuntimeError` | `handle_closed` |
    | `ServiceUnavailable` | `StarlingClientError` | 传输层，transient |
    | `ServiceTimeout` | `StarlingClientError` | 传输层，transient |
    | `ServiceError` | `StarlingClientError` | 5xx，带 transient |
    | `ProtocolMismatch` | `StarlingClientError` | `protocol_mismatch` |
    | `SessionExpired` | `StarlingClientError` | `session_expired` |
    | `WorldError` | `StarlingClientError` | 其余 `world_*` 码 |

    `event_not_found` 抛 `KeyError`（同时是 `StarlingClientError`）。
  - `types.py`：`RememberResult`、`PendingEvent`（字段同 `starling.memory.PendingEvent`，加 `holder`）、`EmbedStats`、`StatementRow`（字段同 `StatementRow`：`id, subject_id, predicate, object_value, polarity, modality, confidence, holder_id, ...`，`frozen=True` 的 dataclass）。
  - `_transport.Transport(url, token, timeout_s)`：`post(path, json) -> dict`、`get(path, params) -> dict`，把错误体 `{"code","transient","detail"}` 按 `errors.from_body(status, body)` 映射成异常。
  - `PROTOCOL = 1`（整数）。
- [ ] **Step 1: 写失败测试**：`test_error_body_maps_to_named_exceptions`（每个码映射到上表的异常，`code` 与 `transient` 正确）；`test_connect_refused_is_service_unavailable_transient`；`test_timeout_is_service_timeout`；`test_5xx_is_service_error_with_transient_from_body`；`test_client_has_no_starling_core_import`（`sys.modules` 里没有 `starling._core`）。
- [ ] **Step 2: 运行，确认失败** — Run: `cd clients/python && python -m pytest tests -q` — Expected: FAIL（`ImportError`）。
- [ ] **Step 3: 实现** `errors.py`、`types.py`、`_transport.py`。
- [ ] **Step 4: `_build_info.py` 的生成**：`clients/python/pyproject.toml` 用 setuptools，加一个构建钩子 `clients/python/build_info_hook.py`。钩子在构建时写入 `COMMIT = "<git rev-parse HEAD>"`、`VERSION`；不在 git 仓库里时写 `"unknown"`。`_build_info.py` 进 `.gitignore`。
- [ ] **Step 5: 运行，确认通过**，然后提交 — `git commit -m "feat(client): starling-client 包骨架、传输层与异常映射"`

### Task 4.5: 客户端路由与 `RemoteMemory`

**Files:**
- Create: `python/starling/dashboard/routes/client.py`（`/api/v1/client/...`）
- Modify: `python/starling/dashboard/app.py`（挂载路由）
- Modify: `python/starling/dashboard/engine.py`（门面方法按 holder 调：`recall(holder=...)`、`remember_event(holder=...)`、`abandon_event(holder=...)`，在途调用前后调 `sessions.call_enter/exit`）
- Create: `clients/python/starling_client/client.py`, `clients/python/starling_client/remote_memory.py`
- Test: `tests/python/test_client_routes.py`, `clients/python/tests/test_remote_memory.py`

**Interfaces:**
- Consumes: Task 4.1 的码表、4.2 的 `SessionTable`、4.3 的 `app.state.sessions`、4.4 的传输层与类型。
- Produces（HTTP 都是 POST，body 为 JSON，鉴权沿用 Bearer token）：

| 路径 | body | 返回 |
|---|---|---|
| `/api/v1/client/connect` | `{protocol}` | `{session_id, lease_ms, heartbeat_ms, info}`；协议不同 → `protocol_mismatch` |
| `/api/v1/client/heartbeat` | `{session_id}` | `{}` |
| `/api/v1/client/disconnect` | `{session_id}` | `{}` |
| `/api/v1/client/info` | `{}` | `{version, commit, protocol, extraction_config, default_settings}`（不含密钥与地址） |
| `/api/v1/client/open` | `{session_id, world, holder, read_only, settings?}` | `{handle_id}` |
| `/api/v1/client/close` | `{handle_id, timeout_s?}` | `{}`；超时 → `world_busy`，句柄保持打开 |
| `/api/v1/client/call` | `{handle_id, method, args}` | 方法返回值；`method` 取 `recall / remember_event / abandon_event / content_ids / digest / pending_events / next_seq / embed_pending` |
| `/api/v1/client/admin/<op>` | 各操作的参数 | `create_world / fork / upgrade_seed / snapshot(held) / restore / delete_snapshot / digest / pending_events / close_world / list_worlds` |

  - 客户端（§13.3 的签名原样照抄）：`starling_client.connect(url, *, token, timeout_s=30.0) -> Client`；`Client.open(world, *, holder, read_only=False, settings=None) -> RemoteMemory`；`RemoteMemory` 上的方法同 §13.3；心跳由后台线程每 `heartbeat_ms` 发一次，失败只记日志；`close(timeout)` 先在本地等在途调用，再请求服务端。
  - `recall` 每项是 `{"content_id", "score", "row": StatementRow}`。
  - `open` 给了 `settings`，且与该世界已存的设置不同（按 C++ `world_effective_settings` 规范化后比较）时，返回 `world_settings_mismatch`。比较放在 C++：新增 `bool settings_equal(const std::string& stored_json, const std::string& given_json)`，位于 `world_settings.cpp`。
  - `admin/list_worlds` 的参数是 `{labels?, page, page_size, with_digest=false}`。每项含名字、标签、`seed`、`read_only`、来源、holder 数、语句数、最后写入时间、状态；`with_digest=true` 时再加 `digest`，按世界只读打开计算（§13.3）。
- [ ] **Step 1: 写服务端失败测试**（`TestClient`）：
  - connect→open→call(remember_event)→call(recall)→close 的往返；
  - `world_held`（两个会话）；
  - `session_expired`（注入时钟推进 31 s）；
  - `handle_closed`；
  - `close` 超时后句柄保持打开；
  - `info` 里没有 `api_key` 与 `base_url` 的值（遍历 JSON 断言）；
  - `open` 带不同的 `settings` → `world_settings_mismatch`；
  - `list_worlds(with_digest=True)` 每项的 `digest` 等于该世界的 `digest()`，计算前后世界文件的 sha256 不变。
- [ ] **Step 2: 写客户端失败测试**（`clients/python/tests/test_remote_memory.py`，用 `httpx.MockTransport` 回放服务端响应）：方法签名与 §13.3 一致；`recall` 的 `row` 是 frozen `StatementRow`；心跳线程在 `close()` 后停止；`session_expired` 后下一次调用抛 `SessionExpired`，并且不会自动重开句柄。
- [ ] **Step 3–5: 运行确认失败 → 实现 → 运行确认通过**。
- [ ] **Step 6: 提交** — `git commit -m "feat(service): /api/v1/client 路由与 RemoteMemory"`

### Task 4.6: 假端点与第 4 期验收（§16 第 1、1b、6、7、8 条）

**Files:**
- Create: `tests/python/fakes/openai_fake_server.py`（子进程里的本机 OpenAI 兼容假端点：按事件 key 让抽取回 500 或坏 JSON，嵌入按词面给确定向量；参照 rise 的 `tests/starling_fakes.py`）
- Create: `tests/python/test_acceptance_service.py`

**Interfaces:**
- Consumes: Task 4.3 的 `starling-serve`、4.5 的客户端。
- Produces: 夹具 `fake_openai(tmp_path) -> base_url`；夹具 `service(tmp_path, fake_openai) -> (url, token)`，用子进程起 `starling-serve`，`D/starling.json` 里的 provider 指向假端点。

- [ ] **Step 1: 写验收测试**（名字里带验收编号）：
  - `test_1_three_worlds_isolated`。
  - `test_1b_snapshot_write_restore_y_while_x_writes`：X 开着句柄在写时，对 Y 快照、写入、恢复都成功，X 的摘要不变；Y 自己还有句柄时恢复 Y 返回 `world_held`。
  - `test_6_facade_parity`：同一组调用分别走进程内 `Memory` 和走客户端。调用包括同 key 同文本重入、同 key 异文本冲突、abandon 后再写冲突、嵌入失败时 strict 召回抛 `RecallDegradedError`、`close` 超时句柄不被标成已关。两边结果逐项相等。
  - `test_7_sigterm_restart_and_second_server`。
  - `test_8_info_commit_equals_install_commit`。
  - `test_cross_world_gil`：一个世界的抽取在假端点阻塞 700 ms，同时另一个世界的召回在 200 ms 内返回（§12）。
  - `test_logs_and_errors_have_no_address_or_key`：遍历服务 stderr 与所有错误体，断言里面没有假端点的 URL 和 key。
- [ ] **Step 2: 运行** — Run: `python -m pytest tests/python/test_acceptance_service.py -v` — Expected: PASS。
- [ ] **Step 3: 提交** — `git commit -m "test(service): 假端点与验收 1、1b、6、7、8"`

---

## 第 5 期：看板前端补齐

> 第 1 期 Task 7 已做完世界选择器、按世界取数、实时推送、标签过滤与分页。本期只补 spec §14 剩下的部分。

### Task 5.1: 世界状态提示与列表新列

**Files:**
- Modify: `dashboard/web/src/lib/worlds.ts`（`WorldRow` 加 `seed`、`read_only`、`reclaimable`、`fork_of_world`、`fork_of_snapshot`、`schema_behind`、`held_by_client`）
- Create: `dashboard/web/src/lib/components/WorldStatusBanner.svelte`
- Modify: `dashboard/web/src/routes/+layout.svelte`（在顶栏下渲染横幅）、`dashboard/web/src/routes/worlds/+page.svelte`（加分叉来源列和状态列）
- Modify: `python/starling/dashboard/routes/worlds.py`（列表与详情加 `held_by_client: bool`，来自 `sessions.world_write_held_by_other(world, "", now)`）
- Test: `dashboard/web/src/lib/components/WorldStatusBanner.test.ts`、`dashboard/web/src/routes/worlds/page.test.ts`（追加）、`tests/python/test_dashboard_world_fork.py`（追加 `held_by_client`）

**Interfaces:**
- Consumes: 第 3 期的 seed、read_only、fork 字段，第 4 期的 `SessionTable`。
- 横幅文案（精确）：
  - 只读：「只读世界（种子或复盘副本）：可以检视和召回，不能写入」
  - 被占用：「这个世界正被客户端写入：写入已置灰，召回可用」
  - schema 落后：「这个世界的库版本落后于当前 Starling，部分检视页可能报错」
- 写入类按钮（记住、tick、清空记忆）在只读或被占用时 `disabled`，并带 `title` 写明原因。

- [ ] **Step 1: 写失败测试**：每种状态各渲染对应的横幅文案；写入按钮置灰；列表显示分叉来源 `w1@20261105T…`。
- [ ] **Step 2–4: 运行确认失败 → 实现 → 运行确认通过** — Run: `cd dashboard/web && npx vitest run src/lib/components/WorldStatusBanner.test.ts src/routes/worlds/page.test.ts && npm run check` — Expected: PASS。
- [ ] **Step 5: 提交** — `git commit -m "feat(web): 世界状态提示，列表显示分叉来源与状态"`

### Task 5.2: 第 5 期验收（§16 第 4 条）

**Files:**
- Create: `tests/python/test_acceptance_4_dashboard_live.py`

- [ ] **Step 1: 写验收测试** `test_4_dashboard_sees_client_writes_and_reopens_closed_world_read_only`：客户端写一个可回收世界的同时，看板 API `/api/w/{world}/statements` 在 5 s 内读到新语句，并能在里面召回；客户端 `close_world` 之后，按标签 `list_worlds?label.run_id=…` 能找到它，看板打开它是只读的，sha256 不变。
- [ ] **Step 2: 运行，提交** — `git commit -m "test(web): 验收 4——看板实时读到客户端写入，关掉后只读打开"`

---

## 第 6 期：自动清理

### Task 6.1: 清理规则与删除（C++，P3 合并后即可开始）

**Files:**
- Create: `include/starling/governance/cleanup.hpp`, `src/governance/cleanup.cpp`
- Modify: `src/governance/registry_db.cpp`（`cleanup_log` 表；注册表 `kSchemaVersion` 从 1 升到 2，`open` 时对 1 版做 `CREATE TABLE IF NOT EXISTS cleanup_log` 的升级）
- Modify: `bindings/python/bind_15_world.cpp`
- Test: `tests/cpp/test_cleanup.cpp`

**Interfaces:**
- Consumes: 第 3 期的 `reclaimable`、`retention_group`、`held` 快照；`purge_rollback_archives`。
- Produces:
  ```cpp
  struct CleanupConfig { std::int64_t keep_per_group = 200; std::int64_t max_age_days = 14;
                         std::int64_t archive_max_age_days = 14; };
  struct CleanupCandidate { std::string world, group, reason; };   // reason: "over_count" | "over_age"
  struct CleanupSkip { std::string world, reason; };                // reason: "open" | "held_snapshot"
  struct CleanupPlan { std::vector<CleanupCandidate> remove; std::vector<CleanupSkip> skip; };
  CleanupPlan plan_cleanup(const RegistryDb& reg, const CleanupConfig& cfg, std::string_view now_iso,
                           const std::set<std::string>& open_worlds);
  std::vector<CleanupCandidate> execute_cleanup(RegistryDb& reg, const CleanupPlan& plan,
                                                std::string_view now_iso);  // 删除并写 cleanup_log
  std::vector<CleanupLogEntry> cleanup_log(const RegistryDb& reg, std::int64_t limit);
  ```
  - `execute_cleanup` 删每个世界之前，在注册表锁内重新检查它是否仍可删：仍是 reclaimable、没有 held 快照、此刻不在 `open_worlds` 里（由 Task 6.2 在世界锁下传入）。不满足就跳过，写 `skip` 记录（Review Focus 第 5 条）。
  - 恢复前的旧库归档：属于可回收世界的随世界一起删；属于不可回收世界的超过 `archive_max_age_days` 就删。
- [ ] **Step 1: 写失败测试**（`now_iso` 注入）：
  - `KeepsNewest200PerGroup`：一组 205 个，删最旧的 5 个，reason 为 `over_count`。
  - `DropsOlderThan14Days`。
  - `NeverTouchesNonReclaimableSeedDefault`。
  - `SkipsWorldWithHeldSnapshotAndLogsReason`。
  - `SkipsOpenWorlds`。
  - `ArchivesOfNonReclaimableWorldsAgeOut`。
  - `CleanupLogRecordsEachRemoval`。
  - `RegistryV1UpgradesToV2WithCleanupLog`。
- [ ] **Step 2–4: 运行确认失败 → 实现 → 运行确认通过**。
- [ ] **Step 5: 提交** — `git commit -m "feat(world): C++ 自动清理规则、删除与清理记录"`

### Task 6.2: 清理调度、配置项与看板记录

**Files:**
- Modify: `python/starling/dashboard/world_manager.py`（每 `cleanup_interval_s`（默认 600）跑一次：取 `open_worlds`，调 `plan_cleanup`，对每个候选在世界锁下确认引擎已关再 `execute_cleanup`）
- Modify: `python/starling/dashboard/config.py`（`cleanup_keep_per_group=200`、`cleanup_max_age_days=14`、`cleanup_interval_s=600`、`idle_close_s=600`、`session_lease_s=30`、`heartbeat_s=10`、`stats_refresh_s=30`）
- Modify: `python/starling/dashboard/routes/worlds.py`（`GET /api/worlds/cleanup_log?limit=50`）
- Modify: `dashboard/web/src/routes/worlds/+page.svelte`（底部「最近清理」表：世界、分组、原因、时间）
- Test: `tests/python/test_cleanup_scheduler.py`、`tests/python/test_acceptance_4a_5_cleanup.py`、`dashboard/web/src/routes/worlds/page.test.ts`（追加）

- [ ] **Step 1: 写失败测试**：
  - `test_4a_cleanup_respects_count_age_and_protections`（时间注入，覆盖 §16 第 4a 条的全部不删项）。
  - `test_5_background_off_world_digest_stable`：关掉后台任务的世界静置，推进注入时钟并跑若干轮清理与调度，摘要不变（§16 第 5 条）。
  - `test_cleanup_races_open`：清理选中世界 W 的同时另一线程 `open(W)`。结果只能是两种之一：W 被跳过且记录原因为 `open`；或 `open` 得到 `world_not_found` 且 W 的文件已不在。不能出现 W 的引擎开着而文件被删（Review Focus 第 5 条）。
  - vitest：清理记录表渲染。
- [ ] **Step 2–4: 运行确认失败 → 实现 → 运行确认通过**。
- [ ] **Step 5: 提交** — `git commit -m "feat(world): 清理调度、配置项与看板清理记录"`

### Task 6.3: 回告 rise（§19）

**Files:**
- Create: `docs/superpowers/specs/2026-11-06-memory-service-reply-to-rise.md`（§19 的答复，加上客户端安装方式 `starling-client @ git+<仓库地址>@<commit>#subdirectory=clients/python` 和一页迁移指引：`starling.Memory.open(path, agent=...)` 换成 `client.open(world, holder=...)`）

- [ ] **Step 1: 写文档**：逐条对照 §19，每条附上实现位置（文件和测试名）。
- [ ] **Step 2: 提交** — `git commit -m "docs: 记忆服务对 rise 的答复与迁移指引"`
