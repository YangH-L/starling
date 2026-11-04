<script lang="ts">
	import { api, worldPath } from '$lib/api';
	import { createQuery } from '$lib/query.svelte';
	import { lastWsEvent } from '$lib/health';
	import { refetchOnEvent } from '$lib/world.svelte';
	import { labelFor, glossFor, orderedEntries } from '$lib/labels';
	import StatCard from '$lib/components/StatCard.svelte';
	import PageHeader from '$lib/components/PageHeader.svelte';
	import { Card, Badge, Button, Skeleton, EmptyState } from '$lib/components/ui';
	import { toast } from '$lib/ui/toast';

	type QueueData = {
		dispatch: Record<string, number>;
		embedding_backlog: number;
		vectors_by_status: Record<string, number>;
	};

	const q = createQuery(() => api.get<QueueData>(worldPath('/api/queues')));
	$effect(() => {
		q.refetch();
	});
	// T8 review I1 — 原先只订 tick 是错的:这页读的三样里有两样被「写入」直接改。
	//   dispatch          = bus_events 按 dispatch_status 计数,任何业务事件 append 都进这表。
	//   embedding_backlog = statements 里没有向量的行 —— 而 post-write pump 没有 embed 阶段
	//                       (嵌入只在 tick 跑),故新语句落库「瞬间」积压 +1,只订 tick 要等 30s。
	$effect(() => {
		if (refetchOnEvent($lastWsEvent)) q.refetch();
	});

	let ticking = $state(false);

	async function tick() {
		ticking = true;
		try {
			// tick 批量嵌入积压语句(逐条走网络),按积压量放宽。
			await api.post(worldPath('/api/tick'), {}, { timeoutMs: 120_000 });
			toast.success('Tick 发送成功');
			await q.refetch();
		} catch (e) {
			toast.error(e instanceof Error ? e.message : String(e));
		} finally {
			ticking = false;
		}
	}

	type BacklogTone = 'success' | 'warn' | 'danger';

	function getBacklogInfo(backlog: number): { tone: BacklogTone; label: string } {
		if (backlog === 0) return { tone: 'success', label: '健康' };
		if (backlog <= 50) return { tone: 'warn', label: '积压' };
		return { tone: 'danger', label: '高积压' };
	}

	let backlogInfo = $derived(q.data != null ? getBacklogInfo(q.data.embedding_backlog) : null);
</script>

<PageHeader title="队列 / 运维" subtitle="队列健康:outbox 派发、嵌入积压与向量状态。">
	{#snippet actions()}
		<Button variant="soft" loading={ticking} onclick={tick}>手动 Tick</Button>
	{/snippet}
</PageHeader>

{#if q.error}
	<EmptyState title="加载失败" description={q.error.message} />
{:else if q.loading && !q.data}
	<div class="grid grid-cols-2 gap-3 md:grid-cols-4">
		{#each Array(4) as _}<Skeleton class="h-20 w-full" />{/each}
	</div>
{:else if q.data}
	<div class="space-y-6">
		<Card title="Embedding 积压">
			<div class="flex items-center gap-3">
				<StatCard label="embedding backlog" value={q.data.embedding_backlog} hint="待嵌入语句数" />
				{#if backlogInfo}
					<Badge tone={backlogInfo.tone}>{backlogInfo.label}</Badge>
				{/if}
			</div>
		</Card>
		<Card title="Outbox dispatch" description="outbox 事件按派发状态计数。">
			<div class="grid grid-cols-2 gap-3 md:grid-cols-4">
				{#each orderedEntries(q.data.dispatch) as [k, v]}
					<StatCard label={labelFor(k)} value={v} hint={glossFor(k)} />
				{/each}
			</div>
		</Card>
		<Card title="向量状态" description="嵌入向量按状态计数。">
			<div class="grid grid-cols-2 gap-3 md:grid-cols-4">
				{#each orderedEntries(q.data.vectors_by_status) as [k, v]}
					<StatCard label={labelFor(k)} value={v} hint={glossFor(k)} />
				{/each}
			</div>
		</Card>
	</div>
{/if}
