<script lang="ts">
	import { untrack } from 'svelte';
	import { createQuery } from '$lib/query.svelte';
	import { lastWsEvent } from '$lib/health';
	import { shouldRefetch } from '$lib/feed';
	import { explicitWorld, setWorld } from '$lib/world.svelte';
	import { toast } from '$lib/ui/toast';
	import PageHeader from '$lib/components/PageHeader.svelte';
	import AutoSnapshotFields from '$lib/components/AutoSnapshotFields.svelte';
	import WorldRollbackDrawer from '$lib/components/WorldRollbackDrawer.svelte';
	import {
		Badge,
		Button,
		Card,
		ConfirmDialog,
		Drawer,
		EmptyState,
		Field,
		Input,
		Skeleton
	} from '$lib/components/ui';
	import {
		apiDetail,
		autoSnapshotLabel,
		formatBytes,
		isValidWorldName,
		parseIntervalHours,
		parseLabels,
		shortId,
		snapshotReasonLabel,
		worldErrorMessage,
		worldsApi,
		type AutoSnapshotConfig,
		type SnapshotRow,
		type WorldRow
	} from '$lib/worlds';

	// W1–W4 — 世界管理:一个世界 = 一个库文件 + 一个租户(HTTP 契约见 $lib/worlds)。
	// 列表按标签过滤(label.<key>=<value>,多个同时满足)并分页(offset/limit,后端回 total)。
	const PAGE_SIZE = 25;
	let filterText = $state(''); // 输入框里的原文
	let appliedFilter = $state<Record<string, string>>({}); // 点「过滤」后生效的
	let offset = $state(0);
	let filterInvalid = $derived(parseLabels(filterText) === null);
	const q = createQuery(() =>
		worldsApi.list({ labels: appliedFilter, offset, limit: PAGE_SIZE })
	);
	$effect(() => {
		q.refetch();
	});
	// snapshot_created / world_rolled_back / world_changed 都归入 mutatesMemory 的「会改变可见状态」一侧
	// (只有 recall 是纯读,见 $lib/ws),所以一个判据同时覆盖它们。世界列表对所有世界的变化都关心
	// (语句数、最近写入、快照数都显示在行上),所以不按 world 过滤(shouldRefetch 的 null)。
	// 快照抽屉开着时一并重取它的列表;抽屉状态用 untrack 读,免得开关抽屉本身触发重取。
	$effect(() => {
		if (!shouldRefetch($lastWsEvent, null)) return;
		untrack(() => {
			void q.refetch();
			if (snapOpen && snapWorld) void loadSnaps(snapWorld);
		});
	});

	let worlds = $derived(q.data?.worlds ?? []);
	let defaultName = $derived(q.data?.default ?? '');
	let total = $derived(q.data?.total ?? 0);
	let pageFrom = $derived(worlds.length ? offset + 1 : 0);
	let pageTo = $derived(offset + worlds.length);
	// 删除后本页可能空了而前面还有:退回最后一页。
	$effect(() => {
		if (q.data && worlds.length === 0 && offset > 0)
			offset = Math.max(0, Math.floor((Math.max(total, 1) - 1) / PAGE_SIZE) * PAGE_SIZE);
	});

	function applyFilter() {
		const f = parseLabels(filterText);
		if (f === null) return;
		appliedFilter = f;
		offset = 0;
	}
	function clearFilter() {
		filterText = '';
		appliedFilter = {};
		offset = 0;
	}
	const labelPairs = (w: WorldRow) => Object.entries(w.labels ?? {});
	let retention = $derived(q.data?.snapshot_retention);
	// W4:自动快照失败只记录、不影响摄入;有失败时在页面顶部提示一次。
	let autoStatus = $derived(q.data?.auto_snapshot_status);
	let autoFailureText = $derived.by(() => {
		const st = autoStatus;
		if (!st || st.failures <= 0) return '';
		const last = [st.last_error_at, st.last_error].filter(Boolean).join(' ') || '—';
		return `自动快照失败 ${st.failures} 次，最近一次：${last}；摄入未受影响，这几次没拍成的快照不会作为回滚点`;
	});

	const errText = (e: unknown) => worldErrorMessage(apiDetail(e));
	const worldTitle = (w: WorldRow) => (w.label ? `${w.name} · ${w.label}` : w.name);
	// 列表行不带库文件大小和原始数据条数(要打开库才有,几百个世界时撑不住),只给语句数与最近写入。
	const statsLine = (w: WorldRow) =>
		`语句 ${w.statements} · 认知体 ${w.holders} · 最近写入 ${w.last_write_at || '—'} · ` +
		`快照 ${w.snapshots} 份（${formatBytes(w.snapshot_bytes)}）`;
	// 默认世界不可删;后端同样拒绝(world_protected),前端不给入口。
	const isDeletable = (w: WorldRow) => w.name !== defaultName;

	// ── 新建 ──────────────────────────────────────────────────────────────
	let newName = $state('');
	let newLabel = $state('');
	let newLabels = $state('');
	// 自动快照的初值取默认世界当前的设置(spec §8.2):新世界默认继承它。
	// 只有用户改了才随请求发 auto_snapshot,没改就不发,由后端继承。
	let defaultAuto = $state<AutoSnapshotConfig | null>(null);
	let defaultAutoFor = '';
	const FALLBACK_AUTO: AutoSnapshotConfig = { on_session: false, interval_hours: 0 };
	let newOnSession = $state(FALLBACK_AUTO.on_session);
	let newHours = $state<number | string | null>(FALLBACK_AUTO.interval_hours);
	$effect(() => {
		const name = defaultName;
		if (!name || name === defaultAutoFor) return;
		defaultAutoFor = name;
		void worldsApi
			.detail(name)
			.then((d) => {
				const a = d.settings?.background?.auto_snapshot;
				if (!a) return;
				defaultAuto = { on_session: a.on_session, interval_hours: a.interval_hours };
				// 表单还停在保守初值(用户没动过)时,用默认世界的设置覆盖它。
				if (newOnSession === FALLBACK_AUTO.on_session && newHours === FALLBACK_AUTO.interval_hours) {
					newOnSession = a.on_session;
					newHours = a.interval_hours;
				}
			})
			.catch(() => {
				/* 取不到就用保守初值且不发 auto_snapshot,让后端继承 */
			});
	});
	let creating = $state(false);
	let nameValid = $derived(isValidWorldName(newName));
	// 空输入不标红:还没写不算写错,只是不能提交。
	let nameInvalid = $derived(newName !== '' && !nameValid);
	let newHoursParsed = $derived(parseIntervalHours(newHours));
	let newLabelsParsed = $derived(parseLabels(newLabels));
	let canCreate = $derived(
		nameValid && newHoursParsed !== null && newLabelsParsed !== null && !creating
	);
	// 自动快照字段与默认世界的设置(取不到时是保守初值)不同才算「改过」。
	let autoChanged = $derived.by(() => {
		const base = defaultAuto ?? FALLBACK_AUTO;
		return newOnSession !== base.on_session || newHoursParsed !== base.interval_hours;
	});

	async function doCreate() {
		const hours = newHoursParsed;
		const labels = newLabelsParsed;
		if (!canCreate || hours === null || labels === null) return; // 函数级守卫:不依赖按钮的 disabled
		const name = newName;
		creating = true;
		try {
			await worldsApi.create(
				name,
				newLabel.trim(),
				autoChanged ? { on_session: newOnSession, interval_hours: hours } : undefined,
				labels
			);
			toast.success(`已创建世界「${name}」`);
			newName = '';
			newLabel = '';
			newLabels = '';
			const base = defaultAuto ?? FALLBACK_AUTO;
			newOnSession = base.on_session;
			newHours = base.interval_hours;
			await q.refetch();
		} catch (e) {
			toast.error(errText(e));
		} finally {
			creating = false;
		}
	}

	// ── 自动快照设置(W4,抽屉) ──────────────────────────────────────────
	let autoOpen = $state(false);
	let autoWorld = $state('');
	let autoOnSession = $state(false);
	let autoHours = $state<number | string | null>(0);
	let autoSaving = $state(false);
	let autoHoursParsed = $derived(parseIntervalHours(autoHours));
	let canSaveAuto = $derived(!!autoWorld && autoHoursParsed !== null && !autoSaving);

	function openAutoSnapshot(w: WorldRow) {
		autoWorld = w.name;
		autoOnSession = w.auto_snapshot.on_session;
		autoHours = w.auto_snapshot.interval_hours;
		autoOpen = true;
	}

	async function doSaveAuto() {
		const name = autoWorld;
		const hours = autoHoursParsed;
		if (!canSaveAuto || hours === null) return; // 函数级守卫;范围的最终校验在后端
		autoSaving = true;
		try {
			const r = await worldsApi.setAutoSnapshot(name, {
				on_session: autoOnSession,
				interval_hours: hours
			});
			toast.success(`已更新「${name}」的自动快照：${autoSnapshotLabel(r.auto_snapshot)}`);
			autoOpen = false;
			await q.refetch();
		} catch (e) {
			toast.error(errText(e));
		} finally {
			autoSaving = false;
		}
	}

	// ── 重新确认端点(裁定 R53,ConfirmDialog) ─────────────────────────────
	// 供应商的地址或种类改了之后,继承它的世界一律 endpoint_changed;这一步把默认配置里各供应商此刻的地址
	// 复制进该世界引用它们的角色(供应商已删除的角色解绑)。默认世界的端点跟着配置走,不给入口。
	let reconfirmOpen = $state(false);
	let reconfirmTarget = $state('');
	let reconfirming = $state<string | null>(null);

	function askReconfirm(name: string) {
		reconfirmTarget = name;
		reconfirmOpen = true;
	}

	async function doReconfirm() {
		const name = reconfirmTarget;
		if (!name || reconfirming !== null) return;
		reconfirming = name;
		try {
			await worldsApi.reconfirm(name);
			toast.success(`已为「${name}」重新确认端点`);
			await q.refetch();
		} catch (e) {
			toast.error(errText(e));
		} finally {
			reconfirming = null;
		}
	}

	// ── 删除(抽屉 + 手输世界名) ─────────────────────────────────────────
	let deleteOpen = $state(false);
	let deleteTarget = $state('');
	let deleteConfirm = $state('');
	let deleting = $state(false);
	let deleteRow = $derived(worlds.find((w) => w.name === deleteTarget));
	// 抽屉开着时列表可能因 ws 事件刷新,所以每次都按最新行重判。
	let canDelete = $derived(
		!!deleteRow &&
			isDeletable(deleteRow) &&
			deleteConfirm === deleteRow.name &&
			!deleting
	);

	function askDelete(name: string) {
		deleteTarget = name;
		deleteConfirm = '';
		deleteOpen = true;
	}

	async function doDelete() {
		if (!canDelete) return; // 函数级守卫:前端校验一次,后端 confirm_mismatch 再校验一次
		const name = deleteTarget;
		deleting = true;
		try {
			await worldsApi.remove(name, deleteConfirm);
			toast.success(`已删除世界「${name}」`);
			// 删的恰好是顶栏选着的世界:回到默认世界,免得各页对着已删除的世界 404。
			if (explicitWorld() === name) setWorld(defaultName);
			deleteOpen = false;
			deleteConfirm = '';
			await q.refetch();
		} catch (e) {
			toast.error(errText(e));
		} finally {
			deleting = false;
		}
	}

	// ── 快照 ──────────────────────────────────────────────────────────────
	let snapOpen = $state(false);
	let snapWorld = $state('');
	let snaps = $state<SnapshotRow[]>([]);
	let snapsLoading = $state(false);
	let snapsErr = $state('');
	let snapNote = $state('');
	let snapshotting = $state(false);
	let removeSnapOpen = $state(false);
	let removeSnapTarget = $state<SnapshotRow | null>(null);
	let removingSnap = $state<string | null>(null);
	let snapReq = 0; // 防陈旧:连点不同世界时,只让最新一次的列表落地

	async function loadSnaps(name: string) {
		const mine = ++snapReq;
		snapsLoading = true;
		snapsErr = '';
		try {
			const r = await worldsApi.snapshots(name);
			if (mine !== snapReq) return;
			snaps = r.snapshots;
		} catch (e) {
			if (mine !== snapReq) return;
			snaps = [];
			snapsErr = errText(e);
			toast.error(snapsErr);
		} finally {
			if (mine === snapReq) snapsLoading = false;
		}
	}

	function openSnapshots(name: string) {
		snapWorld = name;
		snapNote = '';
		snaps = [];
		snapOpen = true;
		void loadSnaps(name);
	}

	async function doSnapshot() {
		const name = snapWorld;
		if (!name || snapshotting) return;
		snapshotting = true;
		try {
			await worldsApi.snapshot(name, snapNote.trim());
			toast.success(`已为「${name}」拍下快照`);
			if (snapWorld === name) {
				snapNote = '';
				await loadSnaps(name);
			}
			void q.refetch();
		} catch (e) {
			toast.error(errText(e));
		} finally {
			snapshotting = false;
		}
	}

	function askRemoveSnap(s: SnapshotRow) {
		removeSnapTarget = s;
		removeSnapOpen = true;
	}

	async function doRemoveSnap() {
		const target = removeSnapTarget;
		const name = snapWorld;
		if (!target || !name || removingSnap !== null) return;
		removingSnap = target.id;
		try {
			await worldsApi.removeSnapshot(name, target.id);
			toast.success('已删除快照');
			if (snapWorld === name) await loadSnaps(name);
			void q.refetch();
		} catch (e) {
			toast.error(errText(e));
		} finally {
			removingSnap = null;
		}
	}

	// ── 回滚到快照(W3 语义 A:丢掉快照之后的写入;抽屉 + 手输世界名) ──────────
	// 确认、调用、错误文案与刷新规则都在 WorldRollbackDrawer(/sessions 页共用)。
	let rollbackOpen = $state(false);
	let rollbackTarget = $state<{ world: string; snap: SnapshotRow } | null>(null);
	let rollbackBusy = $state(false);
	let rollingBack = $derived(rollbackBusy ? (rollbackTarget?.snap.id ?? null) : null); // 在途回滚的快照 id
	let rollbackRow = $derived.by(() => {
		const t = rollbackTarget;
		return t ? worlds.find((w) => w.name === t.world) : undefined;
	});
	let snapRowBusy = $derived(rollingBack !== null || removingSnap !== null || snapshotting);
	const snapSummary = (snap: SnapshotRow) =>
		`${snap.created_at} · ${snapshotReasonLabel(snap.reason)} · ${formatBytes(snap.size_bytes)}` +
		(snap.note ? ` · ${snap.note}` : '');

	function askRollback(s: SnapshotRow) {
		rollbackTarget = { world: snapWorld, snap: s };
		rollbackOpen = true;
	}

	// ── 清理回滚归档(抽屉 + 手输世界名) ─────────────────────────────────
	let purgeOpen = $state(false);
	let purgeTarget = $state('');
	let purgeConfirm = $state('');
	let purging = $state(false);
	let purgeRow = $derived(worlds.find((w) => w.name === purgeTarget));
	let canPurge = $derived(
		!!purgeRow &&
			purgeRow.archives > 0 &&
			purgeConfirm === purgeRow.name &&
			!purging &&
			rollingBack === null
	);

	// 任一会动库文件或引擎的请求在途时,世界行上的清理/删除按钮一律禁用。
	let worldBusy = $derived(deleting || rollingBack !== null || purging);

	function askPurge(name: string) {
		purgeTarget = name;
		purgeConfirm = '';
		purgeOpen = true;
	}

	async function doPurge() {
		if (!canPurge) return; // 函数级守卫
		const name = purgeTarget;
		purging = true;
		try {
			const r = await worldsApi.purgeArchives(name, purgeConfirm);
			toast.success(`已清理「${name}」的 ${r.deleted} 份回滚归档`);
			purgeOpen = false;
			purgeConfirm = '';
			await q.refetch();
		} catch (e) {
			toast.error(errText(e));
		} finally {
			purging = false;
		}
	}
