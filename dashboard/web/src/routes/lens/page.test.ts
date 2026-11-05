import { describe, it, expect, vi, beforeEach } from 'vitest';
import { tick } from 'svelte';
import { render } from '@testing-library/svelte';
import { page } from '$app/state';
import { noteDefaultWorld } from '$lib/world.svelte';

const get = vi.fn();
vi.mock('$lib/api', async (orig) => {
	const real = await orig<typeof import('$lib/api')>();
	return { ...real, api: { get: (...a: unknown[]) => get(...a), post: vi.fn() } };
});

import Lens from './+page.svelte';

const settle = async () => {
	await tick();
	await Promise.resolve();
	await Promise.resolve();
	await tick();
};
const at = (u: string) => {
	(page as { url: URL }).url = new URL(u, 'http://localhost');
};
const TREE = (world: string) => ({
	id: 'S1', found: true, summary: { subject_id: `subj-of-${world}`, predicate: 'p', object_value: 'o' }
});

describe('/lens 换世界', () => {
	beforeEach(() => {
		get.mockReset().mockImplementation(async (path: string) =>
			TREE(path.startsWith('/api/w/') ? path.split('/')[3] : 'default')
		);
		noteDefaultWorld('default');
		at('/lens?stmt=S1');
	});

	it('同一个 stmt 换了世界：按新世界的路径重取，旧世界的取证树被清掉', async () => {
		const { container } = render(Lens);
		await settle();
		expect(get).toHaveBeenCalledTimes(1);
		expect(get.mock.calls[0][0]).toBe('/api/provenance/S1');
		expect(container.textContent).toContain('subj-of-default');

		at('/lens?stmt=S1&world=w1'); // 顶栏 setWorld：stmt 没变，只有 world 变
		await settle();
		expect(get).toHaveBeenCalledTimes(2);
		expect(get.mock.calls[1][0]).toBe('/api/w/w1/provenance/S1');
		expect(container.textContent).toContain('subj-of-w1');
		expect(container.textContent).not.toContain('subj-of-default');
	});

	it('世界没变、stmt 也没变时不重复取数', async () => {
		render(Lens);
		await settle();
		at('/lens?stmt=S1');
		await settle();
		expect(get).toHaveBeenCalledTimes(1);
	});
});
