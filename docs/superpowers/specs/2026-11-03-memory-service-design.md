# Starling 记忆服务：多世界同时运行、客户端、分叉与清理

日期 2026-11-03。状态：设计已与用户逐段确认，待实施计划。

需求来源是 rise（RismForge）一侧写的需求文档《Starling 记忆服务：rise 一侧的需求》（rise 仓库 `docs/superpowers/specs/2026-11-02-starling-service-requirements.md`，按 2026-11-03「一个模拟世界一个 Starling 世界」那一版，下文简称「需求」，引用按它的节号，如需求 5.3）。本文是 Starling 一侧的回答：做成什么样、核心逻辑放在哪一层、分几期做。

承接 `2026-10-25-world-scene-management-design.md`（下文简称「世界设计」）。世界设计 §8.1「只允许一个活世界」被本文推翻；它的「一个世界 = 一个库文件 + 一个租户」、快照与回滚语义 A、分叉保留来源租户这几条照旧成立。

## 1. 为什么做

rise 今天在自己进程里 `starling.Memory.open(path)`，一个人格一个库文件。这些库不在 Starling 的注册表里，Starling 的看板看不到、管不了。用户要在 Starling 里查看、管理所有 agent 的记忆，包括训练时每条 rollout 的临时世界：还在跑时实时看，结束后留着复盘。

rise 同时把对应关系改成「一个模拟世界一个 Starling 世界，人格是世界里的 holder」（需求 5.2）。所以 Starling 要变成一个能同时跑多个世界、每个世界里有多个 holder 同时读写的服务，rise 用 Starling 提供的客户端连它，不再自己打开库文件。

## 2. 目标、非目标、反目标

**目标**

- 一个服务进程同时打开多个世界，每个世界各有引擎，同时读写，互不阻塞。
- 一个世界能同时开多个句柄，每个句柄绑定一个 holder。
- rise 用纯 Python 客户端连服务，方法签名与返回形状和 `starling.Memory` 一致。
- 分叉、快照、恢复、只读世界、标签、句柄、自动清理，满足需求 §5 的全部「必须」项和 §7 的验收。
- 看板能直接切到任何一个世界查看，正被 rise 写着的世界也一样。

**非目标**

- 不换机器：只用回环地址，不做 TLS、跨机传文件（需求 §3）。
- 不改记忆语义：抽取、嵌入、召回、摘要算法照旧。不改感知语义（需求 5.2 末段：每个 holder 只记自己名下的事件）。
- 不导入 rise 现有的旧库（需求 5.3，rise 已定）。
- 不做按单个 holder 的快照、恢复、删除（用户 2026-11-03 裁定，需求 5.2 也写明只做到世界一级）。
- 服务端不内置桩模式（需求 §7 第 6 条）。测试用子进程里的本机 OpenAI 兼容假端点。

**反目标**（技术上能跑，但这样就算失败）

- 核心逻辑在 Python 里实现。换一种绑定语言就要重写的逻辑一律放 C++（仓库硬规则，见 `CLAUDE.md`「架构边界」）。
- 种子或同组 rollout 世界的摘要被后台任务、看板打开、迁移悄悄改掉。
- 恢复出半新半旧的库；或者世界里还有句柄开着时恢复成功。
- 自动清理删掉了 rise 拍的快照、种子、部署世界或正开着的世界。
- 看板「切换世界」影响到服务端别的世界，或影响别人正在看的页面。
- 同一轮发言写进两个 holder 名下时，第二个 holder 静默拿到第一个 holder 的语句。
- 异常文字、日志或 HTTP 响应里出现密钥、端点地址。

## 3. 已定的决定

| # | 决定 | 来源 |
|---|---|---|
| D1 | 只有一种服务进程：把现有看板服务扩展成多世界。rise 配了地址就连它（可以就是用户的 8787），没配就自己拉起一个（数据目录、端口由 rise 给）。不管哪种，rise 的世界都在那个服务的注册表里，看板能看到、能点进去。 | 用户 |
| D2 | 多个世界同时运行（多个引擎同时工作）。注册表有一个默认世界。**没有「活动世界」**：看板上直接切到哪个世界就看哪个。 | 用户 |
| D3 | 一个模拟世界一个 Starling 世界，人格是其中的 holder：每个情境一个部署世界；每条 rollout 从该情境的种子分叉一个世界，训练位与对手位都是 holder；验证从部署世界的快照分叉临时世界。 | 用户、需求 5.2 |
| D4 | Starling 一侧统一说 holder。「人格」是 rise 的概念，rise 的 `persona_id` 就是世界租户下的一个 holder。 | 用户 |
| D5 | 快照、恢复、摘要、`pending_events`、`embed_pending`、`next_seq` 都作用于整个世界，可以从任一句柄或管理接口发起。不做按 holder 的局部快照与恢复。恢复要求世界里所有句柄都已关闭，世界里所有 holder 一起回到快照时的状态。 | 用户（2026-11-03 两次确认）、需求 5.2、5.4 |
| D6 | 事件 key 按 holder 区分：同一个 key 可以在不同 holder 名下各有一条事件。 | 用户确认；需求 §7 第 1a 条的前提 |
| D7 | 后台任务（tick、摄入、自动快照）是每个世界自己的开关，建世界、分叉时调用方可以指定；不指定就与默认世界的行为一致。Starling 不替 rise 默认关掉。 | 用户 |
| D8 | 抽取与嵌入端点在服务启动时配置，归默认世界。新世界默认继承默认世界的配置，**建世界时复制一份**，之后改默认世界的配置，已有世界不跟着变；客户端显式给了配置就用客户端的。 | 用户 |
| D9 | 种子是普通世界加只读标记（答复需求 §8 的待定项）。 | 用户确认 |
| D10 | 分叉、调用方持有的快照、按分组的自动清理、注册表改存 SQLite（§6–§9 的形状）。 | 用户确认 |
| D11 | 看板：顶栏选世界、按世界寻址的页面与 API、按需打开、实时推送、按标签过滤与分页。 | 用户确认 |
| D12 | 核心逻辑放 C++：注册表、标签、设置合并、分叉、快照、只读、事件、句柄与租约状态机、空闲判定、清理规则、错误码目录。Python 只做 HTTP 转发与引擎对象的建停；客户端只做 HTTP 转发与异常映射。 | 用户 |

