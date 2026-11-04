// vitest 用的 $app/navigation 桩:goto 只记录调用,不真的导航。
import { vi } from 'vitest';
export const goto = vi.fn(async () => {});
