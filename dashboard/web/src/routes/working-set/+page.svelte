<script lang="ts">
	import { api, worldPath } from '$lib/api';
	import { toast } from '$lib/ui/toast';
	import PageHeader from '$lib/components/PageHeader.svelte';
	import { Button, Input, Badge, CopyButton, EmptyState } from '$lib/components/ui';

	let interlocutor = $state('Alice');
	let goal = $state('');
	let budget = $state(2000);
	let ws = $state<{
		render: string;
		blocks: { label: string; content: string; tokens: number }[];
		truncated: string[];
	} | null>(null);
	let busy = $state(false);
	// 持久错误面(T12):只弹 toast 的话,渲染失败会静默退回「尚未渲染」空态,
	// 看着像用户还没点按钮。错误优先于内容,对齐 createQuery 的载入/空/错阶梯。
	let loadErr = $state('');

	async function load() {
		busy = true;
		try {
			const q = new URLSearchParams({ interlocutor, token_budget: String(budget) });
			if (goal) q.set('goal', goal);
			ws = await api.get(worldPath(`/api/working_set?${q}`), { timeoutMs: 60_000 }); // 内含 recall(网络 embed)
			loadErr = '';
		} catch (e) {
			ws = null;
			loadErr = e instanceof Error ? e.message : String(e);
			toast.error(loadErr);
		} finally {
			busy = false;
		}
	}
	let totalTokens = $derived((ws?.blocks ?? []).reduce((s, b) => s + b.tokens, 0));
	let pctBudget = $derived(Math.min(100, budget ? (totalTokens / budget) * 100 : 0));
	const SECTION_LABEL: Record<string, string> = {
		persona: '人设 Persona',
		common_ground: '共识 Common Ground',
		relevant_memories: '相关记忆',
		pending_commitments: '待办承诺',
		affect: '情感 Affect'
	};
	const hasDue = (b: { label: string; content: string }) =>
		b.label === 'pending_commitments' && b.content.includes('⚠');
</script>

<PageHeader title="工作集" subtitle="Working Set:按 token 预算组装的提示词上下文分区。" />
<div class="mb-3 flex flex-wrap items-end gap-2">
	<Input bind:value={interlocutor} placeholder="interlocutor" class="max-w-40" />
	<Input bind:value={goal} placeholder="goal (optional)" class="max-w-60" />
	<Input type="number" bind:value={budget} placeholder="token budget" class="max-w-32" />
	<Button variant="soft" loading={busy} onclick={load}>渲染</Button>
</div>
{#if loadErr}
	<EmptyState title="渲染失败" description={loadErr}>
		<Button variant="soft" loading={busy} onclick={load}>重试</Button>
	</EmptyState>
{:else if ws}
	<div class="space-y-3">
		<div class="flex flex-wrap items-center gap-2">
			<span class="text-xs text-muted">{totalTokens} / {budget} tokens</span>
			<div class="h-2 w-40 overflow-hidden rounded-full bg-surface">
				<div
					class="h-full rounded-full {pctBudget > 90 ? 'bg-warn' : 'bg-brand'}"
					style="width: {pctBudget}%"
				></div>
			</div>
			{#if ws.truncated.length}<Badge tone="warn">truncated: {ws.truncated.join(',')}</Badge>{/if}
			<div class="ml-auto"><CopyButton text={ws.render} label="复制为 prompt" /></div>
		</div>
		{#if ws.blocks.length === 0}
			<EmptyState title="空 working set" description="该 interlocutor / goal 下没有可组装的上下文。" />
		{:else}
			<div class="space-y-2">
				{#each ws.blocks as b}
					<section
						class="rounded-control border-l-4 bg-card py-2 pl-3 pr-3 {hasDue(b)
							? 'border-l-warn'
							: 'border-l-brand-border'}"
					>
						<div class="mb-1 flex items-center justify-between">
							<span
								class="text-xs font-semibold uppercase tracking-wide {hasDue(b)
									? 'text-warn'
									: 'text-brand'}"
							>
								{SECTION_LABEL[b.label] ?? b.label}
							</span>
							<span class="text-xs text-subtle">{b.tokens} tok</span>
						</div>
						<pre class="whitespace-pre-wrap break-words font-sans text-sm text-fg">{b.content}</pre>
					</section>
				{/each}
			</div>
		{/if}
	</div>
{:else if !busy}
	<EmptyState title="尚未渲染" description="填入 interlocutor 与 goal 后点渲染" />
{/if}
