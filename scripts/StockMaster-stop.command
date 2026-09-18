#!/bin/bash
# 双击停止 StockMaster 本地开发服务
cd "$(dirname "$0")"
exec ./stockmaster.sh stop
