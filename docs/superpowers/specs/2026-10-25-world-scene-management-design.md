# 世界/场景管理：完整设计

日期 2026-10-25。状态：**§8.1 被 `2026-11-03-memory-service-design.md` 取代**；**W1+W2 已实现并合入 main（注册表、新建/切换/删除世界、手动快照与保留策略、`/worlds` 页）；W3 回滚语义 A（丢掉后续）与归档已在 `feat/world-rollback` 分支实现（含前端：C++ `rollback_world` / 归档列出与清理、绑定、`WorldManager.rollback`、`POST /api/worlds/{name}/rollback`、`DELETE /api/worlds/{name}/archives`、`/worlds` 页的回滚与清理归档）；W4（会话清单、每个世界各自配置的按会话 / 按时间自动快照、快照元数据 `sessions_complete` / `session_id` / `content_mark`，含 `/sessions` 页）在 `feat/world-sessions` 分支实现；W5（分叉与语义 B）未实现**。承接 `2026-10-19-form-s-source-lane-design.md` §14 的用户裁定。

**范围**：多世界 Dashboard、快照、回滚（两种语义）、分叉、场景清单。

**不在范围**：感知授权（已在形态 S 落地）、**场景级召回隔离（用户 2026-10-25 明确放弃）**、跨世界检索。

### 0.1 已裁定事项

| 事项 | 决定 | 日期 |
|---|---|---|
| 按场景隔离召回 | **放弃，不做** | 2026-10-25 用户裁定 |
| 默认世界是否搬家 | **不搬**，`dashboard.db` 原地；新世界才进 `worlds/`。理由：不动物理部署；代价是两套布局并存，写进 §2.3 | 2026-10-25 默认值 |
| 快照保留份数 | **5**，清理优先保留 manual；且至少留 1 份 auto 作回滚锚点（N<2 拒绝） | 2026-10-25 默认值 |
| 分片推进 | **W1+W2 先做完停下**，看过效果再决定 W3–W5 | 2026-10-25 默认值 |
| 场景是否是通用概念 | **不是**。陪伴机器人这类真实部署是长时程连续交互，不按场景组织；`session_id` 只当元数据保留，取「一次相遇」，界面统一叫「会话」（§7） | 2026-10-28 用户裁定 |
| 自动快照的触发 | **按会话、按时间两种都做，每个世界单独配置**；默认世界按时间 24 小时、按会话关，新建世界按会话开、按时间关（§4.3） | 2026-10-28 用户裁定 |
| 自动快照失败 | **照常摄入，只记录失败**（日志 + `/api/ingest_status`、`GET /api/worlds` 的计数与最近错误） | 2026-10-28 用户裁定 |

「默认世界是否搬家」「快照保留份数」「分片推进」这三项是按设计默认值定的，不是我替你做的产品决定——任何一项要改，改配置或改分片顺序即可，不影响别处。

**关于"放弃按场景隔离召回"的后果**：`SourceSearchScope` 不加 session 字段，`statements` / `episodic_events` 也无需加场景列。这同时确认了 §12 的两条"未解"仍然是未解（并发场景的精确回滚做不到、右脑无法按场景归属）——放弃场景过滤等于接受这两条长期存在，不再有"给语句加场景列"的改造动机。

---

## 0. 结论

四条可执行的决定：

1. **一个世界 = 一个数据库文件 + 一个租户 id**。快照、回滚、分叉因此全部退化为文件操作，完整性由构造保证，而不是靠"逐表删干净"。
2. **快照用 `VACUUM INTO`**，不搬 2.4GB 的 WAL 库，只写活数据。生产库实测 **57MB / 0.16 秒**。
3. **场景不是召回边界**（沿用形态 S §3.3），它的管理能力建立在 `source_unit_meta.session_id` 上，因此**只对形态 S 落地之后摄入的数据可见**。
4. **"只回滚该场景、保留后续" = 分叉 + 重新摄入原话**，是语义重建不是精确恢复。这一点 §14.2 已经写死，本设计不试图改变它。

---

## 1. 既有事实（实测，非推断）

### 1.1 生产库现状（`~/.starling/dashboard.db`，2026-10-25 01:11）

| 项 | 实测值 |
|---|---|
| 文件大小 | 2.4 GB |
| WAL | 1.4 GB |
| `page_count` | 642,193 |
| `freelist_count` | 627,259（**97.7%** 是空洞） |
| `page_size` | 4096 |
| `VACUUM INTO` 产出 | **57 MB，0.16 秒** |
| `statement_vectors` 占用 | 29 MB |
| engrams / statements | 101 / 3,632 |
| `statements` 中 volatile | 3,290 |
| `bus_events` | 10,514 |
| 租户数 | 1（`default`） |
| `schema_migrations` 最高版本 | **36** |

> 2026-10-28 更新：上表是 10-25 的测量。此后生产库已迁移到 **37**，`source_unit_meta` 有 **1393** 行、**1 个会话**；10-25 时「场景层为空」的说法已经过时。

### 1.2 快照机制实测

在带 1.4GB WAL、97.7% freelist 的活库上实测：

| 场景 | 结果 |
|---|---|
| 有并发读事务时 `VACUUM INTO` | 成功 |
| 在写事务内 `VACUUM INTO` | 失败：`cannot VACUUM from within a transaction` |
| 耗时 | 0.16 秒（这个库） |

结论：快照必须**用独立连接、在事务之外**执行；不能复用引擎那条写连接。

### 1.3 世界状态散布在 32 张表

`src/memory/memory_ops.cpp:800` 的 `clear_tenant` 列出了全部租户作用域表（从 `episodic_events` 到 `replay_ledger`），并有守卫测试 `tests/cpp/test_memory_ops.cpp:539 ClearTenant.CoversEveryTenantScopedTable`——它用 `pragma_table_info` 扫描任何带 `tenant_id` / `window_tenant_id` 列的表，清单漏一张就红。

这张清单是**世界级**操作的现成工具（清空、按世界抽取）。它**不是**场景级工具：没有一张表带场景列，场景信息只在 `source_unit_meta.session_id` 里，而语句（`statements`）无法确定性回溯到场景。

### 1.4 原话 payload 自足

`src/retrieval/source_retriever.cpp:1384` 起的写入路径：每个 `(session_id, 听众集合)` 分组成一个 engram，payload 含说话人、文本、场景、句序、世界时刻、听众、来源；`retention_mode='audit_retain'`，只追加不改写。这是"重新摄入"唯一可依赖的输入。

---

## 2. 核心决策：一个世界 = 一个文件 + 一个租户

### 2.1 决定

```
~/.starling/
  worlds.json                       ← 注册表（唯一权威）
  dashboard.db                      ← 默认世界 default（保持原地，不迁移）
  metrics.db                        ← 默认世界的指标库（现状位置）
  worlds/
    <world-name>/                   ← 新建世界的目录
      world.db
      metrics.db
      snapshots/                    ← 该世界的快照
  ingest-spool/tenant-<encoded>/    ← 现状不变，按租户分区的
```

