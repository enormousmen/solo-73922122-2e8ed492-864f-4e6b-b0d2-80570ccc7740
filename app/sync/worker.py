"""逻辑复制同步主循环。

处理顺序保证（对应需求）：

1. 启动先确保发布与**持久复制槽**就绪（槽先于演示写入创建）。
2. 咨询锁保证单进程；从 SQLite 读取已确认 LSN，无则从槽起始点续接。
3. 收到 Begin 后在内存累积行变更，仅在 Commit 时整笔写入 SQLite——
   源端回滚的事务没有 Commit，天然不会生效。
4. SQLite 事务（行数据 + 确认 LSN 同一事务）成功后，才向源端
   ``send_feedback`` 确认复制进度；确认失败则下次重放，LSN 守卫保证幂等。
5. 任何连接/协议错误都退避重连，重启进程不重建槽，从持久进度续接。
"""

from __future__ import annotations

import logging
import time

import psycopg2
from psycopg2.extras import REPLICATION_LOGICAL

from .config import (
    FEEDBACK_INTERVAL_SECONDS,
    POLL_IDLE_SECONDS,
    RECONNECT_MAX_DELAY,
    RECONNECT_MIN_DELAY,
    SyncConfig,
    format_lsn,
)
from .pgoutput import (
    PgOutputError,
    RelationInfo,
    TransactionBatch,
    parse_message,
)
from .slot import (
    SlotError,
    acquire_worker_lock,
    ensure_publication,
    ensure_replication_slot,
    open_replication_connection,
)
from .store import ResultStore

logger = logging.getLogger(__name__)


