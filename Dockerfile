# syntax=docker/dockerfile:1

# 共享基础镜像：API、同步进程、演示脚本使用同一套依赖。
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY demo ./demo

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data
USER appuser

# API 镜像（含原有核对接口与新增同步查询接口）。
FROM base AS api
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# 同步进程镜像：消费逻辑复制槽并写入 SQLite。
FROM base AS syncer
CMD ["python", "-m", "app.sync.worker"]

# 演示镜像：按阶段执行写入与验证，结束后退出。
FROM base AS demo
USER root
RUN chmod +x /app/demo/sync/run_demo.sh
USER appuser
WORKDIR /app/demo/sync
ENV PYTHONPATH=/app/demo/sync
CMD ["./run_demo.sh"]
