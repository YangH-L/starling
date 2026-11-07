"""_core.SessionTable 与写入准入、只读冻结绑定（spec §11，Task 4.2，裁定 R49、R50、R51）。

状态机与规则全在 C++（tests/cpp/test_session_table.cpp 钉语义），这里只钉绑定：方法名与参数、HandleInfo 的字段、
错误翻译成带 code 的 WorldStoreError（或 ValueError / RuntimeError）、world_read_only_frozen 的 held_for_write、
world_require_writable 的三个参数。
"""
from __future__ import annotations

import threading

import pytest

from starling import _core

LEASE = 30000


def _code(exc: BaseException) -> str:
    return _core.world_store_error_code(exc)


def test_session_round_trip_and_handle_info():
    table = _core.SessionTable(lease_ms=LEASE, idle_close_ms=1000)
    sid = table.connect(0)
    assert isinstance(sid, str) and len(sid) == 32
    hid = table.open_handle(sid, "w", "alice", False, 0)
    info = table.resolve(hid, 1)
    assert (info.session_id, info.world, info.holder, info.read_only) == (sid, "w", "alice", False)
    table.heartbeat(sid, 10)
    table.call_enter(hid, 11)
    assert table.in_flight(hid) == 1
    table.call_exit(hid, 12)
    assert table.in_flight(hid) == 0
    assert table.world_has_live_handles("w", 12)
    assert table.world_write_held("w", 12)
    assert not table.world_write_held_by_other("w", sid, 12)
    assert table.world_write_held_by_other("w", "", 12)
    table.close_handle(hid)
    assert not table.may_close_world("w", False, 1012)
    assert table.may_close_world("w", False, 1013)
    assert not table.may_close_world("w", True, 1013)
    table.touch_world("w", 2000)
    assert not table.may_close_world("w", False, 3000)
    table.disconnect(sid)


def test_defaults_match_the_spec():
    table = _core.SessionTable()
    sid = table.connect(0)
    table.open_handle(sid, "w", "alice", False, 0)
    assert table.world_write_held("w", LEASE - 1)
    assert not table.world_write_held("w", LEASE)
    table.touch_world("idle", 0)
    assert not table.may_close_world("idle", False, 600000)
    assert table.may_close_world("idle", False, 600001)


def test_errors_carry_codes():
    table = _core.SessionTable()
    first = table.connect(0)
    second = table.connect(0)
    table.open_handle(first, "w", "alice", False, 0)
    with pytest.raises(_core.WorldStoreError) as held:
        table.open_handle(second, "w", "bob", False, 1)
    assert _code(held.value) == "world_held"
    reader = table.open_handle(second, "w", "bob", True, 1)

    # 两个会话都在 30000 过期（expire 还没跑也一样）。
    with pytest.raises(_core.WorldStoreError) as expired:
        table.resolve(reader, LEASE)
    assert _code(expired.value) == "session_expired"
    with pytest.raises(_core.WorldStoreError) as beat:
        table.heartbeat(first, LEASE)
    assert _code(beat.value) == "session_expired"
    with pytest.raises(_core.WorldStoreError) as closed:
        table.resolve("no-such-handle", 0)
    assert _code(closed.value) == "handle_closed"


def test_expire_returns_released_handles():
    table = _core.SessionTable()
    sid = table.connect(0)
    first = table.open_handle(sid, "w", "alice", False, 0)
    second = table.open_handle(sid, "w", "bob", True, 0)
    assert table.expire(LEASE - 1) == []
    assert sorted(table.expire(LEASE)) == sorted([first, second])


def test_argument_errors_map_to_python_types():
    with pytest.raises(ValueError):
        _core.SessionTable(lease_ms=0)
    table = _core.SessionTable()
    sid = table.connect(0)
    with pytest.raises(ValueError):
        table.open_handle(sid, "", "alice", False, 0)
    hid = table.open_handle(sid, "w", "alice", False, 0)
    with pytest.raises(RuntimeError):
        table.call_exit(hid, 1)   # 没有配对的 call_enter（logic_error）


def test_shared_across_threads():
    table = _core.SessionTable()
    sid = table.connect(0)
    hid = table.open_handle(sid, "w", "alice", False, 0)
    errors: list[BaseException] = []

    def worker():
        try:
            for step in range(500):
                table.call_enter(hid, step)
                table.call_exit(hid, step)
        except BaseException as exc:   # noqa: BLE001 — 报给主线程
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert table.in_flight(hid) == 0


@pytest.mark.parametrize("flags,held,frozen", [
    ({"reclaimable": True}, False, True),
    ({"reclaimable": True}, True, False),
    ({"seed": True, "read_only": True}, False, True),
    ({"seed": True, "read_only": True}, True, False),
    ({}, False, False),
    ({}, True, False),
])
def test_world_read_only_frozen_held_for_write(flags, held, frozen):
    """裁定 R49 (c)：被会话占用写入时一律 Shared；held_for_write 默认 False，位置参数与关键字都行。"""
    assert _core.world_read_only_frozen(flags, held) is frozen
    assert _core.world_read_only_frozen(flags, held_for_write=held) is frozen
    if not held:
        assert _core.world_read_only_frozen(flags) is frozen


@pytest.mark.parametrize("flags,for_client_write,held_by_other,code", [
    ({}, False, False, None),
    ({}, True, False, None),
    ({}, False, True, "world_held"),
    ({}, True, True, "world_held"),
    ({"reclaimable": True}, False, False, "world_read_only"),
    ({"reclaimable": True}, True, False, None),
    ({"reclaimable": True}, False, True, "world_held"),
    ({"reclaimable": True}, True, True, "world_held"),
    ({"seed": True, "read_only": True}, False, False, "world_read_only"),
    ({"seed": True, "read_only": True}, True, False, "world_read_only"),
    ({"seed": True, "read_only": True}, False, True, "world_held"),
    ({"seed": True, "read_only": True}, True, True, "world_held"),
])
def test_world_require_writable(flags, for_client_write, held_by_other, code):
    """裁定 R51：held_by_other → world_held；否则 decide_open_mode 为只读 → world_read_only；否则通过。"""
    call = lambda: _core.world_require_writable(   # noqa: E731
        flags, for_client_write=for_client_write, held_by_other=held_by_other)
    if code is None:
        assert call() is None
    else:
        with pytest.raises(_core.WorldStoreError) as err:
            call()
        assert _code(err.value) == code


def test_world_require_writable_defaults():
    _core.world_require_writable({})
    with pytest.raises(_core.WorldStoreError) as err:
        _core.world_require_writable({"reclaimable": True})
    assert _code(err.value) == "world_read_only"
