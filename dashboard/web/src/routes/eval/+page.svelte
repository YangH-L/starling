<script lang="ts">
	import { describeError } from '$lib/worlds';
	import { marked } from 'marked';
	import DOMPurify from 'dompurify';
	import { api } from '$lib/api';
	import { createQuery } from '$lib/query.svelte';
	import PageHeader from '$lib/components/PageHeader.svelte';
	import { Card, EmptyState, Skeleton } from '$lib/components/ui';

	type Report = { name: string; markdown: string; [k: string]: unknown };
	const q = createQuery(() => api.get<{ reports: Report[] }>('/api/eval'));
	$effect(() => {
		q.refetch();
	});
	let reports = $derived([...(q.data?.reports ?? [])].reverse());
	// Eval reports can embed model-generated transcripts (untrusted), so sanitize
	// the parsed HTML before {@html} — strips <script>, event handlers, and
	// javascript: URLs while keeping safe markdown markup. Prevents token-stealing
	// XSS in the dashboard.
	const render = (md: string) =>
		DOMPurify.sanitize(marked.parse(md ?? '', { async: false }) as string);
</script>

<PageHeader title="评测报告" subtitle="准入 eval 的 markdown 输出,最新优先。" />
{#if q.error}
	<EmptyState title="加载失败" description={describeError(q.error)} />
{:else if q.loading && !q.data}
	<Skeleton class="h-40 w-full" />
{:else if reports.length === 0}
	<EmptyState title="暂无报告" description="跑一次评测后报告会出现在这里。" />
{:else}
	<div class="space-y-3">
		{#each reports as r}
			{@const meta = Object.entries(r).filter(([k]) => k !== 'name' && k !== 'markdown')}
			<Card title={r.name}>
				{#if meta.length}
					<div class="mb-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-subtle">
						{#each meta as [k, v]}<span>{k}: <span class="text-muted">{String(v)}</span></span>{/each}
					</div>
				{/if}
				<details open>
					<summary class="mb-2 cursor-pointer select-none text-xs text-muted">展开 / 折叠</summary>
					<div class="md">{@html render(r.markdown)}</div>
				</details>
			</Card>
		{/each}
	</div>
{/if}

<style>
	.md :global(h1),
	.md :global(h2),
	.md :global(h3) {
		font-weight: 600;
		color: var(--color-fg);
		margin: 0.7em 0 0.3em;
	}
	.md :global(h1) {
		font-size: 1.05rem;
	}
	.md :global(h2) {
		font-size: 0.98rem;
	}
	.md :global(h3) {
		font-size: 0.9rem;
	}
	.md :global(p) {
		margin: 0.4em 0;
		font-size: 0.875rem;
		color: var(--color-fg);
	}
	.md :global(ul),
	.md :global(ol) {
		margin: 0.4em 0;
		padding-left: 1.4em;
		font-size: 0.875rem;
		color: var(--color-fg);
	}
	.md :global(li) {
		margin: 0.15em 0;
	}
	.md :global(code) {
		background: var(--color-surface);
		padding: 0.1em 0.3em;
		border-radius: 4px;
		font-size: 0.8em;
	}
	.md :global(pre) {
		background: var(--color-surface);
		padding: 0.75em;
		border-radius: 8px;
		overflow-x: auto;
	}
	.md :global(pre code) {
		background: none;
		padding: 0;
	}
	.md :global(table) {
		border-collapse: collapse;
		font-size: 0.8rem;
		margin: 0.4em 0;
	}
	.md :global(th),
	.md :global(td) {
		border: 1px solid var(--color-border);
		padding: 0.3em 0.6em;
	}
	.md :global(a) {
		color: var(--color-brand);
		text-decoration: underline;
	}
	.md :global(blockquote) {
		border-left: 3px solid var(--color-border);
		padding-left: 0.8em;
		color: var(--color-muted);
	}
</style>
