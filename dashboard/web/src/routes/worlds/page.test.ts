import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { tick } from 'svelte';
import { render, fireEvent } from '@testing-library/svelte';
import { ApiError } from '$lib/api';
import { lastWsEvent } from '$lib/health';
import { noteDefaultWorld } from '$lib/world.svelte';
import { page } from '$app/state';
import { goto } from '$app/navigation';

const toastError = vi.fn();
const toastSuccess = vi.fn();
vi.mock('$lib/ui/toast', () => ({
	toast: {
		error: (...a: unknown[]) => toastError(...a),
		success: (...a: unknown[]) => toastSuccess(...a),
		info: vi.fn()
	}
}));

const list = vi.fn();
const create = vi.fn();
const detail = vi.fn();
const remove = vi.fn();
const snapshots = vi.fn();
const snapshot = vi.fn();
const removeSnapshot = vi.fn();
const rollback = vi.fn();
const purgeArchives = vi.fn();
const setAutoSnapshot = vi.fn();
const listSessions = vi.fn();
const reconfirm = vi.fn();
vi.mock('$lib/worlds', async (orig) => {
	const real = await orig<typeof import('$lib/worlds')>();
	return {
		...real,
		worldsApi: {
			list: (...a: unknown[]) => list(...a),
			create: (...a: unknown[]) => create(...a),
			detail: (...a: unknown[]) => detail(...a),
			remove: (...a: unknown[]) => remove(...a),
			snapshots: (...a: unknown[]) => snapshots(...a),
			snapshot: (...a: unknown[]) => snapshot(...a),
			removeSnapshot: (...a: unknown[]) => removeSnapshot(...a),
			rollback: (...a: unknown[]) => rollback(...a),
			purgeArchives: (...a: unknown[]) => purgeArchives(...a),
			setAutoSnapshot: (...a: unknown[]) => setAutoSnapshot(...a),
			listSessions: (...a: unknown[]) => listSessions(...a),
			reconfirm: (...a: unknown[]) => reconfirm(...a)
		}
	};
});

import Worlds from './+page.svelte';
import { worldErrorMessage } from '$lib/worlds';

const settle = async () => {
	await tick();
	await Promise.resolve();
	await Promise.resolve();
	await tick();
};

const DATA = {
	default: 'default', total: 2, snapshot_retention: 5,
	auto_snapshot_status: { failures: 0, last_error: null, last_error_at: null },
	worlds: [
		{ name: 'default', label: '主世界', labels: {}, tenant: 'default', created_at: '', open: true,
		  holders: 4, last_write_at: '2026-11-03T10:00:00Z', statements: 3632,
		  snapshots: 0, snapshot_bytes: 0, archives: 0, archive_bytes: 0,
		  auto_snapshot: { on_session: false, interval_hours: 24 } },
		{ name: 'sim-1', label: '', labels: { env: 'test' }, tenant: 'sim-1', created_at: '', open: false,
		  holders: 0, last_write_at: null, statements: 0, snapshots: 0, snapshot_bytes: 0,
		  archives: 0, archive_bytes: 0, auto_snapshot: { on_session: true, interval_hours: 0 } }
	]
};

// 默认世界的详情:新建世界表单的自动快照初值取自它的 settings.background.auto_snapshot。
const DEFAULT_DETAIL = {
	...DATA.worlds[0],
	db_bytes: 2_500_000_000, live_bytes: 60_000_000, engrams: 101,
	settings: { background: { auto_snapshot: { on_session: true, interval_hours: 0 } } }
};

// 每个用例都先把默认世界的详情接好（新建表单挂载时就会取它）。
beforeEach(() => {
	detail.mockReset().mockResolvedValue(DEFAULT_DETAIL);
	noteDefaultWorld('default');
});

const button = (c: HTMLElement, text: string) =>
	[...c.querySelectorAll('button')].find((b) => b.textContent?.trim() === text) as
		| HTMLButtonElement
		| undefined;

describe('/worlds', () => {
	beforeEach(() => {
		list.mockReset().mockResolvedValue(DATA);
		detail.mockReset().mockResolvedValue(DEFAULT_DETAIL);
		noteDefaultWorld('default');
		create.mockReset();
		remove.mockReset();
		snapshots.mockReset().mockResolvedValue({ snapshots: [] });
		snapshot.mockReset();
		removeSnapshot.mockReset();
		toastError.mockReset();
		toastSuccess.mockReset();
		lastWsEvent.set(null);
	});

	it('列出世界：默认有标记、开着的有「已打开」，没有「当前」徽标，也没有切换/激活按钮', async () => {
		const { container } = render(Worlds);
		await settle();
		expect(container.textContent).toContain('default');
		expect(container.textContent).toContain('sim-1');
		expect(container.textContent).toContain('默认');
		expect(container.textContent).toContain('3632');
		const badges = [...container.querySelectorAll('span')].map((e) => e.textContent?.trim());
		expect(badges).not.toContain('当前');
		expect(container.textContent).not.toContain('切换');
		expect(container.textContent).toContain('已打开');
		expect(container.textContent?.match(/已打开/g)?.length).toBe(1); // 只有 default 开着
	});

	it('每个世界行显示标签（key=value）', async () => {
		const { container } = render(Worlds);
		await settle();
		const labels = container.querySelector('[aria-label="sim-1 的标签"]');
		expect(labels?.textContent).toContain('env=test');
		expect(container.querySelector('[aria-label="default 的标签"]')).toBeNull();
	});

	it('首次请求第一页：offset 0、limit 25、无标签过滤', async () => {
		render(Worlds);
		await settle();
		expect(list).toHaveBeenCalledWith({ labels: {}, offset: 0, limit: 25 });
		expect(list.mock.calls[0][0].labels).toEqual({});
	});

	it('按标签过滤：key=value 提交后带 labels 重取并回到第一页；清除后恢复', async () => {
		const { container } = render(Worlds);
		await settle();
		const input = container.querySelector('input[name="world-label-filter"]') as HTMLInputElement;
		await fireEvent.input(input, { target: { value: 'env=test, team=x' } });
		await tick();
		await fireEvent.click(button(container, '过滤')!);
		await settle();
		expect(list).toHaveBeenLastCalledWith({ labels: { env: 'test', team: 'x' }, offset: 0, limit: 25 });
		await fireEvent.click(button(container, '清除过滤')!);
		await settle();
		expect(list).toHaveBeenLastCalledWith({ labels: {}, offset: 0, limit: 25 });
	});

	it('标签过滤写错（不是 key=value）：提示且「过滤」禁用，不发请求', async () => {
		const { container } = render(Worlds);
		await settle();
		const before = list.mock.calls.length;
		const input = container.querySelector('input[name="world-label-filter"]') as HTMLInputElement;
		await fireEvent.input(input, { target: { value: 'env' } });
		await tick();
		expect(button(container, '过滤')!.disabled).toBe(true);
		expect(container.textContent).toContain('每段都要写成 key=value');
		expect(list.mock.calls.length).toBe(before);
	});

	it('分页：显示「第 a–b 个，共 N 个」；下一页 offset 前进 25，上一页退回；首/末页按钮禁用', async () => {
		list.mockReset().mockImplementation(async (o: { offset: number }) => ({
			...DATA, total: 60, worlds: o.offset === 0 ? DATA.worlds : [DATA.worlds[1]]
		}));
		const { container } = render(Worlds);
		await settle();
		expect(container.textContent).toContain('第 1–2 个，共 60 个');
		expect(button(container, '上一页')!.disabled).toBe(true);
		await fireEvent.click(button(container, '下一页')!);
		await settle();
		expect(list).toHaveBeenLastCalledWith({ labels: {}, offset: 25, limit: 25 });
		expect(container.textContent).toContain('第 26–26 个，共 60 个');
		expect(button(container, '上一页')!.disabled).toBe(false);
		await fireEvent.click(button(container, '上一页')!);
		await settle();
		expect(list).toHaveBeenLastCalledWith({ labels: {}, offset: 0, limit: 25 });
	});

	it('最后一页：下一页禁用', async () => {
		list.mockReset().mockResolvedValue({ ...DATA, total: 2 });
		const { container } = render(Worlds);
		await settle();
		expect(button(container, '下一页')!.disabled).toBe(true);
	});

	it('过滤后没有匹配：显示空态', async () => {
		list.mockReset().mockResolvedValue({ ...DATA, total: 0, worlds: [] });
		const { container } = render(Worlds);
		await settle();
		expect(container.textContent).toContain('还没有世界');
	});

	it('世界名不合法时新建按钮禁用，且不调用 API', async () => {
		const { container } = render(Worlds);
		await settle();
		const input = container.querySelector('input[name="world-name"]') as HTMLInputElement;
		await fireEvent.input(input, { target: { value: 'Bad Name' } });
		await tick();
		const btn = button(container, '新建世界')!;
		expect(btn.disabled).toBe(true);
		await fireEvent.click(btn);
		expect(create).not.toHaveBeenCalled();
	});

	it('合法名称可以新建', async () => {
		create.mockResolvedValue({ ...DATA.worlds[1], name: 'sim-2' });
		const { container } = render(Worlds);
		await settle();
		const input = container.querySelector('input[name="world-name"]') as HTMLInputElement;
		await fireEvent.input(input, { target: { value: 'sim-2' } });
		await tick();
		await fireEvent.click(button(container, '新建世界')!);
		await settle();
		// 自动快照没改过：不发 auto_snapshot，由后端继承默认世界（spec §8.2）；没写标签就是 {}
		expect(create).toHaveBeenCalledWith('sim-2', '', undefined, {});
	});

	it('加载失败显示错误态', async () => {
		list.mockReset().mockRejectedValue(new Error('500 boom'));
		const { container } = render(Worlds);
		await settle();
		expect(container.textContent).toContain('加载失败');
	});
});

