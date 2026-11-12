"""SQLite 重放语义测试：原子提交、LSN 守卫、删除不遗漏、旧值不覆盖新值。"""

from __future__ import annotations

import pytest

from app.sync.pgoutput import RowChange, TransactionBatch
from app.sync.store import ResultStore, StoreError, commit_ts_to_iso

TS = 1_700_000_000_000_000


def test_commit_timestamp_uses_postgres_epoch():
    # PostgreSQL 纪元为 2000-01-01；0 微秒应对到该日期，而非 1970。
    assert commit_ts_to_iso(0).startswith("2000-01-01T00:00:00")
    # 已知值：PG 微秒 700_000_000_000_000 ≈ 2022-02-12 附近。
    iso = commit_ts_to_iso(700_000_000_000_000)
    assert iso.startswith("2022-")


def make_store(tmp_path) -> ResultStore:
    return ResultStore(str(tmp_path / "lims.db"), "test_slot")


def make_batch(
    commit_lsn: int,
    *changes: RowChange,
    end_lsn: int | None = None,
    commit_ts: int = TS,
) -> TransactionBatch:
    batch = TransactionBatch(commit_lsn=commit_lsn, commit_ts_us=commit_ts)
    batch.changes.extend(changes)
    batch.end_lsn = end_lsn if end_lsn is not None else commit_lsn + 100
    return batch


def insert(result_id: int, lsn: int, value: str = "7.1") -> RowChange:
    return RowChange("insert", 1, result_id, ("S1", "pH", value), lsn)


def update(result_id: int, lsn: int, value: str) -> RowChange:
    return RowChange("update", 1, result_id, ("S1", "pH", value), lsn)


def delete(result_id: int, lsn: int) -> RowChange:
    return RowChange("delete", 1, result_id, None, lsn)


def test_initial_state_is_not_started(tmp_path):
    store = make_store(tmp_path)
    state = store.get_state()
    assert state["confirmed_lsn"] == 0
    assert state["applied_tx_count"] == 0
    assert store.count_results() == 0


def test_insert_then_correction_keeps_latest_and_persists_lsn(tmp_path):
    store = make_store(tmp_path)
    store.apply_transaction(
        make_batch(1000, insert(1, 1000, "7.1"), end_lsn=1100)
    )
    store.apply_transaction(
        make_batch(1200, update(1, 1200, "7.9"), end_lsn=1300)
    )

    rows = store.find_by_sample_code("S1")
    assert len(rows) == 1
    assert rows[0]["measured_value"] == "7.9"
    assert rows[0]["source_lsn"] == 1200
    assert rows[0]["committed_at"] == commit_ts_to_iso(TS)
    assert store.confirmed_lsn == 1300
    assert store.get_state()["applied_tx_count"] == 2


def test_correction_in_same_transaction_overwrites_insert(tmp_path):
    """同一源事务内先插入再更正：LSN 相同，靠事务内序号保证更正生效。"""

    store = make_store(tmp_path)
    batch = TransactionBatch(commit_lsn=1000, commit_ts_us=TS, end_lsn=1100)
    batch.changes.extend(
        [
            RowChange("insert", 1, 1, ("S1", "pH", "7.1"), 1000, 0),
            RowChange("update", 1, 1, ("S1", "pH", "7.42"), 1000, 1),
        ]
    )
    store.apply_transaction(batch)

    rows = store.find_by_sample_code("S1")
    assert len(rows) == 1
    assert rows[0]["measured_value"] == "7.42"
    assert rows[0]["lsn_seq"] == 1


def test_insert_then_delete_in_same_transaction_removes_row(tmp_path):
    """同一源事务内插入后删除：删除序号更大，行必须消失（删除不遗漏）。"""

    store = make_store(tmp_path)
    batch = TransactionBatch(commit_lsn=1000, commit_ts_us=TS, end_lsn=1100)
    batch.changes.extend(
        [
            RowChange("insert", 1, 1, ("S1", "pH", "7.1"), 1000, 0),
            RowChange("delete", 1, 1, None, 1000, 1),
        ]
    )
    store.apply_transaction(batch)
    assert store.find_by_sample_code("S1") == []
    assert store.count_results() == 0


def test_multi_row_transaction_applies_atomically(tmp_path):
    store = make_store(tmp_path)
    batch = make_batch(2000, insert(1, 2000), insert(2, 2000), insert(3, 2000))
    store.apply_transaction(batch)
    assert store.count_results() == 3
    assert store.confirmed_lsn == 2100


def test_failed_transaction_rolls_back_data_and_progress(tmp_path):
    store = make_store(tmp_path)
    store.apply_transaction(make_batch(1000, insert(1, 1000), end_lsn=1100))

    bad = make_batch(2000, insert(2, 2000, ""))  # 空字符串触发校验失败
    with pytest.raises(StoreError):
        store.apply_transaction(bad)

    # 数据与确认 LSN 都停留在上一个成功事务。
    assert store.count_results() == 1
    assert store.confirmed_lsn == 1100


