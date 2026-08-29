#!/usr/bin/env bash

set -euo pipefail

: "${TRADE_NEWS_TUNNEL_USER:?请先设置 TRADE_NEWS_TUNNEL_USER}"
: "${TRADE_NEWS_TUNNEL_PASSWORD:?请先设置 TRADE_NEWS_TUNNEL_PASSWORD}"

port="${TRADE_NEWS_TUNNEL_PORT:-8000}"
case "$port" in
  ""|*[!0-9]*)
    echo "TRADE_NEWS_TUNNEL_PORT 必须是数字" >&2
    exit 2
    ;;
esac

command -v ngrok >/dev/null 2>&1 || {
  echo "未找到 ngrok，请先安装并完成 ngrok config add-authtoken" >&2
  exit 2
}
command -v python3 >/dev/null 2>&1 || {
  echo "未找到 python3，无法安全生成 ngrok Traffic Policy" >&2
  exit 2
}

policy_dir="$(mktemp -d "${TMPDIR:-/tmp}/trade-news-ngrok.XXXXXX")"
policy_file="$policy_dir/traffic-policy.json"

cleanup() {
  rm -f "$policy_file"
  rmdir "$policy_dir" 2>/dev/null || true
}
trap cleanup EXIT INT TERM HUP

umask 077
python3 - "$policy_file" <<'PY'
import json
import os
import sys

username = os.environ["TRADE_NEWS_TUNNEL_USER"]
password = os.environ["TRADE_NEWS_TUNNEL_PASSWORD"]
if any(char in username for char in ":\r\n"):
    raise SystemExit("TRADE_NEWS_TUNNEL_USER 不能包含冒号或换行")
if any(char in password for char in "\r\n"):
    raise SystemExit("TRADE_NEWS_TUNNEL_PASSWORD 不能包含换行")
if len(password) < 8:
    raise SystemExit("TRADE_NEWS_TUNNEL_PASSWORD 至少需要 8 个字符")

policy = {
    "on_http_request": [
        {
            "actions": [
                {
                    "type": "basic-auth",
                    "config": {
                        "realm": "trade-news",
                        "credentials": [f"{username}:{password}"],
                        "enforce": True,
                    },
                }
            ]
        }
    ]
}
with open(sys.argv[1], "x", encoding="utf-8") as file:
    json.dump(policy, file)
PY

echo "正在通过 ngrok 安全转发 http://127.0.0.1:${port}"
echo "请复制下方 Forwarding 的 https://...ngrok.app 地址作为 PUBLIC_BASE_URL"
ngrok http "http://127.0.0.1:${port}" --traffic-policy-file "$policy_file"
