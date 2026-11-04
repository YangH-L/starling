import { describe, it, expect, vi, beforeEach } from 'vitest';
import * as tok from './token';
import { ApiError } from './api';
import {
	worldsApi, isValidWorldName, formatBytes, worldErrorMessage, apiDetail,
	parseIntervalHours, describeError, autoSnapshotLabel, snapshotReasonLabel, shortId, parseLabels
} from './worlds';

let calls: Array<[string, RequestInit]> = [];
beforeEach(() => {
	calls = [];
	vi.spyOn(tok, 'getToken').mockReturnValue('');
	globalThis.fetch = vi.fn(async (p: string, init: RequestInit) => {
		calls.push([p, init]);
		return new Response(JSON.stringify({ ok: 1 }), { status: 200 });
	}) as unknown as typeof fetch;
});

describe('世界名校验（首字符字母或数字，其后 [A-Za-z0-9._-]，1–128，区分大小写）', () => {
	it.each(['default', 'sim-1', 'a_b', '0abc', 'Run-A.1', 'Up', 'a'.repeat(128)])('接受 %s', (n) =>
		expect(isValidWorldName(n)).toBe(true));
	it.each(['', '-x', '.x', '_x', 'a b', '../x', 'a/b', 'a'.repeat(129)])('拒绝 %s', (n) =>
		expect(isValidWorldName(n)).toBe(false));
});

describe('formatBytes', () => {
	it('格式化', () => {
		expect(formatBytes(0)).toBe('0 B');
		expect(formatBytes(1536)).toBe('1.5 KB');
		expect(formatBytes(57 * 2 ** 20)).toBe('57.0 MB');
		expect(formatBytes(2.4 * 2 ** 30)).toBe('2.4 GB');
	});
});

describe('worldsApi 路径与方法', () => {
	it('list/create/remove/snapshot', async () => {
		await worldsApi.list();
		await worldsApi.create('sim-1', '模拟');
		await worldsApi.remove('sim-1', 'sim-1');
		await worldsApi.snapshots('sim-1');
		await worldsApi.snapshot('sim-1', 'n');
		await worldsApi.removeSnapshot('sim-1', 'id 1');
		expect(calls.map(([p, i]) => `${i.method ?? 'GET'} ${p}`)).toEqual([
			'GET /api/worlds',
			'POST /api/worlds',
			'DELETE /api/worlds/sim-1?confirm=sim-1',
			'GET /api/worlds/sim-1/snapshots',
			'POST /api/worlds/sim-1/snapshots',
			'DELETE /api/worlds/sim-1/snapshots/id%201'
		]);
		expect(JSON.parse(String(calls[1][1].body))).toEqual({ name: 'sim-1', label: '模拟' });
		expect(JSON.parse(String(calls[4][1].body))).toEqual({ note: 'n' });
	});
});

