"""Read-only SQLite inspection queries for the dashboard.

Opens a fresh read-only connection per call (the C++ read-only URI + PRAGMA query_only).
Every public query takes a keyword-only ``frozen`` flag (default False) that is passed to
``_core.read_only_uri``; the inspect routes pass ``_core.world_read_only_frozen(record)`` (R36).
List endpoints return rows as dicts keyed by cursor column names, so they are
robust to schema column additions. The engine remains the single writer.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from starling import _core


@contextmanager
def open_ro(db_path: str, frozen: bool = False):
    # 裁定 R36：URI 取自 C++（_core.read_only_uri）。frozen 默认 False（普通 mode=ro，别的连接可以照常写，读事务
    # 一致）。只读打开的世界（种子、只读、可回收）由调用方传 _core.world_read_only_frozen(record)=True：WAL 头、
    # 旁边没有 -wal/-shm 时走 immutable=1，不在库旁边新建文件。
    uri = _core.read_only_uri(str(db_path), frozen=frozen)
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.row_factory = sqlite3.Row
        yield conn
    finally:
        conn.close()


def _rows(conn, sql: str, params: tuple = ()) -> list[dict]:
    cur = conn.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def _count(conn, table: str, tenant: str | None) -> int:
    if tenant is None:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    try:
        return conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE tenant_id = ?", (tenant,)
        ).fetchone()[0]
    except sqlite3.OperationalError:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def overview(db_path: str, tenant: str, *, frozen: bool = False) -> dict:
    with open_ro(db_path, frozen) as conn:
        commit_states = _rows(
            conn,
            "SELECT state, COUNT(*) AS n FROM commitments WHERE tenant_id=? GROUP BY state",
            (tenant,),
        )
        queue = _rows(
            conn,
            "SELECT dispatch_status, COUNT(*) AS n FROM bus_events WHERE tenant_id=? "
            "GROUP BY dispatch_status",
            (tenant,),
        )
        return {
            "counts": {
                "statements": _count(conn, "statements", tenant),
                "statement_edges": _count(conn, "statement_edges", tenant),
                "cognizers": _count(conn, "cognizers", tenant),
                "commitments": _count(conn, "commitments", tenant),
                "bus_events": _count(conn, "bus_events", tenant),
            },
            "commitments_by_state": {r["state"]: r["n"] for r in commit_states},
            "queue_by_status": {r["dispatch_status"]: r["n"] for r in queue},
        }


def statements(db_path: str, tenant: str, *, holder: str = "", perspective: str = "",
               predicate: str = "", review_status: str = "", consolidation_state: str = "",
               modality: str = "", belief_order: str = "", subject_kind: str = "",
               limit: int = 100, offset: int = 0, frozen: bool = False) -> dict:
    where = ["tenant_id = ?"]
    params: list = [tenant]
    # review_status 过滤(片 6 审批队列复用本查询:筛 review_requested)。
    for col, val in (("holder_id", holder), ("holder_perspective", perspective),
                     ("predicate", predicate), ("review_status", review_status)):
        if val:
            where.append(f"{col} = ?")
            params.append(val)
    # T0b — consolidation_state 过滤:支持逗号分隔多值(海马三态一次筛),
    # 值域以 C++ ConsolidationState 枚举为准(见 statement_enums.hpp),本函数
    # 不硬编码语义、纯参数化传值(无注入面)。
    if consolidation_state:
        states = [s.strip() for s in consolidation_state.split(",") if s.strip()]
        if states:
            placeholders = ",".join("?" for _ in states)
            where.append(f"consolidation_state IN ({placeholders})")
            params.extend(states)
    # T0d-1 — modality 过滤:同款逗号分隔多值范式(新皮层 Semantic/Norms 子区深链
    # 一次筛多个 modality),值域以 C++ modality 枚举为准,本函数不硬编码语义、
    # 纯参数化传值(无注入面)。
    if modality:
        modalities = [m.strip() for m in modality.split(",") if m.strip()]
        if modalities:
            placeholders = ",".join("?" for _ in modalities)
            where.append(f"modality IN ({placeholders})")
            params.extend(modalities)
    # T0e ② — 信念阶层过滤:nesting_depth 是 INTEGER,语义是比较而非多值 IN。
    # belief_order 空=不筛;"first"=一阶(nesting_depth = 0);"higher"=二阶及以上
    # (nesting_depth >= 1)。本函数只翻译这两个固定取值,不接受任意 SQL/比较符,
    # 前端也不硬编码 WHERE 语义。
    if belief_order == "first":
        where.append("nesting_depth = 0")
    elif belief_order == "higher":
        where.append("nesting_depth >= 1")
    # T0e ① — subject_kind 过滤:精确匹配(DB CHECK 值域 cognizer/entity,见 0001),
    # 本函数不硬编码语义、纯参数化传值。
    if subject_kind:
        where.append("subject_kind = ?")
        params.append(subject_kind)
    clause = " AND ".join(where)
    with open_ro(db_path, frozen) as conn:
        rows = _rows(
            conn,
            f"SELECT id, holder_id, holder_perspective, subject_id, predicate, "
            f"object_kind, object_value, modality, polarity, confidence, salience, "
            f"observed_at, review_status, consolidation_state, nesting_depth "
            f"FROM statements WHERE {clause} ORDER BY observed_at DESC "
            f"LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        edges = _rows(
            conn,
            "SELECT src_id, dst_id, edge_kind, weight FROM statement_edges "
            "WHERE tenant_id=? LIMIT 2000",
            (tenant,),
        )
        return {"rows": rows, "edges": edges}


def statement_by_id(db_path: str, tenant: str, statement_id: str, *, frozen: bool = False) -> dict | None:
    """Single statement by id, tenant-scoped. None when absent or cross-tenant."""
    with open_ro(db_path, frozen) as conn:
        rows = _rows(
            conn,
            "SELECT id, holder_id, holder_perspective, subject_id, predicate, "
            "object_kind, object_value, modality, polarity, confidence, salience, "
            "observed_at, review_status, consolidation_state, nesting_depth, "
            "created_at, updated_at "
            "FROM statements WHERE tenant_id = ? AND id = ?",
            (tenant, statement_id),
        )
        return rows[0] if rows else None


def cognizers(db_path: str, tenant: str, *, frozen: bool = False) -> dict:
    with open_ro(db_path, frozen) as conn:
        nodes = _rows(
            conn,
            "SELECT id, kind, canonical_name, last_seen_at FROM cognizers "
            "WHERE tenant_id=? AND archived_at IS NULL "
            "ORDER BY last_seen_at DESC LIMIT 500",
            (tenant,),
        )
        rels = _rows(
            conn,
            "SELECT a_id, b_id, affinity, power_asymmetry, fiske_weights_json "
            "FROM cognizer_relations "
            "WHERE tenant_id=? LIMIT 2000",
            (tenant,),
        )
        presence = _rows(
            conn,
            "SELECT cognizer_id, observed_at, channel FROM cognizer_presence_log "
            "WHERE tenant_id=? ORDER BY observed_at DESC LIMIT 200",
            (tenant,),
        )
        return {"nodes": nodes, "relations": rels, "presence": presence}


def commitments(db_path: str, tenant: str, *, frozen: bool = False) -> dict:
    with open_ro(db_path, frozen) as conn:
        rows = _rows(
            conn,
            "SELECT c.stmt_id, c.state, c.broken_count, c.deadline, c.created_at, "
            "c.updated_at, s.subject_id, s.predicate, s.object_value "
            "FROM commitments c LEFT JOIN statements s "
            "ON s.id = c.stmt_id AND s.tenant_id = c.tenant_id "
            "WHERE c.tenant_id=? ORDER BY c.updated_at DESC LIMIT 500",
            (tenant,),
        )
        triggers = _rows(
            conn,
            "SELECT commitment_stmt_id, kind, status, spec_json FROM commitment_triggers "
            "WHERE tenant_id=? LIMIT 1000",
            (tenant,),
        )
        return {"rows": rows, "triggers": triggers}


def replay(db_path: str, tenant: str, *, frozen: bool = False) -> dict:
    with open_ro(db_path, frozen) as conn:
        state = _rows(conn, "SELECT * FROM replay_scheduler_state WHERE id=1")
        ledger = _rows(
            conn,
            "SELECT replay_batch_id, mode, sampled_count, ops_applied_json, "
            "started_at, finished_at "
            "FROM replay_ledger WHERE tenant_id=? OR tenant_id IS NULL "
            "ORDER BY started_at DESC LIMIT 100",
            (tenant,),
        )
        windows = _rows(
            conn,
            "SELECT stmt_id, opened_at, close_deadline, status FROM reconsolidation_windows "
            "WHERE tenant_id=? ORDER BY opened_at DESC LIMIT 200",
            (tenant,),
        )
        return {"scheduler": state[0] if state else {}, "ledger": ledger, "windows": windows}


def conflicts(db_path: str, tenant: str, *, frozen: bool = False) -> dict:
    with open_ro(db_path, frozen) as conn:
        by_kind = _rows(
            conn,
            "SELECT edge_kind, COUNT(*) AS n FROM statement_edges WHERE tenant_id=? "
            "GROUP BY edge_kind",
            (tenant,),
        )
        edges = _rows(
            conn,
            "SELECT e.src_id, e.dst_id, e.edge_kind, e.weight, e.metadata_json, "
            "s.subject_id AS src_subject, s.predicate AS src_predicate, s.object_value AS src_object, "
            "s.consolidation_state AS src_state, "
            "d.subject_id AS dst_subject, d.predicate AS dst_predicate, d.object_value AS dst_object, "
            "d.consolidation_state AS dst_state "
            "FROM statement_edges e "
            "LEFT JOIN statements s ON s.id = e.src_id AND s.tenant_id = e.tenant_id "
            "LEFT JOIN statements d ON d.id = e.dst_id AND d.tenant_id = e.tenant_id "
            "WHERE e.tenant_id=? AND e.edge_kind='conflicts_with' "
            "ORDER BY e.weight DESC LIMIT 500",
            (tenant,),
        )
        return {"by_kind": {r["edge_kind"]: r["n"] for r in by_kind}, "conflicts": edges}


def gists(db_path: str, tenant: str, *, frozen: bool = False) -> dict:
    """#38-C v2 observability: consolidation NORM gists (provenance=
    consolidation_abstract) — the LLM-judged, gated, consolidated norms — with
    their summary/confidence, derived_from lineage, and lifecycle state. Read-only
    检视; tenant-scoped. by_state counts every state (volatile = ungated/inert,
    consolidated = verified+promoted, archived = conflict/decayed, forgotten)."""
    with open_ro(db_path, frozen) as conn:
        by_state = _rows(
            conn,
            "SELECT consolidation_state, COUNT(*) AS n FROM statements "
            "WHERE tenant_id=? AND provenance='consolidation_abstract' "
            "GROUP BY consolidation_state",
            (tenant,),
        )
        rows = _rows(
            conn,
            "SELECT id, holder_id, subject_id, predicate, object_value, confidence, "
            "consolidation_summary, consolidation_state, review_status, "
            "derived_from_json, derived_depth, created_at, updated_at "
            "FROM statements WHERE tenant_id=? AND provenance='consolidation_abstract' "
            "ORDER BY created_at DESC LIMIT 500",
            (tenant,),
        )
        for row in rows:
            # _safe_json → (value, ok); take the value (malformed/NULL → []).
            row["derived_from"] = _safe_json(row.pop("derived_from_json"), [])[0]
        return {"by_state": {r["consolidation_state"]: r["n"] for r in by_state},
                "gists": rows}


def gist_members(db_path: str, tenant: str, gist_id: str, *, frozen: bool = False) -> dict:
    """#38-C v2 obs: the source-cluster members a gist generalizes — the specific
    statements in its derived_from (lineage drill-down for /gists). Read-only,
    tenant-scoped; the gist's own derived_from ids are the trusted, internal keys."""
    with open_ro(db_path, frozen) as conn:
        row = conn.execute(
            "SELECT derived_from_json FROM statements "
            "WHERE id=? AND tenant_id=? AND provenance='consolidation_abstract'",
            (gist_id, tenant),
        ).fetchone()
        if row is None:
            return {"gist_id": gist_id, "members": []}
        ids, _ = _safe_json(row[0], [])
        if not ids:
            return {"gist_id": gist_id, "members": []}
        placeholders = ",".join("?" for _ in ids)
        members = _rows(
            conn,
            "SELECT id, holder_id, subject_id, predicate, object_value, "
            "consolidation_state, review_status, confidence FROM statements "
            f"WHERE tenant_id=? AND id IN ({placeholders}) ORDER BY holder_id",
            (tenant, *ids),
        )
        return {"gist_id": gist_id, "members": members}


