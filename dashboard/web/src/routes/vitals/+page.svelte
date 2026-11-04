<script lang="ts">
	import { describeError } from '$lib/worlds';
	import { api, worldPath } from '$lib/api';
	import { createQuery } from '$lib/query.svelte';
	import { lastWsEvent } from '$lib/health';
	import { refetchOnEvent, withWorld } from '$lib/world.svelte';
	import { allHealthy, lagTone, type VitalsResponse } from '$lib/vitals';
	import PageHeader from '$lib/components/PageHeader.svelte';
	import StatCard from '$lib/components/StatCard.svelte';
	import { Card, Badge, Skeleton, EmptyState, StatusDot } from '$lib/components/ui';

	// Decision A (/plan-design-review): reuse createQuery + the existing WS tick
	// event to refetch. No new WS event types, no realtime.py change.
	const q = createQuery(() => api.get<VitalsResponse>(worldPath('/api/vitals')));
	$effect(() => {
		q.refetch();
	});
	$effect(() => {
		if (refetchOnEvent($lastWsEvent)) q.refetch();
	});

	const healthy = $derived(q.data != null && allHealthy(q.data));
	// danger only when there's actually something wrong — keep the teal calm.
	const countTone = (n: number, warn = false): 'default' | 'warn' | 'danger' =>
		n === 0 ? 'default' : warn ? 'warn' : 'danger';
	const badgeTone = (t: string): 'neutral' | 'success' | 'warn' | 'danger' =>
		t === 'success' ? 'success' : t === 'warn' ? 'warn' : t === 'danger' ? 'danger' : 'neutral';
</script>

<PageHeader
	title="生命体征 / Vitals"
	subtitle="管线滞后与生命周期诊断:每订阅泵 outbox 滞后、VOLATILE 卡住、抽取失败、超期再固化窗口。"
/>

