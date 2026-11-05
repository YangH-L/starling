import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { tick } from 'svelte';
import { render, fireEvent } from '@testing-library/svelte';
import { ApiError } from '$lib/api';
import { lastWsEvent } from '$lib/health';
import { page } from '$app/state';
import { noteDefaultWorld } from '$lib/world.svelte';

const toastError = vi.fn();
const toastSuccess = vi.fn();
vi.mock('$lib/ui/toast', () => ({
	toast: {
		error: (...a: unknown[]) => toastError(...a),
		success: (...a: unknown[]) => toastSuccess(...a),
		info: vi.fn()
	}
}));

const detail = vi.fn();
const listSessions = vi.fn();
const rollback = vi.fn();
vi.mock('$lib/worlds', async (orig) => {
	const real = await orig<typeof import('$lib/worlds')>();
	return {
		...real,
		worldsApi: {
			...real.worldsApi,
			detail: (...a: unknown[]) => detail(...a),
			listSessions: (...a: unknown[]) => listSessions(...a),
			rollback: (...a: unknown[]) => rollback(...a)
		}
	};
});

import Sessions from './+page.svelte';
import { worldErrorMessage, type SessionRow } from '$lib/worlds';

const settle = async () => {
	await tick();
	await Promise.resolve();
	await Promise.resolve();
	await tick();
};

// 世界详情(GET /api/worlds/{name}):open = 引擎现在开着。
const world = (name: string, open: boolean, label = '') => ({
	name, label, labels: {}, tenant: name, created_at: '', open, holders: 0, last_write_at: null,
	db_bytes: 0, live_bytes: 0, statements: 0, engrams: 0, snapshots: 0, snapshot_bytes: 0,
	archives: 0, archive_bytes: 0, auto_snapshot: { on_session: true, interval_hours: 0 }
});
const WORLDS: Record<string, ReturnType<typeof world>> = {
	default: world('default', true, '主世界'),
	'sim-1': world('sim-1', false)
};
const mockDetail = () =>
	detail.mockReset().mockImplementation(async (n: string) => WORLDS[n]);
// 选中的世界在 URL ?world= 里(顶栏选择器写它);没写就是默认世界。
const at = (world?: string) => {
	(page as { url: URL }).url = new URL(world ? `http://localhost/sessions?world=${world}` : 'http://localhost/sessions');
};

const ANCHORED: SessionRow & { anchor: NonNullable<SessionRow['anchor']> } = {
	session_id: 'sess-2026-10-27-evening-abcdef', units: 42, speakers: 2,
	first_at: '2026-10-27T19:00:00Z', last_at: '2026-10-27T20:30:00Z',
	anchor: { snapshot_id: '20261027T185959Z-ab12', created_at: '2026-10-27T18:59:59Z' }
};
// 两个夹具的前 12 位不同('sess-2026-10' / 'chat-2026-10'),「前 12 位」的用例才分得出行序。
const UNANCHORED: SessionRow = {
	session_id: 'chat-2026-10-26-morning-123456', units: 7, speakers: 1,
	first_at: '2026-10-26T08:00:00Z', last_at: '2026-10-26T08:40:00Z', anchor: null
};
const sessionsOf = (w: string, rows: SessionRow[] = [ANCHORED, UNANCHORED], extra = {}) => ({
	world: w, source_lane_enabled: true, total: rows.length, null_observed: 0, truncated: false,
	sessions: rows, ...extra
});

const button = (root: ParentNode, text: string) =>
	[...root.querySelectorAll('button')].find((b) => b.textContent?.trim() === text) as
		| HTMLButtonElement
		| undefined;
const inBody = (text: string) => button(document.body, text);
const apiErr = (status: number, detail: string) => new ApiError(status, '', `${status} ${detail}`);
const bodyRows = (c: HTMLElement) => [...c.querySelectorAll('tbody tr')];
const cells = (tr: Element) => [...tr.children] as HTMLElement[];
const rollbackButtons = (c: HTMLElement) =>
	[...c.querySelectorAll('button')].filter((b) => b.textContent?.trim() === '回滚到此会话之前');
const confirmInput = () =>
	document.body.querySelector('input[name="world-rollback-confirm"]') as HTMLInputElement;