class SyncWorker:
    """单实例同步进程。"""

    def __init__(self, config: SyncConfig, store: ResultStore) -> None:
        self.config = config
        self.store = store
        self._relations: dict[int, RelationInfo] = {}
        self._batch: TransactionBatch | None = None
        self._replication_conn = None
        self._cursor = None
        self._stop = False
        self._last_feedback = 0.0

    def stop(self) -> None:
        self._stop = True

    # ----- 启动与重连 ------------------------------------------------

    def run(self) -> None:
        """阻塞运行，直到 :meth:`stop` 被调用或发生不可恢复错误。"""

        # 发布与复制槽在普通连接上准备；槽建好后才允许演示侧写入。
        ensure_publication(self.config.dsn, self.config.publication_name)
        ensure_replication_slot(self.config.dsn, self.config.slot_name)

        # 咨询锁随普通连接持有，进程退出即释放。
        lock_conn = acquire_worker_lock(self.config.dsn, self.config.slot_name)
        delay = RECONNECT_MIN_DELAY
        try:
            while not self._stop:
                try:
                    self._run_stream()
                    delay = RECONNECT_MIN_DELAY  # 正常结束后重置退避
                except (psycopg2.Error, OSError, PgOutputError) as exc:
                    if self._stop:
                        break
                    logger.warning(
                        "复制流中断（%s），%.1f 秒后重连续接", exc, delay
                    )
                    self._close_stream()
                    time.sleep(delay)
                    delay = min(RECONNECT_MAX_DELAY, delay * 2)
        finally:
            self._close_stream()
            lock_conn.close()
            logger.info("同步进程已停止，复制槽 %s 保留未删除", self.config.slot_name)

    def _run_stream(self) -> None:
        start_lsn = self.store.confirmed_lsn
        logger.info(
            "启动复制流：slot=%s start_lsn=%s",
            self.config.slot_name,
            format_lsn(start_lsn) if start_lsn else "SLOT_ORIGIN",
        )
        self._replication_conn = open_replication_connection(self.config.dsn)
        self._cursor = self._replication_conn.cursor()
        self._cursor.start_replication(
            slot_name=self.config.slot_name,
            slot_type=REPLICATION_LOGICAL,
            start_lsn=start_lsn,
            options={
                "proto_version": "1",
                "publication_names": self.config.publication_name,
            },
            decode=False,
            status_interval=FEEDBACK_INTERVAL_SECONDS,
        )
        self._relations.clear()
        # 重连后服务器从 confirmed_lsn 重新下发完整事务；若上次连接在
        # Begin/Commit 之间断开，内存里的半成品批次必须丢弃，避免与
        # 重新下发的消息拼接。源端回滚的事务同样不会有 Commit。
        self._batch = None
        self._last_feedback = time.monotonic()

        while not self._stop:
            message = self._cursor.read_message()
            if message is None:
                # 无消息时轮询，便于及时响应 stop 与发送定期反馈。
                self._maybe_force_feedback()
                time.sleep(POLL_IDLE_SECONDS)
                continue
            self._handle_xlog(message)

    def _close_stream(self) -> None:
        if self._cursor is not None:
            try:
                self._cursor.close()
            except Exception:  # noqa: BLE001 - 关闭路径忽略二次异常
                pass
            self._cursor = None
        if self._replication_conn is not None:
            try:
                self._replication_conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._replication_conn = None

    # ----- 消息处理 --------------------------------------------------

    def _handle_xlog(self, message: object) -> None:
        payload = bytes(message.payload)
        kind, parsed = parse_message(
            payload,
            self._relations,
            self.config.source_table,
            self._batch,
        )

        if kind == "begin":
            # 解析器已保证事务外才会收到 begin。
            self._batch = TransactionBatch(
                commit_lsn=parsed.commit_lsn,
                commit_ts_us=parsed.commit_ts_us,
            )
            logger.info(
                "事务开始 xid=%s commit_lsn=%s",
                parsed.xid,
                format_lsn(parsed.commit_lsn),
            )
            return

        if kind == "relation":
            # 事务内外都可能出现；缓存最新表结构即可。
            logger.info("收到 Relation 消息：relation OID=%s", parsed.relation_id)
            return

        if kind == "row":
            assert self._batch is not None
            self._batch.add(parsed)
            return

        if kind == "commit":
            batch = self._batch
            if batch is None:
                raise PgOutputError("收到 Commit 但没有活动事务")
            batch.end_lsn = parsed.end_lsn
            if parsed.commit_lsn != batch.commit_lsn:
                raise PgOutputError(
                    f"Commit LSN {parsed.commit_lsn} 与 Begin LSN "
                    f"{batch.commit_lsn} 不一致"
                )
            self._commit_batch(batch)
            self._batch = None
            return

        raise PgOutputError(f"未处理的消息种类 {kind!r}")  # pragma: no cover

    def _commit_batch(self, batch: TransactionBatch) -> None:
        before = self.store.confirmed_lsn
        self.store.apply_transaction(batch)
        after = self.store.confirmed_lsn

        if after == before:
            # 重复事务（重连后重放已确认范围），无需再次反馈。
            logger.info(
                "事务 %s 已确认过，跳过进度反馈", format_lsn(batch.commit_lsn)
            )
            return

        # 本地落库成功后才确认复制进度；feedback 失败会抛出异常进入重连，
        # 重启后从 confirmed_lsn 重新消费，LSN 守卫保证不会旧值覆盖新值。
        assert self._cursor is not None
        self._cursor.send_feedback(
            write_lsn=after,
            flush_lsn=after,
            apply_lsn=after,
            force=True,
        )
        self._last_feedback = time.monotonic()
        logger.info(
            "已同步事务 commit_lsn=%s 行数=%d 确认至 %s",
            format_lsn(batch.commit_lsn),
            len(batch.changes),
            format_lsn(after),
        )

    def _maybe_force_feedback(self) -> None:
        if time.monotonic() - self._last_feedback < FEEDBACK_INTERVAL_SECONDS:
            return
        confirmed = self.store.confirmed_lsn
        if confirmed and self._cursor is not None:
            try:
                self._cursor.send_feedback(
                    write_lsn=confirmed,
                    flush_lsn=confirmed,
                    apply_lsn=confirmed,
                    force=True,
                )
                self._last_feedback = time.monotonic()
                self.store.touch_activity()
            except psycopg2.Error:
                raise  # 交给外层重连逻辑


def build_worker(config: SyncConfig | None = None) -> tuple[SyncWorker, ResultStore]:
    """组装配置、SQLite 存储与 worker（供入口脚本与测试使用）。"""

    config = config or SyncConfig.from_env()
    store = ResultStore(config.sqlite_path, config.slot_name)
    return SyncWorker(config, store), store


def main() -> None:
    """容器入口：配置日志并运行同步进程。"""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = SyncConfig.from_env()
    worker, store = build_worker(config)
    try:
        worker.run()
    except SlotError as exc:
        # 单进程锁冲突等启动期错误：快速失败，交由容器编排决定重启策略。
        logger.error("同步进程启动失败: %s", exc)
        raise SystemExit(1) from exc
    finally:
        store.close()


if __name__ == "__main__":
    main()
