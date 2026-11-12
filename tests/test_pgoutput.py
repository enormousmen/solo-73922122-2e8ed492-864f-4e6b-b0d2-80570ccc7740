"""pgoutput v1 协议解析器测试：手工构造二进制消息。"""

from __future__ import annotations

import struct

import pytest

from app.sync.config import SOURCE_TABLE
from app.sync.pgoutput import (
    EXPECTED_COLUMNS,
    EXPECTED_TYPE_OIDS,
    MSG_DELETE,
    MSG_INSERT,
    MSG_UPDATE,
    PgOutputError,
    TransactionBatch,
    UnsupportedMessageError,
    parse_begin,
    parse_commit,
    parse_message,
    parse_relation,
    parse_row_change,
)

REL_OID = 16384
COMMIT_LSN = 0x0000000016B3748
COMMIT_TS = 1_700_000_000_000_000


def _cstring(text: str) -> bytes:
    return text.encode("utf-8") + b"\x00"


def _relation_payload(
    oid: int = REL_OID,
    namespace: str = "public",
    name: str = "lims_test_result",
    columns: tuple[str, ...] = EXPECTED_COLUMNS,
    type_oids: tuple[int, ...] = EXPECTED_TYPE_OIDS,
    replica_identity: int = ord("d"),
) -> bytes:
    payload = struct.pack("!I", oid) + _cstring(namespace) + _cstring(name)
    payload += bytes([replica_identity])
    payload += struct.pack("!H", len(columns))
    for column, type_oid in zip(columns, type_oids):
        payload += b"\x01"  # column flags
        payload += _cstring(column)
        payload += struct.pack("!Ii", type_oid, -1)
    return payload


def _text_value(text: str) -> bytes:
    raw = text.encode("utf-8")
    return b"t" + struct.pack("!I", len(raw)) + raw


def _int_value(value: int) -> bytes:
    return _text_value(str(value))


def _tuple(*values: str) -> bytes:
    """构造 pgoutput TupleData：Int16 列数 + 每列 t 标记/Int32 长度/值。"""

    payload = struct.pack("!H", len(values))
    for value in values:
        payload += _text_value(value)
    return payload


def _old_key_tuple(result_id: int) -> bytes:
    """构造默认 replica identity 的 Delete 旧键：主键有值，其余 NULL。"""

    payload = struct.pack("!H", 4) + _int_value(result_id) + b"n" * 3
    return payload


def _insert_payload(result_id: int, *texts: str) -> bytes:
    # 真实 pgoutput 行消息体：relation OID + 'N' 标记 + TupleData。
    return struct.pack("!I", REL_OID) + b"N" + _tuple(str(result_id), *texts)


def _update_payload(result_id: int, *texts: str) -> bytes:
    # 默认 replica identity 下 Update：'N' 标记 + 完整新行 TupleData。
    return struct.pack("!I", REL_OID) + b"N" + _tuple(str(result_id), *texts)


def _delete_payload(result_id: int) -> bytes:
    # Delete：'K' 标记 + 旧键 TupleData。
    return struct.pack("!I", REL_OID) + b"K" + _old_key_tuple(result_id)


def _begin_payload(lsn: int = COMMIT_LSN, ts: int = COMMIT_TS, xid: int = 99) -> bytes:
    return struct.pack("!QQI", lsn, ts, xid)


def _commit_payload(lsn: int = COMMIT_LSN, end_lsn: int = COMMIT_LSN + 120) -> bytes:
    return b"\x00" + struct.pack("!QQQ", lsn, end_lsn, COMMIT_TS)


def test_parse_relation_accepts_expected_table():
    info = parse_relation(_relation_payload(), SOURCE_TABLE)
    assert info.relation_id == REL_OID
    assert info.namespace == "public"
    assert info.name == "lims_test_result"
    assert info.column_names == EXPECTED_COLUMNS
    assert info.type_oids == EXPECTED_TYPE_OIDS


def test_parse_relation_rejects_schema_change():
    changed = _relation_payload(columns=("result_id", "sample_code", "test_item"))
    with pytest.raises(UnsupportedMessageError, match="列结构"):
        parse_relation(changed, SOURCE_TABLE)

    changed_type = _relation_payload(type_oids=(23, 25, 25, 23))
    with pytest.raises(UnsupportedMessageError, match="列类型"):
        parse_relation(changed_type, SOURCE_TABLE)


def test_parse_relation_rejects_foreign_table():
    foreign = _relation_payload(name="other_table")
    with pytest.raises(UnsupportedMessageError, match="非受管表"):
        parse_relation(foreign, SOURCE_TABLE)


