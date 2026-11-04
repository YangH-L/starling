<script lang="ts">
	import { api, type CommonGroundResponse, worldPath } from '$lib/api';
	import { createQuery } from '$lib/query.svelte';
	import PageHeader from '$lib/components/PageHeader.svelte';
	import { Badge, EmptyState, Skeleton } from '$lib/components/ui';

	// T0d-2 — 新皮层 · 共识(CommonGround):某租户的 common_ground 五态只读检视。
	// 数据源 common_ground 表,LEFT JOIN statements 带出被共识语句文本。共识的推进/
	// 状态机在 C++ 内核,本页纯只读列快照 —— 不新增任何可写路径。
	const q = createQuery(() => api.get<CommonGroundResponse>(worldPath('/api/common_ground')));
	$effect(() => {
		q.refetch();
	});

	let rows = $derived(q.data?.rows ?? []);
	let byStatus = $derived(q.data?.by_status ?? {});

	// 五态 → Badge tone:grounded 成立→success,suspected_diverge 疑似分歧→warn,
	// expired/recanted 失效/撤回→danger,asserted_unack 待确认→neutral(默认)。
	type Tone = 'neutral' | 'success' | 'warn' | 'danger';
	const statusTone = (s: string): Tone =>
		s === 'grounded'
			? 'success'
			: s === 'suspected_diverge'
				? 'warn'
				: s === 'expired' || s === 'recanted'
					? 'danger'
					: 'neutral';

	const statusLabel: Record<string, string> = {
		asserted_unack: '待确认',
		grounded: '已成立',
		suspected_diverge: '疑似分歧',
		expired: '已失效',
		recanted: '已撤回'
	};
	const label = (s: string) => statusLabel[s] ?? s;

	// 固定五态展示顺序(概览徽标),跳过未观测到的态。
	const STATUS_ORDER = ['grounded', 'asserted_unack', 'suspected_diverge', 'expired', 'recanted'];
	let statusChips = $derived(STATUS_ORDER.filter((s) => (byStatus[s] ?? 0) > 0));

	const fmt = (v: unknown) => (v == null || v === '' ? '—' : String(v));
</script>

<PageHeader title="共识" subtitle="双方共识池(CommonGround)五态。只读快照,状态机在内核。" />

{#if q.error}
	<EmptyState title="加载失败" description={q.error.message} />
{:else if q.loading && !q.data}
	<div class="space-y-2">{#each Array(4) as _}<Skeleton class="h-8 w-full" />{/each}</div>
{:else if rows.length === 0}
	<EmptyState
		title="还没有共识记录"
		description="当记忆被标记为双方共识后,会在这里按状态出现。"
	/>
{:else}
	<div class="mb-4 flex flex-wrap items-center gap-2">
		{#each statusChips as s}
			<Badge tone={statusTone(s)}>{label(s)} · {byStatus[s]}</Badge>
		{/each}
		<span class="ml-auto text-xs text-subtle">{rows.length} 条共识</span>
	</div>
	<div class="overflow-x-auto rounded-lg border border-border">
		<table class="w-full text-sm">
			<thead class="bg-surface text-left">
				<tr>
					{#each ['状态', '语句', 'statement_id', 'grounded_at', 'last_confirmed_at'] as h}
						<th scope="col" class="px-3 py-2 font-medium text-muted">{h}</th>
					{/each}
				</tr>
			</thead>
			<tbody>
				{#each rows as r}
					<tr class="border-t border-border/60">
						<td class="px-3 py-2"><Badge tone={statusTone(r.status)}>{label(r.status)}</Badge></td>
						<td class="px-3 py-2 text-fg"
							>{r.subject_id
								? `${fmt(r.subject_id)} ${fmt(r.predicate)} ${fmt(r.object_value)}`
								: '—'}</td
						>
						<td class="px-3 py-2 font-mono text-xs text-muted">{fmt(r.statement_id)}</td>
						<td class="px-3 py-2 text-muted">{fmt(r.grounded_at)}</td>
						<td class="px-3 py-2 text-muted">{fmt(r.last_confirmed_at)}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
{/if}
