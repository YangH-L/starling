// W1–W4 — 世界管理的前端数据层(HTTP 契约:routes/worlds.py;W4 契约见
// docs/superpowers/plans/2026-10-28-world-w4-sessions.md「HTTP 契约」)。
//
// 一个世界 = 一个库文件 + 一个租户。本模块只放类型、与后端同一规则的名称校验、
// 字节格式化、REST 客户端和错误码 → 中文提示;页面(/worlds)只读这里。
//
// 路径段一律 encodeURIComponent:世界名受 WORLD_NAME_RE 约束本不含特殊字符,
// 但快照 id 等路径段不受此约束,统一编码比逐个判断更不易漏。

import { api, ApiError } from './api';

/**
 * W4 每个世界各自的自动快照配置。两种触发互不排斥:
 * on_session —— 某个 session_id 在该世界第一次写入之前先拍一份;
 * interval_hours —— 距上一份自动快照超过这么多小时、且期间有新写入时拍一份,0 = 关闭。
 */
export type AutoSnapshotConfig = {
	on_session: boolean;
	interval_hours: number;
};

/** W4 自动快照失败的累计情况(GET /api/worlds 顶层)。失败不影响摄入。 */
export type AutoSnapshotStatus = {
	failures: number;
	last_error: string | null;
	last_error_at: string | null;
};

export type WorldRow = {
	name: string;
	label: string;
	tenant: string;
	created_at: string;
	/** 建世界时给的标签(key → value),建好后不可改(labels_immutable)。 */
	labels: Record<string, string>;
	/** 引擎现在开着。 */
	open: boolean;
	holders: number;
	/** 该世界最近一次写入的时间;从没写过为 null/空。 */
	last_write_at: string | null;
	/** 列表行不带 db_bytes / live_bytes / engrams(要打开库才有);详情(worldsApi.detail)才有。 */
	db_bytes?: number;
	live_bytes?: number;
	engrams?: number;
	statements: number;
	snapshots: number;
	snapshot_bytes: number;
	/** W3:回滚时被替换下来的库,保留在 <世界 home>/archive/ 下的份数与总字节。 */
	archives: number;
	archive_bytes: number;
	/** 该世界的库读不出(损坏、被占):统计全为 0。只在出错的行上出现。 */
	stats_error?: boolean;
	/** 详情才有:实际生效的设置。endpoint 段只带 key_set,不带密钥。 */
	settings?: WorldSettings;
	/** W4:该世界的自动快照配置。 */
	auto_snapshot: AutoSnapshotConfig;
};

/** 世界设置(GET /api/worlds/{name} 的 settings)。形状由后端定义,前端只读 background.auto_snapshot。 */
export type WorldSettings = {
	background?: { auto_snapshot?: AutoSnapshotConfig; [k: string]: unknown };
	[k: string]: unknown;
};

/** W4 快照的触发原因:manual 对应 kind=manual,session / interval 对应 kind=auto。 */
export type SnapshotReason = 'manual' | 'session' | 'interval';

export type SnapshotRow = {
	id: string;
	kind: 'auto' | 'manual';
	/** 契约取值见 SnapshotReason;类型放宽到 string,未知值原样显示而不是崩。 */
	reason: SnapshotReason | (string & {});
	note: string;
	created_at: string;
	world_time: string;
	size_bytes: number;
	/** 注册项还在、文件已不在:size_bytes 为 0,仍可删除。只在缺失的行上出现。 */
	missing?: boolean;
	/** W4:快照时该世界已有的会话数;W4 之前的条目为 null。 */
	sessions_complete: number | null;
	/** W4:触发这份快照的会话;只有 reason="session" 时非空。 */
	session_id: string | null;
};

/** W4 会话清单的一行(按 session_id 汇总 source_unit_meta)。 */
export type SessionRow = {
	session_id: string;
	/** 该会话的话轮(unit)数。 */
	units: number;
	speakers: number;
	/** observed_at 的最早 / 最晚值;全为 NULL 时为 null。 */
	first_at: string | null;
	last_at: string | null;
	/** 该会话开始前拍的那份快照(reason="session",同一会话多份时取最新);没有时为 null。 */
	anchor: { snapshot_id: string; created_at: string } | null;
};