**不变式**：一个 `world.db` 内只允许存在一个租户 id，且必须等于该世界配置里的租户 id。激活世界时校验；不满足则拒开（`world_tenant_mismatch`）。

> 已实现：C++ `verify_world_tenant`（`src/governance/world_store.cpp`）在切换动旧引擎之前只读扫描所有带 `tenant_id` 列的表，`WHERE tenant_id IS NOT NULL AND tenant_id <> ?` 命中即拒，HTTP 409，状态不前进；库文件不存在（新世界首次激活）时放行。2.6GB 生产库全表扫约 90ms。

### 2.2 为什么不是"一个库、多租户"

现有代码本来就支持多租户共库（`tenant_id` 列），看起来更省事。否决它的理由是**回滚**：

- 多世界共库时，某个世界的快照是整个库的快照（含别的世界）。恢复它等于把别的世界也一起回滚——错的。
- 要只恢复一个世界，就得从快照库里逐表抽取 `tenant_id = W` 的行再搬回来。这正是 §14.2 警告的"逐表按场景删除会漏表"的同构问题，只从场景换成了世界。虽然 `clear_tenant` 的清单加守卫测试把这个风险压低了，但**正确性要靠一张手维护的清单 + 一个测试来保证**，而文件方案**根本不需要这张清单**。
- 世界是安全边界。文件级隔离下，租户过滤写错最多泄漏到"同一个世界内的另一个租户"，而那个世界本来就只有一个租户。

### 2.3 代价（明确列出）

| 代价 | 说明 |
|---|---|
| 多世界 = 多引擎 | 每个活世界有自己的 C++ Runtime / 写连接 / embedder。**只允许一个活世界**（§8），其余是冷文件。 |
| 迁移按文件跑 | 开世界时 `MigrationRunner` 在该文件上跑一次。多世界 = 多份迁移，但这正是隔离的代价。 |
| 默认世界不搬家 | `dashboard.db` 原地保留。新世界才进 `worlds/`。避免动生产部署。 |
| 世界数不该多 | 用户裁定"大多数情况下只有一个世界"。本设计不为上千世界做优化。 |

---

## 3. 注册表 `worlds.json`

### 3.1 形态

```json
{
  "version": 1,
  "default": "default",
  "active": "default",
  "worlds": {
    "default": {
      "tenant": "default",
      "db_path": "/Users/x/.starling/dashboard.db",
      "snapshot_dir": "/Users/x/.starling/worlds/default/snapshots",
      "created_at": "2026-10-17T08:20:46Z",
      "label": "主世界"
    }
  },
  "snapshots": {
    "default": [
      {"id": "20261025T011200Z-a1b2", "path": "/Users/x/.starling/worlds/default/snapshots/20261025T011200Z-a1b2.db",
       "kind": "auto|manual", "reason": "scene_boundary",
       "created_at": "...", "world_time": "2026-10-24T18:03:11Z",
       "sessions_complete": 12, "size_bytes": 60123456, "note": ""}
    ]
  }
}
```

- 权限 0600，写入用 **临时文件 + `os.replace`**（与 `starling.json` 同款原子写，见 `python/starling/dashboard/config.py`）。
- 所有读写都在世界管理锁内，单写者。
- `db_path` **只由注册表给出，永不接受客户端传路径**（§14.2 的硬约束）。

### 3.2 为什么是 JSON 而不是又一个 SQLite

注册表要满足的是：小、低频、单进程、原子、人可读可修。JSON + 原子替换全部满足。用 sidecar SQLite 会引入第二套迁移机制，收益是事务性——但单写者 + 原子替换已经够了。**若将来出现并发管理请求，再换 SQLite**，那时是替换一个模块而不是改全部调用方。

---

## 4. 快照

### 4.1 做法

```
VACUUM INTO '<snapshot_dir>/<id>.db'    -- 独立连接，事务之外
```

- **独立连接**（不能用引擎那条写连接：`VACUUM` 不能在事务内，而引擎连接随时可能在事务里）。
- 得到的是**紧凑、自洽、可直接当世界库打开**的文件，无 WAL、无 freelist。
- 时间戳 `<UTC compact>-<4 hex>`，单调可排序，同名冲突时加后缀。

### 4.2 元数据

每条快照记录这些字段：

| 字段 | 用途 |
|---|---|
| `world_time` | 快照覆盖到的**世界时刻水位**（`MAX(observed_at)` on `source_unit_meta`，为空则 `MAX(created_at)` on `engrams`） |
| `sessions_complete` | 快照时该世界已有的会话数（`COUNT(DISTINCT session_id)` on `source_unit_meta`，按租户；表不存在记 0）。W4 之前的条目没有，HTTP 给 `null` |
| `session_id` | 触发它的会话，只有 `reason="session"` 时非空；其余为 `null` |
| `content_mark` | 内容标记（格式见下），只给自动快照判断「有没有新写入」用，**只在注册表与绑定内部，HTTP 不外露** |
| `size_bytes` | 显示与配额判断 |
| `kind` / `reason` | `manual` / `manual`，或 `auto` / `session` \| `interval` |

`world_time` 是回滚的锚：**"丢掉后续"回滚到哪个快照，就是看它的 `world_time`**。

**`content_mark` 的格式**（W4）：三段依次是 `engrams`、`statements`、`source_unit_meta`，都按 `tenant_id` 过滤，每段 `<前缀><count>:<max_rowid>:<rowid 最大那一行的 id>`，用 `|` 连接，例如 `e3:3:eng-c|s5:5:st-9|u2:2:eng-c:c1`（`source_unit_meta` 的 id 是 `engram_ref || ':' || clause_id`）。计数为 0 时 id 为空（`s0:0:`），表不存在的那段写 `-`，三张表都为空时整个标记是空串。带上 rowid 最大那一行的 id，是为了让「删掉最大 rowid 再插入（rowid 被复用）」和「清空租户后重新摄入到相同行数（rowid 从 1 重来）」也能改变标记。

**标记与会话数在拍出来的快照文件上算**（Codex I-2），在 `VACUUM INTO` 之后、校验 `integrity_check` 的那条只读连接上，与 `world_time` 同一处。快照文件是静止的，所以它们**恰好描述快照里有什么**，不需要任何先后顺序的论证。早先在源库上、`VACUUM INTO` 之前算是错的：autocommit 下每条语句读各自的版本，标记与复制之间提交的删除（`forget`、`clear_memory`）会让快照**少于**标记，三段标记也可能出自不同版本，`unchanged` 与按会话去重因此可能对着一个从未存在过的状态比较。拿当前标记（源库上算）与快照的标记比较，依赖 `VACUUM INTO` 保留 rowid（标记里有 `max_rowid`）。**这是实测、由测试钉住的行为，SQLite 文档并不承诺**：三张表都没有 `INTEGER PRIMARY KEY`，文档允许 `VACUUM` 重排它们的 rowid。2026-10-29 实测：SQLite 3.51.3 上中间与末尾的空洞都原样保留，生产副本上三张表 rowid → id 完全一致；C++ 契约测试 `VacuumIntoKeepsRowidsAcrossAMiddleGapContract` 在真实 schema 上各删一行中间的行，再要求快照标记等于源库标记、按时间与按会话都判 `unchanged`（Codex r2 R2-1）。哪天它坏了，失败方向是多拍（误判有变化：按时间多一份、按会话重试多一份锚点，都受保留份数封顶），不会漏拍；届时标记要换成与 rowid 无关的形式。

