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

import Engrams from './+page.svelte';

const settle = async () => {
	await tick();
	await Promise.resolve();
	await Promise.resolve();
	await tick();
};
const at = (u: string) => {
	(page as { url: URL }).url = new URL(u, 'http://localhost');
};
const ROW = {
	id: 'E1', source_kind: 'chat', privacy_class: 'normal', retention_mode: 'keep', refcount: 1,
	created_at: '', erased_at: null, source_item_id: 'i', chunk_index: 0, adapter_name: 'a'
};
const DETAIL = {
	engram: { ...ROW, tenant_id: 't', content_hash: 'h', ingest_policy: '', ingest_mode: '',
		payload_uri: null, adapter_version: '', source_version: '', declared_transformations_json: '',
		byte_preserving: 1 },
	preview: 'PREVIEW-TEXT', preview_suppressed_reason: null, referencing_statements: []
};

describe('/engrams 详情抽屉与换世界', () => {
	beforeEach(() => {
		get.mockReset().mockImplementation(async (path: string) =>
			path.includes('/engram/') ? DETAIL : { rows: [ROW], total: 1 }
		);
		noteDefaultWorld('default');
		at('/engrams');
	});

	it('抽屉开着时换世界：抽屉关闭、上个世界的详情不再显示；再点开走新世界路径', async () => {
		const { container } = render(Engrams);
		await settle();
		await fireEvent.click(container.querySelector('tbody tr')!);
		await settle();
		expect(get.mock.calls.map((c) => c[0])).toContain('/api/engram/E1');
		expect(document.body.textContent).toContain('PREVIEW-TEXT');

		at('/engrams?world=w1');
		await settle();
		expect(document.body.textContent).not.toContain('PREVIEW-TEXT');

		await fireEvent.click(container.querySelector('tbody tr')!);
		await settle();
		expect(get.mock.calls.map((c) => c[0])).toContain('/api/w/w1/engram/E1');
		expect(document.body.textContent).toContain('PREVIEW-TEXT');
	});
});
