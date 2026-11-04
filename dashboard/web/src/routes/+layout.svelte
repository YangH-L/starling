<script lang="ts">
	import '../app.css';
	import favicon from '$lib/assets/favicon.svg';
	import { onMount } from 'svelte';
	import { browser } from '$app/environment';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { api } from '$lib/api';
	import { connectWs } from '$lib/ws';
	import { currentWorld, setWorld, noteDefaultWorld, withWorld } from '$lib/world.svelte';
	import { worldsApi, type WorldRow } from '$lib/worlds';
	import { wsConn, llmConfigured, embedderConfigured, lastWsEvent } from '$lib/health';
	import { type Config, roleConfigured } from '$lib/models';
	import { ALL_NAV_ITEMS, NAV_GROUPS, matchesHref, activeNavItem } from '$lib/nav';
	import { density, applyDensity, type Density } from '$lib/ui/density';
	import NavIcon from '$lib/components/NavIcon.svelte';
	import { StatusDot, IconButton, Toaster, ThemeToggle } from '$lib/components/ui';

	const ALL_ITEMS = ALL_NAV_ITEMS;

	let { children } = $props();
	let mobileOpen = $state(false);

	// 分组折叠(持久化):分组眉标是可交互的展开/收起,与条目拉开层级。
	const COLLAPSE_KEY = 'starling_nav_collapsed';
	let collapsed = $state<Record<string, boolean>>(
		browser ? JSON.parse(localStorage.getItem(COLLAPSE_KEY) ?? '{}') : {}
	);
	function toggleGroup(title: string) {
		collapsed = { ...collapsed, [title]: !collapsed[title] };
		if (browser) localStorage.setItem(COLLAPSE_KEY, JSON.stringify(collapsed));
	}

	// nav 深链 item 的 href 带 query(如海马 /statements?consolidation_state=…);
	// 精确 href===pathname 会让这类项永不高亮、面包屑消失(T0b+T0c 引入,后续
	// 深链任务复现)。active 高亮与面包屑共用 nav.ts 的 matchesHref/activeNavItem
	// 单一逻辑(pathname 相等 + href 的 query 参数都在当前 URL 命中),已在 nav.test 覆盖。
	const isActive = (href: string) => matchesHref(href, page.url);
	let crumb = $derived(activeNavItem(page.url));

	// 全局搜索(⌘K):按面板名过滤,Enter/点击跳转。
	let search = $state('');
	let searchFocused = $state(false);
	let searchEl = $state<HTMLInputElement | null>(null);
	let hits = $derived(
		search.trim()
			? ALL_ITEMS.filter((i) => i.label.toLowerCase().includes(search.trim().toLowerCase()))
			: []
	);
	function go(href: string) {
		search = '';
		searchEl?.blur();
		goto(withWorld(href));
	}
	function onSearchKey(e: KeyboardEvent) {
		if (e.key === 'Enter' && hits.length) go(hits[0].href);
		if (e.key === 'Escape') {
			search = '';
			searchEl?.blur();
		}
	}
	$effect(() => {
		const onKey = (e: KeyboardEvent) => {
			if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
				e.preventDefault();
				searchEl?.focus();
			}
		};
		window.addEventListener('keydown', onKey);
		return () => window.removeEventListener('keydown', onKey);
	});

	// 版本 chip(侧栏底):/health 的 version + 状态点。
	let version = $state('');
	let healthOk = $state<boolean | null>(null);
	$effect(() => {
		api
			.get<{ status: string; version: string }>('/health')
			.then((h) => {
				version = h.version;
				healthOk = h.status === 'ok';
			})
			.catch(() => (healthOk = false));
	});

	// ── 世界选择器(裁定 R44) ──────────────────────────────────────────────
	// 选中的世界只在 URL ?world= 里。下拉列第一页(50 个)世界,在已载入的这一页里按输入过滤;
	// 更多世界去 /worlds(那里有标签过滤与分页)。启动时顺带取一次默认世界名。
	const WORLD_PAGE = 50;
	let worldList = $state<WorldRow[]>([]);
	let worldTotal = $state(0);
	let worldMenuOpen = $state(false);
	let worldFilter = $state('');
	let worldFilterEl = $state<HTMLInputElement | null>(null);
	let curWorld = $derived(currentWorld());
	let shownWorlds = $derived.by(() => {
		const f = worldFilter.trim().toLowerCase();
		return f
			? worldList.filter((w) => `${w.name} ${w.label}`.toLowerCase().includes(f))
			: worldList;
	});
	async function loadWorlds() {
		try {
			const r = await worldsApi.list({ offset: 0, limit: WORLD_PAGE });
			noteDefaultWorld(r.default);
			worldList = r.worlds;
			worldTotal = r.total;
		} catch {
			/* 世界列表取不到时选择器仍显示当前世界名,不挡住别的页面 */
		}
	}
	$effect(() => {
		void loadWorlds();
	});
	function toggleWorldMenu() {
		worldMenuOpen = !worldMenuOpen;
		if (worldMenuOpen) {
			worldFilter = '';
			void loadWorlds();
			setTimeout(() => worldFilterEl?.focus(), 0);
		}
	}
	function pickWorld(name: string) {
		worldMenuOpen = false;
		setWorld(name);
	}

	$effect(() => {
		const dispose = connectWs((e) => lastWsEvent.set(e));
		return dispose;
	});
	$effect(() => {
		api
			.get<Config>('/api/config')
			.then((c) => {
				// Header dots derive from role bindings now (extraction→LLM,
				// embedding→Embedder), not the removed flat llm/embedder shape.
				llmConfigured.set(roleConfigured(c, 'extraction'));
				embedderConfigured.set(roleConfigured(c, 'embedding'));
			})
			.catch(() => {
				llmConfigured.set(null);
				embedderConfigured.set(null);
			});
	});

	let connTone = $derived(
		($wsConn === 'open' ? 'ok' : $wsConn === 'connecting' ? 'warn' : 'down') as 'ok' | 'warn' | 'down'
	);
	let connLabel = $derived($wsConn === 'open' ? 'Live' : $wsConn === 'connecting' ? '连接中' : '断开');
	const cfgTone = (v: boolean | null) => (v === true ? 'ok' : v === false ? 'warn' : 'unknown');

	// 密度切换(宽松/紧凑):跟 ThemeToggle 同款分段簇 + 持久化,见 lib/ui/density.ts。
	let curDensity = $state<Density>('comfortable');
	const unsubDensity = density.subscribe((d) => (curDensity = d));
	onMount(() => {
		applyDensity(curDensity);
		return unsubDensity;
	});
	const DENSITY_MODES: { value: Density; label: string }[] = [
		{ value: 'comfortable', label: '宽松' },
		{ value: 'compact', label: '紧凑' }
	];
	function pickDensity(d: Density) {
		density.set(d);
		applyDensity(d);
	}