**D5、D6 的来历**：设计讨论中曾定过「`pending_events`、`next_seq`、`digest` 都有按 holder 的版本」。随后 rise 更新需求（2026-11-03 版需求 5.2），写明这几项按整个世界做，并在需求 §7 第 1a 条要求「世界的摘要反映两者的写入；`pending_events` 列出两者的事件」。本文按更新后的需求：只保留 D6（事件 key 按 holder 区分），其余按世界。

## 4. 世界与 holder

### 4.1 映射

- 一个世界 = 一个 SQLite 文件 + 一个租户 id（世界设计 §2.1 不变）。分叉出来的世界保留来源的租户 id。
- 一个世界里可以有多个 holder。语句表已有 `holder_id` 列。客户端 `open(world, holder)` 得到一个句柄，句柄上的 `recall`、`remember_event`、`abandon_event` 以这个 holder 的身份做，等价于今天的 `Memory.open(path, agent=holder)`；`digest`、`pending_events`、`embed_pending`、`next_seq` 作用于整个世界（D5）。
- 同一个世界的所有句柄共用一个引擎，写入由引擎串行化（需求 5.2）。不提供跨句柄的事务。

### 4.2 现状的缺口

只有 `statements` 带 `holder_id`。`engrams`、`events` 只按租户划分。一个世界里放多个 holder 之后有两处错：

1. **事件撞键**。`events` 的主键是 `(tenant_id, key)`（`migrations/0038_memory_events.sql`）。rise 的事件 key 是 `<episode_id>#<turn_index>`，文本是 `说话人：发言`；一个实例记住所有别人的发言（rise `src/actio/instance.py` 的 `_remember_new`），所以三方对话里 A 和 C 都会写 B 的同一轮，同 key 同文本。C 后写时被当成重入，直接拿到 A 名下的语句 id，C 一条语句都没有，不报错。
2. **原文共用导致时间错位**。`remember_event` 的原文是 `[key] text`，engram 按 `source_prefix + sha256(payload)` 去重（`src/memory/memory_ops.cpp:69`），不含 holder。两个 holder 写同一段话共用一个 engram，而 engram 的 `created_at` 是 `epoch + 第一个写入者的 seq`（`src/memory/memory_events.cpp:273`），语句的 `observed_at` 与 `source_time` 又取自 engram 的 `created_at`（`src/extractor/extractor.cpp:749`、`754`）。结果是后写的 holder 的语句带的是先写者的时刻，摘要还取决于谁先写。

### 4.3 改法（C++）

- **迁移 `0039`**：`events` 加 `holder_id TEXT NOT NULL DEFAULT ''`，主键改为 `(tenant_id, holder_id, key)`。`seq` 的唯一约束保持 `(tenant_id, seq)`：`seq` 是世界一级的，`next_seq()` 也是世界一级的（D5），rise 的事件时钟（`EPOCH + next_seq()`）在一个世界里单调递增。SQLite 不能直接改主键，迁移按「建新表、复制、删旧表、改名」做，在一个事务里。
  - 旧行回填：从 `statement_ids_json` 指向的语句取 `holder_id`；没有语句的行（pending、abandoned、零语句的 committed）回填 `''`。
  - **认领规则**（我的判断）：`remember_event(holder=H, key=K)` 找不到 `(H, K)` 时，若有 `('', K)` 的旧行，就把它的 `holder_id` 改成 `H` 再按原状态机处理；`abandon_event` 同理。旧库都是一库一 holder（今天的门面用法），所以第一个碰到它的 holder 就是它的主人。实施时在一份旧格式的库上钉住这条。
- **原文按 holder 分开**：`remember_event` 生成 engram 时，来源身份（`source_item_id` 前缀）带上 holder，同一段话写进两个 holder 名下就是两个 engram，各自的 `created_at` 取各自事件的 `seq`。`remember_event` 重试 pending 行时的「补齐分支」按这个来源身份找 engram，不能只按 `content_hash`（同内容现在可以有多个 engram）。
  - 摘要不受影响：摘要里 engram 引用换成 engram 的 `content_hash`（内容哈希），不含来源身份（`src/memory/memory_digest.cpp`）。
- **按世界的查询**：`pending_events()` 返回全世界的 pending 事件，每项增加 `holder` 字段（`PendingEvent` 多一个字段，原有字段不变），rise 据此在对应 holder 的句柄上 `abandon_event`。`next_seq()`、`digest()`、`embed_pending()` 都按世界，与今天按租户的实现一致，不用改。
- **摘要会变一次**：`events` 多了 `holder_id` 列，摘要默认纳入所有没被排除的列（`memory_digest.cpp:35-42`），所以有事件行的旧库在 0039 之后摘要会变。可以接受：rise 不导入旧库（需求 5.3），8787 的默认世界没有事件行（实测 0 行）；进程内 `Memory` 与服务走同一份 C++，需求 §7 第 6 条的一致性不受影响。
- 被这次改动碰到的绑定按现有规矩释放 GIL（§12）。

