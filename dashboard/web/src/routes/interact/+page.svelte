<script lang="ts">
	import { describeError } from '$lib/worlds';
	import { withWorld } from '$lib/world.svelte';
	import { api, type PlannedRecallResponse, worldPath } from '$lib/api';
	import { toast } from '$lib/ui/toast';
	import PageHeader from '$lib/components/PageHeader.svelte';
	import { Button, Textarea, Input, Badge, Card, Chip, EmptyState, Select } from '$lib/components/ui';
	import ScoreBreakdown from '$lib/components/ScoreBreakdown.svelte';

	let text = $state('');
	let holder = $state('');
	let interlocutor = $state('');
	let query = $state('');
	let mode = $state('semantic');
	let target = $state('');
	let remembered = $state<string[]>([]);
	let outcome = $state('');
	let results = $state<
		{ subject: string; predicate: string; object: string; score: number; label?: string }[]
	>([]);
	let planned = $state<PlannedRecallResponse | null>(null);
	let recalled = $state(false);
	let busyR = $state(false);
	let busyQ = $state(false);
	// 持久错误面(T12):toast 转瞬即逝,失败后页面上不留痕。两条命令各留一块错误面,
	// 且错误优先于上一次的结果 —— 否则失败后旧结果仍在,像是本次的答案。
	let rememberErr = $state('');
	let recallErr = $state('');

	// Per-turn model selection (#35): pick a registry provider for remember's
	// extraction, or '' to use the bound extraction role. (Recall uses the embedder,
	// which is bound — changing it re-embeds — so it has no per-turn override.)
	let providers = $state<string[]>([]);
	let extractProvider = $state('');
	$effect(() => {
		api
			.get<{ providers?: Record<string, unknown> }>('/api/config')
			.then((c) => (providers = Object.keys(c.providers ?? {})))
			.catch(() => {});
	});
	const providerOpts = () => [
		{ value: '', label: '模型:默认(extraction 角色)' },
		...providers.map((p) => ({ value: p, label: '模型:' + p }))
	];

	// 'semantic'/'completion' 走原 recall;'intent:*' 走 RetrievalPlanner(P3.a1)。
	const MODE_OPTIONS = [
		{ value: 'semantic', label: '语义检索' },
		{ value: 'completion', label: '模式补全' },
		{ value: 'intent:FACT_LOOKUP', label: '意图:事实' },
		{ value: 'intent:BELIEF_OF_OTHER', label: '意图:他者信念' },
		{ value: 'intent:META_BELIEF', label: '意图:二阶信念' },
		{ value: 'intent:HISTORY', label: '意图:时间线' },
		{ value: 'intent:COMMITMENT_DUE', label: '意图:待办承诺' },
		{ value: 'intent:PREFERENCE', label: '意图:偏好' },
		{ value: 'intent:NORM_LOOKUP', label: '意图:规范' },
		{ value: 'intent:COMMON_GROUND', label: '意图:共识' },
		{ value: 'intent:ABSTAIN_CHECK', label: '意图:拒答检查' }
	];
	const needsTarget = $derived(
		['intent:BELIEF_OF_OTHER', 'intent:COMMON_GROUND', 'intent:PREFERENCE'].includes(mode)
	);

	async function remember() {
		busyR = true;
		try {
			const body: Record<string, unknown> = { text };
			if (holder) body.holder = holder;
			if (interlocutor) body.interlocutor = interlocutor;
			if (extractProvider) body.provider = extractProvider;
			// 真模型抽取实测 20-110s(长文本更慢),放宽到 180s(后端有自己的重试与超时)。
			const r = await api.post<{ statement_ids: string[]; outcome: string }>(worldPath('/api/remember'), body, {
				timeoutMs: 180_000
			});
			remembered = r.statement_ids;
			outcome = r.outcome;
			rememberErr = '';
			toast.success(`outcome: ${r.outcome} · ${r.statement_ids.length} statements`);
		} catch (e) {
			rememberErr = describeError(e);
			toast.error(rememberErr);
		} finally {
			busyR = false;
		}
	}
	async function recall() {
		busyQ = true;
		try {
			if (mode.startsWith('intent:')) {
				const body: Record<string, unknown> = { query, intent: mode.slice(7) };
				if (target.trim()) body.target = target.trim();
				const r = await api.post<PlannedRecallResponse>(worldPath('/api/recall'), body, {
					timeoutMs: 60_000
				});
				planned = r;
				results = r.results;
			} else {
				const r = await api.post<{ results: typeof results }>(
					worldPath('/api/recall'),
					{ query, mode },
					{ timeoutMs: 60_000 } // query embed 走网络,留足重试余量
				);
				planned = null;
				results = r.results;
			}
			recalled = true;
			recallErr = '';
		} catch (e) {
			recallErr = describeError(e);
			toast.error(recallErr);
		} finally {
			busyQ = false;
		}
	}
	const pct = (s: number) => Math.max(2, Math.min(100, s * 100));
