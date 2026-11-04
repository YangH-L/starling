<script lang="ts">
	import { api, worldPath } from '$lib/api';
	import { createQuery } from '$lib/query.svelte';
	import { lastWsEvent } from '$lib/health';
	import { refetchOnEvent } from '$lib/world.svelte';
	import PageHeader from '$lib/components/PageHeader.svelte';
	import { Card, Badge, Skeleton, EmptyState } from '$lib/components/ui';
	import { stateTone, isHealthy, nonZeroMetrics, type RuntimeHealthResponse } from '$lib/runtime_health';

	// D4 (/plan-design-review): reuse createQuery + existing WS tick refetch. No new WS event types.
	const q = createQuery(() => api.get<RuntimeHealthResponse>(worldPath('/api/runtime_health')));
	$effect(() => {
		q.refetch();
	});
	$effect(() => {
		if (refetchOnEvent($lastWsEvent)) q.refetch();
	});

	const healthy = $derived(q.data != null && isHealthy(q.data));
</script>

<PageHeader title="运行时健康" subtitle="RuntimeHealth 状态机 + 转换事件(只读)" />

{#if q.error}
	<EmptyState title="加载失败" description={q.error.message} />
{:else if q.loading && !q.data}
	<Skeleton class="h-24 w-full" />
{:else if q.data}
	<div class="space-y-6">
		<!-- D3: 当前状态 pill 是第一张/标题卡(层级锚点) -->
		<Card title="当前状态">
			<div class="flex items-center gap-2 py-2">
				<Badge tone={stateTone(q.data.status)}>{q.data.status}</Badge>
				{#if healthy}
					<span class="text-sm text-success">一切正常</span>
				{/if}
			</div>
		</Card>

		<!-- D3: 转换事件为次要卡片 -->
		<Card title="转换事件" description="最近的健康状态转换(最多 64 条)">
			{#if q.data.events.length}
				<ul class="divide-y divide-border">
					{#each [...q.data.events].reverse() as e}
						<li class="py-2 text-sm">
							<div class="flex items-center gap-2">
								<Badge tone={stateTone(e.previous_status)}>{e.previous_status}</Badge>
								<span>→</span>
								<Badge tone={stateTone(e.current_status)}>{e.current_status}</Badge>
								<span class="text-muted">{e.trigger}</span>
								{#if e.missing_capabilities.length}
									<span class="text-danger">缺: {e.missing_capabilities.join(', ')}</span>
								{/if}
							</div>
							{#if nonZeroMetrics(e.metrics_snapshot).length}
								<dl class="mt-1 flex flex-wrap gap-x-4 gap-y-0.5 text-xs text-muted">
									{#each nonZeroMetrics(e.metrics_snapshot) as [key, val]}
										<div class="flex gap-1">
											<dt class="text-subtle">{key}:</dt>
											<dd class="font-mono" aria-label="{key} {val}">{val}</dd>
										</div>
									{/each}
								</dl>
							{/if}
						</li>
					{/each}
				</ul>
			{:else}
				<EmptyState title="暂无转换" description="启动后将记录第一次转换" />
			{/if}
		</Card>
	</div>
{/if}