## 5. 注册表改存 SQLite

### 5.1 为什么换

需求 5.7：注册表会有「每个 run 200 份 × 同时保留的 run 数」个世界，每条 rollout 开始时还要分叉一次。今天的 `worlds.json` 每次改动都整文件重写，并持有全局管理锁；`GET /api/worlds` 逐个打开库取统计。几百个世界时两者都撑不住。

### 5.2 形状

注册表是数据目录下的 `registry.db`（SQLite，WAL 模式），由 C++ 独占读写，替换现有 `world_store` 读写 `worlds.json` 的那部分。表（列名是初稿，实施时可调）：

- `worlds`：`name`（主键）、`dir_id`（世界目录名，见 §5.3）、`tenant`、`db_path`、`label`、`created_at`、`seed`、`read_only`、`reclaimable`、`retention_group`（可空）、`fork_of_world`、`fork_of_snapshot`、`settings_json`（§8）、统计缓存列（`holders`、`statements`、`last_write_at`、`stats_at`）。
- `world_labels`：`(name, key, value)`，按 `(key, value)` 建索引。**建世界或分叉时一次写入，之后不能改**（需求 5.11）。
- `snapshots`：与今天 `SnapshotEntry` 同字段，`kind` 增加 `held`（§7）。
- `meta`：注册表 schema 版本、默认世界名。
- `cleanup_log`：最近的自动清理记录（§9）。

### 5.3 世界名与目录

- 世界名由 rise 生成，「仓库前缀 + 种类 + id」，字符集 `[A-Za-z0-9._-]`（需求 5.2）。Starling 的规则改为：首字符是字母或数字，其余是 `[A-Za-z0-9._-]`，长度 1–128，区分大小写。今天的规则是 `[a-z0-9][a-z0-9_-]{0,31}`（`src/governance/world_store.cpp:37`），太窄。
- **世界目录不再用世界名**：macOS 默认的文件系统不区分大小写（`Foo` 与 `foo` 会撞），`.`、`..` 也不能直接当路径段。新世界的目录是 `<数据目录>/worlds/<dir_id>/`，`dir_id` 由 C++ 生成（随机十六进制），存在注册表里。导入的旧世界保留原目录名作为 `dir_id`。
- 世界设计 §3 的布局规则（世界目录在 `<数据目录>/worlds/` 之下、符号链接与越界检查）照旧，比较对象从 `<name>` 换成 `<dir_id>`。

### 5.4 迁移与兼容

服务第一次在某个数据目录启动时，如果有 `worlds.json` 而没有 `registry.db`，把 `worlds.json` 导入 `registry.db`，原文件改名为 `worlds.json.imported-<时刻>` 保留。用户的 8787（数据目录 `~/.starling`）走这条路径。

### 5.5 统计缓存

- 不在每次提交时写 `registry.db`：那样所有世界的提交都要抢注册表的写锁，违背「引擎间没有共享锁」。
- 开着的世界（不超过十个，需求 5.1）：列表接口直接从它的引擎读当前计数。
- 关着的世界不会变：关闭世界时把计数写进缓存列；开着的世界另由一个后台线程每 30 秒刷一次缓存（只写缓存列）。
- 摘要不缓存。需求 5.12 的「列世界时带摘要」做成可选参数，按需逐个计算，有分页上限。

## 6. 分叉、种子与只读

### 6.1 分叉

`fork(source, new_name, *, snapshot_id, labels, retention_group, reclaimable, seed, settings)`，C++ 实现：

- 来源是一个世界，或一个世界的一份快照。
- 步骤：校验新名 → 检查来源的 schema 版本 → 用只读连接对来源执行 `VACUUM INTO`，写到新世界目录的 partial → `integrity_check` → 在 partial 上算摘要，与来源比较 → 改名为 `world.db` → 登记注册表（来源、标签、设置）。任一步失败不留文件，也不留注册项。
- **来源字节不变**：只读连接、不跑迁移。来源是开着的世界时，读的是它最近一次提交时的一致视图。
- **摘要相等**：不等就失败（`fork_digest_mismatch`），不登记。
- **schema 落后**：来源库 `schema_migrations` 的最高版本低于当前代码时拒绝，返回 `world_schema_behind`（需求 5.3）。
- **快**：1 MB 级的库，`VACUUM INTO` 是毫秒级。分叉不持全局锁，只在最后登记时短暂写注册表。
- 验证重放：rise 对部署世界拍快照，再以快照为来源分叉出临时世界（`reclaimable=true`，分组由 rise 给）。

### 6.2 种子

- 种子是 `seed=1` 的世界，`seed` 蕴含 `read_only=1`。建空世界时指定 `seed=true`，或从部署世界分叉时指定 `seed=true`。一个情境一份种子（需求 §8）。
- 种子不参与自动清理，不跑后台任务，打开时不跑迁移。
- **升级种子**由人显式来做：`upgrade_seed(source, new_name)` 复制来源 → 在副本上跑迁移 → 标成新种子，旧种子原样保留（需求 5.3）。

### 6.3 只读打开

只读不能只在存储层加开关：`SqliteAdapter::open` 每次都会跑迁移（`src/persistence/sqlite_adapter.cpp:13`），连接打开时还会设 WAL 模式并建目录（`src/persistence/connection.cpp`）。只读世界要一条完整的只读打开路径（C++）：

