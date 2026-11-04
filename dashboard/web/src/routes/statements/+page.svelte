<script lang="ts">
	import { describeError } from '$lib/worlds';
	import { page } from '$app/state';
	import { goto } from '$app/navigation';
	import { api, type CascadePreview, worldPath } from '$lib/api';
	import { createQuery } from '$lib/query.svelte';
	import { lastWsEvent } from '$lib/health';
	import { refetchOnEvent, withWorld, onWorldChange } from '$lib/world.svelte';
	import { toast } from '$lib/ui/toast';
	import { labelFor, glossFor, sectionize } from '$lib/labels';
	import DataTable from '$lib/components/DataTable.svelte';
	import CodeBlock from '$lib/components/CodeBlock.svelte';
	import PageHeader from '$lib/components/PageHeader.svelte';
	import {
		Badge,
		Button,
		Chip,
		EmptyState,
		Input,
		Select,
		Drawer,
		ConfirmDialog
	} from '$lib/components/ui';

	// consolidation_state 六态(值域以 C++ ConsolidationState 枚举为准,见
	// include/starling/schema/statement_enums.hpp;本页只传参,不硬编码语义)。
	const CONSOLIDATION_STATES = [
		{ value: '', label: '全部' },
		{ value: 'volatile', label: '易逝(海马)' },
		{ value: 'replaying_consolidating', label: '固化回放中' },
		{ value: 'replaying_reconsolidating', label: '再固化回放中' },
		{ value: 'consolidated', label: '已固化(新皮层)' },
		{ value: 'archived', label: '已归档' },
		{ value: 'forgotten', label: '已遗忘' }
	];

	// T0d-1 — modality 服务端过滤(值域小写,与 nav 深链的 Semantic/Norms 子区
	// 一致;本页只传参,不硬编码语义)。空值 = 不过滤。
	const MODALITIES = [
		{ value: '', label: '全部' },
		{ value: 'believes', label: 'believes' },
		{ value: 'knows', label: 'knows' },
		{ value: 'norm_ought', label: 'norm_ought' },
		{ value: 'norm_forbid', label: 'norm_forbid' }
	];

	// T0e ② — 信念阶层快捷视角:空=不筛;"first"=一阶(我信 X,nesting_depth=0);
	// "higher"=二阶及以上(我以为你信 X,展平存储,nesting_depth>=1)。后端只翻译
	// 这两个固定取值,本页不硬编码 WHERE 语义。
	const BELIEF_ORDERS = [
		{ value: '', label: '全部' },
		{ value: 'first', label: '一阶(我信 X)' },
		{ value: 'higher', label: '二阶及以上(我以为你信 X)' }
	];

	// T0e ① — subject_kind 过滤(DB CHECK 值域 cognizer/entity,见 migrations/0001)。
	const SUBJECT_KINDS = [
		{ value: '', label: '全部' },
		{ value: 'cognizer', label: 'cognizer(认知体)' },
		{ value: 'entity', label: 'entity(实体)' }
	];

	let predicate = $state('');
	let perspective = $state('');
	let reviewStatus = $state(''); // 服务端过滤(片 6:='review_requested' 即审批队列)
	// T0b — 从 URL 深链初始化(nav 海马组短期视角深链带 ?consolidation_state=多值)。
	let consolidationState = $state(page.url.searchParams.get('consolidation_state') ?? '');
	// T0d-1 — 从 URL 深链初始化(nav 新皮层组 Semantic/Norms 深链带 ?modality=多值)。
	let modality = $state(page.url.searchParams.get('modality') ?? '');
	// T0e ② — 从 URL 深链初始化(nav 他者心智组 belief_order 深链)。
	let beliefOrder = $state(page.url.searchParams.get('belief_order') ?? '');
	// T0e ① — 从 URL 深链初始化。
	let subjectKind = $state(page.url.searchParams.get('subject_kind') ?? '');
	let polarity = $state('');

	function url() {
		const p = new URLSearchParams();
		if (predicate) p.set('predicate', predicate);
		if (perspective) p.set('perspective', perspective);
		if (reviewStatus) p.set('review_status', reviewStatus);
		if (consolidationState) p.set('consolidation_state', consolidationState);
		if (modality) p.set('modality', modality);
		if (beliefOrder) p.set('belief_order', beliefOrder);
		if (subjectKind) p.set('subject_kind', subjectKind);
		return worldPath(`/api/statements?${p}`);
	}
	const q = createQuery(() => api.get<{ rows: Record<string, unknown>[] }>(url()));
	$effect(() => {
		reviewStatus; // dep:改服务端审批过滤即重取(predicate/perspective 仍走「筛选」按钮)
		consolidationState; // dep:T0b 服务端过滤即重取
		modality; // dep:T0d-1 服务端过滤即重取
		beliefOrder; // dep:T0e ② 服务端过滤即重取
		subjectKind; // dep:T0e ① 服务端过滤即重取
		q.refetch();
	});
	// T8 — 单独一个 effect:若混进上面的过滤 effect,$lastWsEvent 会成为它的依赖,
	// 于是「任意」WS 事件都触发那句无条件 q.refetch()(effect 内无从区分是哪个依赖变的)。
	// review I3 — 原先只订写事件,漏了后台 tick:表格有 consolidation_state 一列,
	// 而固化/归档/衰减都由 tick 推进,只订写事件会让状态徽章陈旧。
	$effect(() => {
		if (refetchOnEvent($lastWsEvent)) q.refetch();
	});

	let allRows = $derived(q.data?.rows ?? []);
	let polarities = $derived([
		'',
		...[...new Set(allRows.map((r) => String(r.polarity ?? '')).filter(Boolean))].sort()
	]);
	// modality 已改服务端过滤(见 url()/$effect),这里只保留客户端 polarity 过滤。
	let rows = $derived(allRows.filter((r) => !polarity || r.polarity === polarity));

	let detailOpen = $state(false);
	let detail = $state<Record<string, unknown> | null>(null);
	// 换世界:抽屉里是上个世界的行,关掉并清空,免得挂在新世界的名字下。
	onWorldChange(() => {
		detailOpen = false;
		detail = null;
		confirmOpen = false;
		confirmCfg = null;
		confirmForId = '';
	});
	function openDetail(r: Record<string, unknown>) {
		detail = r;
		detailOpen = true;
	}
	const fmtv = (v: unknown) => (v == null ? '—' : typeof v === 'object' ? JSON.stringify(v) : String(v));

	// ── T6 detail drawer 策展 ────────────────────────────────────────────────
	// 原先是裸 Object.entries dump(DB 列名当标签、无层次)。改为三个语义分区:
	// 核心命题(谁以何样态对什么持有判断)/ 元数据(记忆体对它的加工状态)/ 时间。
	// 时间区按 `_at` 后缀动态收编,后端新增时间列自动归位;所有分区都没收编的 key
	// 由 sectionize 落进「其它」兜底区 —— 不丢字段,用户仍看得到全部原始数据。
	// subject_kind / provenance / derived_depth 目前不在 /api/statements 的 SELECT 里,
	// 先声明好归属:后端补列即自动落到对应分区而非兜底区。
	const STATEMENT_CORE = [
		'id',
		'holder_id',
		'holder_perspective',
		'subject_kind',
		'subject_id',
		'predicate',
		'object_kind',
		'object_value',
		'modality',
		'polarity'
	];
	const STATEMENT_META = [
		'confidence',
		'salience',
		'consolidation_state',
		'review_status',
		'nesting_depth',
		'provenance',
		'derived_depth'
	];
	let sections = $derived(
		sectionize(detail, [
			{ title: '核心命题', keys: STATEMENT_CORE },
			{ title: '元数据', keys: STATEMENT_META },
			{
				title: '时间',
				keys: ['observed_at', ...Object.keys(detail ?? {}).filter((k) => k.endsWith('_at'))]
			}
		])
	);

	// 枚举值呈现:只有真正带健康含义的字段上语义色(固化态 / 审批状态),
	// modality / polarity 这类纯分类走中性 Chip —— 不滥用颜色。值保持后端原样
	// (与表格列、筛选下拉的取值一一对应,便于回查),只有 label 是中文。
	type Tone = 'neutral' | 'brand' | 'success' | 'warn' | 'danger' | 'info';
	const BADGE_FIELDS = ['consolidation_state', 'review_status'];
	const CHIP_FIELDS = ['modality', 'polarity', 'subject_kind', 'object_kind'];
	const VALUE_TONES: Record<string, Record<string, Tone>> = {
		consolidation_state: {
			volatile: 'warn',
			replaying_consolidating: 'info',
			replaying_reconsolidating: 'info',
			consolidated: 'success',
			archived: 'neutral',
			forgotten: 'neutral'
		},
		review_status: {
			review_requested: 'warn',
			pending_review: 'info',
			approved: 'success',
			rejected: 'danger'
		}
	};

	// ── 片 6 干预动作(全经唯一写者漏斗;成功后关抽屉 + 重取) ─────────────
	let busy = $state(false);
	let confirmOpen = $state(false);
	let confirmCfg = $state<{ title: string; desc: string; run: () => Promise<void> } | null>(null);
	let confirmForId = $state(''); // guards the background cascade-preview against a stale dialog

	async function act(label: string, fn: () => Promise<unknown>) {
		busy = true;
		try {
			await fn();
			toast.success(`${label}成功`);
			detailOpen = false;
			await q.refetch();
		} catch (e) {
			toast.error(`${label}失败:${describeError(e)}`);
		} finally {
			busy = false;
		}
	}
	const approve = (id: string) => act('批准', () => api.post(worldPath('/api/review'), { stmt_id: id }));
	const reconsolidate = (id: string) =>
		act('请求再固化', () => api.post(worldPath('/api/reconsolidate'), { stmt_id: id }));
	// 片 6 级联预览(inform-only):立刻开确认框(不阻塞遗忘),再后台取「会波及哪些派生
	// 后代」折进提示。预览只读、best-effort——慢/失败都不挡操作;若用户已切到别的语句
	// (confirmForId 变了)或框已关,陈旧结果丢弃。遗忘仍只逻辑删除本条,不级联删后代。
	function askForget(id: string, reject: boolean) {
		const base = '逻辑删除(→ forgotten,移出检索与回放池)。不可逆。';
		confirmForId = id;
		confirmCfg = {
			title: reject ? '拒绝并遗忘此语句?' : '遗忘此语句?',
			desc: base,
			run: () => act(reject ? '拒绝' : '遗忘', () => api.post(worldPath('/api/forget'), { ids: [id] }))
		};
		confirmOpen = true;
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

<PageHeader title="语句" subtitle="记忆原子 Statement:谁、以何样态与极性、对什么持有判断。" />
<div class="mb-4 flex flex-wrap items-end gap-3">
	<label class="block">
		<span class="mb-1 block text-xs text-muted">predicate</span>
		<Input bind:value={predicate} placeholder="如 responsible_for" class="w-44" />
	</label>
	<label class="block">
		<span class="mb-1 block text-xs text-muted">perspective</span>
		<Input bind:value={perspective} placeholder="如 first_person" class="w-44" />
	</label>
	<label class="block">
		<span class="mb-1 block text-xs text-muted">review_status</span>
		<Select
			bind:value={reviewStatus}
			class="w-36"
			aria-label="review_status"
			options={[
				{ value: '', label: '全部' },
				{ value: 'review_requested', label: '待审批' },
				{ value: 'approved', label: '已批准' },
				{ value: 'pending_review', label: '待复核' },
				{ value: 'rejected', label: '已拒绝' }
			]}
		/>
	</label>
	<label class="block">
		<span class="mb-1 block text-xs text-muted">consolidation_state</span>
		<Select
			bind:value={consolidationState}
			class="w-40"
			aria-label="consolidation_state"
			options={CONSOLIDATION_STATES}
		/>
	</label>
	<label class="block">
		<span class="mb-1 block text-xs text-muted">modality</span>
		<Select
			bind:value={modality}
			class="w-32"
			aria-label="modality"
			options={MODALITIES}
		/>
	</label>
	<label class="block">
		<span class="mb-1 block text-xs text-muted">信念阶层</span>
		<Select
			bind:value={beliefOrder}
			class="w-44"
			aria-label="belief_order"
			options={BELIEF_ORDERS}
		/>
	</label>
	<label class="block">
		<span class="mb-1 block text-xs text-muted">subject_kind</span>
		<Select
			bind:value={subjectKind}
			class="w-36"
			aria-label="subject_kind"
			options={SUBJECT_KINDS}
		/>
	</label>
	<label class="block">
		<span class="mb-1 block text-xs text-muted">polarity</span>
		<Select
			bind:value={polarity}
			class="w-28"
			aria-label="polarity"
			options={polarities.map((p) => ({ value: p, label: p || '全部' }))}
		/>
	</label>
	<Button variant="soft" onclick={() => q.refetch()}>筛选</Button>
	<span class="ml-auto pb-1.5 text-xs text-subtle">{rows.length} 条 · 点击行看详情</span>
</div>
{#if q.error}
	<EmptyState title="加载失败" description={describeError(q.error)} />
{:else}
	<DataTable
		{rows}
		loading={q.loading}
		emptyText="无 statements"
		onRowClick={openDetail}
		columns={['subject_id', 'predicate', 'object_value', 'modality', 'consolidation_state', 'review_status']}
	/>
{/if}

<Drawer bind:open={detailOpen} title="Statement 详情">
	{#if detail}
		{@const sid = String(detail.id ?? '')}
		{@const rs = String(detail.review_status ?? '')}
		{@const cs = String(detail.consolidation_state ?? '')}
		{#if sid}
			<div class="mb-3 flex flex-wrap gap-2">
				<Button variant="soft" onclick={() => goto(withWorld(`/lens?stmt=${encodeURIComponent(sid)}`))}>
					透视来源 →
				</Button>
				{#if rs === 'review_requested'}
					<Button variant="soft" disabled={busy} onclick={() => approve(sid)}>批准</Button>
					<Button variant="danger" disabled={busy} onclick={() => askForget(sid, true)}>拒绝</Button>
				{/if}
				{#if cs === 'consolidated'}
					<Button variant="soft" disabled={busy} onclick={() => reconsolidate(sid)}>请求再固化</Button>
				{/if}
				<Button variant="ghost" disabled={busy} onclick={() => askForget(sid, false)}>遗忘</Button>
			</div>
		{/if}
		<div class="space-y-3">
			{#each sections as section}
				<div class="border-t border-border pt-3 first:border-t-0 first:pt-0">
					<p class="mb-1.5 text-xs uppercase tracking-wide text-subtle">{section.title}</p>
					<dl class="space-y-2 text-sm">
						{#each section.entries as [k, v]}
							<div>
								<dt class="text-xs text-muted" title={glossFor(k)}>{labelFor(k)}</dt>
								{#if v !== null && typeof v === 'object'}
									<dd><CodeBlock content={JSON.stringify(v)} language="json" /></dd>
								{:else if v == null || v === ''}
									<dd class="text-subtle">—</dd>
								{:else if BADGE_FIELDS.includes(k)}
									<dd><Badge tone={VALUE_TONES[k]?.[String(v)] ?? 'neutral'}>{String(v)}</Badge></dd>
								{:else if CHIP_FIELDS.includes(k)}
									<dd><Chip>{String(v)}</Chip></dd>
								{:else}
									<dd class="break-words text-fg">{fmtv(v)}</dd>
								{/if}
							</div>
						{/each}
					</dl>
				</div>
			{/each}
		</div>
	{/if}
</Drawer>

<ConfirmDialog
	bind:open={confirmOpen}
	title={confirmCfg?.title ?? ''}
	description={confirmCfg?.desc ?? ''}
	confirmLabel="确认"
	danger
	onconfirm={() => confirmCfg?.run()}
/>