def queues(db_path: str, tenant: str, *, frozen: bool = False) -> dict:
    with open_ro(db_path, frozen) as conn:
        dispatch = _rows(
            conn,
            "SELECT dispatch_status, COUNT(*) AS n FROM bus_events WHERE tenant_id=? "
            "GROUP BY dispatch_status",
            (tenant,),
        )
        backlog = conn.execute(
            "SELECT COUNT(*) FROM statements s LEFT JOIN statement_vectors v "
            "ON v.stmt_id = s.id AND v.tenant_id = s.tenant_id "
            "WHERE s.tenant_id=? AND v.stmt_id IS NULL",
            (tenant,),
        ).fetchone()[0]
        vec = _rows(
            conn,
            "SELECT status, COUNT(*) AS n FROM statement_vectors WHERE tenant_id=? "
            "GROUP BY status",
            (tenant,),
        )
        return {
            "dispatch": {r["dispatch_status"]: r["n"] for r in dispatch},
            "embedding_backlog": backlog,
            "vectors_by_status": {r["status"]: r["n"] for r in vec},
        }


# ── Vitals: Phase 0 read-only observability (pipeline lag + lifecycle stuck) ──
#
#   subscriber-pump checkpoints (global single-row) ─┐
#   bus_events.outbox_sequence (global monotonic) ───┼─► lag = head − cursor
#                                                     │   (GLOBAL signal, not
#                                                     │    tenant-scoped: the
#                                                     │    pumps process every
#                                                     │    tenant's events)
#   statements (tenant) ─► VOLATILE-stuck ────────────┤
#   extraction_attempt⋈pipeline_run (tenant) ─► fails ─┤  (tenant-scoped)
#   reconsolidation_windows (tenant) ─► overdue ──────┘
#
# Each block degrades to ok=False (empty) if its table is absent (migration not
# run) rather than 500-ing — the dashboard is a diagnostic surface, the worst
# case is a missing panel, never a crash. Embedded-mode caveat for the UI: lag
# reflects "distance since the last tick processed events", not a distributed
# queue depth — pumps advance synchronously inside tick()/remember().
_PUMP_CHECKPOINTS = (
    # (pump label, checkpoint table, cursor column)
    ("belief_tracker", "tom_belief_tracker_checkpoint", "last_processed_outbox_sequence"),
    ("reconsolidation", "reconsolidation_checkpoint", "last_processed_outbox_sequence"),
    ("projection", "projection_subscriber_checkpoint", "last_processed_outbox_sequence"),
    ("common_ground", "common_ground_subscriber_checkpoint", "last_processed_outbox_sequence"),
    ("persona", "persona_subscriber_checkpoint", "last_processed_outbox_sequence"),
    ("policy_engine", "policy_engine_checkpoint", "seq"),
)


