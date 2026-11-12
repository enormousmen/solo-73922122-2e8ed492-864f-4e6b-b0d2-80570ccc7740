# 环保实验室 LIMS 数据服务

本服务包含两部分：

1. **水样核对 API**（原有功能，完整保留）：把仪器上录错的样品编号核对回送检水样。
2. **LIMS 检测结果增量同步**（新增）：从本地 PostgreSQL 16 的逻辑复制槽读取
   pgoutput 协议 v1，把检测结果的插入、更正、删除实时同步到 SQLite 本地分析库，
   减少业务库查询负担。

---

## 一、增量同步模块

### 工作模型

```text
PostgreSQL 16 源库                同步进程（单实例）              SQLite 本地分析库
lims_test_result                ┌───────────────────┐          lims_test_result（镜像）
  │  WAL（逻辑复制）             │ 1. 持久复制槽      │          sync_state（同步进度）
  └── pgoutput v1 ───────────▶  │ 2. 累积一个事务    │  原子    ┌──────────────────┐
   发布 lims_pub / 槽 lims_      │ 3. Commit 后整笔   │ ──────▶ │ 行数据 + 提交 LSN │
   lite_slot（先建槽后写）       │    写入 SQLite     │  事务   │ 同一事务落库      │
                                │ 4. 成功后反馈 LSN  │ ◀──────  └──────────────────┘
                                └──────────────────┘
```

### 关键正确性保证

| 需求 | 实现方式 |
| --- | --- |
| 先建持久复制槽再允许写入 | 同步进程启动先 `pg_create_logical_replication_slot`；演示脚本带门闩，槽不存在时拒绝写入 |
| 仅处理一张固定结构表 | pgoutput `Relation` 消息强校验列名/类型 OID，结构变更或非受管表直接报错，不重放 DDL |
| 源事务提交后才整笔发布 | 收到 Begin 后在内存累积，仅 Commit 时写入一个 SQLite 事务；回滚事务没有 Commit，天然不生效 |
| 本地结果与提交 LSN 同时保存 | 镜像行带 `source_lsn`/`lsn_seq`/`committed_at`，与进度行在同一 SQLite 事务提交 |
| 落库成功后才确认复制进度 | 先提交 SQLite 事务，再向源端 `send_feedback(flush_lsn=...)`；反馈失败则下次重放 |
| 重放不漏删除、旧值不覆盖新值 | 每行带 `(source_lsn, 事务内序号)` 版本，UPSERT/DELETE 带版本守卫，重放幂等 |
| 断线自动重连 | 复制连接带 TCP keepalive；任何连接/协议错误按指数退避（1→30s）重连 |
| 中断后从持久进度续接 | 重启从 SQLite `confirmed_lsn` 续接；复制槽只复用、绝不删除重建，不跳过积压 |
| 单同步进程 | 会话级咨询锁 `pg_try_advisory_lock` 防双消费，第二个进程快速失败退出 |
| 每事务最多 100 行 | 累积到第 101 行即拒绝重放并报错 |

### 数据结构

源表与镜像表（整数主键 + 三个不超过 100 字节的非空文本列）：

```sql
-- PostgreSQL 源表
CREATE TABLE lims_test_result (
    result_id      INTEGER PRIMARY KEY,
    sample_code    TEXT NOT NULL CHECK (octet_length(sample_code)    BETWEEN 1 AND 100),
    test_item      TEXT NOT NULL CHECK (octet_length(test_item)      BETWEEN 1 AND 100),
    measured_value TEXT NOT NULL CHECK (octet_length(measured_value) BETWEEN 1 AND 100)
);
```

```sql
-- SQLite 镜像表（额外记录同步来源信息）
lims_test_result(result_id PK, sample_code, test_item, measured_value,
                 source_lsn, lsn_seq, committed_at)
sync_state(confirmed_lsn, last_commit_lsn, applied_tx_count, applied_row_count, ...)
```

### 同步查询 API

| 方法与路径 | 说明 |
| --- | --- |
| `GET /api/v1/sync/status` | 复制槽、确认 LSN、累计事务/行数、镜像行数 |
| `GET /api/v1/sync/results/{sample_code}` | 按样品码查询完整已提交结果及同步状态 |

