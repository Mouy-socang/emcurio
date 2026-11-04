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
# 启动后台（保持运行；只监听本机 127.0.0.1）
uv run python -m curio.review
```

电脑打开 http://127.0.0.1:8788 ：`/` 是后台一页拣选（01 新货，手机优先），旧审核页在 http://127.0.0.1:8788/old 。服务只绑定本机地址，`--host` 只接受 `127.0.0.1` / `localhost`，不提供局域网或公网监听（AD-4）。

### 手机访问：tailscale serve（由墨墨自行安装与操作）

手机经 Tailscale 访问 Mac 上的后台，不上公网。Tailscale 本仓库不安装、不配置，launchd 任务也不改，以下步骤由墨墨操作：

1. 在 Mac 与手机上安装 Tailscale，用同一账号登录（同一个 tailnet）。
2. 在 Mac 上保持后台运行（`uv run python -m curio.review`），然后执行：
   ```sh
   tailscale serve --bg 8788
   ```
   它把本机 `127.0.0.1:8788` 以 HTTPS 只在 tailnet 内提供；`tailscale serve status` 显示访问地址（形如 `https://<mac 名>.<tailnet>.ts.net/`）。
3. 手机浏览器打开该地址。停止共享：`tailscale serve --https=443 off`（或 `tailscale serve reset`）。

不要用 `tailscale funnel`（那会公开到互联网）。写接口仍校验页面令牌（`X-Curio-Token`，每次启动重新生成，重启服务后刷新页面）与 Origin；经 tailscale serve 反代时，Origin 与 `Host` 或 `X-Forwarded-Host` 一致即认可。

### 后台 01 新货（CAP-6，故事 7；故事 22 瀑布流与行为记录）

- 顶部分区 01–07：01 新货显示待处理件数；02 商品池、03 榜见下节；04 匹配见"后台 04 匹配"一节；05 待审核见"后台 05 待审核"一节；06 客人见"后台 06 客人"一节；07 符合度见"后台 07 符合度"一节。
- **瀑布流**（故事 22，`GET /api/triage/feed?limit=&cursor=&session=`，`triage.feed`）：多列大图卡片（手机 2 列，≥600px 3 列，≥900px 4 列；一个 `<ol>`，DOM 顺序 = 阅读顺序，脚本按顺序放进当前较短的一列，不用 CSS columns）。集合与旧队列相同（新货、非 backlog、在售），但**不按分数排**：捞回的最前（最近捞回优先），其余按 `first_seen` 新到旧、同时间按 `item_id`；卡上**不显示分数**（货详情里照旧）。每批 40 件，滑到底按游标（上一批最后一件的排序键，不用 offset）续载，浏览中新入库的货排在游标之前，不会让后面的批次重复或漏件；加载失败显示「未能加载 · 重试」。旧接口 `GET /api/triage`（分数排序的 `triage.queue`）保留给其他调用方。
- 卡片：主图（`store.card` 的图片来源，按原比例）、名称与标签、价格、平台、货号（已留下才有）；「去掉」「留下」两颗按钮；点图打开货详情。界面先让卡片淡出再保存（`POST /api/triage/action`，`request_id` 幂等）；保存失败时该卡原位放回并在卡内显示「未保存 · 重试」，顶部同时显示「N 件未保存 · 重试」；断网显示「网络已断开，操作未保存 · 重试」。旧版的滑动手势与 ←/→ 键已随单卡界面移除。
- **看过即不要**（墨墨 2026-11-04）：某件货在一次打开 01（`session_id`）里被记为 `seen`、且没被留下，之后的打开里不再进瀑布流（本次打开内保持原位）。不改拣选状态（仍为新货），只是瀑布流排除；01 计数为瀑布流实际能看到的件数（新货减去被排除的）。比较用实际看到的时间 min(`client_ts`, `at`)（列 `eff_at`；`client_ts` 无效或在未来时取 `at`），只算捞回之后、最近一次「放回」之后的 seen，晚到的补发不会把已放回的货又藏起来；一闪而过（未到 seen）的下次照常出现。离开 01 再回来算一次新的打开（先补发排队的事件，再从头重读瀑布流）。
- **看过未选**（`GET /api/triage/seen?limit=&cursor=&session=`）：01 顶部"看过未选 N 件 · 查看"进入子视图，按最近看到时间新到旧、游标续载；每件可「放回」（记一条 `restore` 行为事件，event_id 固定、重试幂等；只对仍在新货、在售的货有效，否则丢弃并提示「未能放回」）——该件重新出现在瀑布流（按上架时间的位置）——或直接「留下」。瀑布流读完、只剩看过未选的货时，空状态里有「看过未选 N 件 · 查看」。
- 页头（随瀑布流吸顶）："今日已留下 N 件 · 商品池共 M 件"：今日按柏林日期，N 为今天未撤销的人工留下（按件去重），M 为当前留下状态的件数（与 02 计数同口径）；写入后读 `GET /api/triage/header` 刷新。
- **行为记录**（表 `owner_events`，只在本机 `curio.db`，不上线、不进 GoodCustomer、不给客人页）：`seen`（卡片 ≥50% 在视口且持续 ≥0.6 秒，每件每次打开记一次）、`dwell`（卡片 ≥50% 在视口的一段停留秒数，离开视口、页面隐藏或打开详情时上报；短于 0.6 秒的不报，服务端截到 1–600）、`open`（从瀑布流打开详情）、`detail_dwell`（详情停留秒数，关闭时上报；页面隐藏时先报已停留的部分；截到 1–1800）、`restore`（看过未选里放回）。字段 `event_id`（客户端 UUID，主键，`INSERT OR IGNORE` 去重）、`type`、`item_id`、`client_ts`、`at`（服务器写入时间，权威）、`session_id`（每次打开 01 生成）、`params`（JSON，秒数在 `params.seconds`）。留下 / 去掉不在这里重复记（以 `triage_events` 为准）。上报 `POST /api/owner-events`（`{events:[…]}`，每批 ≤100 条，超过 400；令牌与 Origin 校验同其他写接口，令牌也可放在请求体 `token` 字段，供页面离开时 `sendBeacon` 补发）；未知类型、不存在的 `item_id`、字段不合规的单条丢弃，返回 `{stored, duplicate, dropped}`。页内每 5 秒或攒满 20 条发一批，失败的留在页内队列稍后重发；令牌失效（403）时停止重发并提示「请刷新本机审核页后重试」。
- 「撤销上一步」无时限、可连续多步（`triage.undo`，撤最近一次人工事件；货号保留不复用）。
- 「已自动移出 N 件 · 查看」：列表附理由与移出时间，「捞回」后该件回到瀑布流最前。
- 点图进入货详情：货号（已留下才有）、名称与标签（外画、仅后台 · Kleinanzeigen）、图库与放大层（图片取 `product_snapshots` 已下载的图，否则 `items.image`）、卖家原文（照录、标 `lang`）、来源与判断、费用档位（故事 12，可改，见下文"单件页、预估费用与币种"）、墨墨点评（表 `comments`）、图片替代文字（表 `alt_texts`，默认「标准名称 · 第 N 张 / 共 M 张」），点评与替代文字失焦自动保存。
- 「复查」按平台即时确认这一件是否在售（`curio_monitor.recheck_item`，与批量在售检查共用单件判定，计入当日在售检查预算，写 `status_check_events`，下架即 `items.status='gone'`；已发布的货同一事务入队隐藏，后台随即在后台线程触发一次限时 `sync.flush`，失败的下一轮采集重试）；预算用完、平台暂停或查询失败时显示「暂时无法确认，请稍后再试」。
- 接口：`GET /api/triage`、`/api/triage/feed`、`/api/triage/seen`、`/api/triage/header`、`/api/triage/auto-removed`、`/api/triage/item?id=`；`POST /api/owner-events`、`/api/triage/action`（keep / remove / rescue / undo）、`/api/triage/comment`、`/api/triage/alt`、`/api/triage/fee`、`/api/triage/recheck`。

### 后台 02 商品池、03 榜、预览与发布（CAP-7，故事 11；AD-5、AD-22、AD-28）

- **02 商品池**（`/#pool`，计数 = 已留下件数）：拣选状态"留下"的货，按分数高到低；每件显示货号、名称与标签、欧元价、来源平台、在售 / 已下架、在榜第几名。「加入榜」（榜满 12 件或 Kleinanzeigen、已下架、没有欧元标价的货为 `aria-disabled` 并写明原因）/「移出榜」、「移出商品池」（经 `triage.out_of_pool`：已发布的货同一事务入队隐藏；在榜的同时移出榜）；「标记售出」（需确认：先展开确认行，再点「确认标记售出」；经 `triage.mark_sold`：带售出标记的移出商品池，事件 reason 记"售出"，已发布的货同一事务入队隐藏、原因 sold，客人单件页墓碑"已售出"；在榜的同时移出榜；货详情里留下的货同样有此按钮）。移出、标记售出入队隐藏后，后台立即触发一次限时发送。下方「已移出商品池」可「放回商品池」（入队取消隐藏，不自动回榜）；标记售出的那件显示"已售出"，按钮为「撤销售出」（撤销该次标记售出、回到留下、入队取消隐藏）。点卡片打开货详情（主按钮为"加入榜 / 移出榜"）。
- **03 榜**（`/#rank`，计数 `N / 10`）：行 = 名次、缩略图、名称、货号与价格、右侧拖动把手 ≡。只在把手上长按（约 0.35 秒）后拖动，触摸与鼠标一致；把手获得焦点时 ↑/↓ 也能移动。点行其它区域选中并展开「上移 / 下移 / 移出榜 / 查看详情」。播报"已拿起，第 N 名""已放到第 N 名"。每次改动立即保存（`publish.set_rank`，表 `publish_rank`；已在榜上、之后下架或不再合格的货允许原样留着以便先排序再补位），失败时保留当前排序并显示"排序未保存 · 重试"。下架或不再合格的行去色、标"已下架"，并提示移出补位。
- **预览**：榜不足 12 件、排序尚未保存、或榜里有不能发布的货时「预览」为 `aria-disabled` 并写明原因。点「预览」→ Mac 用 `publish.candidate` 生成候选快照（与发布同样的入选、榜校验、字段白名单与图片：本机转码，必要时先转码；**不分配编号、不写发件箱、不改发布记录、不取发布锁**），存在后台进程内存里，再打开 `/preview/`。页面由 `server/preview.py` 用客人页同一份 `server/templates` 与 `guest.py` 里同一组渲染函数渲染（Mac 端不装 Jinja2：经 `server/.venv/bin/python server/preview.py` 子进程，标准输入 JSON → 标准输出 JSON），页内地址加 `/preview` 前缀，由后台路由提供：`/preview/`、`/preview/item/CO-…`、`/preview/legal`、`/preview/api/pool`（续载）、`/preview/img/<哈希>.jpg`（只限本次候选快照引用的本机转码图）、`/preview/static/…`（同一份 guest.css / guest.js，续载地址换成预览路由）。响应带与客人页相同的 CSP（只许本机资源），不连服务器、不记录任何行为；"退出登录"在预览里只回到预览首页。同一份预览的页面渲染一次后缓存。改排序、移出 / 放回商品池、改点评或替代文字、在 01 留下 / 去掉 / 捞回 / 撤销、以及发布，都会让预览作废（显示"预览已失效"）；生成途中被作废的预览不保存。同一时刻只生成一个预览。有货没有可用图片时，先列出这些货号，再由墨墨点"继续预览"。需先在 `server/` 下 `uv sync`。
- **发布**：只从预览顶部横条（反色）的「发布」发起：`POST /api/rank/publish` 带预览所见的榜与 `request_id`；须有当前预览且榜未变；后台在调用前即作废该预览，然后调用故事 10 的 `publish.publish`（发布锁、转码、入发件箱）：在发布锁内、入队的同一事务里再核对榜与内容指纹（候选快照的榜、商品池与全部字段的哈希，不含编号与时间），不同则拒绝"内容在预览后有变化，请重新预览"；`request_id` 的结果与快照在同一事务里记下，同一 `request_id` 的重试（含第一次还在进行时）只返回第一次的结果。入队后在后台线程触发一次 `sync.flush`。成功回到 03，显示"已发布 · HH:MM"（最近一次入队的发布时间）；发布时记下内容指纹（榜、入选货号与顺序、欧元价、点评与替代文字，表 `publishes.status_fp`）；当前指纹与之不同即显示"已发布 · HH:MM · 有未发布的修改"（改回原样则消失）。失败（另一个发布进行中、排序已变等）在横条下显示"未发布（原因） · 重试"，排序不动。
- 接口：`GET /api/pool`、`/api/rank`；`POST /api/rank`（`codes`、`request_id`）、`/api/pool/action`（`out_of_pool` / `back_to_pool`）、`/api/rank/preview`、`/api/rank/publish`。写接口沿用页面令牌、Origin 校验与 `request_id` 幂等（表 `triage_requests`）。命令行 `scripts/publish.py` 保留。

