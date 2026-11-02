# 桌面旧版 Curio 遗留代码

`etsy_source.py` 从 `~/Desktop/curio/etsy_source.py` 原样复制（2026-11-02）。

**仅供故事 2（接入 Etsy 采集）参考，未接入。** 本仓库的采集、判断、审核代码都不导入此文件；密钥通过环境变量 `ETSY_API_KEY`、`ETSY_SHARED_SECRET` 读取，文件中不含任何密钥。

桌面旧版 `curio.db` 中的货已由 `scripts/import_desktop.py` 并入 `data/curio.db`（只读导入，跳过重复与不支持的平台）。

## 故事 2 移植时注意

- 文件开头说明「兼容 macOS 自带 Python 3.9」已过时：本仓库运行在 uv 管理的 Python 3.13。
- 密钥必须走本仓库 `.env` 的读取方式，不要像原文件那样直接读进程环境变量 `os.environ`。
- `check_gone` 在 `fetch_batch` 提前停止（只取回部分批次）时，会把未取到的货误标为下架；移植时必须跳过未取到详情的编号。
- `(quantity or 1)` 会把库存 0 当作 1，从而把已售罄的货判为在售；应区分 `None` 与 `0`。