- SQLite `mode=ro` 连接，不跑迁移，不改 `journal_mode`，不建目录。
- 引擎在只读模式下拒绝一切写入（`world_read_only`），不起后台线程。
- 召回、检视照常可用。召回与检索路径本身不写库（`src/retrieval/` 里只有来源 lane 的入库函数写库，召回不调它们）；实施时加一条测试钉住「只读世界召回前后文件 sha256 不变」。
- 只读打开一个 schema 落后的库时照常打开，标出 `schema_behind`；依赖新列的检视可能报错，看板显示提示即可。

### 6.4 看板打开一个关着的世界

规则（我的判断，C++ 判定）：

- 世界是 `read_only` 或 `reclaimable`（种子、rollout 世界、验证分叉）：看板一律只读打开，不跑迁移。复盘副本因此不会因为升级 Starling 后在看板里点开而被迁移、改字节。
- 其他世界（部署世界、用户自己建的世界、默认世界）：看板可写打开，按今天的规矩跑迁移。
- 世界被客户端会话占着写入时，看板与它共用那个引擎，看板只能读（§11）。

## 7. 快照与恢复

### 7.1 快照

- 沿用 `take_snapshot`（`VACUUM INTO` + `integrity_check` + 原子改名），作用于整个世界。返回值增加该世界的 `digest`。
- 快照 `kind` 增加 `held`：**调用方持有**，不参与「保留 N 份」的修剪，只能显式删除（需求 5.4）。客户端拍的快照一律是 `held`。看板上手动拍的仍是 `manual`，自动快照仍是 `auto`。

### 7.2 恢复

- 沿用 `rollback_world`（语义 A）：复制到 partial、校验、硬链接归档旧库、一步 `rename` 替换。崩溃时库路径上要么是旧库，要么是完整的新库（世界设计 §5.1）。
- 返回值增加恢复后的 `digest`，必须等于快照记录的那个，不等报 `restore_digest_mismatch`（库已替换，说明快照文件被改过，作为硬错误上报）。
- 前提：这个世界没有活着的句柄（所有 holder 的都算），也没有在途调用。不满足返回 `world_held` 或 `world_busy`，什么都不动。
- 只停被恢复的世界的引擎；别的世界开着句柄、正在写，都不受影响（需求 §7 第 1b 条）。
- 恢复前的旧库归档（`pre-rollback`）保留供复盘，按 §9 清理。

## 8. 每个世界的设置

### 8.1 内容

每个世界在注册表里有一份 `settings`，C++ 负责合并、校验、存储。字段覆盖今天看板引擎实际用到的全部配置（`python/starling/dashboard/engine.py:112-152`、`config.py:38-45`），漏一项就会在继承时悄悄改变行为：

- `extraction`、`embedding`：`provider`（openai / anthropic）、`provider_name`（引用 `starling.json` 里的 provider，取密钥用）、`base_url`、`model`、`dim`（嵌入）、`max_tokens`、`timeout_ms`、`max_retries`、`enable_thinking` / `thinking_type`。
- `chat`：同上（看板对话用；rise 不用）。
- `vector_backend`：只允许 `sqlite`（快照、分叉、恢复都要求向量在库文件里，世界设计 §12）。默认世界若配了 `zvec`，继承时报错，要求显式给 `sqlite`。
- `source_lane`：今天 `source_lane_*` 的那几项。
- `gist_thresholds`。
- `background`：`tick_interval_s`（0 关）、`ingest`（开关）、`auto_snapshot`（`on_session`、`interval_hours`）。

### 8.2 规则

- 建世界或分叉时，调用方给了哪一项就用哪一项；没给的项从默认世界的当前设置复制进新世界（D7、D8）。分叉时没给的项从**来源世界**复制（保证 rollout 世界与种子的行为一致；我的判断）。
- 复制而不是引用：之后改默认世界的设置，已有世界不变（D8）。
- 只读世界的 `background` 强制全关，调用方给了也忽略，并在返回里说明。
- 打开已有世界时调用方再给设置：与存储的不同就报 `world_settings_mismatch`，不静默覆盖（rise 把这份配置写进记忆运行时身份）。改设置走单独的管理接口。
- `info()` 与世界详情报出实际生效的设置，不含密钥，只给 `key_set: bool`。

### 8.3 密钥

- 密钥不走 HTTP，也不写进注册表（需求 5.6）。
- 继承默认配置的世界，记下 `provider_name` 和复制时的 `base_url`；调用时用该 provider 当前保存的密钥。**若该 provider 的 `base_url` 后来改了，与世界里记的不同，拒绝调用**，报 `endpoint_changed`，不把密钥发到另一个地址（用户 2026-11-03 选定）。
- 客户端显式给了端点配置的世界，密钥取服务进程环境变量 `OPENAI_API_KEY`（与 `make_openai_llm` 一致）；没有就报 `endpoint_key_missing`。

### 8.4 摄入的分区

今天摄入目录按租户分区（`python/starling/dashboard/ingest_spool.py:8`），而分叉保留来源租户，一个种子分出的所有世界会共用一个分区、抢同一批任务。改成按世界分区：默认世界保留今天的 `tenant-<tenant>` 目录（本机 hook 照旧写那里），其他世界用 `world-<dir_id>`。摄入根目录在数据目录下（`<数据目录>/ingest-spool`）。所以新世界继承「摄入开」也只是轮询自己那个空目录，不会抢默认世界的任务。

### 8.5 默认世界

