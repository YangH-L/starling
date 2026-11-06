"""只读打开路径的绑定(spec §6.3):`_core.SqliteAdapter.open_read_only` / `.read_only` /
`.schema_version()` / `_core.latest_schema_version()` 只做转发,语义由 C++ 用例
`SqliteAdapterReadOnly.*` 钉住;这里钉绑定面本身。
"""

import hashlib
import sqlite3

from starling import _core


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sidecars(path):
    return {s for s in ("-wal", "-shm") if path.with_name(path.name + s).exists()}


def test_read_only_open_forwards_and_leaves_file_alone(tmp_path):
    db = tmp_path / "world.db"
    _core.SqliteAdapter.open(str(db)).close()
    with sqlite3.connect(db) as conn:  # 模拟旧版 Starling 写出的库
        conn.execute(
            "DELETE FROM schema_migrations WHERE version = (SELECT MAX(version) FROM schema_migrations)"
        )
    conn.close()
    before_sha, before_sidecars = _sha256(db), _sidecars(db)

    reader = _core.SqliteAdapter.open_read_only(str(db))
    assert reader.read_only is True
    assert reader.write_admitted() is False
    assert reader.schema_version() == _core.latest_schema_version() - 1
    reader.close()

    assert _sha256(db) == before_sha
    assert _sidecars(db) <= before_sidecars


def test_writable_open_is_not_read_only(tmp_path):
    adapter = _core.SqliteAdapter.open(str(tmp_path / "rw.db"))
    assert adapter.read_only is False
    assert adapter.schema_version() == _core.latest_schema_version()
    adapter.close()
