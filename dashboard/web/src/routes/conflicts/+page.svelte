<script lang="ts">
	import { api, type CascadePreview, worldPath } from '$lib/api';
	import { createQuery } from '$lib/query.svelte';
	import { lastWsEvent } from '$lib/health';
	import { refetchOnEvent } from '$lib/world.svelte';
	import { toast } from '$lib/ui/toast';
	import PageHeader from '$lib/components/PageHeader.svelte';
	import { Badge, Card, EmptyState, Skeleton, Drawer, Button, ConfirmDialog } from '$lib/components/ui';

	type Conflict = {
		src_id: string;
		dst_id: string;
		edge_kind: string;
		weight: number;
		metadata_json?: string;
		src_subject?: string;
		src_predicate?: string;
		src_object?: string;
		dst_subject?: string;
		dst_predicate?: string;
		dst_object?: string;
		src_state?: string;
		dst_state?: string;
	};
	type ConflictData = { by_kind: Record<string, number>; conflicts: Conflict[] };

	const q = createQuery(() => api.get<ConflictData>(worldPath('/api/conflicts')));
	$effect(() => {
		q.refetch();
	});
	// T8 review I3 — 新语句可能引入冲突、遗忘一侧则就地解决,但漏了后台 tick:这页拿
	// src_state/dst_state 既过滤行(:39 滤掉 forgotten)又决定按钮显隐(:204/:225 只对
	// consolidated 给「再固化」),而这两个 state 正是被后台 tick 推进的 —— 只订写事件的话,
	// 已解决的冲突仍挂在列表上、动作按钮显隐错位。
	$effect(() => {
		if (refetchOnEvent($lastWsEvent)) q.refetch();
	});

	// 一侧已遗忘 = 冲突已就地解决 → 不再列出(遗忘一侧后重取即消失,使工作台动作可见生效)。
	let sorted = $derived(
		[...(q.data?.conflicts ?? [])]
			.filter((c) => c.src_state !== 'forgotten' && c.dst_state !== 'forgotten')
			.sort((a, b) => b.weight - a.weight)
	);
	// 某侧语句缺失(LEFT JOIN 未命中,孤儿边):禁用其干预按钮(命令本身对缺失 id 安全 no-op,
	// 但避免误导性的「成功」toast)。
	const missing = (subj?: string, pred?: string, obj?: string) => !subj && !pred && !obj;
	let maxWeight = $derived(sorted.length ? Math.max(...sorted.map((c) => c.weight)) : 1);
	const barWidth = (w: number, max: number) =>
		w <= 1 ? `${Math.min(100, w * 100)}%` : `${Math.min(100, (w / max) * 100)}%`;

	const label = (subj?: string, pred?: string, obj?: string, fallback = '') =>
		subj || pred || obj ? `${subj ?? '?'} ${pred ?? ''} ${obj ?? ''}`.replace(/\s+/g, ' ').trim() : fallback;
	const reason = (m?: string) => {
		try {
			return m ? (JSON.parse(m).reason ?? '') : '';
		} catch {
			return '';
		}
	};

	let detailOpen = $state(false);
	let detail = $state<Conflict | null>(null);
	function openDetail(c: Conflict) {
		detail = c;
		detailOpen = true;
	}

	// 片6 冲突工作台(最小):在冲突详情里就地对任一侧语句执行已有干预(再固化 / 遗忘),
	// 免去跳去 /statements。复用已测命令(/api/reconsolidate、/api/forget)+ 遗忘的级联
	// 预览警示。全经单写者命令漏斗;成功后重取冲突边刷新。
	let busy = $state(false);
	let confirmOpen = $state(false);
	let confirmCfg = $state<{ title: string; desc: string; run: () => Promise<void> } | null>(null);
	let confirmForId = $state(''); // guards the background cascade-preview against a stale dialog

	async function act(verb: string, fn: () => Promise<unknown>) {
		busy = true;
		try {
			await fn();
			toast.success(`${verb}成功`);
			detailOpen = false;
			await q.refetch();
		} catch (e) {
			toast.error(`${verb}失败:${e instanceof Error ? e.message : String(e)}`);
		} finally {
			busy = false;
		}
	}

	const reconsolidate = (id: string) =>
		act('请求再固化', () => api.post(worldPath('/api/reconsolidate'), { stmt_id: id }));

	function askForget(id: string) {
		const base = '逻辑删除(→ forgotten,移出检索与回放池)。不可逆。';
		confirmForId = id;
		confirmCfg = {
			title: '遗忘此语句?',
			desc: base,
			run: () => act('遗忘', () => api.post(worldPath('/api/forget'), { ids: [id] }))
		};
		confirmOpen = true;
		// best-effort, non-blocking blast-radius warning (same as the statements page).
		api
			.get<CascadePreview>(worldPath(`/api/cascade_preview/${encodeURIComponent(id)}`))
			.then((preview) => {
				if (!confirmOpen || confirmForId !== id || preview.affected_count === 0) return;
				const sample = preview.affected
					.slice(0, 5)
					.map((a) => `${a.subject_id} ${a.predicate} ${a.object_value}`)
					.join('；');
				const more = preview.affected_count > 5 ? ` 等 ${preview.affected_count} 条` : '';
				confirmCfg = {
					...confirmCfg!,
					desc:
						base +
						` ⚠ 有 ${preview.affected_count}${preview.truncated ? '+' : ''} 条记忆派生自它（${sample}${more}）;` +
						'遗忘不会级联删除它们,但它们会成为孤儿派生（derived_from 指向已遗忘的源）。'
				};
			})
			.catch(() => {
				/* 预览 best-effort:取不到就保持纯确认框 */
			});
	}