默认世界的设置与今天看板的行为一致：tick 30 秒、摄入开、自动快照按时间 24 小时。本机 hook 投递来的对话记录写进默认世界。

## 9. 自动清理

C++ 实现「选出该删的世界」和删除；Python 只定时调用它，并为要删的世界先确认引擎已关（开着就跳过）。

- 只清理 `reclaimable=1` 的世界。rise 分叉 rollout 世界与验证分叉时传 `reclaimable=true` 和分组键（rollout 用 `run_id`）。没标成可回收的世界、种子、默认世界都不在候选里。
- 规则（需求 5.7 的默认值，服务配置里可调）：每个分组只保留最近 200 份（按 `created_at`），并且只保留 14 天；两条哪条先到按哪条删。
- 不删：正开着的世界（有引擎或有活句柄）；**带有 `held` 快照的世界**（需求 §7 第 4a 条要求 rise 拍的快照不被删，所以这样的世界跳过，并在清理记录里写明原因，等 rise 删了快照再清）。
- 恢复前的旧库归档：属于可回收世界的随世界一起删；属于不可回收世界的只按天数删（14 天，我的判断）。
- 每 10 分钟跑一次。每次删除写进 `cleanup_log`（世界名、分组、原因），看板上能看到最近的记录。

## 10. 服务进程

### 10.1 启动

```
starling-serve --data-dir D --host 127.0.0.1 --port P [--token-file F]
```

- 所有路径都在 `D` 之下：`D/starling.json`（provider 与默认世界配置，可以没有，没有时从环境变量取）、`D/registry.db`、`D/worlds/`、`D/ingest-spool/`。不读 `~/.starling`，除非 `D` 就是它（需求 5.9）。
- token 必须给：`--token-file` 或环境变量 `STARLING_SERVICE_TOKEN`，不放在命令行参数里（`ps` 能看到）。`starling-serve` 从不把 token 写回 `starling.json`（今天 `DashboardConfig.load` 没有 token 时会生成并写回，`config.py:145-147`，服务入口不走这条）。
- 启动时对 `D/.lock` 取排他文件锁（`flock`），拿不到就打印 `data_dir_in_use` 并以非零码退出（需求 5.9、§7 第 7 条）。
- `/health` 在注册表打开、默认世界引擎建好之后才返回就绪；之前所有 API 回 503。
- 现有的 `scripts/run_dashboard.py` 改成调用同一个入口（数据目录 `~/.starling`，token 照旧来自 `starling.json`）。默认世界的库保持在 `~/.starling/dashboard.db` 原地（世界设计里默认世界原地的规则）。

### 10.2 停止

收到 SIGTERM：停止接受新请求 → 等在途请求结束（上限 30 秒，可配）→ 停各世界的后台线程 → 逐个关闭世界（合并 WAL、写统计缓存）→ 释放数据目录锁 → 退出。

### 10.3 引擎池

- 每个打开着的世界一个 `DashboardEngine`（Python 对象，包着 C++ Runtime）。默认世界启动即开、一直开着。
- 打开的触发：客户端 `open`、看板点进一个世界、管理操作需要。
- 关闭的触发：客户端显式 `close_world`；或 C++ 的空闲判定（没有活句柄、没有在途调用、超过空闲时限，默认 10 分钟，§11）说可以关。默认世界不因空闲关闭。
- 引擎之间没有共享锁。一个世界在等抽取或嵌入的 HTTP 响应时，其他世界照常读写（GIL 见 §12）。
- 同一世界内的调用由引擎锁串行化（需求 5.2）。所以同一世界里 holder A 抽取时，holder B 的召回要等。rise 的 rollout 一条一条顺序跑，可以接受；把 `remember_event` 拆成「锁外抽取、锁内提交」（像今天看板的 `remember` 那样）留作以后的优化。
- 今天 `WorldManager` 的「切换窗口 + 租约归零才换引擎」那一套缩小到单个世界：恢复、关闭、删除一个世界时，只等这个世界的租约归零。

## 11. 句柄、租约与会话

全部状态机在 C++（D12）。Python 只把 HTTP 请求转成 C++ 调用，并把当前时间传进去（C++ 里不读墙钟，便于测试）。

- **客户端会话**：`connect` 时服务端分配 `session_id`，租约默认 30 秒，客户端每 10 秒发一次心跳续约。
- **句柄**：`open(world, holder)` 在该会话下登记一个句柄。同一个会话可以在同一个世界里开多个句柄，各绑一个 holder（也可以重复绑同一个），这些句柄共用一个引擎（需求 5.1、5.2）。
- **写占用**：一个世界在任一时刻至多被一个客户端会话占用写入。另一个会话来开可写句柄，返回 `world_held`（非瞬态）。另一个会话只读打开可以。看板的查看不算写（需求 5.1）。
- **看板**：看板请求不是客户端会话。被客户端占用的世界，看板可以检视、召回；写入类命令返回 `world_held`。
- **释放**：句柄 `close()` 释放一个句柄；会话断开（`disconnect`）或租约过期，该会话的句柄全部释放。恢复与清理只数活着的句柄。
- **在途调用**：C++ 记每个世界、每个句柄的在途调用数（Python 在调用前后报入报出）。句柄的 `close(timeout)` 等这个句柄的在途调用归零，超时报错且句柄保持打开（需求 5.5，与 `Memory.close` 语义一致）。
- **空闲判定**：C++ 根据「有没有活句柄、有没有在途调用、最后一次活动时间、是不是默认世界」给出「这个世界现在可以关」的判断。

## 12. GIL

需求 4.4：一个世界在等网络时不放 GIL，会卡住所有世界。

