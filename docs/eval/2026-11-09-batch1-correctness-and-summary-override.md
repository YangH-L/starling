# 第一批正确性修复与可选总结原文预算

本批来自 BEAM-1M 基线复现：逐步核对检索、证据渲染和再巩固路径时发现历史链、冲突边大小写、证据引用及充分性回执问题；总结题还受到原文预算限制。补丁同时包含正确性修复与一个默认关闭的预算扩展。两者应逐提交审阅，尤其优先看第 1、3 个提交。

本轮固定 main 基线为 `cff319d952360727823024ff43c71b15055bd8e3`。先独立编译并测试基线，再逐提交编译验证。main 前进涉及的旧一轮 31 个、新一轮 13 个相关非合并提交已逐个审查，未发现额外语义冲突；重放无文本冲突。旧一轮重点核对 GIL、内容身份、按 holder 分离的事件与原文、新世界绑定；新一轮主要增加 memory-service、会话和事件接口，没有改动本批的 planner、store、再巩固和 memory_ops 实现。没有把“文本能干净应用”当作语义兼容的依据。

## 逐提交说明

### 01 — fix(reconsolidation): keep recalled events out of contradiction evidence

- 问题：召回事件被混入矛盾证据，可能推动错误分叉。
- 修法：召回权重置零、矛盾聚合过滤召回；关窗清 pending；分叉复制完整列以保留来源信息。
- 默认行为：会改变在线再巩固，不等同于 BEAM 默认上下文的全局语义不变。
- 验证：再巩固、分叉保真测试；相关 C++ 49 项。
- 分支代码位置：
  - [include/starling/reconsolidation/plastic_window.hpp:34](../../include/starling/reconsolidation/plastic_window.hpp#L34)
  - [src/reconsolidation/arbitration.cpp:144](../../src/reconsolidation/arbitration.cpp#L144)
  - [src/reconsolidation/plastic_window.cpp:120](../../src/reconsolidation/plastic_window.cpp#L120)
  - [src/reconsolidation/reconsolidation_engine.cpp:197](../../src/reconsolidation/reconsolidation_engine.cpp#L197)
  - [src/store/sqlite_statement_store.cpp:316](../../src/store/sqlite_statement_store.cpp#L316)
  - [tests/cpp/test_reconsolidation_engine.cpp:411](../../tests/cpp/test_reconsolidation_engine.cpp#L411)
  - [src/store/sqlite_statement_store.cpp:339](../../src/store/sqlite_statement_store.cpp#L339)
  - [src/reconsolidation/plastic_window.cpp:229](../../src/reconsolidation/plastic_window.cpp#L229)

### 02 — fix(retrieval): complete supersedes history from a single visible seed

- 问题：HISTORY 只有一个可见种子时漏掉 supersedes 链。
- 修法：以可见种子展开链，visited 集合阻止重复和循环。
- 默认行为：HISTORY 可能返回更多同范围历史。
- 验证：相关 C++ 13 项；第 8 个提交补 holder 与环测试。
- 分支代码位置：
  - [src/retrieval/retrieval_planner.cpp:339](../../src/retrieval/retrieval_planner.cpp#L339)
  - [tests/cpp/test_retrieval_planner.cpp:281](../../tests/cpp/test_retrieval_planner.cpp#L281)

### 03 — fix(retrieval): read written conflict edges and prefer renderable sources

- 问题：写方落库小写 conflicts_with，读方却使用大写；冲突还会否决已有原文的整题。
- 修法：启用生产冲突检测，统一读取小写；有可渲染来源时允许返回原文。
- 默认行为：有冲突的检索行为改变；详见维护者决定。
- 验证：相关 C++ 18 项及原文 lane 测试。
- 分支代码位置：
  - [src/retrieval/abstention.cpp:8](../../src/retrieval/abstention.cpp#L8)
  - [src/retrieval/retrieval_planner.cpp:693](../../src/retrieval/retrieval_planner.cpp#L693)
  - [tests/cpp/test_abstention_gate.cpp:44](../../tests/cpp/test_abstention_gate.cpp#L44)
  - [tests/cpp/test_planner_source_lane.cpp:482](../../tests/cpp/test_planner_source_lane.cpp#L482)

### 04 — fix(context): accept production and legacy evidence reference keys

- 问题：生产证据引用使用的键与 context renderer 的旧键不同。
- 修法：同时识别生产 engram_ref 与旧 engram_id；同一个证据对象中的两个别名只计一次。
- 默认行为：有相关证据时标签与上下文可能改变。
- 验证：相关 C++ 6 项；生产键、旧键及单对象别名去重用例。
- 分支代码位置：
  - [src/retrieval/context_pack.cpp:22](../../src/retrieval/context_pack.cpp#L22)
  - [tests/cpp/test_context_pack.cpp:100](../../tests/cpp/test_context_pack.cpp#L100)

### 05 — fix(retrieval): count original evidence toward sufficiency receipts

- 问题：原文证据未计入充分性回执。
- 修法：原文通道命中后更新 sufficiency_status 与 MISSING_INFO。
- 默认行为：回执值有意改变；上下文字节等价不代表回执不变。
- 验证：相关 C++ 15 项；原文充分性回执。
- 分支代码位置：
  - [src/retrieval/retrieval_planner.cpp:828](../../src/retrieval/retrieval_planner.cpp#L828)
  - [tests/cpp/test_planner_source_lane.cpp:500](../../tests/cpp/test_planner_source_lane.cpp#L500)

### 06 — feat(memory): add opt-in summary source budget override

- 问题：固定原文预算限制总结题所需跨段证据。
- 修法：新增可选 SummarySourceOverride，仅检测为总结请求时扩大原文预算；默认 None/nullopt。
- 默认行为：默认路径保持现有预算；启用后只有总结题扩大预算。
- 验证：相关 C++ 29 项、Python 5 项。
- 分支代码位置：
  - [bindings/python/bind_13_memory_ops.cpp:220](../../bindings/python/bind_13_memory_ops.cpp#L220)
  - [include/starling/memory/memory_ops.hpp:166](../../include/starling/memory/memory_ops.hpp#L166)
  - [python/starling/_memory_core.py:376](../../python/starling/_memory_core.py#L376)
  - [src/memory/memory_ops.cpp:408](../../src/memory/memory_ops.cpp#L408)
  - [tests/cpp/test_converse_phases.cpp:301](../../tests/cpp/test_converse_phases.cpp#L301)
  - [tests/python/test_converse_phases_binding.py:72](../../tests/python/test_converse_phases_binding.py#L72)

### 07 — fix(memory): validate effective summary source limits

- 问题：覆盖值可超过 k_max；原 k_source=0 的比较忽略 query.k。
- 修法：先验证原依赖，拒绝 k_source>k_max，并用实际生效的原值检查不得缩小。
- 默认行为：无覆盖时不改变路径；非法覆盖抛 invalid_argument/Python ValueError。
- 验证：有效 RED：C++ 2 项失败、Python 1 项失败；GREEN C++ 32 项、Python 6 项。
- 分支代码位置：
  - [src/memory/memory_ops.cpp:416](../../src/memory/memory_ops.cpp#L416)
  - [tests/cpp/test_converse_phases.cpp:384](../../tests/cpp/test_converse_phases.cpp#L384)
  - [tests/python/test_converse_phases_binding.py:100](../../tests/python/test_converse_phases_binding.py#L100)

### 08 — fix(retrieval): keep history completion within the queried holder

- 问题：HISTORY fetch_by_id 未跟主路径一样约束 holder。
- 修法：补全查询绑定本次 querier 的 holder_id。
- 默认行为：跨 holder 的 supersedes 不再带回其他 holder 语句。
- 验证：跨 holder 用例 RED；环正控制通过；GREEN C++ 30 项。
- 分支代码位置：
  - [src/retrieval/retrieval_planner.cpp:341](../../src/retrieval/retrieval_planner.cpp#L341)
  - [tests/cpp/test_retrieval_planner.cpp:303](../../tests/cpp/test_retrieval_planner.cpp#L303)

### 09 — fix(reconsolidation): emit supports events only after an update

- 问题：Supports 更新被 archived/forgotten 守卫拒绝后仍发 consolidated 事件。
- 修法：store 返回 sqlite3_changes，实际改动为零时提前返回。
- 默认行为：被守卫拒绝时状态、置信度和事件均不变；可更新语句仍发事件。
- 验证：事件用例 RED；GREEN 相关 C++ 51 项，含两个终态及正控制。
- 分支代码位置：
  - [include/starling/store/sqlite_statement_store.hpp:52](../../include/starling/store/sqlite_statement_store.hpp#L52)
  - [include/starling/store/statement_store.hpp:97](../../include/starling/store/statement_store.hpp#L97)
  - [src/reconsolidation/arbitration.cpp:205](../../src/reconsolidation/arbitration.cpp#L205)
  - [src/store/sqlite_statement_store.cpp:314](../../src/store/sqlite_statement_store.cpp#L314)
  - [tests/cpp/test_reconsolidation_engine.cpp:563](../../tests/cpp/test_reconsolidation_engine.cpp#L563)

### 10 — fix(dashboard): read canonical lowercase conflict edges

- 问题：看板后端 conflicts 查询仍用大写持久化值。
- 修法：读取 canonical 小写 conflicts_with。
- 默认行为：看板能显示生产写入的冲突边。
- 验证：两个 tenant 参数用例 RED；GREEN 后端 14 项。
- 分支代码位置：
  - [python/starling/dashboard/queries.py:238](../../python/starling/dashboard/queries.py#L238)
  - [tests/python/test_dashboard_conflicts.py:11](../../tests/python/test_dashboard_conflicts.py#L11)

### 11 — fix(dashboard): count canonical conflicts after visibility filtering

- 问题：看板前端计数仍读大写键，且 badge 未按可见条目过滤。
- 修法：读小写键，badge 采用过滤后的 sorted.length；加入真实组件渲染测试。
- 默认行为：忽略 forgotten 条目后显示正确计数；其他边不计冲突。
- 验证：组件用例 RED；完整前端 414 项通过、check 0 错误/0 警告、build 通过。全局 teardown 在 cleanup 后等 50ms，让 bits-ui 24ms 滚动锁清理在 jsdom 存活期完成，不屏蔽异常。
- 分支代码位置：
  - [dashboard/web/src/routes/conflicts/+page.svelte:138](../../dashboard/web/src/routes/conflicts/+page.svelte#L138)
  - [dashboard/web/src/routes/conflicts/conflicts.test.ts:1](../../dashboard/web/src/routes/conflicts/conflicts.test.ts#L1)
  - [dashboard/web/vitest.setup.ts:1](../../dashboard/web/vitest.setup.ts#L1)

### 12 — style(core): satisfy clang-tidy on batch-one changed lines

- 问题：严格 clang-tidy 改动行检查发现本批风格诊断。
- 修法：仅修改本批触及行的参数命名、括号、绑定和局部变量；不加豁免。
- 默认行为：不改变业务逻辑或警告参数。
- 验证：受影响 Debug C++ 103 项通过，随后完整 Debug 1912 项及改动行 tidy 通过。
- 分支代码位置：
  - [bindings/python/bind_13_memory_ops.cpp:245](../../bindings/python/bind_13_memory_ops.cpp#L245)
  - [include/starling/store/sqlite_statement_store.hpp:52](../../include/starling/store/sqlite_statement_store.hpp#L52)
  - [include/starling/store/statement_store.hpp:98](../../include/starling/store/statement_store.hpp#L98)
  - [src/reconsolidation/plastic_window.cpp:298](../../src/reconsolidation/plastic_window.cpp#L298)
  - [src/retrieval/abstention.cpp:11](../../src/retrieval/abstention.cpp#L11)
  - [src/retrieval/context_pack.cpp:18](../../src/retrieval/context_pack.cpp#L18)
  - [src/retrieval/retrieval_planner.cpp:395](../../src/retrieval/retrieval_planner.cpp#L395)

## 总结预算：实验依据与启用

前期实验得到召回 1M 宏平均 +1.27pp，总结题 6.3% → 15.6%；67 道总结题作答均分 0.502 → 0.584，Δ=+0.082，95% 区间 [0.055, 0.109]（按会话聚类 bootstrap 10,000 次）。这些是前期实验结果，本轮没有调用 LLM 重新评估作答质量。代价是总结题上下文中位数约 4.04 倍，范围约 2.4–8.4 倍。

调用方须已配置原文 source_lane，并持有依赖对象。一次请求的显式启用方式：

```cpp
params.summary_source_override = starling::memoryops::SummarySourceOverride{100, 128000};
```

```python
prepared = memory.converse_prepare(message, summary_source_override=_core.SummarySourceOverride(100, 128000))
```

Python 的 `_core` 来自 `from starling import _core`；`memory` 是已有 MemoryCore 实例。未设置时保持默认路径。开启但检测为非总结时仍使用原预算；参数合法性验证仍执行。修改公开 C++ 结构体和虚接口后，下游 native 扩展和 store 实现须同步重新编译。

## 全量验证与默认路径等价

工具链为 clang/clang++/clang-tidy 17.0.6、Ninja 1.11.1、CMake 3.28.3、SQLite 3.46.1、pybind11 3.1.0。保留仓库全部 `-Wall -Wextra -Wpedantic -Wconversion -Wshadow -Werror`；没有修改警告参数或测试期望。

| 检查 | main 基线 | 本批 HEAD |
|---|---:|---:|
| Release CTest | 1888 通过，0 跳过 | 1912 通过，0 跳过 |
| Python 3.12 根目录 pytest | 3341 通过，630 跳过 | 3346 通过，630 跳过 |
| CI Python 3.11.18 根目录 pytest | — | 3346 通过，630 跳过 |
| clients/python pytest | 109 通过 | 109 通过（两个 Python 版本） |
| Debug CTest | — | 1912 通过，0 跳过 |
| 改动行 clang-tidy / 静态扫描 | — | 均通过 |
| 前端 Vitest / svelte-check / build | — | 414 通过 / 0 错误 0 警告 / 通过 |

两版 Python 的 630 个跳过项与 main 完全相同，按 classname、name、skip message 逐项比较；没有新增跳过。CI 模拟采用 Debug 全量构建和测试、仓库 `ci_clang_tidy_diff.py` 改动行检查、`ci_static_scan.py`、Python 3.11 editable 安装主包和 clients/python 后运行测试。改动行 tidy 通过不表示仓库旧代码全部无诊断。

Python 3.11 首轮使用的独立发行包内置了 SQLite 3.53.1，与 native 扩展的 3.46.1 不同，出现 34 项跨接口读写失败。改用官方 CPython 3.11.18 源码（核验发布方 SHA-256）构建动态 SQLite 模块，使 Python/native 均加载 3.46.1 后，原 34 项不改期望重放通过，随后全量通过。本次恢复只调整验证环境，没有修改仓库代码、测试或警告参数。

生产 `memory_converse_prepare` 对照采用独立构建并冻结的 main native 扩展与 HEAD native 扩展；每个会话、每个路径均使用独立金样副本。参数为 tenant=default、holder=user、AS_OF=2099-01-01T00:00:00Z、recall_k=10、tau_source=0.56、k_max=200、k_source=30、chunk_byte_budget=32000。逐题比较整包 UTF-8 上下文字节，未用归档输出代替当前 main。

- 覆盖关闭：**625/625** 与当前 main 逐字节一致。
- 覆盖 100/128000：非总结 **558/558** 与 main 一致；检测到总结 67 题。
- 67 题上下文字节倍数：最小 **2.406281**，中位数 **4.036645**，最大 **8.378083**。
- main 已前进，67 题与旧 A1 作答存档逐字节对比不再适用。
- 基线缓存命中 1250，HEAD 的关闭/开启两路共命中 2500；缓存未命中 0、上游嵌入请求 0、LLM 调用 0。localhost 仅返回只读缓存，未命中即失败。
- 输入问题 JSON SHA-256：`c507d2e0aafee63be86eacdc89ee41c1334f75ed03b104e41be90c7c2003a23f`；前后复核不变。金样 SQL 打开次数 0，只在副本执行 SQL。

该等价结论仅覆盖上述数据和 `converse_prepare` 上下文，不证明在线再巩固、带真实冲突/多 holder 的请求或所有回执值不变。

## 35 个金样库的数据统计

在只读副本上使用 SQLite 3.46.1 汇总：35 库、12,741 条 statements；`conflicts_with` 边 0，大写 `CONFLICTS_WITH` 边也为 0；holder≠user 的语句 0；跨 holder 的 supersedes 边 0。数据库及已有 WAL/SHM 先复制，再以 mode=ro 读取副本；原库没有被 SQL 打开。

因此这些真实数据不会触发跨 holder 和冲突分支，625 题等价不能替代合成边界测试；多 holder 的证据标签变化也不能由该数据集证明安全。

## 待维护者决定

| 事项 | 当前实现与影响 | 可选做法 |
|---|---|---|
| 召回权重 | 召回权重为零，不能作为矛盾证据；影响既有再巩固动力 | 接受零权重，或另定小权重并独立验证 |
| 强制关窗 | 召回仍计入关窗次数；关窗清空真正 pending 且不仲裁，可能丢掉待处理证据 | 接受现状；或分开召回计数；或强关窗前仲裁 |
| MildContradict | 原有路径仍可能复活 archived/forgotten；Supports 修复不覆盖此路径 | 统一终态守卫，或明确允许复活条件 |
| 分叉复制 | 继承全部列，可保留来源，也复制生命周期/状态元数据 | 接受保真复制；或定义逐列重置策略并加迁移测试 |
| pending 异常边界 | clear_pending_evidence 在 try/catch 外，清理失败可向上传播 | 保持传播；或纳入统一失败/事务处理 |
| 生产冲突检测 | 原来未生效的检测现在生效，会影响真实冲突请求 | 启用并观察；或另加 rollout 开关 |
| 原文优先 | 有可渲染原文时不再按原 spec 整题保守否决 | 接受来源优先；或坚持保守拒答；或按冲突类型分层 |
| 未解决冲突 | 只按边存在判断，不读取独立解决状态 | 接受边生命周期语义；或增加 resolution 协议 |
| 他者多证据 | 多证据条目由 HEARSAY 变为 BELIEF，可能提高读者感知可信度 | 接受标签；或按 holder/来源重新划分 |
| 证据重复 | 跨条目不去重，同一来源可能重复计数 | 接受条目计数；或按稳定来源身份去重 |

召回权重在多个入口被归零，显式传入的召回权重也会被忽略。频繁召回不再因矛盾聚合直接推动分叉/归档，但已有错误分叉不会自动修复。HISTORY 现在允许读回已过期版本，同时仍受本次 holder 范围限制；是否采用这套历史呈现语义也需审阅。总结预算扩大带来的上下文成本仅由显式开关承担。

## 引用与契约审查

全仓扫描五个符号：`CONFLICTS_WITH`、`conflicts_with`、`sufficiency_status`、`MISSING_INFO`、`SummarySourceOverride`，覆盖 tracked 文件及 python/dashboard/scripts/migrations。共 115 文件、598 匹配行，其中代码文件 51 个（含 Svelte）；本说明自身加入后的文档匹配数可能增加。

持久化边 kind 的契约为小写 `conflicts_with`；Python 枚举成员名 `CONFLICTS_WITH` 的值仍为小写。充分性枚举/API 的 `MISSING_INFO` 是独立大写名字，不应改成边 kind。测试里直接写入大写边的通用存储用例测试任意字符串，并非生产冲突写入契约。`seed_demo.py` 中的大写仅是注释/显示文本，不是 SQL 写入值；Svelte 空态里的大写也仅作说明。后端 SQL 与 Svelte 计数已补齐小写读方；交互页面把 sufficiency_status 当状态文本展示。

下表列出全部代码引用文件，测试文件保留为验证读方。

| 文件 | 符号 |
|---|---|
| `bindings/python/bind_05_retrieval.cpp` | `MISSING_INFO`, `sufficiency_status` |
| `bindings/python/bind_13_memory_ops.cpp` | `SummarySourceOverride` |
| `dashboard/web/src/lib/api.ts` | `sufficiency_status` |
| `dashboard/web/src/routes/conflicts/+page.svelte` | `conflicts_with`, `CONFLICTS_WITH` |
| `dashboard/web/src/routes/conflicts/conflicts.test.ts` | `conflicts_with` |
| `dashboard/web/src/routes/interact/+page.svelte` | `sufficiency_status` |
| `include/starling/bus/conflict_key_backfill.hpp` | `conflicts_with` |
| `include/starling/memory/memory_ops.hpp` | `SummarySourceOverride` |
| `include/starling/retrieval/abstention.hpp` | `CONFLICTS_WITH` |
| `include/starling/retrieval/context_pack.hpp` | `CONFLICTS_WITH` |
| `include/starling/retrieval/retrieval_receipt.hpp` | `MISSING_INFO`, `sufficiency_status` |
| `include/starling/store/graph_store.hpp` | `conflicts_with` |
| `migrations/0009_conflict_key_unique.sql` | `conflicts_with` |
| `python/starling/dashboard/engine.py` | `sufficiency_status` |
| `python/starling/dashboard/queries.py` | `conflicts_with` |
| `python/starling/retrieval/__init__.py` | `MISSING_INFO`, `sufficiency_status` |
| `python/starling/retrieval/types.py` | `sufficiency_status`, `MISSING_INFO` |
| `python/starling/schema/enums.py` | `CONFLICTS_WITH`, `conflicts_with` |
| `scripts/seed_demo.py` | `CONFLICTS_WITH` |
| `src/bus/bus.cpp` | `conflicts_with` |
| `src/bus/conflict_key_backfill.cpp` | `conflicts_with` |
| `src/bus/conflict_probe.cpp` | `conflicts_with` |
| `src/retrieval/basic_retriever.cpp` | `sufficiency_status`, `MISSING_INFO` |
| `src/retrieval/pattern_completor.cpp` | `sufficiency_status`, `MISSING_INFO` |
| `src/retrieval/retrieval_planner.cpp` | `sufficiency_status`, `conflicts_with`, `MISSING_INFO` |
| `src/retrieval/semantic_retriever.cpp` | `sufficiency_status`, `MISSING_INFO` |
| `src/store/sqlite_graph_store.cpp` | `conflicts_with` |
| `tests/cpp/test_basic_retriever_filter_predicates.cpp` | `sufficiency_status` |
| `tests/cpp/test_basic_retriever_frontier_filter.cpp` | `sufficiency_status`, `MISSING_INFO` |
| `tests/cpp/test_basic_retriever_multi_holder_reject.cpp` | `sufficiency_status`, `MISSING_INFO` |
| `tests/cpp/test_bus_write_conflict.cpp` | `conflicts_with` |
| `tests/cpp/test_bus_write_supersedes.cpp` | `conflicts_with` |
| `tests/cpp/test_conflict_key_backfill.cpp` | `conflicts_with` |
| `tests/cpp/test_conflict_key_unique.cpp` | `conflicts_with` |
| `tests/cpp/test_converse_phases.cpp` | `SummarySourceOverride` |
| `tests/cpp/test_planner_source_lane.cpp` | `conflicts_with`, `sufficiency_status` |
| `tests/cpp/test_retrieval_planner.cpp` | `sufficiency_status` |
| `tests/cpp/test_store_foundation.cpp` | `CONFLICTS_WITH`, `conflicts_with` |
| `tests/python/test_basic_retrieve_filters.py` | `MISSING_INFO`, `sufficiency_status` |
| `tests/python/test_basic_retrieve_frontier_integration.py` | `sufficiency_status` |
| `tests/python/test_basic_retrieve_receipt.py` | `sufficiency_status`, `MISSING_INFO` |
| `tests/python/test_basic_retrieve_smoke.py` | `sufficiency_status` |
| `tests/python/test_converse_phases_binding.py` | `SummarySourceOverride` |
| `tests/python/test_dashboard_commands.py` | `sufficiency_status` |
| `tests/python/test_dashboard_conflicts.py` | `conflicts_with` |
| `tests/python/test_e2e_severe_conflict.py` | `sufficiency_status`, `conflicts_with`, `MISSING_INFO` |
| `tests/python/test_p1_non_critical.py` | `conflicts_with` |
| `tests/python/test_retrieval_planner_e2e.py` | `sufficiency_status` |
| `tests/python/test_self_pollution_guard.py` | `sufficiency_status` |
| `tests/python/test_tc_conflict_key_unique.py` | `CONFLICTS_WITH`, `conflicts_with` |
| `tests/python/test_tc_new_conflict_severe.py` | `conflicts_with` |

写方包括 bus 冲突检测与回填、graph store 和迁移唯一索引；读方包括 planner、看板 SQL 与前端；SummarySourceOverride 由 C++ 参数和 Python 绑定接收，MemoryCore 透传；充分性回执由各 retriever/planner/pattern completer 写入，绑定、Python 类型、dashboard engine/API 与交互页读取。头文件中的大写冲突字样是文档描述，不是 SQL 字面量。

## 覆盖边界与其他已知问题

本轮没有新评估真实在线冲突探测的准确率、MildContradict 的终态复活、强制关窗导致 pending 丢弃、跨条目证据去重、clear_pending 异常与事务回滚、并发竞争或大规模历史图的性能。已有及新增测试覆盖可重复的局部行为；这些缺口对应上述维护者决策，不能由 BEAM 的零冲突/单 holder 样本外推。

GCC 13 下 main 的 `src/net/http_post_json.cpp:177/293` 因 `HttpResult::attempts` 未在指定初始化器中给出，触发 `-Werror=missing-field-initializers`。该问题与本批无关，本 PR 没有改该文件，建议另开 PR 修复。官方 CI 使用 clang-17，本轮严格按该工具链验证。
