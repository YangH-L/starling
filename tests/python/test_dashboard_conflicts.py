"""Dashboard conflicts must read the canonical edge kind written by the bus."""
import sqlite3

import pytest

from starling import _core
from starling.dashboard import queries


@pytest.mark.parametrize("tenant", ["default", "other"])
def test_conflicts_reads_lowercase_edges_and_keeps_tenant_scope(tmp_path, tenant):
    db = tmp_path / "conflicts.db"
    adapter = _core.SqliteAdapter.open(str(db))
    with sqlite3.connect(db) as conn:
        conn.executemany(
            "INSERT INTO statement_edges(id, tenant_id, src_id, dst_id, edge_kind, "
            "weight, created_at) VALUES (?, ?, 'a', 'b', ?, 1.0, '2026-11-08T00:00:00Z')",
            [("default-conflict", "default", "conflicts_with"),
             ("other-conflict", "other", "conflicts_with"),
             ("default-supersedes", "default", "supersedes")])
    result = queries.conflicts(str(db), tenant)
    assert result["by_kind"]["conflicts_with"] == 1
    assert len(result["conflicts"]) == 1
    assert result["conflicts"][0]["edge_kind"] == "conflicts_with"
    assert result["conflicts"][0]["src_id"] == "a"
    assert result["conflicts"][0]["dst_id"] == "b"
    # Keep the writer alive while the read-only query observes its committed WAL.
    del adapter