**原地更新不改变标记，这是设计如此**：tick 改 activation、forget、archive、approve、置信度更新都是对已有行的 `UPDATE`，计数、最大 rowid 和它的 id 都不变。标记检测的是新增或删除的行，不是编辑。因此一段只有原地编辑的时间，对**按时间**触发算 `unchanged`（不拍），对**按会话**触发的重试去重（§4.3 的 I-1 规则）也算同一个标记。但「没有新摄入」不等于「只有原地编辑」：tick 与 idle replay 会插入新行，见 §4.3 的「稳态」一段（整支审查 M-3）。

### 4.3 触发

| 触发 | 时机 | kind / reason |
|---|---|---|
| 按会话 | 某个 `session_id` 在该世界**第一次写入之前**，先拍快照再写 | auto / session |
| 按时间 | 距上一份自动快照超过 `interval_hours`，且这期间有新增或删除的行（不一定来自摄入，见下方「稳态」） | auto / interval |
| 手动 | Dashboard 按钮 / `POST /api/worlds/{w}/snapshots` | manual / manual |

按会话的快照是「能回滚到任意会话之前」的前提；按时间的快照给长时程连续部署（一个会话一直不结束）一个按天的回滚点。没有它们，手动快照只能回到拍过的点。

**每个世界单独配置**（W4，用户 2026-10-28 裁定）：注册表每个世界条目带 `auto_snapshot: {on_session: bool, interval_hours: int}`，`interval_hours` 取 0–8760，0 表示关闭，越界回 400 `invalid_auto_snapshot`。

- **默认世界**：按时间 24 小时，按会话关。它是日常在用的连续部署。
- **新建世界**：按会话开，按时间关。`POST /api/worlds` 可带 `auto_snapshot` 改掉，之后用 `PUT /api/worlds/{w}/auto_snapshot` 再改。
- 已有注册表里没有这个字段的世界，加载时按上面两条补默认值。

**要不要拍全由 C++ 判**（`take_auto_snapshot`），Python 只取锁、存注册表、记失败：

- 向量后端是 zvec 时直接不拍（`unsupported_backend`），**不计为失败**。
- 按会话：配置关 → 不拍；库里已有这个会话 → `known_session`；注册表里已有一份同一 `session_id`、同一 `content_mark` 的按会话快照 → `unchanged`（**I-1**：钩子在写入之前触发，写入反复失败时会话一直是新的；没有这条，每次重试都拍一份一样的快照，把更早的锚点挤出保留策略）。否则拍一份。
- 按时间：配置为 0 → 不拍；库里没有内容（标记为空）→ `empty`；取最新一份 auto 快照（任意 reason）作 last：没到间隔 → `not_due`，到了但标记与 last 相同 → `unchanged`，否则拍一份。**M-1**：`now` 早于 last 的 `created_at`（时钟回拨，或 last 来自跳到未来的时钟）时，时间条件按「已满足」处理，标记照常比较；last 没有标记（W4 之前的条目）、时间解析不了，也都按需要拍处理。手动快照不参与判断。

**稳态：每个间隔最多一份按时间的快照，由保留份数封顶（默认 5），而不是「空闲时不再拍」**（整支审查 M-3）。标记数的是 `engrams` / `statements` / `source_unit_meta` 的插入与删除；生产的 tick（默认每 30 秒）与 idle replay 不需要新摄入也能插入行——gist 语句经 `Bus::write` 写入（`src/replay/gist_writer.cpp`），推断语句同理（只读查询生产库：10-20 以来有 44 条 `tom_inferred` 语句落在没有 `user_input` 写入的 10 分钟窗口里，来源未进一步确认）。2026-10-28 生产副本上「25 小时后 `unchanged`」是在不跑 tick 的副本上测的，证明不了生产空闲期不拍。所以默认世界（24 小时）最坏每天一份，稳态约 5 × 114 MB ≈ 570 MB，有界、不会无限增长。同一机制也会削弱按会话的重试去重（I-1）：两次重试之间若 tick 插了行，标记就变了，同一会话的重试会再拍一份；这受 `_ingest_max_attempts=5` 限制，并同样受保留份数封顶。

**调用点**：按会话的钩子在摄入线程上调用，位置在 transcript 写进来源通道（`retain_source_turns`）之前、第一次代数检查之后，**不持 `engine._lock`**（钩子取的 `manager.lock` 在锁序上排在它之前）。钩子返回之后，权威的代数检查与 `retain_source_turns` 在同一个 `engine._lock` 块里完成（Codex C1，见 §12）：清空要么在检查之前（job 作废，一行不写），要么在写入之后（刚写的来源行随清空删掉），不会落在两者之间。`retain_source_turns` 只做本地校验与写行（向量行写 pending，由嵌入线程之后补），没有网络调用，持锁的代价与 `remember` 的 prepare / commit 段相同。它只在左脑 lane 开启时才被调用，lane 关闭时没有会话，也就没有按会话的快照，这是设计如此。按时间的触发由 lifespan 起的调度线程 `starling-auto-snapshot` 负责：启动后先等 60 秒，此后每 10 分钟检查一次，关停时停掉。

**后台线程只用非阻塞方式取 `manager.lock`**。取不到（切换、回滚、手动快照正持着它）时：按时间跳过这一轮；按会话把摄入 job 原样放回 spool，**不计重试次数**（这个 job 还什么都没写），下一轮再来；claim 之后若发生过 `clear_memory`，则改进 `discarded/`、不放回（整支审查 I-1：清空只丢弃 spool 根下的 pending job，看不到正在处理的这一个，原样放回会把清空前的会话重新灌回来。比较代数与放回在 `engine._lock` 内，与清空互斥；通用重试分支走同一个判断）。这样切换和回滚不会被后台线程拖成 `ingest_busy`。活动世界按会话关着时（默认世界的默认配置），钩子先不加锁读一次注册表（`worlds.json` 原子替换）就直接返回，不取锁，摄入也就不会因无关的管理操作被推迟；按会话开着或这次读失败，才走上面的非阻塞取锁，在锁内按最新配置重新判（W4 终修 M-3）。

