<script lang="ts">
	import { api, worldPath } from '$lib/api';
	import { createQuery } from '$lib/query.svelte';
	import { lastWsEvent } from '$lib/health';
	import { untrack } from 'svelte';
	import { refetchOnEvent, currentWorld } from '$lib/world.svelte';
	import { labelFor, glossFor, orderedEntries } from '$lib/labels';
	import { describeEvent, isForWorld, type FeedEventView } from '$lib/feed';
	import PageHeader from '$lib/components/PageHeader.svelte';
	import StatCard from '$lib/components/StatCard.svelte';
	import { Card, Skeleton, EmptyState, Badge } from '$lib/components/ui';

	type Overview = {
		counts: Record<string, number>;
		commitments_by_state: Record<string, number>;
		queue_by_status: Record<string, number>;
	};

	const q = createQuery(() => api.get<Overview>(worldPath('/api/overview')));
	$effect(() => {
		q.refetch();
	});
	$effect(() => {
		if (refetchOnEvent($lastWsEvent)) q.refetch();
	});

	// T7 — feed 存结构化视图(describeEvent 按事件类型渲染成人话 + 语义色),
	// 不再是 JSON.stringify(payload).slice(0,80) 的腰斩 JSON。
	type FeedEntry = { t: string; view: FeedEventView };
	let feed = $state<FeedEntry[]>([]);

	$effect(() => {
		const e = $lastWsEvent;
		// 广播带 world:总览只记当前世界的活动。
		if (e && isForWorld(e, untrack(currentWorld))) {
			const entry: FeedEntry = {
				t: new Date().toLocaleTimeString(),
				view: describeEvent(e.type, e.payload)
			};
			feed = [entry, ...feed].slice(0, 12);
		}
	});

	// 承诺状态的语义色:醒目但不滥用——BROKEN 红、ACTIVE 品牌色、FULFILLED 绿,其余中性。
	// whole-branch review Minor(a) — 补 FULFILLED:承诺状态机页的 STATE_TONES 给的是
	// success,这里却是 default,而那边的注释写着「与总览看板一致」。两处同一概念必须同色,
	// 否则同一条承诺在两个页面上是两种颜色。
	const laneTone = (k: string) =>
		k === 'BROKEN' ? 'danger' : k === 'ACTIVE' ? 'brand' : k === 'FULFILLED' ? 'success' : 'default';
</script>

<PageHeader title="总览" subtitle="记忆体状态、关键计数与实时活动。" />

{#if q.error}
	<EmptyState title="加载失败" description={q.error.message} />
{:else if q.loading && !q.data}
	<div class="grid grid-cols-2 gap-3 md:grid-cols-5">
		{#each Array(5) as _}<Skeleton class="h-24 w-full" />{/each}
	</div>
{:else if q.data}
	<div class="space-y-5">
		<div class="grid grid-cols-2 gap-3 md:grid-cols-5">
			{#each orderedEntries(q.data.counts) as [k, v]}
				<StatCard label={labelFor(k)} value={v} hint={glossFor(k)} />
			{/each}
		</div>
		<div class="grid gap-5 lg:grid-cols-2">
			<Card title="承诺分态" description="六态机当前分布。">
				<div class="grid grid-cols-3 gap-3">
					{#each orderedEntries(q.data.commitments_by_state) as [k, v]}
						<StatCard label={labelFor(k)} value={v} tone={laneTone(k)} hint={glossFor(k)} />
					{/each}
				</div>
			</Card>
			<Card title="队列状态" description="outbox 派发与向量嵌入。">
				<div class="grid grid-cols-2 gap-3">
					{#each orderedEntries(q.data.queue_by_status) as [k, v]}
						<StatCard label={labelFor(k)} value={v} hint={glossFor(k)} />
					{/each}
				</div>
			</Card>
		</div>
		<Card title="最近活动" description="经 /ws 的 tick 与 statement_added 实时增量。">
			{#if feed.length === 0}
				<p class="text-sm text-subtle">等待事件…(tick / statement_added)</p>
			{:else}
				<ul class="space-y-1.5">
					{#each feed as entry}
						<li class="flex items-baseline gap-2 text-xs">
							<Badge tone={entry.view.tone}>{entry.view.label}</Badge>
							<span class="flex-1 truncate text-muted">{entry.view.detail}</span>
							<span class="shrink-0 font-mono text-subtle">{entry.t}</span>
						</li>
					{/each}
				</ul>
			{/if}
		</Card>
	</div>
{/if}