{#if q.error}
	<EmptyState title="加载失败" description={describeError(q.error)} />
{:else if q.loading && !q.data}
	<div class="grid grid-cols-2 gap-3 md:grid-cols-4">
		{#each Array(4) as _}<Skeleton class="h-20 w-full" />{/each}
	</div>
{:else if q.data}
	<div class="space-y-6">
		<!-- 头条指标(克制:2-3 个,非装饰性网格) -->
		<div class="grid grid-cols-2 gap-3 md:grid-cols-4">
			<StatCard label="max pump lag" value={q.data.max_lag} hint="最滞后泵的 outbox 落后量"
				tone={countTone(q.data.max_lag, true)} />
			<StatCard label="volatile stuck" value={q.data.volatile_stuck_total} hint="未固化语句"
				tone={countTone(q.data.volatile_stuck_total, true)} />
			<StatCard label="extraction failures" value={q.data.extraction_failures_total}
				hint="抽取失败 attempt" tone={countTone(q.data.extraction_failures_total)} />
			<StatCard label="overdue windows" value={q.data.overdue_windows_total}
				hint="超期再固化窗口" tone={countTone(q.data.overdue_windows_total)} />
		</div>

		{#if healthy}
			<!-- 肯定式全健康态(不是空白):给安心感 -->
			<Card title="一切正常">
				<div class="flex items-center gap-2 py-2">
					<StatusDot tone="ok" label="无泵滞后 · 无 VOLATILE 卡住 · 无抽取失败 · 无超期窗口" />
				</div>
			</Card>
		{/if}

		<!-- 订阅泵 outbox 滞后(全局信号) -->
		<Card
			title="订阅泵 outbox 滞后"
			description="全局信号:MAX(outbox_sequence, head={q.data.outbox_head}) − 各泵 checkpoint 游标。嵌入单进程模式下 = 距上次 tick 处理事件的距离,非分布式队列深度。"
		>
			<ul class="divide-y divide-border">
				{#each q.data.lag as l}
					{@const t = lagTone(l.lag)}
					<li class="flex items-center justify-between gap-3 py-2">
						<span class="font-mono text-sm text-fg">{l.pump}</span>
						<span class="flex items-center gap-3">
							<span class="text-xs tabular-nums text-muted">
								{#if l.ok}cursor {l.cursor} / head {l.head}{:else}—{/if}
							</span>
							<Badge tone={badgeTone(t.tone)}>
								{l.lag === null ? t.label : `lag ${l.lag} · ${t.label}`}
							</Badge>
						</span>
					</li>
				{/each}
			</ul>
		</Card>

		<!-- VOLATILE 卡住 -->
		{#if q.data.volatile_stuck.length}
			<Card
				title="VOLATILE 卡住 ({q.data.volatile_stuck_total})"
				description="salience < 0.01 永不被回放采样;年龄 > 7 天会被 TTL 归档。"
			>
				<ul class="divide-y divide-border">
					{#each q.data.volatile_stuck as s}
						<li class="flex items-center justify-between gap-3 py-2 text-sm">
							<a href={withWorld('/statements')} title={s.id} class="flex min-w-0 items-center gap-2">
								<Badge tone="brand">{s.id.slice(0, 8)}…</Badge>
								<span class="truncate">
									{s.subject_id} <span class="text-subtle">{s.predicate}</span> {s.object_value}
								</span>
							</a>
							<span class="shrink-0 text-xs tabular-nums text-muted">
								sal {s.salience.toFixed(4)} · replay {s.replay_count}
							</span>
						</li>
					{/each}
				</ul>
			</Card>
		{/if}

		<!-- 抽取失败(error + raw LLM 输出可展开) -->
		{#if q.data.extraction_failures.length}
			<Card title="抽取失败 ({q.data.extraction_failures_total})" description="extraction_attempt.status='failed';展开看原始 LLM 输出。">
				<ul class="space-y-2">
					{#each q.data.extraction_failures as f}
						<li class="rounded-control border border-danger/40 bg-surface px-3 py-2">
							<div class="flex items-center justify-between gap-2 text-sm">
								<span class="truncate text-danger">{f.error ?? '(no error)'}</span>
								<span class="shrink-0 text-xs tabular-nums text-muted">attempt {f.attempt_number}</span>
							</div>
							<div class="mt-0.5 truncate font-mono text-xs text-subtle">span {f.extraction_span_key}</div>
							{#if f.raw_output}
								<details class="mt-1 text-xs">
									<summary class="cursor-pointer select-none text-muted">原始 LLM 输出</summary>
									<pre class="mt-1 overflow-x-auto whitespace-pre-wrap rounded bg-bg p-2 font-mono text-subtle">{f.raw_output}</pre>
								</details>
							{/if}
						</li>
					{/each}
				</ul>
			</Card>
		{/if}

		<!-- 历史成本(0027):租户级 token 汇总 + 近 N 次 run 逐次成本。成本在适配器核心采集,此处只读。 -->
		{#if q.data.extraction_cost.attempts > 0}
			<Card
				title="历史成本 / Cost"
				description="extraction_attempt 的 token 用量 + 往返时延汇总({q.data.extraction_cost.attempts} attempt)。成本在适配器(核心)采集,此处只读;fake/未采集 usage 的端点记 0。"
			>
				<div class="grid grid-cols-2 gap-3 md:grid-cols-4">
					<StatCard label="total tokens" value={q.data.extraction_cost.total_tokens}
						hint="累计 token" />
					<StatCard label="prompt tokens" value={q.data.extraction_cost.prompt_tokens} />
					<StatCard label="completion tokens" value={q.data.extraction_cost.completion_tokens} />
					<StatCard label="total latency" value={q.data.extraction_cost.latency_ms}
						hint="ms · 累计往返耗时" />
				</div>
				{#if q.data.extraction_cost_runs.length}
					<ul class="mt-3 divide-y divide-border">
						{#each q.data.extraction_cost_runs as r}
							<li class="flex items-center justify-between gap-3 py-2 text-sm">
								<span class="flex min-w-0 items-center gap-2">
									<Badge tone="brand">{r.run_id.slice(0, 8)}…</Badge>
									<span class="truncate text-xs text-muted">{r.started_at ?? '—'}</span>
								</span>
								<span class="shrink-0 text-xs tabular-nums text-muted">
									{r.total_tokens} tok · {r.latency_ms} ms · {r.attempts} attempt
								</span>
							</li>
						{/each}
					</ul>
				{/if}
			</Card>
		{/if}

		<!-- 超期再固化窗口 -->
		{#if q.data.overdue_windows.length}
			<Card title="超期再固化窗口 ({q.data.overdue_windows_total})" description="status='open' 且 close_deadline 已过——可能卡在仲裁。">
				<ul class="divide-y divide-border">
					{#each q.data.overdue_windows as w}
						<li class="flex items-center justify-between gap-3 py-2 text-sm">
							<a href={withWorld('/statements')} title={w.stmt_id}><Badge tone="brand">{w.stmt_id.slice(0, 8)}…</Badge></a>
							<span class="shrink-0 text-xs tabular-nums text-muted">deadline {w.close_deadline}</span>
						</li>
					{/each}
				</ul>
			</Card>
		{/if}
	</div>
{/if}