**修剪卡住时自动快照拒绝拍**（Codex I-3、r2 R2-2）：手动快照有人在场，`take_snapshot` 吞掉修剪失败、暂时多一份；自动快照无人值守，最旧的 auto 条目若删不掉（路径越界、解析不了、文件删不掉），每次触发都会多登记一份，一直涨到磁盘守卫。所以 `take_auto_snapshot` 决定要拍之后、拍之前先跑一次修剪：修剪抛了、而快照数仍 ≥ 保留份数，就**不拍、不抛**，返回 `refused`（原因以 `retention cannot prune` 开头，附修剪的错误）和**部分修剪后的注册表**——卡住条目之前排着的正常 victim 已删文件、已摘注册项（修剪逐个「先删文件、再摘注册项」）。Python 先**存下这份注册表**，再按 `snapshot_failed` 计一次失败（`/worlds` 横幅与 `/api/ingest_status` 外露的就是这个码，原因只进日志），摄入照常继续；存注册表本身失败时外层照常记一次失败，没有新快照文件需要补偿。不存的话，`worlds.json` 会留下文件已不在的条目（`/worlds` 列出幽灵快照，回滚到它 fail closed 报 `snapshot_corrupt`），而且每次触发都从旧注册表重来、重演一遍。修剪成功或本来就不超出时照常拍，`take_snapshot` 自己的修剪随后把总数压回保留份数。边界：修剪只在超出保留份数时才有事可做，所以一个恰好在保留份数上、最旧条目卡住的世界还会再拍一份（到保留份数 + 1，这次 `take_snapshot` 的修剪失败被吞掉），之后每次自动触发都拒绝。**这个 R+1 封顶只约束自动触发**：手动快照不变（W2 语义，吞掉修剪失败），条目卡住期间可以把总数推过 R+1。

**失败照常摄入**（用户裁定）：C++ 错误（如 `insufficient_disk`）、注册表存不下来（新快照文件随即删掉）都只计数、记 warning，钩子照常返回，摄入继续。失败计数、最近一次错误码及其时间在 `GET /api/worlds` 顶层的 `auto_snapshot_status: {failures, last_error, last_error_at}`，`/api/ingest_status` 另带 `auto_snapshot_failures` 与 `auto_snapshot_last_error`。计数从进程启动算起，busy 与各种跳过都不算失败。

**关停窗口里仍可能拍出一份按会话的快照**（整支审查 M-5，不修）：lifespan 关停先 `disable_workers()`（停调度线程、禁止再起线程），再 `begin_drain()` 关写门，最后 `stop_ingest_worker()`。`disable_workers` 返回之后、`stop_ingest_worker` 之前，摄入线程仍可能进入钩子拍一份按会话的快照——`VACUUM INTO` 是只读的，写门挡不住它；随后的写入被写门拒绝，job 计一次 attempts 回到 spool。重启后同一会话、同一标记由 I-1 判 `unchanged`，不会重复拍（重启后 tick 若先插了行，按上面的「稳态」至多再多拍一份）。多出的这一份拍在该会话任何写入之前，是合法的锚点。

成本：实测 0.16 秒 / 57MB（当前库）。**快照体积 ≈ 活数据量，不是库文件大小**——57MB 来自 2.4GB 库里 97.7% 是 freelist；库若已被 `VACUUM` 压过，快照与库文件基本同大（实测 65.6MB 的紧凑副本拍出 65.6MB / 282 毫秒）。**代价随记忆量线性增长**，`statement_vectors` 已占 29MB/57MB，是主要增长项。

### 4.4 保留 N 份

- `snapshot_retention` 默认 **5**，可配置（§14.1 的要求）。
- 清理在最旧的非 `manual` 快照上做（手动快照优先保留）；若全是 manual 则按时间清最旧的。
- **清理后必须至少保留最近一份 auto 快照**，否则"丢掉后续"就没有锚点。若 N<2，配置被拒。

### 4.5 磁盘守卫

拍快照前检查：`available < 3 × live_bytes` 时拒绝并回 507（恰好 3 倍放行，与实现和测试一致）。`live_bytes = (page_count - freelist_count) × page_size`，**不是库文件大小，也不是"当前快照体积"**——后者在还没有任何快照时无定义，且会让一个 2.4GB / 97.7% freelist 的库通过一个本该拒绝它的检查。理由：`VACUUM INTO` 失败会留半个文件，且写满磁盘会拖垮主库 WAL。`fs::space` 返回"未知"（`uintmax_t(-1)`，FUSE / 网络文件系统）时跳过守卫。

---

## 5. 回滚：两种语义

### 5.1 语义 A — 丢掉后续（恢复到快照 S）

```
1. 关闭活世界引擎（停 tick、停 ingest worker、close core）
2. 校验 S 的文件可打开、tenant 一致、integrity_check 通过
3. 目标库改名为 <db>.pre-rollback-<ts>（保留，供再回滚）
4. S 复制到目标路径（跨文件系统时先 copy 到同目录再 os.replace）
5. 重建引擎
```

- **完整、确定、零 LLM 成本**。
- 语义是"回到那一刻"，因此 S 之后的一切（包括其他并发场景）一并消失——这正是 §14.2 说的并发场景下的必然结果。
- 旧库不删，改名保留；清理交给显式的"清理回收站"操作，避免一次误操作不可逆。

**已实现（W3，2026-10-26；最终修复轮 2026-10-27）**，与上面的草图有几处不同：

