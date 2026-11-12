"""FastAPI 服务：把仪器检测记录核对到送检水样。"""

from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, model_validator

from .matcher import InstrumentRow, SubmissionRow, analyze_matches
from .sync.api import router as sync_router

API_DESCRIPTION = """
将仪器检测记录按样品编号核对到送检水样。

- 样品编号只允许英文字母和数字，比对时忽略大小写。
- 仅当两个编号的 Levenshtein 编辑距离不超过 2 时才建立候选边。
- 每一条送检记录、每一条仪器记录最多配对一次。
- 先最大化配对数量，再最小化所有配对的距离总和。
- 只有当某条仪器记录在**所有**最优方案中都对应同一条送检记录时，才给出确定归档映射。
- 其余记录会返回它在所有最优方案中出现过的全部候选水样，并用
  `possible_unmatched` 标明该记录是否可能没有匹配。
- 送检清单的样品编号允许重复；结果始终精确到送检行（`submission_id`），
  以免重复编号的不同采样点被混用。
"""

app = FastAPI(
    title="环保实验室 LIMS 数据服务",
    version="1.1.0",
    description=API_DESCRIPTION,
    contact={"name": "环保实验室"},
    docs_url="/docs",
    redoc_url="/redoc",
)

# LIMS 检测结果逻辑复制同步查询路由（原核对接口保持不变）。
app.include_router(sync_router)


class Submission(BaseModel):
    """一条送检清单记录。"""

    id: str = Field(
        min_length=1,
        pattern=r"^[A-Za-z0-9]+$",
        description="送检行独立 ID，仅限英文字母和数字，忽略大小写后不可重复",
        examples=["S01"],
    )
    sample_no: str = Field(
        min_length=1,
        pattern=r"^[A-Za-z0-9]+$",
        description="样品编号，仅限英文字母和数字；允许在不同送检行中重复",
        examples=["WQ1001"],
    )
    sampling_point: str = Field(
        min_length=1,
        description="采样点名称",
        examples=["东岸取水口"],
    )


class InstrumentRecord(BaseModel):
    """一条仪器检测记录。"""

    id: str = Field(
        min_length=1,
        pattern=r"^[A-Za-z0-9]+$",
        description="仪器记录独立 ID，仅限英文字母和数字，忽略大小写后不可重复",
        examples=["I01"],
    )
    sample_no: str = Field(
        min_length=1,
        pattern=r"^[A-Za-z0-9]+$",
        description="仪器上录入的样品编号，仅限英文字母和数字",
        examples=["WQ1002"],
    )
    value: Annotated[
        float,
        Field(allow_inf_nan=False, description="仪器检测值", examples=[7.42]),
    ]


class CheckRequest(BaseModel):
    """核对请求。两个清单分别最多 12 行。"""

    submissions: list[Submission] = Field(
        max_length=12,
        description="送检清单，最多 12 行",
    )
    instruments: list[InstrumentRecord] = Field(
        max_length=12,
        description="仪器记录，最多 12 行",
    )

    @model_validator(mode="after")
    def _ids_must_be_unique_case_insensitively(self) -> "CheckRequest":
        seen: dict[str, str] = {}
        for label, rows in (
            ("送检清单", self.submissions),
            ("仪器记录", self.instruments),
        ):
            for row in rows:
                key = row.id.casefold()
                if key in seen:
                    raise ValueError(
                        f"行 ID 重复（忽略大小写）：{seen[key]} 与 {row.id}"
                    )
                seen[key] = f"{label}/{row.id}"
        return self

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "submissions": [
                        {
                            "id": "S01",
                            "sample_no": "WQ1001",
                            "sampling_point": "东岸取水口",
                        },
                        {
                            "id": "S02",
                            "sample_no": "WQ1003",
                            "sampling_point": "西岸取水口",
                        },
                        {
                            "id": "S03",
                            "sample_no": "WQ5555",
                            "sampling_point": "南岸取水口",
                        },
                        {
                            "id": "S04",
                            "sample_no": "WQ1003",
                            "sampling_point": "西岸复采点",
                        },
                    ],
                    "instruments": [
                        {
                            "id": "I01",
                            "sample_no": "WQ1002",
                            "value": 7.42,
                        },
                        {
                            "id": "I02",
                            "sample_no": "WQ1004",
                            "value": 7.18,
                        },
                        {
                            "id": "I03",
                            "sample_no": "WQ1000",
                            "value": 7.51,
                        },
                        {
                            "id": "I04",
                            "sample_no": "WQ5555",
                            "value": 8.03,
                        },
                        {
                            "id": "I05",
                            "sample_no": "ZZZ999",
                            "value": 6.97,
                        },
                        {
                            "id": "I06",
                            "sample_no": "WQ1006",
                            "value": 7.66,
                        },
                    ],
                }
            ]
        }
    }


