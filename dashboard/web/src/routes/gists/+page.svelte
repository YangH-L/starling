<script lang="ts">
	import { describeError } from '$lib/worlds';
	import { api, type Gist, type GistData, type GistMember, worldPath } from '$lib/api';
	import { createQuery } from '$lib/query.svelte';
	import { lastWsEvent } from '$lib/health';
	import { refetchOnEvent, onWorldChange } from '$lib/world.svelte';
	import { toast } from '$lib/ui/toast';
	import PageHeader from '$lib/components/PageHeader.svelte';
	import { Badge, Card, EmptyState, Skeleton, Drawer } from '$lib/components/ui';

	// #38-C v2 可观测:固化 NORM gist 的只读检视(GET /api/gists)+ 谱系钻取。
	const q = createQuery(() => api.get<GistData>(worldPath('/api/gists')));
	$effect(() => {
		q.refetch();
	});
	// T8 — gist 由后台 replay/consolidation 产出(手动 replay_trigger 也广播 tick)。
	// review M1 — 但页面还渲染 review_status 与 consolidation_state,而审批走 /review
	// (广播 statement_added)、遗忘走 statement_forgotten,只订 tick 会漏掉这两类。
	$effect(() => {
		if (refetchOnEvent($lastWsEvent)) q.refetch();
	});

	let gists = $derived(q.data?.gists ?? []);
	let byState = $derived(q.data?.by_state ?? {});

	// consolidated=已验证晋升(live、可检索);volatile=未门控/惰性(不可检索);
	// archived/forgotten=出局(冲突仲裁/衰减/遗忘)。
	const stateTone = (s: string): 'success' | 'info' | 'neutral' =>
		s === 'consolidated' ? 'success' : s === 'volatile' ? 'info' : 'neutral';
	const pct = (conf: number) => `${Math.round(conf * 100)}%`;
	const triple = (subj: string, pred: string, obj: string) =>
		`${subj} ${pred} ${obj}`.replace(/\s+/g, ' ').trim();

	// 谱系钻取:点开一条 gist → 拉取并展示它泛化自的来源簇成员(derived_from)。
	let detailOpen = $state(false);
	let detail = $state<Gist | null>(null);
	let members = $state<GistMember[]>([]);
	let membersLoading = $state(false);
	let detailForId = $state(''); // 防陈旧:并发点开时只让最新一次的 members 落地

	// 换世界:抽屉里是上个世界的 gist,关掉并清空;在途的旧 members 因 detailForId 清空而丢弃。
	onWorldChange(() => {
		detailOpen = false;
		detail = null;
		members = [];
		membersLoading = false;
		detailForId = '';
	});

	async function openDetail(gist: Gist) {
		detail = gist;
		detailOpen = true;
		members = [];
		membersLoading = true;
		detailForId = gist.id;
		try {
			const res = await api.get<{ members: GistMember[] }>(
				worldPath(`/api/gist_members/${encodeURIComponent(gist.id)}`)
			);
			if (detailForId === gist.id) members = res.members;
		} catch (e) {
			if (detailForId === gist.id)
				toast.error(`取来源失败:${describeError(e)}`);
		} finally {
			if (detailForId === gist.id) membersLoading = false;
		}
	}
</script>

<PageHeader
	title="固化 gist"
	subtitle="NORM gist:多 holder 共识经 LLM 判定 + 门控固化的范式(点开看来源谱系,只读)。"
/>

{#if q.error}
	<EmptyState title="加载失败" description={describeError(q.error)} />
{:else if q.loading && !q.data}
	<div class="space-y-2">{#each Array(4) as _}<Skeleton class="h-16 w-full" />{/each}</div>
{:else}
	{#if Object.keys(byState).length}
		<div class="mb-4 flex flex-wrap gap-2">
			{#each Object.entries(byState) as [s, n]}
				<Badge tone={stateTone(s)}>{s}: {n}</Badge>
			{/each}
		</div>
	{/if}

	{#if gists.length === 0}
		<EmptyState
			title="暂无 gist"
			description="配置 consolidation LLM 角色并跑离线回放(run_sleep)后,经验证门控固化的共识范式会出现在这里。"
		/>
	{:else}
		<Card>
			<ul class="divide-y divide-border">
				{#each gists as g}
					<li>
						<button
							type="button"
							onclick={() => openDetail(g)}
							class="flex w-full items-start justify-between gap-3 py-3 text-left transition hover:opacity-80"
						>
							<div class="min-w-0 flex-1">
								<div class="text-sm text-fg">{g.consolidation_summary || triple(g.subject_id, g.predicate, g.object_value)}</div>
								<div class="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-subtle">
									<span class="font-mono">{g.predicate}</span>
									<span aria-hidden="true">·</span>
									<span class="truncate">{g.object_value}</span>
									<span aria-hidden="true">·</span>
									<span>派生自 {g.derived_from.length} 条(depth {g.derived_depth})</span>
								</div>
							</div>
							<div class="flex shrink-0 flex-col items-end gap-1">
								<Badge tone={stateTone(g.consolidation_state)}>{g.consolidation_state}</Badge>
								<span class="text-xs tabular-nums text-muted">conf {pct(g.confidence)}</span>
							</div>
						</button>
					</li>
				{/each}
			</ul>
		</Card>
	{/if}
{/if}

<Drawer bind:open={detailOpen} title="gist 谱系">
	{#if detail}
		<div class="space-y-3 text-sm">
			<div>
				<div class="text-xs uppercase tracking-wide text-subtle">范式</div>
				<div class="mt-1 text-fg">
					{detail.consolidation_summary || triple(detail.subject_id, detail.predicate, detail.object_value)}
				</div>
				<div class="mt-1 flex flex-wrap items-center gap-2 text-xs text-subtle">
					<Badge tone={stateTone(detail.consolidation_state)}>{detail.consolidation_state}</Badge>
					<span>{detail.review_status}</span>
					<span>·</span>
					<span class="tabular-nums">conf {pct(detail.confidence)}</span>
					<span>·</span>
					<span>depth {detail.derived_depth}</span>
				</div>
			</div>

			<div class="rounded-control border border-border bg-surface p-3">
				<div class="text-xs uppercase tracking-wide text-subtle">
					来源簇成员（derived_from，{detail.derived_from.length}）
				</div>
				{#if membersLoading}
					<div class="mt-2 space-y-2">{#each Array(3) as _}<Skeleton class="h-8 w-full" />{/each}</div>
				{:else if members.length === 0}
					<div class="mt-2 text-xs text-subtle">
						无可显示的来源（成员可能已被遗忘/清理;谱系 id 仍记录于 derived_from)。
					</div>
				{:else}
					<ul class="mt-1 divide-y divide-border">
						{#each members as m}
							<li class="flex items-center justify-between gap-2 py-2">
								<div class="min-w-0 flex-1">
									<div class="truncate text-fg">
										<span class="text-subtle">{m.holder_id}:</span>
										{triple(m.subject_id, m.predicate, m.object_value)}
									</div>
								</div>
								<Badge tone={stateTone(m.consolidation_state)}>{m.consolidation_state}</Badge>
							</li>
						{/each}
					</ul>
				{/if}
			</div>
			<p class="text-xs text-subtle">
				这条范式由上述 {detail.derived_from.length} 条独立持有者信念聚簇、经 LLM 蕴含验证固化而来。
			</p>
		</div>
	{/if}
</Drawer>
