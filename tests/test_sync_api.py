"""同步查询 API 测试：复用 FastAPI TestClient，原核对接口同时回归。"""

from __future__ import annotations

import importlib
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "lims-test.db"
    monkeypatch.setenv("LIMS_SQLITE_PATH", str(db_path))
    monkeypatch.setenv("LIMS_SLOT_NAME", "test_slot")

    # 配置在导入时读取环境变量，确保模块以测试路径重新初始化。
    from app.sync import api as sync_api
    from app.sync import config as sync_config
    importlib.reload(sync_config)
    importlib.reload(sync_api)

    from app.main import app
    app.router.lifespan_context  # 仅确认应用对象可用
    with TestClient(app) as test_client:
        yield test_client, str(db_path)


def _seed_database(db_path: str) -> None:
    from app.sync.store import ResultStore
    from app.sync.pgoutput import RowChange, TransactionBatch

    store = ResultStore(db_path, "test_slot")
    batch = TransactionBatch(commit_lsn=5000, commit_ts_us=1_700_000_000_000_000, end_lsn=5100)
    batch.changes.extend(
        [
            RowChange("insert", 1, 1, ("S-01", "pH", "7.42"), 5000),
            RowChange("insert", 1, 2, ("S-01", "COD", "38.6"), 5000),
            RowChange("insert", 1, 3, ("S-02", "浊度", "3.5"), 5000),
        ]
    )
    store.apply_transaction(batch)
    store.close()


def test_status_reports_not_started_without_database(client):
    test_client, _ = client
    body = test_client.get("/api/v1/sync/status").json()
    assert body["state"] == "not_started"
    assert body["confirmed_lsn"] is None
    assert body["mirrored_rows"] == 0


def test_results_endpoint_returns_503_before_sync_starts(client):
    test_client, _ = client
    response = test_client.get("/api/v1/sync/results/S-01")
    assert response.status_code == 503


def test_results_query_returns_full_committed_rows_with_sync_status(client):
    test_client, db_path = client
    _seed_database(db_path)

    response = test_client.get("/api/v1/sync/results/S-01")
    assert response.status_code == 200
    body = response.json()
    assert body["sample_code"] == "S-01"
    assert body["count"] == 2
    assert [row["result_id"] for row in body["results"]] == [1, 2]
    assert body["results"][0]["measured_value"] == "7.42"
    assert body["results"][0]["source_lsn_text"].count("/") == 1
    assert body["sync"]["slot_name"] == "test_slot"
    assert body["sync"]["confirmed_lsn"] == 5100
    assert body["sync"]["applied_transactions"] == 1


def test_unknown_sample_code_returns_empty_result_list(client):
    test_client, db_path = client
    _seed_database(db_path)
    body = test_client.get("/api/v1/sync/results/NO-SUCH").json()
    assert body["count"] == 0
    assert body["results"] == []


def test_status_endpoint_reports_progress(client):
    test_client, db_path = client
    _seed_database(db_path)
    body = test_client.get("/api/v1/sync/status").json()
    assert body["state"] == "streaming"
    assert body["confirmed_lsn_text"] is not None
    assert body["applied_rows"] == 3
    assert body["mirrored_rows"] == 3


def test_sample_code_validation(client):
    test_client, _ = client
    long_code = "x" * 101
    response = test_client.get(f"/api/v1/sync/results/{long_code}")
    assert response.status_code == 422


def test_original_check_api_still_works(client):
    test_client, _ = client
    payload = {
        "submissions": [
            {"id": "S01", "sample_no": "WQ1001", "sampling_point": "东岸"},
        ],
        "instruments": [
            {"id": "I01", "sample_no": "WQ1001", "value": 7.42},
        ],
    }
    response = test_client.post("/api/v1/check", json=payload)
    assert response.status_code == 200
    assert response.json()["results"][0]["status"] == "confirmed"


def test_docs_and_health_remain_available(client):
    test_client, _ = client
    assert test_client.get("/docs").status_code == 200
    assert test_client.get("/health").json() == {"status": "ok"}
