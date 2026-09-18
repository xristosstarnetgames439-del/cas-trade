#!/bin/bash
# 双击启动 StockMaster 本地开发（后端 8010 --reload + 前端 3110 Vite HMR）
cd "$(dirname "$0")"
exec ./stockmaster.sh start