- **归档位置**：`<世界 home>/archive/<库文件名>.pre-rollback-<UTC 时刻>-<4 hex>`，世界 home 是 `<注册表目录>/worlds/<name>`，与新建、删除世界同一推导，`snapshot_dir` 不参与（默认世界即 `worlds/default/archive/`，它的库仍在原地）。建归档、列出、清理之前都先确认 `worlds/` 与世界 home 解析符号链接后仍在注册表目录之下，否则 `snapshot_failed`，世界目录之外什么都不建、不删。布局规则（加固 T1，2026-10-27 用户裁定）：世界目录本身、非默认世界的库文件、`snapshots/` 三者解析符号链接后（含末端悬空的链接）都必须严格位于 `<注册表目录>/worlds/<名>` 之下，否则 `load_registry` / `save_registry` 报 `registry_corrupt` 并写明解析到了哪里；解析出错（EACCES、ELOOP）同样按越界处理。世界目录本身更严（最终修复轮 A1）：解析后必须恰好是解析后的 `worlds/<名>`，不能是指向兄弟世界目录或其子目录的链接（`worlds/sim-1 -> worlds/sim-2` 时删 sim-1 会删掉 sim-2 的整个目录和库）；尚不存在的世界目录照常通过。也就是说，整个注册表目录可以外链（例如 `~/.starling` 是符号链接），世界目录内的子路径不可以指到世界目录之外（指向同一世界目录内别处可以）。「`snapshots/` 是指到另一块盘的符号链接」这种布局不再支持。归档位置仍只由注册表目录推出，不依赖 `snapshot_dir`：`rollback_world` 也可以不经 `load` 直接调用。列出与清理：`GET /api/worlds` 每行的 `archives` / `archive_bytes`，`DELETE /api/worlds/{name}/archives?confirm=<name>`。
- **硬链接归档 + 原子替换**：不改名挪走旧库，而是给它在 `archive/` 里建一个硬链接，再把校验过的副本 `rename` 到库路径。库路径上任何时刻都是一份完整的库，旧 inode 最后只剩归档这一个名字。若进程死在建链接之后、替换之前，`archive/` 里会多一个现役库的第二个名字：列出与清理都跳过它（`fs::equivalent`），下一次回滚开头摘掉它。
- **不写注册表**：快照与世界条目都不变，没有「先动磁盘、后存注册表」的分叉。
- **停引擎之前的预检（Codex 修复轮 2026-10-27）**：`WorldManager.rollback` 在进切换窗口之前先调 C++ `check_rollback`（绑定 `world_check_rollback`）。它只做 `rollback_world` 第 1–4 步的只读检查，顺序与错误码相同：世界、快照、向量后端、快照路径不越出 `snapshot_dir`、快照文件存在且是普通文件、世界 home、2 倍磁盘守卫。它不清扫、不复制、不建目录。不存在的快照 id、缺失的快照文件、zvec 后端、磁盘不足，这几种不改输入重试也过不了，在这里直接拒，活动世界的引擎、后台线程、库文件都不动。`rollback_world` 自己仍会再查一遍，以它为准。
- **别的连接（`world_in_use`）**：建硬链接之前，C++ 新开一条读写连接，`busy_timeout` 0，`PRAGMA locking_mode=EXCLUSIVE; BEGIN EXCLUSIVE`，拿不到锁就拒绝并回 `world_in_use`（409），什么都不改。它看得见哪些连接取决于库的日志模式（在生产库副本上实测）：

  | | WAL | rollback-journal（delete） |
  |---|---|---|
  | 闲置的只读连接（读过） | BUSY | 看不见 |
  | 闲置的读写连接（读过） | BUSY | 看不见 |
  | 开着的读事务 | BUSY | BUSY |
  | 打开了、从没读过 | 看不见 | 看不见 |

  恢复出来的库是 `VACUUM INTO` 的产物，引擎打开它之前一直是 delete 模式（例如回滚过、还没激活过的非活动世界）。所以这个探测只是纵深防御：别的进程（sqlite3 CLI、运维脚本）对 dashboard 管的库开写连接不在支持的使用方式之内；进程内的安全靠 Python 的租约（见 §8.1）、旧 Runtime 弱引用已失效的证明，以及切换遗留 Runtime 的登记。
- **旁车**：`-wal` / `-journal` 必须为空或不存在，`-shm` 必须是普通文件或不存在（大小不论，wal-index 不承载持久数据），否则 `snapshot_failed`。三者在替换之前删掉：它们不带状态，替换随后失败时旧库少了它们照常可用；替换之后再删，会有一段时间旧库的 `-shm` / `-wal` 挂在新库旁边。
- **失败复原**：先把快照复制到库所在目录的隐藏 partial，`integrity_check` 与租户校验针对这份副本；替换之前任何一步失败，目标库原样不动、partial 删掉；替换失败时库仍是旧库，归档名摘掉。要被替换的文件头不是 `SQLite format 3\0` 时拒绝（`snapshot_failed`）。活动世界的旧 Runtime（写连接）回收不了就拒绝（`ingest_busy`），绝不在写连接还开着时替换库文件；每个出口都按原配置重建引擎。替换成功、重建失败时回 `rollback_reopen_failed`（500），仍广播 `world_rolled_back`，前端整页刷新；其余重建失败（库未动）回 `world_open_failed`。两种重建失败都把引擎留 None，下一条**命令**请求（`routes/commands.py` 里惰性建引擎的那些路由：写入、召回、`/api/tick` 等；前端看得见的入口是互动页的写入/召回与「队列」页的「手动 Tick」）按原配置惰性重建，并为它补起 tick（`tick_interval_s>0` 时）与摄入线程（加固 T2，2026-10-27，见 §8.1），不用重启服务。只读路由（检视、`GET /api/worlds`、`/api/config`、`/health`）、`/ws/converse` 与页面加载都不建引擎，所以整页刷新（包括 `rollback_reopen_failed` 之后前端自动的那次）本身不会触发恢复；在命令请求到来之前，`/api/runtime_health`、`/api/ingest_status` 回 503 `engine not initialized`，摄入也停着。zvec 向量后端拒绝（`snapshot_unsupported_backend`，见 §12）。
- **已知限制**：归档是硬链接，归档目录（`<注册表目录>/worlds/<name>/archive`）和库必须在同一个卷上。`STARLING_WORLDS_REGISTRY` 指到另一个卷、default 世界的库还在原卷时，每次回滚 default 世界都会 fail closed，回 `snapshot_failed`，库不动，错误信息写明要把注册表挪到库旁边；别的世界的库在 `worlds/<name>/` 下，不受影响。不退回复制：复制要双倍磁盘和另一套磁盘守卫，而这种配置今天没人部署。

### 5.2 语义 B — 只回滚该场景，保留后续

场景 S 的边界：

```
cut = MAX(observed_at) over source_unit_meta WHERE session_id = S
```

```
1. 找到 world_time ≤ cut 的最近快照 S0（没有 → 拒绝，报 "snapshot too old"）
2. 分叉：从 S0 生成新世界 W'
3. 从**当前世界**（不是 S0）导出所有 min(observed_at) > cut 的场景的原话
4. 重新摄入到 W'：
   - 每个 (session_id, audience) 分组 → 一次 source_turns 摄入
   - 走正常写入路径（重新嵌入、重新 LLM 抽取）
5. W' 成为候选世界，用户确认后才切换
```

三点必须写进 UI：

1. **这是语义重建，不是精确恢复。** 重新抽取会产生不同的语句、不同的置信度、不同的向量。
2. **要花 token。** 每个保留场景重新抽一次。
3. **并发场景可能重叠。** 若 S 与 S' 的世界时刻区间相交，按 `cut` 切分会把 S' 的一部分也切掉。逐场景精确切分在"一个查询、一张语句表、没有场景列"的结构下做不到——这是已知限制，不是待办。

### 5.3 两种语义的取舍

| | 丢掉后续 | 只回滚该场景 |
|---|---|---|
| 成本 | 文件复制 | 文件复制 + 全部保留场景重新抽取 |
| 结果 | 逐字节精确 | 语义重建，右脑不保证一致 |
| 并发场景 | 一并回滚 | 边界模糊 |
| 依赖 | 无 | 原话可重放（形态 S §3.5） |

---

## 6. 分叉

**分叉就是"以某个快照为起点创建一个新世界"**，与语义 B 的步骤 2 是同一个操作，因此先实现分叉，语义 B 只是它上面加一次重新摄入。

```
POST /api/worlds/{w}/fork {snapshot_id, name}
→ 复制快照文件到 worlds/<name>/world.db
→ 注册新世界（tenant 沿用来源租户，见下）
→ 打开新世界跑迁移（快照已是当前版本，应为 no-op）
```

