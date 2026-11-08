# Starling 记忆服务：给 rise 的答复与迁移指引

日期 2026-11-06。写给 rise 的维护者。记忆服务按期合并（设计源 `2026-11-03-memory-service-design.md` §17），每期合并时在这里补一节，说明这一期对 rise 有什么影响、rise 要改什么。

## 第 2 期（一个世界多个 holder）

这一期让一个库文件里可以有多个 holder：`events` 表按 `(tenant_id, holder_id, key)` 区分（迁移 `0039`），同一个 key 在两个 holder 名下是两条事件，各写各的语句。

**`abandon_event` 改为按 holder 作用。** 放弃只作用于句柄自己那个 holder 名下的事件，要在拥有这条事件的 holder 的句柄上调。`pending_events()` 每项的 `holder` 字段说明它属于谁。用 `agent="world"` 的短连接放弃一条记在某个 holder 名下的事件，会抛 `KeyError`，消息点名主人，例如：

```
abandon_event: key 'ep1#3@pa' is recorded under holder(s) pa, not 'world'
```

rise 的 key 有两种。轮次的 key 是 `{episode_id}#{turn_index}@{holder}`，带 holder（`src/actio/instance.py:209`）。准入的 key 是 `{proposal_id}#{k}`，不带 holder（`src/mindagent/admit.py:102`）。

rise 要把端口改成 `abandon_event(world_id, holder, key)`，由调用方给 holder；`src/actio/memory_starling.py:477-482` 改成以 `agent=holder` 开连接再放弃。各调用处：

- `src/mindagent/admit.py:205` 与 `:210` 传 `pid`，它们手里本来就有。
- `src/mindagent/deployment.py:457` 传这一项的 `PendingEvent.holder`；holder 是 `''` 时（见下面的无主旧行）取 key 里的 `@holder`。为此 rise 端口的 `pending_events`（`memory_starling.py:484-489`）要返回 `(key, holder)`，不能只返回 key。

不能只靠 `pending_events()` 找 holder：它只列 pending 行，不是 pending 的 key 查不到主人。`admit.py:205` 放弃的正是一条已经放弃过的 key（已放弃是空操作），在 `pending_events()` 里找不到；退回 `"world"` 就抛 `KeyError`，记一次 T2，提案留在 approved，下一 tick 再走同一路径，一直循环。

rise 的 `translate_failure`（`memory_starling.py:79-94`）只留异常类名，所以点名主人的那条消息到不了 rise 的日志；要看到它，rise 得把消息留下来。

认领规则：0039 回填不出主人的旧行 holder 是 `''`，从 0038 升级上来的库里会有这种行。第一个对它调 `remember_event` 或 `abandon_event` 的 holder 会把它认领过去。所以对 pending 或 abandoned 的 `''` 行，用 `"world"` 放弃不会报错，而是把行记到 `world` 名下；之后真正的主人对同一个 key 调 `remember_event`，会另开一行，这个 key 又能写了。零语句的 committed `''` 行放弃时照样抛 `EventConflict`，认领随之回滚。

holder 不能为空：对空 holder 调 `remember_event` 或 `abandon_event` 抛 `ValueError`，库里不写任何东西。`''` 只留给上面说的无主旧行。

**`pending_events()` 返回整个世界的 pending 事件。** 每个 holder 的都在里面，每项多一个 `holder` 字段，原有字段不变。rise 端口的 `pending_events`（同一文件 484-489 行）今天只取 `.key`，按上面的改法要连 `holder` 一起返回。

**升级 Starling 的锁定版本会改一次摘要。** 进程内 `Memory.open` 第一次打开库时会把它迁到 0039。种子库原地不改，但 `library_digest` 在副本上打开、副本照样迁移，算出的摘要也是 0039 之后的。新增的 `holder_id` 列进摘要，所以库里有 pending 或 committed 事件行的，摘要会变一次；含库摘要的训练 `config_hash` 也跟着变一次。没有事件行的库摘要不变；事件行全是 abandoned 的也不变，因为摘要对 abandoned 行只记 key 和状态。另有一种情况会再变一次：回填成 `''` 的 pending 行和零语句的 committed 行，被某个 holder 认领时 `holder_id` 改了，摘要也跟着变。

库迁到 0039 之后，别再用 0038 的旧锁定版本打开它。旧代码打开时不报错，但它按 `(tenant, key)` 读 `events`，会随便拿到某个 holder 的行；放弃时也会把所有 holder 同 key 的 pending 行一起改掉。

**已知残留。** 两处都只在写入时开着 `attribute_first_order_mental_to_holder` 时才会出错。这个策略默认关闭，没有哪份配置打开它。