// ConfirmDialog / Drawer 走 bits-ui 的 Portal,渲染在 document.body 而不是 container 里。
const inBody = (text: string) => button(document.body, text);
const apiErr = (status: number, detail: string) => new ApiError(status, '', `${status} ${detail}`);

const SNAPS = [
	{ id: 's-auto', kind: 'auto', reason: 'interval', note: '', created_at: '2026-10-25T08:00:00Z',
	  world_time: '', size_bytes: 57 * 1024 * 1024, sessions_complete: null, session_id: null },
	{ id: 's-man', kind: 'manual', reason: 'manual', note: '切换前留底', created_at: '2026-10-25T09:00:00Z',
	  world_time: '2026-03-01T00:00:00Z', size_bytes: 1536, sessions_complete: 3, session_id: null }
];

describe('/worlds 不再有激活', () => {
	beforeEach(() => {
		list.mockReset().mockResolvedValue(DATA);
		detail.mockReset().mockResolvedValue(DEFAULT_DETAIL);
	});

	it('没有「切换到此世界」按钮；每行有指向 ?world= 的「查看」链接', async () => {
		const { container } = render(Worlds);
		await settle();
		expect(button(container, '切换到此世界')).toBeUndefined();
		const link = container.querySelector('a[aria-label="查看世界 sim-1"]') as HTMLAnchorElement;
		expect(link.getAttribute('href')).toBe('/?world=sim-1');
	});
});

describe('/worlds 删除', () => {
	beforeEach(() => {
		list.mockReset().mockResolvedValue(DATA);
		remove.mockReset();
		toastError.mockReset();
		toastSuccess.mockReset();
	});

	it('默认世界没有删除按钮,只有 sim-1 有', async () => {
		const { container } = render(Worlds);
		await settle();
		const dels = [...container.querySelectorAll('button')].filter(
			(b) => b.textContent?.trim() === '删除'
		);
		expect(dels.length).toBe(1);
		expect(dels[0].getAttribute('aria-label')).toBe('删除 sim-1');
	});

	it('手输世界名一致之前确认键禁用;一致后才调用 remove(name, confirm)', async () => {
		remove.mockResolvedValue({ deleted: 'sim-1' });
		const { container } = render(Worlds);
		await settle();
		await fireEvent.click(button(container, '删除')!);
		await settle();

		const confirm = inBody('确认删除')!;
		expect(confirm.disabled).toBe(true);
		await fireEvent.click(confirm);
		expect(remove).not.toHaveBeenCalled();

		const input = document.body.querySelector('input[name="world-delete-confirm"]') as HTMLInputElement;
		await fireEvent.input(input, { target: { value: 'sim' } });
		await tick();
		expect(inBody('确认删除')!.disabled).toBe(true);

		await fireEvent.input(input, { target: { value: 'sim-1' } });
		await tick();
		expect(inBody('确认删除')!.disabled).toBe(false);
		await fireEvent.click(inBody('确认删除')!);
		await settle();
		expect(remove).toHaveBeenCalledWith('sim-1', 'sim-1');
		expect(toastSuccess).toHaveBeenCalled();
		expect(list.mock.calls.length).toBeGreaterThanOrEqual(2); // 删除成功后重取
	});

	it('函数级守卫:绕过按钮的 disabled 也不会在名称不符时调用 remove', async () => {
		const { container } = render(Worlds);
		await settle();
		await fireEvent.click(button(container, '删除')!);
		await settle();
		const confirm = inBody('确认删除')!;
		confirm.disabled = false; // 模拟日后多出一条绕过按钮的调用路径
		await fireEvent.click(confirm);
		await settle();
		expect(remove).not.toHaveBeenCalled();
	});

	it('后端拒绝(world_protected)时 toast 中文提示', async () => {
		remove.mockRejectedValue(apiErr(409, 'world_protected'));
		const { container } = render(Worlds);
		await settle();
		await fireEvent.click(button(container, '删除')!);
		await settle();
		const input = document.body.querySelector('input[name="world-delete-confirm"]') as HTMLInputElement;
		await fireEvent.input(input, { target: { value: 'sim-1' } });
		await tick();
		await fireEvent.click(inBody('确认删除')!);
		await settle();
		expect(toastError).toHaveBeenCalledWith('默认世界不能删除');
	});
});

