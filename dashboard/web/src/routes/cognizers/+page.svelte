<script lang="ts">
	import { api, worldPath } from '$lib/api';
	import { createQuery } from '$lib/query.svelte';
	import Graph from '$lib/components/Graph.svelte';
	import DataTable from '$lib/components/DataTable.svelte';
	import PageHeader from '$lib/components/PageHeader.svelte';
	import { Card, EmptyState, Drawer } from '$lib/components/ui';

	type Cognizer = { id: string; canonical_name: string; kind: string; last_seen_at: string };
	type Relation = {
		a_id: string;
		b_id: string;
		affinity: number;
		power_asymmetry: number;
		fiske_weights_json: string;
	};

	const q = createQuery(() => api.get<{ nodes: Cognizer[]; relations: Relation[] }>(worldPath('/api/cognizers')));
	$effect(() => {
		q.refetch();
	});

	// T0e ① — kind 传给 Graph 做节点分类着色(表格已有 kind 列,保留)。
	let gnodes = $derived(
		(q.data?.nodes ?? []).map((n) => ({ id: n.id, label: n.canonical_name, kind: n.kind }))
	);
	// T3 — affinity/power_asymmetry 随边一起传给 Graph 做粗细/方向映射。
	let gedges = $derived(
		(q.data?.relations ?? []).map((r) => ({
			a: r.a_id,
			b: r.b_id,
			affinity: r.affinity,
			power_asymmetry: r.power_asymmetry
		}))
	);

	let drawerOpen = $state(false);
	let selected = $state<Cognizer | null>(null);

	function openNode(id: string) {
		const node = (q.data?.nodes ?? []).find((n) => n.id === id) ?? null;
		selected = node;
		drawerOpen = true;
	}

	function openNodeRow(row: Record<string, unknown>) {
		const id = row.id as string | undefined;
		if (id) openNode(id);
	}
</script>

<PageHeader title="认知体社会图" subtitle="Cognizer 节点、关系与在场记录。" />

{#if q.error}
	<EmptyState title="加载失败" description={q.error.message} />
{:else}
	<div class="space-y-4">
		<Card>
			<Graph
				nodes={gnodes}
				edges={gedges}
				onNodeClick={openNode}
				selectedId={selected?.id ?? null}
			/>
		</Card>
		<DataTable
			rows={q.data?.nodes ?? []}
			loading={q.loading}
			emptyText="无 cognizer"
			columns={['canonical_name', 'kind', 'last_seen_at']}
			onRowClick={openNodeRow}
		/>
	</div>
{/if}

<Drawer bind:open={drawerOpen} title="Cognizer">
	{#if selected}
		<dl class="space-y-3 text-sm">
			<div>
				<dt class="text-xs uppercase tracking-wide text-muted">canonical_name</dt>
				<dd class="mt-0.5 font-medium text-fg">{selected.canonical_name}</dd>
			</div>
			<div>
				<dt class="text-xs uppercase tracking-wide text-muted">kind</dt>
				<dd class="mt-0.5 text-fg">{selected.kind}</dd>
			</div>
			<div>
				<dt class="text-xs uppercase tracking-wide text-muted">last_seen_at</dt>
				<dd class="mt-0.5 text-fg">{selected.last_seen_at}</dd>
			</div>
			<div>
				<dt class="text-xs uppercase tracking-wide text-muted">id</dt>
				<dd class="mt-0.5 break-all font-mono text-xs text-subtle">{selected.id}</dd>
			</div>
		</dl>
	{/if}
</Drawer>
