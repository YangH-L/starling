import { describe, it, expect, vi, beforeEach } from 'vitest';
import { tick } from 'svelte';
import { render, fireEvent } from '@testing-library/svelte';

// /api/config 是这页唯一的外部依赖。get 决定加载成败;post 是我们要证明「没被调用」的那个。
const get = vi.fn();
const post = vi.fn();
vi.mock('$lib/api', async (orig) => {
	const real = await orig<typeof import('$lib/api')>();
	return {
		...real, // worldPath 用真的:危险区的路径改写是被测行为
		// ApiError 用真的:worlds.ts 的 apiDetail 靠 instanceof 取错误码
		api: { get: (...a: unknown[]) => get(...a), post: (...a: unknown[]) => post(...a), del: vi.fn() }
	};
});

import Settings from './+page.svelte';
import { ApiError as RealApiError } from '$lib/api';
import { page } from '$app/state';
import { noteDefaultWorld } from '$lib/world.svelte';

const byText = (c: HTMLElement | HTMLBodyElement, t: string) =>
	[...c.querySelectorAll('button')].find((b) => b.textContent?.trim() === t) as
		| HTMLButtonElement
		| undefined;

const saveButton = (c: HTMLElement) => byText(c, '保存');

// 顺序有讲究:先 tick() 让挂载后的 $effect 真正跑起来(它才发出 api.get),再 flush
// 微任务让 promise 落地,最后一次 tick() 把状态变更刷进 DOM。先 flush 再 tick 是不行的
// —— 那时请求还没发出去。
const settle = async () => {
	await tick();
	await Promise.resolve();
	await Promise.resolve();
	await tick();
};

// embedding 绑定的 provider 必须带 dim:dim 那个 <Input> 是 bind:value 且有 '' fallback,
// undefined 会让 Svelte 5 抛 props_invalid_value(源码里那条 $effect 正是为此兜底的,
// 但它赶不在首次渲染之前)。这是既有行为,与本文件要测的加载门无关,故在 fixture 里避开。
const CONFIG = {
	providers: { p1: { provider: 'openai', model: 'm', base_url: 'http://x', dim: 1024 } },
	roles: { extraction: 'p1', embedding: 'p1', chat: 'p1' },
	gist_thresholds: { min_holders: 5 }
};

