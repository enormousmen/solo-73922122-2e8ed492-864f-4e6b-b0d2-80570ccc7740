"""阶段 3：删除演示——删除操作必须可靠同步，不能漏掉。"""

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
            "VALUES (3001, 'S-DELETE', 'pH', '6.50')"
        )
        conn.commit()

    print("[delete] 已插入 3001，随后在独立事务中删除")
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM lims_test_result WHERE result_id = 3001")
        conn.commit()

    rows = current_rows()
    assert 3001 not in rows, "删除后源表不应再有 3001"
    print("[delete] 3001 已在源端删除，等待同步后本地库同样消失（删除不遗漏）")


if __name__ == "__main__":
    main()