def _rows_or_empty(conn, sql: str, params: tuple = ()):
    """(rows, ok) — ok=False (empty rows) when the table is absent."""
    try:
        return _rows(conn, sql, params), True
    except sqlite3.OperationalError:
        return [], False


def _count_or_zero(conn, sql: str, params: tuple = ()) -> int:
    try:
        return conn.execute(sql, params).fetchone()[0]
    except sqlite3.OperationalError:
        return 0


def brain_map(db_path: str, tenant: str, *, frozen: bool = False) -> dict:
    """Phase 3 片 1 — 类脑 IA 落地页:9 脑区活体计数(经 plan-design-review 定稿)。
    每区一个只读计数(tenant-scoped,逐区 _count_or_zero 独立降级,缺表→0);
    dormant=True 标记尚未落地的区(片 3 落地后全 9 区皆 active);落地页静默显、
    导航空组不渲染。对话/配置/透视镜无「容量」语义 → count=None(不显计数徽标)。脑区句式与排序
    与 nav 一致:功能名 + 脑区 gloss,按记忆流(输入→快存→慢存→他者→意图→固化→
    内省→体征→配置)。consolidation_state 在 DB 存小写串。"""
    with open_ro(db_path, frozen) as conn:
        def n(sql: str, params: tuple = ()) -> int:
            return _count_or_zero(conn, sql, params)
        regions = [
            {"key": "converse", "label": "对话", "region": None,
             "href": "/converse", "count": None, "dormant": False},
            {"key": "short_term", "label": "短期记忆", "region": "海马",
             "href": "/working-set", "dormant": False,
             "count": n("SELECT COUNT(*) FROM statements WHERE tenant_id=? AND "
                        "consolidation_state IN ('volatile','replaying_consolidating')", (tenant,))},
            {"key": "long_term", "label": "长期记忆", "region": "新皮层",
             "href": "/statements", "dormant": False,
             "count": n("SELECT COUNT(*) FROM statements WHERE tenant_id=? AND "
                        "consolidation_state='consolidated'", (tenant,))},
            {"key": "theory_of_mind", "label": "他者心智", "region": "心智化",
             "href": "/cognizers", "dormant": False,
             "count": n("SELECT COUNT(*) FROM cognizers WHERE tenant_id=?", (tenant,))},
            {"key": "prospective", "label": "意图与承诺", "region": "前额叶",
             "href": "/commitments", "dormant": False,
             "count": n("SELECT COUNT(*) FROM commitments WHERE tenant_id=?", (tenant,))},
            {"key": "consolidation", "label": "睡眠与固化", "region": "回放",
             "href": "/replay", "dormant": False,
             "count": n("SELECT COUNT(*) FROM replay_ledger "
                        "WHERE tenant_id=? OR tenant_id IS NULL", (tenant,))},
            {"key": "lens", "label": "透视镜", "region": None,
             "href": "/lens", "count": None, "dormant": False},  # 片 3 已落地(来源取证)
            {"key": "vitals", "label": "生命体征", "region": "脑干",
             "href": "/vitals", "dormant": False,
             "count": n("SELECT COUNT(*) FROM bus_events WHERE tenant_id=? AND "
                        "dispatch_status='pending'", (tenant,))},   # 出箱待派发(0=健康)
            {"key": "config", "label": "配置", "region": None,
             "href": "/settings", "count": None, "dormant": False},
        ]
        return {"regions": regions}


# ── Phase 3 片 3 — 透视镜(Lens):来源取证(只读检视) ─────────────────────
# statement 不直接存 extraction_span_key;它的 source_spans 存 engram_ref/chunk,
# span_key = sha256(engram_ref⏟chunk⏟predicate⏟canonical_object_hash)由 C++
# compute_extraction_span_key 算出。**绝不在 Python 复算该哈希**(那是核心语义、
# 换绑定语言要重写=出界)。链路改走 C++ 已写入的 bus event:statement.written 事件
# payload 同时带 stmt_id 与 extraction_span_key → extraction_attempt。Python 只读。

_PROV_PREVIEW_CHARS = 280   # engram 源文预览上限(privacy:只取 inline、未抹除的前 N 字符)
# 预览抑制:除已抹除/非 inline 外,也尊重系统隐私分级——regulated/sensitive 不显源文
# (检索层早有抹除过滤,但 provenance 直读 payload_inline 绕过它,故在此守同一隐私不变式)。
_PREVIEW_SUPPRESS_PRIVACY = ("regulated", "sensitive", "personal")  # PII 级也不显源文

_PROV_STMT_COLS = (
    "id, holder_id, holder_perspective, subject_kind, subject_id, predicate, "
    "object_kind, object_value, modality, polarity, confidence, salience, "
    "consolidation_state, review_status, provenance, derived_depth, nesting_depth, "
    "observed_at, created_at, updated_at, evidence_json, derived_from_json, supersedes_id"
)


def _safe_json(raw, fallback):
    """(value, ok) — ok=False(→fallback)当列为 malformed/NULL/空。片 3 失败模式:
    遇坏 JSON 列不崩树,节点标注「无法解析」。"""
    if raw is None or raw == "":
        return fallback, True
    try:
        return json.loads(raw), True
    except (ValueError, TypeError):
        return fallback, False


def _extraction_for(conn, tenant: str, stmt_id: str) -> dict | None:
    """回溯某语句到创建它的 extraction_attempt(状态/原始 LLM 输出/error),只读。
    链路:bus_events(event_type=statement.written, primary_id=stmt_id)的 C++ 写入
    payload 带 extraction_span_key → extraction_attempt。派生/推断/系统写入无此事件
    → None(诚实:非抽取来源)。缺表(bus_events/extraction_attempt)→ 降级 None。

    实证发现(extractor.cpp):每 span_key 可有多行——创建语句的 success + 重复 remember
    的 noop;**原始 LLM 输出只在失败/解析失败行留底**(成功路径不存 raw)。故:
      ① 权威行 = 优先 success/partial(语句真正的创建),noop/failed 次之;
      ② raw LLM 输出从同一 pipeline_run 的失败/部分尝试取(failed_attempts)——这才是
         「回溯到原始 LLM 输出」的落点。span_key 哈希由 C++ 算,Python 只读、绝不复算。"""
    # 过滤 aggregate_id(非 primary_id):二者对 statement.written 同为 stmt_id
    # (statement_writer.cpp:349-350),但 aggregate_id 有 idx_bus_events_aggregate
    # → 索引检索而非全表扫(否则 provenance 递归每节点全扫 append-only 出箱表)。
    ev, _ = _rows_or_empty(
        conn,
        "SELECT payload_json FROM bus_events WHERE tenant_id=? AND aggregate_id=? "
        "AND event_type='statement.written' ORDER BY outbox_sequence ASC LIMIT 1",
        (tenant, stmt_id),
    )
    if not ev:
        return None
    payload, ok = _safe_json(ev[0]["payload_json"], {})
    span_key = payload.get("extraction_span_key") if ok and isinstance(payload, dict) else None
    if not span_key:
        return None
    empty = {"span_key": span_key, "status": None, "attempt_number": None,
             "raw_output": None, "error": None, "created_at": None,
             "run_id": None, "prompt_tokens": None, "completion_tokens": None,
             "total_tokens": None, "latency_ms": None, "failed_attempts": []}
    rows, _ = _rows_or_empty(
        conn,
        "SELECT pipeline_run_id, status, attempt_number, raw_output, error, created_at, "
        "prompt_tokens, completion_tokens, total_tokens, latency_ms "
        "FROM extraction_attempt WHERE extraction_span_key=? "
        "ORDER BY CASE status WHEN 'success' THEN 0 WHEN 'partial_success' THEN 1 "
        "WHEN 'failed' THEN 2 ELSE 3 END, created_at ASC LIMIT 1",
        (span_key,),
    )
    if not rows:
        return empty   # event 记了 span_key 但 ledger 无对应行(罕见:非抽取写入路径)
    a = rows[0]
    run_id = a["pipeline_run_id"]
    failed: list = []
    if run_id:
        # 同一 run 的失败/部分尝试携带原始 LLM 输出(成功不留底)——取证关键。
        failed, _ = _rows_or_empty(
            conn,
            "SELECT attempt_number, status, raw_output, error, created_at, "
            "prompt_tokens, completion_tokens, total_tokens, latency_ms "
            "FROM extraction_attempt WHERE pipeline_run_id=? "
            "AND status IN ('failed','partial_success') "
            "ORDER BY attempt_number ASC LIMIT 10",
            (run_id,),
        )
    return {"span_key": span_key, "status": a["status"],
            "attempt_number": a["attempt_number"], "raw_output": a["raw_output"],
            "error": a["error"], "created_at": a["created_at"],
            "run_id": run_id,
            "prompt_tokens": a["prompt_tokens"],
            "completion_tokens": a["completion_tokens"],
            "total_tokens": a["total_tokens"], "latency_ms": a["latency_ms"],
            "failed_attempts": failed}