def test_replay_older_insert_cannot_revive_deleted_row(tmp_path):
    store = make_store(tmp_path)
    store.apply_transaction(make_batch(1000, insert(1, 1000), end_lsn=1100))
    store.apply_transaction(make_batch(2000, delete(1, 2000), end_lsn=2100))
    assert store.find_by_sample_code("S1") == []

    # 断线重连重放旧事务：旧插入 LSN 更小，不能让已删除行复活。
    store.apply_transaction(make_batch(1000, insert(1, 1000), end_lsn=1100))
    assert store.find_by_sample_code("S1") == []


def test_replay_older_update_cannot_overwrite_newer_value(tmp_path):
    store = make_store(tmp_path)
    store.apply_transaction(make_batch(1000, insert(1, 1000, "new"), end_lsn=1100))

    # 重放一个 LSN 更小的“更正”，新值必须保留。
    older = make_batch(900, update(1, 900, "stale"), end_lsn=950)
    store.apply_transaction(older)
    rows = store.find_by_sample_code("S1")
    assert rows[0]["measured_value"] == "new"
    assert rows[0]["source_lsn"] == 1000


def test_older_delete_cannot_remove_reinserted_row(tmp_path):
    store = make_store(tmp_path)
    store.apply_transaction(make_batch(1000, insert(1, 1000), end_lsn=1100))
    store.apply_transaction(make_batch(2000, delete(1, 2000), end_lsn=2100))
    # 同主键重新插入（业务允许，主键复用为新结果）。
    store.apply_transaction(make_batch(3000, insert(1, 3000, "fresh"), end_lsn=3100))
    # 重放历史删除，不能删掉 LSN 更大的新行。
    store.apply_transaction(make_batch(2000, delete(1, 2000), end_lsn=2100))

    rows = store.find_by_sample_code("S1")
    assert len(rows) == 1
    assert rows[0]["measured_value"] == "fresh"


def test_delete_is_not_missed_on_replay(tmp_path):
    store = make_store(tmp_path)
    store.apply_transaction(make_batch(1000, insert(1, 1000), end_lsn=1100))
    store.apply_transaction(
        make_batch(
            2000,
            delete(1, 2000),
            RowChange("insert", 1, 2, ("S2", "pH", "9.0"), 2000),
            end_lsn=2100,
        )
    )
    assert store.find_by_sample_code("S1") == []
    assert store.count_results() == 1  # 同事务的插入仍生效
    assert store.find_by_sample_code("S2")[0]["result_id"] == 2


def test_duplicate_confirmed_transaction_is_skipped(tmp_path):
    store = make_store(tmp_path)
    batch = make_batch(1000, insert(1, 1000), end_lsn=1100)
    store.apply_transaction(batch)
    store.apply_transaction(batch)  # 重连后重复投递

    assert store.count_results() == 1
    assert store.confirmed_lsn == 1100
    assert store.get_state()["applied_tx_count"] == 1
    assert store.get_state()["applied_row_count"] == 1


def test_text_byte_limit_enforced(tmp_path):
    store = make_store(tmp_path)
    long_value = "水" * 34  # UTF-8 每字 3 字节 -> 102 字节
    with pytest.raises(StoreError, match="100 字节"):
        store.apply_transaction(
            make_batch(1000, insert(1, 1000, long_value), end_lsn=1100)
        )


def test_query_by_sample_code_orders_by_primary_key(tmp_path):
    store = make_store(tmp_path)
    batch = TransactionBatch(commit_lsn=1000, commit_ts_us=TS, end_lsn=1100)
    batch.changes.extend(
        [
            RowChange("insert", 1, 3, ("S2", "pH", "1"), 1000),
            RowChange("insert", 1, 1, ("S2", "pH", "2"), 1000),
            RowChange("insert", 1, 2, ("S2", "pH", "3"), 1000),
        ]
    )
    store.apply_transaction(batch)
    rows = store.find_by_sample_code("S2")
    assert [row["result_id"] for row in rows] == [1, 2, 3]


def test_state_survives_reopen(tmp_path):
    db_path = tmp_path / "lims.db"
    store = ResultStore(str(db_path), "test_slot")
    store.apply_transaction(make_batch(1000, insert(1, 1000), end_lsn=1100))
    store.close()

    reopened = ResultStore(str(db_path), "test_slot")
    assert reopened.confirmed_lsn == 1100
    assert reopened.count_results() == 1
    assert reopened.find_by_sample_code("S1")[0]["measured_value"] == "7.1"
    reopened.close()
