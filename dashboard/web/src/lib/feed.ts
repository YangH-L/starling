// T7 — 概览「最近活动」feed 的事件结构化。
//
// 原先是 `JSON.stringify(payload).slice(0, 80)` 裸截断:用户看到的是被腰斩的 JSON
// 而非「发生了什么」。这里按事件类型渲染成一句人话 + 语义色。
//
// 事件源:`routes/commands.py` 的 `_broadcast(kind, payload)`(手动动作)与
// `app.py` 的后台 tick 循环。各类型 payload:
//
//   tick                   四个来源共用此类型,形状各异——详见 describeTick 上方注释
//   statement_added        {statement_ids: string[]}   commands.py:107,149,183
//                          (注:/review 审批也复用此类型,是既有后端选择)
//   recall                 {n: number}                 commands.py:124,132
//   commitment_fired       {fired: number}             commands.py:159
//   commitment_transition  {stmt_id, state}            commands.py:214,226
//   statement_forgotten    {forgotten: number}         commands.py:174
//   memory_cleared         {tenant, total, spool_discarded}  commands.py clear_memory
//   world_changed          {statements, last_write_at, ...}  引擎池,每世界每秒至多一条(去抖)
//   snapshot_created       {world, id}                 routes/worlds.py(W1/W2)
//   world_rolled_back      {world, snapshot_id}        routes/worlds.py(W3)
//
// 未知类型/缺字段/脏 payload 一律优雅回退(只读面板,不因后端加事件或改 payload 而崩)。
// 原则:宁可少说,不断言——无法确认时给空细节,不编造「空转」之类的结论。

import { mutatesMemory, type WsEvent } from './ws';

/**
 * 这条消息属于 world 吗?广播 /ws 把所有世界的消息发给每个客户端,页面只处理当前世界的。
 * 没带 world 的帧(旧后端)一律放行,免得升级窗口里页面不刷新。
 */
export const isForWorld = (e: { world?: string }, world: string): boolean =>
	e.world === undefined || e.world === world;

/**
 * 当前页要不要因为这条消息重取。纯读事件(recall)不重取;别的世界的消息忽略;
 * world 传 null = 不限世界(世界列表页:任何世界变了都要刷新)。
 */
export const shouldRefetch = (e: WsEvent | null | undefined, world: string | null): boolean =>
	!!e && mutatesMemory(e) && (world === null || isForWorld(e, world));

export type FeedTone = 'neutral' | 'brand' | 'success' | 'warn' | 'danger' | 'info';

export type FeedEventView = {
	/** 事件类型的中文短标签(Badge 文案)。 */
	label: string;
	/** 一句话描述发生了什么;无可述细节时为空串(调用方可省略该行)。 */
	detail: string;
	tone: FeedTone;
};

const num = (v: unknown): number | null =>
	typeof v === 'number' && Number.isFinite(v) ? v : null;

// `tick` 是四个来源共用的事件名,payload 形状各不相同(review 抓到的真问题:
// 早期版本只认后台 tick_all 的键,对另外三种一律打印「空转」——用户点了「触发回放」
// 却看到「空转」,比腰斩 JSON 更糟:好看但说谎)。四个来源:
//   1. 后台循环 / POST /tick  → memory_tick_all(bind_13):embedded/fired/broken/
//      auto_withdrawn/replay_sampled/consolidated/ttl_archived/projected/dispatched/
//      stage_timings_ms/stages_skipped
//   2. POST /replay_trigger   → run_replay:mode/sampled/compressed/abstracted/
//      reinforced/decayed/reconciled/forced_consolidated/ttl_archived/gist_*/replay_batch_id
//   3. 同上但写门关闭        → {mode, rejected:"draining"}
//   4. POST /reconsolidate    → {reconsolidate_requested: stmt_id}
// 故:已知键优先按中文序渲染,未知数值键泛化列出(后端加字段自动生效),
// 特例键(rejected/reconsolidate_requested/mode)单独成句。只有「确实出现了已知
// 计数键且全为 0」才敢说空转——无法确认时宁可少说,不断言。
const TICK_COUNT_LABELS: Record<string, string> = {
	// 后台 tick_all(与 T5 labels.ts 的既有译法保持一致)
	embedded: '已嵌入',
	consolidated: '固化',
	replay_sampled: '重放采样',
	fired: '到期触发',
	broken: '违约',
	auto_withdrawn: '自动撤回',
	ttl_archived: '到期归档',
	projected: '投影',
	dispatched: '已派发',
	// 手动 replay(run_replay)
	sampled: '采样',
	compressed: '压缩',
	abstracted: '抽象',
	reinforced: '强化',
	decayed: '衰减',
	reconciled: '调和',
	forced_consolidated: '强制固化',
	gist_candidates: 'gist 候选',
	gist_failed: 'gist 失败',
	gist_gated: 'gist 门控'
};
// 这些不是计数,单独成句(否则会被当作未知数值键或被静默吞掉)。
const TICK_NON_COUNT = new Set(['mode', 'replay_batch_id', 'stage_timings_ms', 'stages_skipped']);

