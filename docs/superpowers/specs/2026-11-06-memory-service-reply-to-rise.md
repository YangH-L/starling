# Starling 记忆服务：给 rise 的答复与迁移指引

日期 2026-11-06。写给 rise 的维护者。记忆服务按期合并（设计源 `2026-11-03-memory-service-design.md` §17），每期合并时在这里补一节，说明这一期对 rise 有什么影响、rise 要改什么。

## 第 2 期（一个世界多个 holder）

这一期让一个库文件里可以有多个 holder：`events` 表按 `(tenant_id, holder_id, key)` 区分（迁移 `0039`），同一个 key 在两个 holder 名下是两条事件，各写各的语句。

**`abandon_event` 改为按 holder 作用。** 放弃只作用于句柄自己那个 holder 名下的事件，要在拥有这条事件的 holder 的句柄上调。`pending_events()` 每项的 `holder` 字段说明它属于谁。用 `agent="world"` 的短连接放弃一条记在某个 holder 名下的事件，会抛 `KeyError`，消息点名主人，例如：

```
abandon_event: key 'ep1#3@pa' is recorded under holder(s) pa, not 'world'
```

rise 要改的地方是 `src/actio/memory_starling.py:477-483` 的 `abandon_event`：改成以事件的 holder 开连接（`agent=holder`）再放弃。holder 可以取 `pending_events()` 里那一项的 `holder`。rise 的 key 本来就带 `@holder`（`src/actio/instance.py:209`），两个 holder 的 key 从不相同，所以下面的认领规则在 rise 自己的数据上用不到。

认领规则：0039 回填不出主人的旧行 holder 是 `''`，第一个对它调 `remember_event` 或 `abandon_event` 的 holder 会把它认领过去。所以对这种行，用 `"world"` 放弃不会报错，而是把它记到 `world` 名下。这种行的 `PendingEvent.holder` 是 `''`，rise 要按 key 里的 `@holder` 去放弃。

holder 不能为空：对空 holder 调 `remember_event` 或 `abandon_event` 抛 `ValueError`，库里不写任何东西。`''` 只留给上面说的无主旧行。

**`pending_events()` 返回整个世界的 pending 事件。** 每个 holder 的都在里面，每项多一个 `holder` 字段，原有字段不变。rise 的 `pending_events`（同一文件 484-489 行）只读 `.key`，照常可用。

**升级 Starling 的锁定版本会改一次摘要。** 进程内 `Memory.open` 第一次打开库时会把它迁到 0039。种子库原地不改，但 `library_digest` 在副本上打开、副本照样迁移，算出的摘要也是 0039 之后的。新增的 `holder_id` 列进摘要，所以库里有 pending 或 committed 事件行的，摘要会变一次；含库摘要的训练 `config_hash` 也跟着变一次。没有事件行的库摘要不变；事件行全是 abandoned 的也不变，因为摘要对 abandoned 行只记 key 和状态。另有一种情况会再变一次：回填成 `''` 的 pending 行和零语句的 committed 行，被某个 holder 认领时 `holder_id` 改了，摘要也跟着变。

**已知残留。** 两处都只在写入时开着 `attribute_first_order_mental_to_holder` 时才会出错。这个策略默认关闭，没有哪份配置打开它。

- 裁定 R16：0039 回填取的是事件行里第一条查得到的语句的 holder。开着这个策略时，一阶欲望类语句记在欲望的主体名下，不在写入者名下；这样一条语句排在第一位时，行会被回填给主体。
- 裁定 R9：0039 之前崩溃留下的 pending 行（语句已落库、收据没写成）被另一个 holder B 认领时，补齐只收 B 名下的语句。开着这个策略时，原写入者的抽取可能把部分语句记在 B 名下，B 会把这些语句当成自己这条事件的结果。

（第 3–6 期的答复在各期合并时补充。）
