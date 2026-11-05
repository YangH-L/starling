import { describe, it, expect, vi, beforeEach } from 'vitest';
import { api, ApiError, worldPath } from './api';
import { noteDefaultWorld } from './world.svelte';
import * as tok from './token';

beforeEach(() => {
	vi.spyOn(tok, 'getToken').mockReturnValue('secret');
	globalThis.fetch = vi.fn(
		async () => new Response(JSON.stringify({ ok: 1 }), { status: 200 })
	) as unknown as typeof fetch;
});

describe('api', () => {
	it('attaches bearer token', async () => {
		await api.get('/api/overview');
		const [, init] = (globalThis.fetch as any).mock.calls[0];
		expect(new Headers(init.headers).get('Authorization')).toBe('Bearer secret');
	});
	it('honors per-request timeoutMs (LLM 路径放宽,默认 15s 不变)', async () => {
		vi.useFakeTimers();
		// fetch 挂起直到 signal abort——模拟慢后端。
		globalThis.fetch = vi.fn(
			(_p: string, init: RequestInit) =>
				new Promise((_resolve, reject) => {
					init.signal?.addEventListener('abort', () =>
						reject(new DOMException('aborted', 'AbortError'))
					);
				})
		) as any;
		const pending = api.post('/api/remember', { text: 'x' }, { timeoutMs: 120_000 });
		const caught = pending.catch((e) => e as ApiError);
		await vi.advanceTimersByTimeAsync(15_000); // 默认超时点:不应中断
		await vi.advanceTimersByTimeAsync(104_000); // 119s:仍未中断
		expect((globalThis.fetch as any).mock.calls.length).toBe(1);
		await vi.advanceTimersByTimeAsync(2_000); // 越过 120s → 中断
		const err = (await caught) as ApiError;
		expect(err).toBeInstanceOf(ApiError);
		expect(String(err.message)).toContain('120');
		vi.useRealTimers();
	});
	it('throws ApiError on non-ok with status + path', async () => {
		globalThis.fetch = vi.fn(
			async () => new Response(JSON.stringify({ detail: 'nope' }), { status: 401 })
		) as any;
		let err!: ApiError;
		await api.get('/api/overview').catch((e) => { err = e as ApiError; });
		expect(err).toBeInstanceOf(ApiError);
		expect(err.status).toBe(401);
		expect(err.isAuth).toBe(true);
		expect(err.path).toBe('/api/overview');
		expect(String(err.message)).toContain('nope');
	});
});

describe('worldPath', () => {
	beforeEach(() => noteDefaultWorld('default'));

	it('默认世界保持旧路径,其他世界加 /api/w/<世界>/ 前缀', () => {
		expect(worldPath('/api/statements', 'default')).toBe('/api/statements');
		expect(worldPath('/api/statements?q=1', 'Run-A')).toBe('/api/w/Run-A/statements?q=1');
		expect(worldPath('/api/statements', 'a b')).toBe('/api/w/a%20b/statements');
	});
	it('默认世界不叫 default 时,按注册表的默认世界名判断', () => {
		noteDefaultWorld('main');
		expect(worldPath('/api/statements', 'main')).toBe('/api/statements');
		expect(worldPath('/api/statements', 'default')).toBe('/api/w/default/statements');
	});
	it('管理路由不加前缀', () => {
		for (const p of [
			'/api/worlds',
			'/api/worlds/x/close',
			'/api/worlds?offset=0',
			'/api/config',
			'/api/config/test',
			'/api/eval',
			'/health',
			'/api/ping'
		])
			expect(worldPath(p, 'Run-A')).toBe(p);
	});
	it('前缀相近但不是管理路由的路径仍按世界寻址', () => {
		expect(worldPath('/api/worldsx', 'Run-A')).toBe('/api/w/Run-A/worldsx');
		expect(worldPath('/api/evaluate', 'Run-A')).toBe('/api/w/Run-A/evaluate');
	});
});
