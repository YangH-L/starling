import { describe, it, expect, vi, beforeEach } from 'vitest';
import { tick } from 'svelte';
import { render, fireEvent } from '@testing-library/svelte';

// 这页的外部依赖：GET /api/queues(加载)与 POST /api/tick(手动 tick)。
const get = vi.fn();
const post = vi.fn();
vi.mock('$lib/api', async (orig) => {
	const real = await orig<typeof import('$lib/api')>();
	return {
		...real,
		api: { get: (...a: unknown[]) => get(...a), post: (...a: unknown[]) => post(...a), del: vi.fn() }
	};
});

const toastError = vi.fn();
const toastSuccess = vi.fn();
const toastWarning = vi.fn();
vi.mock('$lib/ui/toast', () => ({
	toast: {
		error: (...a: unknown[]) => toastError(...a),
		success: (...a: unknown[]) => toastSuccess(...a),
		warning: (...a: unknown[]) => toastWarning(...a),
		info: vi.fn()
	}
}));

import Queues from './+page.svelte';

const settle = async () => {
	await tick();
	await Promise.resolve();
	await Promise.resolve();
	await tick();
};

const QUEUES = { dispatch: { pending: 0 }, embedding_backlog: 3, vectors_by_status: { embedded: 1 } };
const TICK_BASE = { embedded: 0, fired: 0, stage_timings_ms: {}, stages_skipped: [] as string[] };

const tickButton = () =>
	[...document.body.querySelectorAll('button')].find((b) => b.textContent?.trim() === '手动 Tick') as
		| HTMLButtonElement
		| undefined;

describe('/queues 手动 tick（裁定 R59 N-4）', () => {
	beforeEach(() => {
		get.mockReset().mockResolvedValue(QUEUES);
		post.mockReset();
		toastError.mockReset();
		toastSuccess.mockReset();
		toastWarning.mockReset();
	});

	it('stages_skipped 含 embed：成功之外 toast.warning 指向重新确认端点', async () => {
		post.mockResolvedValue({ ...TICK_BASE, stages_skipped: ['embed'] });
		render(Queues);
		await settle();
		await fireEvent.click(tickButton()!);
		await settle();
		expect(toastSuccess).toHaveBeenCalledWith('Tick 发送成功');
		expect(toastWarning).toHaveBeenCalledWith(
			'嵌入端点失效，本轮跳过嵌入；到「世界」页重新确认端点'
		);
	});

	it('没跳过嵌入：不出 warning', async () => {
		post.mockResolvedValue({ ...TICK_BASE, stages_skipped: ['persona'] });
		render(Queues);
		await settle();
		await fireEvent.click(tickButton()!);
		await settle();
		expect(toastSuccess).toHaveBeenCalledWith('Tick 发送成功');
		expect(toastWarning).not.toHaveBeenCalled();
	});
});