**租户 id 的决定：分叉时保留来源租户 id 不变。**

曾经考虑让新世界用自己的名字作 tenant id，被否决：那需要把快照里所有 `tenant_id` 行改写成新值，而 `statements.tenant_id` 是**不可变字段**（迁移 0007 的触发器会 ABORT），绕过它要么禁触发器要么直改表，两条路都在破坏不变量。

因此 §2.1 的不变式要精确表述：*一个世界文件里**至多**一个租户 id；但同一个租户 id 可以出现在多个世界文件里（分叉后代）。*

代价是 §8.2 那条：同一租户的两个世界共用 spool 分区。

---

## 7. 会话清单

**场景不是通用概念**（用户 2026-10-28 裁定）。陪伴机器人这类真实部署是长时程的连续交互，不需要按场景组织。`session_id` 只当作元数据保留，取「一次相遇」最自然；它不切断召回（2026-10-25 已裁定）。界面与 API 统一叫「会话」，不叫「场景」。下文其余章节里的「场景」都按「会话」读。

数据来源只有一处：`source_unit_meta`。实现在 C++ `list_sessions`（`src/governance/world_store.cpp`），接口 `GET /api/worlds/{w}/sessions?limit=200`（`limit` 取 1–1000，越界 422）。

```sql
SELECT session_id,
       COUNT(*)                    AS units,
       MIN(observed_at)            AS first_at,
       MAX(observed_at)            AS last_at,
       COUNT(DISTINCT speaker)     AS speakers
  FROM source_unit_meta
 WHERE tenant_id = ?1
 GROUP BY session_id
 ORDER BY first_at DESC
```

实际的排序是 `first_at` 为空的排最后、同值按 `session_id` 升序；空串的 `observed_at` 与 NULL 一样不算时间。每行另带 `anchor`：注册表里该会话最新一份按会话快照的 `{snapshot_id, created_at}`，没有时为 `null`。`first_at` / `last_at` 全为 NULL 时为 `null`。响应顶层带 `total`（会话总数，不受 `limit` 影响）、`null_observed`（无时间的 unit 数）、`truncated`（`total > limit`）和 `source_lane_enabled`（取当前 config，对非活动世界也是：它表示新写入会不会产生会话）。路由持引擎租约只读打开库，切换窗口内回 503 `world_switching`。

| 限制 | 状态 |
|---|---|
| 只覆盖形态 S 之后的数据 | 已知。生产库 2026-10-28 有 1393 个 unit、1 个会话 |
| 没有会话列的表无法归属 | 已知。`statements` 无法确定性地回溯到会话——因此删除一个会话**只能**走快照回滚 |
| 会话名来自调用方 | `session_id` 是可信后端的断言（信任模型 A），Starling 不验证，只展示 |
| 无 `observed_at` 的 unit | `observed_at IS NULL` 的 unit 不参与召回（设计如此），会话清单里单独计数（`null_observed`）但不参与 `world_time` 水位 |
| lane 关闭 | 不写 `source_unit_meta`，新写入不产生会话，也不触发按会话的快照；页面据 `source_lane_enabled` 提示 |

---

## 8. 世界切换与引擎生命周期

### 8.1 只允许一个活世界

`app.state.engine` 是单引擎。切换世界 = 换引擎，不是并存。

```
activate(world):                          # 实现：python/starling/dashboard/world_manager.py
  with manager.lock:
     switching = True                     # 此后新租约一律拒绝 → 503 world_switching
     verify_world_tenant(world)           # C++：库内只有该世界的租户，否则 world_tenant_mismatch
     quiesce(engine)                      # 停 tick / 摄入线程（不持引擎锁 join，两个线程合计 5 秒）；
                                          #   停不下 → 恢复线程、ingest_busy
     drain_leases(timeout=30s)            # 等在途请求的租约归零；超时 → 恢复线程、ingest_busy
     quiesce(engine)                      # 再停一次此刻的引擎：等租约期间在途的惰性重建可能写回了
                                          #   引擎并补起线程（B1）；停不下 → 恢复线程、ingest_busy
     old = engine; engine = None
     drop(old); gc.collect()              # 旧 Runtime 必须被回收：close() 不释放 C++ 写连接
     new = DashboardEngine(cfg_of(world)) # 开新库；失败 → 回滚到旧世界，world_open_failed
     save(registry.active = world)        # 先写注册表；失败 → 丢新引擎、回滚，world_open_failed
     app.state.config, app.state.engine = cfg_of(world), new
     start_workers(new)
     switching = False                    # 任何出口（成功或失败）都清标志
```

- **引擎租约**：每个用到引擎的请求从头到尾持一份租约——线程池排队、`remember` 的锁外抽取、`/ws/converse` 的锁外生成、最后的提交都在租约内。只等 `old._lock` 不够：锁外阶段不持引擎锁，请求会在切换后写进旧世界，旧 Runtime 也一直活着，切回原世界就是同一个库上两条写连接。
- 租约按请求发放，不按 WebSocket 连接；后台 tick / 摄入线程不是租约，由 `quiesce` 停：两个线程的 join 串行，合计上限 5 秒（先停的用掉多少，后停的就只剩多少），超时回 `ingest_busy`。
- 任何一步失败，注册表 `active`、`app.state.config`、`app.state.engine` 三者都留在旧世界，不分叉。
- **失败后惰性重建补起后台线程（加固 T2，2026-10-27）**：两条失败路径会把运行中的引擎丢成 None：切换时新世界打不开、回到旧世界也打不开（`world_open_failed`），以及回滚活动世界之后重开失败（`rollback_reopen_failed`，或库未动的 `world_open_failed`）。这两处置 `_workers_lost`；下一条**命令**请求（只有命令路由的租约带 `build=`）在 `_build_lock` 内真正新建引擎，先按 `workers_enabled` 起 tick（`tick_interval_s>0` 时）与摄入线程、清位，再写回引擎（整支终审 I1：先写回的话，切换的第一次 quiesce 可能读到正在 `Thread.start()` 里的引擎，join 一个没启动的线程抛 `RuntimeError`，切换回 500，线程随后退出、句柄却留着，静默停摆；引擎层的 `start_*` 也改为 `start()` 返回之后才挂句柄）。只读路由（检视、`GET /api/worlds`、`/api/config`）、`/ws/converse` 与页面加载都不惰性建引擎，所以刷新页面不会触发恢复，要等下一条命令请求（互动页的写入/召回、「队列」页的「手动 Tick」）；在那之前 `/api/runtime_health`、`/api/ingest_status` 回 503。拿到已有引擎的租约不碰它；成功的切换和回滚发布新引擎时已起过线程，同样清位。起线程抛错不让该请求失败：记日志、保持置位，下一次真正新建引擎时再试。部分失败（tick 已起、摄入线程抛错）时，已起的线程照常运行，这个引擎在下一次成功的切换、回滚或重启之前都没有摄入线程：拿到已有引擎的租约不碰标志，所以不会对它重试；同样的失败发生在 lifespan 启动时会直接让启动失败，这里不比现状差。切换或回滚开始时若有惰性重建正在进行，第一次 quiesce 读到的是 None；重建在切换等租约期间补起线程并写回引擎，所以 `_quiesce_and_drain` 等完租约之后对此刻的引擎无条件再 quiesce 一次（停不下同样回 `ingest_busy`），切换接手的是已停好线程的引擎。启动语义不变：`tick_interval_s=0` 且启动时没有引擎，首个请求惰性建出的引擎不起摄入线程（标志从未置位）。lifespan 关停调 `WorldManager.disable_workers()`：在 `manager.lock` 内再取 `_build_lock` 才关 `workers_enabled`（锁序不变），进行中的惰性构建连同补起线程先收尾，它写回的引擎由关停段按 `app.state.engine` 停掉；此后的惰性构建不再起线程。
- 锁顺序见 `world_manager.py` 模块文档：`manager.lock → _gate → _build_lock → engine._lock → supervisor`，持租约的代码不得去拿 `manager.lock`。
- **打开活动世界库的路径都持租约**：inspect 的 SQL 路由（按 `app.state.config` 只读打开库）和 `GET /api/worlds`（`world_stats` 只读打开每个世界的库）从打开库之前到关闭之后都持一份租约（不惰性建引擎），窗口内回 503 `world_switching`。只读也要持：回滚替换的是同一路径上的 inode，一条在替换之前打开了旧 inode、替换之后才第一次读的只读连接，会按路径打开新库的 `-shm`；SQLite 的进程内锁记录按 inode 区分，同进程的 fcntl 锁互不冲突，它会认为自己是第一个连接，把引擎正在用的 wal-index 截断重建（实测 iChange 从 5 变 0）。C++ 的 `world_in_use` 探测看不见这种「打开了还没读」的连接，所以靠租约：回滚等租约归零才动文件。
- 已接受的限制：回滚**非活动**世界不进切换窗口、不等租约，可能与 `GET /api/worlds` 对那个世界的 `world_stats` 交错，`world_stats` 可能读到一次旧 inode，或重置新库的 `-shm`。那个库上没有写连接，所以无害。