- 裁定 R16：0039 回填取的是事件行里第一条查得到的语句的 holder。开着这个策略时，一阶欲望类语句记在欲望的主体名下，不在写入者名下；这样一条语句排在第一位时，行会被回填给主体。
- 裁定 R9：0039 之前崩溃留下的 pending 行（语句已落库、收据没写成）被另一个 holder B 认领时，补齐只收 B 名下的语句。开着这个策略时，原写入者的抽取可能把部分语句记在 B 名下，B 会把这些语句当成自己这条事件的结果。

## 第 3 期（分叉、种子与只读）

这一期在 C++ 里实现分叉、种子、升级种子和只读打开，看板加了分叉与升级种子两个路由。rise 要用的 Python 客户端在第 4 期，所以这一期合并后 rise 还不用改代码；下面是第 4 期接上客户端以后 rise 可以依赖的行为。

**分叉。** 来源是一个世界或它的一份快照。分叉不改来源的任何字节，也不跑迁移。新世界的摘要在登记前与来源比对，不等就失败（`fork_digest_mismatch`），不留文件也不留注册项。来源开着时，分叉读的是它最近一次提交；复制途中来源又有写入，返回 `world_busy`（瞬态，重试即可）。来源的 schema 落后于当前代码时拒绝，返回 `world_schema_behind`，不会静默迁移。来源库里有别的租户的行时也拒绝，返回 `world_tenant_mismatch`；rise 的世界都是单租户，不会遇到。

**种子。** `seed=true` 蕴含只读。种子不跑后台任务、不参与自动清理，打开时不迁移。升级种子是显式操作：`upgrade_seed(source, new_name)` 复制旧种子、在副本上迁移、登记为新种子，旧种子原样保留。来源不是种子时返回 `world_not_seed`。种子不能恢复：对只读世界（含种子）恢复返回 `world_read_only`，种子的字节因此不会被换掉。新种子照抄旧种子的标签（裁定 R13），所以按标签查会同时找到新旧两份；rise 要按 `created_at` 或名字选最新的一份。建空种子（`create_world(seed=true)`）的客户端接口在第 4 期。

**摘要的来源。** rise 今天的 `library_digest`（`src/simple_actio/memory_starling.py:308`）在迁移过的副本上算摘要。种子在当前 schema 上时，它与分叉返回的摘要相同；种子落后时，`library_digest` 照样给出一个迁移后的摘要，但分叉会拒绝这个种子。接上客户端以后，起点摘要建议直接取分叉返回的 `digest`（或第 4 期 `list_worlds(with_digest=True)` 的值），不再自己拷文件算。

**`read_only` 和 `open_mode` 是两个字段（裁定 R18）。** `read_only` 是注册表里的原始标记（种子与显式只读的世界为真），第 4 期 `list_worlds` 原样返回给 rise。`open_mode`（`"rw"` 或 `"ro"`）是看板实际怎么打开：`read_only`、`seed`、`reclaimable` 三者任一为真，看板就只读打开。所以 rollout 世界和验证分叉（`reclaimable=true`）在看板里只读，客户端照样能写。

**快照与恢复返回摘要。** 拍快照返回该世界此刻的摘要；恢复返回恢复后的摘要，与快照记录的不等时报 `restore_digest_mismatch`，这是硬错误，说明快照文件被改过。`held` 类快照归调用方所有，不参与「保留 5 份」的修剪（需求 5.4），由 rise 显式删除。C++ 这一期已经支持 `held`，客户端的 `snapshot(world)` 在第 4 期接上；看板拍的快照仍是 `manual`。0038 之前的旧库拍的快照不带摘要，恢复时不比对（裁定 R10）；rise 的世界都从空开始建，不会遇到这种库。

**别直接打开世界文件。** 只读世界（种子、显式只读、可回收）被只读打开时，库没有 `-wal`、`-shm` 就用 SQLite 的 `immutable=1`，这样复盘时点开不会改目录（裁定 R28、R36）。这要求期间没有任何连接写这个文件。服务内部保证这一点；服务之外的进程直接写世界文件不受支持，只读的一方可能读到不一致的数据或报错。可写世界一律用普通只读连接。接上客户端以后，rise 不再按路径打开任何世界文件。

还没做的：恢复时世界还有句柄开着返回 `world_held`，属于第 4 期的写占用（裁定 R25）。看板正在只读查看一个 rollout 世界时客户端来写，第 4 期会先等看板的检视连接关掉、关掉只读的那份，再以可写方式打开（裁定 R30、R37），rise 不用为此做什么。

## 第 4 期（服务进程与客户端）

