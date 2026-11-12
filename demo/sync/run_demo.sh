#!/bin/sh
# 编排完整 LIMS 同步演示：
#   同事务多行更正 -> 回滚不生效 -> 删除不遗漏 -> 断连续接 -> API 验证
# 所有写入步骤都会先等待持久复制槽创建（先建槽、后写入）。
set -eu

cd "$(dirname "$0")"
export PYTHONPATH="$(pwd):${PYTHONPATH:-}"
export PYTHONUNBUFFERED=1

echo "== 阶段 1：初始插入 + 同事务多行更正 =="
python -I 01_seed_and_correct.py

echo
echo "== 阶段 2：回滚事务（本地不生效） =="
python -I 02_rollback.py

echo
echo "== 阶段 3：删除同步 =="
python -I 03_delete.py

echo
echo "== 阶段 4：断连续接 =="
python -I 04_reconnect.py

echo
echo "== 阶段 5：API 验证 =="
python -I 05_verify.py
