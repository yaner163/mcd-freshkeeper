# MCP_INTEGRATION

本项目基于**麦当劳中国官方 MCP 服务**开发。

## 接入信息

| 项 | 值 |
| --- | --- |
| 服务名称 | 麦当劳 MCP 服务（McDonald's China MCP Server） |
| 提供方 | 金拱门（中国）有限公司 |
| 接入地址 | `https://mcp.mcd.cn` |
| 传输协议 | Streamable HTTP |
| MCP 版本 | 2025-06-18 |
| 认证方式 | 请求头 `Authorization: Bearer <MCP_TOKEN>` |
| 速率限制 | 600 次/分钟/Token，超限返回 `429` |
| 服务区域 | 中国大陆地区（不含港澳台） |
| 申请地址 | https://open.mcd.cn/mcp |
| 官方文档 | https://github.com/M-China/mcd-mcp-server |

配置示例见 `mcp-config.example.json`（仅含环境变量占位符，无真实 Token）。

## 实际使用的 MCP Tool

本项目**真实调用**以下 8 个官方 Tool：

| Tool | 用途 | 调用时机 |
| --- | --- | --- |
| `now-time-info` | 获取当前准确时间 | **每次体检第一步**，剩余天数计算基准 |
| `query-my-coupons` | 查询账户全部优惠券 | 临期体检主数据源 |
| `query-my-account` | 查询积分账户信息 | 积分保鲜主数据源 |
| `available-coupons` | 查询当前可领券 | 晨间播报、领券补位 |
| `auto-bind-coupons` | 一键领取全部可用券 | 体检前补领新券 |
| `campaign-calendar` | 查询当月营销活动日历 | 播报、凑单活动叠加判断 |
| `calculate-price` | 商品价格计算（含优惠券） | 用券凑单实算验证 |
| `query-store-coupons` | 查询当前门店可用券 | 凑单前二次确认 |

可选 Tool（在对应场景下由 Skill 调用，不在默认链路）：

| Tool | 触发场景 |
| --- | --- |
| `query-nearby-stores` | 需要给出具体门店建议时 |
| `query-meals` / `query-meal-detail` | 用户问「怎么用掉这张券」时 |
| `mall-points-products` / `mall-product-detail` | 临期积分需要兑换建议时 |
| `delivery-query-addresses` / `delivery-query-stores` | 麦乐送路径 |

**资金类 Tool（`create-order`、`mall-create-order`、`draw-lottery`）本项目不自动调用**，仅在用户显式确认后由 Skill 层触发。

## 调用流程

### 主流程：临期体检

```
用户："体检一下"
  │
  ├─1. now-time-info            → 取准确日期 T（不猜系统时间）
  │
  ├─2. auto-bind-coupons        → 先补领新券（幂等，已领则跳过）
  │
  ├─3. query-my-coupons         → 卡包全量券
  │     query-my-account        → 积分账户
  │
  ├─4. 本地脚本处理（关键：不靠模型心算）
  │     整理为 {"coupons":[...], "points":{...}}
  │     python scripts/coupon_auditor.py --input my_coupons.json
  │     → 按剩余天数分档 P0–P4，生成排序表与逐张建议
  │
  └─5. 输出体检报告 → 用户追问某张券怎么用
        └─→ query-store-coupons → query-meals → calculate-price
```

### 分支流程：积分保鲜

```
用户："积分会过期吗"
  │
  ├─1. query-my-account  → 重点读 expiringPoints，不是 availablePoints
  │
  ├─2. 若有过期积分且 ≤7 天到期
  │     └─→ mall-points-products → mall-product-detail
  │         按「过期积分可换最高价值」排序出方案
  │
  └─3. 停在「出方案」这一步 —— 等用户明确确认后才走 mall-create-order
```

### 晨间播报

```
每天 08:00（WorkBuddy 定时任务）
  │
  ├─ now-time-info
  ├─ available-coupons          → 新可领券
  ├─ query-my-coupons           → 临期预警
  ├─ query-my-account           → 积分临期
  ├─ campaign-calendar          → 仅每月 1 号拉全量，其余用缓存
  └─ coupon_auditor.py          → 汇总播报
```

## 数据流

```
麦当劳 MCP Server (远程托管)
        │ Streamable HTTP + Bearer Token
        ▼
   MCP Client (WorkBuddy / Cursor / Cherry Studio / Trae / VSCode)
        │
        ▼
   SKILL.md 决策层  ── 分档/判断/话术
        │
        ▼
   scripts/coupon_auditor.py  ── 确定性计算（日期差、分档、排序、渲染）
        │
        ▼
   体检报告 / 播报 / 处置建议
```