def _engrams_for(conn, tenant: str, evidence) -> list[dict]:
    """解析 evidence_json([{engram_ref,content_hash,status}])→ 逐条挂上 engram 元数据
    + 有界源文预览(只读)。privacy:仅 inline 且未抹除取前 _PROV_PREVIEW_CHARS 字符;
    uri-only / 已抹除 → 无预览。engram 缺失 → engram=None(孤儿证据,不崩)。"""
    out: list[dict] = []
    if not isinstance(evidence, list):
        return out
    for e in evidence:
        d = e if isinstance(e, dict) else {}
        ref = d.get("engram_ref")
        node = {"engram_ref": ref, "content_hash": d.get("content_hash"),
                "status": d.get("status"), "engram": None}
        if ref:
            rows, _ = _rows_or_empty(
                conn,
                "SELECT source_kind, privacy_class, created_at, erased_at, "
                "payload_inline FROM engrams WHERE tenant_id=? AND id=?",
                (tenant, ref),
            )
            if rows:
                g = rows[0]
                preview = None
                if (g["erased_at"] is None and g["payload_inline"] is not None
                        and (g["privacy_class"] or "").lower() not in _PREVIEW_SUPPRESS_PRIVACY):
                    blob = g["payload_inline"]
                    try:
                        text = (blob.decode("utf-8", "replace")
                                if isinstance(blob, (bytes, bytearray)) else str(blob))
                        preview = text[:_PROV_PREVIEW_CHARS]
                    except Exception:
                        preview = None
                node["engram"] = {
                    "source_kind": g["source_kind"], "privacy_class": g["privacy_class"],
                    "created_at": g["created_at"], "erased": g["erased_at"] is not None,
                    "payload_preview": preview,
                }
        out.append(node)
    return out


def provenance(db_path: str, tenant: str, statement_id: str, *, max_depth: int = 6, frozen: bool = False) -> dict | None:
    """片 3 透视镜:某语句的来源取证树(只读检视)。open_ro = 物理只读(query_only),
    故保证不写 statement.recalled、不沉淀(片 3 验收的「不 emit / 不写」由构造满足)。
    递归解析 statements 的 JSON 列 derived_from / supersedes(**非 SQL join**),逐节点
    回溯 extraction_attempt + 证据 engram。返回 None 当根语句缺失/跨租户(路由→404)。
    有界:max_depth 截断 + seen 去重(共享父 / supersedes 环 → 标 repeat,不展开,不爆栈)。"""
    with open_ro(db_path, frozen) as conn:
        seen: set[str] = set()

        def build(sid: str, depth: int) -> dict:
            rows, _ = _rows_or_empty(
                conn,
                f"SELECT {_PROV_STMT_COLS} FROM statements WHERE tenant_id=? AND id=?",
                (tenant, sid),
            )
            if not rows:
                return {"id": sid, "found": False}   # 孤儿父 / 跨租户引用,不崩
            r = rows[0]
            summary = {"subject_id": r["subject_id"], "predicate": r["predicate"],
                       "object_value": r["object_value"]}
            if sid in seen:
                # 已在上文展开(共享父或环):给摘要,不再递归。
                return {"id": sid, "found": True, "repeat": True, "summary": summary}
            seen.add(sid)
            ev, ev_ok = _safe_json(r["evidence_json"], [])
            df, df_ok = _safe_json(r["derived_from_json"], [])
            stmt = {k: r[k] for k in r.keys()
                    if k not in ("evidence_json", "derived_from_json")}
            node = {
                "id": sid, "found": True,
                "statement": stmt,
                "summary": summary,
                "origin": {"provenance": r["provenance"],
                           "extraction": _extraction_for(conn, tenant, sid)},
                "evidence": _engrams_for(conn, tenant, ev),
                "evidence_parse_error": not ev_ok,
                "derived_from_parse_error": not df_ok,
                "derived_from": [],
                "supersedes": None,
                "truncated": False,
            }
            has_deeper = bool((isinstance(df, list) and df) or r["supersedes_id"])
            if depth >= max_depth:
                node["truncated"] = has_deeper   # 到顶不再展开,诚实标注还有更深来源
                return node
            if isinstance(df, list):
                node["derived_from"] = [build(str(p), depth + 1) for p in df if p]
            if r["supersedes_id"]:
                node["supersedes"] = build(str(r["supersedes_id"]), depth + 1)
            return node

        root = build(str(statement_id), 0)
        return root if root.get("found") else None


def cascade_preview(db_path: str, tenant: str, statement_id: str,
                    *, max_depth: int = 6, max_nodes: int = 200, frozen: bool = False) -> dict | None:
    """片 6 级联预览:遗忘某语句前,预览会级联波及哪些「派生自它」的语句(只读检视)。

    与 provenance 反向——provenance 上溯祖先(我从何而来);这里下溯后代(谁派生自我):
    BFS statements.derived_from_json(该 JSON 数组含本 id 的行即直接后代)逐层展开,
    seen 去重(环不爆栈),max_depth 截断(truncated 标注还有更深)。tenant-scoped。
    返回 None 当根语句缺失/跨租户(路由 → 404)。

    inform-only:本接口只读,绝不改记忆。实际遗忘仍是 /api/forget 的单条逻辑删除——
    后代不会被自动级联删除;预览只是让用户看清「遗忘它,这 N 条派生记忆将成为孤儿派生
    (其 derived_from 指向一条已 forgotten 的源)」后再决定。级联删除是另一回事(核心写,
    另议),不在此。"""
    with open_ro(db_path, frozen) as conn:
        root, _ = _rows_or_empty(
            conn, "SELECT id FROM statements WHERE tenant_id=? AND id=?",
            (tenant, statement_id))
        if not root:
            return None
        # One scan of the tenant's edges; parse each row's derived_from_json in Python
        # (TOLERANT, via _safe_json) so a single malformed row only skips itself rather
        # than aborting the whole walk — a SQL json_each(EXISTS) approach raises on the
        # first bad row, which _rows_or_empty would swallow into a false "0 affected"
        # (a safety green-light that isn't true). Build the reverse parent→children
        # graph once, then BFS in memory (also avoids a per-node full-table scan).
        # The scan is tenant-wide and intentionally un-LIMITed (reverse edges need the
        # full edge set to be correct); memory is O(tenant statements) — fine at the
        # dashboard's single-store scale, and the returned payload is capped by max_nodes.
        rows, _ = _rows_or_empty(
            conn,
            "SELECT id, subject_id, predicate, object_value, consolidation_state, "
            "review_status, provenance, derived_from_json FROM statements "
            "WHERE tenant_id=? ORDER BY created_at ASC",
            (tenant,))
        children: dict[str, list[str]] = {}
        meta: dict[str, dict] = {}
        for row in rows:
            rid = str(row["id"])
            meta[rid] = {
                "id": rid, "subject_id": row["subject_id"], "predicate": row["predicate"],
                "object_value": row["object_value"],
                "consolidation_state": row["consolidation_state"],
                "review_status": row["review_status"], "provenance": row["provenance"]}
            parents, ok = _safe_json(row["derived_from_json"], [])
            if ok and isinstance(parents, list):
                for parent in parents:
                    if parent:
                        children.setdefault(str(parent), []).append(rid)

        seen: set[str] = {str(statement_id)}
        affected: list[dict] = []
        frontier = [str(statement_id)]
        depth = 0
        capped = False
        while frontier and depth < max_depth and not capped:
            next_frontier: list[str] = []
            for parent_id in frontier:
                for child_id in children.get(parent_id, []):
                    if child_id in seen:
                        continue
                    if len(affected) >= max_nodes:
                        capped = True
                        break
                    seen.add(child_id)
                    affected.append({**meta[child_id], "depth": depth + 1})
                    next_frontier.append(child_id)
                if capped:
                    break
            frontier = next_frontier
            depth += 1
        # truncated = hit the node cap, OR a frontier node at the depth cap still has an
        # unseen child beyond max_depth (mirror provenance's has-deeper check — a subtree
        # that fits exactly at the boundary reports False, not a misleading "+").
        deeper = any(child_id not in seen
                     for parent_id in frontier
                     for child_id in children.get(parent_id, []))
        return {"stmt_id": str(statement_id), "affected": affected,
                "affected_count": len(affected), "truncated": capped or deeper}


