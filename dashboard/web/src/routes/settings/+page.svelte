<script lang="ts">
	import { api, worldPath } from '$lib/api';
	import { currentWorld } from '$lib/world.svelte';
	import { apiDetail, worldErrorMessage, worldsApi } from '$lib/worlds';
	import { llmConfigured, embedderConfigured } from '$lib/health';
	import { getToken, setToken } from '$lib/token';
	import { toast } from '$lib/ui/toast';
	import { CHAT_PROVIDERS, EMBED_PROVIDERS, chatPreset } from '$lib/providers';
	import { type Config, type Prov, roleConfigured } from '$lib/models';
	import PageHeader from '$lib/components/PageHeader.svelte';
	import {
		Button, Input, Field, Card, CopyButton, ConfirmDialog, SecretInput, Select, StatusDot,
		Badge, EmptyState, Skeleton
	} from '$lib/components/ui';

	type TestResult = { ok: boolean; detail: string; ms: number };

	// Registry state: named providers + role bindings (Phase 2a). extraction +
	// embedding + chat (converse, 2c) are all bindable here.
	let providers = $state<Record<string, Prov>>({});
	// Init every role key so the role <Select bind:value> never binds `undefined`
	// before /api/config resolves: Svelte 5 throws props_invalid_value on
	// bind:value={undefined} when the child has a $bindable('') fallback, and that
	// throw propagated up through the layout and blanked the whole page.
	let roles = $state<Record<string, string>>({ extraction: '', embedding: '', chat: '' });
	// #38-C v2 threshold surface: null = use the C++ default; a number = override.
	let gistThresholds = $state<Record<string, number | null>>({
		min_holders: null,
		min_replay_count: null,
		min_confidence: null,
		similarity_threshold: null,
		entity_gist_enabled: null
	});
	let keyInputs = $state<Record<string, string>>({}); // per-provider new key (blank = keep)
	let embBaseline = $state(''); // embedding provider+dim snapshot → re-embed confirm
	let saving = $state(false);
	let confirmOpen = $state(false);
	let token = $state(getToken());
	let tests = $state<Record<string, TestResult>>({});
	let testing = $state<Record<string, boolean>>({});

	// 危险区:清空「顶栏选中的世界」的全部记忆(与其他命令一样随 ?world= 走,裁定 R47)。
	// 确认串是该世界自己的租户名,取自 GET /api/worlds/<world> 的 tenant,不再用 /api/config 的
	// (那是默认世界的租户)。详情没取到就禁用清空,绝不回退到默认世界的租户。
	// 手输一致才放行,后端再按 confirm === 租户名校验一次(POST /api/clear_memory)。
	let clearWorld = $derived(currentWorld());
	let clearInfo = $state<{ world: string; tenant: string } | null>(null);
	let clearInfoErr = $state('');
	// 只认与当前世界对应的那份详情:换世界后旧租户名立刻失效,不会串到新世界上。
	let tenant = $derived(clearInfo && clearInfo.world === clearWorld ? clearInfo.tenant : '');
	let clearReq = 0; // 防陈旧:换世界后只让最新一次的详情落地
	async function loadClearInfo(world: string) {
		const mine = ++clearReq;
		clearInfoErr = '';
		try {
			const d = await worldsApi.detail(world);
			if (mine !== clearReq) return;
			clearInfo = typeof d?.tenant === 'string' && d.tenant ? { world, tenant: d.tenant } : null;
			if (!clearInfo) clearInfoErr = '没有取到这个世界的租户名';
		} catch (e) {
			if (mine !== clearReq) return;
			clearInfo = null;
			clearInfoErr = worldErrorMessage(apiDetail(e));
		}
	}
	$effect(() => {
		const w = clearWorld;
		clearInfo = null;
		void loadClearInfo(w);
	});
	let clearConfirm = $state('');
	let clearOpen = $state(false);
	let clearing = $state(false);
	let canClear = $derived(!!tenant && clearConfirm === tenant && !clearing);

	// New-provider draft
	let draftName = $state('');
	let draftProvider = $state('openai');

	const provOpts = (list: { id: string; label: string }[]) =>
		list.map((p) => ({ value: p.id, label: p.label }));
	const nameOpts = () =>
		[{ value: '', label: '（未绑定）' }, ...Object.keys(providers).map((n) => ({ value: n, label: n }))];

	function embKey(): string {
		const name = roles['embedding'];
		const p = providers[name];
		return JSON.stringify({ n: name, b: p?.base_url, d: p?.dim, k: keyInputs[name] || '' });
	}

	// Apply a Config from the API: keep all role keys defined (so the role <Select
	// bind:value> never binds `undefined`) and seed a blank key-input per provider
	// (SecretInput also has a '' fallback, so keyInputs[name] must not be undefined).
	function applyConfig(c: Config) {
		providers = c.providers ?? {};
		roles = { extraction: '', embedding: '', chat: '', ...(c.roles ?? {}) };
		const gt = c.gist_thresholds ?? {};
		gistThresholds = {
			min_holders: gt.min_holders ?? null,
			min_replay_count: gt.min_replay_count ?? null,
			min_confidence: gt.min_confidence ?? null,
			similarity_threshold: gt.similarity_threshold ?? null,
			entity_gist_enabled: gt.entity_gist_enabled ?? null
		};
		keyInputs = Object.fromEntries(Object.keys(providers).map((n) => [n, '']));
	}

	// ── 配置加载门 ────────────────────────────────────────────────────────────
	// 此前加载失败只弹一个转瞬即逝的 toast,页面照常渲染成「什么都没配」的样子。
	// 用户此时点保存,提交的是「从未加载过的初值」,而后端对三个字段的语义并不相同:
	//   providers  {}                                → upsert 循环不执行,安全(no-op)
	//   roles      {extraction:'',embedding:'',chat:''} → cfg.roles[role]=name 是赋值,
	//              三个角色全部解绑,随即热切换 set_llm({}) / rebuild_embedder({}),
	//              抽取与嵌入当场停摆
	//   gist_thresholds 全 null → 送 {} → 后端 `is not None` 成立 → 重置为默认
	// 末了 cfg.save() 持久化进 starling.json:系统安静地不再工作,直到有人发现。
	// 触发条件并不罕见 —— /api/config 要 token,token 失效或网络抖动就走这条路。
	// 三重防护:(1) 顶部持久错误面 + 重试,明说「下面为空是没读到配置,不是真没配」;
	// (2) 保存按钮 disabled;(3) doSave 里的函数级守卫。没有整体隐藏配置区 —— 那样要么
	// 重排上百行缩进、把真实改动淹没在空白 diff 里,要么让人以为页面坏了;既然写入只能
	// 经由保存,把写入这一条路堵死即可,空态由上方错误面解释。
	let loaded = $state(false);
	let loadErr = $state('');

	async function loadConfig() {
		try {
			const c = await api.get<Config>('/api/config');
			applyConfig(c);
			embBaseline = embKey();
			loaded = true;
			loadErr = '';
		} catch (e) {
			// 不清 loaded:重试失败时保留上一次已加载的配置,总比退回空态安全。
			loadErr = e instanceof Error ? e.message : String(e);
		}
	}
	$effect(() => {
		void loadConfig();
	});

	// Keep the embedding-bound provider's dim defined — the dim <Input> has a ''
	// fallback, so an undefined dim (a freshly-added provider) throws props_invalid_value.
	$effect(() => {
		const emb = roles['embedding'];
		if (emb && providers[emb] && providers[emb].dim == null) providers[emb].dim = 1024;
	});

	let embChanged = $derived(embBaseline !== embKey());

	function addProvider() {
		const name = draftName.trim();
		if (!name) return toast.error('provider 名称必填');
		if (providers[name]) return toast.error('名称已存在');
		const preset = chatPreset(draftProvider);
		providers[name] = {
			provider: draftProvider,
			model: preset?.models[0] ?? '',
			base_url: preset?.base_url ?? ''
		};
		keyInputs[name] = ''; // seed the SecretInput slot so its bind:value isn't undefined
		draftName = '';
	}

	async function deleteProvider(name: string) {
		try {
			const c = await api.del<Config>('/api/config/provider/' + encodeURIComponent(name));
			applyConfig(c);
			llmConfigured.set(roleConfigured(c, 'extraction'));
			embedderConfigured.set(roleConfigured(c, 'embedding'));
			toast.success(`已删除 ${name}`);
		} catch (e) {
			toast.error(e instanceof Error ? e.message : String(e));
		}
	}

	async function testProvider(name: string, kind: 'llm' | 'embedder') {
		testing[name] = true;
		delete tests[name];
		try {
			const p = providers[name];
			const r = await api.post<{ ok: boolean; detail: string; latency_ms: number }>(
				'/api/config/test',
				{
					kind,
					name,
					provider: p.provider,
					model: p.model,
					base_url: p.base_url,
					...(kind === 'embedder' ? { dim: p.dim } : {}),
					...(keyInputs[name] ? { api_key: keyInputs[name] } : {})
				},
				{ timeoutMs: 90_000 }
			);
			tests[name] = { ok: r.ok, detail: r.detail, ms: r.latency_ms };
		} catch (e) {
			tests[name] = { ok: false, detail: e instanceof Error ? e.message : String(e), ms: 0 };
		} finally {
			testing[name] = false;
		}
	}

	function onSaveClick() {
		if (embChanged) confirmOpen = true;
		else void doSave();
	}

	async function doSave() {
		// 函数级守卫,不只靠按钮的 disabled:提交从未加载过的初值会解绑全部角色并重置
		// gist 阈值,且持久化进 starling.json(详见 loadConfig 上方注释)。按钮之外若
		// 日后再多一条调用路径,这道守卫仍在。
		if (!loaded) return;
		saving = true;
		try {
			const payload = {
				providers: Object.fromEntries(
					Object.entries(providers).map(([name, p]) => [
						name,
						{
							provider: p.provider,
							model: p.model,
							base_url: p.base_url,
							...(p.dim ? { dim: p.dim } : {}),
							...(keyInputs[name] ? { api_key: keyInputs[name] } : {})
						}
					])
				),
				roles,
				// #38-C v2: send only the overridden knobs; omitted → C++ default.
				gist_thresholds: {
					...(gistThresholds.min_holders != null ? { min_holders: gistThresholds.min_holders } : {}),
					...(gistThresholds.min_replay_count != null
						? { min_replay_count: gistThresholds.min_replay_count }
						: {}),
					...(gistThresholds.min_confidence != null
						? { min_confidence: gistThresholds.min_confidence }
						: {}),
					...(gistThresholds.similarity_threshold != null
						? { similarity_threshold: gistThresholds.similarity_threshold }
						: {}),
					...(gistThresholds.entity_gist_enabled != null
						? { entity_gist_enabled: gistThresholds.entity_gist_enabled }
						: {})
				}
			};
			// changing the embedding provider re-embeds every memory → widen timeout
			const c = await api.post<Config>('/api/config', payload, { timeoutMs: 120_000 });
			applyConfig(c);
			embBaseline = embKey();
			llmConfigured.set(roleConfigured(c, 'extraction'));
			embedderConfigured.set(roleConfigured(c, 'embedding'));
			toast.success('已保存');
			// Non-fatal warnings (e.g. the embedding provider rejected the config):
			// the save succeeded, but surface them so embeddings aren't silently broken.
			for (const w of c.warnings ?? []) toast.error(w);
		} catch (e) {
			toast.error(e instanceof Error ? e.message : String(e));
		} finally {
			saving = false;
		}
	}

	function saveToken() {
		setToken(token);
		toast.success('Token 已更新');
	}

	async function doClear() {
		// 确认框打开期间输入框仍可改:提交前按当前值再校验一次。
		if (!tenant || clearConfirm !== tenant) return;
		clearing = true;
		try {
			const r = await api.post<{ tenant: string; total: number; spool_discarded: number }>(
				worldPath('/api/clear_memory'),
				{ confirm: clearConfirm },
				{ timeoutMs: 120_000 }
			);
			clearConfirm = '';
			toast.success(
				r.spool_discarded
					? `已清空世界「${clearWorld}」${r.total} 行,丢弃 ${r.spool_discarded} 个待摄入会话`
					: `已清空世界「${clearWorld}」${r.total} 行`
			);
		} catch (e) {
			toast.error(e instanceof Error ? e.message : String(e));
		} finally {
			clearing = false;
		}
	}
