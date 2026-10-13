#!/usr/bin/env bash
# Cortex Console — ai-server 统一启动入口
#
# 读 /etc/cortex-console/ai-server.conf 的 default 指向的 section，设置 env 后
# exec 该 section 的 execute（推理服务前台运行，崩溃由 systemd Restart=on-failure 拉起）。
#
# 配置格式与解析逻辑全部在 service/ai_conf.py —— 与 cortex-console 管理页共用同一份实现，
# 不存在两份解析漂移。详见 service/README.md。
#
# 用法：
#   dispatch.sh                      正常启动（systemd 调用）
#   CORTEX_DRY_RUN=1 dispatch.sh     只打印将执行的命令，不启动
#   AI_SERVER_CONF=/path dispatch.sh 指定其他 conf 文件（测试用）
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="${AI_SERVER_CONF:-/etc/cortex-console/ai-server.conf}"

if [ ! -f "$HERE/ai_conf.py" ]; then
    echo "ERROR: 缺少 $HERE/ai_conf.py（cortex-console 安装不完整）" >&2
    exit 1
fi

exec python3 "$HERE/ai_conf.py" launch "$CONF"
