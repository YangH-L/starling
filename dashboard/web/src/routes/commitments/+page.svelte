<script lang="ts">
	import { describeError } from '$lib/worlds';
	import { api, ApiError, worldPath } from '$lib/api';
	import { byDeadline, deriveFired, triggersFor, triggerKindLabel, describeTrigger } from '$lib/commitments';
	import { createQuery } from '$lib/query.svelte';
	import { lastWsEvent } from '$lib/health';
	import { refetchOnEvent } from '$lib/world.svelte';
	import { toast } from '$lib/ui/toast';
	import { labelFor, glossFor, sectionize } from '$lib/labels';
	import PageHeader from '$lib/components/PageHeader.svelte';
	import { Badge, Card, EmptyState, Skeleton, Input, Drawer, Button, ConfirmDialog } from '$lib/components/ui';

	type CommitmentRow = {
		stmt_id: string;
		state: string;
		subject_id: string;
		predicate: string;
		object_value: string;
		broken_count: number;
		deadline?: string | null;
		created_at: string; // /api/commitments 一直返回它,补齐类型(此前 drawer 靠 Object.entries 才看得到)
		updated_at: string;
	};
	type Trigger = { commitment_stmt_id: string; kind?: string; status: string; spec_json?: string };

	const STATES = ['created', 'ACTIVE', 'FULFILLED', 'BROKEN', 'RENEGOTIATED', 'WITHDRAWN'];

	const q = createQuery(() =>
		api.get<{ rows: CommitmentRow[]; triggers: Trigger[] }>(worldPath('/api/commitments'))
	);
	$effect(() => {
		q.refetch();
	});
	// T8 review I2 — 六态看板不止被 commitment_* 与 tick 改:承诺的「创建」走的是写入路径。
	// run_post_write 每次写入都跑 policy_engine(subscriber_pump.cpp 第 6 项)→
	// create_from_statement → upsert_active_commitment,故新许下的承诺是随 statement_added 落库的。
	// 原先只订 commitment_* + tick,新承诺要等下一次 tick(最多 30s)才出现在泳道上。
	$effect(() => {
		if (refetchOnEvent($lastWsEvent)) q.refetch();
	});

	let filter = $state('');
	let firedSet = $derived(deriveFired(q.data?.triggers));
	let rows = $derived(
		(q.data?.rows ?? [])
			.map((r) => ({ ...r, fired: firedSet.has(r.stmt_id) }))
			.filter((r) => {
				const f = filter.trim().toLowerCase();
				if (!f) return true;
				return (
					(r.subject_id ?? '').toLowerCase().includes(f) ||
					(r.predicate ?? '').toLowerCase().includes(f) ||
					(r.object_value ?? '').toLowerCase().includes(f)
				);
			})
	);
	let byState = $derived(
		STATES.map((s) => ({
			s,
			rows: rows.filter((r) => r.state === s).sort(byDeadline)
		}))
	);

	let detailOpen = $state(false);
	let detail = $state<(CommitmentRow & { fired: boolean }) | null>(null);
	function openDetail(r: CommitmentRow & { fired: boolean }) {
		detail = r;
		detailOpen = true;
	}
	const fmtv = (v: unknown) => (v == null || v === '' ? '—' : String(v));

	// ── T6 detail drawer 策展 ────────────────────────────────────────────────
	// 原先是裸 Object.entries dump。承诺字段少,分两区:核心(承诺本体:谁承诺了什么)
	// 与 元数据 / 时间(履约进度与时点)。没被收编的 key 由 sectionize 落进「其它」
	// 兜底区 —— 不丢字段。触发器区块与 ACTIVE 操作区在下方原样保留。
	const COMMITMENT_CORE = ['stmt_id', 'state', 'subject_id', 'predicate', 'object_value'];
	const COMMITMENT_META = ['broken_count', 'fired', 'deadline', 'created_at', 'updated_at'];
	let sections = $derived(
		sectionize(detail, [
			{ title: '核心', keys: COMMITMENT_CORE },
			{ title: '元数据与时间', keys: COMMITMENT_META }
		])
	);

	// 六态机的语义色:与总览看板一致 —— BROKEN 红、ACTIVE 品牌色,其余克制。
	// 状态值保持后端原样(与看板泳道标题一一对应),只有 label 是中文。
	type Tone = 'neutral' | 'brand' | 'success' | 'warn' | 'danger' | 'info';
	const STATE_TONES: Record<string, Tone> = {
		created: 'neutral',
		ACTIVE: 'brand',
		FULFILLED: 'success',
		BROKEN: 'danger',
		RENEGOTIATED: 'warn',
		WITHDRAWN: 'neutral'
	};

	// #39 片6 手动流转(仅对 ACTIVE 有效):fulfill=标记完成;withdraw=撤回(释放保护,需确认)。
	// 核心 ACTIVE 守卫,非 ACTIVE → 路由 409 → 提示并刷新(可能已被后台 tick 改状态)。
	// 全经单写者命令漏斗(POST /api/commitment/*),成功后重取刷新看板。
	let busy = $state(false);
	let confirmOpen = $state(false);
	let confirmCfg = $state<{ title: string; desc: string; run: () => Promise<void> } | null>(null);

	async function transition(stmtId: string, kind: 'fulfill' | 'withdraw') {
		busy = true;
		try {
			await api.post(worldPath(`/api/commitment/${kind}`), { stmt_id: stmtId });
			toast.success(kind === 'fulfill' ? '已标记完成' : '已撤回');
			detailOpen = false;
			await q.refetch();
		} catch (e) {
			if (e instanceof ApiError && e.status === 409) {
				// 409 同时覆盖「已非 ACTIVE(后台 tick 改了状态)」与「该承诺不存在」两种 no-op。
				toast.error('无法流转:该承诺已非 ACTIVE 或不存在(可能已被后台 tick 改变);已刷新');
				detailOpen = false;
				await q.refetch();
			} else {
				toast.error(`流转失败:${describeError(e)}`);
			}
		} finally {
			busy = false;
		}
	}

	function askWithdraw(stmtId: string) {
		confirmCfg = {
			title: '撤回此承诺?',
			desc: '承诺 → WITHDRAWN 并释放保护:它保护的记忆此后按正常衰减、可能被遗忘。仅对 ACTIVE 有效,不可逆。',
			run: () => transition(stmtId, 'withdraw')
		};
		confirmOpen = true;
	}
