# Curio 第一期 · 本地实现

采集 eBay / Kleinanzeigen → 所有货进入 SQLite → 规则筛选 → 本地空模型层 → 店主审核。

这是独立本地交付，未覆盖桌面旧 Curio。复用了桌面 `curio_monitor.py` 的解析和 curl 采集函数、`judge.py` 的规则，以及本地 `screening/pipeline.py` 的记录方式。没有获得 GitHub main，原仓库的 `price.py`、26 个测试、`scripts/accept_unified.py`、310 件验收案例及价格草稿不可用，因此**不能声称原验收通过，也没有伪造对应脚本或输出**。此目录尚不是原仓库上的 PR。

## 安装与启动（Mac，Python 3.9+）

在本目录执行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

用文本编辑器填写 `.env` 中的 `EBAY_CLIENT_ID`、`EBAY_CLIENT_SECRET`，使用具备 Browse API 权限的正式环境凭据。只读该文件，不读取系统环境变量，不记录认证响应或密钥。不要提交 `.env`。

```sh
# 单次采集、规则判断、价格和下架检查
.venv/bin/python -m curio.curio_monitor
# 启动审核页（保持运行）
.venv/bin/python -m curio.review
```

电脑打开 http://127.0.0.1:8788 。手机使用同一局域网时，以 `--host 0.0.0.0` 启动，然后访问 `http://电脑局域网IP:8788`。此页面没有用户登录，仅用于可信局域网，不要映射到公网。

打开网页不会触发采集或模型调用。所有操作数据都在 `data/curio.db`：items、price_history、price_drops、cases、screening_jobs、kv。可用 `.venv/bin/python scripts/import_desktop.py /旧版路径/curio.db` 只读导入 eBay/Kleinanzeigen 商品与审核记录（不含 Lottissimo）。重复导入不会重复创建记录。旧审核缺失的当时价格快照留空，不拿当前价冒充；原价格历史保留在旧库，不迁移。导入商品需后续重新核验在售状态。采集状态和错误摘要保存在 kv。

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

## 每小时本机自动采集

桌面 `Curio.app` 的实际启动入口（旧 `EM 西洋瓷器/scripts/em-browser/desktop.py`）现已改为静默启动，源码保存在 `scripts/desktop_background.py`。双击只确保下面的每小时任务已加载：任务已存在就直接退出，不重置计时，不打开网页，不再启动旧 Playwright 采集器。原启动脚本备份在 `data/launcher-backups/`。旧浏览器采集进程已通过其关闭接口退出；旧数据不删除。

此定时任务执行增量采集和既有规则判断。桌面导出的客户证据初筛报告仍是静态快照，不会随采集自动更新。

按本次要求，调度间隔改为 **3600秒**，由当前 Mac 用户的 launchd 执行，不依赖 Codex 对话，不调用 AI。默认关键词仍为 Meissen，可在 `config/settings.yaml` 调整。

安装或更新：

```sh
.venv/bin/python scripts/install_hourly.py
```

检查 / 手动执行 / 停止：

```sh
launchctl print gui/$(id -u)/com.curio.collect.phase1
launchctl kickstart gui/$(id -u)/com.curio.collect.phase1
launchctl bootout gui/$(id -u)/com.curio.collect.phase1
```

安装文件：`~/Library/LaunchAgents/com.curio.collect.phase1.plist`。登录后加载；电脑关机或睡眠时不保证执行。若要永久停用，可把该 plist 移出 LaunchAgents，再执行 bootout。不要移动本项目和 `.venv`，移动后需重装调度路径。

该任务只运行采集与规则判断，不负责启动审核网页。每轮结果、开始/结束时间存储在同一数据库的 `collection_runs` 和 `kv` 表。失败时不会把现有商品误标为已售；下一轮重试。Kleinanzeigen 被拦暂停2小时，会明确记录 paused，而不是当成“没有新货”。旧置顶广告不会导致整页新货漏抓；eBay 增量中断时保留原去重边界，重试会继续收取未完成的新货。

每轮每关键词最多检查 Kleinanzeigen 20页；初次达到上限会记录 page_limit，不代表已经抓完历史商品。数据库对 item_id 唯一约束，不重复创建同一个平台商品；不同平台不同编号的同一件货尚不做图片去重。

不相关信息沿用旧硬过滤（招聘、房产、汽车、求购、仿品/非瓷器等），并补齐实际出现的土地、地砖、代金券。排除货仍入库保存原因，只是不进待审核。此次对旧库的两条不相关记录做了一次纠正，保留在 rule_corrections；不改变店主的历史 cases。日常判断仍为新商品一次、价格变化才重判。

## 验证

```sh
.venv/bin/python -m pytest -q
```

测试使用模拟API和临时数据库，不连接商品平台，不消耗模型额度。测试结果见 `VALIDATION.txt`。原仓库基线验收仍待拿到 main 和案例后运行；目标计数为店主「要」21件：6/15/0，店主「不要」289件：4/245/40，当前未验证。

## 2026-10-05 本机部署结果

已安装每小时任务 `com.curio.collect.phase1`。首次实采 eBay 新增1000、Kleinanzeigen新增509；随后通过 launchd 执行第二轮，两个平台均成功、新增均为0，确认已知编号不会重复入库。31个本地测试通过。新版 `.env` 已从桌面旧版安全配置，旧项目未修改。

## 采集、筛选、字段规范和微信 Bot

- [当前采集与筛选方式](docs/COLLECTION_AND_SCREENING.md)
- [字段映射及核验边界](docs/FIELD_MAPPING.md)
- [V2 全部75字段](docs/product-fields-v2.json)
- [V2 字段与命名规范正文](docs/瓷器字段与命名规则V2.txt)
- [微信接入与运行](docs/WECHAT.md)

微信适配代码位于 `wechat_bot/` 和 `scripts/wechat_feed.py`；固定版本SDK在 `third_party/`。
