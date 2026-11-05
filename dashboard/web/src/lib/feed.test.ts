import { describe, expect, it } from 'vitest';
import { describeEvent, isForWorld, shouldRefetch } from './feed';

describe('describeEvent — 概览 feed 事件结构化', () => {
	it('tick: 只列非零计数,按后端字段名对齐', () => {
		const v = describeEvent('tick', {
			embedded: 3,
			consolidated: 2,
			replay_sampled: 0,
			fired: 0,
			broken: 0,
			auto_withdrawn: 0,
			ttl_archived: 0
		});
		expect(v.label).toBe('后台 tick');
		// 术语跟随 T5 labels.ts 的既有译法(embedded→已嵌入),避免同键两套说法
		expect(v.detail).toBe('已嵌入 3 · 固化 2');
		expect(v.tone).toBe('neutral');
	});

	it('tick: stages_skipped 含 embed → 说跳过嵌入,不说空转(裁定 R59)', () => {
		const skipped = describeEvent('tick', {
			embedded: 0,
			consolidated: 0,
			stages_skipped: ['embed']
		}).detail;
		expect(skipped).toContain('跳过嵌入');
		expect(skipped).not.toContain('空转');
		const busy = describeEvent('tick', { fired: 2, stages_skipped: ['embed'] }).detail;
		expect(busy).toContain('到期触发 2');
		expect(busy).toContain('跳过嵌入');
		// 别的阶段被跳过不提嵌入。
		expect(
			describeEvent('tick', { embedded: 0, stages_skipped: ['persona'] }).detail
		).toBe('空转(无待办)');
	});

	it('tick: 见到已知计数键且全零 → 空转;空 payload 形状陌生 → 不妄断', () => {
		expect(describeEvent('tick', { embedded: 0, consolidated: 0 }).detail).toBe('空转(无待办)');
		// 空对象没有任何已知计数键,无从判断是否真空闲 → 宁可空细节
		expect(describeEvent('tick', {}).detail).toBe('');
	});

	it('tick: 违约/自动撤回 → warn(值得注意的后台结果)', () => {
		expect(describeEvent('tick', { broken: 1 }).tone).toBe('warn');
		expect(describeEvent('tick', { auto_withdrawn: 2 }).tone).toBe('warn');
		expect(describeEvent('tick', { embedded: 5 }).tone).toBe('neutral');
	});

	it('statement_added: 用 statement_ids 长度', () => {
		const v = describeEvent('statement_added', { statement_ids: ['a', 'b', 'c'] });
		expect(v.label).toBe('新增语句');
		expect(v.detail).toBe('落库 3 条');
		expect(v.tone).toBe('success');
	});

	it('recall / commitment_fired / statement_forgotten: 各自的计数字段', () => {
		expect(describeEvent('recall', { n: 7 }).detail).toBe('召回 7 条');
		expect(describeEvent('commitment_fired', { fired: 2 }).detail).toBe('2 条到期');
		expect(describeEvent('statement_forgotten', { forgotten: 4 }).detail).toBe('4 条移出检索');
		expect(describeEvent('statement_forgotten', { forgotten: 4 }).tone).toBe('danger');
	});

	it('memory_cleared: 删除行数 + 丢弃的待摄入会话数(0 时不提),danger', () => {
		const v = describeEvent('memory_cleared', { tenant: 'default', total: 12, spool_discarded: 2 });
		expect(v.label).toBe('清空记忆');
		expect(v.detail).toBe('删除 12 行 · 丢弃 2 个待摄入会话');
		expect(v.tone).toBe('danger');
		expect(describeEvent('memory_cleared', { total: 5, spool_discarded: 0 }).detail).toBe('删除 5 行');
	});

	it('world_switched 已去掉:当作未知类型,不崩', () => {
		expect(describeEvent('world_switched', { active: 'sim-1' }).detail).toBe('');
	});
	it('world_changed: 语句数与最近写入', () => {
		const v = describeEvent('world_changed', { statements: 12, last_write_at: 'T' });
		expect(v.label).toBe('世界有写入');
		expect(v.detail).toBe('语句共 12 条');
		expect(describeEvent('world_changed', {}).detail).toBe('');
	});

	it('snapshot_created: 世界 X 拍了快照(success);缺 world 时优雅回退为空细节', () => {
		const v = describeEvent('snapshot_created', { world: 'sim-1', id: '20261025T101500Z' });
		expect(v.label).toBe('世界快照');
		expect(v.detail).toBe('世界 sim-1 拍了快照');
		expect(v.tone).toBe('success');
		expect(describeEvent('snapshot_created', { id: 'x' }).detail).toBe('');
		expect(describeEvent('snapshot_created', undefined).detail).toBe('');
		expect(describeEvent('snapshot_created', { world: 3 }).detail).toBe('');
	});

	it('world_rolled_back: 世界 X 已回滚到快照 Y(warn);缺字段时优雅回退', () => {
		const v = describeEvent('world_rolled_back', {
			world: 'sim-1',
			snapshot_id: '20261026T120000Z-ab12'
		});
		expect(v.label).toBe('世界回滚');
		expect(v.detail).toBe('世界 sim-1 已回滚到快照 20261026T120000Z-ab12');
		expect(v.tone).toBe('warn');
		// 有世界、缺快照 id:只说回滚了,不编造快照
		expect(describeEvent('world_rolled_back', { world: 'sim-1' }).detail).toBe('世界 sim-1 已回滚');
		expect(describeEvent('world_rolled_back', { world: 'sim-1', snapshot_id: 9 }).detail).toBe(
			'世界 sim-1 已回滚'
		);
		// 缺世界名:空细节
		expect(describeEvent('world_rolled_back', { snapshot_id: 's1' }).detail).toBe('');
		expect(describeEvent('world_rolled_back', null).detail).toBe('');
		expect(describeEvent('world_rolled_back', { world: '' }).detail).toBe('');
	});

	it('commitment_transition: 短 id + 目标态,FULFILLED 为 success', () => {
		const v = describeEvent('commitment_transition', {
			stmt_id: '0123456789abcdef',
			state: 'FULFILLED'
		});
		expect(v.detail).toBe('01234567… → FULFILLED');
		expect(v.tone).toBe('success');
		expect(describeEvent('commitment_transition', { stmt_id: 'x', state: 'WITHDRAWN' }).tone).toBe(
			'warn'
		);
	});

	// ── T7 review 抓到的真误报:tick 是四个来源共用的事件名 ──────────────────
	it('tick(手动 replay): 认得 run_replay 的键,不再谎报空转', () => {
		const v = describeEvent('tick', {
			mode: 'sleep',
			sampled: 30,
			compressed: 5,
			reinforced: 12,
			abstracted: 0,
			replay_batch_id: 'b1'
		});
		expect(v.detail).toContain('采样 30');
		expect(v.detail).toContain('压缩 5');
		expect(v.detail).toContain('强化 12');
		expect(v.detail).toContain('sleep:'); // mode 前缀
		expect(v.detail).not.toContain('空转');
		expect(v.detail).not.toContain('b1'); // 非计数键不混进摘要
	});

	it('tick(写门拒绝): 拒绝不被静默吞掉', () => {
		const v = describeEvent('tick', { mode: 'idle', rejected: 'draining' });
		expect(v.detail).toBe('idle 回放被拒(draining)');
	});

	it('tick(再固化请求): 用户动作要可见', () => {
		const v = describeEvent('tick', { reconsolidate_requested: '0123456789abcdef' });
		expect(v.detail).toBe('请求再固化 01234567…');
	});

	it('tick: 未知数值键泛化列出(后端加字段自动可见,不被吞成空转)', () => {
		expect(describeEvent('tick', { brand_new_counter: 7 }).detail).toBe('brand_new_counter 7');
	});

	it('tick: 只有确实见到已知计数键且全为 0 才敢说空转;形状陌生则不妄断', () => {
		expect(describeEvent('tick', { embedded: 0, consolidated: 0 }).detail).toBe('空转(无待办)');
		// 陌生形状:没有任何已知计数键 → 宁可空细节,不谎报空转
		expect(describeEvent('tick', { some_string_field: 'x' }).detail).toBe('');
	});

	it('statement_added: 空数组是确知 0 条,不是「数量未知」(且不给绿色 success)', () => {
		const zero = describeEvent('statement_added', { statement_ids: [] });
		expect(zero.detail).toBe('落库 0 条');
		expect(zero.tone).toBe('neutral');
		// 只有非数组才叫未知
		const unknown = describeEvent('statement_added', {});
		expect(unknown.detail).toBe('落库(数量未知)');
		expect(unknown.tone).toBe('neutral');
	});

	it('未知类型 / 脏 payload 一律优雅回退,不崩', () => {
		expect(describeEvent('brand_new_event', { whatever: 1 })).toEqual({
			label: 'brand_new_event',
			detail: '',
			tone: 'neutral'
		});
		// payload 非对象 / null / 缺字段
		expect(describeEvent('recall', null).detail).toBe('');
		expect(describeEvent('recall', 'garbage').detail).toBe('');
		expect(describeEvent('statement_added', {}).detail).toBe('落库(数量未知)');
		// 非有限数不当作计数
		expect(describeEvent('recall', { n: NaN }).detail).toBe('');
		// 非数值的 embedded 不算「见到已知计数键」→ 形状陌生,不妄断空转
		expect(describeEvent('tick', { embedded: 'x' }).detail).toBe('');
	});
});