/** GET /api/worlds/{w}/sessions 的响应。 */
export type SessionList = {
	world: string;
	/** 当前 config 的左脑 lane 开关:为 false 时新的写入不会记录会话(每个世界各取自己生效的设置)。 */
	source_lane_enabled: boolean;
	total: number;
	/** observed_at IS NULL 的 unit 数。 */
	null_observed: number;
	/** total > limit:只返回了按开始时间最近的 limit 个。 */
	truncated: boolean;
	sessions: SessionRow[];
};

/** W3 回滚语义:目前只有语义 A「丢掉快照之后的写入」;"scene"(语义 B)属于 W5。 */
export type RollbackMode = 'discard_after';

export type RollbackResult = {
	world: string;
	snapshot_id: string;
	/** 回滚前库存在、已改名进 archive/;库原本不存在时为 false。 */
	archived: boolean;
	/** 回滚前引擎开着、回滚后已重新打开。引擎在服务端重建,页面不用整页刷新。 */
	reopened: boolean;
};

export type WorldsData = {
	default: string;
	/** 符合标签过滤的世界总数(分页用)。 */
	total: number;
	snapshot_retention: number;
	/** W4:自动快照失败计数与最近一次错误。 */
	auto_snapshot_status: AutoSnapshotStatus;
	worlds: WorldRow[];
};

/** 与后端同一规则:首字符字母或数字,其后 [A-Za-z0-9._-],长度 1–128,区分大小写。 */
export const WORLD_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;

export function isValidWorldName(name: string): boolean {
	return WORLD_NAME_RE.test(name);
}

/**
 * 把 "env=prod, team=x" 解析成标签对象(逗号或空白分隔,key=value);空串给 {};
 * 任何一段不是 key=value(key 或 value 为空)返回 null。用于新建世界的标签与列表的标签过滤。
 */
export function parseLabels(text: string): Record<string, string> | null {
	const out: Record<string, string> = {};
	for (const part of text.split(/[,\s]+/)) {
		if (!part) continue;
		const i = part.indexOf('=');
		if (i <= 0 || i === part.length - 1) return null;
		out[part.slice(0, i)] = part.slice(i + 1);
	}
	return out;
}

const UNITS = ['B', 'KB', 'MB', 'GB', 'TB'] as const;

/** 1024 进制;B 不带小数,其余保留一位(0→"0 B",1536→"1.5 KB",57 MiB→"57.0 MB")。 */
export function formatBytes(n: number): string {
	if (!Number.isFinite(n) || n <= 0) return '0 B';
	let v = n;
	let i = 0;
	while (v >= 1024 && i < UNITS.length - 1) {
		v /= 1024;
		i++;
	}
	return i === 0 ? `${Math.round(v)} B` : `${v.toFixed(1)} ${UNITS[i]}`;
}

/** 契约:interval_hours 取 0–8760,0 表示关闭。 */
export const MAX_INTERVAL_HOURS = 8760;

/**
 * 把表单里的间隔(type=number 的 input 绑定出来可能是 number、空串或 null)解析成
 * 0–8760 的整数;不合法返回 null。前端只做提示与禁用,范围的最终校验在 C++
 * (invalid_auto_snapshot)。
 */
export function parseIntervalHours(v: unknown): number | null {
	let n: number;
	if (typeof v === 'number') n = v;
	else if (typeof v === 'string' && v.trim() !== '') n = Number(v.trim());
	else return null;
	return Number.isInteger(n) && n >= 0 && n <= MAX_INTERVAL_HOURS ? n : null;
}

/** 世界行上的一句话描述:「按会话 · 每 24 小时」「按会话」「每 24 小时」「关闭」。 */
export function autoSnapshotLabel(cfg: AutoSnapshotConfig): string {
	const parts: string[] = [];
	if (cfg.on_session) parts.push('按会话');
	if (cfg.interval_hours > 0) parts.push(`每 ${cfg.interval_hours} 小时`);
	return parts.length ? parts.join(' · ') : '关闭';
}

const REASON_LABELS: Record<string, string> = {
	manual: '手动',
	session: '会话开始前',
	interval: '定时'
};

/** 快照 reason → 中文;未知值原样返回(同 worldErrorMessage,用 hasOwn 防原型链键)。 */
export function snapshotReasonLabel(reason: string): string {
	return Object.hasOwn(REASON_LABELS, reason) ? REASON_LABELS[reason] : reason;
}

/** session_id 等长 id 的前 n 位(界面上省地方,全文放 title)。 */
export function shortId(id: string, n: number): string {
	return id.slice(0, n);
}