打开网页不会触发采集或模型调用。所有操作数据都在 `data/curio.db`（WAL 模式，连接等待 30 秒；备份请用 `VACUUM INTO`，不要直接复制正在写入的库）：items、price_history、price_drops、cases、screening_jobs、kv。可用 `uv run python scripts/import_desktop.py /旧版路径/curio.db` 只读导入 eBay/Kleinanzeigen 商品与审核记录（不含 Lottissimo）。重复导入不会重复创建记录。旧审核缺失的当时价格快照留空，不拿当前价冒充；原价格历史保留在旧库，不迁移。导入商品需后续重新核验在售状态。采集状态和错误摘要保存在 kv。

## 调度与接口调用预算（AD-27）

launchd 每 5 分钟唤醒一次 `curio.curio_monitor`，由 `curio/schedule.py` 按 kv 里的状态决定本轮做什么：

- **新货抓取**：基础间隔初始 30 分钟；每轮抓完按三平台合计新货调整——≥10 件减半、0 件 ×1.5、其余不变，夹在 5–60 分钟。柏林时间 01:00–06:59 实际间隔再 ×2（仍不超过 60）。唤醒时刻允许 30 秒抖动。
- **已发布货下架检查**（`check_published`，故事 13，CAP-2、AD-10）：独立节拍，每 15 分钟最多一轮，每次唤醒都判断是否到期（不受新货抓取间隔影响）。只查当前可能在客人页上的货（表 `published` 中已送达 + 待送达快照里、未隐藏、仍在售的 eBay / Etsy 货；Kleinanzeigen 不上线、不查），最久没查的在前。计入当天**在售检查**预算（`purpose=check`），在新货抓取与普通在售检查之前占用、不受每轮份额限制（优先级：复查 > 已发布货检查 > 新货 > 普通在售检查）。单件判定复用 `_check_one` / Etsy 批量判定：判下架即 `mark_gone`，同一事务入发件箱隐藏，本轮末尾的同步就把它发出去。某平台本轮全部判定完成（预算未耗尽、无请求失败、没有 Etsy 未返回的货、未超 240 秒——Etsy 每批之前也检查时限）才记该平台"最近成功时间"（kv `published_check_ok_at:<平台>`，UTC 带 Z）——这就是心跳；没有已发布货的平台视为"无需检查"、同样记为成功（这种轮次不算有工作，不写 `collection_runs`）。结果写 `published_check`：`items`、`attempted`、`active`、`gone`、`errors`、`unknown`、`paused`、`budget_skipped` 与 `platforms.<平台>`（`items`、`ok`、`status`：ok / nothing_published / unavailable / budget_exhausted / error / timeout）。
- **已发布货检查间隔（如实）：** 调度器只在 launchd 唤醒时判断，所以实际间隔 = 15 分钟向上取整到唤醒间隔。`scripts/install_hourly.py` 自故事 2 起就写的是每 300 秒唤醒（→ 15 分钟），本故事没有改它；但本机现装的任务仍是旧的 `StartInterval` 1800（**→ 实际 30 分钟一轮**），待墨墨授权后重装（`uv run python scripts/install_hourly.py`）。每轮结果里的 `published_check_interval_min`（以及 `launchd_interval_min`，读本机 `~/Library/LaunchAgents/com.curiobject.collect.plist`；读不出时按设计值 300 秒计算）显示当前真实值。
- **已发布货检查容量：** 每天预计调用数 = 每轮请求数（eBay 每件一次，Etsy 每 100 件一次）× 每天轮数（1440 ÷ 实际间隔）。超过该平台在售检查保底份额（每日预算 − 新货 60% 份额，默认 eBay 1800）时，当轮结果写 `published_check_warning`（中文说明），日志每天记一次（kv `published_capacity_warned`）。例：间隔 15 分钟（每天 96 轮）时 eBay 已发布超过 18 件即超出默认份额，部分轮次会因预算用完不算成功，客人页可能出现状态未更新提示——需调高 `api_budget` 或减少已发布件数。
- **在售检查**（`check_existing`）：独立节拍，每 30 分钟最多一轮，每轮最多 `status_check_limit` 件，在还有检查预算的平台间平分；每平台本轮份额再不超过「剩余检查预算 ÷ 到柏林午夜还剩的检查轮数」（至少 1），让检查份额撑满一整天。Etsy 按 100 个编号一批查。预算中途用完而没发出的检查不写检查记录、不算错误，计入 `budget_skipped`。
- 新货抓取后只有至少一个平台状态为 ok 时才调整间隔；全部出错、暂停或预算用完时间隔保持不变。
- 三者都没到期时本轮不采集（输出 `{"status":"idle",...}`）、不写 `collection_runs`，但仍做一次同步：flush 发件箱 + 发心跳（AD-10）。同一时间只有一轮（kv 锁）。
- **每日预算** `config/settings.yaml` 的 `api_budget`（默认 eBay 4500、Etsy 4500、Kleinanzeigen 1500）：每个外部 HTTP 请求（含 eBay OAuth——令牌缓存在 `data/ebay_oauth.json`（gitignore，权限 0600，不进数据库），到期前 60 秒内复用，遇 401 换一次令牌重试，不是每次唤醒都换；Etsy 换 key 写法与 429 重试、Kleinanzeigen 每次 curl）发出前计数，按柏林日期归零，计数存 kv `api_usage`。新货抓取最多用 60%，在售检查保底 40%。某平台用完当天跳过，结果写 `<平台>_status: budget_exhausted`（在售检查写入 `status_check_summary.budget_exhausted`）。
- 每轮结果（`collection_runs.result` 与 kv `last_collection_result`）含：`fetch_ran` / `published_check_ran` / `status_check_ran`、`published_check`、`published_check_interval_min`、`ebay_status` / `kleinanzeigen_status` / `etsy_status`（ok、error、paused、page_limit、budget_exhausted、disabled、not_due）、各平台 `*_new` 与 `new_total`、`api_usage`（当日各平台已用、上限、新货份额）、`fetch_interval_min`、`fetch_base_interval_min`、`night_mode`、`next_fetch_at`。另有：
  - 文字说明 `ebay`、`etsy`（出错原因）与 `kleinanzeigen_error`；只做在售检查的轮次 eBay 缺凭据时 `ebay_status` 为 error。
  - `status_check_summary`：`attempted`、`active`、`gone`、`errors`、`paused`、`unknown`（Etsy 未返回该货或无 state，保持原状态）、`budget_skipped`、`budget_exhausted`、`etsy_error`、`remaining_active`；整轮检查失败时写 `status_checks`。
  - `collection_runs.status`：三平台状态都是 ok / disabled / not_due、且已发布货检查与在售检查都没有 errors（在售检查也没有 `etsy_error`）时为 ok，否则 partial。

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
- 每件新货调用一次 judge；仅标价或币种变化重判。规则排除的也保存原因。更改规则配置不会自动重判历史货，需运行 `scripts/rejudge.py`。
- 拍卖总价 = 标价 × (1 + auction_fee_rate)，其他总价=标价。这里只包含配置手续费，不含未知运费/税费；竞拍标价是当前出价，不是最终成交价。

## 规则、模型与审核

`config/price_rules.yaml` 和 `config/slow_sellers.yaml` 初始为空，文件注释有格式。没有把未经确认的旧价格线当作正式配置；旧 judge 的默认调用行为保留，本流程禁用旧硬编码价格阈值，使用 YAML。原有文字分类规则继续使用。

破损/修复线索默认不要（规则与否定说法见下文「自动筛除与捞回」）。`cheap_damaged_max: null` 关闭便宜破损例外；设欧元阈值后，低于阈值改为询市。只基于商品文本，不能验证图片品相或真伪。

want / probe 进入 cheap / deep 记录。`daily_budget: 0` 时不调用任何 provider；即使改为正数，也只允许本地 NoopProvider，结果原样返回。没有付费模型 SDK，没有自动出价、联系卖家功能。

审核默认显示未审核的要/询市，新货优先；被规则排除页可以改判。点击要/询市/不要后可填原因和心理价，不要必须写原因。保存后从待审核中移除；每次判断追加 cases，保留当时的规则原因、标价、总价和规则版本。重复请求幂等，价格更新后的旧表单会被拒绝，避免错配快照。已审核商品改价不会自动重新进入待审核。

## 拣选状态与货号（AD-26、AD-7）

拣选状态只经 `curio/triage.py` 写入（表 `triage`、追加式事件表 `triage_events`、货号表 `item_codes`，由 `Store` 建表）：

- 状态：`new` 新货、`kept` 留下、`removed` 去掉、`auto_removed` 自动移出、`out_of_pool` 移出商品池。标记售出（故事 13）不新增状态：`kept` → `out_of_pool` 并置 `triage.sold=1`（独立字段，不复用 `items.status`），事件 action `mark_sold`、reason"售出"；其余任何转换清除该标记，撤销按被撤销动作之前的有效动作恢复。
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

## 自动筛除与捞回（CAP-3，AD-15、AD-26）

纯规则，不调用 AI。判断入口仍只有 `rules.evaluate`，顺序：屏蔽词 → 硬过滤 → 破损/修复 → 滞销 → 价格规则。

**移出类别与理由：**

| 类别 | 理由写法 |
|---|---|
| 屏蔽词 | `屏蔽词：<词>（标题/描述）` |
| 硬过滤（招聘、房产、求购、纯白瓷、纪念盘、灯具、仿品等） | `硬过滤：<类别>`，如 `硬过滤：年度纪念盘/收藏盘` |
| 破损或修复 | `破损或修复：<类别>（"<原文片段>"）`，类别为 破损 / 修复 / 磕口 / 裂纹 / 需修理，如 `破损或修复：裂纹（"kleiner Haarriss am Rand"）` |
| 滞销、价格规则 | `config/slow_sellers.yaml`、`config/price_rules.yaml` 里写的理由 |

纪念盘（只看标题）另认 Gedenkteller、Gedenktasse、Gedenkbecher、Erinnerungsteller、Erinnerungstasse、commemorative。

**破损的否定规则**（`rules.damage_reason`）：在标题、描述、品相（condition）字段里找破损词（德文 defekt、beschädigt/Beschädigung、restauriert、repariert、geklebt、Abplatzer、bestoßen、Chip、Macken、Fehlstelle、Absplitterung、Flohbiss、Haarriss、Riss；英文 chip/chipped、crack、broken、repaired、restored；中文 破损、裂纹、缺口、修复）。句子先按句号、问号、感叹号以及 aber、allerdings、jedoch、nur、außer、but、except 断开，否定与排除**只看命中所在的逗号/分号短句**：
- 本短句里命中词左边有否定词 keine/kein…/ohne/nicht/nichts/nirgends/weder/no/without/not/never/free of（卖家漏空格的 `istnicht` 也算）；
- 命中词右边紧跟后置否定：`Beschädigungen: keine`、`Risse: nein`、`… none`、`ist/sind/wurde nicht`；
- 否定只经**纯破损词列表**跨逗号延续（否定词后面须是破损类词，如 Chips、Risse、Splits、Verfärbungen；Gebrauchsspuren 不算）：`keine Chips, Risse oder Abplatzer` 整串都算否定；但 `Neuwertig, keine Gebrauchsspuren, Haarriss am Boden`、`ohne Deckel, mit Chip am Fuß` 仍移出；`keine Chips, aber ein Haarriss` 也移出；
- 本短句命中词左侧（及右侧紧邻约 20 个字符）有包装（Karton、Verpackung、OVP）或保修、运输（Garantie、Gewährleistung、Haftung、Versand、Rücknahme）说法的命中不算：`Karton beschädigt, Tasse einwandfrei` 不移出，`Versand im Karton, Tasse hat Haarriss` 移出。

`product_v2` 用的 `rules.damage_statements`（品相陈述）保持原来按整句判断的行为不变。

金彩磨损（Goldabrieb 等）只在品相陈述里记录，不移出。便宜破损例外 `cheap_damaged_max` 不变。

**外画标签：** 标题或描述里有 Hausmalerei、Hausmaler、Außenmalerei/Aussenmalerei、nachträglich bemalt/staffiert、übermalt、Fremdbemalung/fremdbemalt/Fremdmalerei、overpaint/overpainted/over-painted、outside decorated、later decorated、decorated outside the factory（大小写、ß/ss、ä/ae 不敏感；前面有 nicht/keine 等或后面紧跟 nein/keine/no/not 的不算）时，只打标签、不移出、不改判：`standard_name` 结果 `overpaint: true`，标准名称在品牌后加「外画」（`Meissen梅森 外画 盘`），`std_key` 末尾追加 `|外画`，所以 `similar_prices` 只与外画同类比价；表 `std_names` 增列 `overpaint`（0/1，已有库启动时自动补列）。

**已自动移出列表与捞回：** 系统落位事件（`ingest` / `rejudge`）在 `triage_events.reason` 记下当时的 `agent_reason` 快照；列表显示的是该货当前的 `agent_reason`（重判后即最新理由）。

```python
triage.auto_removed(c, limit=100, offset=0)   # 当前 auto_removed 的货：item_id、title、platform、price（标价）、total_price、currency、url、status、reason、removed_at，按移出时间新到旧；limit<1 或 offset<0 抛 ValueError
triage.rescue(c, item_id)                      # 捞回：回新货队列最前，写人工事件
```

