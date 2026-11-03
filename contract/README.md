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
| 发布快照 `snapshot`（AD-5、AD-6、AD-22；字段即 AD-6 白名单） | Mac → 服务器 | `snapshot.v1.schema.json` |
| 隐藏推送 `hide`（含 `action: unhide`；`hide_seq` 与快照 `publish_no` 同一计数器） | Mac → 服务器 | `hide.v1.schema.json` |
| 图片查询（`POST /admin/images/check` 的请求 / 响应，AD-13） | Mac ⇄ 服务器 | `image_check.v1.schema.json` / `image_check_result.v1.schema.json` |
| 图片上传确认（`PUT /admin/images/<name>` 的响应，AD-13） | 服务器 → Mac | `image_ack.v1.schema.json` |

信封的 `payload` 按 `<kind>.v<payload.schema_version>.schema.json` 另行校验；新增载荷类别时同时把它加进信封的 `kind` 枚举。Mac 端用标准库最小校验器（`curio/sync`，只支持 `type`、`required`、`properties`、`additionalProperties`、`const`、`enum`、`pattern`、`minLength`、`maxLength`、`items`、`minItems`、`maxItems`、`minimum`、`maximum`、`dependentRequired`），Schema 里出现其它关键字会直接报错；服务器端用 `jsonschema`。

需要定义的载荷（由后续故事逐个加入 `schemas/`）：

| 载荷 | 方向 | 建议文件名 |
| --- | --- | --- |
| 指令 | Mac → 服务器 | `command.v1.schema.json` |
| 收件箱事件 | 服务器 → Mac（Mac 拉取） | `inbox-event.v1.schema.json` |
| 账号副本 | 服务器 → Mac（Mac 拉取） | `account-copy.v1.schema.json` |

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
