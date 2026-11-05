import { getToken } from './token';
import { explicitWorld, defaultWorld } from './world.svelte';

export class ApiError extends Error {
	constructor(
		public status: number,
		public path: string,
		message: string
	) {
		super(message);
		this.name = 'ApiError';
	}
	/** 401/403 → 多半是 token 缺失/失效。 */
	get isAuth(): boolean {
		return this.status === 401 || this.status === 403;
	}
}

const TIMEOUT_MS = 15000;

// 默认 15s 适用于本地读查询;走 LLM/embedder 网络的端点(remember/recall/
// working_set/config test/save/tick)真模型下要 20-45s 甚至更久,调用点用
// timeoutMs 按请求放宽——否则前端先报超时、后端继续完成,UI 与事实错位。
type ReqInit = RequestInit & { timeoutMs?: number };

async function req<T>(path: string, init: ReqInit = {}): Promise<T> {
	const { timeoutMs = TIMEOUT_MS, ...rest } = init;
	const headers = new Headers(rest.headers);
	const tok = getToken();
	if (tok) headers.set('Authorization', `Bearer ${tok}`);
	headers.set('Content-Type', 'application/json');
	const ctrl = new AbortController();
	const timer = setTimeout(() => ctrl.abort(), timeoutMs);
	// caller 传入的 signal 链到内部 controller:保证超时始终生效,且 caller 取消也能传播。
	if (rest.signal) {
		if (rest.signal.aborted) ctrl.abort();
		else rest.signal.addEventListener('abort', () => ctrl.abort(), { once: true });
	}
	try {
		const res = await fetch(path, { ...rest, headers, signal: ctrl.signal });
		if (!res.ok) {
			let detail = res.statusText;
			try {
				const body = await res.json();
				if (body?.detail) detail = String(body.detail);
			} catch {
				/* 非 JSON body,保留 statusText */
			}
			throw new ApiError(res.status, path, `${res.status} ${detail}`);
		}
		return (await res.json()) as T;
	} catch (e) {
		if (e instanceof ApiError) throw e;
		if (e instanceof DOMException && e.name === 'AbortError')
			throw new ApiError(0, path, `请求超时（>${timeoutMs / 1000}s）`);
		throw new ApiError(0, path, String(e));
	} finally {
		clearTimeout(timer);
	}
}

