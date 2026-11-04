import { describe, it, expect, vi, beforeEach } from 'vitest';
import { tick } from 'svelte';
import { render, fireEvent } from '@testing-library/svelte';
import { page } from '$app/state';
import { noteDefaultWorld } from '$lib/world.svelte';

const get = vi.fn();
vi.mock('$lib/api', async (orig) => {
	const real = await orig<typeof import('$lib/api')>();
	return { ...real, api: { get: (...a: unknown[]) => get(...a), post: vi.fn() } };
});

import Statements from './+page.svelte';

const settle = async () => {
	await tick();
	await Promise.resolve();
	await Promise.resolve();
	await tick();
};
const at = (u: string) => {
	(page as { url: URL }).url = new URL(u, 'http://localhost');
};
const rowsOf = (tag: string) => ({
	rows: [
		{ id: 'S1', subject_id: tag, predicate: 'likes', object_value: 'tea', holder_id: 'h',
		  consolidation_state: 'volatile', review_status: 'ok', observed_at: '' }
	]
});

describe('/statements 详情抽屉与换世界', () => {
	beforeEach(() => {
		get.mockReset().mockImplementation(async (path: string) =>
			rowsOf(path.startsWith('/api/w/') ? 'subj-w1' : 'subj-default')
		);
		noteDefaultWorld('default');
		at('/statements');
	});

	it('抽屉开着时换世界：抽屉关闭，旧世界的行不再挂在新世界下；列表按新世界路径重取', async () => {
		const { container } = render(Statements);
		await settle();
		await fireEvent.click(container.querySelector('tbody tr')!);
		await settle();
		expect(document.body.textContent).toContain('Statement 详情');
		const dlg = () => document.body.querySelector('[role="dialog"]');
		expect(dlg()?.textContent).toContain('subj-default');

		at('/statements?world=w1');
		await settle();
		expect(dlg()?.textContent ?? '').not.toContain('subj-default');
		expect(get.mock.calls.some((c) => String(c[0]).startsWith('/api/w/w1/statements'))).toBe(true);
	});
});