</script>

<PageHeader
	title="设置"
	subtitle="模型 provider 注册表与任务角色绑定(抽取 / 嵌入 / 对话)、gist 固化阈值,以及访问令牌。"
/>
<div class="max-w-2xl space-y-5">
	<!-- 加载失败时给持久错误面而非转瞬即逝的 toast:下面各卡片会是空的,若不解释,
	     用户会把「加载失败」读成「什么都没配」——正是那个误读会让人去点保存。
	     Access 卡片不受影响(它纯客户端),这是有意的:加载失败的高概率原因就是 token
	     不对,那张卡是唯一的自救途径,连它一起藏会把人锁死在外面。 -->
	{#if loadErr && !loaded}
		<EmptyState title="配置加载失败" description="{loadErr}(下方各项为空是因为没读到配置,不是真的没配;保存已禁用)">
			<Button variant="soft" onclick={() => void loadConfig()}>重试</Button>
		</EmptyState>
	{:else if !loaded}
		<Skeleton class="h-24 w-full" />
	{/if}
	<!-- 审查 M-3:下面的供应商与角色只作用于默认世界和以后新建的世界;页尾的危险区却跟着 ?world= 走。 -->
	<p class="text-xs text-muted" data-testid="settings-scope-note">
		这里的供应商与角色只改默认世界和以后新建的世界；已有世界的端点在「世界」页重新确认。
	</p>
	<Card title="角色绑定" description="把每个任务绑定到一个已配 provider。">
		<div class="grid gap-3 sm:grid-cols-3">
			<Field label="extraction(抽取)" for="role-ex" hint="remember 抽取记忆用">
				<Select id="role-ex" bind:value={roles['extraction']} options={nameOpts()} />
			</Field>
			<Field label="embedding(嵌入/召回)" for="role-em" hint="改绑会重嵌已有记忆">
				<Select id="role-em" bind:value={roles['embedding']} options={nameOpts()} />
			</Field>
			<Field label="chat(对话)" for="role-chat" hint="converse 对话生成用">
				<Select id="role-chat" bind:value={roles['chat']} options={nameOpts()} />
			</Field>
		</div>
	</Card>

	<Card
		title="固化阈值(NORM gist)"
		description="多 holder 共识固化成范式的聚簇/门控参数。留空 = 用默认。语义相似阈值留空/0 = 关闭语义聚簇(仅精确匹配),设 0.85 启用。"
	>
		<div class="grid gap-3 sm:grid-cols-2">
			<Field label="最少 holder 数 K" for="gt-k" hint="一条范式需跨越的不同 holder 数,默认 3">
				<Input id="gt-k" type="number" min="1" placeholder="3" bind:value={gistThresholds.min_holders} />
			</Field>
			<Field label="最少 replay_count T" for="gt-t" hint="成员的 replay_count 下限,默认 1">
				<Input id="gt-t" type="number" min="0" placeholder="1" bind:value={gistThresholds.min_replay_count} />
			</Field>
			<Field label="confidence 下限" for="gt-c" hint="LLM 判定晋升门槛 0–1,默认 0.6">
				<Input
					id="gt-c"
					type="number"
					min="0"
					max="1"
					step="0.05"
					placeholder="0.6"
					bind:value={gistThresholds.min_confidence}
				/>
			</Field>
			<Field label="语义相似阈值 cosine" for="gt-s" hint="0/留空=关闭语义聚簇;启用建议 0.85,越高越严">
				<Input
					id="gt-s"
					type="number"
					min="0"
					max="1"
					step="0.05"
					placeholder="0(关闭)"
					bind:value={gistThresholds.similarity_threshold}
				/>
			</Field>
			<Field label="entity-gist 共识" for="gt-e" hint="对具体实体的共识 gist;1=开启,0/留空=关闭">
				<Input
					id="gt-e"
					type="number"
					min="0"
					max="1"
					step="1"
					placeholder="0(关闭)"
					bind:value={gistThresholds.entity_gist_enabled}
				/>
			</Field>
		</div>
	</Card>

	<Card title="Providers">
		<div class="space-y-4">
			{#each Object.keys(providers) as name (name)}
				{@const isEmb = roles['embedding'] === name}
				<div class="rounded-control border border-border bg-surface p-3">
					<div class="mb-2 flex items-center justify-between">
						<span class="flex items-center gap-2 text-sm font-medium text-fg">
							{name}
							{#if roles['extraction'] === name}<Badge tone="brand">extraction</Badge>{/if}
							{#if isEmb}<Badge tone="brand">embedding</Badge>{/if}
							{#if roles['chat'] === name}<Badge tone="brand">chat</Badge>{/if}
						</span>
						<Button variant="ghost" onclick={() => deleteProvider(name)}>删除</Button>
					</div>
					<div class="grid gap-2 sm:grid-cols-2">
						<Field label="provider" for="{name}-prov">
							<Select id="{name}-prov" bind:value={providers[name].provider}
								options={provOpts(isEmb ? EMBED_PROVIDERS : CHAT_PROVIDERS)} />
						</Field>
						<Field label="model" for="{name}-model">
							<Input id="{name}-model" bind:value={providers[name].model} placeholder="模型名" />
						</Field>
						<Field label="base_url" for="{name}-base" hint="留空用默认端点">
							<Input id="{name}-base" bind:value={providers[name].base_url} />
						</Field>
						{#if isEmb}
							<Field label="dim" for="{name}-dim" hint="改 dim 会重嵌">
								<Input id="{name}-dim" type="number" bind:value={providers[name].dim} />
							</Field>
						{/if}
						<Field label="api_key" for="{name}-key">
							<SecretInput id="{name}-key" bind:value={keyInputs[name]}
								placeholder={providers[name].key_set ? '已设置 · 留空不改' : 'api_key'} />
						</Field>
					</div>
					<div class="mt-2 flex flex-wrap items-center gap-3">
						<Button variant="soft" loading={testing[name]}
							onclick={() => testProvider(name, isEmb ? 'embedder' : 'llm')}>测试连接</Button>
						{#if tests[name]}
							<StatusDot tone={tests[name].ok ? 'ok' : 'down'}
								label={tests[name].ok ? `连通 · ${tests[name].detail || tests[name].ms + 'ms'}` : `失败: ${tests[name].detail}`} />
						{/if}
					</div>
				</div>
			{/each}

			<div class="flex flex-wrap items-end gap-2 border-t border-border pt-3">
				<Field label="新增 provider 名称" for="draft-name">
					<Input id="draft-name" bind:value={draftName} placeholder="如 openai-main / claude" class="max-w-44" />
				</Field>
				<Field label="类型" for="draft-prov">
					<Select id="draft-prov" bind:value={draftProvider} options={provOpts(CHAT_PROVIDERS)} class="max-w-40" />
				</Field>
				<Button variant="soft" onclick={addProvider}>添加</Button>
			</div>
		</div>
	</Card>

	<Button loading={saving} disabled={!loaded} onclick={onSaveClick}>保存</Button>

	<Card title="Access">
		<div class="space-y-3">
			<Field label="API Token" for="tok" hint="粘贴 #token=… 登录 URL,或在此直接输入">
				<div class="flex gap-2">
					<SecretInput id="tok" bind:value={token} placeholder="bearer token" />
					<CopyButton text={token} />
				</div>
			</Field>
			<Button variant="soft" onclick={saveToken}>保存 Token</Button>
		</div>
	</Card>

	<!-- 危险区:清空的是顶栏选中的世界;租户名取自该世界的详情,没取到就禁用。 -->
	<Card
		title="危险区"
		class="border-danger"
		description={tenant
			? `将清空世界「${clearWorld}」的全部记忆(租户「${tenant}」):engram、语句、事件、向量、承诺、共同基础与认知体(保留 self)。该租户尚未处理的会话摄入任务移到 spool 的 discarded/。不可撤销,其他世界不受影响。`
			: `清空世界「${clearWorld}」的全部记忆。需要先取到这个世界的租户名才能确认。`}
	>
		<div class="space-y-3">
			{#if clearInfoErr}
				<p role="alert" class="text-xs text-danger">
					没能取到世界「{clearWorld}」的租户名,不能清空:{clearInfoErr}
					<button type="button" class="underline" onclick={() => void loadClearInfo(clearWorld)}
						>重试</button
					>
				</p>
			{/if}
			<Field
				label={tenant ? `输入租户名「${tenant}」以确认` : '输入租户名以确认'}
				for="clear-confirm"
			>
				<Input
					id="clear-confirm"
					bind:value={clearConfirm}
					placeholder={tenant}
					disabled={!tenant}
					autocomplete="off"
					class="max-w-64"
				/>
			</Field>
			<Button variant="danger" loading={clearing} disabled={!canClear} onclick={() => (clearOpen = true)}
				>清空世界「{clearWorld}」</Button
			>
		</div>
	</Card>
</div>

<ConfirmDialog
	bind:open={confirmOpen}
	title="确认更改 Embedder?"
	description="改了 embedding 角色的 provider / dim 会按新设置重嵌已有记忆,可能耗时。继续?"
	confirmLabel="继续保存"
	onconfirm={doSave}
/>

<ConfirmDialog
	bind:open={clearOpen}
	title="确认清空记忆?"
	description={`将物理删除世界「${clearWorld}」(租户「${tenant}」)的全部记忆,无法恢复。`}
	confirmLabel="确认清空"
	danger
	onconfirm={doClear}
/>
