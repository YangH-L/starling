<script lang="ts">
	import { describeError } from '$lib/worlds';
	import { api, worldPath } from '$lib/api';
	import { createQuery } from '$lib/query.svelte';
	import DataTable from '$lib/components/DataTable.svelte';
	import PageHeader from '$lib/components/PageHeader.svelte';
	import { Card, EmptyState, Skeleton, Badge, Button } from '$lib/components/ui';
	import { toast } from '$lib/ui/toast';
	import { modeLabel, opsSummary, type LedgerRow } from '$lib/dream';
	import { labelFor, orderedEntries } from '$lib/labels';

	type ReplayData = {
		scheduler: Record<string, unknown>;
		ledger: LedgerRow[];
		windows: Record<string, unknown>[];
	};
	const q = createQuery(() => api.get<ReplayData>(worldPath('/api/replay')));
	$effect(() => {
		q.refetch();
	});

	// 片 6:手动触发回放(写动作,持写锁整段 → 放宽超时)。
	let triggering = $state(false);
	async function trigger(mode: 'sleep' | 'idle') {
		triggering = true;
		try {
			const r = await api.post<{ mode: string; sampled: number; compressed: number }>(
				worldPath('/api/replay_trigger'),
				{ mode },
				{ timeoutMs: 60000 }
			);
			toast.success(`${mode === 'sleep' ? '睡眠' : '空闲'}回放:采样 ${r.sampled} · 固化 ${r.compressed}`);
			await q.refetch();
		} catch (e) {
			toast.error(`触发失败:${describeError(e)}`);
		} finally {
			triggering = false;
		}
	}

	let sched = $derived(q.data?.scheduler ?? {});
	let schedEntries = $derived(
		orderedEntries(sched as Record<string, unknown>).filter(([k]) => k !== 'id')
	);
	const fmt = (v: unknown) => (v == null || v === '' ? '—' : String(v));

	let ledger = $derived(q.data?.ledger ?? []);
</script>

<PageHeader
	title="回放与再巩固 · 梦境日志"
	subtitle="Replay / Reconsolidation:每次回放批次做了什么(在线随手固化 / 空闲 / 睡眠),以及打开的再巩固窗口。"
/>
{#if q.error}
	<EmptyState title="加载失败" description={describeError(q.error)} />
{:else if q.loading && !q.data}
	<Skeleton class="h-40 w-full" />
{:else if q.data}
	<div class="space-y-4">
		<Card title="调度器状态" description="在线 / 空闲 / 睡眠通道的最近运行。">
			{#if schedEntries.length === 0}
				<p class="text-sm text-muted">无调度器状态</p>
			{:else}
				<dl class="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3 lg:grid-cols-5">
					{#each schedEntries as [k, v]}
						<div>
							<dt class="text-xs text-subtle">{labelFor(k)}</dt>
							<dd class="mt-0.5 text-sm font-medium text-fg">{fmt(v)}</dd>
						</div>
					{/each}
				</dl>
			{/if}
		</Card>

		<div>
			<div class="mb-2 flex flex-wrap items-center justify-between gap-3">
				<h2 class="text-sm font-semibold text-muted">梦境日志(回放批次)</h2>
				<div class="flex gap-2">
					<Button variant="soft" loading={triggering} disabled={triggering} onclick={() => trigger('sleep')}>
						触发睡眠回放
					</Button>
					<Button variant="ghost" loading={triggering} disabled={triggering} onclick={() => trigger('idle')}>
						空闲回放
					</Button>
				</div>
			</div>
			<p
				class="mb-2 rounded-control border border-dashed border-border bg-bg px-3 py-2 text-xs text-subtle"
			>
				空闲(IDLE)回放由后台 tick 每 30s 驱动;睡眠(SLEEP,深度批 200)无自动调用方 —— 用上方「触发睡眠回放」按需运行。在线(ONLINE)随手固化在 remember / tick 后产生。
			</p>
			{#if ledger.length === 0}
				<EmptyState title="还没有梦" description="尚无回放批次。在线固化会在 remember / tick 后产生记录。" />
			{:else}
				<ul class="space-y-2">
					{#each ledger as b (b.replay_batch_id)}
						<li
							class="flex items-center justify-between gap-3 rounded-control border border-border bg-surface px-4 py-2.5"
						>
							<div class="flex min-w-0 items-center gap-3">
								<Badge tone={modeLabel(b.mode).tone}>{modeLabel(b.mode).label}</Badge>
								<span class="truncate text-sm text-fg">{opsSummary(b.ops_applied_json)}</span>
								<span class="shrink-0 text-xs text-subtle">采样 {b.sampled_count}</span>
							</div>
							<span class="shrink-0 text-xs text-subtle">
								{b.started_at}{b.finished_at ? '' : ' · 进行中'}
							</span>
						</li>
					{/each}
				</ul>
			{/if}
		</div>

		<div>
			<h2 class="mb-2 text-sm font-semibold text-muted">再巩固窗口</h2>
			<DataTable
				rows={q.data.windows}
				emptyText="无窗口"
				columns={['stmt_id', 'opened_at', 'close_deadline', 'status']}
			/>
		</div>
	</div>
{/if}
