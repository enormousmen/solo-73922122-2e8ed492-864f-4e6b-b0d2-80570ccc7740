"""同步结果查询 API。

在保留原有水样核对接口的前提下，新增：

- ``GET /api/v1/sync/status``：复制槽、确认 LSN、事务/行数等同步状态。
- ``GET /api/v1/sync/results/{sample_code}``：按样品码查询完整已提交结果。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException

from ..sync.config import SyncConfig, format_lsn
from ..sync.store import STATE_NOT_STARTED, ResultStore, database_exists

router = APIRouter(prefix="/api/v1/sync", tags=["LIMS 同步"])


def _config() -> SyncConfig:
    return SyncConfig.from_env()


def _open_store() -> ResultStore:
    config = _config()
    if not database_exists(config.sqlite_path):
        raise HTTPException(
            status_code=503,
            detail=(
                "同步尚未初始化：本地 SQLite 库不存在，请先启动同步进程 "
                "并创建持久复制槽"
            ),
        )
    return ResultStore(config.sqlite_path, config.slot_name)


@router.get(
    "/status",
    summary="同步状态：复制槽、确认 LSN 与累计事务/行数",
)
def sync_status() -> dict[str, Any]:
    """返回当前持久化的同步进度。同步进程未运行时仍可读取最后状态。"""

    config = _config()
    if not database_exists(config.sqlite_path):
        return {
            "state": STATE_NOT_STARTED,
            "slot_name": config.slot_name,
            "confirmed_lsn": None,
            "confirmed_lsn_text": None,
            "applied_transactions": 0,
            "applied_rows": 0,
            "last_committed_at": None,
            "updated_at": None,
            "mirrored_rows": 0,
        }

    store = _open_store()
    try:
        state = store.get_state()
        confirmed = int(state["confirmed_lsn"])
        return {
            "state": "streaming",
            "slot_name": state["slot_name"],
            "confirmed_lsn": confirmed,
            "confirmed_lsn_text": format_lsn(confirmed) if confirmed else None,
            "last_commit_lsn": int(state["last_commit_lsn"]),
            "last_commit_lsn_text": (
                format_lsn(int(state["last_commit_lsn"]))
                if state["last_commit_lsn"]
                else None
            ),
            "applied_transactions": int(state["applied_tx_count"]),
            "applied_rows": int(state["applied_row_count"]),
            "last_committed_at": state["last_committed_at"],
            "updated_at": state["updated_at"],
            "mirrored_rows": store.count_results(),
        }
    finally:
        store.close()


@router.get(
    "/results/{sample_code}",
    summary="按样品码查询完整已提交结果及同步状态",
)
def results_by_sample(sample_code: str) -> dict[str, Any]:
    """返回该样品码下全部已提交（含更正后最新值）的检测结果。

    仅包含源端已提交并已同步落库的行；已删除的行不会出现，
    回滚事务的行也从未进入本地库。
    """

    if not sample_code:
        raise HTTPException(status_code=422, detail="样品码不能为空")
    if len(sample_code.encode("utf-8")) > 100:
        raise HTTPException(status_code=422, detail="样品码超过 100 字节上限")

    store = _open_store()
    try:
        rows = store.find_by_sample_code(sample_code)
        state = store.get_state()
        results = [_serialize_row(row) for row in rows]
        return {
            "sample_code": sample_code,
            "count": len(results),
            "results": results,
            "sync": {
                "slot_name": state["slot_name"],
                "confirmed_lsn": int(state["confirmed_lsn"]),
                "confirmed_lsn_text": (
                    format_lsn(int(state["confirmed_lsn"]))
                    if state["confirmed_lsn"]
                    else None
                ),
                "applied_transactions": int(state["applied_tx_count"]),
                "last_committed_at": state["last_committed_at"],
                "updated_at": state["updated_at"],
            },
        }
    finally:
        store.close()


def _serialize_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "result_id": row["result_id"],
        "sample_code": row["sample_code"],
        "test_item": row["test_item"],
        "measured_value": row["measured_value"],
        "source_lsn": row["source_lsn"],
        "source_lsn_text": format_lsn(int(row["source_lsn"])),
        "committed_at": row["committed_at"],
    }
