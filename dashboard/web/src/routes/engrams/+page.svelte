<script lang="ts">
	import { withWorld } from '$lib/world.svelte';
	import { goto } from '$app/navigation';
	import { api, type EngramDetail, type EngramListResp, type EngramRow, worldPath } from '$lib/api';
	import { createQuery } from '$lib/query.svelte';
	import DataTable from '$lib/components/DataTable.svelte';
	import PageHeader from '$lib/components/PageHeader.svelte';
	import { Badge, Button, Chip, Drawer, EmptyState, Input, Select, Skeleton } from '$lib/components/ui';

	// T0a — 原始数据 · 证据(engram):记忆流最上游的 verbatim 原始证据,不可变。
	// 此前无专属端点/页面,只能从某条 statement 反向溯源(透视镜)时顺带瞥见。
	// 本页纯只读浏览/搜索 —— 不新增任何可写路径。
	let sourceKind = $state('');
	let privacyClass = $state('');
	let erased = $state(''); // 三态:'' 全部 / 'no' 未擦除 / 'yes' 已擦除
	let q = $state('');

	function url() {
		const p = new URLSearchParams();
		if (sourceKind) p.set('source_kind', sourceKind);
		if (privacyClass) p.set('privacy_class', privacyClass);
		if (erased) p.set('erased', erased);
		if (q) p.set('q', q);
		return worldPath(`/api/engrams?${p}`);
	}
	const listQuery = createQuery(() => api.get<EngramListResp>(url()));
	$effect(() => {
		listQuery.refetch();
	});

	let allRows = $derived(listQuery.data?.rows ?? []);
	const distinct = (key: keyof EngramRow) => [
		'',
		...[...new Set(allRows.map((r) => String(r[key] ?? '')).filter(Boolean))].sort()
	];
	let sourceKinds = $derived(distinct('source_kind'));
	let privacyClasses = $derived(distinct('privacy_class'));

	// ── 详情抽屉:单条全展示列 + payload 预览(privacy-gated)+ 被引用 statements ──
	let detailOpen = $state(false);
	let detail = $state<EngramDetail | null>(null);
	let detailLoading = $state(false);
	let detailError = $state('');
	let detailForId = $state(''); // 防陈旧:并发点开时只让最新一次结果落地

	async function openDetail(row: Record<string, unknown>) {
		const id = String(row.id ?? '');
		detailOpen = true;
		detail = null;
		detailError = '';
		detailLoading = true;
		detailForId = id;
		try {
			const res = await api.get<EngramDetail>(worldPath(`/api/engram/${encodeURIComponent(id)}`));
			if (detailForId === id) detail = res;
		} catch (e) {
			if (detailForId === id) detailError = e instanceof Error ? e.message : String(e);
		} finally {
			if (detailForId === id) detailLoading = false;
		}
	}

	const fmtv = (v: unknown) => (v == null || v === '' ? '—' : String(v));
</script>

<PageHeader title="原始数据" subtitle="记忆的 verbatim 证据源头。只读,不可变。" />

<div class="mb-4 flex flex-wrap items-end gap-3">
	<label class="block">
		<span class="mb-1 block text-xs text-muted">source_kind</span>
		<Select
			bind:value={sourceKind}
			class="w-40"
			aria-label="source_kind"
			options={sourceKinds.map((k) => ({ value: k, label: k || '全部' }))}
		/>
	</label>
	<label class="block">
		<span class="mb-1 block text-xs text-muted">privacy_class</span>
		<Select
			bind:value={privacyClass}
			class="w-40"
			aria-label="privacy_class"
			options={privacyClasses.map((k) => ({ value: k, label: k || '全部' }))}
		/>
	</label>
	<label class="block">
		<span class="mb-1 block text-xs text-muted">擦除状态</span>
		<Select
			bind:value={erased}
			class="w-32"
			aria-label="擦除状态"
			options={[
				{ value: '', label: '全部' },
				{ value: 'no', label: '未擦除' },
				{ value: 'yes', label: '已擦除' }
			]}
		/>
	</label>
	<label class="block">
		<span class="mb-1 block text-xs text-muted">搜索</span>
		<Input bind:value={q} placeholder="content_hash / source_item_id" class="w-56" />
	</label>
	<Button variant="soft" onclick={() => listQuery.refetch()}>筛选</Button>
	<span class="ml-auto pb-1.5 text-xs text-subtle">
		{listQuery.data?.total ?? 0} 条 · 点击行看详情
	</span>
