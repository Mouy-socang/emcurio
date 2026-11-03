# Curiobject · Mac 本地权威端

采集 eBay / Etsy / Kleinanzeigen → 所有货进入 SQLite → 规则筛选 → 本地空模型层 → 店主审核。

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

用文本编辑器填写 `.env` 中的 `EBAY_CLIENT_ID`、`EBAY_CLIENT_SECRET`，使用具备 Browse API 权限的正式环境凭据。Etsy 可选：填 `ETSY_API_KEY`（Keystring）与 `ETSY_SHARED_SECRET`；不填或留空则跳过 Etsy，每轮结果写 `etsy_status: disabled`。只读该文件，不读取系统环境变量，不记录认证响应或密钥。不要提交 `.env`。

```sh
# 单次采集、规则判断、价格和下架检查
uv run python -m curio.curio_monitor
# 启动审核页（保持运行）
uv run python -m curio.review
```

电脑打开 http://127.0.0.1:8788 。手机使用同一局域网时，以 `--host 0.0.0.0` 启动，然后访问 `http://电脑局域网IP:8788`。此页面没有用户登录，仅用于可信局域网，不要映射到公网。

打开网页不会触发采集或模型调用。所有操作数据都在 `data/curio.db`（WAL 模式，连接等待 30 秒；备份请用 `VACUUM INTO`，不要直接复制正在写入的库）：items、price_history、price_drops、cases、screening_jobs、kv。可用 `uv run python scripts/import_desktop.py /旧版路径/curio.db` 只读导入 eBay/Kleinanzeigen 商品与审核记录（不含 Lottissimo）。重复导入不会重复创建记录。旧审核缺失的当时价格快照留空，不拿当前价冒充；原价格历史保留在旧库，不迁移。导入商品需后续重新核验在售状态。采集状态和错误摘要保存在 kv。

## 调度与接口调用预算（AD-27）

launchd 每 5 分钟唤醒一次 `curio.curio_monitor`，由 `curio/schedule.py` 按 kv 里的状态决定本轮做什么：

- **新货抓取**：基础间隔初始 30 分钟；每轮抓完按三平台合计新货调整——≥10 件减半、0 件 ×1.5、其余不变，夹在 5–60 分钟。柏林时间 01:00–06:59 实际间隔再 ×2（仍不超过 60）。唤醒时刻允许 30 秒抖动。
- **在售检查**（`check_existing`）：独立节拍，每 30 分钟最多一轮，每轮最多 `status_check_limit` 件，在还有检查预算的平台间平分；每平台本轮份额再不超过「剩余检查预算 ÷ 到柏林午夜还剩的检查轮数」（至少 1），让检查份额撑满一整天。Etsy 按 100 个编号一批查。预算中途用完而没发出的检查不写检查记录、不算错误，计入 `budget_skipped`。
- 新货抓取后只有至少一个平台状态为 ok 时才调整间隔；全部出错、暂停或预算用完时间隔保持不变。
- 两者都没到期时本轮直接结束（输出 `{"status":"idle",...}`），不写 `collection_runs`。同一时间只有一轮（kv 锁）。
- **每日预算** `config/settings.yaml` 的 `api_budget`（默认 eBay 4500、Etsy 4500、Kleinanzeigen 1500）：每个外部 HTTP 请求（含 eBay OAuth——令牌缓存在 `data/ebay_oauth.json`（gitignore，权限 0600，不进数据库），到期前 60 秒内复用，遇 401 换一次令牌重试，不是每次唤醒都换；Etsy 换 key 写法与 429 重试、Kleinanzeigen 每次 curl）发出前计数，按柏林日期归零，计数存 kv `api_usage`。新货抓取最多用 60%，在售检查保底 40%。某平台用完当天跳过，结果写 `<平台>_status: budget_exhausted`（在售检查写入 `status_check_summary.budget_exhausted`）。
- 每轮结果（`collection_runs.result` 与 kv `last_collection_result`）含：`fetch_ran` / `status_check_ran`、`ebay_status` / `kleinanzeigen_status` / `etsy_status`（ok、error、paused、page_limit、budget_exhausted、disabled、not_due）、各平台 `*_new` 与 `new_total`、`api_usage`（当日各平台已用、上限、新货份额）、`fetch_interval_min`、`fetch_base_interval_min`、`night_mode`、`next_fetch_at`。另有：
  - 文字说明 `ebay`、`etsy`（出错原因）与 `kleinanzeigen_error`；只做在售检查的轮次 eBay 缺凭据时 `ebay_status` 为 error。
  - `status_check_summary`：`attempted`、`active`、`gone`、`errors`、`paused`、`unknown`（Etsy 未返回该货或无 state，保持原状态）、`budget_skipped`、`budget_exhausted`、`etsy_error`、`remaining_active`；整轮检查失败时写 `status_checks`。
  - `collection_runs.status`：三平台状态都是 ok / disabled / not_due、且在售检查没有 errors 与 `etsy_error` 时为 ok，否则 partial。

