<script lang="ts">
	import { untrack } from 'svelte';
	import { createQuery } from '$lib/query.svelte';
	import { lastWsEvent } from '$lib/health';
	import { shouldRefetch } from '$lib/feed';
	import { currentWorld } from '$lib/world.svelte';
	import PageHeader from '$lib/components/PageHeader.svelte';
	import WorldRollbackDrawer from '$lib/components/WorldRollbackDrawer.svelte';
	import { Button, EmptyState, Skeleton } from '$lib/components/ui';
	import {
		apiDetail,
		shortId,
		worldErrorMessage,
		worldsApi,
		type SessionList,
		type SessionRow
	} from '$lib/worlds';

	// W4 — 会话清单:按 session_id 汇总 source_unit_meta(HTTP 契约见 $lib/worlds)。
	// 会话只是元数据,不切断召回;删除一个会话的写入只能走快照回滚:
	// 「回滚到此会话之前」= 回滚到该会话开始前自动拍的那份快照(语义 A,丢掉其后全部写入)。
	const SESSION_LIMIT = 200;
	const NO_ANCHOR_HINT = '未开启按会话快照、快照失败，或已被保留策略清理';

	// 选中的世界就是顶栏选的那个(URL ?world=),本页不再有自己的下拉。
	// 回滚抽屉要这个世界的一行(是否开着、名字);取详情而不是列表的第一页,世界再多也取得到。
	let selected = $derived(currentWorld());
	const worldsQ = createQuery(() => worldsApi.detail(selected));
	$effect(() => {
		void selected;
		worldsQ.refetch();
	});
	let worlds = $derived(worldsQ.data ? [worldsQ.data] : []);

	let sessions = $state<SessionList | null>(null);
	let sessionsErr = $state('');
	let sessReq = 0; // 防陈旧:换世界后,只让最新一次的清单落地

	async function loadSessions(name: string) {
		const mine = ++sessReq;
		sessionsErr = '';
		try {
			const r = await worldsApi.listSessions(name, SESSION_LIMIT);
			if (mine !== sessReq) return;
			sessions = r;
		} catch (e) {
			if (mine !== sessReq) return;
			sessions = null;
			sessionsErr = worldErrorMessage(apiDetail(e));
		}
	}

	// 选中世界变化时取该世界的清单。
	$effect(() => {
		const name = selected;
		if (!name) return;
		untrack(() => {
			sessions = null;
			void loadSessions(name);
		});
	});

	// 当前世界有写入或回滚时一并重取世界行与清单(recall 是纯读,不重取)。
	$effect(() => {
		if (!shouldRefetch($lastWsEvent, untrack(() => selected))) return;
		untrack(() => {
			void worldsQ.refetch();
			if (selected) void loadSessions(selected);
		});
	});

	// 清单是当前选中世界的(换世界后旧清单先清空,不会串到别的世界上)。
	let rows = $derived(sessions && sessions.world === selected ? sessions.sessions : []);
	let current = $derived(sessions && sessions.world === selected ? sessions : null);

	const fmt = (v: string | null) => (v ? v : '—');
	// 有锚点 = snapshot_id 非空。C++ 用空串表示无锚点;路由应映射为 null,这里再防一道,
	// 免得给空 id 渲染回滚按钮。
	const anchorOf = (s: SessionRow) => (s.anchor?.snapshot_id ? s.anchor : null);

	// ── 回滚到此会话之前 ──────────────────────────────────────────────────
	let rollbackOpen = $state(false);
	let rollbackBusy = $state(false);
	let rollbackTarget = $state<{ world: string; session: SessionRow } | null>(null);
	let rollbackRow = $derived.by(() => {
		const t = rollbackTarget;
		return t ? worlds.find((w) => w.name === t.world) : undefined;
	});
	let rollbackSnap = $derived.by(() => {
		const a = rollbackTarget ? anchorOf(rollbackTarget.session) : null;
		return a ? { id: a.snapshot_id, created_at: a.created_at } : null;
	});
	let rollbackSummary = $derived.by(() => {
		const t = rollbackTarget;
		const a = t ? anchorOf(t.session) : null;
		if (!t || !a) return '';
		return `${a.snapshot_id} · ${a.created_at} · 会话 ${t.session.session_id} 开始前`;
	});

	function askRollback(s: SessionRow) {
		if (!selected || !anchorOf(s)) return;
		rollbackTarget = { world: selected, session: s };
		rollbackOpen = true;
	}
</script>