_ENGRAM_LIST_COLS = (
    "id, source_kind, privacy_class, retention_mode, refcount, created_at, "
    "erased_at, source_item_id, chunk_index, adapter_name"
)
# 刻意不含 redacted_content / key_ref / audit_trail_json:这三列绕过下面的隐私门。
# payload_inline 是取出来再按 erased/privacy_class 判定后才决定给不给预览的,而它们若
# 列在这里就会「无条件」随详情返回——包括已合规擦除(erased_at 非空)的 engram,以及
# regulated/sensitive/personal 分级的 engram。redacted_content 正是「脱敏后但仍敏感」
# 的正文。今天前端只渲染 payload_uri,危害是潜伏的:一旦有人照 api.ts 的契约把抽屉接上,
# 默认就泄露了。真要暴露审计轨迹,应当各自过一道与 preview 同款的门,而不是搭便车。
_ENGRAM_DETAIL_COLS = (
    "id, tenant_id, content_hash, source_kind, ingest_policy, ingest_mode, "
    "privacy_class, retention_mode, refcount, payload_uri, created_at, erased_at, "
    "adapter_name, adapter_version, source_item_id, source_version, chunk_index, "
    "declared_transformations_json, byte_preserving"
)


def engrams_list(db_path: str, tenant: str, *, source_kind: str = "",
                 privacy_class: str = "", erased: str = "", q: str = "",
                 limit: int = 100, offset: int = 0, frozen: bool = False) -> dict:
    """T0a — 原始数据·证据浏览:engram(不可变原文证据)列表(只读派生查询,
    tenant-scoped)。engram 是记忆流最上游源头(先于海马/新皮层),此前无专属
    端点/页面,只能从某条 statement 反向溯源(provenance._engrams_for)时顺带
    瞥见。本函数只列展示列,**不带 payload_inline**(源文预览是单条详情
    engram_detail 的 privacy-gated 特权,列表页不泄露)。"""
    where = ["tenant_id = ?"]
    params: list = [tenant]
    if source_kind:
        where.append("source_kind = ?")
        params.append(source_kind)
    if privacy_class:
        where.append("privacy_class = ?")
        params.append(privacy_class)
    if erased == "yes":
        where.append("erased_at IS NOT NULL")
    elif erased == "no":
        where.append("erased_at IS NULL")
    if q:
        where.append("(content_hash LIKE ? OR source_item_id LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like])
    clause = " AND ".join(where)
    with open_ro(db_path, frozen) as conn:
        rows = _rows(
            conn,
            f"SELECT {_ENGRAM_LIST_COLS} FROM engrams WHERE {clause} "
            f"ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        total = conn.execute(
            f"SELECT COUNT(*) FROM engrams WHERE {clause}", tuple(params)
        ).fetchone()[0]
        return {"rows": rows, "total": total}


def engram_detail(db_path: str, tenant: str, engram_id: str, *, frozen: bool = False) -> dict | None:
    """T0a — 单条 engram 详情(只读派生查询,tenant-scoped)。payload 预览**复用
    _engrams_for/provenance 的同款隐私抑制规则**(_PROV_PREVIEW_CHARS +
    _PREVIEW_SUPPRESS_PRIVACY):只对 inline、未抹除、非受限 privacy_class 的
    engram 给前 280 字符预览,否则 preview=None 且带诚实的抑制理由(而非静默
    空白)。同时反查引用它的 statements(evidence_json LIKE 该 id,best-effort、
    有界 limit 50)——让 refcount 可展开成「哪些 statement 引用了它」。
    跨租户/不存在 → None(路由转 404)。"""
    with open_ro(db_path, frozen) as conn:
        rows = _rows(
            conn,
            f"SELECT {_ENGRAM_DETAIL_COLS}, payload_inline FROM engrams "
            f"WHERE tenant_id=? AND id=?",
            (tenant, engram_id),
        )
        if not rows:
            return None
        g = rows[0]
        blob = g.pop("payload_inline")
        erased = g["erased_at"] is not None
        privacy = (g["privacy_class"] or "").lower()
        preview = None
        reason = None
        if erased:
            reason = "该证据已被合规擦除(erased_at 非空)"
        elif blob is None:
            reason = "该证据仅存 URI(非 inline),不支持源文预览"
        elif privacy in _PREVIEW_SUPPRESS_PRIVACY:
            reason = f"该证据隐私分级为 {privacy},预览已抑制"
        else:
            try:
                text = (blob.decode("utf-8", "replace")
                        if isinstance(blob, (bytes, bytearray)) else str(blob))
                preview = text[:_PROV_PREVIEW_CHARS]
            except Exception:
                reason = "预览解码失败"
        referencing, _ = _rows_or_empty(
            conn,
            "SELECT id, subject_id, predicate FROM statements "
            "WHERE tenant_id=? AND evidence_json LIKE ? LIMIT 50",
            (tenant, f"%{engram_id}%"),
        )
        return {"engram": g, "preview": preview,
                "preview_suppressed_reason": reason,
                "referencing_statements": referencing}


def personae(db_path: str, tenant: str, *, frozen: bool = False) -> dict:
    """T0d-2 — 新皮层·画像(Persona):某租户的 persona 容器(只读派生查询,
    tenant-scoped)。数据源 containers WHERE kind='persona'(migrations/0001)。
    scope_descriptor 是 canonical JSON(此处原样带出,前端只读展示,绝不复算)。
    容器的内容/合并语义在 C++ 内核,dashboard 只列元数据快照。"""
    with open_ro(db_path, frozen) as conn:
        rows = _rows(
            conn,
            "SELECT id, holder_id, scope_descriptor, created_at, updated_at, version "
            "FROM containers WHERE tenant_id=? AND kind='persona' "
            "ORDER BY updated_at DESC",
            (tenant,),
        )
        return {"rows": rows}


def common_ground(db_path: str, tenant: str, *, frozen: bool = False) -> dict:
    """T0d-2 — 新皮层·共识(CommonGround):某租户的 common_ground 行(只读派生查询,
    tenant-scoped)。数据源 common_ground 表(migrations/0010),五态 status。LEFT JOIN
    statements 带出被共识语句的 subject/predicate/object(JOIN 也带 tenant_id 约束,
    跨租户语句永不泄露;语句被遗忘/缺失 → 三列为 NULL,不崩)。by_status 给五态计数,
    供前端按状态分组概览。共识的推进/状态机在 C++,dashboard 只读快照。"""
    with open_ro(db_path, frozen) as conn:
        rows = _rows(
            conn,
            "SELECT cg.id, cg.statement_id, cg.status, cg.parties_json, "
            "cg.grounded_at, cg.last_confirmed_at, "
            "s.subject_id, s.predicate, s.object_value "
            "FROM common_ground cg "
            "LEFT JOIN statements s ON s.id=cg.statement_id AND s.tenant_id=cg.tenant_id "
            "WHERE cg.tenant_id=? ORDER BY cg.created_at DESC",
            (tenant,),
        )
        by_status = _rows(
            conn,
            "SELECT status, COUNT(*) AS n FROM common_ground WHERE tenant_id=? "
            "GROUP BY status",
            (tenant,),
        )
        return {"rows": rows, "by_status": {r["status"]: r["n"] for r in by_status}}


def search_statements(db_path: str, tenant: str, q: str, *, limit: int = 20, frozen: bool = False) -> dict:
    """透视镜取镜:按文本找语句(只读 LIKE over subject/predicate/object_value,
    tenant-scoped,有界 LIMIT)。副作用自由(open_ro)——故意不用语义召回,绕开
    recalled 旁路争议(片 3 验收:不 emit statement.recalled)。空 query → 空结果。"""
    text = (q or "").strip()
    if not text:
        return {"rows": [], "query": ""}
    like = f"%{text}%"
    with open_ro(db_path, frozen) as conn:
        rows = _rows(
            conn,
            "SELECT id, holder_id, subject_id, predicate, object_value, "
            "consolidation_state, review_status, observed_at FROM statements "
            "WHERE tenant_id=? AND (subject_id LIKE ? OR predicate LIKE ? "
            "OR object_value LIKE ?) ORDER BY observed_at DESC LIMIT ?",
            (tenant, like, like, like, limit),
        )
        return {"rows": rows, "query": text}


def lifecycle(db_path: str, tenant: str, *, frozen: bool = False) -> dict:
    """Phase 3 片 4 — 生命周期:记忆从 VOLATILE 经固化到 CONSOLIDATED,再 ARCHIVED/FORGOTTEN。
    两层只读派生(决策 F4=A,零 migration,不新增状态转移审计表):
      ① 当前占用 occupancy = statements 按 consolidation_state 分组(**精确快照**)。
      ② 流转 events = bus_events 按 typed 'statement.*' 事件计数(**事件派生·累计**)。
    遗忘无 typed 事件(forget 直接 UPDATE→forgotten),故 FORGOTTEN 量由占用快照给出
    (forgotten 是终态,快照即累计)。tenant-scoped。run_sleep/idle 未驱动 → consolidated
    事件偏少,前端据 events 诚实标注(非静默空白)。缺表逐项降级。"""
    with open_ro(db_path, frozen) as conn:
        occ_rows, _ = _rows_or_empty(
            conn,
            "SELECT consolidation_state AS state, COUNT(*) AS n FROM statements "
            "WHERE tenant_id=? GROUP BY consolidation_state",
            (tenant,),
        )
        ev_rows, _ = _rows_or_empty(
            conn,
            "SELECT event_type, COUNT(*) AS n FROM bus_events "
            "WHERE tenant_id=? AND event_type LIKE 'statement.%' GROUP BY event_type",
            (tenant,),
        )
        return {
            "occupancy": {r["state"]: r["n"] for r in occ_rows},
            "events": {r["event_type"]: r["n"] for r in ev_rows},
        }


def _project_iso(last_accessed_iso: str, seconds: float):
    """投影绝对时点 = last_accessed + Δt(纯日期算术;曲线数学全在 C++)。
    seconds<0(S0<=0 / target 出界)或 None / 溢出 → None。"""
    if seconds is None or seconds < 0 or not last_accessed_iso:
        return None
    try:
        base = datetime.fromisoformat(last_accessed_iso.replace("Z", "+00:00"))
        return (base + timedelta(seconds=seconds)).astimezone(
            timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        return None


def forecast(db_path: str, tenant: str, *, now: str, limit: int = 200,
             threshold: float = 0.05, frozen: bool = False) -> dict:
    """Phase 3 片 5 — 衰减预报:把 C++ forgetting_curve 投影到候选语句,排「最快被遗忘」。
    **公式与其逆全在 C++**(compute_s_t / seconds_until_retrievability);这里只做只读
    候选装载 + 调绑定 + 排序(换绑定语言不需重写=守边界,绝不在 Python 复算公式)。
    输入**镜像 op_decay**:salience/access_count/modality/last_accessed + active_grounded
    =受 ACTIVE commitment 保护;op_decay 不读 affect → valence 传 0,保持预报与实际衰减
    一致。候选**有界 LIMIT**(按 last_accessed ASC,最久未访问优先=最可能低 S(t)),
    不全表算。只取 VOLATILE/CONSOLIDATED(archived/forgotten 已终态)。threshold=0.05
    与 op_decay 归档阈值一致。tenant-scoped。"""
    from starling import _core
    with open_ro(db_path, frozen) as conn:
        prot_rows, _ = _rows_or_empty(
            conn,
            "SELECT DISTINCT cp.protected_stmt_id AS sid FROM commitment_protection cp "
            "JOIN commitments c ON c.tenant_id=cp.tenant_id AND c.stmt_id=cp.commitment_stmt_id "
            "WHERE cp.tenant_id=? AND c.state='ACTIVE'",
            (tenant,),
        )
        protected = {r["sid"] for r in prot_rows}
        rows, _ = _rows_or_empty(
            conn,
            "SELECT id, subject_id, predicate, object_value, modality, salience, "
            "access_count, last_accessed, consolidation_state FROM statements "
            "WHERE tenant_id=? AND consolidation_state IN ('volatile','consolidated') "
            "ORDER BY last_accessed ASC LIMIT ?",
            (tenant, limit),
        )
    out = []
    for r in rows:
        grounded = r["id"] in protected
        modality = r["modality"] or ""
        la = r["last_accessed"] or ""
        sal = r["salience"] or 0.0
        acc = r["access_count"] or 0
        s_t = _core.forgetting_s_t(
            salience=sal, access_count=acc, active_grounded=grounded,
            modality=modality, affect_valence=0.0, last_accessed_iso=la, now_iso=now)
        secs = _core.forgetting_seconds_until(
            salience=sal, access_count=acc, active_grounded=grounded,
            modality=modality, affect_valence=0.0, target=threshold)
        out.append({**r, "active_grounded": grounded, "s_t": s_t,
                    "forget_at": _project_iso(la, secs)})
    out.sort(key=lambda x: x["s_t"])   # 升序:最可能被遗忘在前
    return {"rows": out, "threshold": threshold, "now": now, "candidate_limit": limit}


def vitals(db_path: str, tenant: str, *, now: str, list_limit: int = 50, frozen: bool = False) -> dict:
    """Phase 0 observability: per-subscriber outbox lag (global) + VOLATILE-stuck
    / extraction failures / overdue reconsolidation windows (tenant-scoped)."""
    with open_ro(db_path, frozen) as conn:
        head_row = conn.execute(
            "SELECT MAX(outbox_sequence) AS head FROM bus_events").fetchone()
        head = head_row["head"] if head_row and head_row["head"] is not None else 0

        lag: list[dict] = []
        for label, table, col in _PUMP_CHECKPOINTS:
            try:
                r = conn.execute(f"SELECT {col} AS cur FROM {table} LIMIT 1").fetchone()
            except sqlite3.OperationalError:
                lag.append({"pump": label, "ok": False, "cursor": None,
                            "head": head, "lag": None})
                continue
            cur = r["cur"] if r and r["cur"] is not None else 0
            lag.append({"pump": label, "ok": True, "cursor": cur,
                        "head": head, "lag": max(0, head - cur)})
        # in-process dispatcher checkpoint(s), keyed per consumer_id
        disp, _ = _rows_or_empty(
            conn, "SELECT consumer_id, last_delivered_sequence FROM consumer_checkpoint")
        for r in disp:
            cur = r["last_delivered_sequence"] or 0
            lag.append({"pump": f"dispatcher:{r['consumer_id']}", "ok": True,
                        "cursor": cur, "head": head, "lag": max(0, head - cur)})

        volatile_stuck, _ = _rows_or_empty(
            conn,
            "SELECT id, subject_id, predicate, object_value, salience, replay_count, "
            "consolidation_state, created_at, observed_at, last_replayed "
            "FROM statements WHERE tenant_id=? "
            "AND consolidation_state IN ('volatile','replaying_consolidating') "
            "ORDER BY created_at ASC LIMIT ?",
            (tenant, list_limit),
        )
        volatile_stuck_total = _count_or_zero(
            conn,
            "SELECT COUNT(*) FROM statements WHERE tenant_id=? "
            "AND consolidation_state IN ('volatile','replaying_consolidating')",
            (tenant,),
        )

        extraction_failures, _ = _rows_or_empty(
            conn,
            "SELECT a.id, a.pipeline_run_id, a.extraction_span_key, a.attempt_number, "
            "a.status, a.error, a.raw_output, a.created_at "
            "FROM extraction_attempt a JOIN governance_pipeline_run p ON p.id = a.pipeline_run_id "
            "WHERE p.tenant_id=? AND a.status='failed' "
            "ORDER BY a.created_at DESC LIMIT ?",
            (tenant, list_limit),
        )
        extraction_failures_total = _count_or_zero(
            conn,
            "SELECT COUNT(*) FROM extraction_attempt a JOIN governance_pipeline_run p "
            "ON p.id = a.pipeline_run_id WHERE p.tenant_id=? AND a.status='failed'",
            (tenant,),
        )

        # 历史成本(0027):租户级 token 用量 + 时延汇总,以及近 N 次 run 的逐次成本。
        # 成本只在适配器(核心)采集,Python 只读聚合;缺列(DB 未迁移到 0027)→
        # _rows_or_empty 吞 OperationalError,降级为全 0(诚实:无成本数据)。
        cost_total_rows, _ = _rows_or_empty(
            conn,
            "SELECT COUNT(*) AS attempts, "
            "COALESCE(SUM(a.prompt_tokens),0) AS prompt_tokens, "
            "COALESCE(SUM(a.completion_tokens),0) AS completion_tokens, "
            "COALESCE(SUM(a.total_tokens),0) AS total_tokens, "
            "COALESCE(SUM(a.latency_ms),0) AS latency_ms "
            "FROM extraction_attempt a JOIN governance_pipeline_run p ON p.id = a.pipeline_run_id "
            "WHERE p.tenant_id=?",
            (tenant,),
        )
        extraction_cost = cost_total_rows[0] if cost_total_rows else {
            "attempts": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "total_tokens": 0, "latency_ms": 0}
        extraction_cost_runs, _ = _rows_or_empty(
            conn,
            "SELECT a.pipeline_run_id AS run_id, COUNT(*) AS attempts, "
            "COALESCE(SUM(a.prompt_tokens),0) AS prompt_tokens, "
            "COALESCE(SUM(a.completion_tokens),0) AS completion_tokens, "
            "COALESCE(SUM(a.total_tokens),0) AS total_tokens, "
            "COALESCE(SUM(a.latency_ms),0) AS latency_ms, "
            "MIN(a.created_at) AS started_at "
            "FROM extraction_attempt a JOIN governance_pipeline_run p ON p.id = a.pipeline_run_id "
            "WHERE p.tenant_id=? "
            "GROUP BY a.pipeline_run_id "
            "ORDER BY started_at DESC LIMIT ?",
            (tenant, list_limit),
        )

        overdue_windows, _ = _rows_or_empty(
            conn,
            "SELECT stmt_id, opened_at, close_deadline, status FROM reconsolidation_windows "
            "WHERE tenant_id=? AND status='open' AND close_deadline < ? "
            "ORDER BY close_deadline ASC LIMIT ?",
            (tenant, now, list_limit),
        )
        overdue_windows_total = _count_or_zero(
            conn,
            "SELECT COUNT(*) FROM reconsolidation_windows WHERE tenant_id=? "
            "AND status='open' AND close_deadline < ?",
            (tenant, now),
        )

        max_lag = max((l["lag"] for l in lag if l["lag"] is not None), default=0)
        return {
            "outbox_head": head,
            "max_lag": max_lag,
            "lag": lag,
            "volatile_stuck": volatile_stuck,
            "volatile_stuck_total": volatile_stuck_total,
            "extraction_failures": extraction_failures,
            "extraction_failures_total": extraction_failures_total,
            "extraction_cost": extraction_cost,
            "extraction_cost_runs": extraction_cost_runs,
            "overdue_windows": overdue_windows,
            "overdue_windows_total": overdue_windows_total,
        }


# ── Metrics: dogfood 子项 B(时间序列)— embed 深度 + 抽取时延,只读、host-derive ──
#
# embed_depth 读 Task 1 采样器写的 host metrics.db(embed_depth_samples;独立于
# dashboard.db 的文件,采样器负责建表/建索引)。metrics.db 尚不存在(采样器还没跑
# 过一轮,例如刚起的全新部署)时诚实返回空 series,不报错——采样器与本查询解耦,
# 谁先跑不影响另一方(镜像本文件其余只读查询「缺表→降级」的一贯风格)。
#
# latency 派生 dashboard.db 的 extraction_attempt(0001 建表 + 0027 补 token/latency
# 列)。该表当前无 tenant_id 列(M0.4 ledger 表原设计如此),故不按 tenant 过滤——
# tenant 参数保留给该列补上后的未来接入点,不是死代码去不掉的残留。
#
# 百分位在 Python 算(SQLite 无内置 percentile_cont);数据量是 dashboard 规模
# (单机、单店铺量级),排序取下标的近似足够,不需要插值。
def _parse_iso(s: str) -> datetime:
    """ISO8601(Z 或 +00:00 后缀,含/不含微秒)→ aware datetime,供按值比较。
    since 过滤必须走这里而非字典序字符串比较:since 常整秒、存量 ts 常带微秒,
    同一墙钟秒内 "." (0x2E) 排在 "Z" (0x5A) 之前会把整秒 since 字典序判定为
    "大于"带微秒的同秒 ts,静默丢边界行——同类 bug 已在采样保留期截断修过
    (engine.py:363 的注释)。"""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _bucket(ts: str, bucket_s: int) -> str:
    """ts(ISO8601,Z 或 +00:00 后缀)→ 桶起点 ISO,按 bucket_s 秒对齐(epoch 整除)。"""
    dt = _parse_iso(ts)
    epoch = int(dt.timestamp())
    start = epoch - (epoch % bucket_s)
    return datetime.fromtimestamp(start, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def metrics_embed_depth(db_path: str, since_iso: str, bucket_s: int) -> dict:
    """embed 队列深度时间序列。每桶聚合桶内样本:backlog 的 max/avg,embedded 取桶内
    最后一条样本的值(累计量快照,取末值而非求和)。since 过滤按解析后的时间值比较
    (_parse_iso),不在 SQL 层做字典序 WHERE——理由见 _parse_iso docstring。metrics.db
    受采样器 retention 约束(dashboard 规模),全量读入 Python 过滤/分桶开销可忽略。"""
    mp = Path(db_path).parent / "metrics.db"
    if not mp.exists():
        return {"series": []}
    since_dt = _parse_iso(since_iso)
    with open_ro(str(mp)) as conn:
        rows = _rows(conn, "SELECT ts, backlog, embedded FROM embed_depth_samples ORDER BY ts")
    buckets: dict = {}
    for r in rows:
        if _parse_iso(r["ts"]) < since_dt:
            continue
        b = _bucket(r["ts"], bucket_s)
        agg = buckets.setdefault(b, {"backlog_max": 0, "backlog_sum": 0, "n": 0, "embedded": 0})
        agg["backlog_max"] = max(agg["backlog_max"], r["backlog"])
        agg["backlog_sum"] += r["backlog"]; agg["n"] += 1; agg["embedded"] = r["embedded"]
    series = [{"bucket_ts": b, "backlog_max": a["backlog_max"],
               "backlog_avg": round(a["backlog_sum"] / a["n"], 1), "embedded": a["embedded"]}
              for b, a in sorted(buckets.items())]
    return {"series": series}


def _pct(sorted_vals: list, q: float) -> int:
    """近似分位数:已排序值按 q 比例定位下标(数据量小,够用;非插值)。"""
    if not sorted_vals:
        return 0
    idx = min(len(sorted_vals) - 1, int(q * len(sorted_vals)))
    return int(sorted_vals[idx])


_SUMMARY_LEN_BUCKETS = ("0-50", "50-100", "100-200", "200+")


def metrics_gist_quality(db_path: str, tenant: str, since_iso: str, bucket_s: int, *, frozen: bool = False) -> dict:
    """dogfood 子项 B(Task 3):gist 质量代理——funnel(candidates→abstracted)+
    confidence/member/summary 分布。

    funnel 只有两段,**不是** candidates→abstracted/gated/failed 全派生:
    replay_scheduler.cpp 的 write_ledger 只持久化 ops_applied_json=
    {"compress":n,"gist_candidates":n}(见该文件尾部 write_ledger 调用点),
    abstracted/gated/failed 从未落盘,现有表派不出来(需 C++ 侧改 replay 才能补,
    deferred backlog——别试图从别处凑)。故:
      ① candidates = 解 replay_ledger.ops_applied_json.gist_candidates,按 started_at 分桶;
      ② abstracted = provenance='consolidation_abstract' 的 statements 计数(每条即一次
         促成的 gist),按 created_at 同桶。
    confidence / member_counts / summary_lengths 三个分布派生同一批 consolidation_abstract
    行:confidence 按 0.1 分桶;member 数 = derived_from_json 数组长度;summary 长度 =
    consolidation_summary 字符数按 0-50/50-100/100-200/200+ 分桶(固定枚举顺序输出,
    跳过未观测到的桶——字典序会把 "100-200" 排到 "200+"/"50-100" 前,不是数值序)。

    since 过滤按解析后的时间值比较(_parse_iso),不做字典序 SQL WHERE——replay_ledger.
    started_at / statements.created_at 与其他表一样,整秒 since 对带微秒的存量值在同一
    墙钟秒内字典序会误判(同类 bug 已在 metrics_embed_depth 修过,见 _parse_iso
    docstring)。replay_ledger.tenant_id 可空(迁移 0036):单租户批次记租户,跨租户批次与
    迁移前的旧行为 NULL、无法归属——读取时「本租户 或 NULL」都算,与迁移前的口径一致。
    """
    since_dt = _parse_iso(since_iso)
    with open_ro(db_path, frozen) as conn:
        led = _rows(conn, "SELECT ops_applied_json, started_at FROM replay_ledger "
                          "WHERE tenant_id=? OR tenant_id IS NULL ORDER BY started_at",
                    (tenant,))
        gists = _rows(conn, "SELECT confidence, derived_from_json, consolidation_summary, "
                            "created_at FROM statements WHERE tenant_id=? AND "
                            "provenance='consolidation_abstract' ORDER BY created_at",
                      (tenant,))

    # funnel: candidates(ledger) + abstracted(consolidation_abstract 计数) 按桶。
    funnel: dict = {}
    for r in led:
        if _parse_iso(r["started_at"]) < since_dt:
            continue
        b = _bucket(r["started_at"], bucket_s)
        try:
            cand = int(json.loads(r["ops_applied_json"] or "{}").get("gist_candidates", 0))
        except Exception:
            cand = 0
        funnel.setdefault(b, {"candidates": 0, "abstracted": 0})["candidates"] += cand
    gists = [g for g in gists if _parse_iso(g["created_at"]) >= since_dt]
    for g in gists:
        b = _bucket(g["created_at"], bucket_s)
        funnel.setdefault(b, {"candidates": 0, "abstracted": 0})["abstracted"] += 1
    funnel_series = [{"bucket_ts": b, **v} for b, v in sorted(funnel.items())]

    # confidence 分布(0.1 桶)
    conf: dict = {}
    for g in gists:
        key = round(float(g["confidence"] or 0), 1)
        conf[key] = conf.get(key, 0) + 1

    # member 数分布 + summary 长度分布
    members: dict = {}
    summ: dict = {}
    for g in gists:
        try:
            m = len(json.loads(g["derived_from_json"] or "[]"))
        except Exception:
            m = 0
        members[m] = members.get(m, 0) + 1
        slen = len(g["consolidation_summary"] or "")
        lb = ("0-50" if slen < 50 else "50-100" if slen < 100
              else "100-200" if slen < 200 else "200+")
        summ[lb] = summ.get(lb, 0) + 1

    return {"funnel": funnel_series,
            "confidence": [{"bucket": k, "n": v} for k, v in sorted(conf.items())],
            "member_counts": [{"members": k, "n": v} for k, v in sorted(members.items())],
            "summary_lengths": [{"len_bucket": k, "n": summ[k]}
                                for k in _SUMMARY_LEN_BUCKETS if k in summ]}


def metrics_latency(db_path: str, tenant: str, since_iso: str, bucket_s: int, *, frozen: bool = False) -> dict:
    """租户级抽取时延时间序列，通过所属 pipeline_run 解析 tenant。since 过滤按解析后的
    时间值比较(_parse_iso),不做字典序 SQL WHERE——created_at 由 C++ iso8601_utc()
    写入、恒整秒,本表暂不触发字典序边界 bug,但与 metrics_embed_depth 统一走同一条
    健壮路径,避免写入格式未来变化时静默重犯(见 _parse_iso docstring)。"""
    since_dt = _parse_iso(since_iso)
    with open_ro(db_path, frozen) as conn:
        rows = _rows(
            conn,
            "SELECT a.created_at, a.latency_ms, a.total_tokens "
            "FROM extraction_attempt a JOIN governance_pipeline_run p ON p.id=a.pipeline_run_id "
            "WHERE p.tenant_id=? ORDER BY a.created_at",
            (tenant,),
        )
    buckets: dict = {}
    for r in rows:
        if _parse_iso(r["created_at"]) < since_dt:
            continue
        b = _bucket(r["created_at"], bucket_s)
        agg = buckets.setdefault(b, {"lat": [], "tokens": 0})
        agg["lat"].append(r["latency_ms"]); agg["tokens"] += r["total_tokens"] or 0
    series = []
    for b, a in sorted(buckets.items()):
        lat = sorted(a["lat"])
        series.append({"bucket_ts": b, "count": len(lat), "p50_ms": _pct(lat, 0.50),
                       "p95_ms": _pct(lat, 0.95), "total_tokens": a["tokens"]})
    return {"series": series}