describe('/worlds 删除当前选中的世界', () => {
	const del = async (container: HTMLElement) => {
		await fireEvent.click(button(container, '删除')!);
		await settle();
		const input = document.body.querySelector('input[name="world-delete-confirm"]') as HTMLInputElement;
		await fireEvent.input(input, { target: { value: 'sim-1' } });
		await tick();
		await fireEvent.click(inBody('确认删除')!);
		await settle();
	};
	beforeEach(() => {
		list.mockReset().mockResolvedValue(DATA);
		remove.mockReset().mockResolvedValue({ deleted: 'sim-1' });
		(goto as unknown as { mockClear: () => void }).mockClear();
	});
	afterEach(() => {
		(page as { url: URL }).url = new URL('http://localhost/worlds'); // 不把 ?world= 带给后面的用例
	});

	it('删的正是顶栏选着的世界：回到默认世界（去掉 ?world=）', async () => {
		(page as { url: URL }).url = new URL('http://localhost/worlds?world=sim-1');
		const { container } = render(Worlds);
		await settle();
		await del(container);
		expect(remove).toHaveBeenCalledWith('sim-1', 'sim-1');
		expect(goto).toHaveBeenCalledTimes(1);
		expect((goto as any).mock.calls[0][0]).toBe('/worlds');
	});

	it('删的不是选着的世界：不动 URL', async () => {
		(page as { url: URL }).url = new URL('http://localhost/worlds?world=other');
		const { container } = render(Worlds);
		await settle();
		await del(container);
		expect(remove).toHaveBeenCalled();
		expect(goto).not.toHaveBeenCalled();
	});
});

describe('/worlds 新建的错误与 label', () => {
	beforeEach(() => {
		list.mockReset().mockResolvedValue(DATA);
		create.mockReset();
		toastError.mockReset();
	});

	it('名称不合法时显示字段错误并标 aria-invalid', async () => {
		const { container } = render(Worlds);
		await settle();
		const input = container.querySelector('input[name="world-name"]') as HTMLInputElement;
		await fireEvent.input(input, { target: { value: 'Bad Name' } });
		await tick();
		expect(input.getAttribute('aria-invalid')).toBe('true');
		expect(container.textContent).toContain('世界名以字母或数字开头');
	});

	it('世界名输入框经 aria-describedby 关联提示；出错时改为关联错误并标 aria-invalid', async () => {
		const { container } = render(Worlds);
		await settle();
		const input = container.querySelector('input[name="world-name"]') as HTMLInputElement;
		const described = () =>
			(input.getAttribute('aria-describedby') ?? '')
				.split(/\s+/)
				.filter(Boolean)
				.map((id) => document.getElementById(id)?.textContent?.trim());
		expect(described()).toEqual([
			'字母或数字开头，其后可含字母、数字、点、下划线和连字符，最长 128 个字符，区分大小写'
		]);
		expect(input.getAttribute('aria-invalid')).toBe('false');

		await fireEvent.input(input, { target: { value: 'Bad Name' } });
		await tick();
		expect(described()).toEqual([worldErrorMessage('invalid_world_name')]);
		expect(input.getAttribute('aria-invalid')).toBe('true');
	});

	it('间隔小时数输入框经 aria-describedby 关联提示；出错时改为关联错误并标 aria-invalid', async () => {
		const { container } = render(Worlds);
		await settle();
		const input = container.querySelector('input[name="world-new-interval"]') as HTMLInputElement;
		const described = () =>
			(input.getAttribute('aria-describedby') ?? '')
				.split(/\s+/)
				.filter(Boolean)
				.map((id) => document.getElementById(id)?.textContent?.trim());
		expect(described()).toEqual([
			'0 表示关闭；距上一份自动快照超过这么多小时、且期间有新写入时拍一份'
		]);
		expect(input.getAttribute('aria-invalid')).toBe('false');

		await fireEvent.input(input, { target: { value: '9000' } });
		await tick();
		expect(described()).toEqual([worldErrorMessage('invalid_auto_snapshot')]);
		expect(input.getAttribute('aria-invalid')).toBe('true');
	});

	it('显示名随名称一起提交,world_exists 走中文 toast', async () => {
		create.mockRejectedValue(apiErr(409, 'world_exists'));
		const { container } = render(Worlds);
		await settle();
		await fireEvent.input(container.querySelector('input[name="world-name"]')!, { target: { value: 'sim-1' } });
		await fireEvent.input(container.querySelector('input[name="world-label"]')!, { target: { value: ' 推演 ' } });
		await tick();
		await fireEvent.click(button(container, '新建世界')!);
		await settle();
		expect(create).toHaveBeenCalledWith('sim-1', '推演', undefined, {});
		expect(toastError).toHaveBeenCalledWith('同名世界已存在');
	});

	it('label 非空时标题显示「name · label」', async () => {
		const { container } = render(Worlds);
		await settle();
		expect(container.textContent).toContain('default · 主世界');
	});

	it('stats_error 的世界显示「统计不可用」,只出现在那一行', async () => {
		list.mockReset().mockResolvedValue({
			...DATA,
			worlds: [DATA.worlds[0], { ...DATA.worlds[1], stats_error: true }]
		});
		const { container } = render(Worlds);
		await settle();
		expect(container.textContent?.match(/统计不可用/g)?.length).toBe(1);
	});

	it('没有 stats_error 时不显示「统计不可用」', async () => {
		const { container } = render(Worlds);
		await settle();
		expect(container.textContent).not.toContain('统计不可用');
	});
});