</script>

<PageHeader
	title="世界"
	subtitle="一个世界 = 一个数据库文件 + 一个租户。所有世界同时在线，用顶栏的选择器看某个世界；快照是该世界库的紧凑只读副本，可以回滚到它。"
/>

{#if q.error && !q.data}
	<EmptyState title="加载失败" description={q.error.message}>
		<Button variant="soft" onclick={() => void q.refetch()}>重试</Button>
	</EmptyState>
{:else if !q.data}
	<div class="space-y-2">{#each Array(2) as _}<Skeleton class="h-24 w-full" />{/each}</div>
{:else}
	{#if q.error}
		<p class="mb-3 text-xs text-danger">刷新失败，下面可能是旧数据：{q.error.message}</p>
	{/if}
	{#if autoFailureText}
		<p
			role="status"
			class="mb-3 break-words rounded-control border border-warn/40 bg-warn/10 px-3 py-2 text-xs text-warn"
		>
			{autoFailureText}
		</p>
	{/if}

	<form
		class="mb-3 flex flex-wrap items-end gap-2"
		onsubmit={(e) => {
			e.preventDefault();
			applyFilter();
		}}
	>
		<Field
			label="按标签过滤"
			for="world-label-filter"
			hint="key=value，多个用逗号分隔，需同时满足"
			error={filterInvalid ? '每段都要写成 key=value' : ''}
		>
			{#snippet children(f)}
				<Input
					id="world-label-filter"
					name="world-label-filter"
					bind:value={filterText}
					invalid={f.invalid}
					aria-describedby={f.describedBy}
					placeholder="env=prod"
					autocomplete="off"
					class="max-w-64"
				/>
			{/snippet}
		</Field>
		<Button type="submit" variant="soft" disabled={filterInvalid}>过滤</Button>
		{#if Object.keys(appliedFilter).length}
			<Button variant="secondary" onclick={clearFilter}>清除过滤</Button>
		{/if}
	</form>

	<div class="space-y-3">
		{#each worlds as w (w.name)}
			<Card title={worldTitle(w)}>
				{#snippet actions()}
					{#if w.open}<Badge tone="info">已打开</Badge>{/if}
					{#if w.name === defaultName}<Badge tone="neutral">默认</Badge>{/if}
					{#if w.stats_error}<Badge tone="warn">统计不可用</Badge>{/if}
				{/snippet}
				{#if labelPairs(w).length}
					<p class="mb-1 flex flex-wrap gap-1" aria-label={`${w.name} 的标签`}>
						{#each labelPairs(w) as [k, v] (k)}
							<Badge tone="neutral">{k}={v}</Badge>
						{/each}
					</p>
				{/if}
				<p class="text-xs tabular-nums text-muted">{statsLine(w)}</p>
				{#if w.archives > 0}
					<p class="mt-1 text-xs tabular-nums text-muted">
						归档 {w.archives} 份 · {formatBytes(w.archive_bytes)}
					</p>
				{/if}
				{#if w.auto_snapshot}
					<p class="mt-1 text-xs tabular-nums text-muted">
						自动快照：{autoSnapshotLabel(w.auto_snapshot)}
					</p>
				{/if}
				<div class="mt-3 flex flex-wrap gap-2">
					<a
						href={`/?world=${encodeURIComponent(w.name)}`}
						class="inline-flex items-center rounded-control border border-border px-3 py-1.5 text-sm text-fg hover:bg-brand-tint"
						aria-label={`查看世界 ${w.name}`}>查看</a
					>
					<Button
						variant="secondary"
						aria-label={`快照 ${w.name}`}
						onclick={() => openSnapshots(w.name)}>快照</Button
					>
					{#if w.auto_snapshot}
						<Button
							variant="secondary"
							aria-label={`设置自动快照 ${w.name}`}
							onclick={() => openAutoSnapshot(w)}>设置</Button
						>
					{/if}
					{#if w.archives > 0}
						<Button
							variant="secondary"
							aria-label={`清理归档 ${w.name}`}
							disabled={worldBusy}
							onclick={() => askPurge(w.name)}>清理归档</Button
						>
					{/if}
					{#if w.name !== defaultName}
						<Button
							variant="secondary"
							aria-label={`重新确认端点 ${w.name}`}
							loading={reconfirming === w.name}
							disabled={reconfirming !== null || worldBusy}
							onclick={() => askReconfirm(w.name)}>重新确认端点</Button
						>
					{/if}
					{#if isDeletable(w)}
						<Button
							variant="secondary"
							aria-label={`删除 ${w.name}`}
							disabled={worldBusy}
							onclick={() => askDelete(w.name)}>删除</Button
						>
					{/if}
				</div>
			</Card>
		{/each}

		{#if worlds.length === 0}
			<EmptyState
				title={Object.keys(appliedFilter).length ? '没有符合标签的世界' : '还没有世界'}
			/>
		{/if}
		<div class="flex items-center justify-between gap-2 text-xs tabular-nums text-subtle">
			<span>第 {pageFrom}–{pageTo} 个，共 {total} 个</span>
			<span class="flex gap-2">
				<Button
					variant="secondary"
					disabled={offset === 0 || q.loading}
					onclick={() => (offset = Math.max(0, offset - PAGE_SIZE))}>上一页</Button
				>
				<Button
					variant="secondary"
					disabled={offset + PAGE_SIZE >= total || q.loading}
					onclick={() => (offset = offset + PAGE_SIZE)}>下一页</Button
				>
			</span>
		</div>

		<Card
			title="新建世界"
			description="创建一个新世界（独立的数据库文件与租户）。名称创建后不能修改。"
		>
			<form
				class="space-y-3"
				onsubmit={(e) => {
					e.preventDefault();
					void doCreate();
				}}
			>
				<Field
					label="名称"
					for="world-name"
					hint="字母或数字开头，其后可含字母、数字、点、下划线和连字符，最长 128 个字符，区分大小写"
					error={nameInvalid ? worldErrorMessage('invalid_world_name') : ''}
				>
					{#snippet children(f)}
						<Input
							id="world-name"
							name="world-name"
							bind:value={newName}
							invalid={f.invalid}
							aria-describedby={f.describedBy}
							placeholder="sim-1"
							autocomplete="off"
							class="max-w-64"
						/>
					{/snippet}
				</Field>
				<Field label="显示名" for="world-label" hint="可选，只用来在列表里辨认">
					<Input
						id="world-label"
						name="world-label"
						bind:value={newLabel}
						autocomplete="off"
						class="max-w-64"
					/>
				</Field>
				<Field
					label="标签"
					for="world-labels"
					hint="可选，key=value，多个用逗号分隔；建好后不能改"
					error={newLabelsParsed === null ? '每段都要写成 key=value' : ''}
				>
					{#snippet children(f)}
						<Input
							id="world-labels"
							name="world-labels"
							bind:value={newLabels}
							invalid={f.invalid}
							aria-describedby={f.describedBy}
							placeholder="env=test"
							autocomplete="off"
							class="max-w-64"
						/>
					{/snippet}
				</Field>
				<AutoSnapshotFields
					idPrefix="world-new"
					bind:onSession={newOnSession}
					bind:hours={newHours}
				/>
				<p class="text-xs text-subtle">自动快照的初值是默认世界当前的设置；不改就沿用它。</p>
				<Button type="submit" variant="soft" loading={creating} disabled={!canCreate}
					>新建世界</Button
				>
			</form>
		</Card>
	</div>
{/if}

<Drawer bind:open={deleteOpen} title={`删除世界 ${deleteTarget}`}>
	{#if deleteRow}
		<div class="space-y-3 text-sm">
			<p class="text-xs text-muted">
				将物理删除世界「{deleteRow.name}」，无法恢复；其他世界不受影响。
			</p>
			<p class="text-xs tabular-nums text-subtle">{statsLine(deleteRow)}</p>
			<Field label={`输入世界名「${deleteRow.name}」以确认`} for="world-delete-confirm">
				<Input
					id="world-delete-confirm"
					name="world-delete-confirm"
					bind:value={deleteConfirm}
					placeholder={deleteRow.name}
					autocomplete="off"
				/>
			</Field>
			<Button variant="danger" loading={deleting} disabled={!canDelete} onclick={doDelete}
				>确认删除</Button
			>
		</div>
	{/if}
</Drawer>

<Drawer bind:open={snapOpen} title={`${snapWorld} 的快照`}>
	<div class="space-y-3 text-sm">
		<div class="space-y-2">
			<Input
				name="snapshot-note"
				bind:value={snapNote}
				placeholder="备注（可选）"
				autocomplete="off"
				aria-label="快照备注"
			/>
			<Button
				variant="soft"
				loading={snapshotting}
				disabled={rollingBack !== null}
				onclick={doSnapshot}>立即拍快照</Button
			>
			{#if snapshotting}
				<p class="text-xs text-subtle">正在拷贝整库，数据量大时需要一两分钟，请不要关闭页面。</p>
			{/if}
		</div>

		{#if snapsLoading && snaps.length === 0}
			<div class="space-y-2">{#each Array(3) as _}<Skeleton class="h-12 w-full" />{/each}</div>
		{:else if snapsErr}
			<div class="space-y-2">
				<p class="text-xs text-danger">{snapsErr}</p>
				<Button variant="secondary" onclick={() => void loadSnaps(snapWorld)}>重试</Button>
			</div>
		{:else if snaps.length === 0}
			<p class="text-xs text-subtle">还没有快照。</p>
		{:else}
			<ul class="divide-y divide-border">
				{#each snaps as s (s.id)}
					<li class="flex items-start justify-between gap-2 py-2">
						<div class="min-w-0 flex-1 space-y-1">
							<div class="flex flex-wrap items-center gap-2">
								<span class="break-all tabular-nums text-fg">{s.created_at}</span>
								<Badge tone={s.kind === 'auto' ? 'info' : 'neutral'}
									>{snapshotReasonLabel(s.reason)}</Badge
								>
								{#if s.reason === 'session' && s.session_id}
									<span class="font-mono text-xs text-muted" title={s.session_id}
										>会话 {shortId(s.session_id, 8)}</span
									>
								{/if}
								{#if s.missing}<Badge tone="warn">文件缺失</Badge>{/if}
							</div>
							<div class="text-xs tabular-nums text-subtle">
								世界时间 {s.world_time || '—'} · 会话数 {s.sessions_complete ?? '—'} · {formatBytes(
									s.size_bytes
								)}
							</div>
							{#if s.note}<div class="break-words text-xs text-muted">{s.note}</div>{/if}
						</div>
						<div class="flex shrink-0 flex-wrap justify-end gap-2">
							<!-- 文件缺失的快照回滚必回 snapshot_corrupt,不给入口;删除仍可。 -->
							{#if !s.missing}
								<Button
									variant="danger"
									aria-label={`回滚到快照 ${s.created_at}`}
									loading={rollingBack === s.id}
									disabled={snapRowBusy}
									onclick={() => askRollback(s)}>回滚到此</Button
								>
							{/if}
							<Button
								variant="secondary"
								aria-label={`删除快照 ${s.created_at}`}
								loading={removingSnap === s.id}
								disabled={removingSnap !== null || rollingBack !== null}
								onclick={() => askRemoveSnap(s)}>删除</Button
							>
						</div>
					</li>
				{/each}
			</ul>
		{/if}

		{#if retention != null}
			<p class="text-xs text-subtle">
				保留最近 {retention} 份，超出时优先清理自动快照，并至少保留 1 份自动快照作为回滚锚点。
			</p>
		{/if}
	</div>
</Drawer>

<ConfirmDialog
	bind:open={removeSnapOpen}
	title="删除这份快照？"
	description={removeSnapTarget
		? `${removeSnapTarget.created_at} 的「${snapshotReasonLabel(removeSnapTarget.reason)}」快照（${formatBytes(removeSnapTarget.size_bytes)}）将被永久删除。`
		: ''}
	confirmLabel="确认删除"
	danger
	onconfirm={doRemoveSnap}
/>

<ConfirmDialog
	bind:open={reconfirmOpen}
	title={`重新确认「${reconfirmTarget}」的端点？`}
	description={`将把默认配置里各供应商当前的地址复制到世界「${reconfirmTarget}」引用它们的角色上；已删除的供应商对应的角色会解绑。客户端单独指定的端点不受影响。`}
	confirmLabel="确认"
	onconfirm={doReconfirm}
/>

<WorldRollbackDrawer
	bind:open={rollbackOpen}
	bind:busy={rollbackBusy}
	worldName={rollbackTarget?.world ?? ''}
	row={rollbackRow}
	snapshot={rollbackTarget?.snap ?? null}
	summary={rollbackTarget ? snapSummary(rollbackTarget.snap) : ''}
	onsuccess={() => q.refetch()}
/>

<Drawer bind:open={autoOpen} title={`${autoWorld} 的自动快照`}>
	<form
		class="space-y-3 text-sm"
		onsubmit={(e) => {
			e.preventDefault();
			void doSaveAuto();
		}}
	>
		<AutoSnapshotFields
			idPrefix="auto-snapshot"
			bind:onSession={autoOnSession}
			bind:hours={autoHours}
		/>
		<p class="text-xs text-subtle">
			两种触发可以同时开。自动快照失败不影响摄入，只会记录失败次数。
		</p>
		<Button type="submit" variant="soft" loading={autoSaving} disabled={!canSaveAuto}>保存</Button>
	</form>
</Drawer>

<Drawer bind:open={purgeOpen} title={`清理 ${purgeTarget} 的回滚归档`}>
	{#if purgeRow}
		<div class="space-y-3 text-sm">
			<p class="text-xs text-muted">
				将永久删除世界「{purgeRow.name}」的 {purgeRow.archives} 份回滚归档（{formatBytes(
					purgeRow.archive_bytes
				)}）。归档是每次回滚前被替换下来的库，删除后不能再手动恢复；快照不受影响。
			</p>
			<Field label={`输入世界名「${purgeRow.name}」以确认`} for="world-purge-confirm">
				<Input
					id="world-purge-confirm"
					name="world-purge-confirm"
					bind:value={purgeConfirm}
					placeholder={purgeRow.name}
					autocomplete="off"
				/>
			</Field>
			<Button variant="danger" loading={purging} disabled={!canPurge} onclick={doPurge}
				>确认清理</Button
			>
		</div>
	{/if}
</Drawer>
