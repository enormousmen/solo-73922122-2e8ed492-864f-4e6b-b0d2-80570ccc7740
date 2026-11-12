"""同步模块的环境变量配置与共享常量。

所有配置均可通过环境变量覆盖，默认值与 docker-compose.yml 中的服务一致。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# 源端发布名与被复制的表名；本模块仅支持这一张表。
PUBLICATION_NAME = "lims_pub"
SOURCE_TABLE = "lims_test_result"

# 每事务最多处理的行数，超过即视为协议/数据异常并拒绝重放。
MAX_ROWS_PER_TRANSACTION = 100

# 文本列按协议承诺不超过 100 字节（UTF-8 编码后）。
MAX_TEXT_BYTES = 100

# 断线重连的退避参数（秒）。
RECONNECT_MIN_DELAY = 1.0
RECONNECT_MAX_DELAY = 30.0

# 无消息时 read_message 的轮询间隔（秒），兼顾中断响应与 CPU 占用。
POLL_IDLE_SECONDS = 0.2

# 即使没有新事务，也按该间隔强制向源端汇报复制进度（秒）。
FEEDBACK_INTERVAL_SECONDS = 10.0


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, ""))
    except ValueError:
        return default


@dataclass(frozen=True)
class SyncConfig:
    """同步进程运行配置。"""

    dsn: str
    slot_name: str
    sqlite_path: str
    publication_name: str = PUBLICATION_NAME
    source_table: str = SOURCE_TABLE

    @classmethod
    def from_env(cls) -> "SyncConfig":
        # 与 docker-compose 中 postgres 服务的账号、数据库保持一致。
        dsn = _env(
            "LIMS_DSN",
            "postgresql://lims:lims@source-db:5432/lims",
        )
        return cls(
            dsn=dsn,
            slot_name=_env("LIMS_SLOT_NAME", "lims_lite_slot"),
            sqlite_path=_env("LIMS_SQLITE_PATH", "/data/lims.db"),
        )


def format_lsn(value: int) -> str:
    """把 64 位 LSN 整数格式化为 PostgreSQL 风格的 ``0/16B3748`` 文本。"""

    return f"{value >> 32:X}/{value & 0xFFFFFFFF:08X}"
