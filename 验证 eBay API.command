#!/bin/bash
cd -- "$(dirname -- "$0")" || exit 1
printf '正在验证 eBay API：授权 + 最多 3 件商品，不启动批量采集。\n'
.venv/bin/python scripts/verify_ebay_api.py
printf '\n检查完成。按回车关闭。\n'
read -r _curio_finish
