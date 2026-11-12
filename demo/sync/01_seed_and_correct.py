"""阶段 1：初始插入 + 同事务多行更正（演示整笔发布与更正同步）。

在**同一个源事务**里：
- 插入 3 条初始结果；
- 对其中 2 条执行 UPDATE 更正检测值（主键不变）；
提交后本地应只看到更正后的最终值，看不到中间旧值。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import connect, current_rows, wait_for_slot


def main() -> None:
    wait_for_slot()

    with connect() as conn, conn.cursor() as cur:
        # 先清理可能残留的演示数据，保证脚本可重复执行。
        cur.execute("DELETE FROM lims_test_result WHERE result_id BETWEEN 1001 AND 1003")

        cur.execute(
            "INSERT INTO lims_test_result (result_id, sample_code, test_item, measured_value) "
            "VALUES (1001, 'S-DEMO-01', 'pH', '7.10'),"
            "       (1002, 'S-DEMO-01', 'COD', '42.0'),"
            "       (1003, 'S-DEMO-02', '浊度', '3.5')"
        )
        # 同一事务内更正两条（主键保持不变）。
        cur.execute(
            "UPDATE lims_test_result SET measured_value = '7.42' "
            "WHERE result_id = 1001"
        )
        cur.execute(
            "UPDATE lims_test_result SET measured_value = '38.6' "
            "WHERE result_id = 1002"
        )
        # 事务尚未提交：本地库此刻不应看到任何数据。
        conn.commit()

    print("[seed] 已提交：1001/1002 为同事务更正后的最终值，1003 为新插入")
    for result_id, row in sorted(current_rows().items()):
        print(f"  {result_id}: {row}")


if __name__ == "__main__":
    main()