```sh
sqlite3 data/curio.db "SELECT result FROM collection_runs ORDER BY id DESC LIMIT 1"
```

## 采集行为

- 关键词在 `config/settings.yaml`：Meissen、KPM Berlin，三个平台共用。
- eBay Browse API：EBAY_DE、itemLocationCountry:DE、newlyListed。
- 首次最多 1,000 条，跨关键词共享上限，按关键词顺序采集；记录分页进度，失败可继续。之后每个关键词按新上架翻页，遇到本轮增量开始前已在库里的 ID 停止（中断重试沿用同一边界）。API 每个查询最多翻至 10,000 条，**不是首次回填 10,000**。
- 已知商品另轮询详情（默认每轮20件，最久未检查优先），保留价格变化和下架记录。未出现在搜索列表不等于下架；明确404/410或无库存才标记，下架不推断为已售。
- Kleinanzeigen 沿用 curl，最多20页/关键词；遇到验证或拦截暂停2小时，状态存库。站点HTML变动仍可能影响解析，不绕过验证码。
- Etsy Open API v3（`curio/etsy.py`，移植自 `legacy/desktop_curio/etsy_source.py`）：只在 `.env` 有 `ETSY_API_KEY` 时启用；按最新上架翻页（每关键词每轮最多 300 条），一页全是已知货即停，只为新货批量补图。货号 `etsy:<listing_id>`，经 `store.ingest` 去重并记录价格变化。账号额度 5000 次/天、5 次/秒，代码保证相邻请求间隔不少于 0.2 秒。在售检查：`state` 不是 active 或 `quantity` 为 0 判下架（`quantity` 缺失视为在售）；批量查询中途失败时只对拿到详情的编号下结论，其余保持原状态并记录错误。Etsy 未启用、鉴权失败（如 403）或出错时不影响 eBay 与 Kleinanzeigen。两种 key 写法都被 401/403 拒绝后，kv 记 `etsy_auth_backoff_until`（1 小时），期间 Etsy 不发任何请求（含在售检查），结果写 `etsy_status: error`、`etsy: Etsy 鉴权失败，1 小时后重试`；之后第一次请求成功即清除。
- 每件新货调用一次 judge；仅标价或币种变化重判。规则排除的也保存原因。更改规则配置不会自动重判历史货。
- 拍卖总价 = 标价 × (1 + auction_fee_rate)，其他总价=标价。这里只包含配置手续费，不含未知运费/税费；竞拍标价是当前出价，不是最终成交价。

## 规则、模型与审核

`config/price_rules.yaml` 和 `config/slow_sellers.yaml` 初始为空，文件注释有格式。没有把未经确认的旧价格线当作正式配置；旧 judge 的默认调用行为保留，本流程禁用旧硬编码价格阈值，使用 YAML。原有文字分类规则继续使用。

破损/修复线索默认不要。`cheap_damaged_max: null` 关闭便宜破损例外；设欧元阈值后，低于阈值改为询市。只基于商品文本，不能验证图片品相或真伪。

want / probe 进入 cheap / deep 记录。`daily_budget: 0` 时不调用任何 provider；即使改为正数，也只允许本地 NoopProvider，结果原样返回。没有付费模型 SDK，没有自动出价、联系卖家功能。

审核默认显示未审核的要/询市，新货优先；被规则排除页可以改判。点击要/询市/不要后可填原因和心理价，不要必须写原因。保存后从待审核中移除；每次判断追加 cases，保留当时的规则原因、标价、总价和规则版本。重复请求幂等，价格更新后的旧表单会被拒绝，避免错配快照。已审核商品改价不会自动重新进入待审核。

## 拣选状态与货号（AD-26、AD-7）

拣选状态只经 `curio/triage.py` 写入（表 `triage`、追加式事件表 `triage_events`、货号表 `item_codes`，由 `Store` 建表）：