</div>

{#if listQuery.error}
	<EmptyState title="加载失败" description={listQuery.error.message} />
{:else if listQuery.loading && !listQuery.data}
	<div class="space-y-2">{#each Array(4) as _}<Skeleton class="h-8 w-full" />{/each}</div>
{:else if allRows.length === 0}
	<EmptyState
		title="还没有原始证据"
		description="remember 一段文本后,它的 engram 会出现在这里。"
	/>
{:else}
	<!-- DataTable 单元格只做字符串化(见 $lib/components/DataTable.svelte 的 fmt()),
	     无逐格自定义渲染插槽,故「已擦除」danger Badge 落在详情抽屉里而非列表列内
	     (erased_at 非空时其字符串值本身即可辨认;抽屉里有明确 Badge)。 -->
	<DataTable
		rows={allRows}
		loading={listQuery.loading}
		emptyText="无 engrams"
		onRowClick={openDetail}
		columns={[
			'id',
			'source_kind',
			'privacy_class',
			'retention_mode',
			'refcount',
			'created_at',
			'erased_at'
		]}
	/>
{/if}

<Drawer bind:open={detailOpen} title="Engram 详情">
	{#if detailLoading}
		<div class="space-y-2">{#each Array(5) as _}<Skeleton class="h-8 w-full" />{/each}</div>
	{:else if detailError}
		<EmptyState title="加载失败" description={detailError} />
	{:else if detail}
		{@const g = detail.engram}
		<div class="space-y-4 text-sm">
			<div class="flex flex-wrap gap-2">
				<Badge tone={g.erased_at ? 'danger' : 'neutral'}
					>{g.erased_at ? '已擦除' : '存活'}</Badge
				>
				<Badge tone="info">{g.privacy_class}</Badge>
				<Chip>{g.source_kind}</Chip>
				<Chip>{g.retention_mode}</Chip>
				<Chip>被引用 {g.refcount} 次</Chip>
			</div>

			<div class="rounded-control border border-border bg-surface p-3">
				<div class="mb-1 text-xs uppercase tracking-wide text-subtle">原文预览</div>
				{#if detail.preview !== null}
					<pre class="whitespace-pre-wrap break-words font-mono text-xs text-fg">{detail.preview}</pre>
					<p class="mt-1 text-xs text-subtle">仅前 280 字符。</p>
				{:else}
					<p class="text-xs text-subtle">预览已抑制:{detail.preview_suppressed_reason ?? '未知原因'}</p>
				{/if}
			</div>

			<dl class="space-y-2">
				{#each [
					['id', g.id],
					['content_hash', g.content_hash],
					['ingest_policy', g.ingest_policy],
					['ingest_mode', g.ingest_mode],
					['payload_uri', g.payload_uri],
					['created_at', g.created_at],
					['erased_at', g.erased_at],
					['adapter_name', g.adapter_name],
					['adapter_version', g.adapter_version],
					['source_item_id', g.source_item_id],
					['source_version', g.source_version],
					['chunk_index', g.chunk_index],
					['byte_preserving', g.byte_preserving]
				] as [k, v]}
					<div>
						<dt class="text-xs uppercase tracking-wide text-subtle">{k}</dt>
						<dd class="break-words text-fg">{fmtv(v)}</dd>
					</div>
				{/each}
			</dl>

			<div class="rounded-control border border-border bg-surface p-3">
				<div class="mb-1 text-xs uppercase tracking-wide text-subtle">
					被引用（referencing statements，{detail.referencing_statements.length}）
				</div>
				{#if detail.referencing_statements.length === 0}
					<p class="text-xs text-subtle">暂无语句引用此证据。</p>
				{:else}
					<ul class="divide-y divide-border">
						{#each detail.referencing_statements as r}
							<li class="flex items-center justify-between gap-2 py-2">
								<span class="truncate text-fg">{r.subject_id} {r.predicate}</span>
								<Button
									variant="ghost"
									onclick={() => goto(withWorld(`/lens?stmt=${encodeURIComponent(r.id)}`))}
								>
									透视 →
								</Button>
							</li>
						{/each}
					</ul>
				{/if}
			</div>
		</div>
	{/if}
</Drawer>