describe('/worlds 快照抽屉', () => {
	beforeEach(() => {
		list.mockReset().mockResolvedValue(DATA);
		snapshots.mockReset().mockResolvedValue({ snapshots: SNAPS });
		snapshot.mockReset();
		removeSnapshot.mockReset();
		toastError.mockReset();
		toastSuccess.mockReset();
	});

	const openSnaps = async (container: HTMLElement) => {
		const btn = [...container.querySelectorAll('button')].find(
			(b) => b.getAttribute('aria-label') === '快照 sim-1'
		)!;
		await fireEvent.click(btn);
		await settle();
	};

	it('展示标题、行内容与保留策略说明', async () => {
		const { container } = render(Worlds);
		await settle();
		await openSnaps(container);
		expect(snapshots).toHaveBeenCalledWith('sim-1');
		const text = document.body.textContent ?? '';
		expect(text).toContain('sim-1 的快照');
		expect(text).toContain('2026-10-25T08:00:00Z');
		expect(text).toContain('定时');
		expect(text).toContain('手动');
		expect(text).toContain('57.0 MB');
		expect(text).toContain('1.5 KB');
		expect(text).toContain('切换前留底');
		expect(text).toContain('世界时间 —'); // world_time 为空
		expect(text).toContain('保留最近 5 份，超出时优先清理自动快照，并至少保留 1 份自动快照作为回滚锚点。');
	});

	it('文件缺失的快照显示「文件缺失」,仍可删除', async () => {
		snapshots.mockReset().mockResolvedValue({
			snapshots: [{ ...SNAPS[0], size_bytes: 0, missing: true }, SNAPS[1]]
		});
		const { container } = render(Worlds);
		await settle();
		await openSnaps(container);
		const text = document.body.textContent ?? '';
		expect(text.match(/文件缺失/g)?.length).toBe(1);
		const del = [...document.body.querySelectorAll('button')].filter((b) =>
			b.getAttribute('aria-label')?.startsWith('删除快照')
		);
		expect(del.length).toBe(2);
		expect(del.every((b) => !b.disabled)).toBe(true);
	});

	it('「立即拍快照」带备注调用 snapshot,成功后重取列表', async () => {
		snapshot.mockResolvedValue(SNAPS[1]);
		const { container } = render(Worlds);
		await settle();
		await openSnaps(container);
		const note = document.body.querySelector('input[name="snapshot-note"]') as HTMLInputElement;
		await fireEvent.input(note, { target: { value: ' 手工留底 ' } });
		await tick();
		await fireEvent.click(inBody('立即拍快照')!);
		await settle();
		expect(snapshot).toHaveBeenCalledWith('sim-1', '手工留底');
		expect(snapshots.mock.calls.length).toBe(2);
		expect(toastSuccess).toHaveBeenCalled();
	});

	it('快照失败(insufficient_disk)toast 中文提示', async () => {
		snapshot.mockRejectedValue(apiErr(507, 'insufficient_disk'));
		const { container } = render(Worlds);
		await settle();
		await openSnaps(container);
		await fireEvent.click(inBody('立即拍快照')!);
		await settle();
		expect(toastError).toHaveBeenCalledWith(
			'磁盘空间不足：快照需要至少 3 倍于数据量、回滚需要至少 2 倍于快照大小的可用空间'
		);
	});

	it('删除快照要经 ConfirmDialog 确认', async () => {
		removeSnapshot.mockResolvedValue({ deleted: 's-man' });
		const { container } = render(Worlds);
		await settle();
		await openSnaps(container);
		const del = [...document.body.querySelectorAll('button')].find(
			(b) => b.getAttribute('aria-label') === `删除快照 ${SNAPS[1].created_at}`
		)!;
		await fireEvent.click(del);
		await settle();
		expect(removeSnapshot).not.toHaveBeenCalled();
		expect(document.body.textContent).toContain('删除这份快照？');
		await fireEvent.click(inBody('确认删除')!);
		await settle();
		expect(removeSnapshot).toHaveBeenCalledWith('sim-1', 's-man');
	});

	it('快照列表加载失败:显示错误与重试,不显示「还没有快照」', async () => {
		snapshots.mockRejectedValueOnce(apiErr(404, 'world_not_found')).mockResolvedValue({ snapshots: SNAPS });
		const { container } = render(Worlds);
		await settle();
		await openSnaps(container);
		expect(document.body.textContent).toContain('世界不存在');
		expect(document.body.textContent).not.toContain('还没有快照');
		await fireEvent.click(inBody('重试')!);
		await settle();
		expect(document.body.textContent).toContain('2026-10-25T09:00:00Z');
	});
});

describe('/worlds ws 事件', () => {
	beforeEach(() => {
		list.mockReset().mockResolvedValue(DATA);
	});

	it.each([
		['world_changed', { statements: 3 }],
		['snapshot_created', { world: 'sim-1', id: 's1' }],
		['world_rolled_back', { world: 'sim-1', snapshot_id: 's1' }],
		['statement_added', {}]
	] as const)('收到 %s 事件时重取列表', async (type, payload) => {
		render(Worlds);
		await settle();
		const before = list.mock.calls.length;
		lastWsEvent.set({ type, world: 'sim-1', payload });
		await settle();
		expect(list.mock.calls.length).toBeGreaterThan(before);
	});

	it('别的世界的消息也触发重取（列表行上的计数属于各个世界）', async () => {
		render(Worlds);
		await settle();
		const before = list.mock.calls.length;
		lastWsEvent.set({ type: 'world_changed', world: 'whatever', payload: {} });
		await settle();
		expect(list.mock.calls.length).toBeGreaterThan(before);
	});

	it('recall(纯读)不触发重取', async () => {
		render(Worlds);
		await settle();
		const before = list.mock.calls.length;
		lastWsEvent.set({ type: 'recall', world: 'default', payload: {} });
		await settle();
		expect(list.mock.calls.length).toBe(before);
	});
});

// ── W3:回滚到快照、清理回滚归档 ──────────────────────────────────────────
const byLabel = (root: ParentNode, label: string) =>
	[...root.querySelectorAll('button')].find((b) => b.getAttribute('aria-label') === label) as
		| HTMLButtonElement
		| undefined;

