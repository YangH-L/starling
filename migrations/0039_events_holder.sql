-- 0039_events_holder.sql:events 按 holder 区分(记忆服务设计 §4.3)。
-- 0038 的主键是 (tenant_id, key):一个世界里两个 holder 写同一个 key,后写的被当成重入,
-- 拿到先写者名下的语句 id。改为 (tenant_id, holder_id, key)。seq 仍是世界一级的,
-- UNIQUE (tenant_id, seq) 不变(next_seq 按世界取号,事件时钟在一个世界里单调递增)。
-- SQLite 不能改主键,按「建新表、复制、删旧表、改名」重建。MigrationRunner 已把本文件
-- 包在 BEGIN IMMEDIATE 里,这里不另开事务。
--
-- 旧行回填:holder_id 取 statement_ids_json 里第一条在本租户 statements 表里查得到的
-- 语句的 holder_id(按数组顺序);一条都查不到(pending、abandoned、零语句的 committed,
-- 或列被写坏、不是 JSON 数组)就是 ''。坏列当空,与 memory_events.cpp 的
-- parse_statement_ids 同一口径,迁移不因一行坏数据失败。CASE 按顺序短路求值,
-- json_type 不会碰到非法 JSON。'' 行由第一个碰到它的 holder 认领(见
-- memory_events.cpp 的 read_or_claim_event)。

CREATE TABLE events_new (
    tenant_id          TEXT NOT NULL,
    holder_id          TEXT NOT NULL DEFAULT '',
    key                TEXT NOT NULL,
    text               TEXT NOT NULL,
    interlocutor       TEXT NOT NULL,
    text_sha256        TEXT NOT NULL,
    engram_ref         TEXT,
    statement_ids_json TEXT NOT NULL DEFAULT '[]',
    seq                INTEGER NOT NULL,
    state              TEXT NOT NULL CHECK (state IN ('pending','committed','abandoned')),
    failure_category   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (tenant_id, holder_id, key),
    UNIQUE (tenant_id, seq)
);

INSERT INTO events_new (
    tenant_id, holder_id, key, text, interlocutor, text_sha256, engram_ref,
    statement_ids_json, seq, state, failure_category
)
SELECT e.tenant_id,
       COALESCE((SELECT s.holder_id
                   FROM json_each(CASE WHEN NOT json_valid(e.statement_ids_json) THEN '[]'
                                       WHEN json_type(e.statement_ids_json) <> 'array' THEN '[]'
                                       ELSE e.statement_ids_json END) AS ids
                   JOIN statements AS s
                     ON s.tenant_id = e.tenant_id AND s.id = ids.value
                  ORDER BY ids.key
                  LIMIT 1), ''),
       e.key, e.text, e.interlocutor, e.text_sha256, e.engram_ref,
       e.statement_ids_json, e.seq, e.state, e.failure_category
FROM events AS e;

DROP TABLE events;
ALTER TABLE events_new RENAME TO events;

-- 不另建 (tenant_id, holder_id, key) 索引:主键已自动建了同列的索引(裁定 R8)。
CREATE INDEX idx_events_state ON events(tenant_id, state);