describe('/sessions 当前世界（顶栏选的，URL ?world=）', () => {
	beforeEach(() => {
		mockDetail();
		at();
		noteDefaultWorld('default');
		listSessions.mockReset().mockImplementation(async (w: string) => sessionsOf(w));
		lastWsEvent.set(null);
	});

	it('没有 ?world= 时是默认世界，按契约的 limit=200 取清单', async () => {
		const { container } = render(Sessions);
		await settle();
		expect(listSessions).toHaveBeenCalledTimes(1);
		expect(listSessions).toHaveBeenCalledWith('default', 200);
		expect(detail).toHaveBeenCalledWith('default');
		expect(container.textContent).toContain('世界：default');
		// 页面不再有自己的世界下拉
		expect(container.querySelector('select')).toBeNull();
	});

	it('?world=sim-1 时取 sim-1 的清单', async () => {
		at('sim-1');
		listSessions.mockImplementation(async (w: string) =>
			w === 'sim-1' ? sessionsOf('sim-1', [UNANCHORED]) : sessionsOf(w)
		);
		const { container } = render(Sessions);
		await settle();
		expect(listSessions).toHaveBeenCalledTimes(1);
		expect(listSessions).toHaveBeenLastCalledWith('sim-1', 200);
		expect(bodyRows(container).length).toBe(1);
		expect(cells(bodyRows(container)[0])[0].textContent?.trim()).toBe('chat-2026-10');
		expect(rollbackButtons(container).length).toBe(0);
	});

	it('清单加载失败：显示中文错误与重试', async () => {
		listSessions.mockReset()
			.mockRejectedValueOnce(apiErr(503, 'world_busy'))
			.mockImplementation(async (w: string) => sessionsOf(w));
		const { container } = render(Sessions);
		await settle();
		expect(container.textContent).toContain('世界正在关闭或恢复，请稍后再试');
		await fireEvent.click(button(container, '重试')!);
		await settle();
		expect(bodyRows(container).length).toBe(2);
	});

	it('副标题说明回滚会一并丢弃之后的会话', async () => {
		const { container } = render(Sessions);
		await settle();
		expect(container.textContent).toContain(
			'要撤掉一个会话及其之后的写入，回滚到它开始前的快照；之后的会话也会一并丢弃'
		);
	});

	it('世界不存在（?world= 写错）：显示中文错误而不是裸错误码', async () => {
		at('nope');
		detail.mockReset().mockRejectedValue(apiErr(404, 'world_not_found'));
		listSessions.mockReset().mockRejectedValue(apiErr(404, 'world_not_found'));
		const { container } = render(Sessions);
		await settle();
		expect(container.textContent).toContain('世界不存在');
		expect(container.textContent).not.toContain('world_not_found');
	});

	it('世界详情加载失败：显示加载失败', async () => {
		detail.mockReset().mockRejectedValue(new Error('500 boom'));
		const { container } = render(Sessions);
		await settle();
		expect(container.textContent).toContain('加载失败');
	});

	it('当前世界的 world_changed 重取清单；recall 与别的世界的消息不重取', async () => {
		render(Sessions);
		await settle();
		const before = listSessions.mock.calls.length;
		lastWsEvent.set({ type: 'recall', world: 'default', payload: {} });
		await settle();
		expect(listSessions.mock.calls.length).toBe(before);
		// 别的世界的消息不触发重取
		lastWsEvent.set({ type: 'world_changed', world: 'sim-1', payload: {} });
		await settle();
		expect(listSessions.mock.calls.length).toBe(before);
		lastWsEvent.set({ type: 'world_changed', world: 'default', payload: {} });
		await settle();
		expect(listSessions.mock.calls.length).toBeGreaterThan(before);
		expect(listSessions).toHaveBeenLastCalledWith('default', 200);
	});
});

