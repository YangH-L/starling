// vitest 用的 $app/state 桩(vitest.config.ts 里别名到这里):真模块依赖 SvelteKit 运行时,
// 单测里没有。测试直接改 page.url 来模拟地址栏。
export const page: { url: URL } = { url: new URL('http://localhost/') };