describe('/worlds 回滚到快照', () => {
	const reload = vi.fn();
	beforeEach(() => {
		list.mockReset().mockResolvedValue(DATA);
		snapshots.mockReset().mockResolvedValue({ snapshots: SNAPS });
		rollback.mockReset();
		removeSnapshot.mockReset();
		reload.mockReset();
		toastError.mockReset();
		toastSuccess.mockReset();
		vi.stubGlobal('location', { ...window.location, reload });
	});
	afterEach(() => {
		vi.unstubAllGlobals();
	});

	const openRollback = async (container: HTMLElement, world: string, snap = SNAPS[1]) => {
		await fireEvent.click(byLabel(container, `快照 ${world}`)!);
		await settle();
		await fireEvent.click(byLabel(document.body, `回滚到快照 ${snap.created_at}`)!);
		await settle();
	};
	const confirmInput = () =>
		document.body.querySelector('input[name="world-rollback-confirm"]') as HTMLInputElement;

	it('每个快照行都有「回滚到此」;点开只弹确认框,显示丢弃后续与归档说明,不直接回滚', async () => {
		const { container } = render(Worlds);
		await settle();
		await fireEvent.click(byLabel(container, '快照 sim-1')!);
		await settle();
		const rows = [...document.body.querySelectorAll('button')].filter(
			(b) => b.textContent?.trim() === '回滚到此'
		);
		expect(rows.length).toBe(2);
		await fireEvent.click(byLabel(document.body, `回滚到快照 ${SNAPS[1].created_at}`)!);
		await settle();
		const text = document.body.textContent ?? '';
		expect(text).toContain('回滚世界 sim-1');
		expect(text).toContain(SNAPS[1].created_at);
		expect(text).toContain(
			'该世界在这份快照之后写入的所有记忆都会被丢弃。当前的库会改名保留在该世界的 archive 目录，可手动恢复。'
		);
		expect(rollback).not.toHaveBeenCalled();
	});

	it('文件缺失的快照不给「回滚到此」(后端必回 snapshot_corrupt)', async () => {
		snapshots.mockReset().mockResolvedValue({
			snapshots: [{ ...SNAPS[0], size_bytes: 0, missing: true }, SNAPS[1]]
		});
		const { container } = render(Worlds);
		await settle();
		await fireEvent.click(byLabel(container, '快照 sim-1')!);
		await settle();
		expect(byLabel(document.body, `回滚到快照 ${SNAPS[0].created_at}`)).toBeUndefined();
		expect(byLabel(document.body, `回滚到快照 ${SNAPS[1].created_at}`)).toBeDefined();
	});

	it('手输世界名完全一致之前确认键禁用;一致后才调用 rollback(name, snapshotId, confirm)', async () => {
		rollback.mockResolvedValue({ world: 'sim-1', snapshot_id: 's-man', archived: true, reopened: false });
		const { container } = render(Worlds);
		await settle();
		await openRollback(container, 'sim-1');

		expect(inBody('确认回滚')!.disabled).toBe(true);
		await fireEvent.click(inBody('确认回滚')!);
		expect(rollback).not.toHaveBeenCalled();

		for (const wrong of ['sim', 'sim-1 ', 'SIM-1', 'default']) {
			await fireEvent.input(confirmInput(), { target: { value: wrong } });
			await tick();
			expect(inBody('确认回滚')!.disabled, wrong).toBe(true);
		}

		await fireEvent.input(confirmInput(), { target: { value: 'sim-1' } });
		await tick();
		expect(inBody('确认回滚')!.disabled).toBe(false);
		await fireEvent.click(inBody('确认回滚')!);
		await settle();
		expect(rollback).toHaveBeenCalledTimes(1);
		expect(rollback).toHaveBeenCalledWith('sim-1', 's-man', 'sim-1');
	});

	it('输入过确认名后关掉抽屉，重新打开时输入框为空、确认键禁用', async () => {
		const { container } = render(Worlds);
		await settle();
		await openRollback(container, 'sim-1');
		await fireEvent.input(confirmInput(), { target: { value: 'sim-1' } });
		await tick();
		expect(inBody('确认回滚')!.disabled).toBe(false);
		// 用回滚抽屉自带的关闭键(✕)关掉
		const dlg = [...document.body.querySelectorAll('[role="dialog"]')].find((d) =>
			d.textContent?.includes('回滚世界 sim-1')
		)!;
		await fireEvent.click(button(dlg as HTMLElement, '✕')!);
		await settle();
		expect(confirmInput()).toBeNull();

		await fireEvent.click(byLabel(document.body, `回滚到快照 ${SNAPS[1].created_at}`)!);
		await settle();
		expect(confirmInput().value).toBe('');
		expect(inBody('确认回滚')!.disabled).toBe(true);
		expect(rollback).not.toHaveBeenCalled();
	});

	it('函数级守卫:绕过按钮的 disabled 也不会在名称不符时调用 rollback', async () => {
		const { container } = render(Worlds);
		await settle();
		await openRollback(container, 'sim-1');
		await fireEvent.input(confirmInput(), { target: { value: 'sim' } });
		await tick();
		const btn = inBody('确认回滚')!;
		btn.disabled = false;
		await fireEvent.click(btn);
		await settle();
		expect(rollback).not.toHaveBeenCalled();
	});

	it('回滚开着的世界成功：提示会重建引擎；成功后不整页刷新（引擎在服务端重建）', async () => {
		rollback.mockResolvedValue({ world: 'default', snapshot_id: 's-man', archived: true, reopened: true });
		const { container } = render(Worlds);
		await settle();
		await openRollback(container, 'default');
		expect(document.body.textContent).toContain('这个世界现在开着');
		await fireEvent.input(confirmInput(), { target: { value: 'default' } });
		await tick();
		await fireEvent.click(inBody('确认回滚')!);
		await settle();
		expect(rollback).toHaveBeenCalledWith('default', 's-man', 'default');
		expect(reload).not.toHaveBeenCalled();
		expect(toastSuccess).toHaveBeenCalledTimes(1);
	});

	it('回滚没开着的世界成功:toast、关确认框、重取列表,不整页刷新', async () => {
		rollback.mockResolvedValue({ world: 'sim-1', snapshot_id: 's-man', archived: true, reopened: false });
		const { container } = render(Worlds);
		await settle();
		const before = list.mock.calls.length;
		await openRollback(container, 'sim-1');
		await fireEvent.input(confirmInput(), { target: { value: 'sim-1' } });
		await tick();
		await fireEvent.click(inBody('确认回滚')!);
		await settle();
		expect(reload).not.toHaveBeenCalled();
		expect(list.mock.calls.length).toBeGreaterThan(before);
		expect(toastSuccess).toHaveBeenCalledTimes(1);
		expect(String(toastSuccess.mock.calls[0][0])).toContain('sim-1');
		expect(confirmInput()).toBeNull(); // 确认框已关
	});

	it('请求在途:确认键、各行「回滚到此」与「删除」都禁用,不能重复提交', async () => {
		let resolve!: (v: unknown) => void;
		rollback.mockReturnValue(new Promise((r) => (resolve = r)));
		const { container } = render(Worlds);
		await settle();
		await openRollback(container, 'sim-1');
		await fireEvent.input(confirmInput(), { target: { value: 'sim-1' } });
		await tick();
		await fireEvent.click(inBody('确认回滚')!);
		await settle();
		expect(inBody('确认回滚')!.disabled).toBe(true);
		expect(document.body.textContent).toContain('请不要关闭页面');
		const rowButtons = [...document.body.querySelectorAll('button')].filter((b) => {
			const l = b.getAttribute('aria-label') ?? '';
			return l.startsWith('回滚到快照') || l.startsWith('删除快照');
		});
		expect(rowButtons.length).toBe(4);
		expect(rowButtons.every((b) => b.disabled)).toBe(true);
		await fireEvent.click(inBody('确认回滚')!);
		expect(rollback).toHaveBeenCalledTimes(1);
		resolve({ world: 'sim-1', snapshot_id: 's-man', archived: true, reopened: false });
		await settle();
	});

	it('失败:错误码经 worldErrorMessage 出中文 toast,不刷新页面,确认键恢复可点', async () => {
		rollback.mockRejectedValue(apiErr(409, 'snapshot_corrupt'));
		const { container } = render(Worlds);
		await settle();
		await openRollback(container, 'sim-1');
		await fireEvent.input(confirmInput(), { target: { value: 'sim-1' } });
		await tick();
		await fireEvent.click(inBody('确认回滚')!);
		await settle();
		expect(toastError).toHaveBeenCalledWith('快照文件损坏或缺失，未做任何改动');
		expect(reload).not.toHaveBeenCalled();
		expect(inBody('确认回滚')!.disabled).toBe(false);
	});

	it('已回滚但引擎重开失败(rollback_reopen_failed):toast 后整页刷新,确认键保持禁用', async () => {
		rollback.mockRejectedValue(apiErr(500, 'rollback_reopen_failed'));
		const { container } = render(Worlds);
		await settle();
		await openRollback(container, 'default');
		await fireEvent.input(confirmInput(), { target: { value: 'default' } });
		await tick();
		await fireEvent.click(inBody('确认回滚')!);
		await settle();
		expect(toastError).toHaveBeenCalledWith(
			'已回滚到快照，但引擎重开失败；页面即将刷新，下一次写入、召回或手动 tick 时会自动重建，仍异常请查看服务日志'
		);
		expect(reload).toHaveBeenCalledTimes(1);
		// 重载前的空档里按钮不复位,免得再回滚一次(会把恢复后的库也归档一份)
		expect(inBody('确认回滚')!.disabled).toBe(true);
	});

	it('回滚被拒(ingest_busy)不刷新页面', async () => {
		rollback.mockRejectedValue(apiErr(409, 'ingest_busy'));
		const { container } = render(Worlds);
		await settle();
		await openRollback(container, 'default');
		await fireEvent.input(confirmInput(), { target: { value: 'default' } });
		await tick();
		await fireEvent.click(inBody('确认回滚')!);
		await settle();
		expect(toastError).toHaveBeenCalledWith(worldErrorMessage('ingest_busy'));
		expect(reload).not.toHaveBeenCalled();
	});
});