describe('/sessions 表格', () => {
	beforeEach(() => {
		mockDetail();
		at();
		noteDefaultWorld('default');
		listSessions.mockReset().mockImplementation(async (w: string) => sessionsOf(w));
	});

	it('列头依次为 会话/话轮数/说话人数/开始/最近/回滚锚点，都是 scope=col 的表头', async () => {
		const { container } = render(Sessions);
		await settle();
		const heads = [...container.querySelectorAll('thead th')];
		expect(heads.map((h) => h.textContent?.trim())).toEqual([
			'会话', '话轮数', '说话人数', '开始', '最近', '回滚锚点'
		]);
		expect(heads.every((h) => h.getAttribute('scope') === 'col')).toBe(true);
	});

	it('每行：session_id 前 12 位（悬停看全文、作行表头）、话轮数、说话人数、开始、最近', async () => {
		const { container } = render(Sessions);
		await settle();
		const [first, second] = bodyRows(container);
		// 按后端给的顺序渲染
		expect(cells(second)[0].textContent?.trim()).toBe('chat-2026-10');
		expect(cells(second)[0].querySelector(`[title="${UNANCHORED.session_id}"]`)).not.toBeNull();
		const c = cells(first);
		expect(c[0].tagName).toBe('TH');
		expect(c[0].getAttribute('scope')).toBe('row');
		expect(c[0].textContent?.trim()).toBe('sess-2026-10');
		expect(c[0].querySelector(`[title="${ANCHORED.session_id}"]`)).not.toBeNull();
		expect(c.slice(1, 5).map((x) => x.textContent?.trim())).toEqual([
			'42', '2', '2026-10-27T19:00:00Z', '2026-10-27T20:30:00Z'
		]);
	});

	it('开始/最近为 null 时显示 —', async () => {
		listSessions.mockResolvedValue(
			sessionsOf('default', [{ ...UNANCHORED, first_at: null, last_at: null }])
		);
		const { container } = render(Sessions);
		await settle();
		const c = cells(bodyRows(container)[0]);
		expect(c[3].textContent?.trim()).toBe('—');
		expect(c[4].textContent?.trim()).toBe('—');
	});

	it('有锚点：显示快照 id 和「回滚到此会话之前」；没锚点：显示「无」，悬停说明原因，没有回滚按钮', async () => {
		const { container } = render(Sessions);
		await settle();
		const [anchored, bare] = bodyRows(container);
		const a = cells(anchored)[5];
		expect(a.textContent).toContain('20261027T185959Z-ab12');
		expect(button(a, '回滚到此会话之前')).toBeDefined();
		// aria-label 以可见文字开头,并带上完整 session_id 以区分各行
		expect(button(a, '回滚到此会话之前')!.getAttribute('aria-label')).toBe(
			`回滚到此会话之前 ${ANCHORED.session_id}`
		);

		const b = cells(bare)[5];
		const hint = '未开启按会话快照、快照失败，或已被保留策略清理';
		const shown = b.querySelector('[title]')!;
		expect(shown.textContent?.trim()).toBe('无');
		expect(shown.getAttribute('title')).toBe(hint);
		// 悬停之外,读屏也能拿到原因
		expect(b.querySelector('.sr-only')?.textContent).toContain(hint);
		expect(b.querySelector('button')).toBeNull();
		expect(rollbackButtons(container).length).toBe(1);
	});

	it('anchor 的 snapshot_id 为空串时当作无锚点：显示「无」，没有回滚按钮', async () => {
		listSessions.mockResolvedValue(
			sessionsOf('default', [{ ...ANCHORED, anchor: { snapshot_id: '', created_at: '' } }])
		);
		const { container } = render(Sessions);
		await settle();
		const c = cells(bodyRows(container)[0])[5];
		expect(c.querySelector('[title]')!.textContent?.trim()).toBe('无');
		expect(c.querySelector('button')).toBeNull();
		expect(rollbackButtons(container).length).toBe(0);
	});

	it('lane 关闭时表格上方提示；lane 开启时不提示', async () => {
		listSessions.mockResolvedValue(sessionsOf('default', [ANCHORED], { source_lane_enabled: false }));
		const { container, unmount } = render(Sessions);
		await settle();
		const note = [...container.querySelectorAll('[role="status"]')].find((n) =>
			n.textContent?.includes('左脑 lane')
		)!;
		expect(note.textContent?.trim()).toBe('左脑 lane 未开启：新的写入不会记录会话');
		// 提示在表格之前
		expect(note.compareDocumentPosition(container.querySelector('table')!) &
			Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
		unmount();

		listSessions.mockResolvedValue(sessionsOf('default'));
		const second = render(Sessions);
		await settle();
		expect(second.container.textContent).not.toContain('左脑 lane 未开启');
	});

	it('lane 关闭且清单为空时，两条提示都在', async () => {
		listSessions.mockResolvedValue(sessionsOf('default', [], { source_lane_enabled: false }));
		const { container } = render(Sessions);
		await settle();
		expect(container.textContent).toContain('左脑 lane 未开启：新的写入不会记录会话');
		expect(container.textContent).toContain('这个世界还没有会话记录');
	});

	it('truncated 时提示「只显示最近 N 个会话，共 M 个」；不截断时不提示', async () => {
		listSessions.mockResolvedValue(sessionsOf('default', [ANCHORED, UNANCHORED], { total: 350, truncated: true }));
		const { container, unmount } = render(Sessions);
		await settle();
		expect(container.textContent).toContain('只显示最近 2 个会话，共 350 个');
		unmount();

		listSessions.mockResolvedValue(sessionsOf('default'));
		const second = render(Sessions);
		await settle();
		expect(second.container.textContent).not.toContain('只显示最近');
	});

	it('null_observed > 0 时说明没有时间的话轮数；为 0 时不说明', async () => {
		listSessions.mockResolvedValue(sessionsOf('default', [ANCHORED], { null_observed: 5 }));
		const { container, unmount } = render(Sessions);
		await settle();
		expect(container.textContent).toContain('这个世界有 5 个话轮没有记录时间');
		unmount();

		listSessions.mockResolvedValue(sessionsOf('default'));
		const second = render(Sessions);
		await settle();
		expect(second.container.textContent).not.toContain('没有记录时间');
	});

	it('空清单显示「这个世界还没有会话记录」，不渲染表格', async () => {
		listSessions.mockResolvedValue(sessionsOf('default', []));
		const { container } = render(Sessions);
		await settle();
		expect(container.textContent).toContain('这个世界还没有会话记录');
		expect(container.querySelector('table')).toBeNull();
	});
});

describe('/sessions 回滚到会话之前', () => {
	const reload = vi.fn();
	beforeEach(() => {
		mockDetail();
		at();
		noteDefaultWorld('default');
		listSessions.mockReset().mockImplementation(async (w: string) => sessionsOf(w));
		rollback.mockReset();
		reload.mockReset();
		toastError.mockReset();
		toastSuccess.mockReset();
		vi.stubGlobal('location', { ...window.location, reload });
	});
	afterEach(() => {
		vi.unstubAllGlobals();
	});

	const openRollback = async (container: HTMLElement) => {
		await fireEvent.click(rollbackButtons(container)[0]);
		await settle();
	};
	// 用抽屉自带的关闭键(✕)关掉回滚抽屉。
	const closeRollback = async () => {
		const dlg = [...document.body.querySelectorAll('[role="dialog"]')].find((d) =>
			d.textContent?.includes('回滚世界')
		)!;
		await fireEvent.click(button(dlg, '✕')!);
		await settle();
	};

	it('点按钮只打开确认，显示目标快照与丢弃说明，不直接回滚', async () => {
		const { container } = render(Sessions);
		await settle();
		await openRollback(container);
		const text = document.body.textContent ?? '';
		expect(text).toContain('回滚世界 default');
		expect(text).toContain('20261027T185959Z-ab12');
		expect(text).toContain(
			'该世界在这份快照之后写入的所有记忆都会被丢弃。当前的库会改名保留在该世界的 archive 目录，可手动恢复。'
		);
		expect(text).toContain('这个世界现在开着'); // 引擎开着：提示会重建引擎
		expect(text).not.toContain('页面会自动刷新');
		expect(rollback).not.toHaveBeenCalled();
	});

	it('确认名不一致时不能提交（含绕过 disabled）；一致后调用同一个回滚 API', async () => {
		rollback.mockResolvedValue({
			world: 'default', snapshot_id: ANCHORED.anchor.snapshot_id, archived: true, reopened: true
		});
		const { container } = render(Sessions);
		await settle();
		await openRollback(container);

		for (const wrong of ['', 'defaul', 'default ', 'Default', 'sim-1']) {
			await fireEvent.input(confirmInput(), { target: { value: wrong } });
			await tick();
			expect(inBody('确认回滚')!.disabled, wrong).toBe(true);
		}
		const btn = inBody('确认回滚')!;
		btn.disabled = false; // 函数级守卫
		await fireEvent.click(btn);
		await settle();
		expect(rollback).not.toHaveBeenCalled();

		await fireEvent.input(confirmInput(), { target: { value: 'default' } });
		await tick();
		expect(inBody('确认回滚')!.disabled).toBe(false);
		await fireEvent.click(inBody('确认回滚')!);
		await settle();
		expect(rollback).toHaveBeenCalledTimes(1);
		expect(rollback).toHaveBeenCalledWith('default', '20261027T185959Z-ab12', 'default');
		// 引擎在服务端重建,页面不整页刷新
		expect(reload).not.toHaveBeenCalled();
	});

	it('输入过确认名后关掉抽屉，重新打开时输入框为空、确认键禁用', async () => {
		const { container } = render(Sessions);
		await settle();
		await openRollback(container);
		await fireEvent.input(confirmInput(), { target: { value: 'default' } });
		await tick();
		expect(inBody('确认回滚')!.disabled).toBe(false);
		await closeRollback();
		expect(confirmInput()).toBeNull();

		await openRollback(container);
		expect(confirmInput().value).toBe('');
		expect(inBody('确认回滚')!.disabled).toBe(true);
		expect(rollback).not.toHaveBeenCalled();
	});

	it('回滚成功（世界没开着）：toast、关确认、重取清单，不整页刷新', async () => {
		rollback.mockResolvedValue({
			world: 'sim-1', snapshot_id: ANCHORED.anchor.snapshot_id, archived: true, reopened: false
		});
		at('sim-1');
		const { container } = render(Sessions);
		await settle();
		const before = listSessions.mock.calls.length;
		await openRollback(container);
		expect(document.body.textContent).not.toContain('这个世界现在开着');
		await fireEvent.input(confirmInput(), { target: { value: 'sim-1' } });
		await tick();
		await fireEvent.click(inBody('确认回滚')!);
		await settle();
		expect(rollback).toHaveBeenCalledWith('sim-1', '20261027T185959Z-ab12', 'sim-1');
		expect(reload).not.toHaveBeenCalled();
		expect(toastSuccess).toHaveBeenCalledTimes(1);
		expect(listSessions.mock.calls.length).toBeGreaterThan(before);
		expect(listSessions).toHaveBeenLastCalledWith('sim-1', 200);
		expect(confirmInput()).toBeNull();
	});

	it('请求在途：确认键与各行回滚按钮禁用，不能重复提交', async () => {
		let resolve!: (v: unknown) => void;
		rollback.mockReturnValue(new Promise((r) => (resolve = r)));
		const { container } = render(Sessions);
		await settle();
		await openRollback(container);
		await fireEvent.input(confirmInput(), { target: { value: 'default' } });
		await tick();
		await fireEvent.click(inBody('确认回滚')!);
		await settle();
		expect(inBody('确认回滚')!.disabled).toBe(true);
		expect(rollbackButtons(container).every((b) => b.disabled)).toBe(true);
		await fireEvent.click(inBody('确认回滚')!);
		expect(rollback).toHaveBeenCalledTimes(1);
		resolve({ world: 'default', snapshot_id: 's', archived: true, reopened: true });
		await settle();
	});

	it('失败：与 /worlds 相同的中文错误文案，不刷新页面，确认键恢复可点', async () => {
		rollback.mockRejectedValue(apiErr(409, 'snapshot_corrupt'));
		const { container } = render(Sessions);
		await settle();
		await openRollback(container);
		await fireEvent.input(confirmInput(), { target: { value: 'default' } });
		await tick();
		await fireEvent.click(inBody('确认回滚')!);
		await settle();
		expect(toastError).toHaveBeenCalledWith(worldErrorMessage('snapshot_corrupt'));
		expect(reload).not.toHaveBeenCalled();
		expect(inBody('确认回滚')!.disabled).toBe(false);
	});

	it('已回滚但引擎重开失败（rollback_reopen_failed）：toast 后整页刷新', async () => {
		rollback.mockRejectedValue(apiErr(500, 'rollback_reopen_failed'));
		const { container } = render(Sessions);
		await settle();
		await openRollback(container);
		await fireEvent.input(confirmInput(), { target: { value: 'default' } });
		await tick();
		await fireEvent.click(inBody('确认回滚')!);
		await settle();
		expect(toastError).toHaveBeenCalledWith(worldErrorMessage('rollback_reopen_failed'));
		expect(reload).toHaveBeenCalledTimes(1);
	});
});