- 现状：`vector_recall` 已在 #115（730e415）修好。`bindings/python/bind_13_memory_ops.cpp` 里抽取、嵌入、`remember_event`、`tick_all`、`backup_to` 等已释放 GIL；`memory_digest`、`memory_next_seq`、`memory_pending_events`、`memory_abandon_event` 还没有。
- 本次：新增与改动的绑定一律释放 GIL；`memory_digest` 改为释放（大库上可能跑几百毫秒）。
- 测试：一个世界的抽取在 C++ 里阻塞 700 毫秒（`FakeLLMAdapter.set_delay_ms`），同时另一个世界的召回必须在 200 毫秒内返回。沿用 `tests/python/test_gil_release.py` 的写法。

## 13. HTTP API 与客户端

### 13.1 API 形状

- 看板 API 按世界寻址：`/api/w/{world}/...`。现有检视与命令路由（`/statements`、`/recall`、`/remember` 等）都挂在这个前缀下。
- 旧路径（`/api/overview` 等，不带世界名）继续可用，指向默认世界。现有脚本、hook 和前端在迁移完之前不受影响。
- 客户端专用路由在 `/api/v1/client/...`：会话（`connect`、`heartbeat`、`disconnect`）、句柄（`open`、`close`）、门面方法（请求带 `handle_id`）、管理操作。
- **不收文件路径**：所有接口按世界名、快照 id 寻址（需求 5.5）。
- 错误响应统一为 `{"code": "...", "transient": bool, "detail": "..."}`。`code` 与 `transient` 都来自 C++ 的错误码目录。`detail` 里不带端点地址和密钥。

### 13.2 错误码目录（C++）

一处定义，每个码带「是否瞬态」，HTTP 层与客户端都从这里取。至少包括：

| code | 瞬态 | 含义 |
|---|---|---|
| `world_not_found` | 否 | 世界不存在 |
| `world_exists` | 否 | 建世界、分叉时重名 |
| `invalid_world_name` | 否 | 世界名不合规则（§5.3） |
| `world_held` | 否 | 别的会话占着写入；或恢复时还有活句柄 |
| `world_busy` | 是 | 恢复、关闭时还有在途调用 |
| `world_read_only` | 否 | 只读世界上写入 |
| `world_schema_behind` | 否 | 分叉来源的 schema 落后 |
| `world_settings_mismatch` | 否 | 打开时给的设置与存储的不同 |
| `labels_immutable` | 否 | 试图改已建世界的标签 |
| `fork_digest_mismatch` | 否 | 分叉出的世界摘要与来源不同 |
| `restore_digest_mismatch` | 否 | 恢复后的摘要与快照记录的不同 |
| `snapshot_not_found` / `snapshot_corrupt` / `insufficient_disk` | 否 | 沿用世界设计 |
| `event_conflict` | 否 | 映射成 `EventConflict` |
| `event_not_found` | 否 | 映射成 `KeyError` |
| `recall_degraded` | 是 | 映射成 `RecallDegradedError` |
| `handle_closed` | 否 | 映射成 `MemoryClosed` |
| `session_expired` | 否 | 租约已过期，要重新 connect |
| `endpoint_changed` | 否 | 继承的 provider 地址已改（§8.3） |
| `endpoint_key_missing` | 否 | 显式端点配置但服务环境里没有密钥 |
| `protocol_mismatch` | 否 | 客户端与服务协议版本不兼容 |
| `data_dir_in_use` | 否 | 启动时数据目录已被占用 |

`world_switching`、`ingest_busy` 这类与「活动世界」相关的旧码随活动世界一起退役，或改成按单个世界的 `world_busy`。

### 13.3 客户端包

- 位置：仓库子目录 `clients/python/`，包名 `starling-client`，导入名 `starling_client`。只依赖标准库和 `httpx`，不依赖 C++ 扩展（需求 5.5）。
- 安装：`starling-client @ git+<仓库地址>@<commit>#subdirectory=clients/python`，与服务同一个提交（需求 5.10）。
- 接口（与 `starling.Memory` 对齐的部分签名、返回形状一致）：

```python
client = starling_client.connect(url, token=..., timeout_s=...)   # 握手时核对协议版本
client.info()            # {version, commit, protocol, extraction_config, default_settings(不含密钥)}

mem = client.open(world, holder=..., read_only=False, settings=None)   # 返回 RemoteMemory
# 以句柄的 holder 身份：
mem.recall(query, k=..., mode="semantic", strict=True, include_volatile=True)
mem.remember_event(key, text, interlocutor=..., epoch=...)
mem.abandon_event(key)
mem.content_ids(ids)
# 作用于整个世界（D5）：
mem.digest(); mem.pending_events(); mem.next_seq(); mem.embed_pending(now)
mem.close(timeout=...)

# 管理操作
client.create_world(name, *, labels=None, settings=None, seed=False)
client.fork(source, name, *, snapshot_id=None, labels=None, retention_group=None,
            reclaimable=False, seed=False, settings=None)
client.upgrade_seed(source, name)
client.snapshot(world, note="")            # held 快照，返回 {snapshot_id, digest}
client.restore(world, snapshot_id)         # 返回恢复后的 digest
client.delete_snapshot(world, snapshot_id)
client.digest(world); client.pending_events(world)     # 不开句柄也能做
client.close_world(world)
client.list_worlds(labels=None, page=..., page_size=..., with_digest=False)
client.close()
```

