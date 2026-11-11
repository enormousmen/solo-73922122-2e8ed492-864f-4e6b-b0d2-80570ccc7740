from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app, demo_request

client = TestClient(app)


def test_demo_and_docs_are_available():
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/docs").status_code == 200
    assert client.get("/api/v1/demo").status_code == 200


def test_demo_request_returns_expected_classification():
    response = client.post("/api/v1/check", json=demo_request().model_dump())
    assert response.status_code == 200

    body = response.json()
    assert body["summary"] == {
        "submission_count": 4,
        "instrument_count": 6,
        "max_matched_pairs": 4,
        "min_total_distance": 3,
        "confirmed_count": 1,
        "review_count": 5,
        "possible_unmatched_count": 5,
    }

    by_id = {row["instrument_id"]: row for row in body["results"]}
    assert by_id["I04"]["status"] == "confirmed"
    assert by_id["I04"]["confirmed_mapping"]["sampling_point"] == "南岸取水口"
    assert by_id["I01"]["status"] == "review"
    assert by_id["I01"]["possible_unmatched"] is True
    assert len(by_id["I01"]["candidate_mappings"]) == 3
    assert by_id["I05"]["candidate_mappings"] == []
    assert by_id["I05"]["possible_unmatched"] is True
    assert by_id["I06"]["possible_unmatched"] is True


def test_bundled_expected_response_matches_live_demo():
    demo_path = (
        Path(__file__).resolve().parent.parent / "demo" / "expected_response.json"
    )
    bundled = json.loads(demo_path.read_text(encoding="utf-8"))
    response = client.get("/api/v1/demo/expected")
    assert response.status_code == 200
    assert response.json() == bundled


def test_duplicate_sample_numbers_are_accepted():
    payload = {
        "submissions": [
            {"id": "S01", "sample_no": "WQ1001", "sampling_point": "东岸"},
            {"id": "S02", "sample_no": "WQ1001", "sampling_point": "西岸"},
        ],
        "instruments": [{"id": "I01", "sample_no": "WQ1001", "value": 1.0}],
    }
    response = client.post("/api/v1/check", json=payload)
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["status"] == "review"
    assert len(result["candidate_mappings"]) == 2


def test_more_than_twelve_rows_is_rejected():
    payload = {
        "submissions": [
            {"id": f"S{i}", "sample_no": f"WQ{i}", "sampling_point": f"P{i}"}
            for i in range(13)
        ],
        "instruments": [],
    }
    response = client.post("/api/v1/check", json=payload)
    assert response.status_code == 422


def test_ids_must_be_alphanumeric_and_unique_case_insensitively():
    payload = {
        "submissions": [
            {"id": "S-01", "sample_no": "WQ1001", "sampling_point": "东岸"},
        ],
        "instruments": [],
    }
    assert client.post("/api/v1/check", json=payload).status_code == 422

    duplicate_payload = {
        "submissions": [
            {"id": "S01", "sample_no": "WQ1001", "sampling_point": "东岸"},
        ],
        "instruments": [
            {"id": "s01", "sample_no": "WQ1002", "value": 1.0},
        ],
    }
    response = client.post("/api/v1/check", json=duplicate_payload)
    assert response.status_code == 422
    assert "忽略大小写" in str(response.json()["detail"])


def test_empty_lists_are_supported():
    response = client.post("/api/v1/check", json={"submissions": [], "instruments": []})
    assert response.status_code == 200
    assert response.json()["summary"]["max_matched_pairs"] == 0
