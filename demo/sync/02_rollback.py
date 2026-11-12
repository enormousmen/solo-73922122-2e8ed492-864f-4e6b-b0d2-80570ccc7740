"""阶段 2：回滚演示——源端回滚的事务在本地不生效。

插入两条数据后显式 ROLLBACK；同步进程永远收不到该事务的 Commit 消息，
本地镜像库中不应出现 2001/2002。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import connect, current_rows, wait_for_slot


def main() -> None:
    wait_for_slot()

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO lims_test_result (result_id, sample_code, test_item, measured_value) "
            "VALUES (2001, 'S-ROLLBACK', 'pH', '9.99'),"
            "       (2002, 'S-ROLLBACK', 'COD', '88.8')"
        )
        # 显式回滚：pgoutput 只会下发 Begin，没有 Commit，同步侧整笔丢弃。
        conn.rollback()

    rows = current_rows()
    assert 2001 not in rows and 2002 not in rows, "回滚数据不应存在于源表"
    print("[rollback] 事务已回滚，2001/2002 在源端不存在，本地库同样不会出现")


if __name__ == "__main__":
    main()