def test_parse_begin_and_commit_roundtrip():
    begin = parse_begin(_begin_payload())
    assert begin.commit_lsn == COMMIT_LSN
    assert begin.commit_ts_us == COMMIT_TS
    assert begin.xid == 99

    from app.sync.pgoutput import parse_commit

    commit = parse_commit(_commit_payload())
    assert commit.commit_lsn == COMMIT_LSN
    assert commit.end_lsn == COMMIT_LSN + 120


def test_parse_insert_update_delete_messages():
    relation = parse_relation(_relation_payload(), SOURCE_TABLE)

    insert = parse_row_change(MSG_INSERT, _insert_payload(1, "S1", "pH", "7.1"), relation, COMMIT_LSN)
    assert insert.kind == "insert"
    assert insert.result_id == 1
    assert insert.values == ("S1", "pH", "7.1")
    assert insert.source_lsn == COMMIT_LSN

    update = parse_row_change(
        MSG_UPDATE, _update_payload(1, "S1", "pH", "7.9"), relation, COMMIT_LSN + 10
    )
    assert update.kind == "update"
    assert update.result_id == 1
    assert update.values == ("S1", "pH", "7.9")

    delete = parse_row_change(MSG_DELETE, _delete_payload(1), relation, COMMIT_LSN + 20)
    assert delete.kind == "delete"
    assert delete.result_id == 1
    assert delete.values is None


def test_null_and_oversized_text_are_rejected():
    relation = parse_relation(_relation_payload(), SOURCE_TABLE)
    # 消息体：OID + 'N' + TupleData（Int16 列数 + 主键 + NULL 列 + 其余两列）。
    payload = struct.pack("!I", REL_OID) + b"N" + struct.pack("!H", 4) + _int_value(1)
    payload += b"n"  # sample_code 为 NULL（协议标记为 ASCII 'n'）
    payload += _text_value("COD") + _text_value("1.0")
    with pytest.raises(PgOutputError, match="不允许为 NULL"):
        parse_row_change(MSG_INSERT, payload, relation, COMMIT_LSN)

    long_text = "x" * 101
    payload = struct.pack("!I", REL_OID) + b"N" + struct.pack("!H", 4) + _int_value(1)
    payload += _text_value(long_text) + _text_value("COD") + _text_value("1.0")
    with pytest.raises(PgOutputError, match="超过上限 100"):
        parse_row_change(MSG_INSERT, payload, relation, COMMIT_LSN)


def test_truncate_and_origin_are_unsupported():
    with pytest.raises(UnsupportedMessageError, match="Truncate"):
        parse_message(b"T" + struct.pack("!IB", REL_OID, 0), {}, SOURCE_TABLE, None)
    with pytest.raises(UnsupportedMessageError, match="Origin"):
        parse_message(b"O" + struct.pack("!Q", 1), {}, SOURCE_TABLE, None)


def test_row_change_requires_open_transaction():
    with pytest.raises(PgOutputError, match="事务外"):
        parse_message(b"I" + _insert_payload(1, "S", "i", "v"), {}, SOURCE_TABLE, None)


def test_dispatch_begin_relation_row_commit():
    relations: dict = {}
    batch = TransactionBatch(commit_lsn=COMMIT_LSN, commit_ts_us=COMMIT_TS)

    kind, begin = parse_message(b"B" + _begin_payload(), relations, SOURCE_TABLE, None)
    assert kind == "begin" and begin.commit_lsn == COMMIT_LSN

    kind, _ = parse_message(
        b"R" + _relation_payload(), relations, SOURCE_TABLE, batch
    )
    assert kind == "relation" and REL_OID in relations

    kind, row = parse_message(
        b"I" + _insert_payload(7, "S7", "pH", "8.0"),
        relations,
        SOURCE_TABLE,
        batch,
    )
    assert kind == "row" and row.result_id == 7
    # parse_message 返回变更对象，由 worker 负责加入批次，这里手工追加验证。
    batch.add(row)
    assert len(batch.changes) == 1


def test_transaction_batch_enforces_100_row_limit():
    batch = TransactionBatch(commit_lsn=1, commit_ts_us=1)
    relation = parse_relation(_relation_payload(), SOURCE_TABLE)
    for index in range(100):
        change = parse_row_change(
            MSG_INSERT, _insert_payload(index + 1, "S", "i", "v"), relation, 1
        )
        batch.add(change)
    overflow = parse_row_change(
        MSG_INSERT, _insert_payload(101, "S", "i", "v"), relation, 1
    )
    with pytest.raises(PgOutputError, match="100"):
        batch.add(overflow)


def test_truncated_payload_is_rejected():
    with pytest.raises(PgOutputError, match="截断"):
        parse_relation(b"\x00\x01", SOURCE_TABLE)