### 8.2 metrics.db 与 spool

- `metrics.db` 是 `db_path` 的兄弟（`engine.py:238`），所以按世界自动分开——**前提是新世界有自己的目录**（§2.1 的布局满足）。
- spool 已经按租户分区（`tenant_spool_dir`），世界切换后自动指向正确分区。默认世界与分叉世界若共用 tenant id（§6），**spool 分区会共用**——这是分叉的已知副作用，写进限制里。分叉出的世界应当**暂停摄入**直到用户显式确认要继续。

---

## 9. Dashboard 面

新增导航组「世界 · 会话」，排在「总览」之后、「原始数据 · 证据」之前（世界是比 engram 更上游的划分）。**注意**：`dashboard/web/src/lib/nav.test.ts:8` 断言了 11 个组的精确标题列表，加组必须同步改测试。

| 页面 | 内容 |
|---|---|
| `/worlds` | 世界列表：名称、路径、大小、会话数、语句数、最后活动、是否当前；操作：新建、切换、快照、分叉、删除 |
| `/worlds`（W4 起） | 每个世界的自动快照设置（按会话开关、按时间间隔），新建世界时也可设；快照清单显示 reason、`sessions_complete`、触发它的会话；顶部显示自动快照失败计数与最近错误 |
| `/sessions` | 会话清单（W4）：可选世界，默认活动世界；每个会话的 unit 数、说话人数、时间区间与回滚锚点 |

危险操作（删除世界、回滚）要求**手输世界名确认**——沿用 `clear_memory` 的 `confirm` 范式（`engine.py:626`），它有现成的 400 `confirm_mismatch` 语义。

API：

```
GET    /api/worlds                       列表（注册表 + 每个世界一行统计 + auto_snapshot；顶层 auto_snapshot_status）
POST   /api/worlds                       新建 {name, label, auto_snapshot?}
PUT    /api/worlds/{w}/auto_snapshot     改自动快照配置 {on_session, interval_hours}
POST   /api/worlds/{w}/activate          切换
DELETE /api/worlds/{w}                   删除（须 confirm，且不能删 default 或 active）
GET    /api/worlds/{w}/snapshots         快照列表
POST   /api/worlds/{w}/snapshots         手动快照 {note}
DELETE /api/worlds/{w}/snapshots/{id}    删除快照
POST   /api/worlds/{w}/rollback          回滚 {mode: "discard_after"|"scene", cutoff|snapshot_id, confirm}
DELETE /api/worlds/{w}/archives?confirm= 清理该世界的全部回滚归档（须 confirm=<w>）
POST   /api/worlds/{w}/fork              分叉 {snapshot_id, name}
GET    /api/worlds/{w}/sessions?limit=   会话清单（limit 1–1000，默认 200）
```

统计数字从各世界库只读连接查（`mode=ro`，与 `engine.py:391` 的既有做法一致），**不打开引擎**——否则列 5 个世界就要建 5 个引擎。

---

## 10. 落点（C++/Python 边界裁定）

按仓库硬规则"换一种绑定语言是否需要重写该逻辑"判定：

| 逻辑 | 落点 | 理由 |
|---|---|---|
| 快照（`VACUUM INTO` + 校验 + 元数据） | **C++** `src/governance/world_store.cpp` | 换语言要重写：连接管理、一致性、错误分类 |
| 回滚的顺序与原子性 | **C++** 同上 | 状态机语义 |
| 保留 N 份的裁剪策略 | **C++** 同上 | 策略，不是适配 |
| 会话清单查询 | **C++** 同上 | 单一权威 SQL |
| 自动快照要不要拍（两种触发、内容标记、I-1 / M-1） | **C++** 同上（`take_auto_snapshot`） | 判断规则，不是适配 |
| 自动快照的取锁、存注册表、失败计数、调度线程、摄入钩子 | **Python** `dashboard/world_manager.py`、`engine.py` | 锁与线程是 Python 侧的引擎生命周期 |
| 注册表读写（JSON） | **C++**（`nlohmann::json` 已用） | 同一份逻辑换语言要重写 |
| 世界切换时引擎重建 | **Python** `dashboard/world_manager.py` | 它调的就是 Python 侧的引擎生命周期；没有绑定语言能共享 |
| 路由 | Python | 适配 |
| 页面 | SvelteKit | 前端 |

**不落 C++ 的部分**要说明白：引擎重建本质是 Python 对象生命周期管理，不属于核心语义。

新增迁移：无。注册表是文件，快照是文件，不碰 schema。

---

## 11. 分片与验收

每片独立可合，测试确定性（无 LLM 依赖，嵌入用 stub）。