</script>

<PageHeader title="承诺状态机" subtitle="Commitment:created → ACTIVE → 终态;⚠ DUE 与逾期醒目。" />
<div class="mb-4 max-w-xs">
	<Input bind:value={filter} placeholder="筛选 subject / predicate / object…" aria-label="筛选承诺" />
</div>

{#if q.error}
	<EmptyState title="加载失败" description={describeError(q.error)} />
{:else if q.loading && !q.data}
	<div class="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-3">
		{#each Array(6) as _}<Skeleton class="h-32 w-full" />{/each}
	</div>
{:else}
	<div class="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-3">
		{#each byState as lane}
			<Card title="{lane.s} ({lane.rows.length})">
				{#if lane.rows.length === 0}
					<p class="text-sm text-muted">—</p>
				{:else}
					<ul class="space-y-2">
						{#each lane.rows as r}
							<li>
								<button
									type="button"
									onclick={() => openDetail(r)}
									class="w-full rounded-control border border-border bg-surface px-3 py-2 text-left text-xs transition hover:border-brand/40"
								>
									<div class="flex items-start justify-between gap-2">
										<span class="font-medium text-fg">{r.subject_id}</span>
										<div class="flex shrink-0 gap-1">
											{#if r.broken_count > 0}<Badge tone="danger">×{r.broken_count}</Badge>{/if}
											{#if r.fired}<Badge tone="warn">⚠ DUE</Badge>{/if}
										</div>
									</div>
									<div class="mt-0.5 text-muted">
										{r.predicate} <span class="text-subtle">→</span> {r.object_value}
									</div>
									{#if r.deadline}
										<div class="mt-1 text-subtle">deadline: {r.deadline}</div>
									{/if}
								</button>
							</li>
						{/each}
					</ul>
				{/if}
			</Card>
		{/each}
	</div>
{/if}

<Drawer bind:open={detailOpen} title="承诺详情">
	{#if detail}
		<div class="space-y-3">
			{#each sections as section}
				<div class="border-t border-border pt-3 first:border-t-0 first:pt-0">
					<p class="mb-1.5 text-xs uppercase tracking-wide text-subtle">{section.title}</p>
					<dl class="space-y-2 text-sm">
						{#each section.entries as [k, v]}
							<div>
								<dt class="text-xs text-muted" title={glossFor(k)}>{labelFor(k)}</dt>
								{#if k === 'state'}
									<dd><Badge tone={STATE_TONES[String(v)] ?? 'neutral'}>{String(v)}</Badge></dd>
								{:else if k === 'fired'}
									<dd>
										{#if v}<Badge tone="warn">⚠ DUE</Badge>{:else}<span class="text-subtle">否</span>{/if}
									</dd>
								{:else if k === 'broken_count' && Number(v) > 0}
									<dd><Badge tone="danger">×{String(v)}</Badge></dd>
								{:else}
									<dd class="break-words text-fg">{fmtv(v)}</dd>
								{/if}
							</div>
						{/each}
					</dl>
				</div>
			{/each}
		</div>
		<!-- whole-branch review Minor(b) — 原先是 `broken_count >= 3` 且不看 state,两处错:
		     (1) 硬编码的 3 是 C++ 的 kMaxBrokenCount(commitment_engine.hpp:10),把内核的
		         策略阈值抄进了前端 —— dashboard 只做只读检视,不复刻策略;
		     (2) C++ 要 state=="ACTIVE" 且 broken_count>=阈值 才自动撤回
		         (commitment_engine.cpp:315-318),不看 state 的话,一条 FULFILLED 的承诺会在
		         同一个抽屉里同时显示「已完成」和「可能已被自动撤回」,自相矛盾。
		     改为只对已是 WITHDRAWN 的行做「解释」而非「预测」:不再需要任何阈值。 -->
		{#if detail.state === 'WITHDRAWN' && detail.broken_count > 0}
			<p class="mt-3 rounded-control border border-warn/40 bg-warn/10 px-3 py-2 text-xs text-warn">
				违约累计 {detail.broken_count} 次:此承诺可能是被后台 auto-withdraw 自动撤回的,而非手动撤回。
			</p>
		{/if}
		{#key detail.stmt_id}
			{@const trigs = triggersFor(q.data?.triggers, detail.stmt_id)}
			{#if trigs.length}
				<div class="mt-4 border-t border-border pt-3">
					<p class="mb-1.5 text-xs uppercase tracking-wide text-subtle">触发器({trigs.length})</p>
					<ul class="space-y-1.5">
						{#each trigs as t}
							<li class="flex items-start gap-2 text-xs">
								<Badge tone={t.status === 'fired' ? 'warn' : t.status === 'cleared' ? 'neutral' : 'brand'}>
									{triggerKindLabel(t.kind)}
								</Badge>
								<span class="flex-1 text-muted">{describeTrigger(t)}</span>
								<span class="shrink-0 text-subtle">{t.status}</span>
							</li>
						{/each}
					</ul>
				</div>
			{/if}
		{/key}
		{#if detail.state === 'ACTIVE'}
			<div class="mt-4 flex gap-2 border-t border-border pt-4">
				<Button variant="soft" disabled={busy} onclick={() => transition(detail!.stmt_id, 'fulfill')}>
					标记完成
				</Button>
				<Button variant="danger" disabled={busy} onclick={() => askWithdraw(detail!.stmt_id)}>
					撤回
				</Button>
			</div>
			<p class="mt-2 text-xs text-subtle">
				手动流转仅对 ACTIVE 承诺有效;fire / break / auto-withdraw 由后台 tick 自动处理。
			</p>
		{/if}
	{/if}
</Drawer>

<ConfirmDialog
	bind:open={confirmOpen}
	title={confirmCfg?.title ?? ''}
	description={confirmCfg?.desc ?? ''}
	onconfirm={() => confirmCfg?.run()}
/>
