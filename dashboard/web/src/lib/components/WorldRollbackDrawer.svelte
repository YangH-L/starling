<script lang="ts">
	import { toast } from '$lib/ui/toast';
	import { Button, Drawer, Field, Input } from '$lib/components/ui';
	import {
		apiDetail,
		worldErrorMessage,
		worldsApi,
		type RollbackResult,
		type WorldRow
	} from '$lib/worlds';

	// W3/W4 — 回滚到快照(语义 A:丢掉快照之后的写入)的确认抽屉,/worlds 与 /sessions 共用:
	// 手输世界名确认 → 同一个 rollback API → 同一套错误文案与刷新规则。
	// 被替换的库不删,改名进该世界的 archive/;注册表不变,所以快照列表回滚前后一样。
	let {
		open = $bindable(false),
		busy = $bindable(false),
		worldName,
		row,
		snapshot,
		summary,
		blocked = false,
		onsuccess
	}: {
		open?: boolean;
		/** 回滚请求在途。已回滚但重开失败时保持 true 直到整页重载。 */
		busy?: boolean;
		/** 目标世界名(抽屉标题用;列表里那一行可能暂时缺失)。 */
		worldName: string;
		/** 目标世界的最新一行:抽屉开着时列表可能因 ws 事件刷新,每次按最新行重判。 */
		row: WorldRow | undefined;
		snapshot: { id: string; created_at: string } | null;
		/** 「目标快照：」之后的描述,由调用页按自己的语境拼。 */
		summary: string;
		/** 调用页另有会动引擎的请求在途时为 true,确认键禁用。 */
		blocked?: boolean;
		/** 回滚成功、抽屉已关之后调用(调用页据此重取列表)。 */
		onsuccess?: (r: RollbackResult) => void | Promise<void>;
	} = $props();

	let confirm = $state('');
	// 每次打开都清空确认名:上一次输入的名字不能带到下一次回滚。
	$effect(() => {
		if (open) confirm = '';
	});

	let canRollback = $derived(
		!!snapshot && !!row && confirm === row.name && !busy && !blocked
	);

	async function doRollback() {
		const snap = snapshot;
		const target = row;
		if (!canRollback || !snap || !target) return; // 函数级守卫:前端校验一次,后端 confirm_mismatch 再校验一次
		const world = target.name;
		busy = true;
		let r: RollbackResult;
		try {
			r = await worldsApi.rollback(world, snap.id, confirm);
		} catch (e) {
			const detail = apiDetail(e);
			toast.error(worldErrorMessage(detail));
			// 库已经换成快照的内容、只是引擎没能重开:页面缓存的全是回滚前的数据,整页刷新,
			// busy 保持置位直到重载。
			if (detail === 'rollback_reopen_failed') {
				location.reload();
				return;
			}
			busy = false;
			return;
		}
		// 回滚前开着的世界,引擎已在恢复后的库上重新打开(reopened);所有世界并行在线,
		// 页面按需取数,不用整页刷新。回滚会广播 world_rolled_back,各页自己重取。
		toast.success(`已将「${world}」回滚到 ${snap.created_at} 的快照`);
		busy = false;
		open = false;
		confirm = '';
		await onsuccess?.(r);
	}
</script>

<Drawer bind:open title={`回滚世界 ${worldName}`}>
	{#if snapshot && row}
		<div class="space-y-3 text-sm">
			<p class="break-all text-xs tabular-nums text-subtle">目标快照：{summary}</p>
			<p class="text-xs text-muted">
				该世界在这份快照之后写入的所有记忆都会被丢弃。当前的库会改名保留在该世界的 archive 目录，可手动恢复。
			</p>
			{#if row.open}
				<p class="text-xs text-warn">
					这个世界现在开着：回滚期间会停止它的后台维护与摄入并重建引擎，其他世界不受影响。
				</p>
			{/if}
			<Field label={`输入世界名「${row.name}」以确认`} for="world-rollback-confirm">
				<Input
					id="world-rollback-confirm"
					name="world-rollback-confirm"
					bind:value={confirm}
					placeholder={row.name}
					autocomplete="off"
				/>
			</Field>
			<Button variant="danger" loading={busy} disabled={!canRollback} onclick={doRollback}
				>确认回滚</Button
			>
			{#if busy}
				<p class="text-xs text-subtle">
					正在校验并复制快照，数据量大时需要一两分钟，请不要关闭页面。
				</p>
			{/if}
		</div>
	{/if}
</Drawer>