**全量重判（幂等）：** `Store.rejudge_all(config)` 与屏蔽词全量重判共用 `store.rejudge` 循环：结论或理由变了的货更新 `agent_decision` / `agent_reason`、`rule_version+1`，拣选状态只对没有人工事件的货在 新货 / 自动移出 间移动（AD-26）；人工决定、`cases`、`market_decisions` 不动。判断与标准名称先在写锁外算好，再在一个短写事务里落库，期间被采集改过（标题、描述、品相、价格、币种、`rule_version` 变化）的货在事务里当场重算。随后刷新标准名称（含外画键），只改写有差异的行；单件命名出错计入 `std_failed`、不中断；知识库状态不是 ok 时不刷新标准名称，报告 `std_skipped`。

**部署判断或命名规则的改动后（改 `rules.py` / `judge.py` 的词表、外画、标准名称逻辑，或知识库大改），必须运行一次 `scripts/rejudge.py`**：采集只在新货或改价改文时重判，存量不会自动更新。

```sh
sqlite3 data/curio.db ".backup data/backups/curio-before-rejudge-<UTC时间>.db"
uv run python scripts/rejudge.py   # 打印 checked / changed / moved / transitions（旧类别（结论）→新类别（结论）：件数）/ std_written / 各状态计数 / 外画件数
```

2026-11-03 真实库重判结果：checked 22122；首次改判 229——35 件旧规则判为破损的货不再按破损移出（否定说法；34 件改为询市/要，1 件因无品牌信号仍移出），61 件新判为破损或修复移出（新增词、英文词），106 件只是理由改为带类别与片段，1 件 Gedenkteller 按纪念盘移出，其余 26 件为旧规则版本遗留的询市理由更新；补上 `istnicht` 否定后补跑改判 1 件（放回新货）。评审修复（否定只看本短句、后置否定、英文 not/never/free of、外画词边界与后置否定）后两次补跑：改判 4 件 + 2 件（5 件新判破损移出，1 件放回），外画由 15 件变为 13 件（同一件货的两条“übermalter Ausbesserung”不再算外画），备份 `data/backups/curio-before-rejudge-v2-20261103T081430Z.db`、`curio-before-rejudge-v3-20261103T081603Z.db`。共移动拣选状态 103 件；人工决定过的 195 件状态与货号全部不变，`cases` 180、`market_decisions` 15 不变；外画 13 件。之后再运行改判 0、标准名称改写 0。备份在 `data/backups/curio-before-rejudge-20261103T080404Z.db`。

## 可能要分数（CAP-4，AD-15、AD-16、AD-20、AD-26）

每件拣选状态为 `new` 的货在表 `scores` 里至多一行：`item_id`（主键）、`score`（0–100 整数）、`source`（`rule` / `ai`）、`model`、`reason`、`rule_version`、`scored_at`。`scores.rule_version` 与 `items.rule_version` 不同（货改价改文或重判后）即视为过期。自动移出、人工处理过的货不必有分。

**两种分数来源：**

- **规则基线分**（`rules.baseline_score(item, std)`，始终可算）：判断想要 60 / 询市 40 / 不要（捞回的）15；标准名称有图案 +12、有器型 +8、有功能 +6；卖家等级 1. Wahl +6、2. Wahl −10、3./4. Wahl −15（"1.WAHL"、"I.Wahl"、"erste Wahl" 等写法都认）；外画只在理由里标明、不减分；结果夹在 0–100，理由如「判断想要；有图案、器型；1. Wahl」。入库、重判（`rejudge` / `rejudge_all`）时随判断写入；未过期的 AI 分不会被基线分覆盖，过期的会先换成基线分、下轮再由 AI 重算。
- **AI 分**（`curio/ai.py`，唯一的 Anthropic 调用点）：`requests` 直接 POST `https://api.anthropic.com/v1/messages`，头 `anthropic-version: 2026-10-21`，模型 `claude-haiku-5-5`，不发 `temperature` / `top_p` / `top_k`。一次最多 20 件（item_id、标题、描述前 600 字、欧元价、标准名称、外画标签、基线分），附墨墨最近最多 30 件「留下」与 30 件「去掉」的标题与理由作偏好示例；要求只回 JSON 数组 `[{item_id, score, reason}]`。返回解析失败、缺件、分数越界的那几件保留原分（记警告），不中断。AI 只写 `scores` 与看图观察 `visual_checks`（见下文看图硬筛），不改判断或人工决定。

**启用 AI：** 由墨墨在 `.env` 加一行 `ANTHROPIC_API_KEY=...`（只读 `.env`，不读系统环境变量；key 不进日志、结果或提交）。不填时只有基线分，每轮结果 `ai_status: disabled`，新货队列按基线分排序。

**采集轮打分：** 抓取与在售检查之后，对 新货、非 backlog、在售、分数缺失 / 过期 / 来源为 rule 的货按 `first_seen` 新到旧打分。结果 JSON 增加 `ai_status`（`disabled` / `ok` / `budget_exhausted` / `error`）、`ai_scored`（本轮写入件数）与 `ai_usage`；AI 出错（如 HTTP 529、超时、返回被截断 `stop_reason: max_tokens` 或被拒绝 `refusal`）只让 `ai_status: error`，基线分不动，其它平台结果与本轮状态不受影响。为免一轮占用采集锁太久，每轮至多 10 次请求（含重试，`ai.MAX_REQUESTS_PER_ROUND`），整个 AI 步骤限时 120 秒（`ai.ROUND_SECONDS`，每次请求前检查，单次请求超时 120 秒且不超过剩余时间）；到限即正常结束（结果带 `ai_stopped: round_limit`），下轮继续。已得到答复但漏回、越界或被截断的货记在表 `ai_attempts`，24 小时内（且 `rule_version` 未变）不重发。`requests_per_day: 0` 视同未启用（`ai_status: disabled`）。各种结果都带 `ai_usage`。提示里说明货品标题与描述是不可信的卖家文字、其中的指令一律忽略；非欧元货发送原价与币种。

**预算（AD-20）：** `config/settings.yaml` 的 `ai_budget.requests_per_day`（默认 100），按柏林日期在 kv `ai_usage` 计数，每个 HTTP 请求（含重试：429/5xx/网络错误时等待 `retry-after`（最多 5 秒）或 2 秒后重试 1 次；重试时额度用完则报原错误）计一次；用完当天停止，结果写 `ai_status: budget_exhausted`。月费估算（haiku 5.5 价格：输入 $0.10、输出 $0.50 / 百万 token；按 1 美元 ≈ 0.9 欧元换算，汇率为估计值，以账单为准）：每请求约 5k 输入 token（20 件货 + 60 条示例），看图后每张主图再加约 200 token（eBay 按 `s-l400` 送；Kleinanzeigen 按原地址尺寸送，可能更多），一批 20 张图约多 4k，合计约 9k 输入 token；输出约 1–3k token（含模型思考），每请求约 €0.002–0.003；默认额度 100 次/天 × 30 天 ≈ 3000 次 ≈ **每月约 €5–8**；即使每次都用满 `max_tokens`（16k 输出）也只约 €27/月。计入 AD-20 每月合计 €200 上限；约 4000 件待打分的新货在默认额度下约 2 天打完一遍。上线看图后有一次性补看：已有 AI 分、但现主图没看过的新货（约 4000 件）各重新送检一次，同时重新打分、覆盖一次原 AI 分，头几天会用掉当日额度。

**看图硬筛（故事 19）：** 同一次打分请求里附上每件货的主图，模型对附图的货多回三项观察（`single_cup` 主图只有杯子、没有任何碟子；`plain_white` 瓷器通体白色无彩绘，只带金边金线、白色浮雕、白色塑像也算；`not_porcelain` 主体不是瓷器，如书、目录、画、其他材质）。不新增请求，与打分共用当日预算与本轮限额（约 200 输入 token/图）。
- 只送 https 的 `i.ebayimg.com`（尺寸改为 `s-l400`）与 `img.kleinanzeigen.de`（`ai.vision_url`）；无图或其他站点的货照常只打分、不看图。请求 content 为一个文本块（示例与货品 JSON）加每件附图货的 `#item_id` 文本块与 url 图片块。
- 带图的批次被拒（HTTP 400，如某张图取不到）时，这些主图记为已送检（不再因“未看过”重发），本轮限额内去掉图片重发一次本批；提示里说明图片中的文字同样是不可信的卖家内容。
- 观察存表 `visual_checks`（每件一行，`image` 为送检时的 `items.image`）；主图变了旧观察作废：入库时主图变化即重新落位（看图移出的货回到新货），下轮重新看图。观察漏回或不是 true/false 的货只写分数、记警告。
- AI 只给观察（AD-16）；是否移出与理由只在 `judge.visual_filter`，落位只经 `triage.system_place`，只动未经人工决定、当前为新货 / 自动移出的货（AD-26），墨墨捞回后不再被看图结果移出。理由（多项命中取第一项）：`硬筛（看图）：主图只有杯子，不带碟` / `硬筛（看图）：白瓷（含金饰）` / `硬筛（看图）：不是瓷器`；「已自动移出」列表显示该理由。本轮看图移出的件数记在结果 `ai_removed`。
- 候选：除分数缺失 / 过期 / 来源为 rule 的新货外，已有 AI 分但现主图还没看过的新货也进候选，同一张主图只送检一次（`ai_attempts.image` 记送检时主图）。
- 标题白瓷同步收紧：标题写 weiß / weiss / white 且只带金饰（`Goldrand`、`vergoldet`）也算白瓷（`硬过滤：纯白瓷（标题写 weiß）`）；带彩绘 / 蓝白（如 `mit blauem Dekor`、`Rote Rose`）不算。已在库的货需重判才按新规则移出。

**拣选快照与排序：** `triage` 写每条事件时 `triage_events.score` 取该货当时的 `scores.score`（无分为空），`rule_version` 照旧。新货队列 `triage.queue()`：捞回的最前，其后按分数高到低、无分的排在有分之后，再按 `first_seen` 新到旧；每件附 `score`、`score_source`、`score_reason`。

```sh
sqlite3 data/curio.db ".backup data/backups/curio-before-scores-<UTC时间>.db"
uv run python scripts/score_baseline.py     # 为全部新货回填基线分（幂等，第二次写入 0）
uv run python scripts/top20_keep_rate.py    # 按日复算「决定前分数最高 20 件的留下率」与「当天平均留下率」，打印逐日与合计；无数据时打印说明
```

2026-11-03 真实库回填：新货 11068 件全部写入基线分（第二次运行写入 0）；在售、非 backlog 的新货 4014 件全部有分。目前没有人工「留下」记录（只有 195 条迁移来的「去掉」，当时无分），复算脚本只打印说明。

## 每5分钟唤醒的本机自动采集（launchd）

只安装一个定时任务 `com.curiobject.collect`：每 300 秒唤醒一次 `curio.curio_monitor`（**本机现装版本仍是 1800 秒，已发布货检查因此实际 30 分钟一轮；重装需墨墨授权**），由调度器决定是否真正抓取（见上文「调度与接口调用预算」），由当前 Mac 用户的 launchd 执行，plist 指向本仓库 `.venv/bin/python`，不依赖 Codex 对话，不调用 AI。日志写入 `data/logs/collect.log` 与 `data/logs/collect-error.log`。

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

## 香港服务器与同步通道（故事 8，AD-3、AD-19、AD-21、AD-23）

香港服务器与域名还没买（墨墨 2026-11-03 决定）。本仓库只写好服务器骨架、部署与备份脚本，并在本机测试；**连接真实 VPS（部署、备份）须墨墨另行授权后才执行**。

### 组成

- `server/`：FastAPI + SQLite 服务器（单独的 `server/pyproject.toml` 与 `server/uv.lock`，Mac 端依赖不变）。
  - `GET /healthz`：无鉴权，只返回 `{"status":"ok","version":…}`。
  - `POST /admin/events`：`Authorization: Bearer <管理密钥>`，否则 401 且不写入；按 `contract/schemas/` 校验信封与载荷，不认识的 `schema_version` / `kind` 或不合 Schema 返回 400（中文 `detail`）；按 `event_id` 去重——重复请求返回第一次的确认、不重复处理（同一 `event_id` 内容不同返回 409）。
  - 不提供 `/docs`、`/openapi.json`；SQLite 开 WAL 与 `busy_timeout`；只监听 `127.0.0.1:8790`，由 Caddy 反代并自动签 HTTPS 证书（`server/Caddyfile`、`server/curiobject.service`）。
  - 设置只来自服务器环境文件 `/etc/curiobject/server.env`（权限 0600）：`CURIO_ADMIN_TOKEN`、`CURIO_SERVER_DB`（默认 `server/data/server.db`，生产用 `/var/lib/curiobject/server.db`）。
- `contract/schemas/`：`envelope.v1.schema.json`（`schema_version`、`event_id`、`kind`、`created_at`、`payload`）与 `ping.v1.schema.json`。两端用同一份文件校验。
- `curio/sync/`：Mac 上唯一调用服务器的代码。

### 发件箱（`curio.db` 表 `outbox`）

