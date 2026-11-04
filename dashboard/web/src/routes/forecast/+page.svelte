<script lang="ts">
	import { api, type ForecastResponse, worldPath } from '$lib/api';
	import { createQuery } from '$lib/query.svelte';
	import PageHeader from '$lib/components/PageHeader.svelte';
	import { Badge, EmptyState, Skeleton } from '$lib/components/ui';
	import { retrievabilityPct, riskTone, riskLabel, type Tone } from '$lib/forecast';

	const q = createQuery(() => api.get<ForecastResponse>(worldPath('/api/forecast')));
	$effect(() => {
		q.refetch();
	});

	let rows = $derived(q.data?.rows ?? []);

	// tone → 进度条填色(app.css @theme 注册的色 token)。
	const FILL: Record<Tone, string> = {
		info: 'bg-info',
		brand: 'bg-brand',
		success: 'bg-success',
		neutral: 'bg-subtle',
		danger: 'bg-danger',
		warn: 'bg-warn'
	};
	const day = (iso: string | null) => (iso ? iso.slice(0, 10) : '—');
</script>

<PageHeader
	title="衰减预报"
	subtitle="按遗忘曲线 S(t)=exp(-Δt/S0) 排出最可能被遗忘的记忆。投影自 C++ forgetting_curve(只读),不改记忆。"
/>
{#if q.error}
	<EmptyState title="加载失败" description={q.error.message} />
{:else if q.loading && !q.data}
	<Skeleton class="h-40 w-full" />
{:else if q.data}
	<p
		class="mb-3 rounded-control border border-dashed border-border bg-bg px-3 py-2 text-xs text-subtle"
	>
		S(t) 与预计时点均由 C++ forgetting_curve 算出(输入同 op_decay)。候选有界 {q.data.candidate_limit}
		条(最久未访问优先);阈值 {q.data.threshold}(op_decay 归档线)。注:op_decay 仅归档 consolidated,volatile
		走 TTL;受 ACTIVE commitment 保护者不会被衰减归档。
	</p>
	{#if rows.length === 0}
		<EmptyState title="无候选" description="没有 volatile / consolidated 语句可预报。" />
	{:else}
		<ul class="space-y-2">
			{#each rows as r (r.id)}
				{@const pct = retrievabilityPct(r.s_t)}
				<li class="rounded-control border border-border bg-surface px-4 py-3">
					<div class="flex items-center justify-between gap-3">
						<span class="truncate text-sm text-fg">{r.subject_id} · {r.predicate} · {r.object_value}</span>
						<div class="flex shrink-0 items-center gap-2">
							{#if r.active_grounded}<Badge tone="brand">受保护</Badge>{/if}
							<Badge tone={riskTone(r.s_t)}>{riskLabel(r.s_t)}</Badge>
						</div>
					</div>
					<div class="mt-2 flex items-center gap-3">
						<div class="h-1.5 flex-1 overflow-hidden rounded-full border border-border">
							<div class="{FILL[riskTone(r.s_t)]} h-full" style="width:{pct}%"></div>
						</div>
						<span class="w-10 shrink-0 text-right text-xs tabular-nums text-muted">{pct}%</span>
					</div>
					<div class="mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-subtle">
						<span>{r.modality} · {r.consolidation_state}</span>
						<span>上次访问 {day(r.last_accessed)}</span>
						<span>预计达阈值 {day(r.forget_at)}</span>
					</div>
				</li>
			{/each}
		</ul>
	{/if}
{/if}