function describeTick(p: Record<string, unknown>): string {
	// 写门拒绝 / 再固化请求:动作本身就是消息,优先成句。
	if (typeof p.rejected === 'string' && p.rejected) {
		const mode = typeof p.mode === 'string' ? `${p.mode} ` : '';
		return `${mode}回放被拒(${p.rejected})`;
	}
	if (typeof p.reconsolidate_requested === 'string' && p.reconsolidate_requested) {
		return `请求再固化 ${p.reconsolidate_requested.slice(0, 8)}…`;
	}

	const parts: string[] = [];
	let sawKnownCount = false;
	// 已知键按上表声明序渲染(稳定顺序,不受 payload 键序影响)。
	for (const [key, zh] of Object.entries(TICK_COUNT_LABELS)) {
		const n = num(p[key]);
		if (n === null) continue;
		sawKnownCount = true;
		if (n > 0) parts.push(`${zh} ${n}`);
	}
	// 未知数值键泛化列出:后端新增计数时自动可见,而非被静默吞成「空转」。
	for (const [key, val] of Object.entries(p)) {
		if (key in TICK_COUNT_LABELS || TICK_NON_COUNT.has(key)) continue;
		const n = num(val);
		if (n !== null && n > 0) parts.push(`${key} ${n}`);
	}
	if (parts.length) {
		const mode = typeof p.mode === 'string' && p.mode ? `${p.mode}:` : '';
		return `${mode}${parts.join(' · ')}`;
	}
	// 只有确实见到已知计数键(且全为 0)才敢断言空转;否则形状陌生,不妄下结论。
	return sawKnownCount ? '空转(无待办)' : '';
}

export function describeEvent(type: string, payload: unknown): FeedEventView {
	const p = (payload && typeof payload === 'object' ? payload : {}) as Record<string, unknown>;
	switch (type) {
		case 'tick': {
			const detail = describeTick(p);
			// 违约/自动撤回是需要注意的后台结果,给 warn;其余后台推进用中性。
			const alarming = (num(p.broken) ?? 0) > 0 || (num(p.auto_withdrawn) ?? 0) > 0;
			return { label: '后台 tick', detail, tone: alarming ? 'warn' : 'neutral' };
		}
		case 'statement_added': {
			// 空数组是「确知 0 条」(remember 未抽出任何语句时 _memory_core 返回 []
			// 且 commands.py:107 无条件广播),不是「数量未知」——旧写法把二者混为一谈,
			// 会给一次什么都没写的 remember 打上绿色 success 的「落库(数量未知)」。
			if (!Array.isArray(p.statement_ids)) {
				return { label: '新增语句', detail: '落库(数量未知)', tone: 'neutral' };
			}
			const n = p.statement_ids.length;
			return {
				label: '新增语句',
				detail: `落库 ${n} 条`,
				tone: n > 0 ? 'success' : 'neutral'
			};
		}
		case 'recall': {
			const n = num(p.n);
			return { label: '检索', detail: n === null ? '' : `召回 ${n} 条`, tone: 'info' };
		}
		case 'commitment_fired': {
			const n = num(p.fired);
			return { label: '承诺触发', detail: n === null ? '' : `${n} 条到期`, tone: 'warn' };
		}
		case 'commitment_transition': {
			const state = typeof p.state === 'string' ? p.state : '';
			const sid = typeof p.stmt_id === 'string' ? p.stmt_id : '';
			const short = sid ? `${sid.slice(0, 8)}…` : '';
			return {
				label: '承诺流转',
				detail: [short, state].filter(Boolean).join(' → '),
				// FULFILLED 是好结果,WITHDRAWN/BROKEN 值得留意。
				tone: state === 'FULFILLED' ? 'success' : state ? 'warn' : 'neutral'
			};
		}
		case 'statement_forgotten': {
			const n = num(p.forgotten);
			return { label: '遗忘', detail: n === null ? '' : `${n} 条移出检索`, tone: 'danger' };
		}
		case 'memory_cleared': {
			// 设置页「危险区」:物理清空当前租户。total 是删除的行数(跨所有表),
			// spool_discarded 是移进 discarded/ 的待摄入会话数(0 时不提)。
			const total = num(p.total);
			const discarded = num(p.spool_discarded);
			const detail = [
				total === null ? '' : `删除 ${total} 行`,
				discarded ? `丢弃 ${discarded} 个待摄入会话` : ''
			]
				.filter(Boolean)
				.join(' · ');
			return { label: '清空记忆', detail, tone: 'danger' };
		}
		case 'world_changed': {
			// 某个世界有写入(后端去抖后的汇总),detail 给出写入后的语句总数。
			const n = num(p.statements);
			return {
				label: '世界有写入',
				detail: n === null ? '' : `语句共 ${n} 条`,
				tone: 'info'
			};
		}
		case 'snapshot_created': {
			const world = typeof p.world === 'string' ? p.world : '';
			return {
				label: '世界快照',
				detail: world ? `世界 ${world} 拍了快照` : '',
				tone: 'success'
			};
		}
		case 'world_rolled_back': {
			// 世界页「回滚到此」成功:快照之后的写入被丢弃(旧库进了 archive/),用 warn 提醒这是有损操作。
			const world = typeof p.world === 'string' ? p.world : '';
			const snap = typeof p.snapshot_id === 'string' ? p.snapshot_id : '';
			const detail = !world
				? ''
				: snap
					? `世界 ${world} 已回滚到快照 ${snap}`
					: `世界 ${world} 已回滚`;
			return { label: '世界回滚', detail, tone: 'warn' };
		}
		default:
			// 后端新增事件类型时不崩:显示原类型名,细节留空。
			return { label: type || '事件', detail: '', tone: 'neutral' };
	}
}