- 状态：`new` 新货、`kept` 留下、`removed` 去掉、`auto_removed` 自动移出、`out_of_pool` 移出商品池。
- 系统（规则归口）：入库时判断 `skip` → `auto_removed`，其余 → `new`；重判（改价、屏蔽词全量重判）只在**没有任何人工事件**的货上于 `new` / `auto_removed` 间移动。有人工事件的货只更新分数与理由，不改状态。
- `cases` / `market_decisions` 仍是决定日志（追加/覆盖照旧，旧读者不变），**拣选状态以 `triage` 为准**；撤销只写 `triage_events`，不改写 cases。
- 人工动作：留下（`new`/`removed` → `kept`）、去掉（`new`/`kept`/`auto_removed`/`out_of_pool` → `removed`）、捞回（`auto_removed` → `new`，排新货队列最前）、移出商品池（`kept` → `out_of_pool`）、放回商品池（`out_of_pool` → `kept`）、撤销上一步（`undo`：撤销最近一个未撤销的人工动作，可连续多步，系统动作不进撤销栈；之后又有人工动作时抛「状态已变化」）。任何人工转换与撤销都清除 `backlog`。非法转换抛中文 `TriageError`。
- 现有入口已接入（事件 `source` 记来源）：`Store.review` 的 want → `triage.keep`（自动移出的捞回+留下记为同一组 `grp`，一次撤销全部回退；移出商品池的放回）、skip/irrelevant → 去掉、probe → 自动移出的先捞回，再 `triage.note_probe` 记一条不改状态的人工询市事件（任何状态都记）；`Store.block` 的那件货 → 去掉；`market.set_decision` 无关 → 去掉（`source='market'`，事件时间等于 `market_decisions.updated_at`），取消无关 → 只撤销最近一次未撤销的市场页去掉，没有则不动，之后有过其他人工动作则报「状态已变化」；只在 `market_items`、不在 `items` 的货跳过拣选。
- 每条事件记：UTC 时间、旧/新状态、动作、执行者（human/system）、当时的 `agent_decision`、分数（`items` 尚无分数列时为空）、`rule_version`、理由。
- 货号 `CO-` + 至少 4 位序号（`CO-0001`…`CO-9999`、`CO-10000`…），首次进入 `kept` 时在写事务内分配（未开事务时本模块执行 `BEGIN IMMEDIATE`；调用方自己开事务须用 `BEGIN IMMEDIATE` 或已写过，即已持有写锁；代码断言 `in_transaction`，`code`/`item_id` 唯一约束兜底）；`code`、`item_id` 各自唯一；再次留下返回原货号；撤销、去掉、移出都不释放，号码永不复用。
- 新货队列 `triage.queue`：只含 `items.status='active'` 的货；捞回的在前（最近捞回优先），其余按 `first_seen` 新到旧，不含 `backlog`。
- 采集 `ingest` 只在新货或改价/改文后调用 `system_place`。

### 迁移

```sh
sqlite3 data/curio.db ".backup data/backups/curio-before-triage-<UTC时间>.db"
uv run python scripts/migrate_triage.py      # 幂等，可重跑；打印各状态计数；数据库路径不存在时报错退出
```

规则：两表都有决定时按解析后的时间取较新者：市场页较新则它说了算（无关 → `removed`，取消无关 → 无人工决定）；否则看 `cases` 最新一条：irrelevant/skip → `removed`（human，沿用原时间）；`cases` 的 want → `kept` 并按决定时间顺序分配货号；probe → `new`（human）；其余按判断结果入 `new`/`auto_removed`。迁移时刻（kv `triage_migrated_at`，首次运行写入，重跑沿用，无法解析时报「迁移时刻无效」）前 7 天以上入库、未经人工的 `new` 货标 `backlog=1`（`first_seen` 为空或无法解析的不标），不进新货队列（待故事 7 处理）。已有人工事件的货，重跑时只应用比它最近一次人工事件**更新**的决定（记为 `migrate` 事件），所以不会重复写事件或货号。`scripts/import_desktop.py`、`market.migrate_legacy` 仍直接写 `cases` / `market_decisions`，用它们导入后需再跑一次本迁移，新导入的较新决定会被应用。重跑还会给早期迁移的市场页去掉补 `source='market'`。

2026-11-02 真实库迁移结果：items 21996 = triage 21996；`removed` 195（cases 180 件 + market_decisions 15 件，全为人工无关/不要）、`auto_removed` 10539、`new` 11262（其中 backlog 7050，新货队列 4212）；无 want，货号 0 个。备份在 `data/backups/curio-before-triage-20261102T200445Z.db`。第二次运行新增事件 0、货号 0。

## 标准名称与价格库（CAP-16，AD-15）

**知识库只读。** 词表唯一来源是 EM 西洋瓷器知识库（`config/settings.yaml` 的 `em_kb_path`，默认 `~/Desktop/EM 西洋瓷器`，`~` 会展开）。Curiobject 只读其中的文件夹名与 CSV（`utf-8-sig`），任何代码路径都不写入、不改名、不建文件；新名称由墨墨在知识库里维护：改了 CSV（或知识库从缺失恢复）后，词表缓存按 `kb_fingerprint` 失效；某件货下次被采集到时，若库里结果的 `kb_fingerprint` 与当前不同就重新生成；要一次刷新全部存量，重跑回填脚本。

**词表：**

