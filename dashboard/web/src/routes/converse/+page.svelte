<script lang="ts">
	import { api, type ConverseResponse } from '$lib/api';
	import { explicitWorld, withWorld } from '$lib/world.svelte';
	import { streamConverse } from '$lib/ws';
	import { toast } from '$lib/ui/toast';
	import { worldErrorMessage } from '$lib/worlds';
	import PageHeader from '$lib/components/PageHeader.svelte';
	import { Button, Textarea, Badge, Chip, EmptyState, StatusDot, Select } from '$lib/components/ui';

	type Turn = {
		role: 'user' | 'assistant';
		text: string;
		trace?: ConverseResponse;
		streaming?: boolean;
		/** 'grounding' = 模型正在写用户看不到的证据块(首个字之前的等待)。 */
		status?: string;
	};
	let turns = $state<Turn[]>([]);
	let input = $state('');
	let busy = $state(false);

	// Per-turn model selection (#35): pick a registry provider for the chat reply,
	// or '' to use the bound chat role. Names come from the same provider registry
	// the Settings page manages.
	let providers = $state<string[]>([]);
	let chatProvider = $state('');
	$effect(() => {
		api
			.get<{ providers?: Record<string, unknown> }>('/api/config')
			.then((c) => (providers = Object.keys(c.providers ?? {})))
			.catch(() => {});
	});
	const providerOpts = () => [
		{ value: '', label: '模型:默认(chat 角色)' },
		...providers.map((p) => ({ value: p, label: '模型:' + p }))
	];

	// 流式一轮(#37):WS /ws/converse。先推一个空的 assistant turn 作流式靶,
	// onToken 逐 delta 追加到 .text(Svelte 5 深层响应),onDone 落最终 reply +
	// 轨迹,onError 出错。一轮 = recall + 生成 + 沉淀,沉淀在流完之后(服务端保证
	// 即便中途断开也跑完落库)。
	function send() {
		const msg = input.trim();
		if (!msg) return;
		turns = [...turns, { role: 'user', text: msg }];
		input = '';
		busy = true;
		const idx = turns.length; // assistant turn 的下标(下面紧接 push)
		turns = [...turns, { role: 'assistant', text: '', streaming: true }];
		streamConverse(
			{
				message: msg,
				// 只在 URL 明确写了 ?world= 时才带 world;否则省略,由服务端取它自己的默认世界。
				...(explicitWorld() ? { world: explicitWorld()! } : {}),
				...(chatProvider ? { provider: chatProvider } : {})
			},
			{
				onStatus: (status) => {
					turns[idx].status = status;
				},
				onToken: (delta) => {
					turns[idx].text += delta;
				},
				onDone: (outcome) => {
					turns[idx].text = outcome.ok
						? outcome.reply
						: `（无回复:${outcome.error || 'generate 失败'}）`;
					turns[idx].trace = outcome;
					turns[idx].streaming = false;
					busy = false;
				},
				onError: (code) => {
					if (code === 'llm_not_configured')
						toast.error('未配置 LLM:去「模型」页绑定 extraction / chat 角色');
					else if (code === 'world_busy') toast.error(worldErrorMessage(code));
					else toast.error('对话出错:' + code);
					if (!turns[idx].text) turns[idx].text = `（出错:${code}）`;
					turns[idx].streaming = false;
					busy = false;
				}
			}
		);
	}
</script>

<PageHeader
	title="对话 / Converse"
	subtitle="带记忆的聊天:每轮取相关记忆注入 → 生成回复 → 把对话沉淀回记忆。展开看本轮认知轨迹。"
/>

<div class="mx-auto flex max-w-2xl flex-col gap-3">
	{#if turns.length === 0}
		<EmptyState title="开始对话" description="发一条消息。每轮会摊开:取了哪些记忆、是否拒答、沉淀了什么。" />
	{/if}
	<ul class="space-y-3">
		{#each turns as t}
			<li class="flex {t.role === 'user' ? 'justify-end' : 'justify-start'}">
				<div class="max-w-[85%] space-y-1">
					<div
						class="rounded-control px-3 py-2 text-sm {t.role === 'user'
							? 'bg-brand-tint text-fg'
							: 'border border-border bg-surface text-fg'}"
					>
						{#if t.streaming && !t.text && t.status === 'grounding'}<span
								class="text-subtle"
								role="status">正在核对记忆…</span
							>{/if}<span class="whitespace-pre-wrap">{t.text}</span>{#if t.streaming}<span
								class="ml-0.5 inline-block animate-pulse text-subtle">▌</span
							>{/if}
					</div>
					{#if t.trace}
						{@const tr = t.trace}
						<details class="text-xs text-muted">
							<summary class="cursor-pointer select-none">
								本轮认知轨迹{tr.abstained ? ' · 拒答' : ''}{tr.statement_ids.length
									? ` · 沉淀 ${tr.statement_ids.length} 条`
									: ''}{!tr.remember_ok && tr.ok ? ' · 记忆未落库' : ''}
							</summary>
							<div class="mt-1 space-y-2">
								<div>
									<span class="text-subtle">注入记忆(Context Pack):</span>
									<pre
										class="mt-1 overflow-x-auto whitespace-pre-wrap rounded bg-bg p-2 font-mono text-subtle">{tr.context_pack ||
											'（空)'}</pre>
								</div>
								<div class="flex flex-wrap items-center gap-2">
									{#if tr.abstained}<Badge tone="warn">主动拒答</Badge>{/if}
									<StatusDot
										tone={tr.remember_ok ? 'ok' : tr.ok ? 'warn' : 'down'}
										label={tr.remember_ok
											? '已沉淀'
											: tr.ok
												? `记忆未落库${tr.remember_error ? ': ' + tr.remember_error : ''}`
												: '无回复'}
									/>
									{#each tr.statement_ids as id}
										<a href={withWorld('/statements')} title={id}><Chip>{id.slice(0, 8)}…</Chip></a>
									{/each}
									{#if tr.gen_total_tokens || tr.gen_latency_ms}
										<span class="text-subtle"
											>· 回复 {tr.gen_total_tokens} tok · {tr.gen_latency_ms}ms</span
										>
									{/if}
								</div>
							</div>
						</details>
					{/if}
				</div>
			</li>
		{/each}
	</ul>

	<div class="sticky bottom-4 space-y-2 rounded-control border border-border bg-card p-2">
		{#if providers.length}
			<Select bind:value={chatProvider} options={providerOpts()} class="max-w-56" />
		{/if}
		<div class="flex items-end gap-2">
			<div class="flex-1">
				<Textarea bind:value={input} rows={2} placeholder="说点什么…" />
			</div>
			<Button loading={busy} disabled={!input.trim()} onclick={send}>发送</Button>
		</div>
	</div>
</div>