</script>

<PageHeader title="交互" subtitle="控制台:remember 写入与 recall 检索。" />
<div class="max-w-2xl space-y-4">
	<Card title="Remember" description="自然语言写入,抽取为 statements。">
		<div class="space-y-2">
			<Textarea bind:value={text} rows={3} placeholder="记一段话…" />
			<div class="flex flex-wrap gap-2">
				<Input bind:value={holder} placeholder="holder (默认 self)" class="max-w-44" />
				<Input bind:value={interlocutor} placeholder="interlocutor (可选)" class="max-w-44" />
				{#if providers.length}
					<Select bind:value={extractProvider} options={providerOpts()} class="max-w-52" aria-label="抽取模型" />
				{/if}
			</div>
			<div class="flex items-center gap-3">
				<Button variant="soft" loading={busyR} disabled={!text.trim()} onclick={remember}>记住</Button>
				{#if !rememberErr && (remembered.length || outcome)}
					<span class="text-xs text-muted">{outcome} · {remembered.length} statements</span>
				{/if}
			</div>
			{#if rememberErr}
				<EmptyState title="写入失败" description={rememberErr}>
					<Button variant="soft" loading={busyR} disabled={!text.trim()} onclick={remember}>
						重试
					</Button>
				</EmptyState>
			{:else if remembered.length}
				<div class="flex flex-wrap gap-1">
					{#each remembered as id}
						<a href={withWorld('/statements')} title={id}><Badge tone="brand">{id.slice(0, 8)}…</Badge></a>
					{/each}
				</div>
			{/if}
		</div>
	</Card>
	<Card title="Recall" description="语义检索、模式补全,或按意图走检索规划(带标签与拒答)。">
		<div class="space-y-3">
			<div class="flex flex-wrap gap-2">
				<Input bind:value={query} placeholder="query" class="min-w-48 flex-1" />
				<Select bind:value={mode} class="max-w-44" aria-label="recall 模式" options={MODE_OPTIONS} />
				{#if needsTarget}
					<Input bind:value={target} placeholder="target(对方)" class="max-w-36" />
				{/if}
				<Button loading={busyQ} disabled={!query.trim()} variant="soft" onclick={recall}>
					检索
				</Button>
			</div>
			{#if recallErr}
				<EmptyState title="检索失败" description={recallErr}>
					<Button variant="soft" loading={busyQ} disabled={!query.trim()} onclick={recall}>
						重试
					</Button>
				</EmptyState>
			{:else if recalled}
				{#if planned?.abstained}
					<EmptyState
						title="主动拒答:{planned.abstention_reason}"
						description={planned.context_pack}
					/>
				{:else if results.length === 0}
					<p class="text-sm text-muted">无召回结果</p>
				{:else}
					{#if planned?.receipt}
						{@const cc = planned.receipt.candidate_counts}
						<div
							class="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-control border border-border bg-surface px-3 py-2 text-xs text-muted"
						>
							<span class="font-medium text-fg">检索健康</span>
							<span
								>本轮取回 <span class="font-mono tabular-nums text-fg">{cc.fetched}</span> → 返回
								<span class="font-mono tabular-nums text-fg">{cc.returned}</span></span
							>
							{#if cc.dropped_by_review}
								<span
									>· review 丢弃 <span class="font-mono tabular-nums text-warn"
										>{cc.dropped_by_review}</span
									></span
								>
							{/if}
							{#if cc.dropped_by_state}
								<span
									>· state 丢弃 <span class="font-mono tabular-nums text-warn"
										>{cc.dropped_by_state}</span
									></span
								>
							{/if}
							{#if cc.dropped_by_time_anchor}
								<span
									>· time 丢弃 <span class="font-mono tabular-nums text-warn"
										>{cc.dropped_by_time_anchor}</span
									></span
								>
							{/if}
							{#if cc.dropped_by_evidence_erasure}
								<span
									>· evidence 丢弃 <span class="font-mono tabular-nums text-warn"
										>{cc.dropped_by_evidence_erasure}</span
									></span
								>
							{/if}
							{#if planned.receipt.degraded_paths.length}
								<span class="text-warn">· 降级 {planned.receipt.degraded_paths.length}</span>
							{/if}
						</div>
					{/if}
					<ul class="space-y-2">
						{#each results as r, i}
							{@const scoreRow = planned?.receipt?.score_breakdown?.[i]}
							<li class="rounded-control border border-border bg-surface px-3 py-2">
								<div class="flex items-center justify-between gap-2">
									<span class="flex min-w-0 items-center gap-2 text-sm text-fg">
										{#if r.label}<Chip>{r.label}</Chip>{/if}
										<span class="truncate">
											{r.subject} <span class="text-subtle">{r.predicate}</span> {r.object}
										</span>
									</span>
									<span class="shrink-0 text-xs tabular-nums text-muted">{r.score.toFixed(3)}</span>
								</div>
								<div class="mt-1 h-1.5 overflow-hidden rounded-full bg-bg">
									<div class="h-full rounded-full bg-brand" style="width: {pct(r.score)}%"></div>
								</div>
								{#if scoreRow}
									<details class="mt-2">
										<summary class="cursor-pointer select-none text-xs text-subtle"
											>归因分解</summary
										>
										<div class="mt-1.5">
											<ScoreBreakdown row={scoreRow} />
										</div>
									</details>
								{/if}
							</li>
						{/each}
					</ul>
				{/if}
				{#if planned && !planned.abstained}
					<details class="text-xs text-muted">
						<summary class="cursor-pointer select-none">规划步骤({planned.scopes_searched.join(' → ') || '—'})</summary>
						<ul class="mt-1 space-y-0.5 font-mono">
							{#each planned.plan_steps as s}
								<li>{s.step}: {s.detail}</li>
							{/each}
						</ul>
					</details>
					{#if planned.receipt}
						{@const rc = planned.receipt}
						<details class="text-xs text-muted">
							<summary class="cursor-pointer select-none">
								归因 receipt(充分性 {rc.sufficiency_status} · 取 {rc.candidate_counts.fetched}→返 {rc.candidate_counts.returned}{rc.frontier_masked_count
									? ` · frontier 屏蔽 ${rc.frontier_masked_count}`
									: ''})
							</summary>
							<div class="mt-1 space-y-2">
								{#if rc.filters_applied.length}
									<div>
										<span class="text-subtle">filters(隐私证明):</span>
										<span class="font-mono">{rc.filters_applied.map((f) => `${f.name}=${f.value}`).join(', ')}</span>
									</div>
								{/if}
								{#if rc.degraded_paths.length}
									<div class="text-warn">
										降级:{rc.degraded_paths.map((d) => `${d.path}→${d.fallback}(${d.reason})`).join('; ')}
									</div>
								{/if}
							</div>
						</details>
					{/if}
				{/if}
			{/if}
		</div>
	</Card>
</div>