export type ListOpts = { labels?: Record<string, string>; offset?: number; limit?: number };

/** GET /api/worlds 的查询串:label.<key>=<value>(可重复)、offset、limit;全空时为 ''。 */
function listQuery(o: ListOpts = {}): string {
	const p: string[] = [];
	for (const [k, v] of Object.entries(o.labels ?? {}))
		p.push(`label.${encodeURIComponent(k)}=${encodeURIComponent(v)}`);
	if (o.offset !== undefined) p.push(`offset=${o.offset}`);
	if (o.limit !== undefined) p.push(`limit=${o.limit}`);
	return p.length ? `?${p.join('&')}` : '';
}

const base = (name: string) => `/api/worlds/${encodeURIComponent(name)}`;

export const worldsApi = {
	list: (o?: ListOpts) => api.get<WorldsData>(`/api/worlds${listQuery(o)}`),
	// 详情:列表的一行加 db_bytes / live_bytes / engrams 与 settings。
	detail: (name: string) => api.get<WorldRow>(base(name)),
	// auto_snapshot 可选:不传时不发这个字段,由后端按默认世界当前设置补(spec §8.2)。
	create: (
		name: string,
		label?: string,
		autoSnapshot?: AutoSnapshotConfig,
		labels?: Record<string, string>
	) =>
		api.post<WorldRow>('/api/worlds', {
			name,
			label,
			...(labels && Object.keys(labels).length ? { labels } : {}),
			...(autoSnapshot
				? {
						auto_snapshot: {
							on_session: autoSnapshot.on_session,
							interval_hours: autoSnapshot.interval_hours
						}
					}
				: {})
		}),
	// 部分设置,没给的键保持不变;默认世界只接受 background 段。
	updateSettings: (name: string, settings: WorldSettings) =>
		api.put<WorldSettings>(`${base(name)}/settings`, settings, { timeoutMs: 120_000 }),
	// 关闭要等该世界的在途请求结束并停线程。
	close: (name: string) =>
		api.post<{ closed: string }>(`${base(name)}/close`, {}, { timeoutMs: 60_000 }),
	remove: (name: string, confirm: string) =>
		api.del<{ deleted: string }>(`${base(name)}?confirm=${encodeURIComponent(confirm)}`),
	snapshots: (name: string) => api.get<{ snapshots: SnapshotRow[] }>(`${base(name)}/snapshots`),
	// 快照要整库拷贝,大库下远超默认 15s。
	snapshot: (name: string, note?: string) =>
		api.post<SnapshotRow>(`${base(name)}/snapshots`, { note }, { timeoutMs: 120_000 }),
	removeSnapshot: (name: string, id: string) =>
		api.del<{ deleted: string }>(`${base(name)}/snapshots/${encodeURIComponent(id)}`),
	// W3 回滚:校验快照 + 复制到 partial + 开着的世界还要停引擎、重建引擎,与拍快照同量级,放宽到 120s。
	// mode 显式发送:确认框描述的正是语义 A,别让后端默认值悄悄决定用户确认过的东西。
	rollback: (name: string, snapshotId: string, confirm: string) =>
		api.post<RollbackResult>(
			`${base(name)}/rollback`,
			{ snapshot_id: snapshotId, confirm, mode: 'discard_after' satisfies RollbackMode },
			{ timeoutMs: 120_000 }
		),
	// W4:改自动快照配置。只发契约里的两个字段。写注册表要等世界管理锁,
	// 前面若有快照或回滚在跑会排队,与 purgeArchives 一样放宽到 120s。
	setAutoSnapshot: (name: string, cfg: AutoSnapshotConfig) =>
		api.put<{ world: string; auto_snapshot: AutoSnapshotConfig }>(
			`${base(name)}/auto_snapshot`,
			{ on_session: cfg.on_session, interval_hours: cfg.interval_hours },
			{ timeoutMs: 120_000 }
		),
	// W4 会话清单:契约 limit 取 1–1000,默认 200。只读,走默认超时。
	listSessions: (name: string, limit = 200) =>
		api.get<SessionList>(`${base(name)}/sessions?limit=${encodeURIComponent(String(limit))}`),
	// 清理要排队等世界管理锁:前面若有快照或回滚在跑(回滚最长约 40s),默认 15s 会先超时,
	// 后端随后照样删完,UI 却报了失败。与 rollback 一样放宽到 120s。
	purgeArchives: (name: string, confirm: string) =>
		api.del<{ deleted: number }>(
			`${base(name)}/archives?confirm=${encodeURIComponent(confirm)}`,
			{ timeoutMs: 120_000 }
		)
};