describe('/worlds 回滚归档', () => {
	const WITH_ARCHIVES = {
		...DATA,
		worlds: [DATA.worlds[0], { ...DATA.worlds[1], archives: 2, archive_bytes: 1536 }]
	};
	beforeEach(() => {
		list.mockReset().mockResolvedValue(WITH_ARCHIVES);
		purgeArchives.mockReset();
		toastError.mockReset();
		toastSuccess.mockReset();
	});
	const purgeInput = () =>
		document.body.querySelector('input[name="world-purge-confirm"]') as HTMLInputElement;

	it('archives 为 0 时不显示归档行与「清理归档」', async () => {
		list.mockReset().mockResolvedValue(DATA);
		const { container } = render(Worlds);
		await settle();
		expect(button(container, '清理归档')).toBeUndefined();
		expect(container.textContent).not.toMatch(/归档 \d+ 份/);
	});

	it('archives > 0 时只在那一行显示「归档 N 份 · 大小」与「清理归档」', async () => {
		const { container } = render(Worlds);
		await settle();
		expect(container.textContent).toContain('归档 2 份 · 1.5 KB');
		expect(container.textContent?.match(/归档 \d+ 份/g)?.length).toBe(1);
		const btns = [...container.querySelectorAll('button')].filter(
			(b) => b.textContent?.trim() === '清理归档'
		);
		expect(btns.length).toBe(1);
		expect(btns[0].getAttribute('aria-label')).toBe('清理归档 sim-1');
	});

	it('手输世界名一致后才调用 purgeArchives(name, confirm);成功后 toast 并重取列表', async () => {
		purgeArchives.mockResolvedValue({ deleted: 2 });
		const { container } = render(Worlds);
		await settle();
		const before = list.mock.calls.length;
		await fireEvent.click(button(container, '清理归档')!);
		await settle();
		expect(document.body.textContent).toContain('清理 sim-1 的回滚归档');
		expect(document.body.textContent).toContain('将永久删除世界「sim-1」的 2 份回滚归档（1.5 KB）');

		expect(inBody('确认清理')!.disabled).toBe(true);
		await fireEvent.click(inBody('确认清理')!);
		expect(purgeArchives).not.toHaveBeenCalled();

		await fireEvent.input(purgeInput(), { target: { value: 'sim' } });
		await tick();
		expect(inBody('确认清理')!.disabled).toBe(true);
		await fireEvent.input(purgeInput(), { target: { value: 'sim-1' } });
		await tick();
		expect(inBody('确认清理')!.disabled).toBe(false);
		await fireEvent.click(inBody('确认清理')!);
		await settle();
		expect(purgeArchives).toHaveBeenCalledWith('sim-1', 'sim-1');
		expect(toastSuccess).toHaveBeenCalledTimes(1);
		expect(String(toastSuccess.mock.calls[0][0])).toContain('2');
		expect(list.mock.calls.length).toBeGreaterThan(before);
		expect(purgeInput()).toBeNull();
	});

	it('函数级守卫:绕过 disabled 也不会在名称不符时调用 purgeArchives', async () => {
		const { container } = render(Worlds);
		await settle();
		await fireEvent.click(button(container, '清理归档')!);
		await settle();
		const btn = inBody('确认清理')!;
		btn.disabled = false;
		await fireEvent.click(btn);
		await settle();
		expect(purgeArchives).not.toHaveBeenCalled();
	});

	it('请求在途时确认键禁用,不能重复提交', async () => {
		let resolve!: (v: unknown) => void;
		purgeArchives.mockReturnValue(new Promise((r) => (resolve = r)));
		const { container } = render(Worlds);
		await settle();
		await fireEvent.click(button(container, '清理归档')!);
		await settle();
		await fireEvent.input(purgeInput(), { target: { value: 'sim-1' } });
		await tick();
		await fireEvent.click(inBody('确认清理')!);
		await settle();
		expect(inBody('确认清理')!.disabled).toBe(true);
		await fireEvent.click(inBody('确认清理')!);
		expect(purgeArchives).toHaveBeenCalledTimes(1);
		resolve({ deleted: 2 });
		await settle();
	});

	it('失败:错误码经 worldErrorMessage 出中文 toast', async () => {
		purgeArchives.mockRejectedValue(apiErr(400, 'confirm_mismatch'));
		const { container } = render(Worlds);
		await settle();
		await fireEvent.click(button(container, '清理归档')!);
		await settle();
		await fireEvent.input(purgeInput(), { target: { value: 'sim-1' } });
		await tick();
		await fireEvent.click(inBody('确认清理')!);
		await settle();
		expect(toastError).toHaveBeenCalledWith('输入的名称与世界名不一致');
		expect(inBody('确认清理')!.disabled).toBe(false);
	});
});

// ── W4:自动快照设置、快照元数据、失败提示 ────────────────────────────────
const checkbox = (root: ParentNode, name: string) =>
	root.querySelector(`input[type="checkbox"][name="${name}"]`) as HTMLInputElement;
const field = (root: ParentNode, name: string) =>
	root.querySelector(`input[name="${name}"]`) as HTMLInputElement;

// 世界行(Card)= 只含这一个世界「设置自动快照」按钮的最外层祖先。
const worldRow = (root: HTMLElement, name: string) => {
	let el: HTMLElement | null | undefined = byLabel(root, `设置自动快照 ${name}`);
	const settingButtons = (n: Element) =>
		[...n.querySelectorAll('button')].filter((b) =>
			b.getAttribute('aria-label')?.startsWith('设置自动快照 ')
		).length;
	while (el?.parentElement && el.parentElement !== root && settingButtons(el.parentElement) === 1)
		el = el.parentElement;
	return el ?? undefined;
};
const autoLine = (row: HTMLElement) =>
	[...row.querySelectorAll('p')]
		.map((p) => p.textContent?.trim() ?? '')
		.filter((t) => t.startsWith('自动快照：'));