describe('worldsApi 回滚与清理归档（W3）', () => {
	it('rollback：POST /rollback，body 显式带 mode=discard_after（确认框文案描述的正是语义 A）', async () => {
		await worldsApi.rollback('sim-1', '20261026T120000Z-ab12', 'sim-1');
		expect(calls.map(([p, i]) => `${i.method ?? 'GET'} ${p}`)).toEqual([
			'POST /api/worlds/sim-1/rollback'
		]);
		expect(JSON.parse(String(calls[0][1].body))).toEqual({
			snapshot_id: '20261026T120000Z-ab12',
			confirm: 'sim-1',
			mode: 'discard_after'
		});
	});

	it('purgeArchives：DELETE /archives?confirm=<编码后的确认名>', async () => {
		await worldsApi.purgeArchives('sim-1', 'sim-1');
		await worldsApi.purgeArchives('sim-1', 'a b&c');
		expect(calls.map(([p, i]) => `${i.method ?? 'GET'} ${p}`)).toEqual([
			'DELETE /api/worlds/sim-1/archives?confirm=sim-1',
			'DELETE /api/worlds/sim-1/archives?confirm=a%20b%26c'
		]);
		expect(calls[0][1].body).toBeUndefined();
	});

	it('purgeArchives 的超时放宽到 120s（要排队等快照或回滚持有的管理锁）', async () => {
		vi.useFakeTimers();
		try {
			globalThis.fetch = vi.fn(
				(_p: string, init: RequestInit) =>
					new Promise((_resolve, reject) => {
						init.signal?.addEventListener('abort', () =>
							reject(new DOMException('aborted', 'AbortError'))
						);
					})
			) as unknown as typeof fetch;
			const caught = worldsApi.purgeArchives('sim-1', 'sim-1').catch((e) => e as ApiError);
			let settled = false;
			void caught.then(() => (settled = true));
			await vi.advanceTimersByTimeAsync(119_000); // 越过默认 15s:仍在等
			expect(settled).toBe(false);
			await vi.advanceTimersByTimeAsync(2_000);
			const err = (await caught) as ApiError;
			expect(err).toBeInstanceOf(ApiError);
			expect(err.message).toContain('120');
		} finally {
			vi.useRealTimers();
		}
	});

	it('rollback 的超时放宽到 120s（默认 15s 不够复制快照 + 重建引擎）', async () => {
		vi.useFakeTimers();
		try {
			globalThis.fetch = vi.fn(
				(_p: string, init: RequestInit) =>
					new Promise((_resolve, reject) => {
						init.signal?.addEventListener('abort', () =>
							reject(new DOMException('aborted', 'AbortError'))
						);
					})
			) as unknown as typeof fetch;
			const caught = worldsApi.rollback('sim-1', 's1', 'sim-1').catch((e) => e as ApiError);
			let settled = false;
			void caught.then(() => (settled = true));
			await vi.advanceTimersByTimeAsync(119_000); // 越过默认 15s 与切换的 60s:仍在等
			expect(settled).toBe(false);
			await vi.advanceTimersByTimeAsync(2_000); // 越过 120s → 中断
			const err = (await caught) as ApiError;
			expect(err).toBeInstanceOf(ApiError);
			expect(err.message).toContain('120');
		} finally {
			vi.useRealTimers();
		}
	});
});

describe('错误文案', () => {
	it('回滚新增的两个错误码有中文文案', () => {
		expect(worldErrorMessage('snapshot_corrupt')).toBe('快照文件损坏或缺失，未做任何改动');
		expect(worldErrorMessage('rollback_mode_unsupported')).toBe('不支持的回滚方式');
	});
	it('已知码与兜底', () => {
		expect(worldErrorMessage('weird')).toBe('操作失败：weird');
	});
	it('切换与回滚共用的码:文案对两种语境都成立', () => {
		expect(worldErrorMessage('ingest_busy')).toBe(
			'后台任务或在途请求暂时停不下来，未做任何改动，请稍后再试'
		);
		expect(worldErrorMessage('world_open_failed')).toBe(
			'世界打不开，请稍后再试或查看服务日志'
		);
		expect(worldErrorMessage('insufficient_disk')).toBe(
			'磁盘空间不足：快照需要至少 3 倍于数据量、回滚需要至少 2 倍于快照大小的可用空间'
		);
		expect(worldErrorMessage('snapshot_failed')).toBe('文件操作失败，数据库未改动；详情见服务日志');
	});
	it('回滚的两个新码:world_in_use 与 rollback_reopen_failed', () => {
		expect(worldErrorMessage('world_in_use')).toBe(
			'该世界的数据库正被其他程序打开，未做任何改动；请关闭后重试'
		);
		expect(worldErrorMessage('rollback_reopen_failed')).toBe(
			'已回滚到快照，但引擎重开失败；页面即将刷新，下一次写入、召回或手动 tick 时会自动重建，仍异常请查看服务日志'
		);
	});
	it('snapshot_not_found（契约里 DELETE 快照的 404）有中文文案', () => {
		expect(worldErrorMessage('snapshot_not_found')).toBe('快照不存在');
	});
	it('租户不变式、切换窗口与服务器内部错误有中文文案', () => {
		expect(worldErrorMessage('world_tenant_mismatch')).toBe(
			'该世界的数据库里有其他租户的数据，拒绝打开'
		);
		expect(worldErrorMessage('world_switching')).toBe('操作失败：world_switching');
		expect(worldErrorMessage('internal_error')).toBe('服务器内部错误');
	});
	it.each(['constructor', 'toString', '__proto__', 'hasOwnProperty'])(
		'继承来的键 %s 不是错误码，走兜底而不是返回函数',
		(k) => {
			const msg = worldErrorMessage(k);
			expect(typeof msg).toBe('string');
			expect(msg).toBe(`操作失败：${k}`);
		}
	);
	it('从 ApiError 提取 detail', () => {
		expect(apiDetail(new ApiError(409, '/api/worlds', '409 world_exists'))).toBe('world_exists');
		expect(apiDetail(new Error('x'))).toBe('Error: x');
	});
});

