<script lang="ts">
	import { describeError } from '$lib/worlds';
	import { api, worldPath } from '$lib/api';
	import { byDeadline, deriveFired, isOverdue } from '$lib/commitments';
	import { createQuery } from '$lib/query.svelte';
	import PageHeader from '$lib/components/PageHeader.svelte';
	import { Badge, EmptyState, Skeleton } from '$lib/components/ui';

	type Row = {
		stmt_id: string;
		state: string;
		subject_id: string;
		predicate: string;
		object_value: string;
		deadline?: string | null;
	};
	type Trigger = { commitment_stmt_id: string; status: string };

	const q = createQuery(() => api.get<{ rows: Row[]; triggers: Trigger[] }>(worldPath('/api/commitments')));
	$effect(() => {
		q.refetch();
	});

	let firedSet = $derived(deriveFired(q.data?.triggers));
	let pending = $derived.by(() => {
		// now 在 derived 内取值:数据每次刷新都重算,长开页面的逾期判定不过期。
		const nowIso = new Date().toISOString();
		return (q.data?.rows ?? [])
			.filter((r) => r.state === 'ACTIVE' || r.state === 'created')
			.map((r) => ({
				...r,
				fired: firedSet.has(r.stmt_id),
				overdue: isOverdue(r.deadline, nowIso)
			}))
			.sort(byDeadline);
	});
</script>

<PageHeader title="承诺提醒" subtitle="待办承诺（active / created），按截止日期升序；逾期与 fired 醒目标注。" />

{#if q.error}
	<EmptyState title="加载失败" description={describeError(q.error)} />
{:else if q.loading && !q.data}
	<div class="space-y-2">{#each Array(3) as _}<Skeleton class="h-16 w-full" />{/each}</div>
{:else if pending.length === 0}
	<EmptyState title="无待办承诺" description="没有处于 active 或 created 状态的承诺。" />
{:else}
	<ul class="space-y-2">
		{#each pending as r}
			<li
				class="flex items-start gap-3 rounded-control border bg-card px-4 py-3 {r.overdue
					? 'border-danger/40'
					: 'border-border'}"
			>
				<div class="min-w-0 flex-1">
					<div class="flex flex-wrap items-center gap-x-2">
						<span class="font-medium text-fg">{r.subject_id}</span>
						<span class="text-sm text-muted">
							{r.predicate} <span class="text-subtle">→</span> {r.object_value}
						</span>
					</div>
					{#if r.deadline}
						<div class="mt-1 text-xs {r.overdue ? 'text-danger' : 'text-subtle'}">
							截止 {r.deadline}{r.overdue ? ' · 已逾期' : ''}
						</div>
					{/if}
				</div>
				<div class="flex shrink-0 items-center gap-1">
					<Badge tone={r.state === 'ACTIVE' ? 'info' : 'neutral'}>{r.state}</Badge>
					{#if r.fired}<Badge tone="warn">⚠ DUE</Badge>{/if}
				</div>
			</li>
		{/each}
	</ul>
{/if}
