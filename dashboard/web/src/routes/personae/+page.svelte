<script lang="ts">
	import { api, type PersonaeResponse, worldPath } from '$lib/api';
	import { createQuery } from '$lib/query.svelte';
	import DataTable from '$lib/components/DataTable.svelte';
	import PageHeader from '$lib/components/PageHeader.svelte';
	import { EmptyState, Skeleton } from '$lib/components/ui';

	// T0d-2 — 新皮层 · 画像(Persona):某租户的 persona 容器只读检视。数据源
	// containers WHERE kind='persona'。容器的合并/演化语义在 C++ 内核,本页纯只读
	// 列元数据快照 —— 不新增任何可写路径,也不复算 scope_descriptor(canonical JSON)。
	const q = createQuery(() => api.get<PersonaeResponse>(worldPath('/api/personae')));
	$effect(() => {
		q.refetch();
	});

	let rows = $derived(q.data?.rows ?? []);
</script>

<PageHeader title="画像" subtitle="他者与自我的 persona 容器。只读快照,合并语义在内核。" />

{#if q.error}
	<EmptyState title="加载失败" description={q.error.message} />
{:else if q.loading && !q.data}
	<div class="space-y-2">{#each Array(4) as _}<Skeleton class="h-8 w-full" />{/each}</div>
{:else if rows.length === 0}
	<EmptyState
		title="还没有画像"
		description="随对话积累,persona 容器会在这里出现。"
	/>
{:else}
	<div class="mb-3 text-xs text-subtle">{rows.length} 个画像容器</div>
	<DataTable
		{rows}
		loading={q.loading}
		emptyText="无画像"
		columns={['id', 'holder_id', 'scope_descriptor', 'version', 'created_at', 'updated_at']}
	/>
{/if}
