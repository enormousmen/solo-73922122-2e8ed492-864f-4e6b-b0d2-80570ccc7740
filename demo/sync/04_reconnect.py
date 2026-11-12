"""阶段 4：断连续接演示。

步骤：
1. 向源端写入积压数据（此时同步进程可能正被断开）。
2. 通过 ``pg_terminate_backend`` 终止同步进程的 walsender 连接，
   模拟网络断开/对端重启。
3. 同步进程自动重连，从 SQLite 中持久化的 confirmed_lsn 续接，
   不重建复制槽、不跳过积压。
4. 再写入一条新数据，验证重连后链路恢复正常。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import SLOT_NAME, connect, current_rows, wait_for_slot


def main() -> None:
    wait_for_slot()

    # 1) 断开前先写一批“积压”数据。
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO lims_test_result (result_id, sample_code, test_item, measured_value) "
            "VALUES (4001, 'S-RECONNECT', 'pH', '7.00'),"
            "       (4002, 'S-RECONNECT', 'COD', '21.0'),"
            "       (4003, 'S-RECONNECT', '氨氮', '0.80')"
        )
        conn.commit()
    print("[reconnect] 已写入断连前积压 4001-4003")

    # 2) 找到消费该槽的 walsender 并终止，模拟断线。
    with connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT active_pid FROM pg_replication_slots WHERE slot_name = %s",
            (SLOT_NAME,),
        )
        row = cur.fetchone()
        pid = row[0] if row else None
        if pid is None:
            print("[reconnect] 当前槽无活跃 walsender（可能正在重连），跳过终止步骤")
        else:
            cur.execute("SELECT pg_terminate_backend(%s)", (pid,))
            print(f"[reconnect] 已终止 walsender pid={pid}，同步进程应自动重连")

    # 3) 断线期间继续写入，制造重连后必须补齐的积压。
    time.sleep(2)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO lims_test_result (result_id, sample_code, test_item, measured_value) "
            "VALUES (4004, 'S-RECONNECT', '总磷', '0.12')"
        )
        conn.commit()
    print("[reconnect] 断线期间写入 4004，重连后必须补齐")

    # 4) 重连恢复后再写一条。
    time.sleep(8)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO lims_test_result (result_id, sample_code, test_item, measured_value) "
            "VALUES (4005, 'S-RECONNECT', '总氮', '1.50')"
        )
        conn.commit()
    print("[reconnect] 重连恢复后写入 4005")

    source = current_rows()
    for rid in (4001, 4002, 4003, 4004, 4005):
        assert rid in source, f"源端缺少 {rid}"
    print("[reconnect] 源端 4001-4005 均在；请通过 API 验证本地库逐条补齐")


if __name__ == "__main__":
    main()