describe('/worlds 自动快照设置', () => {
	beforeEach(() => {
		list.mockReset().mockResolvedValue(DATA);
		setAutoSnapshot.mockReset();
		toastError.mockReset();
		toastSuccess.mockReset();
	});

	it('每个世界行显示自己的自动快照设置', async () => {
		list.mockReset().mockResolvedValue({
			...DATA,
			worlds: [
				DATA.worlds[0],
				DATA.worlds[1],
				{ ...DATA.worlds[1], name: 'sim-2', tenant: 'sim-2',
				  auto_snapshot: { on_session: true, interval_hours: 24 } },
				{ ...DATA.worlds[1], name: 'sim-3', tenant: 'sim-3',
				  auto_snapshot: { on_session: false, interval_hours: 0 } }
			]
		});
		const { container } = render(Worlds);
		await settle();
		const expected: Record<string, string> = {
			default: '自动快照：每 24 小时',
			'sim-1': '自动快照：按会话',
			'sim-2': '自动快照：按会话 · 每 24 小时',
			'sim-3': '自动快照：关闭'
		};
		for (const [n, line] of Object.entries(expected)) {
			// 只在该世界自己那一行里找,别的行的「按会话 · 每 24 小时」不能替 sim-1 过关
			const row = worldRow(container, n);
			expect(row, n).toBeDefined();
			expect(autoLine(row!), n).toEqual([line]); // 恰好一行,且是这一行自己的设置
		}
	});

	it('「设置」打开表单，预填当前值；保存时 PUT 新配置，成功后关表单并重取列表', async () => {
		setAutoSnapshot.mockResolvedValue({
			world: 'default', auto_snapshot: { on_session: true, interval_hours: 6 }
		});
		const { container } = render(Worlds);
		await settle();
		const before = list.mock.calls.length;
		await fireEvent.click(byLabel(container, '设置自动快照 default')!);
		await settle();
		expect(document.body.textContent).toContain('default 的自动快照');
		const on = checkbox(document.body, 'auto-snapshot-on-session');
		const hours = field(document.body, 'auto-snapshot-interval');
		expect(on.checked).toBe(false);
		expect(hours.value).toBe('24');
		expect(setAutoSnapshot).not.toHaveBeenCalled();

		await fireEvent.click(on);
		await fireEvent.input(hours, { target: { value: '6' } });
		await tick();
		await fireEvent.click(inBody('保存')!);
		await settle();
		expect(setAutoSnapshot).toHaveBeenCalledTimes(1);
		expect(setAutoSnapshot).toHaveBeenCalledWith('default', { on_session: true, interval_hours: 6 });
		expect(toastSuccess).toHaveBeenCalledTimes(1);
		expect(list.mock.calls.length).toBeGreaterThan(before);
		expect(field(document.body, 'auto-snapshot-interval')).toBeNull(); // 表单已关
	});

	it('间隔不在 0–8760 的整数内：显示错误、保存禁用，绕过 disabled 也不调用 PUT', async () => {
		const { container } = render(Worlds);
		await settle();
		await fireEvent.click(byLabel(container, '设置自动快照 sim-1')!);
		await settle();
		const hours = field(document.body, 'auto-snapshot-interval');
		for (const bad of ['8761', '-1', '1.5', '']) {
			await fireEvent.input(hours, { target: { value: bad } });
			await tick();
			expect(inBody('保存')!.disabled, bad).toBe(true);
		}
		expect(document.body.textContent).toContain('自动快照设置无效（间隔须为 0–8760 小时）');
		const btn = inBody('保存')!;
		btn.disabled = false;
		await fireEvent.click(btn);
		await settle();
		expect(setAutoSnapshot).not.toHaveBeenCalled();
	});

	it('后端拒绝（invalid_auto_snapshot）时 toast 中文提示，表单不关', async () => {
		setAutoSnapshot.mockRejectedValue(apiErr(400, 'invalid_auto_snapshot'));
		const { container } = render(Worlds);
		await settle();
		await fireEvent.click(byLabel(container, '设置自动快照 sim-1')!);
		await settle();
		await fireEvent.click(inBody('保存')!);
		await settle();
		expect(setAutoSnapshot).toHaveBeenCalledWith('sim-1', { on_session: true, interval_hours: 0 });
		expect(toastError).toHaveBeenCalledWith('自动快照设置无效（间隔须为 0–8760 小时）');
		expect(field(document.body, 'auto-snapshot-interval')).not.toBeNull();
		expect(inBody('保存')!.disabled).toBe(false);
	});
});

describe('/worlds 新建世界带自动快照配置', () => {
	beforeEach(() => {
		list.mockReset().mockResolvedValue(DATA);
		create.mockReset();
	});

	it('初值取默认世界当前的设置；不改就不发 auto_snapshot（后端继承）', async () => {
		detail.mockReset().mockResolvedValue({
			...DEFAULT_DETAIL,
			settings: { background: { auto_snapshot: { on_session: false, interval_hours: 24 } } }
		});
		create.mockResolvedValue({ ...DATA.worlds[1], name: 'sim-2' });
		const { container } = render(Worlds);
		await settle();
		expect(detail).toHaveBeenCalledWith('default');
		expect(checkbox(container, 'world-new-on-session').checked).toBe(false);
		expect(field(container, 'world-new-interval').value).toBe('24');
		await fireEvent.input(field(container, 'world-name'), { target: { value: 'sim-2' } });
		await tick();
		await fireEvent.click(button(container, '新建世界')!);
		await settle();
		expect(create).toHaveBeenCalledWith('sim-2', '', undefined, {});
	});

	it('改过之后随新建一起提交；成功后表单回到默认世界的设置', async () => {
		create.mockResolvedValue({ ...DATA.worlds[1], name: 'sim-2' });
		const { container } = render(Worlds);
		await settle();
		const on = checkbox(container, 'world-new-on-session');
		const hours = field(container, 'world-new-interval');
		expect(on.checked).toBe(true);
		expect(hours.value).toBe('0');
		await fireEvent.input(field(container, 'world-name'), { target: { value: 'sim-2' } });
		await fireEvent.click(on);
		await fireEvent.input(hours, { target: { value: '12' } });
		await tick();
		await fireEvent.click(button(container, '新建世界')!);
		await settle();
		expect(create).toHaveBeenCalledWith('sim-2', '', { on_session: false, interval_hours: 12 }, {});
		expect(checkbox(container, 'world-new-on-session').checked).toBe(true);
		expect(field(container, 'world-new-interval').value).toBe('0');
	});

	it('标签随新建提交；写错的标签禁用新建', async () => {
		create.mockResolvedValue({ ...DATA.worlds[1], name: 'sim-2' });
		const { container } = render(Worlds);
		await settle();
		await fireEvent.input(field(container, 'world-name'), { target: { value: 'sim-2' } });
		await fireEvent.input(field(container, 'world-labels'), { target: { value: 'env' } });
		await tick();
		expect(button(container, '新建世界')!.disabled).toBe(true);
		await fireEvent.input(field(container, 'world-labels'), { target: { value: 'env=test, team=x' } });
		await tick();
		await fireEvent.click(button(container, '新建世界')!);
		await settle();
		expect(create).toHaveBeenCalledWith('sim-2', '', undefined, { env: 'test', team: 'x' });
	});

	it('间隔不合法时新建禁用，且不调用 create', async () => {
		const { container } = render(Worlds);
		await settle();
		await fireEvent.input(field(container, 'world-name'), { target: { value: 'sim-2' } });
		await fireEvent.input(field(container, 'world-new-interval'), { target: { value: '9000' } });
		await tick();
		const btn = button(container, '新建世界')!;
		expect(btn.disabled).toBe(true);
		expect(container.textContent).toContain('自动快照设置无效（间隔须为 0–8760 小时）');
		btn.disabled = false;
		await fireEvent.click(btn);
		await settle();
		expect(create).not.toHaveBeenCalled();
	});
});