| 段 | 来源 | ID |
|---|---|---|
| 品牌 | `06 Brands and Manufactories 品牌与窑厂/` 下的文件夹名（允许德语形容词词尾 -er，如 Meissener） | 文件夹名 |
| 图案（仅 Meissen） | `Meissen/4 Patterns 图案/Meissen_图案主题数据.csv`（外文名及括号内别名）+ `Meissen_官方图案目录数据.csv`（德文名、六位编号；编号已归入某个 MP 的用 MP） | `MP-xxx` 或六位编号 |
| 器型（仅 Meissen） | `Meissen/3 Forms and Relief 器型与浮雕/Forms 器型/Meissen_器型索引.csv` | `MS-xxx` |
| 功能（所有品牌） | `05 General Knowledge 通用知识/买手培训/功能词表.csv`（德文、英文、别名与卖家用词） | `FN-xxx` |

匹配大小写、重音不敏感，按词边界；词内空格/逗号/连字符宽松（`Platte, oval` 能匹配 `Platte Oval`）；功能词还可作德语复合词词尾（`Porzellanvase` → 花瓶）。同时命中多个词条时取最长的；先看标题，标题没有再看描述（年代只看标题）。“im Stil von / wie / nicht / à la / style of Meissen”“Meissen-Art / -Stil / style”不算品牌。NNxxxx 式图案编号取最长匹配前缀。别名单元格里括号说明、“注意…/不写…/另立…”这类反例说明和带日期的批注不作词条。Meissen 专属词表只在品牌识别为 Meissen 时使用；不从图案反推品牌。

**命名（知识库 V2）：** 年代 → 品牌 → 厂家等级 → 图案 → 器型 → 功能，空格分隔，缺值段省略。品牌显示为外文名+中文名（`Meissen梅森`；其它品牌知识库暂无中文名时只写外文名）。厂家等级须可靠证据，卖家写的 `1. Wahl` / `I. Wahl` / `2. Wahl` 等只存原文 `seller_grade`，不入名称。年代只看标题，在出现明确四位年份（`1900`、`um 1900` → 约1900年、`1860-1910`）或 `19. Jh.` / `19./20. Jh.` 时写入；“seit 1710”、价格（Preis/EUR/€/VB 后）、购买与运费（gekauft/Kauf/Versand/Neuware 后）、尺寸里的数字不算。**没有器型也没有功能时不生成名称**（`std_name` 为空，`status='insufficient'`）。全部结果都是据卖家描述的候选，不是正式商品标题。

**存储：** 表 `std_names`（主键 `item_id`）：`std_name`、`std_key`（`品牌|图案ID|器型ID|功能ID`，缺值写 `-`；年代不进分组键）、`status`（named / insufficient）、各段 ID、`era`、`seller_grade`、`segments`（各段显示名 JSON）、`kb_status`（`ok` / `missing` / `partial:缺失的部分`）、`kb_fingerprint`、`generated_at`。`Store.ingest` 在新货或标题/描述/价格变化、该货尚无结果、或其 `kb_fingerprint` 与当前知识库不同时于同一事务写入；判断结论、拣选状态、货号都不受影响；命名本身出错时只记一次日志、不写该行，入库照常。知识库缺失或某个 CSV 无法解析时采集照常，对应段视为未知，日志警告一次。

**查询：**

```python
store.similar_prices('Meissen|MP-001|-|FN-009')   # 同类（std_key 相同且已命名）全部 price_history 行，按时间排序，附币种与标题
store.by_code('CO-0001')                           # 经 item_codes 关联：标准名称各字段 + prices（同类历史价）；未知货号返回 {}
```

`insufficient` 的货、以及品牌段为 `-` 的键（会把各厂同功能的货混在一起）不参与同类查价，返回空列表。价格库只在 Mac `curio.db`，不上线。

**回填（幂等，以 item_id 覆盖；结果相同的行不改写）：**

```sh
sqlite3 data/curio.db ".backup data/backups/curio-before-std-names-<UTC时间>.db"
uv run python scripts/backfill_std_names.py   # 打印 items / std_names 行数、生成与 insufficient 计数、各品牌计数、Meissen 有名称比例
```

2026-11-03 真实库回填结果（评审修复后重跑）：items 22113 = std_names 22113；named 11589、insufficient 10524，同类组 1050 个；Meissen 有名称 9614/19023（50.5%）；`kb_status` 全为 ok。第二次运行写入 0。备份在 `data/backups/curio-before-std-names-v2-20261103T075432Z.db`（首次回填前另有 `curio-before-std-names-20261103T074616Z.db`）。

## 每5分钟唤醒的本机自动采集（launchd）

只安装一个定时任务 `com.curiobject.collect`：每 300 秒唤醒一次 `curio.curio_monitor`，由调度器决定是否真正抓取（见上文「调度与接口调用预算」），由当前 Mac 用户的 launchd 执行，plist 指向本仓库 `.venv/bin/python`，不依赖 Codex 对话，不调用 AI。日志写入 `data/logs/collect.log` 与 `data/logs/collect-error.log`。

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