<PageHeader
	title="会话"
	subtitle="按 session_id 汇总的会话清单。会话只是元数据，不切断召回；要撤掉一个会话及其之后的写入，回滚到它开始前的快照；之后的会话也会一并丢弃。"
/>

{#if worldsQ.error && !worldsQ.data}
	<EmptyState title="加载失败" description={worldErrorMessage(apiDetail(worldsQ.error))}>
		<Button variant="soft" onclick={() => void worldsQ.refetch()}>重试</Button>
	</EmptyState>
{:else if !worldsQ.data}
	<div class="space-y-2">{#each Array(3) as _}<Skeleton class="h-10 w-full" />{/each}</div>
{:else}
	<p class="mb-3 text-sm text-muted">世界：<span class="font-medium text-fg">{selected}</span>（在顶栏切换）</p>

	{#if current && !current.source_lane_enabled}
		<p
			role="status"
			class="mb-3 rounded-control border border-warn/40 bg-warn/10 px-3 py-2 text-xs text-warn"
		>
			左脑 lane 未开启：新的写入不会记录会话
		</p>
	{/if}

	{#if sessionsErr}
		<EmptyState title="会话清单加载失败" description={sessionsErr}>
			<Button variant="soft" onclick={() => void loadSessions(selected)}>重试</Button>
		</EmptyState>
	{:else if !current}
		<div class="space-y-2">{#each Array(3) as _}<Skeleton class="h-10 w-full" />{/each}</div>
	{:else if rows.length === 0}
		<EmptyState title="这个世界还没有会话记录" />
	{:else}
		{#if current.truncated}
			<p class="mb-2 text-xs text-subtle">
				只显示最近 {rows.length} 个会话，共 {current.total} 个
			</p>
		{/if}
		<div class="overflow-x-auto rounded-card border border-border">
			<table class="w-full text-sm">
				<caption class="sr-only">{selected} 的会话清单</caption>
				<thead class="bg-surface text-left">
					<tr>
						{#each ['会话', '话轮数', '说话人数', '开始', '最近', '回滚锚点'] as h}
							<th scope="col" class="px-3 py-2 font-medium whitespace-nowrap text-muted">{h}</th>
						{/each}
					</tr>
				</thead>
				<tbody>
					{#each rows as s (s.session_id)}
						{@const anchor = anchorOf(s)}
						<tr class="border-t border-border/60 align-top">
							<th scope="row" class="px-3 py-2 text-left font-normal">
								<span class="font-mono text-xs text-fg" title={s.session_id}
									>{shortId(s.session_id, 12)}</span
								>
							</th>
							<td class="px-3 py-2 tabular-nums text-fg">{s.units}</td>
							<td class="px-3 py-2 tabular-nums text-fg">{s.speakers}</td>
							<td class="px-3 py-2 tabular-nums whitespace-nowrap text-muted">{fmt(s.first_at)}</td>
							<td class="px-3 py-2 tabular-nums whitespace-nowrap text-muted">{fmt(s.last_at)}</td>
							<td class="px-3 py-2">
								{#if anchor}
									<div class="flex flex-wrap items-center gap-2">
										<span class="font-mono text-xs break-all text-muted" title={anchor.created_at}
											>{anchor.snapshot_id}</span
										>
										<Button
											variant="danger"
											aria-label={`回滚到此会话之前 ${s.session_id}`}
											loading={rollbackBusy && rollbackTarget?.session.session_id === s.session_id}
											disabled={rollbackBusy}
											onclick={() => askRollback(s)}>回滚到此会话之前</Button
										>
									</div>
								{:else}
									<!-- title 只照顾鼠标悬停;读屏与触屏用户读 sr-only 那句。 -->
									<span class="text-xs text-subtle" title={NO_ANCHOR_HINT}>无</span><span
										class="sr-only">（{NO_ANCHOR_HINT}）</span
									>
								{/if}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
		{#if current.null_observed > 0}
			<p class="mt-2 text-xs text-subtle">
				这个世界有 {current.null_observed} 个话轮没有记录时间，计入话轮数，但不计入开始/最近。
			</p>
		{/if}
	{/if}
{/if}

<WorldRollbackDrawer
	bind:open={rollbackOpen}
	bind:busy={rollbackBusy}
	worldName={rollbackTarget?.world ?? ''}
	row={rollbackRow}
	snapshot={rollbackSnap}
	summary={rollbackSummary}
	onsuccess={() => {
		void worldsQ.refetch();
		if (selected) void loadSessions(selected);
	}}
/>
