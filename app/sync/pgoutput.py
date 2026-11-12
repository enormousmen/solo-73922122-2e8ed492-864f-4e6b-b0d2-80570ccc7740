"""PostgreSQL pgoutput 协议（plugin protocol version 1）消息解析。

仅实现本同步场景需要的消息子集：

- ``B`` Begin / ``C`` Commit：事务边界，源端回滚的事务没有 Commit，
  因此不会在本地产生任何效果。
- ``R`` Relation：表结构描述。本模块只处理一张固定结构的表，
  任何结构变化（列数/类型不符）都直接报错，不做 DDL 重放。
- ``I`` Insert / ``U`` Update / ``D`` Delete：行变更。
- ``T`` Truncate、``O`` Origin、``M`` Relation 之外的消息：本场景不处理，
  遇到即抛出 :class:`UnsupportedMessageError`，避免静默漏数据。

消息格式参照 PostgreSQL 文档 “Logical Replication Message Formats”
（pgoutput，protocol version 1）。所有整数均为网络字节序。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import struct
from typing import Final

from .config import MAX_ROWS_PER_TRANSACTION, MAX_TEXT_BYTES

# 消息类型字节。
MSG_BEGIN: Final = b"B"
MSG_COMMIT: Final = b"C"
MSG_RELATION: Final = b"R"
MSG_INSERT: Final = b"I"
MSG_UPDATE: Final = b"U"
MSG_DELETE: Final = b"D"
MSG_TRUNCATE: Final = b"T"
MSG_ORIGIN: Final = b"O"

# TupleData 中每个列的标记（均为 ASCII 字符字节）。
TUPLE_NULL: Final = b"n"  # 0x6E：列值为 NULL
TUPLE_TEXT: Final = b"t"  # 0x74：文本格式的值
TUPLE_BINARY: Final = b"b"  # 0x62：二进制格式：本场景不启用
TUPLE_TOAST: Final = b"u"  # 0x75：未变更的 TOAST 值

# 本模块唯一支持的表结构（4 列，与源端 DDL 对应）。
EXPECTED_COLUMNS: Final = (
    "result_id",
    "sample_code",
    "test_item",
    "measured_value",
)
# int4 / text / text / text 的 pg_type.oid。
EXPECTED_TYPE_OIDS: Final = (23, 25, 25, 25)


class PgOutputError(Exception):
    """pgoutput 消息不符合协议约定或业务约束。"""


class UnsupportedMessageError(PgOutputError):
    """遇到本模块明确不支持的消息（如 Truncate、DDL 后的结构变化）。"""


@dataclass(frozen=True)
class RelationInfo:
    """``R`` 消息描述的表结构。"""

    relation_id: int
    namespace: str
    name: str
    column_names: tuple[str, ...]
    type_oids: tuple[int, ...]


@dataclass(frozen=True)
class BeginMessage:
    commit_lsn: int  # 事务最终提交 LSN（Begin 消息即携带）
    commit_ts_us: int  # 提交时间戳，PostgreSQL 微秒纪元值
    xid: int


@dataclass(frozen=True)
class CommitMessage:
    flags: int
    commit_lsn: int
    end_lsn: int  # 提交记录之后的 LSN，用作复制进度确认点
    commit_ts_us: int


@dataclass(frozen=True)
class RowChange:
    """一条行变更（插入/更正/删除共用）。

    对于删除，``key`` 只保证主键有值（默认 replica identity 下其余列为
    TOAST 占位）；对于插入/更正，``values`` 携带完整新行。
    ``seq_in_tx`` 是该变更在源事务内的序号，用于区分同一提交 LSN 下
    的多条变更（如同事务插入后再更正），保证重放顺序正确。
    """

    kind: str  # "insert" | "update" | "delete"
    relation_id: int
    result_id: int
    values: tuple[str, str, str] | None = None  # 样品码/项目/检测值
    source_lsn: int = 0  # 由所属事务的 Begin 消息补齐
    seq_in_tx: int = 0  # 事务内序号，从 0 开始


@dataclass
class TransactionBatch:
    """一个源事务内累积的行变更，最多 100 行。"""

    commit_lsn: int
    end_lsn: int = 0
    commit_ts_us: int = 0
    changes: list[RowChange] = field(default_factory=list)

    def add(self, change: RowChange) -> None:
        if len(self.changes) >= MAX_ROWS_PER_TRANSACTION:
            raise PgOutputError(
                f"单事务变更行数超过上限 {MAX_ROWS_PER_TRANSACTION}，拒绝重放"
            )
        # 用事务内序号补齐变更，保证同 LSN 下的先后顺序可比较。
        self.changes.append(
            replace(change, seq_in_tx=len(self.changes))
        )


class _Reader:
    """按顺序消费 pgoutput 消息负载的小工具。"""

    def __init__(self, payload: bytes) -> None:
        self._buf = memoryview(payload)
        self._pos = 0

    @property
    def remaining(self) -> int:
        return len(self._buf) - self._pos

    def take(self, size: int) -> memoryview:
        if size < 0 or self._pos + size > len(self._buf):
            raise PgOutputError("pgoutput 消息长度不足，数据被截断")
        chunk = self._buf[self._pos : self._pos + size]
        self._pos += size
        return chunk

    def uint8(self) -> int:
        return self.take(1)[0]

    def uint16(self) -> int:
        return struct.unpack("!H", self.take(2))[0]

    def uint32(self) -> int:
        return struct.unpack("!I", self.take(4))[0]

    def uint64(self) -> int:
        return struct.unpack("!Q", self.take(8))[0]

    def cstring(self) -> str:
        """以 NUL 结尾的 UTF-8 字符串（namespace、relation name、列名）。"""

        raw = bytes(self._buf[self._pos :])
        end = raw.find(b"\x00")
        if end < 0:
            raise PgOutputError("pgoutput 字符串缺少 NUL 终止符")
        try:
            text = raw[:end].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PgOutputError(f"pgoutput 字符串不是合法 UTF-8: {exc}") from exc
        self._pos += end + 1
        return text

    def expect_end(self) -> None:
        if self.remaining != 0:
            raise PgOutputError(f"pgoutput 消息存在 {self.remaining} 字节未消费的尾部数据")


def _read_text_value(reader: _Reader, column_name: str) -> bytes:
    """读取一个文本格式列值的原始字节（``t`` 标记 + Int32 长度 + 内容）。"""

    marker = bytes(reader.take(1))
    if marker == TUPLE_NULL:
        raise PgOutputError(f"列 {column_name} 不允许为 NULL")
    if marker == TUPLE_BINARY:
        raise PgOutputError(f"列 {column_name} 使用了二进制格式，本模块仅接受文本格式")
    if marker == TUPLE_TOAST:
        raise PgOutputError(f"列 {column_name} 未随消息下发（TOAST 占位），无法重放")
    if marker != TUPLE_TEXT:
        raise PgOutputError(f"列 {column_name} 出现未知列标记 {marker!r}")

    length = reader.uint32()
    return bytes(reader.take(length))


def _decode_text_column(reader: _Reader, column_name: str) -> str:
    """读取一个文本格式列值，并执行非空与 100 字节上限校验。"""

    raw = _read_text_value(reader, column_name)
    if len(raw) > MAX_TEXT_BYTES:
        raise PgOutputError(
            f"列 {column_name} 长度 {len(raw)} 字节超过上限 {MAX_TEXT_BYTES}"
        )
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PgOutputError(f"列 {column_name} 不是合法 UTF-8 文本: {exc}") from exc


def _decode_key_integer(reader: _Reader) -> int:
    """读取整数主键列（线格式与文本列一致，值为 ASCII 数字）。"""

    raw = _read_text_value(reader, "result_id")
    try:
        return int(raw.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PgOutputError(f"result_id 不是合法整数: {raw!r}") from exc


def _read_column_count(reader: _Reader, relation: RelationInfo) -> int:
    """读取 TupleData 起始的 Int16 列数并与 Relation 描述核对。"""

    count = reader.uint16()
    if count != len(relation.column_names):
        raise PgOutputError(
            f"TupleData 列数 {count} 与 Relation 描述的 "
            f"{len(relation.column_names)} 列不一致"
        )
    return count


def parse_relation(payload: bytes, expected_table: str) -> RelationInfo:
    """解析 ``R`` Relation 消息并校验是否为唯一受支持的表结构。"""

    reader = _Reader(payload)
    relation_id = reader.uint32()
    namespace = reader.cstring()
    name = reader.cstring()
    replica_identity = reader.uint8()  # d=default, n=nothing, f=full, i=index
    column_count = reader.uint16()

    column_names: list[str] = []
    type_oids: list[int] = []
    for _ in range(column_count):
        # 每列：flags(1) + name(cstring) + type OID(4) + typmod(4)
        reader.uint8()  # column flags，本场景无需使用
        column_names.append(reader.cstring())
        type_oids.append(reader.uint32())
        reader.uint32()  # atttypmod，本场景不使用
    reader.expect_end()

    full_name = f"{namespace}.{name}"
    if full_name != expected_table and name != expected_table.split(".")[-1]:
        raise UnsupportedMessageError(
            f"收到非受管表 {full_name} 的 Relation 消息，本模块仅同步 {expected_table}"
        )
    if tuple(column_names) != EXPECTED_COLUMNS:
        raise UnsupportedMessageError(
            f"表 {full_name} 列结构已变化：{tuple(column_names)} != {EXPECTED_COLUMNS}，"
            "本模块不处理结构变更"
        )
    if tuple(type_oids) != EXPECTED_TYPE_OIDS:
        raise UnsupportedMessageError(
            f"表 {full_name} 列类型已变化：{tuple(type_oids)} != {EXPECTED_TYPE_OIDS}，"
            "本模块不处理结构变更"
        )
    if replica_identity not in (ord("d"), ord("f"), ord("i")):
        raise PgOutputError(
            f"表 {full_name} 的 replica identity 为 {chr(replica_identity)!r}，"
            "删除消息无法携带主键"
        )

    return RelationInfo(
        relation_id=relation_id,
        namespace=namespace,
        name=name,
        column_names=tuple(column_names),
        type_oids=tuple(type_oids),
    )


def parse_begin(payload: bytes) -> BeginMessage:
    """解析 ``B`` Begin 消息。"""

    reader = _Reader(payload)
    commit_lsn = reader.uint64()
    commit_ts_us = reader.uint64()
    xid = reader.uint32()
    reader.expect_end()
    return BeginMessage(
        commit_lsn=commit_lsn,
        commit_ts_us=commit_ts_us,
        xid=xid,
    )


def parse_commit(payload: bytes) -> CommitMessage:
    """解析 ``C`` Commit 消息。"""

    reader = _Reader(payload)
    flags = reader.uint8()
    commit_lsn = reader.uint64()
    end_lsn = reader.uint64()
    commit_ts_us = reader.uint64()
    reader.expect_end()
    return CommitMessage(
        flags=flags,
        commit_lsn=commit_lsn,
        end_lsn=end_lsn,
        commit_ts_us=commit_ts_us,
    )


def _parse_new_tuple(reader: _Reader, relation: RelationInfo) -> tuple[int, tuple[str, str, str]]:
    """解析 Insert/Update 携带的完整新行 TupleData。

    线格式：Int16 列数 + 每列 ``marker + Int32 长度 + 值``。
    """

    _read_column_count(reader, relation)
    result_id = _decode_key_integer(reader)
    texts = tuple(
        _decode_text_column(reader, column)
        for column in relation.column_names[1:]
    )
    return result_id, texts  # type: ignore[return-value]


def _parse_old_key(reader: _Reader, relation: RelationInfo) -> int:
    """解析 Delete（或 replica identity=full 的 Update）中的旧元组主键。

    线格式同样以 Int16 列数开头；默认 replica identity 下只有主键携带值，
    其余列以下发的 NULL/TOAST 标记占位，逐列跳过即可。
    """

    _read_column_count(reader, relation)
    result_id: int | None = None
    for index, column in enumerate(relation.column_names):
        if index == 0:
            result_id = _decode_key_integer(reader)
            continue
        marker = bytes(reader.take(1))
        if marker == TUPLE_TEXT:
            # full replica identity 下旧值会完整下发，读掉长度与内容。
            length = reader.uint32()
            reader.take(length)
        elif marker in (TUPLE_NULL, TUPLE_TOAST):
            continue
        elif marker == TUPLE_BINARY:
            raise PgOutputError(f"列 {column} 使用二进制格式，无法解析")
        else:
            raise PgOutputError(f"列 {column} 出现未知列标记 {marker!r}")
    assert result_id is not None
    return result_id


def parse_row_change(
    msg_type: bytes,
    payload: bytes,
    relation: RelationInfo,
    commit_lsn: int,
) -> RowChange:
    """解析 ``I`` / ``U`` / ``D`` 行变更消息。

    ``msg_type`` 为已剥离的顶层消息类型字节，``payload`` 为剩余消息体。
    实测 pgoutput v1 行消息体布局：

    - Insert/Update：``RelOID(Int32) + 'N' + TupleData``（完整新行）；
    - Delete：``RelOID(Int32) + 'K' + TupleData``（旧键）；
    - TupleData：``Int16 列数`` + 每列 ``marker + Int32 长度 + 值``。
    """

    reader = _Reader(payload)
    relation_id = reader.uint32()
    if relation_id != relation.relation_id:
        raise PgOutputError(
            f"行变更引用 relation OID {relation_id}，"
            f"与当前已知的 {relation.relation_id} 不符（可能漏收 Relation 消息）"
        )

    option_marker = bytes(reader.take(1))
    if msg_type in (MSG_INSERT, MSG_UPDATE):
        # 'N' = 新元组不使用默认 TOAST 表达式；'O' 仅 full identity 的 Update 出现。
        if option_marker == b"O":
            _parse_old_key(reader, relation)
        elif option_marker != b"N":
            raise PgOutputError(f"{msg_type!r} 消息出现异常标记 {option_marker!r}")
        result_id, texts = _parse_new_tuple(reader, relation)
        reader.expect_end()
        kind = "insert" if msg_type == MSG_INSERT else "update"
        return RowChange(kind, relation_id, result_id, texts, commit_lsn)

    if msg_type == MSG_DELETE:
        # 'K' = 仅键；'O' = 完整旧行。
        if option_marker not in (b"K", b"O"):
            raise PgOutputError(f"Delete 消息缺少旧键标记，实际为 {option_marker!r}")
        result_id = _parse_old_key(reader, relation)
        reader.expect_end()
        return RowChange("delete", relation_id, result_id, None, commit_lsn)

    raise PgOutputError(f"未知行变更类型 {msg_type!r}")


def parse_message(
    payload: bytes,
    relations: dict[int, RelationInfo],
    expected_table: str,
    active_batch: TransactionBatch | None,
) -> tuple[str, object]:
    """按消息类型字节分派解析。

    返回 ``(消息种类, 解析结果)``；调用方据此驱动事务状态机。
    ``R`` 可以在事务外出现（初始同步/重连后），``I/U/D`` 必须位于事务内。
    """

    if not payload:
        raise PgOutputError("收到空的 pgoutput 消息")
    msg_type = payload[:1]
    body = payload[1:]

    if msg_type == MSG_BEGIN:
        if active_batch is not None:
            raise PgOutputError("收到 Begin 时仍有未提交事务，协议状态错乱")
        return "begin", parse_begin(body)
    if msg_type == MSG_COMMIT:
        if active_batch is None:
            raise PgOutputError("事务外收到 Commit 消息，协议状态错乱")
        return "commit", parse_commit(body)
    if msg_type == MSG_RELATION:
        info = parse_relation(body, expected_table)
        relations[info.relation_id] = info
        return "relation", info
    if msg_type in (MSG_INSERT, MSG_UPDATE, MSG_DELETE):
        if active_batch is None:
            raise PgOutputError("事务外收到行变更消息，协议状态错乱")
        target_name = expected_table.split(".")[-1]
        relation = next(
            (item for item in relations.values() if item.name == target_name),
            None,
        )
        if relation is None:
            raise PgOutputError("收到行变更前未收到对应表的 Relation 消息")
        return "row", parse_row_change(
            msg_type, body, relation, active_batch.commit_lsn
        )
    if msg_type == MSG_TRUNCATE:
        raise UnsupportedMessageError("收到 Truncate 消息，本模块不处理该操作")
    if msg_type == MSG_ORIGIN:
        raise UnsupportedMessageError("收到 Origin 消息，本模块不处理复制源标记")

    raise UnsupportedMessageError(f"收到不支持的 pgoutput 消息类型 {msg_type!r}")
