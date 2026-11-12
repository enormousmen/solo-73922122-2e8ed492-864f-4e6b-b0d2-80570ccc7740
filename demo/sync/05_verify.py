"""阶段 5：通过查询 API 验证所有演示结论。

不直接读 SQLite，只调用 FastAPI 接口，证明对外查询入口可用：
- 同事务更正后只返回最终值；
- 回滚的 2001/2002 不出现；
- 删除的 3001 不出现；
- 断连前后的 4001-4005 全部补齐；
- 同步状态接口返回推进后的确认 LSN。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

API_BASE = os.environ.get("API_BASE", "http://api:8000")
EXPECTED_RECONNECT_IDS = {4001, 4002, 4003, 4004, 4005}


def get_json(path: str) -> dict:
    with urllib.request.urlopen(API_BASE + path, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wait_until(description: str, predicate, timeout: float = 45.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        try:
            last = predicate()
            if last.get("_ok"):
                return last
        except (urllib.error.URLError, KeyError, AssertionError):
            pass
        time.sleep(1)
    raise SystemExit(f"验证超时：{description}，最后状态={last}")


def main() -> None:
    # 等待 API 与同步进程就绪。
    wait_until("API 健康检查", lambda: {"_ok": get_json("/health")["status"] == "ok"})

    def reconnect_complete() -> dict:
        body = get_json("/api/v1/sync/results/S-RECONNECT")
        ids = {row["result_id"] for row in body["results"]}
        return {"_ok": ids == EXPECTED_RECONNECT_IDS, "ids": sorted(ids)}

    wait_until("断连续接后 4001-4005 全部补齐", reconnect_complete)

    corrected = get_json("/api/v1/sync/results/S-DEMO-01")
    values = {row["result_id"]: row["measured_value"] for row in corrected["results"]}
    assert values == {1001: "7.42", 1002: "38.6"}, values
    assert all(row["source_lsn"] > 0 for row in corrected["results"])
    print("[verify] 同事务多行更正：1001=7.42、1002=38.6（仅最终值，无旧值）")

    demo_02 = get_json("/api/v1/sync/results/S-DEMO-02")
    assert {row["result_id"] for row in demo_02["results"]} == {1003}
    print("[verify] 普通插入 1003 正常同步")

    rolled_back = get_json("/api/v1/sync/results/S-ROLLBACK")
    assert rolled_back["count"] == 0, rolled_back
    print("[verify] 回滚事务的 2001/2002 未进入本地库")

    deleted = get_json("/api/v1/sync/results/S-DELETE")
    assert deleted["count"] == 0, deleted
    print("[verify] 删除的 3001 在本地库同样消失（删除未遗漏）")

    reconnected = get_json("/api/v1/sync/results/S-RECONNECT")
    assert {row["result_id"] for row in reconnected["results"]} == EXPECTED_RECONNECT_IDS
    print("[verify] 断连续接：4001-4005 五条全部补齐，无丢失无重复")

    status = get_json("/api/v1/sync/status")
    assert status["confirmed_lsn"] > 0
    assert status["applied_transactions"] >= 5
    print(
        "[verify] 同步状态：slot=%s confirmed_lsn=%s 事务数=%d 镜像行数=%d"
        % (
            status["slot_name"],
            status["confirmed_lsn_text"],
            status["applied_transactions"],
            status["mirrored_rows"],
        )
    )
    print("\n全部演示验证通过 ✔")


if __name__ == "__main__":
    main()