- 列：`seq`（自增，发送顺序）、`event_id`（UUID，唯一）、`kind`、`payload`、`created_at`（UTC，带 Z，毫秒）、`attempts`、`last_error`、`sent_at`。
- 写入只用 `curio.sync.enqueue(c, kind, payload)`：先按契约校验（坏载荷抛错、不入队），与触发它的状态变更同一事务、由调用方提交。
- `curio.sync.flush()` 按 `seq` 逐条发送；遇失败即停、不跳过，`attempts` 加一、`last_error` 记中文原因，下次从同一条重试；服务器 2xx 且回显 `event_id` 才写 `sent_at`。
- 暂时失败（网络、超时、5xx、401）下轮自动重试。永久失败（服务器 400 / 409 / 413、本机契约校验不过、库里载荷 JSON 坏掉）把该条标 `failed_at` 并停在这里——**不跳过**（顺序要紧），之后每轮都是 `sync_status: blocked`（附 `sync_blocked_seq`），并立刻发一条点名该条的系统通知。处理办法见下方 `scripts/outbox.py`。
- 采集轮在 AI 打分之后、归档之前调用一次 `sync.run`：flush（墙钟限时 30 秒，含慢速响应；没发完的下轮继续）再发一次心跳；空闲轮（什么都不到期）同样调用。结果写入 `sync_status`：`disabled`（`.env` 未配置服务器，发件箱保留，也不发心跳）、`ok`、`error`（附 `sync_error`、`sync_fail_streak`）、`blocked`、`timeout`，另有 `sync_sent`、`sync_pending`、`heartbeat_status`（ok / error，附 `heartbeat_error`）。
- **心跳（AD-10，`heartbeat.v1`）：** 每轮直接 POST `/admin/events`（`kind: heartbeat`，限时 10 秒），**不进发件箱**（它是状态不是事件，只有最新的有意义）。载荷：`sent_at` 与 eBay、Etsy 各自已发布货下架检查最近成功时间；当前没有可能在线的货的平台报发送时间（无需检查，同时把该平台的成功时间更新为此刻，之后再有已发布货时不沿用旧时间），有货却从未成功过的平台不带时间；还有未送达的隐藏 / 取消隐藏时，各平台报的时间不晚于其中最早一条的入队时间（下架已判定、客人页还没更新，久了照样提示）。心跳构造或发送中的意外只记 `heartbeat_status: error`，不影响 flush 结果。服务器要求 ebay、etsy 各一项、成功时间不晚于发送时间；已存心跳的 `sent_at` 比服务器现在还晚时（Mac 时钟曾快、已校正）以后到的为准。心跳失败计入同步连续失败（与 flush 失败合并、一轮最多计一次）。服务器只留 `sent_at` 最新的一份、不存入 `events` 表；取各平台最早的成功时间，距今（Mac 两个时间之差 + 服务器收到后经过的时间，不依赖两端时钟一致）超过 2 小时、或发布过快照却从未收到心跳 / 某平台没有成功时间时，客人首页与单件页页头下方显示 `inline-message`"部分货品状态可能未更新，下单前请先问墨墨。"（页面加载时 polite 读一次），其余照常；从未发布过快照时不显示；Mac 预览从不显示。
- 连续 3 次同步失败（flush 或心跳，含同步模块的意外异常）时发一次 macOS 系统通知（`osascript`）；只有 `ok` 清零，`timeout` 与 `blocked` 不改计数。
- 已送达的发件箱记录与服务器 `events` 表目前不清理，清理策略留到快照故事一并定。

```sh
uv run python scripts/outbox.py list               # 未送达的记录（--all 含已送达）
uv run python scripts/outbox.py ping "部署后测试"   # 入队一条 ping，下一轮采集送出
uv run python scripts/outbox.py requeue <seq>      # 修好服务器或契约后，清除拒收标记重发
uv run python scripts/outbox.py drop <seq> --yes   # 放弃一条卡住的记录（不可撤销），后面的记录才会继续发
```

```sh
sqlite3 data/curio.db "SELECT seq,kind,attempts,last_error,sent_at FROM outbox ORDER BY seq DESC LIMIT 10"
```

### Mac `.env`（密钥不进代码、测试、日志、提交）

```sh
CURIO_SERVER_URL=https://<域名>          # 必须 https（本机测试可用 http://127.0.0.1:端口）
CURIO_ADMIN_TOKEN=<管理密钥>             # 与服务器 /etc/curiobject/server.env 中相同
CURIO_SSH_HOST=deploy@<服务器 IP 或域名>  # 部署与备份用
# 选填（括号内为默认值）：
# CURIO_REMOTE_DIR（/opt/curiobject）        服务器上的仓库目录
# CURIO_REMOTE_DB（/var/lib/curiobject/server.db）  服务器库，备份用
# CURIO_REMOTE_UV（uv）                      服务器上 uv 的路径（非交互 SSH 的 PATH 里找不到时填绝对路径）
# CURIO_REMOTE_PYTHON（<CURIO_REMOTE_DIR>/server/.venv/bin/python）  备份时在服务器上跑 VACUUM INTO 的 Python
```

未填 `CURIO_SERVER_URL` 时同步为 `disabled`；只填地址不填密钥、或地址不是 https 时同样不发送，并在 `sync_error` 说明原因。

### 服务器准备清单（墨墨操作）

1. 买香港 VPS（实测 CN2 线路，≥2 GB 内存、≥10 Mbit/s；见 open-items 第 7 条）与域名，域名 A 记录指向 VPS。
2. 在 Mac 生成 SSH 密钥（已有可跳过：`ssh-keygen -t ed25519`），把公钥加到 VPS 的部署用户（如 `deploy`）`~/.ssh/authorized_keys`；关闭密码登录。
3. 生成管理密钥：`python3 -c "import secrets;print(secrets.token_urlsafe(32))"`，填进 Mac `.env` 的 `CURIO_ADMIN_TOKEN` 与服务器 `/etc/curiobject/server.env`。
4. 服务器一次性准备（root）：安装 uv、rsync、curl 与 Caddy 2.11.x；`useradd --system curiobject`，把 `deploy` 加入 `curiobject` 组；`mkdir -p /opt/curiobject /etc/curiobject`，`/opt/curiobject` 归 `deploy` 且其他用户可读（服务以 `curiobject` 用户运行，要能读代码与 venv）。部署脚本让 uv 把 Python 3.13 装在 `/opt/curiobject/python`（`UV_PYTHON_INSTALL_DIR`），不在 `/home` 下，所以服务的 `ProtectHome=true` 不会挡住解释器。`/var/lib/curiobject` 不用手建：systemd 按 `StateDirectory=curiobject`、`StateDirectoryMode=0770` 建成 `curiobject:curiobject`、组可读写，服务 `UMask=0007` 让库与 WAL 文件也组可读写，`deploy` 因在 `curiobject` 组里才能做备份。写 `/etc/curiobject/server.env`（`CURIO_ADMIN_TOKEN=…`、`CURIO_SERVER_DB=/var/lib/curiobject/server.db`，0600，归 root）；允许 `deploy` 免密 `sudo systemctl restart curiobject`。
5. 首次部署（见下）后：`cp /opt/curiobject/server/curiobject.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now curiobject`；`cp /opt/curiobject/server/Caddyfile /etc/caddy/Caddyfile`，在 Caddy 环境里设 `CURIO_DOMAIN=<域名>`，`systemctl reload caddy`。防火墙只开 22、80、443。

### 部署与备份（须墨墨授权后才对真实 VPS 执行）

```sh
uv run python scripts/deploy_server.py --dry-run   # 只打印命令
uv run python scripts/deploy_server.py             # rsync server/（不含 .venv、data）与 contract/ → uv sync --frozen → 重启 → 健康检查（核对版本）
uv run python scripts/backup_server.py             # 服务器上 mktemp -d（0700）里 VACUUM INTO → 拉回 data/server-backups/server-<UTC>.db → 本机 integrity_check → 删除服务器临时目录与超过 30 天的备份
```

- ssh / rsync 一律 `BatchMode=yes`（不弹密码或主机指纹提示，首次连接前先手动 `ssh deploy@…` 确认指纹）、`ConnectTimeout=15`，每步另有总时限；任一步失败或超时都非零退出。
- 部署后先在服务器本机查 `/healthz`，填了 `CURIO_SERVER_URL` 时再经 HTTPS 查一次，两处报告的版本都须等于 `server/app.py` 的 `VERSION`。
- 端到端验证：部署后在 Mac 执行 `uv run python scripts/outbox.py ping "部署后测试"`，再跑一轮 `uv run python -m curio.curio_monitor`（或等下一轮），输出里 `sync_status: ok`、`scripts/outbox.py list` 显示无未送达记录即通道已通。
- 每日备份可在买好服务器后另加 launchd 任务（本故事不安装）。

### 本机运行服务器

```sh
cd server && uv sync && CURIO_ADMIN_TOKEN=<本机测试密钥> uv run python app.py --port 8790
```

## 客人注册与登录（故事 9，CAP-13；AD-8、AD-18、AD-24）

服务器（`server/`）自己产生账号；本故事只在本机运行与测试，不上线、不连真实邮件服务。登录后暂时只有空首页占位（首页与单件页在故事 10）。

### 组成

- `server/accounts.py`：账号、会话、限频、重设令牌、同意记录、邀请码的表与逻辑。
- `server/guest.py` + `server/templates/` + `server/static/`：服务器端渲染的客人页面；表单是普通 POST，不跑 JS 也能提交，`guest.js` 只做增强（同意框切换注册按钮、注册/登录不跳页切换、去看说明页再回来时保留已填内容）。
- `server/mailer.py`：可替换的发信接口；目前只有 `FileMailer`（写 `.eml` 文件）。
- `server/legal/terms.md`：用户协议与隐私说明**草稿**，文件头 `version:` 即同意记录里的版本号。**上线前必须由墨墨确认并请懂法律的人审阅后替换**（替换时改版本号）。

### 账号规则

- 注册：邀请码 → 用户名 → 密码 → 微信名/称呼 → 邮箱（选填，只用于找回）→ 同意《用户协议》《隐私政策》（数据存放在中国境外）。不收手机号、不发短信。注册成功即登录，回到原来要打开的页面（含 `/item/CO-NNNN`）。
- 用户名 2–20 个字符（中文、字母、数字、下划线；全角自动转半角），不区分大小写唯一；密码 8–128 位；微信名/称呼 1–30 字；邮箱按格式校验。
- 密码与邀请码只存 Argon2id 哈希（m=19 MiB、t=2、p=1）；会话令牌、重设令牌只存 SHA-256；限频计数的键（IP、用户名）与 `events` 表里的邀请码副本用 HMAC-SHA256（密钥 `CURIO_SERVER_SECRET`，不设时在服务器库所在目录生成一次 `server_secret`，0600；换密钥只会让 15 分钟内的限频计数清零）。明文不进库、不进日志。
- 会话：随机令牌放在 `curio_session` Cookie（HttpOnly、SameSite=Lax、Secure、90 天，到期不续）。退出（在任一设备上）、重设密码、停用都使该账号全部会话失效；失效后再打开任何页面显示登录表单（用户名预填）与"登录已过期，请重新登录。"
- 未登录访问任何客人路径都只显示注册/登录页（完成后只会回到 `/` 或 `/item/CO-NNNN`，其它一律回首页）；免登录的只有注册/登录/找回/设置新密码页、`/legal` 说明页、`/static/`、`/healthz`。
- 同意记录（表 `consents`，只追加）：注册时写两行——`terms_privacy` 与 `cross_border_storage`（境外存储单独同意），各带版本号与 UTC 时间。`terms.md` 的版本号改变后，客人登录后先看到"用户协议已更新"确认页（同一个同意框），同意后再追加两行、继续原来的页面。登录后页头与页脚都有"用户协议与隐私说明"。
- 停用账号（Mac 指令 `disable_account`，后台 06 客人发起，见下文"后台 06 客人"）只看到"账号已停用"页与"退出登录"；找回密码显示"此账号已停用，请在微信中联系墨墨。"，不发信、旧重设链接失效。
- 无邮箱客人的密码重置与停用账号由墨墨在后台 06 客人操作（故事 14）。停用不可恢复（不做启用）；删除账号与撤回同意待协议定稿，目前仍由墨墨手动处理（说明页第七条如此写明）。

### 限频（窗口 15 分钟，按 IP 与用户名分别计数，只存哈希）

| 动作 | 按 IP | 按用户名 |
| --- | --- | --- |
| 登录 | 失败 20 次 | 失败 5 次（第 6 次起即使密码正确也拒绝；成功登录清零） |
| 注册 | 每次提交 20 次 | 失败 5 次（邀请码错、用户名已被使用、字段不合规） |
| 找回 | 每次提交 20 次 | 每次提交 5 次 |

超出时显示"尝试次数过多，请稍后再试。"及大约还需等几分钟；用户名不存在时与存在时表现相同（找回除外：有邮箱"重设链接已发送"、无邮箱"此账号未填写邮箱，请在微信中联系墨墨重置密码。"；不存在的用户名也显示"已发送"）。客户端 IP：经 Caddy 反代时由 uvicorn 按 `X-Forwarded-For` 取得。

