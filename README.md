# 环保实验室水样核对 API

把仪器上录错的样品编号核对回送检水样，避免检测值归到错误的采样点。

服务使用 FastAPI，两个清单各最多 12 行。除了给出一个最优匹配，它还会分析
**所有**最优方案：只有一条仪器记录在全部最优方案中都对应同一条送检记录时，
才返回可自动归档的确定映射；否则返回候选水样并提示是否需要人工核对。

## 快速启动

需要 Docker 与 Docker Compose 插件：

```bash
docker compose up --build
```

启动后：

| 地址 | 用途 |
| --- | --- |
| <http://localhost:8000/docs> | Swagger UI 交互式 API 文档 |
| <http://localhost:8000/redoc> | ReDoc 文档 |
| <http://localhost:8000/health> | 健康检查 |
| <http://localhost:8000/api/v1/demo> | 可直接提交的演示请求 |
| <http://localhost:8000/api/v1/demo/expected> | 内置演示数据的核对结果 |

端口可通过环境变量调整，例如 `API_PORT=9000 docker compose up --build`。

运行演示：

```bash
curl -s -X POST http://localhost:8000/api/v1/check \
  -H 'Content-Type: application/json' \
  --data @demo/check_request.json
```

也可以在 Swagger UI 中先调用 `GET /api/v1/demo`，再把响应体粘贴到
`POST /api/v1/check` 的请求体中。

## 请求格式

```json
{
  "submissions": [
    {"id": "S01", "sample_no": "WQ1001", "sampling_point": "东岸取水口"},
    {"id": "S02", "sample_no": "WQ1003", "sampling_point": "西岸取水口"},
    {"id": "S03", "sample_no": "WQ5555", "sampling_point": "南岸取水口"},
    {"id": "S04", "sample_no": "WQ1003", "sampling_point": "西岸复采点"}
  ],
  "instruments": [
    {"id": "I01", "sample_no": "WQ1002", "value": 7.42},
    {"id": "I02", "sample_no": "WQ1004", "value": 7.18},
    {"id": "I03", "sample_no": "WQ1000", "value": 7.51},
    {"id": "I04", "sample_no": "WQ5555", "value": 8.03},
    {"id": "I05", "sample_no": "ZZZ999", "value": 6.97},
    {"id": "I06", "sample_no": "WQ1006", "value": 7.66}
  ]
}
```

- 两个清单均不能超过 12 行，可以为空。
- `id`、`sample_no` 只允许英文字母和数字。
- `id` 是行标识，在两类记录中忽略大小写后都必须唯一。
- `sample_no` 允许重复，因为同一水样可能在多个采样点或容器中送检。
- `sampling_point` 不能为空，接受中文等任意文本。
- `value` 必须是有限数字（不允许 NaN/Infinity）。

请求中任何不符合上述约束的内容都会由 FastAPI 返回 `422`。

## 响应格式

```json
{
  "summary": {
    "submission_count": 4,
    "instrument_count": 6,
    "max_matched_pairs": 4,
    "min_total_distance": 3,
    "confirmed_count": 1,
    "review_count": 5,
    "possible_unmatched_count": 5
  },
  "results": [
    {
      "instrument_id": "I01",
      "recorded_sample_no": "WQ1002",
      "value": 7.42,
      "status": "review",
      "confirmed_mapping": null,
      "candidate_mappings": [
        {
          "submission_id": "S01",
          "sample_no": "WQ1001",
          "sampling_point": "东岸取水口",
          "distance": 1
        },
        {
          "submission_id": "S02",
          "sample_no": "WQ1003",
          "sampling_point": "西岸取水口",
          "distance": 1
        },
        {
          "submission_id": "S04",
          "sample_no": "WQ1003",
          "sampling_point": "西岸复采点",
          "distance": 1
        }
      ],
      "possible_unmatched": true
    },
    {
      "instrument_id": "I04",
      "recorded_sample_no": "WQ5555",
      "value": 8.03,
      "status": "confirmed",
      "confirmed_mapping": {
        "submission_id": "S03",
        "sample_no": "WQ5555",
        "sampling_point": "南岸取水口",
        "distance": 0
      },
      "candidate_mappings": [
        {
          "submission_id": "S03",
          "sample_no": "WQ5555",
          "sampling_point": "南岸取水口",
          "distance": 0
        }
      ],
      "possible_unmatched": false
    }
  ]
}
```

每条仪器记录的结论：

- `status=confirmed`：在全部最优方案中，该记录始终对应同一条送检记录。
  `confirmed_mapping` 可直接用于归档。
- `status=review`：存在多个候选送检记录，或存在“未匹配”的最优方案。
  `confirmed_mapping` 为 `null`，需人工核对。
- `candidate_mappings`：该记录在**所有最优方案中出现过**的全部候选送检行。
  即使只有一个候选，只要 `possible_unmatched=true`，也不能自动归档。
- `possible_unmatched`：是否有某个最优方案使该记录没有匹配。

候选与确定映射都以 `submission_id` 精确到送检行，而不是只到样品编号。
因此，同一个 `sample_no` 出现在不同采样点时，不会误把检测值归到另一个
采样点。

## 核对规则

1. **候选边**：样品编号忽略大小写后计算 Levenshtein 距离，距离不超过 2
   才允许配对。编号本身只含英文字母和数字。
2. **一对一**：每条送检记录和每条仪器记录最多使用一次。
3. **两级最优**：先最大化配对数，再在配对数相同的方案中最小化总距离。
   即使存在“只配一对、总距离更小”的方案，也会优先选择配对数更多的方案。
4. **稳定性判断**：对每条候选边，强制使用该边后重新求解；若仍能得到同一个
   全局最优（配对数与总距离都不变），该边就出现在某个最优方案中。
   对每条仪器记录，去掉它后若仍能达到同一个全局最优，则存在它未匹配的
   最优方案。

内部使用最小费用最大流求两级最优解；最大规模仅 12×12，因此可以逐条边做
强制约束验证，而不必枚举可能数量巨大的所有最优匹配。单次请求最多约 157 次
小规模流计算。

## 内置演示数据说明了什么

`demo/check_request.json` 与 `demo/expected_response.json` 中包含：

- `I04` 的 `WQ5555` 精确命中 `S03`，是唯一确定映射。
- `I01`、`I02`、`I03`、`I06` 都可对应 `S01`、`S02`、`S04`；这些行中
  `S02`、`S04` 的样品编号相同但采样点不同。
- 上述四条记录都可能在某些最优方案中未匹配，因此全部为 `review`。
- `I05` 的 `ZZZ999` 与任何编号距离都超过 2，没有候选，但仍可能未匹配。

演示数据的全局最优为 4 对、总距离 3。

## 本地开发与测试

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

测试：

```bash
pytest -q
```

测试包含随机小规模输入与独立穷举程序的对照，验证最优配对数、最小总距离、
候选集合和“可能未匹配”判定。

## 目录

```text
app/main.py        FastAPI 路由、校验模型、响应组装
app/matcher.py     Levenshtein、最小费用流、最优方案稳定性分析
demo/              可直接提交的演示请求与预期响应
tests/             API 与算法测试
Dockerfile         API 镜像
docker-compose.yml 一键启动配置
```