describe('按世界过滤 ws 消息', () => {
	it('isForWorld：world 相同才算当前世界;缺 world 的旧帧一律放行', () => {
		expect(isForWorld({ world: 'a' }, 'a')).toBe(true);
		expect(isForWorld({ world: 'b' }, 'a')).toBe(false);
		expect(isForWorld({}, 'a')).toBe(true);
	});
	it('world_changed 只对当前世界触发 refetch;别的世界的消息忽略', () => {
		expect(shouldRefetch({ type: 'world_changed', world: 'a', payload: {} }, 'a')).toBe(true);
		expect(shouldRefetch({ type: 'world_changed', world: 'b', payload: {} }, 'a')).toBe(false);
		expect(shouldRefetch({ type: 'statement_added', world: 'b', payload: {} }, 'a')).toBe(false);
		expect(shouldRefetch({ type: 'statement_added', world: 'a', payload: {} }, 'a')).toBe(true);
	});
	it('recall 是纯读:当前世界也不触发 refetch;空事件不触发', () => {
		expect(shouldRefetch({ type: 'recall', world: 'a', payload: {} }, 'a')).toBe(false);
		expect(shouldRefetch(null, 'a')).toBe(false);
	});
	it('current 传 null = 不限世界(世界列表页:任一世界变了都要刷新)', () => {
		expect(shouldRefetch({ type: 'world_changed', world: 'b', payload: {} }, null)).toBe(true);
		expect(shouldRefetch({ type: 'recall', world: 'b', payload: {} }, null)).toBe(false);
	});
});