- 返回对象：`recall` 的每项是 dict，含 `content_id`、`score`、`row`；`row` 是一个只读对象，带 `StatementRow` 的字段（rise 用到 `subject_id`、`predicate`、`object_value`、`polarity`、`modality`、`confidence`），服务端把整行序列化过来。`remember_event` 返回与 `RememberResult` 同字段的 dataclass。`pending_events` 返回 `PendingEvent` 同字段再加 `holder` 的 dataclass。`embed_pending` 返回 `EmbedStats` 同字段的 dataclass。
- `list_worlds` 每项带名字、标签（不可变）、`seed`、`read_only`、来源、holder 数、语句数、最后写入时间、状态，`with_digest=True` 时加摘要（需求 5.11、5.12）。
- 异常：`RecallDegradedError`、`EventConflict`、`MemoryClosed` 同名；`KeyError` 照旧。传输层另有 `ServiceUnavailable`（连不上，瞬态）、`ServiceTimeout`（瞬态）、`ServiceError`（服务端 5xx，带 `transient`）、`ProtocolMismatch`。所有异常都有 `code` 与 `transient` 两个属性。
- `close(timeout)`：服务端等这个句柄的在途调用归零；超时抛 `TimeoutError`，句柄保持打开（需求 5.5）。客户端在本地也计在途调用，语义与 `Memory.close` 一致。
- 心跳由客户端后台线程发。心跳失败只记录；下一次调用收到 `session_expired` 就抛出，由调用方决定重连。客户端不自动重开句柄（重开会掩盖「租约过期期间别人可能占了这个世界」）。

### 13.4 版本

- 构建时把 git 提交写进包（构建步骤生成 `_build_info.py`，C++ 侧也编进一个常量）。`info()` 报出 `version`、`commit`、`protocol`。
- `protocol` 是一个整数，客户端与服务不同就在 `connect` 时报 `protocol_mismatch`。
- `info()` 同时报出生效的 `ExtractionConfig`（提示词与策略的值），rise 写进记忆运行时身份（需求 5.6）。

## 14. 看板

- 顶栏一个世界选择器。选中的世界放进页面地址，刷新、分享链接都保持。切换只影响当前页面，不发任何服务端「切换」请求。
- 所有检视页、互动页（召回、写入）、会话页、快照页按选中的世界取数。点进一个没开着的世界，服务按 §6.4 打开它；没人看了按空闲规则关掉。
- 页面顶部标出世界状态：只读（种子、可回收世界）、被 rise 占用（写入置灰，召回可用）、schema 落后。
- 实时：服务在一个世界每次提交后，经现有 WebSocket 推一条 `{type:"world_changed", world, statements, last_write_at}`。正在看这个世界的页面收到后重新取数，几秒之内看到新语句（需求 5.7）。
- 世界列表页：名字、标签、分叉来源、holder 数、语句数、最后写入时间、状态。按任意标签过滤（`kind`、`scenario_id`、`run_id`、`episode_id` 等），分页，按最后写入时间排序（需求 5.11）。
- 世界里有哪些 holder 从世界本身看（现有的 `/api/cognizers`、`/api/personae`），各检视页沿用现有的 holder 过滤；多 holder 世界默认显示全部 holder。
- 「活动世界」相关的界面（激活按钮、切换中的提示）全部去掉。

## 15. 代码分层（D12）

| 放 C++（`src/` + `include/starling/`） | 放 Python（`python/starling/`） | 客户端（`clients/python/`） |
|---|---|---|
| 注册表存储（`registry.db`）、世界名规则、`dir_id`、布局校验、`worlds.json` 导入 | HTTP 路由：请求解析、调用 C++、原样返回 | HTTP 转发 |
| 标签（不可变）、设置的合并与继承、`endpoint_changed` 判定 | 引擎对象（`DashboardEngine`）与后台线程的建和停，按 C++ 的决定执行 | 按 `code` / `transient` 抛同名异常 |
| 分叉、种子、升级种子、schema 检查 | 服务入口、SIGTERM、调用数据目录锁 | 心跳线程 |
| 只读打开路径（不迁移、`mode=ro`）、看板打开方式的判定 | 看板前端（SvelteKit） | 本地在途计数（`close` 语义） |
| 快照（含 `held`）、恢复、归档 | | |
| 事件按 holder 的键、认领规则、原文按 holder 分开（迁移 0039） | | |
| 句柄、会话、租约、在途计数、空闲判定 | | |
| 自动清理的选择与删除、清理记录 | | |
| 摄入分区路径 | | |
| 错误码目录（码 + 是否瞬态） | | |

越界判据仍是「换一种绑定语言是否要重写这段逻辑」。服务端 Python 里出现「判断」（要不要删、能不能恢复、这个句柄算不算活着、以只读还是可写打开）就是越界。

## 16. 测试与验收

需求 §7 的每一条都变成 Starling 自己的测试，名字里带验收编号，便于 rise 对照。

