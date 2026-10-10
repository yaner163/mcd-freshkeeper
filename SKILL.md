---
name: mcmen-freshkeeper
description: 麦门保鲜员 —— 麦当劳卡包券与积分的临期管理助手。基于麦当劳中国官方 MCP，扫描卡包券有效期、定位临期积分、按剩余天数分档给出处置方案，并可生成每日临期播报。当用户提到麦当劳、麦门、优惠券过期、积分清零、临期券、卡包清理、今天有什么券要过期了时使用。
---

# 麦门保鲜员

## 这个 Skill 解决什么

用户会领券，但不会管券。券的有效期集中在 1–2 天，没人主动核对，于是到期作废；积分年底静默清零。

本 Skill 负责**领券之后的后半程**：扫描 → 分档 → 出方案 → 盯执行。

## 五种模式

### 🩺 临期体检（默认入口）

用户说"体检"、"看看我的券"、"有什么要过期的"时使用。

流程：
1. `query-my-account` 拿积分账户
2. `query-my-coupons` 拿全部券
3. 把两份数据落成 JSON 交给 `scripts/coupon_auditor.py`
4. 输出排序表 + 逐张建议

执行脚本：
```bash
# 1. 先把 MCP 返回整理成 my_coupons.json
#    {"coupons": [...], "points": {...}}
python scripts/coupon_auditor.py --input my_coupons.json
```

### 🔥 今日清仓

用户说"今天有什么快过期的"、"今晚还能用掉哪些"时使用。

只保留 P0（今天到期）和 P1（明天到期），按面额降序排。对每张券给出**具体怎么用掉**的方案：
- 到店自取 → 调 `query-nearby-stores` 找最近门店，再 `query-meals` 看能不能凑单
- 麦乐送 → 调 `delivery-query-addresses` 确认地址，注意起送价

### 🪙 积分保鲜

用户说"我的积分"、"积分会过期吗"时使用。

1. `query-my-account` 读可用/累计/冻结/即将过期积分
2. 若有过期积分且 7 天内到期 → `mall-points-products` 查可兑换商品
3. 按"过期积分能换到的最高价值"排序推荐
4. **绝不自动下单**。必须先出方案，用户确认后才走 `mall-create-order`

### 📅 晨间播报

用户说"麦麦早报"、"今日播报"时使用。

组合输出：今日活动（`campaign-calendar`）+ 新可领券（`available-coupons`）+ 临期资产预警。

每月 1 号额外拉一次完整活动日历，其余日期沿用缓存，避免浪费配额。

可挂 WorkBuddy 定时任务，每天 08:00 执行。

### 🧮 用掉它

用户说"这张券怎么用"、"券怎么搭配划算"时使用。

1. `query-store-coupons` 看当前门店可用券
2. `query-meals` 拉菜单
3. 组候选组合 → `calculate-price` 实算券后价
4. 按实付升序给 Top 3

## 处置原则（硬规则）

1. **只诊断，不自动花钱。** 下单（`create-order`）、积分兑换（`mall-create-order`）、抽奖（`draw-lottery`）必须用户显式确认，禁止推断式执行。
2. **先本地算，再调接口。** 排序、分档、筛选在脚本里做，不靠模型心算，减少幻觉。
3. **缺有效期就说缺。** 接口没返回日期时明确说"需要人工核对"，不要臆造日期。
4. **低面额别误导。** 面额 < ¥10 的券标注"不值得专门跑一趟"，避免用户为省三块油钱跑一趟。
5. **幂等。** 重复体检不产生副作用；签到/领券类操作重复调用只会返回"今日已领"。
6. **控制配额。** 每 Token 600 次/分钟。批量体检前先看数据量，别在循环里狂调接口。

## 数据字段容错

MCP 各版本字段名有过变化，`coupon_auditor.py` 内置多候选键兼容：

- 券名：`name` / `couponName` / `title` / `couponTitle`
- 有效期：`expireDate` / `expireTime` / `endDate` / `validEndTime`
- 面额：`faceValue` / `price` / `amount` / `discount`
- 状态：`status` / `couponStatus` / `state`（中文英文都归一化）

日期兼容 `2026-10-11`、`2026/10/11`、`20261011`、ISO 时间戳。解析失败返回 `None` 并计入告警，不抛异常。

## 参考文档

- `references/tools.md` —— MCP 工具速查与实测要点
- `references/playbook.md` —— 临期处置决策手册
- `examples/demo.md` —— 典型对话示例

## 边界

- 餐品、价格、库存、供应状态以麦当劳官方渠道实时结果为准
- 输出不构成营养、医疗或其他专业建议
- 支付环节必须跳转官方渠道完成，不代付
