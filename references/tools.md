# 麦当劳 MCP 工具速查

按本 Skill 的实际用法分组。完整 35 个工具见 [官方文档](https://github.com/M-China/mcd-mcp-server#3-%E5%B7%A5%E5%85%B7%E5%88%97%E8%A1%A8)。

## 连接信息

| 项 | 值 |
| --- | --- |
| 接入地址 | `https://mcp.mcd.cn` |
| 传输协议 | Streamable HTTP |
| 认证 | 请求头 `Authorization: Bearer YOUR_MCP_TOKEN` |
| 速率限制 | 600 次/分钟/Token，超限返回 429 |
| Token 申请 | https://open.mcd.cn/mcp → 手机号登录 → 控制台 → 激活 |

错误码：`401` Token 无效/过期；`429` 触发限流。

## 本 Skill 常用（Tier 1）

| Tool | 用途 | 在哪一步用 |
| --- | --- | --- |
| `query-my-coupons` | 查我的全部优惠券 | 体检主数据源 |
| `query-my-account` | 积分账户（可用/累计/冻结/即将过期） | 积分保鲜主数据源 |
| `available-coupons` | 当前可领的券 | 晨间播报 |
| `auto-bind-coupons` | 一键领光所有可领券 | 体检前先补领 |
| `campaign-calendar` | 当月营销活动日历 | 播报 + 用券决策 |
| `now-time-info` | 当前时间 | **每次先调**，算剩余天数不能猜 |
| `calculate-price` | 商品金额/优惠/应付实算 | 用券凑单验证 |
| `query-store-coupons` | 当前门店可用券 | 凑单时二次确认 |

## 点餐与门店（Tier 2）

| Tool | 用途 |
| --- | --- |
| `query-nearby-stores` | 查附近可用门店 |
| `query-meals` | 当前门店在售餐品列表 |
| `query-meal-detail` | 餐品详情/套餐组成 |
| `list-nutrition-foods` | 营养成分（能量、蛋白、脂肪、碳水、钠、钙） |
| `create-order` / `cancel-order` / `query-order` | 订单操作（**需用户确认**） |
| `delivery-query-addresses` / `delivery-create-address` / `delivery-query-stores` | 麦乐送配送 |

## 积分与商城（Tier 3）

| Tool | 用途 |
| --- | --- |
| `mall-points-products` | 积分可兑商品列表 |
| `mall-product-detail` | 商品详情 |
| `mall-create-order` | 积分兑换下单（**需用户确认**） |
| `mall-order-list` / `mall-order-detail` | 商城订单查询 |
| `query-lottery-info` / `draw-lottery` / `query-my-prizes` | 积分抽奖（`draw-lottery` **需用户确认**） |

## 实测要点

1. **先调 `now-time-info`。** 剩余天数、是否该拉日历都依赖准确日期。不要凭系统时间猜。
2. **有效期是区间串，不是单个日期。** 实测返回形如：
   - `2026-10-10 00:00-2026-10-16 23:59`（标准区间）
   - `2026-10-05 00:00-2026-10-11 23:59 10:00-22:00`（区间 + 每日时段）
   - `2026-10-11 05:00-2026-10-11 10:29 周日 05:00-10:29`（定点券：当日 + 时段 + 星期）

   尾部时段和星期必须剥掉，只取日期，否则会算错剩余天数。
3. **存在"定点券"这种东西。** 实测同一张 ¥29.9 早餐券有 3 张，分别只在 10-11、10-17、10-18 的 05:00–10:29 有效。这类券按"还剩 N 天"提醒会误导人——必须标出**具体日期 + 时段**，否则用户下午去就白跑。
4. **积分字段名有坑。** 实测真实字段是 `availablePoint` / `accumulativePoint` / `frozenPoint` / `currentMouthExpirePoint`（注意是 `Point` 不是 `Points`，且 `Mouth` 是官方拼写错误，不是 `Month`）。按常规命名猜会全部读成 0。
5. **积分账户可能全为 0。** 新注册账户 `availablePoint` / `accumulativePoint` / `currentMouthExpirePoint` 全是 `"0"`（字符串，不是数字）。别把它当异常。
6. **领券要趁早。** `auto-bind-coupons` 是幂等的，早领早安心；同一天重复调只会返回已领。实测一次可领 6 张。
7. **券的有效期普遍偏短。** 实测 5 张券中 3 张有效期只有 1 天。这是本 Skill 存在的理由。
8. **临期积分要看 `currentMouthExpirePoint` 而非 `availablePoint`。** 只看总可用积分会漏掉即将清零的部分。
9. **活动日历每月 1 号更新。** 非 1 号可沿用缓存，省配额。
10. **重复领取的优惠券无效。** 领券结果以 App「我的优惠券」为准。
11. **门店菜单按门店变。** 同一餐品不同门店可能售罄或价格不同，试算前确认门店。
12. **支付必须走官方渠道。** MCP 侧不代付，会返回支付链接。

## 实测发现的联名活动

2026-10-10 拉取活动日历时，发现官方曾在 **10 月 8 日**上线「麦当劳 X PEACEMINUSONE」联名（GD 同款复古棒球帽，任意餐品消费加 39.9 元可得，含 APP 专享积分抢先购）。这类联名靠 `campaign-calendar` 才能发现，本 Skill 的播报模式会覆盖到。

## 字段容错

MCP 各版本字段名随版本变过，本地脚本已按实测值 + 常见变体做多候选兼容：

```python
# 券名
_first(item, "name", "couponName", "title", "couponTitle")
# 有效期（支持区间/时段/星期混合串）
parse_validity(item["expireDate"])
# 积分（实测字段在前，社区常见字段在后）
_first(raw, "availablePoint", "availablePoints", "available", "usablePoints")
_first(raw, "currentMouthExpirePoint", "expiringPoints", "willExpirePoints")
_first(raw, "accumulativePoint", "cumulativePoints", "totalPoints")
```

接入新版本若出现解析不到数据，优先检查这里的候选键，扩展即可，**不要改分档逻辑**。
