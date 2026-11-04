<script lang="ts">
	import { api, worldPath } from '$lib/api';
	import { createQuery } from '$lib/query.svelte';
	import PageHeader from '$lib/components/PageHeader.svelte';
	import { Badge, Skeleton, EmptyState } from '$lib/components/ui';

	// Phase 3 片 1 — 类脑 IA 落地页。9 脑区按记忆流排序;休眠区(透视镜/Lens 待片 3)
	// 静默显示(地图职责是教脑模型,缺区的脑图不是脑图),非休眠空区显 0。
	type Region = {
		key: string;
		label: string;
		region: string | null; // 脑区 gloss(海马/新皮层/…),对话/配置/透视镜为 null
		href: string;
		count: number | null; // null = 无「容量」语义(对话/配置/休眠)
		dormant: boolean; // true = 特性未落地(透视镜),静默显、不可点
	};

	const q = createQuery(() => api.get<{ regions: Region[] }>(worldPath('/api/brain_map')));
	$effect(() => {
		q.refetch();
	});

	let regions = $derived(q.data?.regions ?? []);
	let allEmpty = $derived(
		regions.length > 0 && regions.every((r) => r.dormant || r.count === null || r.count === 0)
	);
</script>

<PageHeader
	title="脑区地图"
	subtitle="按人脑记忆系统组织的活体视图 —— 每区一个入口与活体计数,按记忆流(输入 → 快存 → 慢存 → 他者 → 意图 → 固化 → 内省 → 体征)排列。"
/>

<div class="mx-auto max-w-2xl">
	{#if q.error}
		<EmptyState title="读取失败" description={String(q.error)} />
	{:else if q.loading && regions.length === 0}
		<div class="space-y-2">
			{#each Array(8) as _}
				<Skeleton class="h-14 w-full" />
			{/each}
		</div>
	{:else if allEmpty}
		<EmptyState
			title="大脑还很安静"
			description="还没有记忆。去「对话」聊一轮,或在「交互」里 remember 一条,各脑区就会亮起来。"
		/>
	{:else}
		<ul class="space-y-2">
			{#each regions as r (r.key)}
				{#if r.dormant}
					<li
						class="flex items-center justify-between rounded-control border border-dashed border-border bg-bg px-4 py-3 opacity-60"
						title="该脑区尚未落地"
					>
						<span class="text-sm text-muted">
							{r.label}{#if r.region}<span class="text-subtle"> · {r.region}</span>{/if}
						</span>
						<span class="text-xs text-subtle">尚无内容 · 待片 3</span>
					</li>
				{:else}
					<li>
						<a
							href={r.href}
							class="flex items-center justify-between rounded-control border border-border bg-surface px-4 py-3 transition hover:border-brand-border hover:bg-brand-tint"
						>
							<span class="text-sm text-fg">
								{r.label}{#if r.region}<span class="text-subtle"> · {r.region}</span>{/if}
							</span>
							{#if r.count !== null}
								<Badge tone="neutral">{r.count}</Badge>
							{/if}
						</a>
					</li>
				{/if}
			{/each}
		</ul>
	{/if}
</div>
