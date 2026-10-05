# 第一期：统一 SQLite 采集、规则筛选与本地审核

新商品统一入库后再判断，规则排除也保留原因；待审核页只展示未审核的要/询市。店主可以写原因和心理价，选择不要必须填写原因。审核追加 cases 并保留当时的规则及价格快照。

- eBay Browse API 首次最多1000条，后续遇到已知ID停止；详情轮询保留下架、改价和降价记录。
- Kleinanzeigen 使用现有curl/HTML解析，拦截后数据库记录暂停两小时。
- 价格与不好卖规则使用空YAML配置；破损例外默认关闭。
- cheap/deep仅本地NoopProvider；零预算不会调用provider，无模型SDK。
- 手机适配审核页，排除/已审核分页、请求幂等、旧价格表单校验。
- launchd 每小时任务，已按后续用户要求安装并实测。

验证：

```text
$ .venv/bin/python -m pytest -q
......................                                                   [100%]
22 passed in 4.30s
JavaScript syntax and launchd plist OK
```

当前是独立本地实现，未对GitHub main创建PR。没有取得main中的26个测试或accept_unified.py及案例，因此无法提供它们的真实输出。原验收目标6/15/0与4/245/40尚未验证，不能据此合并。后续已完成真实采集和 launchd 增量验证，最新结果见 VALIDATION.txt；原仓库基线验收仍未完成。
