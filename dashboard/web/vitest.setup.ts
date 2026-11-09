import { cleanup } from '@testing-library/svelte';
import { afterAll } from 'vitest';

// bits-ui releases its final body scroll lock on a delayed timer (24 ms).
// Keep jsdom alive until that cleanup runs; otherwise the callback accesses
// document after Vitest has disposed the environment. Errors remain observable.
afterAll(async () => {
    cleanup();
    await new Promise((resolve) => setTimeout(resolve, 50));
});

// Node 25 exposes an incomplete localStorage unless a backing file is configured.
// Vitest maps that object onto jsdom's window, so provide the browser contract here.
if (typeof globalThis.localStorage?.getItem !== 'function') {
	class MemoryStorage implements Storage {
		readonly #values = new Map<string, string>();

		get length(): number {
			return this.#values.size;
		}

		clear(): void {
			this.#values.clear();
		}

		getItem(key: string): string | null {
			return this.#values.get(String(key)) ?? null;
		}

		key(index: number): string | null {
			return [...this.#values.keys()][index] ?? null;
		}

		removeItem(key: string): void {
			this.#values.delete(String(key));
		}

		setItem(key: string, value: string): void {
			this.#values.set(String(key), String(value));
		}
	}

	Object.defineProperty(globalThis, 'localStorage', {
		configurable: true,
		value: new MemoryStorage()
	});
}
