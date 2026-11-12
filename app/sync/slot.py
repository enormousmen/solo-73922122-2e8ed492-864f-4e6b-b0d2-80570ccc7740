"""逻辑复制槽与发布的持久化管理。

约束（对应需求）：

- 复制槽必须先于任何演示写入创建：槽不存在时创建，已存在时复用；
  重启进程**绝不**重建槽，避免跳过积压 WAL。
- 发布（publication）幂等创建，仅发布唯一受管表。
- 通过咨询锁保证同一时刻只有一个同步进程消费该槽。
"""

from __future__ import annotations

import logging

import psycopg2
from psycopg2.extras import LogicalReplicationConnection

from .config import PUBLICATION_NAME, SOURCE_TABLE

logger = logging.getLogger(__name__)


class SlotError(Exception):
    """复制槽初始化失败。"""


def ensure_publication(dsn: str, publication_name: str = PUBLICATION_NAME) -> None:
    """幂等创建发布，只包含唯一受管表。"""

    with psycopg2.connect(dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_publication WHERE pubname = %s", (publication_name,)
            )
            if cur.fetchone() is None:
                # 表名来自固定配置而非外部输入，可安全拼接限定名。
                cur.execute(
                    f"CREATE PUBLICATION {publication_name} FOR TABLE {SOURCE_TABLE}"
                )
                logger.info("已创建发布 %s（表 %s）", publication_name, SOURCE_TABLE)
            else:
                logger.info("发布 %s 已存在，直接复用", publication_name)


def ensure_replication_slot(dsn: str, slot_name: str) -> int:
    """确保持久逻辑复制槽存在，返回槽的起始 LSN（整数）。

    - 槽不存在：创建后返回 ``confirmed_flush_lsn``，此时槽开始保留 WAL，
      之后的演示写入都不会丢失。
    - 槽已存在：返回其当前 ``confirmed_flush_lsn``，绝不删除重建。
    """

    with psycopg2.connect(dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT slot_type, confirmed_flush_lsn "
                "FROM pg_replication_slots WHERE slot_name = %s",
                (slot_name,),
            )
            row = cur.fetchone()
            if row is not None:
                slot_type, confirmed_flush = row
                if slot_type != "logical":
                    raise SlotError(
                        f"复制槽 {slot_name} 类型为 {slot_type}，需要 logical 槽"
                    )
                logger.info(
                    "复制槽 %s 已存在（confirmed_flush_lsn=%s），复用而非重建",
                    slot_name,
                    confirmed_flush,
                )
                return _lsn_to_int(confirmed_flush)

            # 使用 SQL 接口创建，output_plugin 固定为 pgoutput。
            # 该函数返回 (slot_name, lsn)；新建槽的起始 lsn 即
            # confirmed_flush_lsn（槽创建前已有的事务不会被解码）。
            cur.execute(
                "SELECT slot_name, lsn FROM "
                "pg_create_logical_replication_slot(%s, 'pgoutput')",
                (slot_name,),
            )
            _slot_name, start_lsn = cur.fetchone()
            logger.info(
                "已创建持久逻辑复制槽 %s（起始 lsn=%s）",
                slot_name,
                start_lsn,
            )
            return _lsn_to_int(start_lsn)


def acquire_worker_lock(dsn: str, slot_name: str) -> psycopg2.extensions.connection:
    """获取会话级咨询锁，保证只有一个同步进程运行。

    返回持有锁的普通连接；连接关闭时锁自动释放。若锁已被占用则抛错退出，
    让容器编排重启而不是静默双消费。
    """

    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    # 固定的 64 位锁键，由槽名派生，同槽同名进程竞争同一把锁。
    lock_key = _lock_key(slot_name)
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (lock_key,))
        acquired = cur.fetchone()[0]
    if not acquired:
        conn.close()
        raise SlotError(
            f"已有同步进程持有复制槽 {slot_name} 的工作锁，本进程退出"
        )
    logger.info("已获取同步工作锁 pg_advisory_lock(%s)", lock_key)
    return conn


def open_replication_connection(dsn: str) -> LogicalReplicationConnection:
    """打开一条 replication=database 的逻辑复制连接。"""

    conn = psycopg2.connect(
        dsn,
        connection_factory=LogicalReplicationConnection,
        # 打开 TCP keepalive，断网/对端重启时 read_message 不会无限阻塞。
        keepalives=1,
        keepalives_idle=10,
        keepalives_interval=5,
        keepalives_count=3,
    )
    return conn


def _lsn_to_int(lsn: object) -> int:
    """psycopg2 返回的 LSN 类型支持 int() 转换；纯文本 LSN 也兼容。"""

    if isinstance(lsn, int):
        return lsn
    text = str(lsn)
    high, low = text.split("/")
    return (int(high, 16) << 32) | int(low, 16)


def _lock_key(slot_name: str) -> int:
    # FNV-1a 64 位，把槽名稳定映射到咨询锁键空间。
    h = 0xCBF29CE484222325
    for byte in slot_name.encode("utf-8"):
        h ^= byte
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    # pg_advisory_lock(bigint) 使用有符号 64 位。
    return h if h < 0x8000000000000000 else h - 0x10000000000000000
