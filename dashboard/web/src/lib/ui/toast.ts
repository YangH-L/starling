import { toast as sonner } from 'svelte-sonner';

export const toast = {
	success: (msg: string) => sonner.success(msg),
	error: (msg: string) => sonner.error(msg),
	warning: (msg: string) => sonner.warning(msg),
	info: (msg: string) => sonner.message(msg)
};