const ERROR_MESSAGES: Record<string, string> = {
	invalid_world_name: '世界名以字母或数字开头，其后可含字母、数字、点、下划线和连字符，最长 128 个字符',
	world_exists: '同名世界已存在',
	world_not_found: '世界不存在',
	worlds_unavailable: '世界注册表暂时不可用，请稍后再试',
	world_busy: '世界正在关闭或恢复，请稍后再试',
	world_not_initialized: '这个世界的库文件不见了',
	endpoint_changed: '供应商地址改了，这个世界需要重新确认端点',
	labels_immutable: '标签建好后不能改',
	invalid_settings: '设置不合法',
	world_protected: '默认世界不能删除',
	confirm_mismatch: '输入的名称与世界名不一致',
	ingest_busy: '后台任务或在途请求暂时停不下来，未做任何改动，请稍后再试',
	world_open_failed: '世界打不开，请稍后再试或查看服务日志',
	snapshot_unsupported_backend: '当前向量后端（zvec）不支持快照',
	insufficient_disk:
		'磁盘空间不足：快照需要至少 3 倍于数据量、回滚需要至少 2 倍于快照大小的可用空间',
	snapshot_failed: '文件操作失败，数据库未改动；详情见服务日志',
	world_in_use: '该世界的数据库正被其他程序打开，未做任何改动；请关闭后重试',
	rollback_reopen_failed: '已回滚到快照，但引擎重开失败；页面即将刷新，下一次写入、召回或手动 tick 时会自动重建，仍异常请查看服务日志',
	snapshot_not_found: '快照不存在',
	world_tenant_mismatch: '该世界的数据库里有其他租户的数据，拒绝打开',
	snapshot_corrupt: '快照文件损坏或缺失，未做任何改动',
	rollback_mode_unsupported: '不支持的回滚方式',
	invalid_auto_snapshot: '自动快照设置无效（间隔须为 0–8760 小时）',
	internal_error: '服务器内部错误'
};

/**
 * 后端错误码 → 中文提示(toast 用);未知码兜底为「操作失败：<detail>」。
 * 用 Object.hasOwn 而非 `ERROR_MESSAGES[detail] ?? …`:detail 来自响应体,
 * 'constructor' / 'toString' / '__proto__' 会命中原型链上继承来的函数或对象,
 * `??` 不会回退,函数就被当作提示返回了。
 */
export function worldErrorMessage(detail: string): string {
	return Object.hasOwn(ERROR_MESSAGES, detail)
		? ERROR_MESSAGES[detail]
		: `操作失败：${detail}`;
}

/**
 * 从错误里取出后端 detail。ApiError.message 形如 "<status> <detail>"(见 api.ts),
 * 取第一个空格之后的部分;非 ApiError 原样转字符串。
 * 超时/网络错误的 ApiError.message 没有「状态码 + 空格」前缀(如 "请求超时（>60s）"
 * 或 "TypeError: Failed to fetch"):前者不含空格,整句原样返回;后者会截掉
 * 第一个词。两者都不在映射表里,落到 worldErrorMessage 的兜底「操作失败：…」,不崩。
 */
export function apiDetail(err: unknown): string {
	if (err instanceof ApiError) {
		const i = err.message.indexOf(' ');
		return i === -1 ? err.message : err.message.slice(i + 1);
	}
	return String(err);
}

/** 读页面遇到这些码时显示中文提示,而不是裸的 "404 world_not_found"。 */
const WORLD_CODES = ['world_not_found', 'world_busy', 'world_not_initialized', 'worlds_unavailable'];

/**
 * 读页面的错误文案:ApiError 的 detail 是已知的世界错误码就给中文提示,否则原样用 message。
 * 链接带着 ?world= 被分享出去时,最常见的失败正是世界不存在 / 正在关闭。
 */
export function describeError(err: unknown): string {
	if (err instanceof ApiError) {
		const d = apiDetail(err);
		if (WORLD_CODES.includes(d)) return worldErrorMessage(d);
		return err.message;
	}
	return err instanceof Error ? err.message : String(err);
}
