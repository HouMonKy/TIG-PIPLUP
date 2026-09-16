#!/bin/bash
VENV_PATH="/opt/cn_log/env_v2"
SERVICE_SCRIPT="main.py"
SCRIPT_DIR=$(dirname "$(readlink -f "$0")")
WORKING_DIR=$(dirname -- "$SCRIPT_DIR")
echo "Starting service from: $SCRIPT_DIR"
# 检查虚拟环境是否存在
if [ ! -f "$VENV_PATH/bin/activate" ]; then
    echo "Error: Virtual environment not found at $VENV_PATH"
    exit 1
fi
# 检查服务脚本是否存在
if [ ! -f "$WORKING_DIR/$SERVICE_SCRIPT" ]; then
    echo "Error: Service script $SERVICE_SCRIPT not found in $SCRIPT_DIR"
    exit 1
fi
# 激活虚拟环境并运行服务
echo "Activating virtual environment: $VENV_PATH"
source "$VENV_PATH/bin/activate"
# 设置环境变量
export PYTHONPATH="$WORKING_DIR:$PYTHONPATH"
echo "Starting Python service with PYTHONPATH: $PYTHONPATH"
cd "$WORKING_DIR" && \
nohup python -u "$SERVICE_SCRIPT" > service.log 2>&1 &
