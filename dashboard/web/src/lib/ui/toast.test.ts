import { describe, it, expect, vi } from 'vitest';

vi.mock('svelte-sonner', () => ({
	toast: { success: vi.fn(), error: vi.fn(), message: vi.fn(), warning: vi.fn() }
}));

import { toast } from './toast';
import { toast as sonner } from 'svelte-sonner';

describe('toast helper', () => {
	it('maps success/error/info to sonner', () => {
		toast.success('a');
		toast.error('b');
		toast.info('c');
		expect((sonner.success as any).mock.calls[0][0]).toBe('a');
		expect((sonner.error as any).mock.calls[0][0]).toBe('b');
		expect((sonner.message as any).mock.calls[0][0]).toBe('c');
	});
	it('maps warning to sonner.warning', () => {
		toast.warning('w');
		expect((sonner.warning as any).mock.calls[0][0]).toBe('w');
	});
});
