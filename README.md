# Curiobject · Mac 本地权威端

采集 eBay / Kleinanzeigen → 所有货进入 SQLite → 规则筛选 → 本地空模型层 → 店主审核。

## 仓库位置与来源（2026-11-02 合并）

- 本仓库位于 `~/Desktop/curiobject`，由原 `curio-phase1`（`~/.codex/.chatgpt-projects/.../curio-phase1`）带完整 git 历史克隆而来；原仓库最后一次提交为「合并前快照」，此后不再使用。
- 桌面旧版 `~/Desktop/curio` 的货已经 `scripts/import_desktop.py` 并入 `data/curio.db`；其未接入的 Etsy 代码保存在 `legacy/desktop_curio/`，仅供故事 2 参考。
- 跨机契约（JSON Schema 与 `schema_version` 约定）见 [`contract/README.md`](contract/README.md)。
- `.env`、`data/` 被 gitignore，从不提交。

## 安装与启动（Mac，Python 3.13，uv）

Python 版本与依赖由 [uv](https://docs.astral.sh/uv/) 管理：`pyproject.toml` 声明，`uv.lock` 锁定，虚拟环境在 `.venv`。运行依赖只有 `requests`、`PyYAML`，测试依赖 `pytest`（dev 组）。不要使用系统 `/usr/bin/python3`。

在本目录执行：

```sh
uv sync                 # 安装 Python 3.13 虚拟环境与锁定依赖
cp .env.example .env    # 首次部署才需要
```

用文本编辑器填写 `.env` 中的 `EBAY_CLIENT_ID`、`EBAY_CLIENT_SECRET`，使用具备 Browse API 权限的正式环境凭据。只读该文件，不读取系统环境变量，不记录认证响应或密钥。不要提交 `.env`。

```sh
# 单次采集、规则判断、价格和下架检查
uv run python -m curio.curio_monitor
# 启动审核页（保持运行）
uv run python -m curio.review
```

电脑打开 http://127.0.0.1:8788 。手机使用同一局域网时，以 `--host 0.0.0.0` 启动，然后访问 `http://电脑局域网IP:8788`。此页面没有用户登录，仅用于可信局域网，不要映射到公网。

打开网页不会触发采集或模型调用。所有操作数据都在 `data/curio.db`（WAL 模式，连接等待 30 秒；备份请用 `VACUUM INTO`，不要直接复制正在写入的库）：items、price_history、price_drops、cases、screening_jobs、kv。可用 `uv run python scripts/import_desktop.py /旧版路径/curio.db` 只读导入 eBay/Kleinanzeigen 商品与审核记录（不含 Lottissimo）。重复导入不会重复创建记录。旧审核缺失的当时价格快照留空，不拿当前价冒充；原价格历史保留在旧库，不迁移。导入商品需后续重新核验在售状态。采集状态和错误摘要保存在 kv。

## 采集行为

- eBay Browse API：EBAY_DE、itemLocationCountry:DE、newlyListed；关键词在 `config/settings.yaml`，默认 Meissen。
- 首次最多 1,000 条，跨关键词共享上限，按关键词顺序采集；记录分页进度，失败可继续。之后每个关键词按新上架翻页，遇到本轮增量开始前已在库里的 ID 停止（中断重试沿用同一边界）。API 每个查询最多翻至 10,000 条，**不是首次回填 10,000**。
- 已知商品另轮询详情（默认每轮20件，最久未检查优先），保留价格变化和下架记录。未出现在搜索列表不等于下架；明确404/410或无库存才标记，下架不推断为已售。
- Kleinanzeigen 沿用 curl，最多20页/关键词；遇到验证或拦截暂停2小时，状态存库。站点HTML变动仍可能影响解析，不绕过验证码。
- 每件新货调用一次 judge；仅标价或币种变化重判。规则排除的也保存原因。更改规则配置不会自动重判历史货。
- 拍卖总价 = 标价 × (1 + auction_fee_rate)，其他总价=标价。这里只包含配置手续费，不含未知运费/税费；竞拍标价是当前出价，不是最终成交价。

## 规则、模型与审核

`config/price_rules.yaml` 和 `config/slow_sellers.yaml` 初始为空，文件注释有格式。没有把未经确认的旧价格线当作正式配置；旧 judge 的默认调用行为保留，本流程禁用旧硬编码价格阈值，使用 YAML。原有文字分类规则继续使用。

破损/修复线索默认不要。`cheap_damaged_max: null` 关闭便宜破损例外；设欧元阈值后，低于阈值改为询市。只基于商品文本，不能验证图片品相或真伪。

want / probe 进入 cheap / deep 记录。`daily_budget: 0` 时不调用任何 provider；即使改为正数，也只允许本地 NoopProvider，结果原样返回。没有付费模型 SDK，没有自动出价、联系卖家功能。

审核默认显示未审核的要/询市，新货优先；被规则排除页可以改判。点击要/询市/不要后可填原因和心理价，不要必须写原因。保存后从待审核中移除；每次判断追加 cases，保留当时的规则原因、标价、总价和规则版本。重复请求幂等，价格更新后的旧表单会被拒绝，避免错配快照。已审核商品改价不会自动重新进入待审核。

## 每30分钟本机自动采集（launchd）

只安装一个定时任务 `com.curiobject.collect`：每 1800 秒运行一次 `curio.curio_monitor`，由当前 Mac 用户的 launchd 执行，plist 指向本仓库 `.venv/bin/python`，不依赖 Codex 对话，不调用 AI。日志写入 `data/logs/collect.log` 与 `data/logs/collect-error.log`。

视觉识别进程（`scripts/install_visual_worker.py`，`com.curio.visual.phase1`）与微信机器人代码保留在仓库中，但**不安装**它们的定时任务。

桌面 `Curio.app` 的静默启动入口源码在 `scripts/desktop_background.py`（路径以仓库根目录为准）：任务已加载就直接退出，否则安装并运行一次。原启动脚本备份在 `data/launcher-backups/`。

安装或更新：

```sh
uv run python scripts/install_hourly.py
```

检查 / 手动执行 / 停止：

```sh
launchctl print gui/$(id -u)/com.curiobject.collect
launchctl kickstart gui/$(id -u)/com.curiobject.collect
launchctl bootout gui/$(id -u)/com.curiobject.collect
```

安装文件：`~/Library/LaunchAgents/com.curiobject.collect.plist`。登录后加载；电脑关机或睡眠时不保证执行。不要移动本仓库和 `.venv`，移动后需重新运行 `uv sync` 与安装脚本。

从旧任务切换（一次性；已于 2026-11-02 完成，以下仅作记录）：

```sh
cd ~/Desktop/curiobject
mkdir -p data/launcher-backups
launchctl bootout gui/$(id -u)/com.curio.collect.phase1
launchctl bootout gui/$(id -u)/com.curio.visual.phase1 || true   # 未加载时会报错，可忽略
mv ~/Library/LaunchAgents/com.curio.*.phase1.plist data/launcher-backups/
uv run python scripts/install_hourly.py
launchctl kickstart gui/$(id -u)/com.curiobject.collect
```

回退：`launchctl bootout gui/$(id -u)/com.curiobject.collect`，再把 `data/launcher-backups/com.curio.collect.phase1.plist` 放回 `~/Library/LaunchAgents/` 并 `launchctl bootstrap gui/$(id -u)` 该文件（原仓库只多了一次「合并前快照」提交，其数据只被读取、未被改动）。

该任务只运行采集与规则判断，不负责启动审核网页。每轮结果、开始/结束时间存储在同一数据库的 `collection_runs` 和 `kv` 表。失败时不会把现有商品误标为已售；下一轮重试。Kleinanzeigen 被拦暂停2小时，会明确记录 paused，而不是当成“没有新货”。旧置顶广告不会导致整页新货漏抓；eBay 增量中断时保留原去重边界，重试会继续收取未完成的新货。

每轮每关键词最多检查 Kleinanzeigen 20页；初次达到上限会记录 page_limit，不代表已经抓完历史商品。数据库对 item_id 唯一约束，不重复创建同一个平台商品；不同平台不同编号的同一件货尚不做图片去重。

不相关信息沿用旧硬过滤（招聘、房产、汽车、求购、仿品/非瓷器等），并补齐实际出现的土地、地砖、代金券。排除货仍入库保存原因，只是不进待审核。此次对旧库的两条不相关记录做了一次纠正，保留在 rule_corrections；不改变店主的历史 cases。日常判断仍为新商品一次、价格变化才重判。

## 验证

```sh
uv run pytest -q
```

测试使用模拟API和临时数据库，不连接商品平台，不消耗模型额度。测试结果见 `VALIDATION.txt`。原仓库基线验收仍待拿到 main 和案例后运行；目标计数为店主「要」21件：6/15/0，店主「不要」289件：4/245/40，当前未验证。

## 2026-10-05 本机部署结果（历史记录（迁移前））

已安装每小时任务 `com.curio.collect.phase1`。首次实采 eBay 新增1000、Kleinanzeigen新增509；随后通过 launchd 执行第二轮，两个平台均成功、新增均为0，确认已知编号不会重复入库。31个本地测试通过。新版 `.env` 已从桌面旧版安全配置，旧项目未修改。

## 采集、筛选、字段规范和微信 Bot

- [当前采集与筛选方式](docs/COLLECTION_AND_SCREENING.md)
- [字段映射及核验边界](docs/FIELD_MAPPING.md)
- [V2 全部75字段](docs/product-fields-v2.json)
- [V2 字段与命名规范正文](docs/瓷器字段与命名规则V2.txt)
- [微信接入与运行](docs/WECHAT.md)

微信适配代码位于 `wechat_bot/` 和 `scripts/wechat_feed.py`；固定版本SDK在 `third_party/`。