### 邀请码下发（Mac → 服务器）

```sh
uv run python scripts/outbox.py invite MOMO2026    # 4–32 位字母、数字、_、-；不区分大小写
```

入队一条 `set_invite_code` 指令（`command_id` + `target_seq`，序号存在 `curio.db` 的 `kv` 表 `cmd_seq:invite`），下一轮采集的 flush 送到 `/admin/events`。服务器只存新码的哈希，确认后新码生效、旧码立即失效；同一 `command_id` 只执行一次（重放返回第一次的结果）；`target_seq` 必须是 1 到 2^63-1 的整数，且**严格大于**已生效的最高序号才生效，否则确认里 `applied: false`；`events` 表里的邀请码替换为 HMAC。`scripts/outbox.py list` 只显示邀请码首尾各一位。服务器从未收到过邀请码时注册关闭（"注册暂未开放"）。Mac 发件箱 `outbox.payload` 里保留明文（Mac 是邀请码的唯一写入方，故事 14 的后台 06 要显示当前码）。

**`applied: false` 的处理（例如 Mac 库从备份恢复后计数器回退）：** Mac 把该条记为被拒收（`last_error`："服务器已有更新的邀请码命令，未生效"），同步停住并发通知。恢复：

```sh
uv run python scripts/outbox.py drop <seq> --yes                             # 放弃卡住的那条
uv run python scripts/outbox.py invite <新码> --seq <比服务器上更大的序号>   # 如上次成功的序号 + 100；序号只会往上抬
```

### 找回密码与发信配置

- 有邮箱的账号：生成一次性重设令牌（1 小时有效），邮件里是 `<CURIO_PUBLIC_URL>/reset?token=…`；设置新密码后该账号全部会话失效，回到登录并提示"新密码已保存，请重新登录。"
- 服务器环境变量：`CURIO_PUBLIC_URL`（生产必填，如 `https://<域名>`，末尾 `/` 会去掉；未设置且 Cookie 为 Secure 时**不发任何重设邮件**，页面显示"暂时无法发送重设邮件，请在微信中联系墨墨重置密码。"并记警告，因为 Host 头可被伪造；只有本机 http 测试 `CURIO_COOKIE_SECURE=0` 时才用请求地址）、`CURIO_SERVER_SECRET`（HMAC 密钥，至少 16 字节，选填）、`CURIO_COOKIE_SECURE`（默认 1；只有本机 http 测试设 0）、`CURIO_MAILER`（目前只有 `file`）、`CURIO_MAIL_DIR`（默认 `server/data/mail/`，目录 0700、文件 0600）。
- 发信服务在购买服务器时再选（计入 AD-20 成本表）：实现一个带 `send(to, subject, body)` 的类，在 `mailer.from_env()` 里按 `CURIO_MAILER` 选择即可，其它代码不用改。

### 本机试用

```sh
cd server && CURIO_ADMIN_TOKEN=<本机测试密钥> CURIO_COOKIE_SECURE=0 CURIO_PUBLIC_URL=http://127.0.0.1:8790 uv run python app.py --port 8790
# 另一个终端：Mac .env 设 CURIO_SERVER_URL=http://127.0.0.1:8790 与同一密钥，然后
uv run python scripts/outbox.py invite MOMO2026 && uv run python -c "from curio.store import Store;from curio import sync;print(sync.flush(Store('data/curio.db')))"
# 浏览器打开 http://127.0.0.1:8790/ ；找回密码的邮件在 server/data/mail/*.eml
```

## 发布快照与客人页浏览（故事 10，CAP-9；AD-5、AD-6、AD-13、AD-22）

### 发布流程（命令行；日常用后台 03 榜 → 预览 → 发布，见上文"后台 02 商品池、03 榜"）

```bash
uv run python scripts/publish.py rank                         # 看当前的榜
uv run python scripts/publish.py rank CO-0001 CO-0002 … CO-0012   # 按名次设榜（至多 12 件，须都在商品池里）
uv run python scripts/publish.py publish [--flush]            # 生成完整快照并入发件箱（--flush 立刻送一次）
```

- **入选：** 拣选状态"留下"、`items.status='active'`、非 Kleinanzeigen、欧元标价的货；商品池按墨墨留下时间新到旧。榜须恰好 12 件且都在入选集合内，否则拒绝生成（"榜需满 12 件才能发布"；榜里有货已下架/去掉时提示"榜里第 N 名 CO-xxxx 已不能发布（原因），请换一件后再发布"）。
- **白名单（AD-6，`contract/schemas/snapshot.v1.schema.json`）：** 货号、标准名称、中文标题（暂取标准名称，未能命名时取清洗后的卖家标题）、品牌 / 器型（标准名称的功能中文名，无功能时取器型）/ 图案 / 价格区间筛选字段、欧元价、图片哈希名与宽高、替代文字、清洗后的卖家原文及语言、点评、墓碑、发布编号、隐藏水位。费用档位、汇率是留空的字段位（故事 12）。卖家原文先去掉平台名、商品号、卖家/店铺、链接与邮箱。
- **价格区间（欧元，左闭右开）：** €200 以下、€200–500、€500–1000、€1000 以上。器型、图案只列商品池里实际出现的；品牌分区按实际出现的品牌（Meissen 在前）。
- **序号与锁（AD-22）：** 发布编号与 `hide_seq` 共用 `kv.publish_seq`，在生成快照的同一写事务内分配；快照水位 = 本次发布编号。`kv.publish_lock` 保证同一时刻只有一个发布（第二个提示"另一个发布正在进行，请稍后再试"）。锁在处理每件货时续期 30 分钟，入队前再核对仍由本次发布持有（被接手则本次不入队）；崩溃留下的锁最迟 30 分钟后失效，锁值格式坏掉视为无锁。
- **待送达与已送达：** 快照入队只记为待送达；同步模块收到服务器确认（applied，或服务器当前编号 ≥ 本次）才记为已送达并更新曾发布记录（墓碑来源）。`scripts/outbox.py drop` 丢掉快照时同时撤掉它的待送达记录，已送达快照里的货照常推隐藏/取消隐藏；之后重新 `publish`。
- **即时隐藏（AD-5）：** 可能在客人页上的货（已送达 ∪ 待送达快照，表 `published`）一旦原平台下架（`Store.mark_gone`，含已发布货检查与复查）、被去掉、移出商品池、标记售出、不再是有效欧元标价，与状态变更同一事务入发件箱 `hide`；撤销或重新在售时入 `unhide`。不需要重新发布，下一次 flush 即生效：队首快照发不出去（被拒收或暂时失败）时，其后的 hide/unhide 照常按序越过它发送，其它类别仍排在快照后面。离开商品池 90 天内的货随下一次发布作为墓碑（已售出 / 已移出）。

### 图片（AD-13）

- Mac 用系统 `sips` 把卖家图片转成最长边 1600 与 600 的 JPEG（不放大，质量 82，按 EXIF 方向转正后去掉 EXIF/XMP（APP1）、IPTC（APP13）与注释段，GPS、序列号不出 Mac），文件名 = 内容 sha256 前 16 位 + `.jpg`，放在 `data/publish_images/`；转码结果按来源缓存在表 `publish_images`，再发布不重转。本机已下载的图（`data/porcelain/images/`）直接用，其余只从平台图片站（`i.ebayimg.com`、`i.etsystatic.com`，https）下载：每次跳转都重新核对、拒绝解析到内网/本机的地址，须 `image/*` 且内容是 JPEG/PNG/WebP/GIF；卖家自建站的图不下载。发布输出列出没有可用图片的货号（`no_images`）。
- 同步模块送快照前（每批 500 个名字）：`POST /admin/images/check` 问缺哪些 → 逐张 `PUT /admin/images/<name>`（服务器核对内容哈希与 JPEG 头，确认 `image_ack.v1`）→ 再查一次全部在才提交快照。服务器拒收引用缺图的快照（400，旧快照不变，发件箱停住并通知）；本机图片丢失时同样停住，处理：`scripts/outbox.py drop <seq> --yes` 后重新 `publish`。
- 服务器保留最近 3 份快照引用的图（即旧图至少保留到其后两次发布之后），以及 1 天内新传的图，其余在新快照生效后删除。

### 大小上限

单条载荷 Mac 端上限 8 MB（`sync.MAX_PAYLOAD`，按实际发出的 UTF-8 JSON 字节计），服务器 `MAX_BODY` 9 MB，单张图片 8 MB，Caddy `request_body max_size 10MB`。

### 客人首页与单件页（服务器）

- `/`：页头（品牌 + 北京时间更新时间 + 用户协议与隐私说明 · 退出登录）→ 今日榜横滑（`<ol aria-label="今日榜，10 件">`，整卡一个链接）→ 全部藏品（品牌编号分区、器型 / 价格区间 / 图案底部面板、汇总行、双列瀑布流、每批 20 件续载）→ 页脚。从未发布显示"墨墨正在准备今天的藏品，请稍后再来。"；筛选无结果显示空状态与"清除全部筛选"。
- 不跑 JS 也能用：筛选与"加载更多"都是普通链接（`/?form=…&n=40`），面板用 `:target` 打开；`static/guest.js` 增强为就地刷新、焦点管理、按阅读顺序放进较短一列的瀑布流（不用 CSS columns）与滚到底自动续载（`GET /api/pool?offset=…`）。
- `/item/CO-NNNN`：完整单件页见下节（故事 12）。
- 快照数据 `/api/pool` 与图片 `/img/<name>` 只经会话鉴权：未登录 401，停用或待重新同意 403。图片 `Cache-Control: private, max-age=31536000, immutable`。

## 单件页、预估费用与币种（故事 12，CAP-10、CAP-11；AD-2、AD-6、AD-14、AD-15、AD-29）

### 费用档位推断（规则归口）

- 档位、金额与词表是数据：`config/fee_tiers.yaml`（`curio/config.fee_tiers` 读取校验，`config.load()['fee_tiers']`）。推断代码只在 `curio/judge.fee_tier`，对外入口 `curio/rules.fee_tier`。
- 档位：A 普通杯碟 约 €30、B 30cm 以下花瓶 约 €42、C 30–50cm 约 €67、D 50cm–1m 约 €140；含收货验货、材料、国际运费分摊、国内派送、服务费，不含保险与进口关税。
- 尺寸从卖家标题与描述读：`cm`、`mm`、英寸（inch；`"` 只认紧贴数字且带 H / Höhe / Ø 标签或在 `a x b` 多维里的，「Nr. 30"」不算），`H`、`Höhe`、`hoch`、`Ø`、`Durchmesser` 等标签（标签后没写单位按厘米），`30 x 20 x 10 cm` 取最大；取读到的最大值。包装 / 运输尺寸（Karton、Paket、Versand…）不算；以米写的（`1,20 m`）读不出。
- 器型依据（`cups` / `hard_to_pack` 各自的 `function_ids`、`words`、`zh_words`，都在 yaml 里）：标准名称的功能 ID 最优先；其次标题词（德语允许复合词，英语词带 `\b` 要求词首）；再次标准名称里的中文。两类词都出现时取标题里先出现的为货本身（「Tasse mit Watteau-Figuren」是杯）。
- 规则：杯碟类（标准名称的功能 ID，或标题里 Tasse、Teller、Becher、cup、saucer 等）没写尺寸或不超过 30 cm → A；否则按尺寸 ≤30 → B、≤50 → C、≤100 → D、超过 1 米 → 请咨询；人物瓷偶等难包装（功能 FN-052 塑像，或 Figur、Gruppe、Putto…）上跳一档（D 上跳为请咨询）；特别易碎（Spitzenfigur 等蕾丝人物、Kronleuchter）或读不出尺寸 → 请咨询。
- 每次推断带理由（如「高 28 cm → B」「人物瓷偶，尺寸 25 cm → B，难包装上跳一档 → C」），只在后台显示，不进快照。

### 后台手动改档

- 货详情 03「费用档位」：显示推断与理由；A / B / C / D / 请咨询 单选，选中稍停（约 0.4 秒，方向键连按只存最后一档）即保存，焦点留在选中项，晚到的旧响应不覆盖新选择（表 `fee_overrides`，手动值优先，AD-2）；选回推断值或「恢复自动」都删除手动值。每次实际改动记一条人工 `fee` 事件（`triage_events`，可追溯、不可撤销，不算拣选决定、不影响撤销与系统落位）。保存失败时选择回到原值，字段下"未保存 · 重试"。
- 档位配置按调用方的 root 读（`config.fees_for`：root 下有 `config/fee_tiers.yaml` 就用它，否则用仓库里的），发布、预览、指纹、货详情都传同一份。
- 接口 `POST /api/triage/fee`（`item_id`、`tier`：A–D / ask / null、`request_id`；页面令牌 + Origin；`request_id` 幂等，表 `triage_requests`）。保存后当前预览作废；生效档位、金额与名称进入"有未发布的修改"的指纹（`publish.status_fingerprint`），改档或改 yaml 里的金额 / 名称即显示"有未发布的修改"，改回原样即消失。

### 汇率（AD-14）