这一期加了服务进程 `starling-serve` 和纯 Python 客户端 `starling-client`。rise 从进程内的 `starling.Memory.open(path, agent=...)` 换成连服务：`client.open(world, holder=...)`。下面按 rise 要改的顺序写。

**安装。** 客户端只依赖标准库和 `httpx`，不依赖 C++ 扩展：

```
starling-client @ git+https://github.com/ruijieguo/starling.git@<commit>#subdirectory=clients/python
```

客户端与服务用同一个提交。`client.info()["commit"]` 来自 C++ 编进 `_core` 的构建常量（裁定 R88），rise 拿它和自己钉住的提交比对，不一致就别跑。`info()` 同时给出 `version`、`protocol` 和生效的 `extraction_config`，rise 写进记忆运行时身份。客户端与服务的 `protocol` 不同时，`connect` 抛 `ProtocolMismatch`。

**起服务。**

```
starling-serve --data-dir D --host 127.0.0.1 --port P --token-file F
```

token 也可以放在环境变量 `STARLING_SERVICE_TOKEN` 里；没有 `--token` 参数（命令行参数 `ps` 看得到）。所有文件都在 D 之下（`D/starling.json`、`D/registry.db`、`D/worlds/` 等），服务不读 `~/.starling`。

- 先监听、后就绪：stdout 先打印 `starling-serve listening on http://HOST:PORT`，开好注册表和默认世界之后再打印 `starling-serve ready`。就绪之前 `/health` 和其余接口一律回 503 `worlds_unavailable`。
- 同一个 D 起第二个实例：stderr 打印 `data_dir_in_use`，退出码 3。
- SIGTERM：不再接新连接，在途请求最多等 30 s（`--drain-timeout`），然后关闭各世界、退出码 0。开注册表或默认世界失败时退出码 1。
- 客户端连回环地址（127.0.0.0/8、`::1`、`localhost`）时不走环境变量和系统代理（裁定 R97）：本机配了代理，bearer token 也不会经过它。连非回环地址时照常读代理设置。

**迁移：`Memory` 换成 `RemoteMemory`。**

```python
import starling_client

client = starling_client.connect(url, token=token, timeout_s=30.0)
settings = dict(client.info()["default_settings"])   # 见下面「设置」
# ……合并 rise 自己的 overrides……
mem = client.open(world, holder=holder, settings=settings)   # 原来是 starling.Memory.open(path, agent=holder)
```

| 进程内 `Memory` | `RemoteMemory` | 不同之处 |
|---|---|---|
| `recall(query, *, k=10, mode="semantic", strict=False, include_volatile=False, perspective=..., holder=...)` | `recall(query, *, k=10, mode="semantic", strict=True, include_volatile=True)` | 缺省 `strict`、`include_volatile` 为 True（spec §13.3）；holder 是句柄的，不能按调用改；没有 `perspective` |
| `remember_event(key, text, *, interlocutor, epoch=...)` → `RememberResult` | 同 | 无 |
| `abandon_event(key)` | 同 | 无 |
| `content_ids(statement_ids)` | 同 | 无（裁定 R100 M2 已对齐参数名） |
| `digest()`、`pending_events()`、`next_seq()`、`embed_pending(now)` | 同 | 作用于整个世界（D5）；`pending_events` 每项带 `holder` |
| `close(timeout=None)` | 同 | 语义一致：超时抛 `TimeoutError`、句柄保持打开；另一个 `close()` 在进行时等它；`timeout=None` 一直等 |

`recall` 每项是 dict：`content_id`、`score`、`row`；`row` 是只读的 `StatementRow`，字段与进程内的相同（整行序列化过来）。`RememberResult`、`PendingEvent`、`EmbedStats` 是同字段的 dataclass。

不开句柄也能做的管理操作在 `client` 上：`create_world`、`fork`、`upgrade_seed`、`snapshot`（held 快照，返回 `snapshot_id` 与 `digest`）、`restore`（返回恢复后的摘要）、`delete_snapshot`、`digest(world)`、`pending_events(world)`、`close_world`、`list_worlds(labels=..., page=..., page_size=..., with_digest=...)`。

**判断写入成功。** 条件是 `outcome in {"accepted", "idempotent"}` 并且 `extraction_failed is False`，不能只看 `outcome == "accepted"`。客户端超时后按同一个 key 重放时：

- 两次调用都可能返回 `idempotent`（第一次的 begin 已经写好了 engram，重放那次的 prepare 把它当成证据重入）；
- 两次返回的都是先提交那次的 `statement_ids`，被顶替那次自己抽到的结果会丢掉（裁定 R95）；
- 被顶替的原请求在服务端仍会跑完，期间占着句柄的在途计数，`close(timeout)` 要等它结束。

