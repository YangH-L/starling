// vitest 用的 $app/state 桩(vitest.config.ts 里别名到这里):真模块依赖 SvelteKit 运行时,
// 单测里没有。用 $state 包一层,让测试改 page.url 时依赖它的 $effect 会重跑(模拟地址栏变化)。
export const page: { url: URL } = $state({ url: new URL('http://localhost/') });