- `curio/fx.py`：每轮采集（`curio_monitor.run`，在同步之前）调用一次；ECB `EXR/D.CNY.EUR.SP00.A` 最近一个观测值（`requests`，超时 10/20 秒）。每天（柏林日期）最多成功取一次——取到的若还是前一天的观测值，工作日柏林 16:30 之后（ECB 约 16:00 CET 发布）再取一次；失败记一次警告，沿用最近一次成功的汇率与它原来的日期，1 小时后再试。存 kv `fx_cny_eur`（失败时间 `fx_attempt`）。采集结果写 `fx_status`（ok / fresh / wait / error）、`fx_rate`、`fx_date`。
- 测试里 ECB 请求一律被替换（`tests/conftest.py` 默认拦截），不连外网。

### 快照字段（`contract/schemas/snapshot.v1`，v1 内补齐）

- 每件：`fee_tier`（A–D 或 `ask`）、`fee_amount`（欧元整数 0–10000）与 `fee_label`（档位名称，取自 yaml；AD-6「费用档位与金额」）；`ask` 时后两项不带。原占位字段 `fee_eur` 从未被填写过，改名为 `fee_amount`，两端同改。Schema 用 `minimum` / `maximum` / `dependentRequired`（Mac 端最小校验器已支持）；"A–D 必带金额、ask 不带"由 `publish.check_fees`（入队前）与服务器 `snapshot.check_snapshot` 同样核对。
- 顶层：`fx_rate`（1–50）、`fx_date`，从未取到汇率时都不带，两者须同时出现。汇率不算"有未发布的修改"。

### 客人单件页（服务器）

- 顺序：页头 → 画廊（横向滑动、"上一张 / 下一张"、缩略图组 `aria-current`、页码、"放大"→ 放大层：双指缩放与拖动、"− / + / 复位 / 上一张 / 下一张"，只用"关闭"或 Esc 关闭）→ 货号 → 名称（标准名称，"外画"已在名称里）→ 卖家原价 → 01 品相（"据卖家描述："+ 原文块，带 `lang`，不高亮）→ 02 预估费用（"约 €42（B 档：30cm 以下花瓶）"或"请咨询"，附"不含保险与进口关税"）→ 03 墨墨点评（没有就整节不出现）→ 复制栏（"复制货号"，成功后常驻"已复制 CO-0123，回到微信粘贴到转账备注"；剪贴板不可用时显示已全选的只读输入框）→"浏览全部"→ 页脚。
- 不跑 JS：画廊可横向滑动、缩略图是页内锚点；"上一张 / 下一张"、"放大"、"复制货号"按钮由 `static/guest.js` 显示。
- 旧快照（故事 10，没有费用档位）不出现预估费用小节，点评顺延为 02。
- 已隐藏（快照里还有这件货）90 天内：去色、"已售出 / 已移出"状态牌在名称之前、划线价前有视觉隐藏的"已售出，原价 / 已移出，原价"、无复制栏、主按钮"浏览全部"；只剩墓碑（货号 + 状态）时只显示状态牌、货号与"此件已售出。/ 此件已移出。"。隐藏或离开超过 90 天、或不在快照里：「此件已不在展出」（404）。

### 币种（CAP-11）

- 首页与单件页页头的 `role="radiogroup"`"欧元 / 人民币"（€ / ¥），默认欧元。选择存在服务器账号上（`accounts.currency`，改动递增 `account_rev`），换设备登录沿用。`POST /currency`（`currency`=EUR / CNY、`next`）：页面脚本以 `Accept: application/json` 调用并就地换算所有价格与预估费用（`data-eur` 原值）、播报"价格已切换为人民币，约数，以欧元为准"；不跑脚本时是普通表单提交，303 回到原页面。
- 首页带筛选时 `next` 是 `/?form=…`（`safe_next` 只放行 brand / form / band / pattern / n 参数），不跑脚本切换后回到同一筛选。续载与就地筛选插入的新卡片按当前选中的币种重新换算（`window.curioMoney`）。
- ¥ 显示"≈ ¥1,443"（读作"约 1443 元人民币"，四舍五入到元），页头下注"按当日汇率，以欧元为准"。快照没有汇率、汇率日期在未来、或距今超过 7 天时不出现切换、只显示欧元（选过 ¥ 的账号另注"汇率暂不可用，暂以欧元显示"）。
- 预览（AD-28）按欧元显示，切换只在页面内换算、不保存（无表单、不提交到后台）。
- 档位名称以快照的 `fee_label` 为准；`server/guest.py` 的 `FEE_LABELS` 只在快照没带名称时兜底（测试核对它与 yaml 一致）。

## 后台 04 匹配：这件货适合谁（故事 16，CAP-5；FR-9、FR-10、FR-17；AD-15、AD-29）

- **对象**：02 商品池里的货（拣选状态 kept：已留下、未移出、未标记售出）× GoodCustomer 客户（`customers/` 与 `潜在客户/`，跳过已合并 / 已归档；读取沿用 06 的 30 秒缓存）。只读每位客户 `06_结构化结果/needs.jsonl` 与 `preferences.jsonl`，从不写资料库，不调用 AI，结果不上线。无效的 UTF-8 字节替换后照读；坏行（不是 JSON 对象）跳过，每个文件每次读取记一次警告，页面注明跳过的行数与读不了的文件数。资料库不在时 04 显示"找不到 GoodCustomer 资料库"。
- **规则（规则归口 `curio/judge.py` 的 `match_facts` / `match_customer`，经 `curio/rules.py` 调用；词表与权重在 `config/matching.yaml`）**：只看货的标准名称（品牌、图案、器型/功能）与卖家标题。
  - **明说想要**：`needs.jsonl` 里 `status=open` 的需求，品牌对上（`maker`；没写 `maker` 时看需求文字里是否提到品牌），写了 `pattern_names_raw` 的须对上图案，写了器型（描述与 `object_filters` 命中器型词组）且货的器型已知时须对上器型。理由如"明说想要：塞弗尔蓝色系列的杯、碟、壶"。
  - **明说喜欢 / 推算口味**：`preferences.jsonl` 里 `polarity=like`、`status=active` 的维度对上（`maker` → 品牌、`pattern` → 图案、`form` / `object_type` / `object_subtype` → 器型，比较 `value`）；对上的喜好里须有一条提到这件货的品牌——**只对上器型或图案不算**。只属于这个品牌的图案词组（如库尔兰之于 KPM）也算说明了品牌。明说（`explicit`）的品牌、或明说的只属于该品牌的图案对上即"明说喜欢"，否则"推算口味"（只对上器型的明说不升级；理由里注明来源，如"推算口味：Meissen（购买）"，最多列 3 项加"等"）。比较用 `value`，为空才用 `value_raw`；`dimension` / `source_type` 不是文字的行当作未知。
  - **不喜欢**：任一 `status=active`、`polarity=dislike` 的喜好对上同一维度 → 这位客人不匹配这件货（需求也不算）。排除词（`negations`：以外 / 之外 / 除外，`exclusions_before`：除）管整句，含"、"并列（到 ，。；!? 为止）："梅森、老德以外的小牌子""除了梅森、KPM"里的品牌一律不算被提到——喜欢、需求、不喜欢都一样，所以不喜欢"梅森、老德以外的小牌子"不会排除梅森。普通否定（`negations_before`：不一定 / 不要 / 不是 / 并非）只看别名自己那一小段（到"、"为止），喜欢与需求里不算提到；不喜欢的喜好不做这项检查（"不要梅森"就是不喜欢梅森）。器型与图案按整词 / 德语复合词词尾比较，同一处几个词都命中时取最长的（Untertasse 是碟、Bildplatte 是瓷板）。
  - **品牌只是门槛**（墨墨 2026-11-04 收紧）：`detail_required` 列出的分级（现为全部三级）只对上品牌不算匹配，还须对上图案或器型——明说想要看需求写的图案 / 器型，明说喜欢与推算口味看是否有一条图案或器型喜好对上。收紧前真实数据里一件梅森平均推给约 83 位客户，收紧后中位 2 位。想放宽就从 `detail_required` 里去掉对应分级。
  - 词表：`brands`（知识库品牌名 → 中文别名，如 Sèvres ↔ 塞弗尔）、`patterns`（图案词组：图案 ID、中文名、卖家标题外文词，可限品牌）、`forms`（器型词组，含烟灰缸，免得 Aschenbecher 被当成杯）、`detail_required`（须再对上图案或器型的分级）、`grades`（分级标签与权重，明说想要 > 明说喜欢 > 推算口味）。格式不对时报错（中文）。
- **界面**：按客人一行（称呼 + 配上件数；有"明说想要"的排前，再按待处理件数、配上件数），点开展开逐件列表（缩略图、货号、标准名称、价格、分级与理由；同级按分数）。每件「确认」或「不合适」（表 `match_decisions(customer_id,item_id,decision,at)`；写请求带 `request_id`，表 `triage_requests` 幂等；失败行内"未保存 · 重试"）。只接受此刻匹配的客人与货（或已有处理记录的）。不合适的此后不再对该客人提出；标完后该行显示"已标不合适 · 撤销"（直到下一次读取），撤销（`decision=undo`）删掉那一行、回到未处理。拒绝类错误（400）只显示原因并重新读取。04 计数 = 有未处理（未确认、未标不合适）匹配的客人数。匹配不标在 02 商品池的货旁。
- **复制单件页链接（AD-29）**：确认后出现。链接 = 站点地址（直接读 `.env` 的 `CURIO_SERVER_URL`，须为 http(s) 地址，不需要管理密钥）+ `/item/CO-NNNN`，不按人区分。只在该货号在当前已确认送达的快照中（`publishes.status=delivered`）且未隐藏（`published.hidden=0`）时可用，否则按钮 `aria-disabled`、原因"请先发布这件货"；发布确认后自动可用，下架 / 售出隐藏后再次禁用。站点地址未配置或无效时也禁用并说明。成功提示"已复制单件页链接 · 去微信发给<称呼>"；剪贴板失败显示已全选的只读输入框。
- 接口（本机后台，令牌 + Origin 同其它写接口）：`GET /api/match`、`POST /api/match/decide`（`customer_id`、`item_id`、`decision`：confirmed / rejected / undo、`request_id`）。`config/matching.yaml` 缺失或格式不对时 `GET /api/match` 返回 400 与中文原因。
- 按账号排序见"千人千面商品池"一节（故事 18；04 标"不合适"的货对该客人排到末尾）。规律沉淀见下节"后台 05 待审核"（故事 17）。

## 后台 05 待审核：判断沉淀（故事 17，CAP-8；FR-18、FR-19；AD-15、AD-26）

- **记录什么（FR-18）**：每次留下 / 去掉 / 捞回都在 `triage_events`（带当时分数与规则版本），点评在 `comments`。归纳只看 `actor=human`、未被撤销的留下、去掉、捞回（捞回算"留下"方向）；同一件货按最后一次这样的人工方向计一次。询市、改费用档位、点评不参与归纳（v1 不从点评文字归纳）。
- **同一类操作 = 特征组合**：特征只取自标准名称（`std_names`）——品牌、器型（功能 `function_id`）、图案；组合为单维（品牌 / 器型 / 图案）、两维（品牌+器型、品牌+图案）与品牌+图案+器型（图案只在 Meissen 下识别，即"Meissen梅森 蓝花的盘"这一级）。外画标记与价格区间（只比 `price_bands.currency` 币种的货）是可选附加维：只在基础组合因比例不够而不成立时才往下细分，且附加维越少越先（如去掉"Meissen梅森的盘（€50 以下）"、留下"Meissen梅森的盘（€150–400）"）。没有某特征的货不进入需要它的组。
- **门槛（`config/learning.yaml`）**：同一方向 ≥ `min_count`（5）件，且占该组合人工决定 ≥ `min_ratio`（80%）。组合越具体越优先：先提出最具体的；更一般的组合已有待审或已确认的同向规律时，具体组合不再提出，已在待审的具体组合撤下；一个组合的依据件已全部落在别的同向规律（任何状态）里时，不再换个组合提出。待审条目每次归纳更新依据件数，跌到门槛以下（如撤销）时撤下。
- **何时归纳**：调度每轮一次（`curio/curio_monitor.py`，在同步之前；结果里 `learning_status`、`rules_pending`，出错时 `learning_error` 写明原因），以及打开 05 时（`GET /api/rules?refresh=1`，同一个库 10 秒内只做一次，写锁被占用 0.5 秒以上就跳过本次）。归纳在写锁外用一次联表查询算好，只在短写事务里落库；人工事件、标准名称、规律与参数都没变时直接跳过（`kv.learning_sig`）。新规律只改 05 分区上的数字，不弹窗、不播报。
- **界面**：每条显示规律一句（模板生成，不调用 AI），如"Meissen梅森 蓝花的盘：6 件里你去掉了 6 件——以后自动去掉？"、依据件数、最近一次时间；"确认" / "否决"均为最终操作、无撤销，条目移出、焦点移到下一条，播报"已确认规律" / "已否决规律"。网络或服务端（5xx）失败行内"未保存 · 重试"，重试沿用同一个 `request_id`（`triage_requests` 幂等）；4xx 拒绝不可重试，只显示原因并重新读取。没有待审时显示空状态（门槛数字取自 `config/learning.yaml`）。`config/learning.yaml` 有误时 05 照常显示已有条目，并注明原因。
- **已生效的规律**：待审条目下方列出已确认的规律（"自动去掉：…" / "新货加分：…"），每条可"停用"（需确认）。停用的规律（status `retired`）不再执行，也不再提出；已被它移出的货仍可在"已自动移出"里捞回，重判时回到新货。
- **生效（只在规则归口 `curio/rules.py`）**：规律存 `curio.db` 表 `learned_rules`（`combo_key` 即特征组合、方向、状态 pending / confirmed / rejected / retired、依据件数、最近时间）。
  - 去掉类：确认时当场把未经人工决定、处于新货的同组合货移出；之后入库（及 `rejudge`、屏蔽词重判）时该组合的货自动移出，理由"已确认规律：…自动去掉"，在"已自动移出"里照常可捞回。
  - 留下类：同组合新货的规则基础分加 `keep_bonus`（10）分，理由里写明；不改拣选状态。确认 / 停用时为现有未经人工决定的新货重算一次基础分；未过期的 AI 分不动，送给 AI 的基线分同口径。同一件货若也适用已确认的去掉类规律，不加分；与已确认的反方向规律可能同时适用于同一类货的规律不能确认（先停用那条）。
  - 规律从不改动已有人工决定的货（有未撤销的留下、去掉、捞回、询市等人工决定：状态、判断理由与基础分都不受规律影响；只做过又全部撤销的货视为未经人工决定）。
  - `config/learning.yaml` 读不了时，入库与打分照常，只是不计规律加分（日志记一次）。
  - 否决、停用的规律按组合键与方向记录，之后不再提出（即使次数继续增加）。
