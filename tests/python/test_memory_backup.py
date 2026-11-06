"""Memory.backup_to:给 library 留一份自洽的单文件副本(rise 部署前的起点副本)。

副本由 VACUUM INTO 生成:rollback 日志模式的单文件,不带 -wal,先写 `<dest>.partial`、
过了 integrity_check 才改名到目标,所以目标路径上要么没有文件、要么是完整副本。

离线:stub LLM、默认 stub 嵌入器,不发任何真实网络请求。
"""
import sqlite3

import pytest

import starling

# 与 tests/python/test_memory_digest.py 的 CANNED_JSON 相同:单元素 JSON 数组。
CANNED_JSON = (
    '[{"holder":"alice","holder_perspective":"FIRST_PERSON",'
    '"subject":"cog-bob","predicate":"responsible_for","object":"auth",'
    '"modality":"BELIEVES","polarity":"POS","nesting_depth":0}]'
)


def _db(tmp_path, name):
    return starling.Memory.open(str(tmp_path / name), agent="alice",
                                llm=starling.make_stub_llm(default_response=CANNED_JSON))


def _fill(mem):
    mem.remember_event("e#0", "bob:我负责鉴权模块", interlocutor="bob")
    mem.embed_pending("2026-01-01T00:01:00Z")


def test_backup_is_self_contained_and_same_digest(tmp_path):
    a = _db(tmp_path, "a.db"); _fill(a)
    a.backup_to(tmp_path / "copy.db")
    assert not (tmp_path / "copy.db-wal").exists()
    with sqlite3.connect(str(tmp_path / "copy.db")) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    b = _db(tmp_path, "copy.db")
    assert b.digest() == a.digest()
    assert [h["content_id"] for h in b.recall("我负责鉴权模块", include_volatile=True)] == \
           [h["content_id"] for h in a.recall("我负责鉴权模块", include_volatile=True)]
    assert b.next_seq() == a.next_seq()


def test_backup_refuses_existing_destination(tmp_path):
    a = _db(tmp_path, "a.db"); (tmp_path / "copy.db").write_bytes(b"x")
    with pytest.raises(FileExistsError):
        a.backup_to(tmp_path / "copy.db")
    assert (tmp_path / "copy.db").read_bytes() == b"x"
    assert not (tmp_path / "copy.db.partial").exists()


def _downgrade_to_0037(path):
    # 造一份"0038 之前的 Starling 写下的库":去掉 events 表与 0038 起的全部迁移记录
    # (0039 重建的也是 events,只删 38 会让表形与迁移记录对不上)。
    with sqlite3.connect(str(path)) as c:
        c.execute("DROP TABLE events")
        c.execute("DELETE FROM schema_migrations WHERE version >= 38")


def test_old_schema_library_backup_opens_with_new_version(tmp_path):
    old = _db(tmp_path, "old.db")
    old.remember("Bob owns the auth module", now="2026-01-01T00:00:00Z")
    old.embed_pending("2026-01-01T00:00:01Z"); old.close()
    _downgrade_to_0037(tmp_path / "old.db")
    with sqlite3.connect(str(tmp_path / "old.db")) as c:      # 旧版本自己做的副本
        c.execute(f"VACUUM INTO '{tmp_path / 'old-copy.db'}'")
    reopened = _db(tmp_path, "old.db")                       # 新版本打开原库,自动迁到 0038
    reopened.backup_to(tmp_path / "new-copy.db")
    for name in ("old-copy.db", "new-copy.db"):
        m = _db(tmp_path, name)
        assert m.next_seq() == 0
        assert m.digest() == reopened.digest()
        assert m.recall("Bob owns the auth module", include_volatile=True)
