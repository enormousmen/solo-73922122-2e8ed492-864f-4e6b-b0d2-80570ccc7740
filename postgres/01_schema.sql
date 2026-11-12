-- LIMS 检测结果源表：仅一张初始空表。
-- 整数主键 result_id；样品码/项目/检测值为不超过 100 字节的非空文本。
-- 长度以字节（octet_length）计，与协议承诺的 100 字节上限一致。
CREATE TABLE IF NOT EXISTS lims_test_result (
    result_id      INTEGER PRIMARY KEY,
    sample_code    TEXT NOT NULL CHECK (octet_length(sample_code)    BETWEEN 1 AND 100),
    test_item      TEXT NOT NULL CHECK (octet_length(test_item)      BETWEEN 1 AND 100),
    measured_value TEXT NOT NULL CHECK (octet_length(measured_value) BETWEEN 1 AND 100)
);

-- 仅发布这一张表；发布由同步进程幂等创建，这里不预先 CREATE PUBLICATION，
-- 以保证“先建持久复制槽，再允许演示写入”的顺序由同步进程显式控制。