**设置。** 写法是取 `info()["default_settings"]`，合并 rise 的 overrides，再把整份传给 `create_world(settings=...)` 和 `open(settings=...)`。`open` 比较的是整份设置（两边补齐默认值之后），不是子集，不同就抛 `WorldError("world_settings_mismatch")`（裁定 R93）。继承来的端点地址不参与比较（服务端填进去的，`info` 不给）。

部署世界要显式关掉后台：`{"background": {"tick_interval_s": 0, "ingest": False, "auto_snapshot": {"on_session": False, "interval_hours": 0}}}`。没给的项从默认世界复制，默认世界的后台是开着的（tick 30 s、摄入开、按时间 24 小时快照），不关的话世界会自己写，摘要会漂移。可回收世界（`reclaimable=True`）不受这条影响，它从不跑后台任务；嵌入由 rise 调 `embed_pending` 驱动（裁定 R68）。

端点：不给就继承服务默认世界的配置（建世界时复制）。客户端显式给了端点（没有 `provider_name`）时，密钥取服务进程的环境变量 `OPENAI_API_KEY`；服务环境里没有它，抽取、`embed_pending`、召回都抛 `WorldError("endpoint_key_missing")`，不会退回占位向量（裁定 R100 I1）。继承的 provider 地址后来被改了，调用报 `endpoint_changed`，密钥不会发到新地址。

**`recall(mode="completion")`** 必须显式传 `strict=False, include_volatile=False`。客户端缺省是 True/True，不传就回 400 `invalid_request`，与进程内 `Memory.recall` 对这两个组合抛 `ValueError` 对应（裁定 R93）。

**并发。**

- 抽取在引擎锁外进行：同一世界里一次 `remember_event` 正在抽取时，别的句柄的 `recall` 不等它（裁定 R92）。
- `recall` 和 `embed_pending` 在嵌入请求期间持引擎锁，同一世界的其他调用会排在它后面。
- 不同世界之间互不影响：一个世界的抽取或嵌入不挡另一个世界的调用（验收 §12，测得 1–2 ms）。

**错误与异常。** 错误体统一是 `{"code", "transient", "detail"}`，`code` 和 `transient` 来自 C++ 错误码目录；`detail` 不带端点地址和密钥。客户端所有异常都有 `code` 和 `transient` 两个属性。

| code | 客户端异常 | 说明 |
|---|---|---|
| `event_conflict` | `EventConflict` | 同 `Memory` |
| `event_not_found` | `EventNotFound`（`KeyError` 的子类） | 只在 `abandon_event` 上出现 |
| `recall_degraded` | `RecallDegradedError` | 瞬态 |
| `handle_closed` | `MemoryClosed`（`RuntimeError` 的子类） | |
| `session_expired` | `SessionExpired` | 租约过期。客户端不自动重开句柄：重开会掩盖「过期期间别的会话可能占了这个世界」，由 rise 决定要不要重新 `connect` |
| `world_busy` | `ServiceError`（503），`transient=True` | 关闭、恢复、分叉途中或写入准入冲突。由调用方重试，客户端不自动重试。例外：`close(timeout)` 收到它时抛 `TimeoutError`、句柄保持打开 |
| `world_held` | `WorldError` | 别的会话占着写入；恢复时还有活句柄 |
| `endpoint_key_missing` | `WorldError` | 见上面「端点」 |
| `endpoint_changed` | `WorldError` | 继承的 provider 地址改了 |
| `invalid_request` | `WorldError` | 参数形状不对、未知方法、`completion` 配 `strict`；400，非瞬态 |
| `llm_not_configured` | `WorldError` | 世界没有绑定抽取角色；在写 pending 行之前就拒绝 |
| `unauthorized` | `Unauthorized` | token 错或没带；401，非瞬态 |
| 其他 4xx | `WorldError` | `world_not_found`、`world_read_only`、`world_settings_mismatch` 等 |
| 其他 5xx | `ServiceError` | 按体里的 `transient`（`worlds_unavailable` 瞬态；`internal_error`、`world_open_failed` 非瞬态） |

传输层另有 `ServiceUnavailable`（连不上，瞬态）、`ServiceTimeout`（瞬态）和 `ProtocolMismatch`。

**已知限制。**

- 空 key 照常接受，与进程内 `Memory` 一致（裁定 R95）；rise 的 key 都是提案或轮次 id，不会是空串。
- 空闲关闭时限（10 分钟）、会话租约（30 s）、心跳间隔（10 s）这几项暂不可配置，留到第 5 期。

（第 5、6 期的答复在各期合并时补充。）