仅返回源端**已提交且已同步落库**的行；已删除与回滚的行不会出现。
原有 `/api/v1/check` 核对接口保持不变。

---

## 二、快速启动（Docker Compose）

需要 Docker 与 Docker Compose 插件：

```bash
# 启动持久化演示源 PostgreSQL 16、同步进程、查询 API
docker compose up --build
```

启动后：

| 地址 | 用途 |
| --- | --- |
| <http://localhost:8000/docs> | Swagger UI，含同步查询与核对全部入口 |
| <http://localhost:8000/redoc> | ReDoc 文档 |
| <http://localhost:8000/health> | 健康检查 |
| <http://localhost:8000/api/v1/sync/status> | 同步状态 |
| <http://localhost:8000/api/v1/sync/results/S-DEMO-01> | 按样品码查询同步结果 |
| <http://localhost:8000/api/v1/check> | 原有水样核对接口 |

PostgreSQL 数据（命名卷 `pgdata`）与 SQLite（命名卷 `sqlite_data`）均持久化，
容器重建后数据、复制槽和同步进度都保留。端口可用 `API_PORT=9000 docker compose up --build` 调整。

### 运行内置演示

演示覆盖**同事务多行更正、回滚不生效、删除不遗漏、断连续接**四个场景，
并在最后通过 HTTP API 自动验证全部结论：

```bash
# 另开一个终端（基础服务保持运行）
docker compose --profile demo run --rm demo
```

演示分五个阶段（`demo/sync/`）：

1. `01_seed_and_correct.py`：同一源事务插入 3 行并更正其中 2 行，本地只出现最终值。
2. `02_rollback.py`：插入后显式回滚，本地库不出现这些行。
3. `03_delete.py`：插入后在另一事务删除，验证删除可靠同步。
4. `04_reconnect.py`：`pg_terminate_backend` 终止 walsender 模拟断线，
   断线期间持续写入，验证自动重连后积压逐条补齐、不丢不重。
5. `05_verify.py`：仅通过 HTTP API 校验以上全部结论并打印同步状态。

所有写入步骤都会先等待复制槽创建（`wait_for_slot` 门闩），保证“先建槽、后写入”。

---

## 三、水样核对 API（原有功能）

把仪器检测记录按样品编号核对到送检水样，两个清单各最多 12 行。除了给出一个
最优匹配，还会分析**所有**最优方案：只有一条仪器记录在全部最优方案中都对应同一
条送检记录时，才返回可自动归档的确定映射；否则返回候选水样并提示人工核对。

```bash
curl -s -X POST http://localhost:8000/api/v1/check \
  -H 'Content-Type: application/json' \
  --data @demo/check_request.json
```

- 候选边：样品编号忽略大小写后 Levenshtein 距离 ≤ 2。
- 一对一匹配，先最大化配对数，再最小化总编辑距离。
- 候选与确定映射都精确到送检行 `submission_id`，重复样品编号不会混用采样点。

内置演示数据：`GET /api/v1/demo` 与 `GET /api/v1/demo/expected`。

---

## 四、本地开发与测试

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

测试（77 个）包含：

- pgoutput v1 二进制协议解析（用真实线格式的手工构造消息回归）；
- SQLite 重放语义：原子提交、同事务更正/删除、版本守卫、旧值不覆盖新值、
  已删除行不被旧插入复活、重复事务幂等；
- 同步查询 API 与原核对接口的 HTTP 回归。

## 目录

```text
app/main.py              FastAPI 路由（原核对 + 新增同步查询）
app/matcher.py           Levenshtein、最小费用流、最优方案稳定性分析
app/sync/config.py       环境变量配置与常量
app/sync/pgoutput.py     pgoutput v1 二进制协议解析
app/sync/slot.py         持久复制槽/发布/咨询锁/复制连接管理
app/sync/store.py        SQLite 镜像、同步进度、版本守卫重放
app/sync/worker.py       同步主循环：重连、整笔发布、落库后反馈
app/sync/api.py          同步状态与按样品码查询接口
demo/sync/               更正/回滚/删除/断连续接演示脚本
postgres/01_schema.sql   源表 DDL（容器初始化执行）
tests/                   全部单元与 API 测试
Dockerfile               api / syncer / demo 三个目标镜像
docker-compose.yml       一键启动编排
```