| 需求 §7 | 测试 |
|---|---|
| 1 三个世界互不可见 | 一个服务进程开三个世界，各自 `remember_event` + `recall`，互相召回不到 |
| 1a 一个世界两个 holder | 同一世界开两个句柄绑 A、B，交替写入（含同 key 同文本）：A 的 `recall` 只返回 A 名下的语句，B 同理；两人各自拿到自己的语句，`observed_at` 各取各的事件时刻；世界摘要反映两者；`pending_events` 列出两者（带 `holder`） |
| 1b 跨世界不受影响 | 世界 X 开着句柄、正在写入时，对 Y 快照、写入、恢复都成功，X 的摘要与语句不变；Y 自己还有句柄时恢复 Y 返回 `world_held` |
| 2 分叉摘要 | 种子分叉两个副本，三者摘要相同；写一个副本，另两个摘要不变；种子文件字节（sha256）不变 |
| 3 快照与恢复 | 快照 → 写 → 恢复，摘要等于快照时；有句柄开着时恢复返回 `world_held` |
| 4 看板实时 | 客户端写一个世界的同时，看板 API 按世界读到新语句（几秒内），能在里面召回；世界关掉后按标签找到并能打开（只读，不迁移） |
| 4a 清理 | 超过份数或天数的可回收世界被删；部署世界、种子、带 `held` 快照的世界、开着的世界不被删（时间注入） |
| 5 后台关掉的世界摘要不漂移 | 关掉后台任务的世界静置（推进注入时钟，跑若干轮清理与调度），摘要不变 |
| 6 门面行为一致 | 同一组调用分别走进程内 `Memory` 和走客户端：同 key 同文本重入、同 key 异文本冲突、abandon 后再写冲突、嵌入失败时 strict 召回抛 `RecallDegradedError`、`close` 超时不被标成已关；两边结果逐项相等 |
| 7 停止与重启 | SIGTERM 后重启，已提交写入都在；同一数据目录起第二个服务报 `data_dir_in_use` 退出 |
| 8 版本 | `info().commit` 等于安装时的提交 |

另加：

- 迁移 0039：旧格式库上跑迁移，旧事件按认领规则被第一个 holder 接管，重入行为不变。
- GIL：§12 的跨世界不阻塞测试。
- 只读：只读世界召回前后文件 sha256 不变；看板打开可回收世界不跑迁移。
- 端点：继承来的 provider 改了 `base_url` 后调用返回 `endpoint_changed`；错误响应与日志里没有地址和密钥。
- 世界名：大小写不同的两个名字各自独立，`.`、`..` 开头的名字被拒。
- 假端点：在子进程里起本机回环的 OpenAI 兼容假端点（参照 rise 的 `tests/starling_fakes.py`：按事件 key 让抽取回 500 或坏 JSON，嵌入按词面给确定向量），设置里把 `base_url` 指过去。

提交门照旧：全量 ctest + `pytest tests/python` 绿；前端 `npm run check` / `npx vitest run` / `npm run build`；客户端包另有自己的 pytest。

## 17. 分期

每期一个 PR，合并后 main 仍可用（8787 照常工作）。

1. **多世界同时运行**：注册表改存 SQLite（导入 `worlds.json`、新世界名规则、`dir_id`、统计缓存）；引擎池，多个世界同时开；看板 API 按世界寻址，旧路径指向默认世界；去掉活动世界；每个世界的设置与继承（§8，含 `endpoint_changed`、摄入按世界分区）；恢复只停单个世界；标签（不可变）。
2. **一个世界多个 holder**：迁移 0039；事件按 holder 的键与认领规则；原文按 holder 分开；`pending_events` 带 `holder`；`digest` 等绑定释放 GIL。
3. **分叉、种子与只读**：分叉（世界或快照为来源）、schema 检查、`held` 快照、只读打开路径、看板打开方式判定、种子与升级种子、快照与恢复返回摘要。
4. **服务与客户端**：`starling-serve` 入口、数据目录锁、SIGTERM、会话与句柄与租约（C++）、错误码目录、`/api/v1/client`、客户端包、版本与协议核对、门面一致性测试。
5. **看板前端**：世界选择器、按世界的页面、状态提示、实时推送、世界列表的标签过滤与分页。
6. **自动清理**：可回收世界与归档的清理、配置项、清理记录。

rise 在第 4 期之后能接上；第 6 期之后 rise 不再负责删世界。

## 18. 留给实施者

- `registry.db` 的具体列名与索引；`dir_id` 的生成方式。
- 看板页面地址里世界名的放法（查询参数或路径段）。
- 空闲关闭时限（默认 10 分钟）、清理间隔（默认 10 分钟）、统计缓存刷新间隔（默认 30 秒）、心跳与租约时长（10 秒 / 30 秒）。都做成服务配置项。
- `_build_info.py` 的生成方式。
- 客户端 `row` 对象的具体类型（dataclass 或 `SimpleNamespace`），只要字段名与 `StatementRow` 一致。

## 19. 需要回告 rise 的

以下是 Starling 对需求 §8「待定」与 §9「rise 先定的默认」的答复，实施完成后同步给 rise：

- 种子：普通世界加只读标记，建空世界或从部署世界分叉时指定 `seed=true`；升级种子用 `upgrade_seed`，得到新名字的新种子。
- 句柄模型、旧库不导入、种子落后拒绝分叉、rise 拍的快照不参与修剪、保留默认 200 份 / 14 天：全部照 rise 的默认。
- **带有 rise 快照的可回收世界不会被自动清理**，rise 删了快照之后才会被清。
- 世界名：首字符字母或数字，其余 `[A-Za-z0-9._-]`，长度 1–128，区分大小写。
- `pending_events()` 每项多一个 `holder` 字段，rise 用对应 holder 的句柄去 `abandon_event`。
- 同一世界内的调用串行化：同一 rollout 世界里训练位抽取时，对手位的召回要等。
- 端点配置：不给就继承服务默认世界的配置（建时复制）；给了的话密钥取服务环境变量 `OPENAI_API_KEY`。继承的 provider 地址被改过就报 `endpoint_changed`。
- 客户端的安装方式与导入名（§13.3）。rise 的适配器从 `starling.Memory.open(path, agent=...)` 换成 `client.open(world, holder=...)`。
