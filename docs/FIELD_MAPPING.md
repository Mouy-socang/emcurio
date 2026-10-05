# 当前输出字段与商品 V2 规范

当前数据分三层：来源挂单、试筛结果、经过核验的正式商品档案。不能将前两层直接当作第三层。75字段标准见 `product-fields-v2.json`；不是当前数据库已实现的75个字段。

## 当前来源挂单（items）

| 字段 | 实际含义 / 整理规则 |
|---|---|
| item_id、platform | 平台挂单身份；Kleinanzeigen编号加 ka:，不是正式 Product 编号 |
| title、description | 来源标题和已取得的描述；当前未另建 raw_title/raw_description 列，迁移时应原样保留 |
| url、image | 原链接、预览图URL；不代表照片已下载或核验 |
| listed_price、currency | 当前标价及币种；拍卖是当前竞价 |
| total_price | 按现有手续费配置计算的总价，不含未知运税，不等于实际采购成本 |
| buying、price_text | 交易类型和原报价文字；VB允许议价，无数值时不能当作免费 |
| created、ends | 来源上架与结束时间；不是瓷器生产年代 |
| country | 物品所在地；不能映射为 country_of_origin |
| condition | 来源陈述；不能直接赋予正式品相等级 |
| first_seen、last_seen、checked_at | 本机发现/更新/检查时间，不保证实时在售 |
| status、gone_at | active / gone / archived；消失或归档不等于已成交 |
| agent_decision、agent_reason、fair_price、rule_version | 规则输出；与人工商品资料核验状态分开 |

## 当前试筛输出

每条结果包含 item（挂单快照）、candidate_attributes、attributes_status、bucket、priority_score、reason、matched_customers、evidence、evidence_count、owner_decision、suggested_purchase_price 和 missing。

| 候选字段 | 将来核验后的去向 |
|---|---|
| maker | 有证据确认后才写正式 maker，不由花样反推品牌 |
| form | 只是粗器型标签；需确定销售单位、主体与成员，再映射 object_type / components |
| decor | 当前混合系列、图案、颜色、形制线索；需分拆到 series_name、pattern_name、motifs、form_styles、ground_color 等，不能直接整列复制 |
| qualifiers | 等级、形状、大小、用途等陈述；逐项核验，不能由“一等”写当前品相1 |
| sizes_cm | 正则提取的尺寸字符串；未知是高、直径还是宽时不能机械分配到正式尺寸字段 |

证据保存类别、客户编号、原文、日期、来源路径、消息或商品编号、匹配属性、未满足要求和局限。它们仍不等于完整 ProductInference：现阶段尚未实现逐属性 evidence_ids、核验人、置信度和核验时间的完整闭环。审核页面的 owner_decision 与商品资料 review_status 也不是同一字段。

## V2 必须遵守的整理原则

1. Product 是一个独立销售单位；组件、订单数量、采购费用分别记录。平台编号进入 external_refs，不替代 product_id，也不按标题去重实物。
2. 原始标题和描述始终保留。卖家陈述和机器提取留在候选层，确认前不进入正式标题。
3. 厂家等级、当前品相、底款观察独立；制造者、装饰者、外画归属独立。四杠不能无条件换算某个等级，外画不属于工艺。
4. sale_piece_count 是业务完整器物数；physical_piece_count 是完整组件树的叶节点计数；service_count 是人数；Order.quantity 是购买的销售单位数。四者不互相机械推算。
5. 登记完整、原套完整、原配关系分别判断。组件不同年代，不生成整组统一年份；缺盖改用途不改变原器型。
6. null 表示未知；有 unknown 枚举用 unknown。列表 [] 和布尔 false 要有确认依据，未检查不能代表无损无修。
7. 金边为真必须有鎏金为真；已知修复不评当前品相1至3；主体立体不自动等于浮雕。
8. 标价、采购成本、目标售价、实际成交价分开，金额带币种，历史汇率带日期与来源。标准售价不代替实际成交。
9. 标题顺序：年代 → 品牌 → 厂家等级 → 主装饰/主体 → 用途与器型 → 规格/组成 → 必要状态说明。缺器型可保留工作标题，但不能通过正式上架标题校验。
10. 缺盖、混配、重要损伤和修复必须披露；客户姓名、地址、物流和交易备注不进入商品标题。事实变化后重新预览，保留人工确认过程。

## 未完成的迁移

尚未把所有挂单转换为75字段 Product，没有稳定的完整受控词表、组件数量计算器或正式标题校验器。这里保存目标规范和映射约束，避免把部分文字分类误说成已完成商品建档。