</script>

<svelte:head><link rel="icon" href={favicon} /></svelte:head>

<div class="flex min-h-screen">
	<nav
		class="{mobileOpen
			? 'flex'
			: 'hidden'} w-56 shrink-0 flex-col border-r border-border bg-surface md:flex"
		aria-label="主导航"
	>
		<a href={withWorld('/')} class="flex items-center gap-2.5 px-4 pb-2 pt-4">
			<span class="flex size-8 items-center justify-center rounded-control bg-brand-tint-strong text-base"
				>🪶</span
			>
			<span>
				<span class="block text-[10px] font-semibold uppercase tracking-wider text-subtle"
					>Memory</span
				>
				<span class="block text-sm font-bold leading-tight text-fg">Starling</span>
			</span>
		</a>
		<div class="flex-1 overflow-y-auto px-3 py-2">
			{#each NAV_GROUPS as g}
				<div class="mb-3">
					<button
						type="button"
						onclick={() => toggleGroup(g.title)}
						aria-expanded={!collapsed[g.title]}
						class="flex w-full items-center justify-between rounded-control px-2 py-1 text-[11px] font-semibold uppercase tracking-wider text-subtle transition hover:text-fg"
					>
						{g.title}
						<svg
							width="12"
							height="12"
							viewBox="0 0 24 24"
							fill="none"
							stroke="currentColor"
							stroke-width="2"
							stroke-linecap="round"
							stroke-linejoin="round"
							aria-hidden="true"
							class="transition-transform {collapsed[g.title] ? '-rotate-90' : ''}"
						>
							<path d="m6 9 6 6 6-6" />
						</svg>
					</button>
					{#if !collapsed[g.title]}
						<div class="mt-0.5">
							{#each g.items as n}
								<a
									href={withWorld(n.href)}
									aria-current={isActive(n.href) ? 'page' : undefined}
									onclick={() => (mobileOpen = false)}
									class="mb-0.5 flex items-center gap-2.5 rounded-control px-2.5 py-1.5 text-sm transition {isActive(
										n.href
									)
										? 'bg-brand-tint-strong font-medium text-brand'
										: 'text-muted hover:bg-brand-tint hover:text-fg'}"
								>
									<NavIcon name={n.icon} />{n.label}
								</a>
							{/each}
						</div>
					{/if}
				</div>
			{/each}
		</div>
		<div class="border-t border-border px-4 py-3">
			<div
				class="flex items-center justify-between rounded-control border border-border bg-card px-2.5 py-1.5"
			>
				<span class="text-[10px] font-semibold uppercase tracking-wider text-subtle">Version</span>
				<span class="flex items-center gap-1.5 text-xs font-medium text-fg">
					{version || '—'}
					<span
						class="size-1.5 rounded-full {healthOk === true
							? 'bg-success'
							: healthOk === false
								? 'bg-danger'
								: 'bg-subtle'}"
						aria-hidden="true"
					></span>
				</span>
			</div>
		</div>
	</nav>

	<div class="flex min-w-0 flex-1 flex-col">
		<header class="flex items-center gap-3 border-b border-border bg-bg px-5 py-2.5">
			<IconButton class="md:hidden" aria-label="菜单" onclick={() => (mobileOpen = !mobileOpen)}
				>☰</IconButton
			>
			<nav aria-label="面包屑" class="flex items-center gap-1.5 text-sm">
				<a href={withWorld('/')} class="text-subtle hover:text-fg">Starling</a>
				{#if crumb}
					<span class="text-subtle" aria-hidden="true">›</span>
					<span class="text-subtle">{crumb.group}</span>
					<span class="text-subtle" aria-hidden="true">›</span>
					<span class="font-medium text-brand">{crumb.label}</span>
				{/if}
			</nav>
			<div class="relative ml-auto">
				<button
					type="button"
					onclick={toggleWorldMenu}
					aria-haspopup="listbox"
					aria-expanded={worldMenuOpen}
					aria-label={`当前世界：${curWorld}，点击切换`}
					class="flex max-w-48 items-center gap-1.5 rounded-full border border-border bg-surface px-3 py-1.5 text-sm text-fg transition hover:bg-brand-tint focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-brand"
				>
					<span class="text-xs text-subtle">世界</span>
					<span class="truncate font-medium">{curWorld}</span>
				</button>
				{#if worldMenuOpen}
					<div
						class="absolute right-0 top-full z-50 mt-1 w-64 overflow-hidden rounded-control border border-border bg-card py-1 shadow-lg"
					>
						<input
							bind:this={worldFilterEl}
							bind:value={worldFilter}
							onkeydown={(e) => {
								if (e.key === 'Escape') worldMenuOpen = false;
								if (e.key === 'Enter' && shownWorlds.length) pickWorld(shownWorlds[0].name);
							}}
							placeholder="输入过滤世界"
							aria-label="过滤世界"
							class="mx-2 mb-1 w-[calc(100%-1rem)] rounded-control border border-border bg-surface px-2 py-1 text-sm text-fg placeholder:text-subtle"
						/>
						<ul role="listbox" aria-label="世界" class="max-h-64 overflow-y-auto">
							{#each shownWorlds as w (w.name)}
								<li role="option" aria-selected={w.name === curWorld}>
									<button
										type="button"
										onclick={() => pickWorld(w.name)}
										class="flex w-full items-center justify-between gap-2 px-3 py-1.5 text-left text-sm hover:bg-brand-tint {w.name ===
										curWorld
											? 'font-medium text-brand'
											: 'text-fg'}"
									>
										<span class="truncate">{w.name}</span>
										{#if w.label}<span class="truncate text-xs text-subtle">{w.label}</span>{/if}
									</button>
								</li>
							{:else}
								<li class="px-3 py-1.5 text-xs text-subtle">没有匹配的世界</li>
							{/each}
						</ul>
						<a
							href="/worlds"
							onclick={() => (worldMenuOpen = false)}
							class="block border-t border-border px-3 py-1.5 text-xs text-muted hover:bg-brand-tint hover:text-fg"
							>全部世界…{worldTotal > worldList.length ? `（共 ${worldTotal} 个）` : ''}</a
						>
					</div>
				{/if}
			</div>
			<div class="relative hidden sm:block">
				<input
					bind:this={searchEl}
					bind:value={search}
					onkeydown={onSearchKey}
					onfocus={() => (searchFocused = true)}
					onblur={() => setTimeout(() => (searchFocused = false), 120)}
					placeholder="搜索面板"
					aria-label="搜索面板"
					class="w-44 rounded-full border border-border bg-surface py-1.5 pl-3.5 pr-10 text-sm text-fg placeholder:text-subtle focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-brand"
				/>
				<kbd
					class="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 rounded border border-border bg-card px-1 text-[10px] text-subtle"
					>⌘K</kbd
				>
				{#if searchFocused && hits.length}
					<ul
						class="absolute right-0 top-full z-50 mt-1 w-52 overflow-hidden rounded-control border border-border bg-card py-1 shadow-lg"
					>
						{#each hits as h}
							<li>
								<button
									type="button"
									onmousedown={() => go(h.href)}
									class="flex w-full items-center justify-between px-3 py-1.5 text-left text-sm text-fg hover:bg-brand-tint"
								>
									{h.label}<span class="text-xs text-subtle">{h.group}</span>
								</button>
							</li>
						{/each}
					</ul>
				{/if}
			</div>
			<div class="flex items-center gap-3.5">
				<StatusDot tone={connTone} label={connLabel} />
				<StatusDot tone={cfgTone($llmConfigured)} label={$llmConfigured === false ? 'LLM 未配' : 'LLM'} />
				<StatusDot
					tone={cfgTone($embedderConfigured)}
					label={$embedderConfigured === false ? 'Embedder 未配' : 'Embedder'}
				/>
				<ThemeToggle />
				<div
					class="flex items-center gap-0.5 rounded-full border border-border bg-surface p-0.5"
					role="group"
					aria-label="密度"
				>
					{#each DENSITY_MODES as m}
						<button
							type="button"
							aria-label={`密度：${m.label}`}
							title={m.label}
							aria-pressed={curDensity === m.value}
							onclick={() => pickDensity(m.value)}
							class="rounded-full px-2 py-1 text-xs font-medium transition focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-brand {curDensity ===
							m.value
								? 'bg-brand-tint-strong text-brand'
								: 'text-subtle hover:text-fg'}"
						>
							{m.label}
						</button>
					{/each}
				</div>
			</div>
		</header>

		<main class="flex-1 px-6 py-5">{@render children()}</main>
	</div>
</div>

<Toaster />