describe('设置页:配置加载门(数据丢失防线)', () => {
	beforeEach(() => {
		get.mockReset();
		post.mockReset();
	});

	// 这是本文件存在的理由。加载失败后页面各项是空的,若此时能保存,提交的就是从未加载过的
	// 初值:providers {} 对后端是 no-op(upsert)所以 key 安全,但 roles 走赋值 ——
	// {extraction:'',embedding:'',chat:''} 会解绑全部角色,后端随即热切换 set_llm({}) /
	// rebuild_embedder({}),抽取与嵌入当场停摆;gist_thresholds 送 {} 则重置为默认。
	// 最后 cfg.save() 写进 starling.json。整条链只能由一次 POST 触发,所以钉住「不 POST」。
	it('加载失败后点保存,绝不 POST /api/config', async () => {
		get.mockRejectedValue(new Error('401 unauthorized'));
		const { container } = render(Settings);
		await settle();

		const btn = saveButton(container);
		expect(btn, '保存按钮应当存在').toBeTruthy();
		expect(btn!.disabled, '加载失败时保存按钮必须禁用').toBe(true);

		await fireEvent.click(btn!);
		await settle();
		expect(post).not.toHaveBeenCalled();
	});

	// 两道防线必须分开测。上面那条只覆盖按钮的 disabled —— 实测证明:把 doSave 里的
	// 函数级守卫删掉、只留 disabled,本文件依然全绿,因为 fireEvent.click 对 disabled
	// 按钮根本不派发事件,守卫压根没被执行过。这里手动摘掉 disabled 再点,模拟「日后
	// 多出一条绕过按钮的调用路径」,让守卫本身也有独立断言力。
	it('函数级守卫:即便绕过按钮的 disabled 触发保存,也不 POST', async () => {
		get.mockRejectedValue(new Error('401 unauthorized'));
		const { container } = render(Settings);
		await settle();

		const btn = saveButton(container)!;
		expect(btn.disabled).toBe(true);
		btn.disabled = false; // 绕过第一道防线
		await fireEvent.click(btn);
		await settle();

		// 未加载时 embBaseline('') 必然 !== embKey(),故 onSaveClick 走的是「先弹确认框」
		// 那条分支 —— doSave 只在确认之后才被调用。不点完这一步,守卫根本没被触达
		// (这正是本条测试第一版假绿的原因)。真实的数据丢失路径也正是这条:
		// 用户点保存 → 看到一个莫名其妙的「确认更改 Embedder?」→ 点继续。
		// ConfirmDialog 走 bits-ui 的 Dialog.Portal,渲染在 document.body 而非 container 里。
		const confirm = byText(document.body, '继续保存');
		expect(confirm, '应当弹出确认框(未加载时 embChanged 恒为真)').toBeTruthy();
		await fireEvent.click(confirm!);
		await settle();
		expect(post, 'doSave 的 !loaded 守卫应当拦住它').not.toHaveBeenCalled();
	});

	it('加载失败时显示原因,并说明空态不等于「没配」', async () => {
		get.mockRejectedValue(new Error('401 unauthorized'));
		const { container } = render(Settings);
		await settle();

		const text = container.textContent ?? '';
		expect(text).toContain('配置加载失败');
		expect(text, '必须带上失败原因,而不是笼统一句失败').toContain('401 unauthorized');
		expect(text, '必须解释下方为空的原因,否则用户会读成「什么都没配」').toContain('不是真的没配');
	});

	it('Access 卡片在加载失败时仍然可见 —— 它是 token 出错时的唯一自救途径', async () => {
		get.mockRejectedValue(new Error('401 unauthorized'));
		const { container } = render(Settings);
		await settle();
		expect(container.textContent ?? '').toContain('Access');
	});

	it('加载成功后保存按钮可用,且 POST 得出去', async () => {
		get.mockResolvedValue(CONFIG);
		post.mockResolvedValue(CONFIG);
		const { container } = render(Settings);
		await settle();

		const btn = saveButton(container);
		expect(btn!.disabled, '加载成功后不该再禁用').toBe(false);
		await fireEvent.click(btn!);
		await settle();
		expect(post).toHaveBeenCalled();
		expect(post.mock.calls[0][0]).toBe('/api/config');
	});

	it('重试成功后解除封锁(一次失败不该永久锁死这页)', async () => {
		// 只让 /api/config 先失败一次;危险区的世界详情请求也走 get,按路径区分。
		let cfgCalls = 0;
		get.mockImplementation(async (path: string) => {
			if (path !== '/api/config') return { name: 'default', tenant: 'default' };
			if (cfgCalls++ === 0) throw new Error('boom');
			return CONFIG;
		});
		const { container } = render(Settings);
		await settle();
		expect(saveButton(container)!.disabled).toBe(true);

		const retry = [...container.querySelectorAll('button')].find(
			(b) => b.textContent?.trim() === '重试'
		);
		expect(retry, '加载失败时应当给一个重试按钮').toBeTruthy();
		await fireEvent.click(retry!);
		await settle();
		expect(saveButton(container)!.disabled, '重试成功后应解除禁用').toBe(false);
	});
});

// 危险区是物理删除且不可恢复:钉住「手输租户名 + 确认框」两道闸,以及请求体形状
// (后端按 confirm === 当前租户名再校验一次)。
// (旧的危险区用例按默认世界租户 /api/config.tenant 写成,已由下方 R47 的用例取代。)