- 接口（本机后台，令牌 + Origin 同其它写接口）：`GET /api/rules`（`refresh=1` 时先归纳；返回 `count`、`items`、`active`、`config`，出错时 `refresh_error`）、`POST /api/rules/decide`（`rule_id`、`decision`：confirm / reject / retire、`request_id`）。

## 后台 06 客人：账号关联与管理（故事 14，CAP-14；AD-8、AD-12、AD-24）

- **账号名单**：服务器 `GET /admin/accounts`（管理密钥，契约 `contract/schemas/accounts_list.v1.schema.json`）只返回账号编号、用户名、微信名/称呼、状态、注册时间、`account_rev`——不含邮箱与任何哈希。Mac 每轮同步（`sync.run` 末尾的 `sync.pull_accounts`）与打开 06（`/api/guests?refresh=1`，限时 5 秒）时拉取，按 `account_id` 覆盖写 `curio.db` 的 `guest_accounts`（`account_rev` 不小于已有的才写）；服务器上没有的账号不删。拉取失败保留上次名单，结果写 `accounts_status: error` / `accounts_error`（kv `accounts_pull_error`），不计入同步连续失败、不影响其余部分。
- **三个列表**：待关联（未关联、未停用；06 计数 = 待关联数）、已关联（按微信名或用户名即时过滤，"没有找到 …"）、已停用。空时显示"暂无待关联的新注册"。
- **关联（只在本机，AD-12）**：表 `guest_links`（账号 → GC 编号，永不上线）与只追加的 `guest_link_log`（link / relink / unlink，含前后编号、时间、request_id）。即时生效，没有"待同步"，不经服务器。
  - 「关联到已有客户」：按微信名、显示名、别名或编号即时搜索 GoodCustomer 档案（只读 `customers/` 与 `潜在客户/` 下的 `customer.json`，跳过已合并 / 已归档），点选即关联。
  - 「新建档案」：以微信名/称呼为显示名调用 GoodCustomer 自带的 `gcdb.py --db <资料库> new-customer --name=…`，拿到新 GC 编号后同一操作内关联；gcdb 失败时不关联、行内报错。Curiobject 从不直接写资料库里的任何文件。
  - 「更正关联」（重新选档案）、「解除关联」（回到待关联）都先出确认对话框。
  - 资料库路径写在 `config/settings.yaml`：`goodcustomer.db`（默认 `~/Desktop/goodcustomerDB`）与 `goodcustomer.gcdb`（默认 `~/Desktop/goodcustomer-plugin/skills/gc-schema/scripts/gcdb.py`）。资料库不存在（没有 `_system/db.json`）时名单照常显示，关联区提示"找不到 GoodCustomer 资料库"。
- **停用 / 重置密码（AD-24 指令）**：`disable_account.v1` / `reset_password.v1`，带 `command_id` 与按账号单调的 `target_seq`（kv `cmd_seq:account:<account_id>`，两种指令共用），经发件箱发送（表 `guest_commands` 记指令与发件箱记录的对应）。入队后行内"待同步"，后台进程内立即触发一次 flush；发件箱确认后换成"已停用"/"已重置"并 polite 播报；服务器回 `applied: false`（序号不大于已应用值、账号不存在、对已停用账号重置）按永久失败，行内"未同步 · 请处理"，不自动重发（`scripts/outbox.py` 处理）。服务器同一 `command_id` 只执行一次、重放返回第一次结果；停用与重置都使该账号全部会话与重设链接立即失效。停用不可恢复，已停用账号不提供重置；重置只在已关联列表提供。
  - 临时密码由 Mac 生成（12 位，去掉 0、1、I、O、l、o），服务器只存 Argon2id 哈希（`events` 表里为 HMAC）。Mac 只把明文放进发件箱等发送，服务器答复后 flush 立即抹掉；`guest_requests` 等表从不写明文；`scripts/outbox.py list` 只显示账号编号前 8 位与序号。后台浏览器只在确认后显示一次临时密码与「复制」，离开 06 或页面即不可再见（同一 request_id 重放不再给出明文，提示再重置一次）。
- **邀请码**：06 顶部显示当前邀请码（最近一条已送达的 `set_invite_code`）与「更换」；更换需确认，复用 `sync.enqueue_invite`；待确认的新码后跟"待同步"，期间旧码仍有效；不合规的码就地报错。
- 接口（本机后台，令牌 + Origin 同其它写接口；写请求带 `request_id`，表 `guest_requests` 幂等）：`GET /api/guests[?refresh=1]`、`GET /api/guests/search?q=`、`POST /api/guests/link`（action：link / relink / unlink / create）、`POST /api/guests/command`（action：disable_account / reset_password）、`POST /api/guests/invite`。
- 不在本故事：按账号排序（故事 16 起）、删除账号与撤回同意、自动匹配建议。关联、更正、解除后重算行为交接文件见下节（故事 15）。

## 后台 07 符合度：系统选的和墨墨选的有多一致（故事 23）

只在本机（`curio/agreement.py`），不上线、不进 GoodCustomer；只读，不改打分、不改 01 排序与拣选。

- **封存**（表 `daily_predictions`，每个柏林日期 pool / rank 各一条，写入后不可改：`INSERT OR IGNORE`，另有禁止 UPDATE 的触发器）：
  `pool` = 01 候选（口径同瀑布流：新货、非 backlog、在售、未被"看过即不要"排除）按当前 `scores` 分数高到低的完整有序 `item_id` 列表，前 100 件为"系统进池"；
  `rank` = 商品池（口径同 02 / `publish.eligible`）按分数高到低的完整排序，前 12 件为"系统 MyR12"。同分按 `first_seen` 新到旧、再按 `item_id`，无分的排最后；与 01 显示顺序无关。
  每条另记封存时的分数列表、分数来源计数（`source`）、口径版本与 `rule_version`（`version`）、封存时间与触发方式（`collect` / `feed`）。
  两种触发都只在**柏林 05:00 之后**：采集调度当天第一轮（`seal_for_run`；空闲轮在调度判断后，有工作的轮次在抓取入库与 AI 打分之后，让当天第一批新货进预测；结果字段 `prediction_status`，失败不影响采集），或墨墨先打开 01 时的首个不带游标的 `GET /api/triage/feed`（`seal_for_feed`，带本次打开的 `session`，本次里刚看到的货不被"看过即不要"排除）。表上有禁止 UPDATE / DELETE 的触发器，代码只用 `INSERT OR IGNORE`。
- **进池符合度**（每日，只算封存时刻 `sealed_at` 之后的事件）：K = 当天有效人工留下（`triage_events`：actor=human、未撤销、非 fee/undo，`new_state='kept'`，按**服务器时间** `at`）且在封存 pool 里的货；
  V = 当天 `owner_events.seen`（按 seen 事件的 **`eff_at`** = min(`client_ts`, `at`)）且在封存 pool 里的货，**再并上 K**（留下的货必然看过；seen 上报丢失时基线不至于超过 100%）；n = |K|。
  系统选 = 封存排序限制在 V 内的前 n 件。符合度 = |K ∩ 系统选| ÷ n；随机基线 = n ÷ |V|；提升倍数 = 符合度 ÷ 基线。n = 0 记"无数据"（不算 0）；当天没有封存记"当天没有封存预测"。
  封存之后才入库的货不在列表里，不计入 K；留下后撤销的不计入。当天封存之前已有人工决定（如零点后、封存前就开始挑）时结果带 `late_seal`、`late_decisions`、`first_decision_at`，页面显示"封存晚于当天最早的 N 次挑选，这些不计入"。另给出 K 里有几件在系统前 100（`kept_in_top`），页面显示"你留下的 N 件里有 M 件在系统前 100"。
- **榜符合度**（每日）：当天最后一次发布（`publishes`，非 dropped，按 `created_at` 的柏林日期）的 12 件；当天没发布但榜改过（`triage_requests` 里当天的保存榜，或结果带 `left_rank` 的请求，如移出商品池 / 标记售出时从榜上拿掉）时取当天结束时的榜（今天 = 当前 `publish_rank`，往日 = 当天最后一次改动后的榜）。与封存 rank 前 12 的重合数 ÷ 12；都没有记"无数据"。
- **差异清单**：「你选了、系统没选」（K − 系统选）与「系统选了、你没选（你看到过）」（系统选 − K），每件含缩略图、名称、价格、系统名次（在封存 pool 中的位置）与封存时的分数，按系统名次排。
- **缓存**：请求时计算；今天与昨天每次实时计算（昨天晚到的 seen 仍会计入），**前天（D−2）及更早**的日期首次计算后写入 `daily_agreement`（同样禁止 UPDATE / DELETE），之后原样返回。最早一次封存之前的日期一律"无数据"、不写库。
- 页面（07 分区，手机优先）：今日 / 昨日两栏（进池符合度、随机基线、提升倍数、留下 / 看到 / 系统同选件数、榜重合 x/12），近 30 天进池符合度（实线）与随机基线（虚线）的内联 SVG 折线（无数据的日子断开），今日 / 昨日差异清单切换。分区计数显示今日进池符合度。
- 页面只在打开 07 时读完整数据（`GET /api/agreement`，含近 30 天摘要）；分区计数在打开后台与 01 留下 / 去掉 / 撤销之后读 `GET /api/agreement/history?days=1`。
- 接口：`GET /api/agreement?day=YYYY-MM-DD`（默认今天；返回该日、前一日与近 30 天摘要 `history`；日期无效或在未来 400）、`GET /api/agreement/history?days=30`（1–90 天，旧到新的每日摘要，不含差异清单；天数无效 400）。
- 2026-11-04 封存上线当天墨墨已在瀑布流留下 181 件但没有封存，第一份可比较的数据从次日开始。

## 行为数据回流（故事 15，CAP-15；AD-9、AD-12、AD-18、AD-25、AD-28）

客人的打开、点开、停留、筛选、币种选择按账号记录，经服务器收件箱临时中转回到 Mac，成为该客人在 GoodCustomer 侧的记忆。行为数据原文不推回线上；点开与停留只在 Mac 上用于千人千面排序（故事 18，见下节），线上只收到算好的货号顺序。

### 事件（契约 `contract/schemas/behavior_event.v1.schema.json`）

字段：`event_id`（客人页生成的 UUID，重试不变）、`account_id`（服务器按会话填，不信客户端）、`type`、`co_code`（可空）、`client_ts`（仅参考）、`server_ts`（服务器入箱时写，权威）、`params`、`event_schema_version`。

| 类型 | 何时 | `co_code` | `params` |
| --- | --- | --- | --- |
| `home_open` | 打开首页 | null | 当前筛选 `brand` / `form` / `band` / `pattern`（各自可缺） |
| `item_open` | 打开单件页 | 货号 | `from`：`rank`（从今日榜点开）/ `pool`（从全部藏品点开）/ `link`（直接打开链接） |
| `item_dwell` | 单件页隐藏或离开时（`sendBeacon`） | 货号 | `seconds`：这一段可见时长，1–1800，超过截为 1800 |
| `filter` | 首页就地筛选改变后 | null | 改变后的所选值（同 `home_open`） |
| `currency` | 币种切换保存到账号且值确实改变时（服务器在 `POST /currency` 里记录，脚本与无脚本表单提交都记，`event_id` 由服务器生成） | null | `currency`：`EUR` / `CNY` |