describe('/worlds 快照行的 W4 元数据', () => {
	const SESSION_SNAP = {
		id: 's-sess', kind: 'auto', reason: 'session', note: '', created_at: '2026-10-27T08:59:59Z',
		world_time: '', size_bytes: 2048, sessions_complete: 4, session_id: 'abcdef1234567890'
	};
	beforeEach(() => {
		list.mockReset().mockResolvedValue(DATA);
		snapshots.mockReset().mockResolvedValue({ snapshots: [SESSION_SNAP, ...SNAPS] });
		removeSnapshot.mockReset();
		rollback.mockReset();
	});

	const openSnaps = async (container: HTMLElement) => {
		await fireEvent.click(byLabel(container, '快照 sim-1')!);
		await settle();
	};

	it('reason 显示为「会话开始前」「定时」「手动」', async () => {
		const { container } = render(Worlds);
		await settle();
		await openSnaps(container);
		const items = [...document.body.querySelectorAll('li')];
		expect(items.length).toBe(3);
		expect(items[0].textContent).toContain('会话开始前');
		expect(items[1].textContent).toContain('定时');
		expect(items[2].textContent).toContain('手动');
	});

	it('sessions_complete 有值时显示数字，为 null 时显示 —', async () => {
		const { container } = render(Worlds);
		await settle();
		await openSnaps(container);
		const items = [...document.body.querySelectorAll('li')];
		expect(items[0].textContent).toContain('会话数 4');
		expect(items[1].textContent).toContain('会话数 —');
		expect(items[2].textContent).toContain('会话数 3');
	});

	it('「会话开始前」的快照显示 session_id 前 8 位，悬停显示全文；其他 reason 不显示', async () => {
		const { container } = render(Worlds);
		await settle();
		await openSnaps(container);
		const tag = document.body.querySelector(`[title="${SESSION_SNAP.session_id}"]`);
		expect(tag).not.toBeNull();
		expect(tag!.textContent).toContain('abcdef12');
		expect(tag!.textContent).not.toContain('abcdef123');
		const items = [...document.body.querySelectorAll('li')];
		expect(items[1].textContent).not.toMatch(/会话 [0-9a-f]{8}/);
	});

	it('删除快照的确认框用 reason 文案描述快照，而不是 kind', async () => {
		const { container } = render(Worlds);
		await settle();
		await openSnaps(container);
		await fireEvent.click(byLabel(document.body, `删除快照 ${SESSION_SNAP.created_at}`)!);
		await settle();
		const dialog = [...document.body.querySelectorAll('[role="dialog"]')].find((d) =>
			d.textContent?.includes('删除这份快照？')
		)!;
		expect(dialog.textContent).toContain(`${SESSION_SNAP.created_at} 的「会话开始前」快照`);
		expect(dialog.textContent).not.toContain('auto');
	});
});

describe('/worlds 自动快照失败提示', () => {
	beforeEach(() => {
		list.mockReset();
	});

	it('failures > 0 时页面顶部显示次数、最近一次的时间与错误', async () => {
		list.mockResolvedValue({
			...DATA,
			auto_snapshot_status: {
				failures: 2, last_error: 'insufficient_disk', last_error_at: '2026-10-28T01:00:00Z'
			}
		});
		const { container } = render(Worlds);
		await settle();
		const status = container.querySelector('[role="status"]');
		expect(status).not.toBeNull();
		expect(status!.textContent?.trim()).toBe(
			'自动快照失败 2 次，最近一次：2026-10-28T01:00:00Z insufficient_disk；摄入未受影响，这几次没拍成的快照不会作为回滚点'
		);
	});

	it('failures 为 0 时不显示', async () => {
		list.mockResolvedValue(DATA);
		const { container } = render(Worlds);
		await settle();
		expect(container.textContent).not.toContain('自动快照失败');
	});
});

describe('/worlds 重新确认端点（裁定 R53）', () => {
	beforeEach(() => {
		list.mockReset().mockResolvedValue(DATA);
		detail.mockReset().mockResolvedValue(DEFAULT_DETAIL);
		reconfirm.mockReset().mockResolvedValue({ ...DATA.worlds[1] });
		toastError.mockReset();
		toastSuccess.mockReset();
		noteDefaultWorld('default');
	});

	const rowButton = (label: string) =>
		[...document.body.querySelectorAll('button')].find(
			(b) => b.getAttribute('aria-label') === label
		) as HTMLButtonElement | undefined;

	it('非默认世界有按钮，默认世界没有', async () => {
		render(Worlds);
		await settle();
		expect(rowButton('重新确认端点 sim-1')).toBeTruthy();
		expect(rowButton('重新确认端点 default')).toBeUndefined();
	});

	it('经确认框点名世界、说明复制默认配置里的供应商地址，确认后才调接口并刷新', async () => {
		render(Worlds);
		await settle();
		await fireEvent.click(rowButton('重新确认端点 sim-1')!);
		await settle();
		expect(reconfirm).not.toHaveBeenCalled();
		expect(document.body.textContent).toContain('重新确认「sim-1」的端点？');
		expect(document.body.textContent).toContain('默认配置里各供应商当前的地址');
		const calls = list.mock.calls.length;
		await fireEvent.click(inBody('确认')!);
		await settle();
		expect(reconfirm).toHaveBeenCalledWith('sim-1');
		expect(toastSuccess).toHaveBeenCalledWith('已为「sim-1」重新确认端点');
		expect(list.mock.calls.length).toBeGreaterThan(calls);
	});

	it('失败时给中文提示', async () => {
		reconfirm.mockRejectedValue(apiErr(404, 'world_not_found'));
		render(Worlds);
		await settle();
		await fireEvent.click(rowButton('重新确认端点 sim-1')!);
		await settle();
		await fireEvent.click(inBody('确认')!);
		await settle();
		expect(toastError).toHaveBeenCalledWith('世界不存在');
	});
});
