<script lang="ts">
	import { withWorld } from '$lib/world.svelte';
	import { page } from '$app/state';
	import { goto } from '$app/navigation';
	import { api, type ProvenanceNode, type StatementSearchResponse, worldPath } from '$lib/api';
	import PageHeader from '$lib/components/PageHeader.svelte';
	import CodeBlock from '$lib/components/CodeBlock.svelte';
	import { Card, Badge, Button, Input, EmptyState, Skeleton } from '$lib/components/ui';
	import { statusLabel, originLabel, nodeSummary } from '$lib/lens';

	// 取镜:只读文本查找(GET /api/statement_search)。
	let queryText = $state('');
	let searchRows = $state<StatementSearchResponse['rows']>([]);
	let searching = $state(false);
	let searchErr = $state('');

	// 取证树(GET /api/provenance/{id})。
	let tree = $state<ProvenanceNode | null>(null);
	let loading = $state(false);
	let loadErr = $state('');
	let currentId = $state('');

	// 深链:?stmt=<id> 到达直接取证(从 /statements「透视来源」跳来,或可分享/回退)。
	$effect(() => {
		const sid = page.url.searchParams.get('stmt') ?? '';
		if (sid && sid !== currentId) void load(sid);
	});

	async function load(id: string) {
		currentId = id;
		loading = true;
		loadErr = '';
		try {
			tree = await api.get<ProvenanceNode>(worldPath(`/api/provenance/${encodeURIComponent(id)}`));
		} catch (e) {
			tree = null;
			loadErr = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	// 选中 → 反映到 URL(可分享/可回退),effect 据此加载。
	function select(id: string) {
		void goto(withWorld(`/lens?stmt=${encodeURIComponent(id)}`), { keepFocus: true, noScroll: true });
	}

	async function runSearch() {
		const q = queryText.trim();
		if (!q) {
			searchRows = [];
			return;
		}
		searching = true;
		searchErr = '';
		try {
			const r = await api.get<StatementSearchResponse>(
				worldPath(`/api/statement_search?q=${encodeURIComponent(q)}`)
			);
			searchRows = r.rows;
		} catch (e) {
			searchErr = e instanceof Error ? e.message : String(e);
			searchRows = [];
		} finally {
			searching = false;
		}
	}

	const short = (s: string | null | undefined, n = 14) =>
		s ? (s.length > n ? s.slice(0, n) + '…' : s) : '—';
</script>

<!-- 折叠的血缘节点(派生自 / 前身),递归展开自身的派生与前身。 -->
{#snippet lineage(node: ProvenanceNode)}
	<li class="border-l border-border pl-3">
		{#if !node.found}
			<span class="text-xs text-subtle">引用 {short(node.id, 10)} · 不在本租户(孤儿,不展开)</span>
		{:else if node.repeat}
			<span class="text-xs text-subtle">↩ {nodeSummary(node)} · 已在上文展开</span>
		{:else}
			<div class="flex flex-wrap items-center gap-2">
				<span class="text-sm text-fg">{nodeSummary(node)}</span>
				{#if node.origin}
					<Badge tone={statusLabel(node.origin.extraction?.status).tone}>
						{originLabel(node.origin.provenance)}
					</Badge>
				{/if}
				<button class="text-xs text-brand hover:underline" onclick={() => select(node.id)}>
					透视此节点 →
				</button>
			</div>
			{#if node.origin?.extraction?.error}
				<p class="mt-0.5 text-xs text-danger">抽取错误:{node.origin.extraction.error}</p>
			{/if}
			{#if node.supersedes}
				<ul class="mt-1 space-y-1">{@render lineage(node.supersedes)}</ul>
			{/if}
			{#if node.derived_from?.length}
				<ul class="mt-1 space-y-1">
					{#each node.derived_from as c (c.id)}{@render lineage(c)}{/each}
				</ul>
			{/if}
			{#if node.truncated}
				<p class="mt-0.5 text-xs text-subtle">…更深来源已达深度上限,未展开</p>
			{/if}
		{/if}
	</li>
{/snippet}

<PageHeader
	title="透视镜 · Lens"
	subtitle="一条记忆从何而来:创建它的原始 LLM 抽取、所依证据、派生与前身链路。只读检视——不改记忆、不留痕。"
/>

<Card title="取镜" description="按 主语 / 谓词 / 宾语 文本查找一条语句,点击后在下方展开它的来源取证。">
	<form class="flex gap-2" onsubmit={(e) => { e.preventDefault(); void runSearch(); }}>
		<Input bind:value={queryText} placeholder="如 Bob / responsible_for / auth" class="flex-1" />
		<Button variant="soft" loading={searching}>查找</Button>
	</form>
	{#if searchErr}
		<p class="mt-2 text-xs text-danger">{searchErr}</p>
	{:else if searchRows.length}
		<ul class="mt-3 divide-y divide-border">
			{#each searchRows as r (r.id)}
				<li>
					<button
						class="flex w-full items-center justify-between gap-3 rounded-control px-2 py-2 text-left hover:bg-bg"
						onclick={() => select(r.id)}
					>
						<span class="truncate text-sm text-fg">
							{r.subject_id} · {r.predicate} · {r.object_value}
						</span>
						<span class="shrink-0 text-xs text-subtle">{r.consolidation_state}</span>
					</button>
				</li>
			{/each}
		</ul>
	{:else if queryText.trim() && !searching}
		<p class="mt-3 text-xs text-subtle">无匹配语句。</p>
	{/if}
</Card>

{#if loading}
	<Skeleton class="mt-4 h-64 w-full" />
{:else if loadErr}
	<div class="mt-4">
		<!-- 重试须直呼 load:上面的 effect 有 sid !== currentId 守卫,而 currentId 已是这条失败的 id,
		     所以点同一条语句不会重新取数(只能整页刷新)。 -->
		<EmptyState title="无法取证" description={loadErr}>
			<Button variant="soft" onclick={() => void load(currentId)}>重试</Button>
		</EmptyState>
	</div>
{:else if tree}
	{@const root = tree}
	{@const ex = root.origin?.extraction}
	<div class="mt-4 space-y-4">
		<Card>
			<div class="flex flex-wrap items-center gap-2">
				<h2 class="text-base font-semibold text-fg">{nodeSummary(root)}</h2>
				{#if root.origin}<Badge tone="info">{originLabel(root.origin.provenance)}</Badge>{/if}
				{#if root.statement?.consolidation_state}
					<Badge>{String(root.statement.consolidation_state)}</Badge>
				{/if}
				{#if root.statement?.review_status}
					<Badge>{String(root.statement.review_status)}</Badge>
				{/if}
			</div>
			{#if root.statement}
				<dl class="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-3 lg:grid-cols-4">
					{#each [['持有者', root.statement.holder_id], ['置信度', root.statement.confidence], ['显著度', root.statement.salience], ['观察于', root.statement.observed_at], ['派生深度', root.statement.derived_depth], ['嵌套深度', root.statement.nesting_depth], ['id', short(String(root.statement.id), 18)]] as [k, v]}
						<div>
							<dt class="text-xs text-subtle">{k}</dt>
							<dd class="mt-0.5 break-words font-medium text-fg">{v ?? '—'}</dd>
						</div>
					{/each}
				</dl>
			{/if}
		</Card>

		<Card title="来源 · 抽取" description="创建这条语句的那次 LLM 抽取:状态 / 时间,以及失败尝试留底的原始 LLM 输出。">
			{#if ex}
				<div class="flex flex-wrap items-center gap-2 text-xs">
					<Badge tone={statusLabel(ex.status).tone}>{statusLabel(ex.status).label}</Badge>
					{#if ex.attempt_number != null}<span class="text-subtle">第 {ex.attempt_number} 次尝试</span>{/if}
					{#if ex.created_at}<span class="text-subtle">{ex.created_at}</span>{/if}
					{#if ex.total_tokens || ex.latency_ms}
						<span class="text-subtle tabular-nums">· {ex.total_tokens ?? 0} tok（prompt {ex.prompt_tokens ?? 0} / completion {ex.completion_tokens ?? 0}）· {ex.latency_ms ?? 0} ms</span>
					{/if}
				</div>
				{#if ex.error}<p class="mt-2 text-sm text-danger">错误:{ex.error}</p>{/if}
				{#if ex.raw_output}
					<p class="mb-1 mt-3 text-xs text-subtle">原始 LLM 输出</p>
					<CodeBlock content={ex.raw_output} language="json" />
				{:else if ex.status === 'success'}
					<p class="mt-2 text-xs text-subtle">成功抽取不留存原始输出(语句本身即解析结果;仅失败/解析失败留底)。</p>
				{/if}
				{#if ex.failed_attempts?.length}
					<p class="mb-1 mt-4 text-xs font-medium text-muted">同次抽取的失败尝试(原始 LLM 输出)</p>
					<ul class="space-y-2">
						{#each ex.failed_attempts as fa, i (i)}
							<li class="rounded-control border border-border bg-bg px-3 py-2">
								<div class="flex flex-wrap items-center gap-2 text-xs">
									<Badge tone={statusLabel(fa.status).tone}>{statusLabel(fa.status).label}</Badge>
									{#if fa.attempt_number != null}<span class="text-subtle">第 {fa.attempt_number} 次</span>{/if}
									{#if fa.error}<span class="text-danger">{fa.error}</span>{/if}
									{#if fa.total_tokens || fa.latency_ms}<span class="text-subtle tabular-nums">{fa.total_tokens ?? 0} tok · {fa.latency_ms ?? 0} ms</span>{/if}
								</div>
								{#if fa.raw_output}
									<div class="mt-2"><CodeBlock content={fa.raw_output} language="json" /></div>
								{/if}
							</li>
						{/each}
					</ul>
				{/if}
			{:else}
				<p class="text-sm text-muted">非抽取来源(派生 / 推断 / 系统写入)——无原始 LLM 记录。</p>
			{/if}
		</Card>

		<Card title="证据 · Engram" description="这条语句锚定的原始材料:源类型 / 内容指纹 / 源文预览。">
			{#if root.evidence_parse_error}
				<p class="text-sm text-warn">evidence_json 无法解析 —— 跳过证据展开,不影响其余取证。</p>
			{:else if root.evidence?.length}
				<ul class="space-y-3">
					{#each root.evidence as evi, i (i)}
						<li class="rounded-control border border-border bg-bg px-3 py-2">
							<div class="flex flex-wrap items-center gap-2 text-xs text-subtle">
								{#if evi.engram}<span class="text-muted">{evi.engram.source_kind}</span>{/if}
								{#if evi.engram}<span>{evi.engram.privacy_class}</span>{/if}
								{#if evi.engram?.erased}<Badge tone="warn">已抹除</Badge>{/if}
								<span>指纹 {short(evi.content_hash, 16)}</span>
							</div>
							{#if evi.engram?.payload_preview}
								<p class="mt-1.5 whitespace-pre-wrap break-words text-sm text-fg">{evi.engram.payload_preview}</p>
							{:else if evi.engram}
								<p class="mt-1 text-xs text-subtle">无源文预览(非 inline / 已抹除)。</p>
							{:else}
								<p class="mt-1 text-xs text-subtle">证据 engram 不存在(孤儿引用 {short(evi.engram_ref, 10)})。</p>
							{/if}
						</li>
					{/each}
				</ul>
			{:else}
				<p class="text-sm text-muted">无证据锚点。</p>
			{/if}
		</Card>

		{#if root.derived_from?.length || root.supersedes || root.derived_from_parse_error}
			<Card title="血缘 · 派生与前身" description="由哪些语句派生而来(derived_from),以及修正/取代了哪条前身(supersedes)。点节点可继续下钻。">
				{#if root.derived_from_parse_error}
					<p class="text-sm text-warn">derived_from_json 无法解析 —— 跳过派生展开。</p>
				{/if}
				{#if root.derived_from?.length}
					<p class="mb-1 text-xs font-medium text-muted">派生自</p>
					<ul class="space-y-1">
						{#each root.derived_from as c (c.id)}{@render lineage(c)}{/each}
					</ul>
				{/if}
				{#if root.supersedes}
					<p class="mb-1 mt-3 text-xs font-medium text-muted">前身(已被取代)</p>
					<ul class="space-y-1">{@render lineage(root.supersedes)}</ul>
				{/if}
			</Card>
		{/if}
	</div>
{:else}
	<div class="mt-4">
		<EmptyState
			title="选一条语句开始"
			description="用上方「取镜」查找,或从「语句」页点开一条记忆的「透视来源」。"
		/>
	</div>
{/if}
