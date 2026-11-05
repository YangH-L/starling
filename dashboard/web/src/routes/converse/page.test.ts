import { describe, it, expect, vi, beforeEach } from 'vitest';
import { tick } from 'svelte';
import { render, fireEvent } from '@testing-library/svelte';

// 对话页的外部依赖：GET /api/config(模型下拉)与 WS /ws/converse(streamConverse)。
vi.mock('$lib/api', async (orig) => {
	const real = await orig<typeof import('$lib/api')>();
	return { ...real, api: { get: vi.fn().mockResolvedValue({ providers: {} }), post: vi.fn(), del: vi.fn() } };
});

type Handlers = { onError: (code: string) => void };
const streamConverse = vi.fn();
vi.mock('$lib/ws', async (orig) => {
	const real = await orig<typeof import('$lib/ws')>();
	return { ...real, streamConverse: (...a: unknown[]) => streamConverse(...a) };
});

const toastError = vi.fn();
vi.mock('$lib/ui/toast', () => ({
	toast: { error: (...a: unknown[]) => toastError(...a), success: vi.fn(), warning: vi.fn(), info: vi.fn() }
}));

import Converse from './+page.svelte';
import { worldErrorMessage } from '$lib/worlds';

const settle = async () => {
	await tick();
	await Promise.resolve();
	await Promise.resolve();
	await tick();
};

async function sendAndFail(code: string) {
	streamConverse.mockImplementation((_body: unknown, handlers: Handlers) => handlers.onError(code));
	const { container } = render(Converse);
	await settle();
	const box = container.querySelector('textarea') as HTMLTextAreaElement;
	await fireEvent.input(box, { target: { value: 'hi' } });
	await settle();
	const send = [...container.querySelectorAll('button')].find((b) => b.textContent?.trim() === '发送')!;
	await fireEvent.click(send);
	await settle();
}

describe('/converse onError 走世界码的中文提示（裁定 R59 I-2）', () => {
	beforeEach(() => {
		streamConverse.mockReset();
		toastError.mockReset();
	});

	it('endpoint_changed → 与 remember / recall 同一份中文提示', async () => {
		await sendAndFail('endpoint_changed');
		expect(toastError).toHaveBeenCalledWith(worldErrorMessage('endpoint_changed'));
	});

	it('world_not_found 也映射；未知码保持「对话出错」', async () => {
		await sendAndFail('world_not_found');
		expect(toastError).toHaveBeenCalledWith('世界不存在');
		toastError.mockReset();
		await sendAndFail('converse_failed');
		expect(toastError).toHaveBeenCalledWith('对话出错:converse_failed');
	});
});