// ---- 按世界寻址 ----
// 检视/命令路由在后端挂了两次:/api/x(默认世界)与 /api/w/<世界>/x(指定世界)。
// 管理路由(世界注册表、配置、评测、健康检查)与具体世界无关,永远不加前缀。
const MANAGEMENT_RE = /^\/(?:api\/(?:worlds|config|eval|ping)|health)(?=$|[/?#])/;

/**
 * `/api/x` → `/api/w/<世界>/x`;世界是默认世界时保持 `/api/x`(旧路径)。
 * world 缺省取当前页选中的世界(URL ?world=,没写则是默认世界)。管理路由与非 /api 路径原样返回。
 */
export function worldPath(path: string, world?: string): string {
	if (!path.startsWith('/api/') || MANAGEMENT_RE.test(path)) return path;
	// URL 没写 ?world= 就是默认世界,走旧路径:这样默认世界名还没从 /api/worlds 取回来时
	// (壳层启动的头几百毫秒)页面的首次取数也不会错发到 /api/w/<猜的名字>/ 上。
	const w = world ?? explicitWorld();
	if (w === null || w === defaultWorld()) return path;
	return `/api/w/${encodeURIComponent(w)}/${path.slice('/api/'.length)}`;
}

export const api = {
	get: <T>(p: string, init?: ReqInit) => req<T>(p, init),
	post: <T>(p: string, body: unknown, init?: ReqInit) =>
		req<T>(p, { ...init, method: 'POST', body: JSON.stringify(body) }),
	put: <T>(p: string, body: unknown, init?: ReqInit) =>
		req<T>(p, { ...init, method: 'PUT', body: JSON.stringify(body) }),
	del: <T>(p: string, init?: ReqInit) => req<T>(p, { ...init, method: 'DELETE' })
};

// 归因 receipt(Phase 0 起 plan_query 纯增量带出;runtime_health 不暴露)。
export type RecallReceipt = {
	trace_id: string;
	query_id: string;
	sufficiency_status: string;
	filters_applied: { name: string; value: string }[];
	candidate_counts: {
		fetched: number;
		returned: number;
		dropped_by_review: number;
		dropped_by_state: number;
		dropped_by_time_anchor: number;
		dropped_by_evidence_erasure: number;
	};
	frontier_masked_count: number;
	evidence_erased_count: number;
	projection_lag_events: number;
	degraded_paths: { path: string; reason: string; fallback: string }[];
	// 顺序契约:score_breakdown[i] 对应 results[i]——两者由后端 retrieval_planner
	// 同序构建(rerank 成对推入 + resize 同步截断到同一 k)。results 不带 statement_id,
	// 前端只能靠位置索引配对。若后端将来改动排序/截断而破坏同序,前端会静默错位显示
	// 错误评分。改动 plan_query 返回形状时务必保持这个不变式(或补 statement_id 显式配对)。
	score_breakdown: {
		statement_id: string;
		base: number;
		recency: number;
		salience: number;
		activation: number;
		affect_consistency: number;
		temporal_penalty: number;
		final_score: number;
	}[];
	skipped_scopes: { scope: string; reason: string }[];
	stop_reason: string;
};

// P3.a1 检索规划响应(POST /api/recall 带 intent 时)。
export type PlannedRecallResponse = {
	results: { subject: string; predicate: string; object: string; score: number; label: string }[];
	context_pack: string;
	abstained: boolean;
	abstention_reason: string;
	plan_steps: { step: string; detail: string }[];
	scopes_searched: string[];
	receipt?: RecallReceipt; // 纯增量:旧 6 字段不变,归因细节新增于此
};

// Phase 2c 带记忆聊天一轮的响应(POST /api/converse)。
export type ConverseResponse = {
	reply: string;
	ok: boolean; // generate 成功 → 有回复
	error: string;
	context_pack: string; // 本轮注入的记忆(带标签)
	abstained: boolean;
	statement_ids: string[]; // 本轮沉淀的语句
	remember_ok: boolean; // false → 回复在但记忆未落库
	remember_error: string;
	// 2b 成本采集:本轮回复生成的 token/延迟(真模型填充,Fake/stub 为 0)。
	gen_prompt_tokens: number;
	gen_completion_tokens: number;
	gen_total_tokens: number;
	gen_latency_ms: number;
};

// Phase 3 片 3 — 透视镜(Lens)来源取证(GET /api/provenance/{id})。
export type ExtractionAttemptRow = {
	attempt_number: number | null;
	status: string | null;
	raw_output: string | null; // 原始 LLM 输出(仅失败/解析失败留底)
	error: string | null;
	created_at: string | null;
	// 成本(0027):每次 LLM 往返的 token 用量 + 时延;fake/未采集端点为 0。
	prompt_tokens: number | null;
	completion_tokens: number | null;
	total_tokens: number | null;
	latency_ms: number | null;
};

export type ExtractionAttempt = {
	span_key: string;
	status: string | null; // 权威行:success|partial_success|failed|noop(DB 无 CHECK)
	attempt_number: number | null;
	raw_output: string | null; // 权威行的 raw(成功路径通常 null)
	error: string | null;
	created_at: string | null;
	run_id: string | null;
	// 权威行的成本(0027);无 ledger 行时为 null。
	prompt_tokens: number | null;
	completion_tokens: number | null;
	total_tokens: number | null;
	latency_ms: number | null;
	failed_attempts: ExtractionAttemptRow[]; // 同 run 失败/部分尝试(带原始 LLM 输出 + 各自成本)
};

export type EngramEvidence = {
	engram_ref: string | null;
	content_hash: string | null;
	status: string | null;
	engram: {
		source_kind: string;
		privacy_class: string;
		created_at: string;
		erased: boolean;
		payload_preview: string | null; // 仅 inline 且未抹除取前 280 字符
	} | null;
};

// 递归节点:完整节点带 statement/origin/evidence/子树;折叠节点(repeat / found=false)只带 summary/id。
export type ProvenanceNode = {
	id: string;
	found: boolean; // false → 孤儿父 / 跨租户引用(只有 id)
	repeat?: boolean; // 已在上文展开(共享父 / 环)→ 不再递归
	summary?: { subject_id: string; predicate: string; object_value: string };
	statement?: Record<string, unknown>;
	origin?: { provenance: string; extraction: ExtractionAttempt | null };
	evidence?: EngramEvidence[];
	evidence_parse_error?: boolean;
	derived_from_parse_error?: boolean;
	derived_from?: ProvenanceNode[];
	supersedes?: ProvenanceNode | null;
	truncated?: boolean; // 到深度上限,更深来源未展开
};

// 片 6 — 级联预览(GET /api/cascade_preview/{id}):遗忘前看会波及哪些「派生自它」的
// 后代(只读 inform-only;遗忘不级联删除,只是让用户看清波及面再决定)。
export type CascadeAffected = {
	id: string;
	subject_id: string;
	predicate: string;
	object_value: string;
	consolidation_state: string;
	review_status: string;
	provenance: string;
	depth: number; // 1=直接派生,2+=传递后代
};
export type CascadePreview = {
	stmt_id: string;
	affected: CascadeAffected[];
	affected_count: number;
	truncated: boolean; // 到深度上限,还有更深后代未展开
};

// #38-C v2 可观测:固化 NORM gist(provenance=consolidation_abstract)只读检视。
export type Gist = {
	id: string;
	holder_id: string;
	subject_id: string;
	predicate: string;
	object_value: string;
	confidence: number;
	consolidation_summary: string | null; // LLM 一句话渲染;NULL=无 LLM(确定性)或未设
	consolidation_state: string; // consolidated=已验证晋升;volatile=未门控/惰性;archived/forgotten
	review_status: string;
	derived_from: string[]; // 来源簇成员 id(谱系)
	derived_depth: number;
	created_at: string;
	updated_at: string;
};
export type GistData = { by_state: Record<string, number>; gists: Gist[] };

// 谱系钻取:一条 gist 的来源簇成员(它泛化自的具体语句)。
export type GistMember = {
	id: string;
	holder_id: string;
	subject_id: string;
	predicate: string;
	object_value: string;
	consolidation_state: string;
	review_status: string;
	confidence: number;
};
export type GistMembers = { gist_id: string; members: GistMember[] };

// 透视镜取镜:只读文本查找(GET /api/statement_search)。
export type StatementSearchResponse = {
	rows: {
		id: string;
		holder_id: string;
		subject_id: string;
		predicate: string;
		object_value: string;
		consolidation_state: string;
		review_status: string;
		observed_at: string;
	}[];
	query: string;
};

// Phase 3 片 4 — 生命周期(GET /api/lifecycle):占用快照 + 事件派生流转(累计)。
export type LifecycleResponse = {
	occupancy: Record<string, number>; // consolidation_state → 当前条数(精确)
	events: Record<string, number>; // statement.* 事件 → 累计计数(事件派生)
};

// T0a — 原始数据·证据(engram)浏览/搜索(GET /api/engrams、GET /api/engram/{id})。
// engram 是不可变的 verbatim 原始证据,记忆流最上游源头,先于海马/新皮层。
export type EngramRow = {
	id: string;
	source_kind: string;
	privacy_class: string;
	retention_mode: string;
	refcount: number;
	created_at: string;
	erased_at: string | null;
	source_item_id: string;
	chunk_index: number;
	adapter_name: string;
};
export type EngramListResp = { rows: EngramRow[]; total: number };

export type EngramReferencingStatement = { id: string; subject_id: string; predicate: string };
export type EngramDetail = {
	engram: {
		id: string;
		tenant_id: string;
		content_hash: string;
		source_kind: string;
		ingest_policy: string;
		ingest_mode: string;
		privacy_class: string;
		retention_mode: string;
		refcount: number;
		payload_uri: string | null;
		created_at: string;
		erased_at: string | null;
		adapter_name: string;
		adapter_version: string;
		source_item_id: string;
		source_version: string;
		chunk_index: number;
		declared_transformations_json: string;
		byte_preserving: number;
		// redacted_content / key_ref / audit_trail_json 已从端点移除:它们绕过隐私门
		// (见 queries.py 的 _ENGRAM_DETAIL_COLS 注释)。契约里也不留声明——留着就是
		// 邀请下一个人把它们接进抽屉。
	};
	preview: string | null; // 仅 inline + 未抹除 + 非受限 privacy 才显,前 280 字符
	preview_suppressed_reason: string | null; // 有 → 显示抑制理由而非空白
	referencing_statements: EngramReferencingStatement[]; // 引用它的 statement(best-effort,limit 50)
};

// Phase 3 片 5 — 衰减预报(GET /api/forecast):C++ forgetting_curve 只读投影,排「最快被遗忘」。
export type ForecastRow = {
	id: string;
	subject_id: string;
	predicate: string;
	object_value: string;
	modality: string;
	salience: number;
	access_count: number;
	last_accessed: string;
	consolidation_state: string;
	active_grounded: boolean; // 受 ACTIVE commitment 保护 → 不会被 decay 归档
	s_t: number; // 当前 retrievability ∈ (0,1],升序排列(最低在前)
	forget_at: string | null; // 预计 S(t) 跌至 threshold 的时点;null=无投影/溢出
};
export type ForecastResponse = {
	rows: ForecastRow[];
	threshold: number; // 归档阈值(0.05,同 op_decay)
	now: string;
	candidate_limit: number; // 候选有界上限
};

// T0d-2 — 新皮层·画像(Persona)只读检视(GET /api/personae)。数据源 containers
// WHERE kind='persona'。scope_descriptor 是 canonical JSON 串(原样带出,前端只读)。
export type PersonaRow = {
	id: string;
	holder_id: string;
	scope_descriptor: string; // canonical JSON 串
	created_at: string;
	updated_at: string;
	version: number;
};
export type PersonaeResponse = { rows: PersonaRow[] };

// T0d-2 — 新皮层·共识(CommonGround)只读检视(GET /api/common_ground)。数据源
// common_ground 表,五态 status;LEFT JOIN statements 带出被共识语句文本(遗忘/缺失→NULL)。
export type CommonGroundRow = {
	id: string;
	statement_id: string;
	status: string; // asserted_unack|grounded|suspected_diverge|expired|recanted
	parties_json: string; // 参与方 JSON 串
	grounded_at: string | null;
	last_confirmed_at: string | null;
	subject_id: string | null; // LEFT JOIN 命中才有;语句缺失→null
	predicate: string | null;
	object_value: string | null;
};
export type CommonGroundResponse = {
	rows: CommonGroundRow[];
	by_status: Record<string, number>; // 五态计数(概览)
};
