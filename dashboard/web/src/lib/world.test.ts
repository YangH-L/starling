import { describe, it, expect, beforeEach } from 'vitest';
import { page } from '$app/state';
import { goto } from '$app/navigation';
import {
	currentWorld, setWorld, defaultWorld, noteDefaultWorld, withWorld, refetchOnEvent
} from './world.svelte';

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

describe('refetchOnEvent(页面 ws 增量刷新判据)', () => {
	it('当前世界的写入类消息触发;别的世界忽略;recall 纯读不触发', () => {
		at('/statements?world=a');
		expect(refetchOnEvent({ type: 'world_changed', world: 'a', payload: {} })).toBe(true);
		expect(refetchOnEvent({ type: 'statement_added', world: 'a', payload: {} })).toBe(true);
		expect(refetchOnEvent({ type: 'world_changed', world: 'b', payload: {} })).toBe(false);
		expect(refetchOnEvent({ type: 'recall', world: 'a', payload: {} })).toBe(false);
		expect(refetchOnEvent(null)).toBe(false);
	});
	it('URL 没写 ?world= 时,当前世界是默认世界', () => {
		noteDefaultWorld('main');
		at('/');
		expect(refetchOnEvent({ type: 'world_changed', world: 'main', payload: {} })).toBe(true);
		expect(refetchOnEvent({ type: 'world_changed', world: 'a', payload: {} })).toBe(false);
	});
});