| 片 | 内容 | 验收 |
|---|---|---|
| W1 | 注册表 + 新建/列出/切换/删除世界 + `/worlds` 页 | 单测：注册表原子写、路径校验（拒相对路径/越界路径）、tenant 不变式；手动：新建 worlds 里点一遍 |
| W2 | 快照（手动）+ 列表 + 删除 + 保留 N | 单测：`VACUUM INTO` 产物可打开且 `integrity_check=ok`、`world_time` 正确、裁剪按 N 且保留至少 1 份 auto、磁盘守卫；实测：在生产库副本上拍，比对 57MB 量级 |
| W3 | 回滚语义 A（丢掉后续） | 单测：回滚后语句数等于快照时的数、旧库被改名保留、失败时目标库不被破坏（用损坏快照 + 只读目录两条路测） |
| W4 | 会话清单 + 每个世界各自配置的按会话 / 按时间自动快照 + 快照元数据（`sessions_complete`、`session_id`、`content_mark`）+ `/sessions` 页 | 单测：新会话的摄入 job 在写入前拍一份 `reason="session"` 快照、快照里没有这个会话，同一会话不再拍；取不到管理锁时 job 回 spool 且不计重试；快照失败照常摄入、失败计数可见；按时间触发按注入时钟拍或不拍（到期且有新写入才拍）；会话清单的 `first_at/last_at`、锚点与写入一致，无时间、无锚点给 `null`；`content_mark` 不出现在任何 HTTP 响应里 |
| W5 | 分叉 + 回滚语义 B（重新摄入） | 单测：分叉后两库互不影响（写一个不动另一个）、tenant id 相同的不变式例外成立；语义 B 用 stub LLM 验证"原话被重新提交"，不验证内容一致 |

**预注册**：W1–W5 都是功能交付，不是基准增益，验收判据是上面的确定性测试。凡涉及评测数字的（如重新摄入后的召回质量）都属于新的评测任务，需要单独预注册。

---

## 12. 风险与未解

| 风险 | 处置 |
|---|---|
| 快照体积随记忆线性增长 | 保留 N 默认 5；`/worlds` 显示当前快照总量；磁盘守卫。按时间触发在空闲期也可能每个间隔拍一份（tick / idle replay 会插入行，§4.3「稳态」），默认世界稳态约 5 × 114 MB ≈ 570 MB |
| 回滚窗口由保留份数与触发频率共同决定 | 保留 N 份（默认 5）：按会话触发时窗口约是最近 N 个会话，按时间触发时约是 N × `interval_hours`（默认世界约 5 天），两种都开时更短。窗口之外显式报 `snapshot too old`，不静默回退到更早的快照；要更长的窗口就调大保留份数或间隔 |
| 清空与来源写入的竞态（Codex C1，W4 之前就有，W4 修掉） | W4 的基线 b5f43c7 上 `_retain_transcript_turns` 已经是「持锁查代数 → 放锁 → 锁外 `retain_source_turns`」：`clear_memory` 若落在两者之间（持 `engine._lock` 清库、前移代数、丢弃 pending job），清空前的 transcript 会在清空之后写回来源表；W4 在钩子之后加的第二次检查有同样的缺口。修法：钩子照旧在锁外调用，钩子返回之后代数检查与 `retain_source_turns` 在同一个 `engine._lock` 块里完成（§4.3「调用点」）。锁序不变（钩子的 `manager.lock` 非阻塞、已放开 → `engine._lock`），没有新边 |
| 升级后第一轮会给默认世界拍一份快照 | 预期行为：默认世界按时间触发、库里有内容、又还没有自动快照，调度线程启动后约 60 秒的第一轮就会拍（2026-10-28 在生产副本上实测：114 MB、0.38 秒；同一副本上 `list_sessions` 2 毫秒）。写进 PR 描述 |
| 降级到 W3 的程序 | W3 保存注册表时会丢掉自定义的 `auto_snapshot` 配置和快照条目上的三个新字段（`sessions_complete`、`session_id`、`content_mark`）。再升级回来，这些世界回到默认配置（§4.3 的默认值）；被 W3 存过的快照按 W4 之前的条目对待：两个外露字段为 `null`，按时间触发视为需要拍，按会话的快照丢了 `session_id`，会话清单里不再显示为锚点。方向是安全的：快照文件与 kind / reason 都在，仍可回滚，也不会少拍 |
| zvec 向量后端（默认关闭）向量在 SQLite 之外 | **分片内 fail loud**：`vector_backend=zvec` 时快照/回滚直接拒绝（`snapshot_unsupported_backend`），不做半截快照 |
| 分叉共用 tenant id → 共用 spool 分区 | 分叉后新世界**暂停摄入**，UI 提示"世界 <name> 与 <parent> 共用摄入分区" |
| 生产库 97.7% 是 freelist | W2 上线时顺带建议一次 `VACUUM`（离线）；不做成自动，因为 2.4GB 的 VACUUM 需要独占 |
| `VACUUM INTO` 失败留半个文件 | 先写 `<id>.db.partial` 再 `os.replace`；失败则删 partial |
| 多世界 = 多份迁移历史 | 每个世界库自带 `schema_migrations`；版本不一致时拒绝激活（报 `world_schema_behind`），提示先升级 |
| 世界目录里指到外部的符号链接，让快照写入、残留清扫、保留策略、删快照、建库落到世界目录之外 | 布局约束在解析符号链接之后判断（§5.1 归档段）：世界目录解析后恰好是 `worlds/<名>`，非默认世界的库、`snapshots/` 解析后都必须严格在 `worlds/<名>` 之下，`load` / `save` 都报 `registry_corrupt`。注册表目录本身可以外链，世界目录内的子路径不可以。只在加载和保存时判定（每个操作都在锁内重新 `load`）；不经 `load` 的 C++ 破坏性入口（`delete_world`，以及回滚、列出、清理归档共用的世界 home 检查）自己再查一遍世界目录恰好是 `worlds/<名>`，否则 `snapshot_failed`（整支终审 M1）。**违反时 dashboard 拒绝启动**：`create_app` 里的 `bootstrap_worlds` 加载注册表即抛 `registry_corrupt`，不只是世界操作失败；W1–W3 下能加载、现在不再支持的布局（`snapshots/` 或 `world.db` 外链、世界目录外链或互为别名）升级后就会这样。消息写明字段、原路径、解析到的位置，并附补救提示（世界目录内的路径不能是指向外部的符号链接；要换盘请移动整个注册表目录）。`load` 与操作之间被人改文件系统的 TOCTOU 不在防护范围内，对方已有世界目录的写权限，能直接改库 |

**未解**（不假装解决了）：

1. **并发场景的精确回滚**做不到——没有场景列，只能按世界时刻切。要解决得先给语句加场景归属，那是另一个设计。
2. **重新摄入的语义一致性**无法保证。形态 S §3.5 已写明：这是重建不是恢复。
3. **默认世界迁不迁目录**：本设计选"不迁"（`dashboard.db` 原地），代价是默认世界与新世界布局不一致。若将来要统一，需要一个搬运世界的操作——本设计不做。
