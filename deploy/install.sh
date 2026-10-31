#!/usr/bin/env bash
# 服务器端一键部署。在目标机执行，不需要 root（systemd 那步用 sudo）。
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/app/content-agent}"
UV="$HOME/.local/bin/uv"
SERVICE=content-agent

echo "==> 目录 $APP_DIR"
cd "$APP_DIR"

echo "==> Python 环境"
if [ ! -x .venv/bin/python ]; then
    "$UV" venv --python 3.12 .venv
fi
"$UV" pip install --python .venv/bin/python -r requirements.txt 2>&1 | tail -4
.venv/bin/python -c "import fastapi, uvicorn; print('deps ok', fastapi.__version__)"

echo "==> 数据目录"
mkdir -p data/artifacts

echo "==> 环境变量文件"
if [ ! -f .env ]; then
    cat > .env <<'EOF'
# 真实模型时把 LLM_MOCK 改成 0，并填入 key
LLM_MOCK=1
DEEPSEEK_MODEL=deepseek-flash
EOF
    chmod 600 .env
    echo "    已创建 .env（LLM_MOCK=1）"
else
    echo "    .env 已存在，保持不变"
fi

echo "==> systemd"
sudo cp deploy/content-agent.service /etc/systemd/system/${SERVICE}.service
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE" >/dev/null 2>&1
sudo systemctl restart "$SERVICE"
sleep 3

echo "==> 状态"
systemctl is-active "$SERVICE"
curl -s --max-time 5 http://127.0.0.1:8080/api/health || echo "健康检查失败"
echo