</script>

<PageHeader title="冲突探针" subtitle="ConflictProbe:互斥语句对,按冲突权重降序。" />

{#if q.error}
	<EmptyState title="加载失败" description={q.error.message} />
{:else if q.loading && !q.data}
	<div class="space-y-2">{#each Array(4) as _}<Skeleton class="h-12 w-full" />{/each}</div>
{:else}
	{#if q.data}
		<div class="mb-4 flex flex-wrap gap-2">
			{#each Object.entries(q.data.by_kind) as [k, v]}
				<!-- CONFLICTS_WITH 计数取过滤后的实际可处理数(sorted),与下方列表一致;
				     by_kind 是原始边 COUNT(含已遗忘侧),直接显示会比列表多。 -->
				<Badge tone={k === 'CONFLICTS_WITH' ? 'danger' : 'neutral'}>
					{k}: {k === 'CONFLICTS_WITH' ? sorted.length : v}
				</Badge>
			{/each}
		</div>
	{/if}

	{#if sorted.length === 0}
		<EmptyState title="无冲突" description="没有 CONFLICTS_WITH 边。" />
	{:else}
		<Card>
			<ul class="divide-y divide-border">
				{#each sorted as c}
					<li>
						<button
							type="button"
							onclick={() => openDetail(c)}
							class="flex w-full items-center gap-3 py-2.5 text-left transition hover:opacity-80"
						>
							<div class="min-w-0 flex-1">
								<div class="truncate text-sm text-fg">
									<span class="font-medium"
										>{label(c.src_subject, c.src_predicate, c.src_object, c.src_id.slice(0, 8))}</span
									>
									<span class="text-subtle"> ⚔ </span>
									<span>{label(c.dst_subject, c.dst_predicate, c.dst_object, c.dst_id.slice(0, 8))}</span>
								</div>
								{#if reason(c.metadata_json)}
									<div class="mt-0.5 truncate text-xs text-subtle">{reason(c.metadata_json)}</div>
								{/if}
							</div>
							<div class="flex w-40 shrink-0 items-center gap-2">
								<div class="h-2 flex-1 overflow-hidden rounded-full bg-surface">
									<div
										class="h-full rounded-full bg-brand"
										style="width: {barWidth(c.weight, maxWeight)}"
									></div>
								</div>
								<span class="w-9 text-right text-xs tabular-nums text-muted">{c.weight}</span>
							</div>
						</button>
					</li>
				{/each}
			</ul>
		</Card>
	{/if}
{/if}

<Drawer bind:open={detailOpen} title="冲突详情">
	{#if detail}
		<div class="space-y-3 text-sm">
			<div>
				<div class="text-xs uppercase tracking-wide text-subtle">权重</div>
				<div class="text-fg">{detail.weight}</div>
			</div>
			{#if reason(detail.metadata_json)}
				<div>
					<div class="text-xs uppercase tracking-wide text-subtle">原因</div>
					<div class="text-fg">{reason(detail.metadata_json)}</div>
				</div>
			{/if}
			<div class="rounded-control border border-border bg-surface p-3">
				<div class="text-xs uppercase tracking-wide text-subtle">语句 A（src）</div>
				<div class="mt-1 text-fg">
					{label(detail.src_subject, detail.src_predicate, detail.src_object, '（已删除或缺失）')}
				</div>
				<div class="mt-1 break-all font-mono text-xs text-subtle">{detail.src_id}</div>
				<div class="mt-2 flex gap-2">
					{#if detail.src_state === 'consolidated'}
						<Button variant="soft" disabled={busy} onclick={() => reconsolidate(detail!.src_id)}>
							再固化
						</Button>
					{/if}
					<Button
						variant="ghost"
						disabled={busy || missing(detail.src_subject, detail.src_predicate, detail.src_object)}
						onclick={() => askForget(detail!.src_id)}
					>
						遗忘
					</Button>
				</div>
			</div>
			<div class="rounded-control border border-border bg-surface p-3">
				<div class="text-xs uppercase tracking-wide text-subtle">语句 B（dst）</div>
				<div class="mt-1 text-fg">
					{label(detail.dst_subject, detail.dst_predicate, detail.dst_object, '（已删除或缺失）')}
				</div>
				<div class="mt-1 break-all font-mono text-xs text-subtle">{detail.dst_id}</div>
				<div class="mt-2 flex gap-2">
					{#if detail.dst_state === 'consolidated'}
						<Button variant="soft" disabled={busy} onclick={() => reconsolidate(detail!.dst_id)}>
							再固化
						</Button>
					{/if}
					<Button
						variant="ghost"
						disabled={busy || missing(detail.dst_subject, detail.dst_predicate, detail.dst_object)}
						onclick={() => askForget(detail!.dst_id)}
					>
						遗忘
					</Button>
				</div>
			</div>
			<p class="text-xs text-subtle">
				就地干预:再固化开可塑窗重评,遗忘逻辑删除一侧。仲裁(指定胜者)仍由后台引擎处理。
			</p>
		</div>
	{/if}
</Drawer>

<ConfirmDialog
	bind:open={confirmOpen}
	title={confirmCfg?.title ?? ''}
	description={confirmCfg?.desc ?? ''}
	danger
	onconfirm={() => confirmCfg?.run()}
/>