class MappingCandidate(BaseModel):
    """一个可能的最优归档去向。"""

    submission_id: str = Field(description="送检行 ID")
    sample_no: str = Field(description="送检样品编号")
    sampling_point: str = Field(description="采样点")
    distance: int = Field(description="仪器编号与样品编号之间的 Levenshtein 距离")


class InstrumentResolution(BaseModel):
    """一条仪器记录的核对结论。"""

    instrument_id: str = Field(description="仪器记录 ID")
    recorded_sample_no: str = Field(description="仪器记录的原始样品编号")
    value: float = Field(description="仪器检测值")
    status: Literal["confirmed", "review"] = Field(
        description=(
            "confirmed：所有最优方案中均对应同一条送检记录，可自动归档；"
            "review：存在歧义，需人工核对"
        ),
        examples=["confirmed", "review"],
    )
    confirmed_mapping: MappingCandidate | None = Field(
        default=None,
        description="确定归档映射；仅 status=confirmed 时非空",
    )
    candidate_mappings: list[MappingCandidate] = Field(
        description=(
            "该仪器记录在所有最优方案中出现过的候选送检行；"
            "status=confirmed 时仅有一个元素"
        )
    )
    possible_unmatched: bool = Field(
        description=(
            "是否存在某个最优方案使该仪器记录没有匹配。" "为 true 时，不能直接归档"
        )
    )


class CheckSummary(BaseModel):
    submission_count: int = Field(description="送检清单行数")
    instrument_count: int = Field(description="仪器记录行数")
    max_matched_pairs: int = Field(description="最优方案中的配对数")
    min_total_distance: int = Field(description="最优方案的总编辑距离")
    confirmed_count: int = Field(description="可直接归档的仪器记录数")
    review_count: int = Field(description="需人工核对的仪器记录数")
    possible_unmatched_count: int = Field(description="可能未匹配的仪器记录数")


class CheckResponse(BaseModel):
    summary: CheckSummary
    results: list[InstrumentResolution]


DEMO_PATH = Path(__file__).resolve().parent.parent / "demo" / "check_request.json"


@lru_cache(maxsize=1)
def _demo_request() -> CheckRequest:
    try:
        payload = json.loads(DEMO_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover
        raise RuntimeError(f"无法读取演示数据 {DEMO_PATH}: {exc}") from exc
    return CheckRequest.model_validate(payload)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """把浏览器访问根路径的请求转到交互式文档。"""

    return RedirectResponse(url="/docs")


@app.get("/health", tags=["运维"], summary="健康检查")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get(
    "/api/v1/demo",
    response_model=CheckRequest,
    tags=["演示数据"],
    summary="获取可直接提交的演示请求",
)
def demo_request() -> CheckRequest:
    """返回演示请求体；把响应完整复制到 POST /api/v1/check 即可。"""

    return _demo_request()


@app.post(
    "/api/v1/check",
    response_model=CheckResponse,
    tags=["核对"],
    summary="核对仪器记录与送检水样",
    response_description="每条仪器记录的确定映射或候选水样",
)
def check(payload: CheckRequest) -> CheckResponse:
    submissions = [
        SubmissionRow(
            row_id=row.id,
            sample_no=row.sample_no,
            sampling_point=row.sampling_point,
        )
        for row in payload.submissions
    ]
    instruments = [
        InstrumentRow(
            row_id=row.id,
            sample_no=row.sample_no,
            value=row.value,
        )
        for row in payload.instruments
    ]

    analysis = analyze_matches(submissions, instruments)

    results: list[InstrumentResolution] = []
    for resolution in analysis.resolutions:
        candidates = [
            MappingCandidate(
                submission_id=candidate.submission.row_id,
                sample_no=candidate.submission.sample_no,
                sampling_point=candidate.submission.sampling_point,
                distance=candidate.distance,
            )
            for candidate in resolution.candidates
        ]
        confirmed_mapping = (
            candidates[0] if resolution.is_confirmed and len(candidates) == 1 else None
        )
        results.append(
            InstrumentResolution(
                instrument_id=resolution.instrument.row_id,
                recorded_sample_no=resolution.instrument.sample_no,
                value=resolution.instrument.value,
                status="confirmed" if confirmed_mapping else "review",
                confirmed_mapping=confirmed_mapping,
                candidate_mappings=candidates,
                possible_unmatched=resolution.possible_unmatched,
            )
        )

    return CheckResponse(
        summary=CheckSummary(
            submission_count=len(submissions),
            instrument_count=len(instruments),
            max_matched_pairs=analysis.max_pairs,
            min_total_distance=analysis.min_total_distance,
            confirmed_count=sum(1 for row in results if row.status == "confirmed"),
            review_count=sum(1 for row in results if row.status == "review"),
            possible_unmatched_count=sum(
                1 for row in results if row.possible_unmatched
            ),
        ),
        results=results,
    )


@app.get("/api/v1/demo/expected", tags=["演示数据"], summary="演示数据的核对结果")
def demo_expected() -> CheckResponse:
    """直接运行内置演示数据并返回结果，方便验证服务是否正常。"""

    return check(_demo_request())
