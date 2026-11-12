"""演示脚本共用的数据库连接与等待工具。"""

from __future__ import annotations

import os
import time

import psycopg2

DSN = os.environ.get(
    "LIMS_DSN", "postgresql://lims:lims@source-db:5432/lims"
)
SLOT_NAME = os.environ.get("LIMS_SLOT_NAME", "lims_lite_slot")


def connect(autocommit: bool = False):
    # psycopg2 不支持在 connect() 上传 autocommit，需在连接上设置。
    conn = psycopg2.connect(DSN)
    conn.autocommit = autocommit
    return conn


def wait_for_slot(timeout_seconds: int = 60) -> None:
    """阻塞直到持久复制槽存在。

    强制“先建槽再写入”：同步进程启动并建好槽之前，演示一律等待，
    避免任何写入发生在槽创建之前而无法被复制。
    """

    deadline = time.monotonic() + timeout_seconds
    with connect(autocommit=True) as conn, conn.cursor() as cur:
        while time.monotonic() < deadline:
            cur.execute(
                "SELECT confirmed_flush_lsn FROM pg_replication_slots "
                "WHERE slot_name = %s AND slot_type = 'logical'",
                (SLOT_NAME,),
            )
            row = cur.fetchone()
            if row is not None:
                print(f"[gate] 复制槽 {SLOT_NAME} 已就绪（confirmed_flush_lsn={row[0]}）")
                return
            print(f"[gate] 等待复制槽 {SLOT_NAME} 创建 ...")
            time.sleep(1)
    raise SystemExit(f"等待复制槽 {SLOT_NAME} 超时，演示终止（拒绝在槽前置写）")


def current_rows() -> dict[int, tuple[str, str, str]]:
    with connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT result_id, sample_code, test_item, measured_value "
            "FROM lims_test_result ORDER BY result_id"
        )
        return {row[0]: row[1:] for row in cur.fetchall()}
