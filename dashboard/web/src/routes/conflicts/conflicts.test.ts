import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/svelte';
import Page from './+page.svelte';

const snapshot = vi.hoisted(() => ({
	by_kind: { conflicts_with: 2, supersedes: 4 },
	conflicts: [
		{ src_id: 'a', dst_id: 'b', edge_kind: 'conflicts_with', weight: 1,
		  src_subject: 'Alice', src_predicate: 'likes', src_object: 'tea',
		  dst_subject: 'Alice', dst_predicate: 'likes', dst_object: 'coffee',
		  src_state: 'consolidated', dst_state: 'consolidated' },
		{ src_id: 'c', dst_id: 'd', edge_kind: 'conflicts_with', weight: 0.8,
		  src_state: 'forgotten', dst_state: 'consolidated' }
	]
}));

vi.mock('$lib/query.svelte', () => ({
	createQuery: () => ({ data: snapshot, loading: false, error: null,
		refetch: vi.fn().mockResolvedValue(undefined) })
}));
vi.mock('$lib/api', () => ({
	api: { get: vi.fn(), post: vi.fn() }, worldPath: (path: string) => path
}));
vi.mock('$lib/health', async () => {
	const { readable } = await import('svelte/store');
	return { lastWsEvent: readable(null) };
});
vi.mock('$lib/world.svelte', () => ({ refetchOnEvent: () => false }));

describe('canonical conflict edge counts', () => {
	it('counts only visible lowercase conflicts while keeping other kinds raw', () => {
		const { getByText, queryByText } = render(Page);
		expect(getByText('conflicts_with: 1')).toBeTruthy();
		expect(getByText('supersedes: 4')).toBeTruthy();
		expect(queryByText('conflicts_with: 2')).toBeNull();
	});
});