各类型的 `co_code` 与 `params` 写在同一文件的 `$defs.<type>`，服务器与 Mac 都先按整份、再按 `$defs.<type>` 校验（Mac 最小校验器为此支持了 `type` 列表与 `$defs`）。

### 客人页（`server/static/guest.js`）

- 只在服务器上已登录客人的首页与单件页运行：页面带 `data-track-page` 标记与账号标记 `data-who`（账号编号的 HMAC 前 16 位，不是账号编号；`home_page` / `item_view_page` 的 `track` 只由客人路由传入）。Mac 预览（`/preview/…`，AD-28）、停用页、待重新同意页不带标记，不生成也不上报；预览的币种切换本来就只在页内。
- 事件先进本机队列（`localStorage` 的 `curio-events`，最多 200 条、7 天；每条带账号标记，同一设备换了账号登录时别的账号留下的未发事件直接丢掉；写回前重新读取并按 `event_id` 合并，多个标签页不互相覆盖；存储写不进时改用内存队列；单条超过 2 KB 的不入队），约 1.5 秒后每批最多 50 条 `POST /events`；网络失败或 5xx / 429 留在队列里，30 秒后或下一页再发（同一 `event_id`，服务器去重）。服务器收下、拒收（400）、未登录 / 不记录（401 / 403）的才从队列删掉。页面隐藏或离开时用 `sendBeacon`（`text/plain` 的 JSON）把队列发出——beacon 只表示浏览器接下了，事件仍留在队列里，下次 fetch 得到答复才删（重复的由服务器按 `event_id` 去掉）。
- 从榜或商品池点开单件时在 `sessionStorage` 记下来源（按链接解析后的路径判断货号），单件页据此填 `item_open.from`；其它情况为 `link`。

### 服务器收件箱（`server/inbox.py`，路由在 `server/app.py`）

- `POST /events`（会话 Cookie；`behavior_upload.v1`，每批 1–50 条，请求体 ≤ 64 KB——不带 Content-Length 的分块请求边读边数，超过即 413；`application/json` 或 `text/plain`；带 Origin 时必须同站，带 `Sec-Fetch-Site` 时必须 `same-origin`；两者都没有时放行，靠会话 Cookie 的 SameSite=Lax 挡住跨站）：未登录 401，停用或待重新同意 403；不合格的事件（如枚举外类型 `share`）列在 `rejected`、计数（`inbox_stats.rejected`）并记日志，一条都不合格时 400。按 `event_id` 去重（`inbox_seen` 保留 30 天，Mac 确认删除后重发也不再入箱），分配单调 `inbox_seq`。每账号每小时（整点窗口）最多 600 条，超出丢弃并计数（`inbox_stats.rate_dropped`）、记日志。
- `GET /admin/inbox?after=<seq>&limit=500`（管理密钥；`inbox_batch.v1`）按 `inbox_seq` 升序返回，`more` 表示之后还有；`stats` 带服务器累计计数（`expired` / `rate_dropped` / `rejected`）。
- `POST /admin/inbox/ack {schema_version: 1, max_seq}`（管理密钥；`inbox_ack.v1`）只删除 `inbox_seq ≤ max_seq` 的事件，幂等。
- 未确认的事件保留 30 天，上报与拉取时清理超期的并记日志（`inbox_stats.expired`）。线上不长期保存行为数据；GC 编号与映射从不上线。

### Mac：拉取 → 入账 → 重算 → 确认（`curio/behavior.py`）

- 随 `sync.run` 每轮进行（在心跳与账号名单之后）：在 `curio.db` 同目录锁文件 `curio.db.inbox.lock` 的 `fcntl.flock`（不等待）内进行，同一时刻只有一个进程；后台与调度同时拉取时另一方跳过本轮（`inbox_status: busy`）。
- 每页：拉取 → 按 `event_id` `INSERT OR IGNORE` 写入 `behavior_events` 并提交（同一事务把受影响客户记入 `handoff_dirty`）→ 重算交接文件 → 确认本页最大 `inbox_seq`（kv `inbox_acked_seq`）。写入失败不确认；确认前崩溃时下轮重拉、不重复。无法解析的事件原样存入 `behavior_unknown`（按内容哈希去重）并照常确认，不阻塞。
- 失败（含重算、契约文件等意外）只写结果字段 `inbox_status: error` / `inbox_error`（kv `inbox_pull_error`），不计入同步连续失败，下轮重试。限时（20 秒，从先补算上轮留下的待重算之后开始）用完而还有页时另有 `inbox_more: true`。结果另有 `inbox_pulled`、`inbox_new`、`inbox_duplicate`、`inbox_unknown`、`handoff_errors`，以及服务器计数 `inbox_server_expired` / `inbox_server_rate_dropped` / `inbox_server_rejected`；计数比上次增长时记警告日志并给出 `inbox_server_grown`（例如 Mac 离线超过 30 天、事件被服务器删掉）。

### GoodCustomer 交接文件

- 位置（墨墨 2026-11-03 决定）：Curiobject 自己的目录 `data/gc_handoff/<GC编号>.json`（即 `curio.db` 同目录下的 `gc_handoff/`），**不写入 GoodCustomer 库**；GoodCustomer 要接时再指过来。重算只用 `curio.db` 里的映射与账本，不依赖 GC 库是否可用。
- 每个已关联客户一份，内容为该客户当前关联的全部账号在账本中的全部事件（按 `server_ts`、`event_id` 排序，`event_id` 唯一）加按类型的计数。每次整份原子覆盖写（临时文件 + `os.replace`），不追加，内容相同则字节相同；没有任何关联账号时删除该文件。未关联账号的事件只留在账本，关联后整段历史进入文件。
- 何时重算：新事件入账、关联、更正（前后两个客户）、解除、新建档案并关联。待重算的客户记在 `handoff_dirty`（与触发它的写入同一事务），重算成功才清掉；写文件失败下一轮拉取（或下一次关联变化）再算。

```json
{
 "handoff_version": 1,
 "customer_id": "GC000123",
 "source": "curiobject",
 "accounts": [{"account_id": "…32 位…", "username": "wangjie", "nickname": "王姐"}],
 "summary": {"total": 5, "by_type": {"home_open": 1, "item_open": 1, "item_dwell": 1, "filter": 1, "currency": 1},
             "first_server_ts": "2026-11-03T08:00:00.000Z", "last_server_ts": "2026-11-03T08:05:12.345Z"},
 "events": [{"event_id": "…", "account_id": "…", "type": "item_open", "co_code": "CO-0012", "client_ts": "…",
             "server_ts": "…", "params": {"from": "rank"}, "event_schema_version": 1}]
}
```

## 验证

```sh
uv run pytest -q
cd server && uv run pytest -q    # 服务器端测试（单独的 venv）
```

同步测试只连本机 127.0.0.1 上临时启动的服务器（标准库假服务器，以及 `server/.venv` 存在时的真服务器端到端），部署与备份脚本用 mock 或本机执行验证，不连真实 VPS。测试使用模拟API和临时数据库，不连接商品平台，不消耗模型额度。测试结果见 `VALIDATION.txt`。原仓库基线验收仍待拿到 main 和案例后运行；目标计数为店主「要」21件：6/15/0，店主「不要」289件：4/245/40，当前未验证。

## 2026-10-05 本机部署结果（历史记录（迁移前））

已安装每小时任务 `com.curio.collect.phase1`。首次实采 eBay 新增1000、Kleinanzeigen新增509；随后通过 launchd 执行第二轮，两个平台均成功、新增均为0，确认已知编号不会重复入库。31个本地测试通过。新版 `.env` 已从桌面旧版安全配置，旧项目未修改。

## 采集、筛选、字段规范和微信 Bot

- [当前采集与筛选方式](docs/COLLECTION_AND_SCREENING.md)
- [字段映射及核验边界](docs/FIELD_MAPPING.md)
- [V2 全部75字段](docs/product-fields-v2.json)
- [V2 字段与命名规范正文](docs/瓷器字段与命名规则V2.txt)
- [微信接入与运行](docs/WECHAT.md)

微信适配代码位于 `wechat_bot/` 和 `scripts/wechat_feed.py`；固定版本SDK在 `third_party/`。

## 千人千面商品池（故事 18，CAP-12、FR-24；AD-5、AD-6、AD-11、AD-28）

客人页"全部藏品"按每个账号的记忆库排序；今日榜人人相同；排序只改先后、不改集合（客人看到的件数与别人一样）。

### 谁有个性化顺序

- 账号已关联 GoodCustomer 客户（06 客人，故事 14）、账号在用（`guest_accounts.status=active`，且没有未被拒收的停用指令），并且该客户有可用记录：GoodCustomer 需求 / 喜好对上池中至少一件货（不喜欢对上也算）、墨墨在 04 对该客户标过"不合适"的货在池中，或该客户关联的任一账号在行为账本 `behavior_events` 里有（最近 10 个半衰期内的）事件。已合并的 GC 档案顺着 `merged_into_customer_id` 用存续档案的需求与喜好（只读）；找不到档案的记日志、只用浏览信号。
- 其余（未关联、无记录、已停用）不下发顺序，看墨墨的顺序；算出来与墨墨顺序完全一样的也不下发（效果相同）。停用账号的顺序在下一次发布时自然移除。同一客户的几个账号用同一份顺序。
- 没有任何在用的已关联账号时，发布根本不读 GoodCustomer；资料库不在时只用浏览信号。

### 怎么算（规则归口 `curio/judge.personal_order`，经 `curio/rules.py`；不调用 AI）

每位客户对池中每件货打分，同分保持墨墨顺序（稳定排序）：

- **匹配分级**：沿用 04 匹配的 `match_customer`，权重取 `config/matching.yaml` 的 `grades`（明说想要 3 > 明说喜欢 2 > 推算口味 1）。
- **浏览信号**（参数在 `config/personalize.yaml`）：该客户各账号的 `item_open` 每次 `open` 分、`item_dwell` 每秒 `dwell_per_second` 分（单条最多 `dwell_cap_seconds` 秒），按服务器入箱时间每 `half_life_days` 天减半；乘以"被浏览那件"与池中这件货的相近度（同品牌 `same.brand` + 同图案 `same.pattern` + 同器型 `same.form`）后合计为信号 b，这件货的浏览加分 = `max_score` × b / (b + 1)：随信号增加、趋近而不超过 `max_score`。`max_score` 须小于 `matching.yaml` 分级权重的最小级差（含推算口味与无匹配之差），所以浏览永远翻不过分级：明说想要 > 明说喜欢 > 推算口味 > 无匹配。被浏览的那件本身不因自己被浏览加分（避免反复推同一件）。
- **沉底**：不喜欢的维度对上（品牌 / 图案 / 器型），或墨墨在 04 对该客户标了"不合适"的货 → 排到末尾（仍可见）。

### 何时刷新、怎么下发

- 只随完整发布刷新（AD-11，没有单独的排序增量）：`publish.publish` 在写事务外读 GoodCustomer、行为账本（只读最近 10 个半衰期）与标准名称（各一次查询）并打分（`personalize.prepare`）；生成快照的写事务里只重新核对谁在用、按算好的分数排序（`personalize.orders`），写进快照的可选字段 `orders`（账号编号 → 货号顺序，只发到与墨墨顺序最后一处不同的位置为止，其余服务器按墨墨顺序补在末尾）。画像、偏好、浏览原文、客户编号都不上线。
- 千人千面是"尽量做到"：配置文件缺失或格式不对、读取或排序出任何错，只记警告，本次发布不带 `orders`，发布照常。
- 顺序不进内容指纹：预览（AD-28）按未关联客人视角、墨墨顺序渲染，预览之后客人有新浏览不会让发布变成"内容有变化"。
- 客户数（同一客户几个账号算一位）记在 `publishes.orders`，03 榜的发布状态显示"已发布 · HH:MM · 已为 N 位客人排好顺序"（只是数字）；发布接口的结果也带 `orders`（故事 18 之前的发布结果重放时为 0）。
- 加上顺序后快照超过 `publish.SNAPSHOT_MAX`（`sync.MAX_PAYLOAD` 减去信封余量）时，从最长的顺序开始去掉直到放得下（去掉的账号看墨墨顺序），记警告日志。
- 两端都核对 `orders`（契约里 `propertyNames` 与 `uniqueItems`，Mac 最小校验器为此支持了这两个关键字）：账号编号为 32 位小写十六进制、货号都在本快照 `pool` 里、不重复；服务器不合即拒收整份快照（400，Mac 发件箱判永久失败并通知）。旧快照没有 `orders` 时全员墨墨顺序。

### 服务器怎么用（`server/snapshot.View.for_account`）

服务器不做任何排序计算：首页"全部藏品"、筛选、续载（`/api/pool`）对当前会话账号照本人那份顺序排；集合永远 = 当前商品池 − 隐藏集合——顺序里已隐藏的货号跳过，池中不在顺序里的货按墨墨顺序排在末尾。今日榜、单件页、筛选项与件数不变。别人的顺序从不出现在页面或接口里。续载带上页面首批所属的发布编号（`pn`）；续载期间换了新快照时服务器从头给第一批（`reset: true`），`guest.js` 整列替换，避免重复或漏掉。

