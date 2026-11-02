# 桌面旧版 Curio 遗留代码

`etsy_source.py` 从 `~/Desktop/curio/etsy_source.py` 原样复制（2026-11-02）。

**仅供故事 2（接入 Etsy 采集）参考，未接入。** 本仓库的采集、判断、审核代码都不导入此文件；密钥通过环境变量 `ETSY_API_KEY`、`ETSY_SHARED_SECRET` 读取，文件中不含任何密钥。

桌面旧版 `curio.db` 中的货已由 `scripts/import_desktop.py` 并入 `data/curio.db`（只读导入，跳过重复与不支持的平台）。
