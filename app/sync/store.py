"""SQLite 本地分析库：镜像表、同步进度与状态查询。

设计要点：

- 镜像表与源表列一一对应，另加 ``source_lsn`` 与 ``committed_at``，
  使“本地结果与提交 LSN 同时保存”。
- 每批源事务在**同一个 SQLite 事务**内落库，并在同一事务里更新
  ``sync_state`` 的已确认 LSN；落库失败则整体回滚，进度不前进。
- UPSERT/DELETE 都带 LSN 守卫：重放旧消息不会覆盖较新的值，
  删除后重放的旧插入也不会让数据复活，保证重放幂等。
"""

from __future__ import annotations

from contextlib import contextmanager
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .config import MAX_TEXT_BYTES
from .pgoutput import RowChange, TransactionBatch

STATE_SINGLETON_ID = 1
STATE_NOT_STARTED = "not_started"
STATE_STREAMING = "streaming"


# pgoutput Commit 消息的时间戳以 2000-01-01 UTC 为纪元（PostgreSQL 纪元），
# 单位为微秒。该常量即该纪元相对 Unix 纪元的偏移量。
_POSTGRES_EPOCH_OFFSET_US = 946_684_800_000_000


def commit_ts_to_iso(commit_ts_us: int) -> str:
    """PostgreSQL 纪元微秒时间戳 -> UTC ISO-8601 文本。"""

    unix_us = commit_ts_us + _POSTGRES_EPOCH_OFFSET_US
    return (
        datetime.fromtimestamp(unix_us / 1_000_000, tz=timezone.utc)
        .replace(microsecond=unix_us % 1_000_000)
        .isoformat()
    )


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS lims_test_result (
    result_id      INTEGER PRIMARY KEY,
    sample_code    TEXT NOT NULL,
    test_item      TEXT NOT NULL,
    measured_value TEXT NOT NULL,
    source_lsn     INTEGER NOT NULL,
    lsn_seq        INTEGER NOT NULL DEFAULT 0,
    committed_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_state (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    slot_name         TEXT NOT NULL,
    confirmed_lsn     INTEGER NOT NULL DEFAULT 0,
    last_commit_lsn   INTEGER NOT NULL DEFAULT 0,
    last_end_lsn      INTEGER NOT NULL DEFAULT 0,
    last_committed_at TEXT,
    applied_tx_count  INTEGER NOT NULL DEFAULT 0,
    applied_row_count INTEGER NOT NULL DEFAULT 0,
    updated_at        TEXT NOT NULL
);
"""


class StoreError(Exception):
    """本地落库失败。"""


class ResultStore:
    """封装 SQLite 连接与同步重放/查询逻辑。"""

    def __init__(self, path: str, slot_name: str) -> None:
        self.path = path
        self.slot_name = slot_name
        parent = Path(path).parent
        parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False：API 线程只读、worker 线程只写，靠短事务隔离。
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._apply_pragmas()
        self._init_schema()

    def _apply_pragmas(self) -> None:
        # WAL 允许 worker 写入时 API 仍可并发读取；NORMAL 同步级别在 WAL 下
        # 依然保证事务原子性，崩溃时 WAL 会自动恢复。
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(SCHEMA_SQL)
            self._conn.execute(
                "INSERT OR IGNORE INTO sync_state "
                "(id, slot_name, confirmed_lsn, last_commit_lsn, last_end_lsn, "
                " last_committed_at, applied_tx_count, applied_row_count, updated_at) "
                "VALUES (1, ?, 0, 0, 0, NULL, 0, 0, ?)",
                (self.slot_name, _now_iso()),
            )

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()

    # ----- 同步进度 -------------------------------------------------

    def get_state(self) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM sync_state WHERE id = ?", (STATE_SINGLETON_ID,)
        ).fetchone()
        if row is None:  # pragma: no cover - 初始化时已插入
            raise StoreError("sync_state 未初始化")
        return row

    @property
    def confirmed_lsn(self) -> int:
        return int(self.get_state()["confirmed_lsn"])

    def touch_activity(self) -> None:
        """更新活动时间戳但不改变 LSN，供 API 判断同步是否在线。"""

        with self._conn:
            self._conn.execute(
                "UPDATE sync_state SET updated_at = ? WHERE id = ?",
                (_now_iso(), STATE_SINGLETON_ID),
            )

    # ----- 事务重放 -------------------------------------------------

    def apply_transaction(self, batch: TransactionBatch) -> None:
        """把一个已提交的源事务整笔写入本地，并推进确认 LSN。

        全部操作在单个 SQLite 事务中完成；任何异常都会回滚，
        ``confirmed_lsn`` 不会前进，复制进度也就不会被确认。
        """

        if not batch.changes:
            # 空事务（理论上 pgoutput 不会下发）直接记录进度。
            end_lsn = batch.end_lsn or batch.commit_lsn
        else:
            end_lsn = batch.end_lsn

        if end_lsn <= self.confirmed_lsn:
            # 断线重连后重复收到已确认过的事务：跳过，保证幂等。
            return

        committed_at = commit_ts_to_iso(batch.commit_ts_us)
        try:
            with self._conn:
                for change in batch.changes:
                    self._apply_change(change, committed_at)
                self._advance_state(batch, end_lsn, committed_at)
        except sqlite3.Error as exc:
            raise StoreError(f"SQLite 落库失败，事务已回滚: {exc}") from exc

    def _apply_change(self, change: RowChange, committed_at: str) -> None:
        if change.kind in ("insert", "update"):
            assert change.values is not None
            sample_code, test_item, measured_value = change.values
            self._validate_texts(sample_code, test_item, measured_value)
            # 版本守卫：仅当消息的 (LSN, 事务内序号) 严格新于当前行时才写入。
            # 同一事务内“先插入后更正”两者 LSN 相同但序号递增，更正生效；
            # 断线重连重放旧消息时版本更旧，不会覆盖新值，也不会让已删除行复活。
            self._conn.execute(
                """
                INSERT INTO lims_test_result
                    (result_id, sample_code, test_item, measured_value,
                     source_lsn, lsn_seq, committed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(result_id) DO UPDATE SET
                    sample_code    = excluded.sample_code,
                    test_item      = excluded.test_item,
                    measured_value = excluded.measured_value,
                    source_lsn     = excluded.source_lsn,
                    lsn_seq        = excluded.lsn_seq,
                    committed_at   = excluded.committed_at
                WHERE excluded.source_lsn > lims_test_result.source_lsn
                   OR (excluded.source_lsn = lims_test_result.source_lsn
                       AND excluded.lsn_seq > lims_test_result.lsn_seq)
                """,
                (
                    change.result_id,
                    sample_code,
                    test_item,
                    measured_value,
                    change.source_lsn,
                    change.seq_in_tx,
                    committed_at,
                ),
            )
        elif change.kind == "delete":
            # 版本守卫：删除版本不早于现存行即生效（同事务内插入后删除，
            # LSN 相同但删除序号更大）；重放历史删除不会删掉后来重新插入
            # 的新行（其 LSN 更大），删除也不会被旧插入复活。
            self._conn.execute(
                """
                DELETE FROM lims_test_result
                 WHERE result_id = ?
                   AND (source_lsn < ?
                        OR (source_lsn = ? AND lsn_seq <= ?))
                """,
                (
                    change.result_id,
                    change.source_lsn,
                    change.source_lsn,
                    change.seq_in_tx,
                ),
            )
        else:  # pragma: no cover - 解析器已限制取值
            raise StoreError(f"未知变更类型 {change.kind!r}")

    def _advance_state(
        self,
        batch: TransactionBatch,
        end_lsn: int,
        committed_at: str,
    ) -> None:
        self._conn.execute(
            """
            UPDATE sync_state
               SET confirmed_lsn   = ?,
                   last_commit_lsn = ?,
                   last_end_lsn    = ?,
                   last_committed_at = ?,
                   applied_tx_count  = applied_tx_count + 1,
                   applied_row_count = applied_row_count + ?,
                   updated_at      = ?
             WHERE id = ?
            """,
            (
                end_lsn,
                batch.commit_lsn,
                end_lsn,
                committed_at,
                len(batch.changes),
                _now_iso(),
                STATE_SINGLETON_ID,
            ),
        )

    @staticmethod
    def _validate_texts(*values: str) -> None:
        for value in values:
            if not value:
                raise StoreError("文本列不允许为空字符串")
            if len(value.encode("utf-8")) > MAX_TEXT_BYTES:
                raise StoreError(f"文本列超过 {MAX_TEXT_BYTES} 字节上限: {value!r}")

    # ----- 查询 API -------------------------------------------------

    def find_by_sample_code(self, sample_code: str) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                """
                SELECT result_id, sample_code, test_item, measured_value,
                       source_lsn, lsn_seq, committed_at
                  FROM lims_test_result
                 WHERE sample_code = ?
                 ORDER BY result_id
                """,
                (sample_code,),
            ).fetchall()
        )

    def count_results(self) -> int:
        return int(
            self._conn.execute("SELECT COUNT(*) FROM lims_test_result").fetchone()[0]
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def open_store(path: str, slot_name: str) -> Iterator[ResultStore]:
    store = ResultStore(path, slot_name)
    try:
        yield store
    finally:
        store.close()


def database_exists(path: str) -> bool:
    """供 API 判断同步是否从未启动（数据库文件尚不存在）。"""

    return os.path.exists(path)
