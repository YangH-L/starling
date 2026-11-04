import { describe, it, expect, beforeEach } from 'vitest';
import { page } from '$app/state';
import { goto } from '$app/navigation';
import { currentWorld, setWorld, defaultWorld, noteDefaultWorld, withWorld } from './world.svelte';

const at = (u: string) => {
	(page as { url: URL }).url = new URL(u, 'http://localhost');
};

beforeEach(() => {
	noteDefaultWorld('default');
	at('/');
	(goto as unknown as { mockClear: () => void }).mockClear();
});

describe('当前世界(URL ?world=)', () => {
	it('没有 ?world= 时是默认世界', () => {
		expect(currentWorld()).toBe('default');
		noteDefaultWorld('main');
		expect(defaultWorld()).toBe('main');
		expect(currentWorld()).toBe('main');
	});
	it('读 ?world=', () => {
		at('/statements?world=Run-A&q=1');
		expect(currentWorld()).toBe('Run-A');
	});
	it('setWorld 只改 URL(goto,replaceState),保留路径和其他查询参数', () => {
		at('/statements?q=1');
		setWorld('Run-A');
		expect(goto).toHaveBeenCalledWith('/statements?q=1&world=Run-A', {
			replaceState: true,
			keepFocus: true,
			noScroll: true
		});
	});
	it('setWorld 选默认世界时去掉 ?world=', () => {
		at('/statements?world=Run-A&q=1');
		setWorld('default');
		expect((goto as any).mock.calls[0][0]).toBe('/statements?q=1');
	});
	it('withWorld:URL 带 ?world= 时,导航链接保留它;否则原样', () => {
		expect(withWorld('/lens?stmt=1')).toBe('/lens?stmt=1');
		at('/statements?world=Run-A');
		expect(withWorld('/lens?stmt=1')).toBe('/lens?stmt=1&world=Run-A');
		expect(withWorld('/')).toBe('/?world=Run-A');
		expect(withWorld('/x?world=Old')).toBe('/x?world=Run-A');
	});
});