**分层理由**：日期差计算和分档排序是确定性任务，交给脚本可复现、可测试（30 项断言覆盖）；模型只负责理解意图、组合方案、表达自然语言。这样既避免算错天数，也避免模型幻觉出不存在的券。

## 字段兼容说明（2026-10-10 官方 MCP 实测校准）

MCP 各版本字段名与直觉差别很大，以下均为实测值：

**积分账户**（注意官方拼写 `Mouth`，且是 `Point` 非 `Points`）：

```python
_first(raw, "availablePoint", "availablePoints", "available")          # 可用积分
_first(raw, "accumulativePoint", "cumulativePoints", "totalPoints")    # 累计积分
_first(raw, "frozenPoint", "frozenPoints")                             # 冻结积分
_first(raw, "currentMouthExpirePoint", "expiringPoints")               # 本月将过期
```

> 按 `availablePoints` / `expiringPoints` 这类常规命名去读，会全部读成 0。
> 另有 `nextMouthExpirePoint`（下月将过期）、`expiredPoint`、`usedPoint`、`currency`（值为「麦享会积分」）。
> 新注册账户上述字段全为字符串 `"0"`，属正常，非异常。

**有效期**：实测返回区间 + 时段混合串，必须先剥掉尾部修饰：

```
2026-10-10 00:00-2026-10-16 23:59                     标准区间
2026-10-05 00:00-2026-10-11 23:59 10:00-22:00          区间 + 每日时段
2026-10-11 05:00-2026-10-11 10:29 周日 05:00-10:29     定点券：当日 + 时段 + 星期
```

若只取单个日期解析，会把开始时间的时分秒误当结束日期，导致剩余天数算错。
`parse_validity()` 的处理逻辑：取第一个日期为起点，在后续匹配中找**数值上晚于起点**的日期作终点，尾部剩余部分再提取时段与星期。

**券名/面额/状态**：

```python
_first(item, "name", "couponName", "title", "couponTitle")
_first(item, "faceValue", "price", "amount", "discount")
STATUS_MAP = {"未使用": "UNUSED", "可用": "UNUSED", "已过期": "EXPIRED", ...}
```

## 真实联调记录（2026-10-10）

用真实 Token 完成端到端验证：

| 环节 | 结果 |
| --- | --- |
| `initialize` 握手 | `serverInfo: mcd-mcp 1.0.0`，协议 2025-06-18 |
| `now-time-info` | 返回 `2026-10-10 12:27:02` |
| `query-my-account` | 全新账户，积分全 0 |
| `available-coupons` | 3 类券可领 |
| `auto-bind-coupons` | **一次性领到 6 张，成功 6 失败 0** |
| `query-my-coupons` | 领券后卡包 5 张可用券 |
| `campaign-calendar` | 返回当月活动，含 10-08「麦当劳 X PEACEMINUSONE」联名 |

**这次联调发现并修复了一个会导致误判的缺陷**：脚本原先只按单个日期解析有效期，无法处理带时段限制的定点券。实测数据里 5 张券有 3 张属于定点券（仅特定日期的早餐时段有效）。若不识别，用户会看到「明天到期」但下午去店里发现已失效。现已新增 `parse_validity()` 并补充 12 项实测回归断言，自测从 30 项扩到 **47 项**。

## 业务价值

**用户侧痛点**：麦当劳优惠券有效期普遍为 1–2 天，用户领券后缺少有效期核对环节，导致券到期作废；积分缺少临期提醒，年底静默清零。实测中同一张早餐券按不同日期拆成多张定点发放，进一步放大了「不知道哪天能用」的问题。

**本项目解法**：把「领券」与「管券」打通成闭环 —— 领券后立即纳入到期监控，按剩余天数分档，0–1 天的高危项主动置顶；**对定点券额外标出具体可用日期与时段**，避免无效奔波；同时给出「值不值得跑一趟」的量化判断。

**技术侧价值**：MCP 返回的字段命名不直观（`availablePoint`、`currentMouthExpirePoint`）、有效期是复合串，直接交给模型极易漏读或算错。本项目采用「MCP 负责取数，脚本负责计算，模型负责表达」的分层结构，核心算法有 47 项离线断言保障可复现性，其中 12 项直接用官方实测数据做回归。