// ── W4:自动快照设置、会话清单 ────────────────────────────────────────────
describe('worldsApi 自动快照与会话清单（W4）', () => {
	it('setAutoSnapshot：PUT /auto_snapshot，body 只有 {on_session, interval_hours}', async () => {
		const cfg = { on_session: false, interval_hours: 24, extra: 'x' };
		await worldsApi.setAutoSnapshot('sim-1', cfg);
		expect(calls.map(([p, i]) => `${i.method ?? 'GET'} ${p}`)).toEqual([
			'PUT /api/worlds/sim-1/auto_snapshot'
		]);
		expect(JSON.parse(String(calls[0][1].body))).toEqual({ on_session: false, interval_hours: 24 });
	});

	it('listSessions：GET /sessions?limit=N，默认 limit=200', async () => {
		await worldsApi.listSessions('sim-1', 50);
		await worldsApi.listSessions('sim-1');
		expect(calls.map(([p, i]) => `${i.method ?? 'GET'} ${p}`)).toEqual([
			'GET /api/worlds/sim-1/sessions?limit=50',
			'GET /api/worlds/sim-1/sessions?limit=200'
		]);
	});

	it('create 带自动快照配置时 body 里有 auto_snapshot；不带时不发这个字段', async () => {
		await worldsApi.create('sim-2', '', { on_session: true, interval_hours: 0 });
		await worldsApi.create('sim-3', 'x');
		expect(JSON.parse(String(calls[0][1].body))).toEqual({
			name: 'sim-2',
			label: '',
			auto_snapshot: { on_session: true, interval_hours: 0 }
		});
		expect(JSON.parse(String(calls[1][1].body))).toEqual({ name: 'sim-3', label: 'x' });
	});

	it('setAutoSnapshot 的超时放宽到 120s（要排队等快照或回滚持有的管理锁）', async () => {
		vi.useFakeTimers();
		try {
			globalThis.fetch = vi.fn(
				(_p: string, init: RequestInit) =>
					new Promise((_resolve, reject) => {
						init.signal?.addEventListener('abort', () =>
							reject(new DOMException('aborted', 'AbortError'))
						);
					})
			) as unknown as typeof fetch;
			const caught = worldsApi
				.setAutoSnapshot('sim-1', { on_session: true, interval_hours: 0 })
				.catch((e) => e as ApiError);
			let settled = false;
			void caught.then(() => (settled = true));
			await vi.advanceTimersByTimeAsync(119_000);
			expect(settled).toBe(false);
			await vi.advanceTimersByTimeAsync(2_000);
			const err = (await caught) as ApiError;
			expect(err).toBeInstanceOf(ApiError);
			expect(err.message).toContain('120');
		} finally {
			vi.useRealTimers();
		}
	});
});

describe('自动快照与会话的显示辅助（W4）', () => {
	it('invalid_auto_snapshot 有中文文案', () => {
		expect(worldErrorMessage('invalid_auto_snapshot')).toBe('自动快照设置无效（间隔须为 0–8760 小时）');
	});

	it.each([
		[0, 0], [24, 24], [8760, 8760], ['12', 12], [' 6 ', 6]
	] as const)('parseIntervalHours 接受 %s → %s', (v, want) => expect(parseIntervalHours(v)).toBe(want));
	it.each([-1, 8761, 1.5, '', '  ', 'abc', null, undefined, Number.NaN])(
		'parseIntervalHours 拒绝 %s',
		(v) => expect(parseIntervalHours(v)).toBeNull()
	);

	it('autoSnapshotLabel：按会话 / 每 N 小时 / 两者 / 关闭', () => {
		expect(autoSnapshotLabel({ on_session: true, interval_hours: 24 })).toBe('按会话 · 每 24 小时');
		expect(autoSnapshotLabel({ on_session: true, interval_hours: 0 })).toBe('按会话');
		expect(autoSnapshotLabel({ on_session: false, interval_hours: 24 })).toBe('每 24 小时');
		expect(autoSnapshotLabel({ on_session: false, interval_hours: 0 })).toBe('关闭');
	});

	it('snapshotReasonLabel：manual/session/interval 有中文，未知值原样显示', () => {
		expect(snapshotReasonLabel('manual')).toBe('手动');
		expect(snapshotReasonLabel('session')).toBe('会话开始前');
		expect(snapshotReasonLabel('interval')).toBe('定时');
		expect(snapshotReasonLabel('pre_switch')).toBe('pre_switch');
		expect(snapshotReasonLabel('constructor')).toBe('constructor');
	});

	it('shortId 取前 N 位', () => {
		expect(shortId('0123456789abcdef', 8)).toBe('01234567');
		expect(shortId('abc', 8)).toBe('abc');
	});
});