// ── 危险区:清空的是顶栏选中的世界(裁定 R47) ─────────────────────────────
describe('设置页:清空记忆随所选世界走', () => {
	const inBody = (t: string) => byText(document.body, t);
	const at = (w?: string) => {
		(page as { url: URL }).url = new URL(
			w ? `http://localhost/settings?world=${w}` : 'http://localhost/settings'
		);
	};
	const clearBtn = (c: HTMLElement) =>
		[...c.querySelectorAll('button')].find((b) => b.textContent?.includes('清空世界「')) as
			| HTMLButtonElement
			| undefined;
	const confirmInput = (c: HTMLElement) => c.querySelector('#clear-confirm') as HTMLInputElement;
	// /api/config 给默认世界的租户 'default-tenant';世界详情给各自的租户。
	const route = (detail: (path: string) => unknown) =>
		get.mockImplementation(async (path: string) => {
			if (path === '/api/config') return { ...CONFIG, tenant: 'default-tenant' };
			return detail(path);
		});

	beforeEach(() => {
		get.mockReset();
		post.mockReset();
		noteDefaultWorld('main');
		at();
	});

	it('确认串取自该世界的详情租户,清空请求走 /api/w/<world>/clear_memory,对话框点名世界', async () => {
		at('w1');
		route(() => ({ name: 'w1', tenant: 'w1-tenant' }));
		post.mockResolvedValue({ tenant: 'w1-tenant', total: 5, spool_discarded: 0 });
		const { container } = render(Settings);
		await settle();
		expect(get).toHaveBeenCalledWith('/api/worlds/w1');
		expect(container.textContent).toContain('输入租户名「w1-tenant」以确认');
		expect(clearBtn(container)!.textContent).toContain('清空世界「w1」');
		// 默认世界的租户名('default-tenant')不能放行 w1 的清空
		await fireEvent.input(confirmInput(container), { target: { value: 'default-tenant' } });
		await tick();
		expect(clearBtn(container)!.disabled).toBe(true);
		await fireEvent.input(confirmInput(container), { target: { value: 'w1-tenant' } });
		await tick();
		expect(clearBtn(container)!.disabled).toBe(false);
		await fireEvent.click(clearBtn(container)!);
		await settle();
		expect(document.body.textContent).toContain('将物理删除世界「w1」');
		await fireEvent.click(inBody('确认清空')!);
		await settle();
		expect(post).toHaveBeenCalledTimes(1);
		expect(post.mock.calls[0][0]).toBe('/api/w/w1/clear_memory');
		expect(post.mock.calls[0][1]).toEqual({ confirm: 'w1-tenant' });
	});

	it('默认世界:取默认世界的详情,走旧路径 /api/clear_memory', async () => {
		route(() => ({ name: 'main', tenant: 'main-tenant' }));
		post.mockResolvedValue({ tenant: 'main-tenant', total: 1, spool_discarded: 0 });
		const { container } = render(Settings);
		await settle();
		expect(get).toHaveBeenCalledWith('/api/worlds/main');
		await fireEvent.input(confirmInput(container), { target: { value: 'main-tenant' } });
		await tick();
		await fireEvent.click(clearBtn(container)!);
		await settle();
		await fireEvent.click(inBody('确认清空')!);
		await settle();
		expect(post.mock.calls[0][0]).toBe('/api/clear_memory');
	});

	it('详情加载失败:清空键与确认输入禁用,不回退到默认世界的租户,绕过 disabled 也不 POST', async () => {
		at('w1');
		// 真的 ApiError(404 world_not_found):走 worldsApi.detail → apiDetail → worldErrorMessage 的真实路径
		route(() => {
			throw new RealApiError(404, '/api/worlds/w1', '404 world_not_found');
		});
		const { container } = render(Settings);
		await settle();
		expect(container.textContent).toContain('没能取到世界「w1」的租户名');
		expect(container.textContent).toContain('世界不存在'); // 错误码已译成中文
		expect(container.textContent).not.toContain('default-tenant');
		expect(confirmInput(container).disabled).toBe(true);
		expect(clearBtn(container)!.disabled).toBe(true);
		const btn = clearBtn(container)!;
		btn.disabled = false; // 绕过第一道防线:确认框打不开 / 守卫拦住
		await fireEvent.click(btn);
		await settle();
		const ok = inBody('确认清空');
		if (ok) await fireEvent.click(ok);
		await settle();
		expect(post).not.toHaveBeenCalled();
	});
});
