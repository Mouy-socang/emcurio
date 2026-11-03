# contract/ — 跨机契约（AD-21）

Mac（权威端）与香港服务器（发布端 + 收件箱）之间的全部载荷结构，**只在这里定义一次**，两端都按这里的 JSON Schema 校验。任何一端都不得在别处另写结构定义。

## 目录

```
contract/
  README.md        本说明
  schemas/         每类载荷一个 JSON Schema 文件
```

已定义：

| 载荷 | 方向 | 文件 |
| --- | --- | --- |
| 管理接口事件信封（`POST /admin/events`，按 `event_id` 去重） | Mac → 服务器 | `envelope.v1.schema.json` |
| 通道测试 `ping`（信封 `kind: ping` 的 `payload`） | Mac → 服务器 | `ping.v1.schema.json` |
| 指令：设置通用邀请码 `set_invite_code`（AD-2、AD-24；`command_id` + 按目标 `invite` 单调的 `target_seq`） | Mac → 服务器 | `set_invite_code.v1.schema.json` |
| 指令：停用账号 `disable_account`（AD-2、AD-8、AD-24；`command_id` + 按账号单调的 `target_seq`，与 `reset_password` 共用计数器 `account:<account_id>`；停用使全部会话与重设链接失效，不可恢复） | Mac → 服务器 | `disable_account.v1.schema.json` |
| 指令：重置密码 `reset_password`（AD-24；Mac 生成 12 位临时密码，服务器只存哈希、`events` 表存 HMAC；重置使全部会话与重设链接失效；Mac 发件箱明文在确认后抹掉） | Mac → 服务器 | `reset_password.v1.schema.json` |
| 发布快照 `snapshot`（AD-5、AD-6、AD-22；字段即 AD-6 白名单） | Mac → 服务器 | `snapshot.v1.schema.json` |
| 隐藏推送 `hide`（含 `action: unhide`；`hide_seq` 与快照 `publish_no` 同一计数器；`reason: sold` 含原平台下架与墨墨标记售出） | Mac → 服务器 | `hide.v1.schema.json` |
| 心跳 `heartbeat`（AD-10；每轮直接发送、不进发件箱；各平台已发布货下架检查最近成功时间，服务器只留 `sent_at` 最新的一份、不存入事件表） | Mac → 服务器 | `heartbeat.v1.schema.json` |
| 账号名单（`GET /admin/accounts` 的响应；只有账号编号、用户名、微信名/称呼、状态、注册时间、`account_rev`，不含邮箱与任何哈希；Mac 按 `account_id` 覆盖写，服务器没有的账号不删） | 服务器 → Mac（Mac 拉取） | `accounts_list.v1.schema.json` |
| 图片查询（`POST /admin/images/check` 的请求 / 响应，AD-13） | Mac ⇄ 服务器 | `image_check.v1.schema.json` / `image_check_result.v1.schema.json` |
| 图片上传确认（`PUT /admin/images/<name>` 的响应，AD-13） | 服务器 → Mac | `image_ack.v1.schema.json` |

信封的 `payload` 按 `<kind>.v<payload.schema_version>.schema.json` 另行校验；新增载荷类别时同时把它加进信封的 `kind` 枚举。Mac 端用标准库最小校验器（`curio/sync`，只支持 `type`、`required`、`properties`、`additionalProperties`、`const`、`enum`、`pattern`、`minLength`、`maxLength`、`items`、`minItems`、`maxItems`、`minimum`、`maximum`、`dependentRequired`），Schema 里出现其它关键字会直接报错；服务器端用 `jsonschema`。

需要定义的载荷（由后续故事逐个加入 `schemas/`）：

| 载荷 | 方向 | 建议文件名 |
| --- | --- | --- |
| 收件箱事件 | 服务器 → Mac（Mac 拉取） | `inbox-event.v1.schema.json` |

## `schema_version` 约定

1. 每类载荷的顶层都必须带整数字段 `schema_version`（从 `1` 开始），各类载荷独立计数。
2. 文件名带版本号：`<载荷>.v<schema_version>.schema.json`；Schema 内用 `"const"` 固定该版本号。
3. Mac 发送前按 Schema 校验，不合格不发送。
4. 服务器拒收不认识的 `schema_version` 或不合 Schema 的载荷，并返回错误。
5. 结构变更顺序：
   1. 新增 `v<N+1>` 文件，旧版文件保留；
   2. 先部署能同时读新旧两版的服务器与客人页；
   3. 再让 Mac 发布新版载荷；
   4. 到下一个版本时才移除旧版读取与旧版文件。
6. 快照公共部分只允许 spine 中列出的字段；新增字段须先改该清单与本目录。

## 同步

`contract/`、服务器代码与客人页模板放在同一仓库；部署脚本把同一份 `contract/` 同步到服务器。