describe('worldsApi 多世界（第 1 期）', () => {
	it('activate 已去掉', () => {
		expect('activate' in worldsApi).toBe(false);
	});
	it('list：按标签过滤与分页拼成 label.<key>=<value>&offset=&limit=', async () => {
		await worldsApi.list({ labels: { env: 'prod', 'team x': 'a&b' }, offset: 50, limit: 25 });
		expect(calls[0][0]).toBe('/api/worlds?label.env=prod&label.team%20x=a%26b&offset=50&limit=25');
		await worldsApi.list();
		expect(calls[1][0]).toBe('/api/worlds');
	});
	it('detail / close / updateSettings 路径与方法', async () => {
		await worldsApi.detail('Run-A');
		await worldsApi.close('Run-A');
		await worldsApi.updateSettings('Run-A', { background: { auto_snapshot: { on_session: true, interval_hours: 0 } } });
		expect(calls.map(([p, i]) => `${i.method ?? 'GET'} ${p}`)).toEqual([
			'GET /api/worlds/Run-A',
			'POST /api/worlds/Run-A/close',
			'PUT /api/worlds/Run-A/settings'
		]);
		expect(JSON.parse(String(calls[2][1].body))).toEqual({
			background: { auto_snapshot: { on_session: true, interval_hours: 0 } }
		});
	});
	it('create：带 labels 时发 labels；没传 auto_snapshot 就不发这个字段', async () => {
		await worldsApi.create('sim-2', '', undefined, { env: 'test' });
		expect(JSON.parse(String(calls[0][1].body))).toEqual({
			name: 'sim-2',
			label: '',
			labels: { env: 'test' }
		});
	});
	it.each([
		['world_busy', '世界正在关闭或恢复，请稍后再试'],
		['world_not_found', '世界不存在'],
		['world_not_initialized', '这个世界的库文件不见了'],
		['endpoint_changed', '供应商地址改了，这个世界需要重新确认端点'],
		['labels_immutable', '标签建好后不能改'],
		['invalid_settings', '设置不合法'],
		['world_tenant_mismatch', '该世界的数据库里有其他租户的数据，拒绝打开']
	])('错误码 %s 的中文文案', (code, text) => expect(worldErrorMessage(code)).toBe(text));
});

describe('parseLabels', () => {
	it('解析 key=value,逗号或空白分隔', () => {
		expect(parseLabels('')).toEqual({});
		expect(parseLabels('env=prod')).toEqual({ env: 'prod' });
		expect(parseLabels('env=prod, team=x  k=v')).toEqual({ env: 'prod', team: 'x', k: 'v' });
		expect(parseLabels('a=b=c')).toEqual({ a: 'b=c' });
	});
	it('不是 key=value 的段返回 null', () => {
		for (const t of ['env', '=x', 'env=', 'a=b,c']) expect(parseLabels(t)).toBeNull();
	});
});

describe('describeError 与 worlds_unavailable', () => {
	it('worlds_unavailable 有中文文案', () => {
		expect(worldErrorMessage('worlds_unavailable')).toBe('世界注册表暂时不可用，请稍后再试');
	});
	it('已知世界错误码给中文；其他错误保持原 message', () => {
		for (const [code, text] of [
			['world_not_found', '世界不存在'],
			['world_busy', '世界正在关闭或恢复，请稍后再试'],
			['world_not_initialized', '这个世界的库文件不见了'],
			['worlds_unavailable', '世界注册表暂时不可用，请稍后再试']
		])
			expect(describeError(new ApiError(404, '/x', `404 ${code}`))).toBe(text);
		expect(describeError(new ApiError(500, '/x', '500 boom'))).toBe('500 boom');
		expect(describeError(new Error('plain'))).toBe('plain');
		expect(describeError('str')).toBe('str');
	});
});
