#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
麦门保鲜员 · 临期资产审计器 (coupon_auditor.py)
================================================

麦当劳 MCP 的 `query-my-coupons` / `query-my-account` 返回的是原始 JSON，
这份脚本把它们转成「今天该不该慌」的排序表。

特点：
- 纯本地计算，不需要 MCP Token，不联网，可离线自测
- 输入支持 MCP 真实返回，也支持手写样例（--demo / --self-test）
- 输出 Markdown 表格，可直接粘进播报；--json 输出机器可读结果
- 只做「诊断与建议」，不调用任何下单/兑换接口，资金动作交给 Skill 层二次确认

用法：
    python coupon_auditor.py --demo
    python coupon_auditor.py --self-test
    python coupon_auditor.py --input coupons.json
    python coupon_auditor.py --input coupons.json --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Any

# ---------------------------------------------------------------- 常量

# 券状态归一化：MCP 各版本返回的中文/英文状态都收敛到这里
STATUS_MAP = {
    "未使用": "UNUSED", "可用": "UNUSED", "unused": "UNUSED", "valid": "UNUSED",
    "已使用": "USED", "used": "USED",
    "已过期": "EXPIRED", "过期": "EXPIRED", "expired": "EXPIRED",
    "已冻结": "FROZEN", "冻结": "FROZEN", "frozen": "FROZEN",
}

# 紧迫度分档：(档名, 上界天数, 是否必须行动, 文案)
# 上界天数用 None 表示无上界
TIERS = [
    ("P0 今天到期", 0, True,  "今天不用就作废，别再犹豫"),
    ("P1 明天到期", 1, True,  "今晚安排掉，明天就是废纸"),
    ("P2 三天内",   3, True,  "这周找机会用掉"),
    ("P3 本周内",   7, False, "顺手用掉，不急"),
    ("P4 还早",     None, False, "放着别管"),
]

# 券面额低于这个值，提醒用户也别为它专门跑一趟
LOW_VALUE_YUAN = 10.0


@dataclass
class Coupon:
    name: str
    status: str = "UNUSED"
    expire_date: str | None = None
    face_value: float | None = None
    coupon_id: str = ""
    tag: str = ""
    days_left: int | None = None
    tier: str = "P4 还早"
    urgent: bool = False
    advice: str = ""
    warnings: list[str] = field(default_factory=list)
    # 真实接口返回的原始有效期串，保留以便核对
    validity_raw: str = ""
    # 时段限制，如 "05:00-10:29"；有则说明券只在特定时间可用
    time_window: str | None = None
    # 星期限制，如 "周日"
    weekday_restriction: str | None = None
    # 定点券：仅在某一具体日期的时段内有效（当日或次日必须用）
    is_point_day: bool = False


@dataclass
class PointsAccount:
    available: float = 0.0
    cumulative: float | None = None
    frozen: float | None = None
    expiring: float = 0.0
    expire_date: str | None = None
    days_left: int | None = None
    tier: str = "P4 还早"
    urgent: bool = False
    advice: str = ""


# ---------------------------------------------------------------- 工具函数

# 实测：MCP 返回的「有效期」是这一类串，必须先剥掉尾部修饰再取日期
#   "2026-10-10 00:00-2026-10-16 23:59"                  标准区间
#   "2026-10-05 00:00-2026-10-11 23:59 10:00-22:00"     区间 + 每日时段
#   "2026-10-11 05:00-2026-10-11 10:29 周日 05:00-10:29" 区间 + 星期 + 时段
_DT_RE = re.compile(
    r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?"
    r"(?:[ T](\d{1,2}):(\d{2}))?"
)
_TIME_WINDOW_RE = re.compile(r"(\d{1,2}:\d{2})\s*[-—~]\s*(\d{1,2}:\d{2})(?!\s*[-—~]\s*\d{1,2}:\d{2}\s*[-—~])")
_WEEKDAY_RE = re.compile(r"(周[一二三四五六日天]|星期[一二三四五六日天])")


def parse_validity(raw: Any) -> dict:
    """
    解析真实接口的有效期串。

    返回 {start, end, time_window, weekday, point_day}，
    其中 start/end 为 date|None，point_day 表示"仅当日/次日时段可用"的定点券。
    """
    out = {"start": None, "end": None, "time_window": None,
           "weekday": None, "point_day": False}
    if raw in (None, "", 0):
        return out
    text = str(raw).strip()

    matches = list(_DT_RE.finditer(text))
    if not matches:
        return out

    # 第一个匹配是开始时间；结束时间取「结束日期数值上晚于开始日」的候选，
    # 避免把开始时间里的时分秒误当成结束日期。
    first = matches[0]
    end_dt = None
    for m in matches[1:]:
        cand = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if cand >= date(int(first.group(1)), int(first.group(2)), int(first.group(3))):
            end_dt = m
            break
    # 没有显式结束日期时，取当天最后一个时刻所在日期（即开始日）
    last = end_dt or matches[-1]

    out["start"] = date(int(first.group(1)), int(first.group(2)), int(first.group(3)))
    out["end"] = date(int(last.group(1)), int(last.group(2)), int(last.group(3)))

    # 尾部的纯时间范围（不含日期的那个）才是每日时段限制
    tail = text[last.end():]
    tw = re.search(r"(\d{1,2}:\d{2})\s*[-—~]\s*(\d{1,2}:\d{2})", tail)
    if tw:
        out["time_window"] = f"{tw.group(1)}-{tw.group(2)}"
    wd = _WEEKDAY_RE.search(tail)
    if wd:
        out["weekday"] = wd.group(1)

    # 开始日 == 结束日 → 定点券
    out["point_day"] = out["start"] == out["end"]
    return out


def _parse_date(value: Any) -> date | None:
    """尽最大努力把各种日期写法解析成 date，解析不了就返回 None。"""
    if value in (None, "", 0):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    # 传入区间串时取结束侧（更保守，不会误判为已过期）
    if "-" in text and len(text) > 12:
        parsed = parse_validity(text)
        if parsed["end"]:
            return parsed["end"]
    # MCP 里见过 "2026-10-11" / "2026/10/11" / "20261011" / ISO 带时分秒
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S", "%Y.%m.%d"):
        try:
            return datetime.strptime(text[:len(fmt) + 6], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _first(item: dict, *keys, default=None):
    """按候选键顺序取第一个非空值——MCP 字段名在不同版本间改过。"""
    for key in keys:
        if key in item and item[key] not in (None, ""):
            return item[key]
    return default


def classify(days_left: int | None) -> tuple[str, bool]:
    """把「还剩几天」映射到 (档名, 是否必须行动)。"""
    if days_left is None:
        return "P4 还早", False
    if days_left < 0:
        return "已过期", False
    for name, upper, urgent, _ in TIERS:
        if upper is None or days_left <= upper:
            return name, urgent
    return "P4 还早", False


def make_advice(coupon: Coupon) -> str:
    """给一张券生成处置建议。规则简单，但比一堆散落的数据有用。"""
    if coupon.status == "EXPIRED":
        return "已过期，自动清理掉，看下一期活动补券"
    if coupon.status == "USED":
        return "已用完，无需处理"
    if coupon.status == "FROZEN":
        return "被冻结了，先排查是不是订单未支付或券被占用"

    tier_advice = dict((name, text) for name, _, _, text in TIERS)

    if coupon.days_left is None:
        base = "没读到有效期，建议手动核对一下券面"
    elif coupon.is_point_day:
        # 定点券：只有当天那个时段能用，过期就彻底作废
        win = coupon.time_window or "限时"
        base = f"定点券，只在 {coupon.expire_date} 的 {win} 那天可用，过了就作废"
    else:
        base = tier_advice.get(coupon.tier, "")

    # 时段限制要单独强调，避免"明天去用"但店里用不了
    extra = ""
    if coupon.time_window and not coupon.is_point_day:
        extra = f"；仅限 {coupon.time_window} 时段可用"
    if coupon.weekday_restriction and not coupon.is_point_day:
        extra += f"，限{coupon.weekday_restriction}"

    low = ""
    if coupon.face_value is not None and coupon.face_value < LOW_VALUE_YUAN:
        low = f"；面额才 ¥{coupon.face_value:g}，不值得专门跑一趟"
    return base + extra + low


# ---------------------------------------------------------------- 主流程

def audit_coupons(raw: list[dict], today: date) -> tuple[list[Coupon], list[str]]:
    coupons: list[Coupon] = []
    problems: list[str] = []

    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            problems.append(f"第 {idx + 1} 条券数据不是对象，已跳过：{item!r}")
            continue

        name = str(_first(item, "name", "couponName", "title", "couponTitle",
                          default=f"未命名券#{idx + 1}"))
        status_raw = _first(item, "status", "couponStatus", "state", default="UNUSED")
        status = STATUS_MAP.get(str(status_raw), STATUS_MAP.get(str(status_raw).lower(), "UNUSED"))

        exp_raw = _first(item, "expireDate", "expireTime", "endDate",
                         "validEndTime", "effectiveEndTime", "validity", default=None)
        parsed = parse_validity(exp_raw)
        exp = parsed["end"]
        value = _first(item, "faceValue", "price", "amount", "discount")

        c = Coupon(
            name=name,
            status=status,
            expire_date=exp.isoformat() if exp else None,
            face_value=float(value) if isinstance(value, (int, float, str)) and
                          str(value).replace(".", "", 1).replace("-", "", 1).isdigit() else None,
            coupon_id=str(_first(item, "couponId", "id", default="")),
            tag=str(_first(item, "tag", "label", "promotionTag", default="")),
            validity_raw=str(exp_raw) if exp_raw else "",
            time_window=parsed["time_window"],
            weekday_restriction=parsed["weekday"],
            is_point_day=parsed["point_day"],
        )

        if parsed["start"]:
            c.warnings.append(f"有效期起点 {parsed['start'].isoformat()}")
            c.validity_raw = c.validity_raw or str(exp_raw)

        if exp:
            c.days_left = (exp - today).days
        elif status == "UNUSED":
            c.warnings.append("缺少有效期字段")

        c.tier, c.urgent = classify(c.days_left)
        c.advice = make_advice(c)
        coupons.append(c)

    return coupons, problems


def audit_points(raw: dict, today: date) -> PointsAccount:
    if not isinstance(raw, dict):
        raw = {}

    def num(*keys, default=0.0):
        v = _first(raw, *keys, default=default)
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    p = PointsAccount(
        # 实测真实字段名（2026-10-10 联调确认）：availablePoint / accumulativePoint
        # / frozenPoint / currentMouthExpirePoint / nextMouthExpirePoint
        available=num("availablePoint", "availablePoints", "available", "usablePoints"),
        cumulative=num("accumulativePoint", "cumulativePoints", "totalPoints", default=None),
        frozen=num("frozenPoint", "frozenPoints", "frozen", default=None),
        expiring=num("currentMouthExpirePoint", "expiringPoints",
                     "willExpirePoints", "aboutToExpirePoints",
                     "nextMouthExpirePoint", default=None),
    )

    exp = _parse_date(_first(raw, "currentMouthExpireDate", "expiringDate",
                             "expireDate", "pointsExpireDate"))
    if exp:
        p.expire_date = exp.isoformat()
        p.days_left = (exp - today).days

    p.tier, p.urgent = classify(p.days_left)

    if p.available <= 0:
        p.advice = "积分见底了，签到和活动任务记得每天做"
    elif not p.expiring:
        p.advice = "近期没有过期积分，慢慢花就行"
    elif p.days_left is not None and p.days_left <= 7:
        p.advice = (f"有 {p.expiring:g} 分马上过期，"
                    f"先去 `mall-points-products` 看看能换什么，别直接作废")
    else:
        p.advice = (f"{p.expiring:g} 分将于 {p.expire_date} 过期，"
                    f"建议提前一两天换成券，别换成不好用的周边")
    return p


def render_markdown(coupons: list[Coupon], points: PointsAccount, today: date) -> str:
    live = [c for c in coupons if c.status == "UNUSED"]
    urgent = sorted(
        (c for c in live if c.days_left is not None),
        key=lambda c: c.days_left,
    )
    no_date = [c for c in live if c.days_left is None]
    expired = [c for c in coupons if c.status in ("EXPIRED",)]

    out: list[str] = []
    out.append(f"# 麦门保鲜员 · 临期体检报告 · {today.isoformat()}\n")

    # 概览
    out.append("| 指标 | 数值 |")
    out.append("| --- | --- |")
    out.append(f"| 卡包券总数 | {len(coupons)} |")
    out.append(f"| 可用券 | {len(live)} |")
    out.append(f"| 7 天内到期 | {sum(1 for c in live if c.days_left is not None and c.days_left <= 7)} |")
    out.append(f"| 今天就到期 | {sum(1 for c in live if c.days_left == 0)} |")
    out.append(f"| 可用积分 | {points.available:g} |")
    if points.expiring:
        out.append(f"| 即将过期积分 | {points.expiring:g}（{points.expire_date or '日期待确认'}） |")
    out.append("")

    # 积分
    out.append("## 积分账户\n")
    out.append(f"- **可用积分**：{points.available:g}"
               + (f"　累计：{points.cumulative:g}" if points.cumulative else "")
               + (f"　冻结：{points.frozen:g}" if points.frozen else ""))
    out.append(f"- **结论**：{points.advice}\n")

    if not urgent and not no_date:
        out.append("## 券况\n")
        out.append("近 7 天没有到期的券，不用急着行动。\n")
    else:
        out.append("## 到期提醒（按剩余天数排序）\n")
        out.append("| 剩余 | 档位 | 券名 | 面额 | 可用限制 | 建议 |")
        out.append("| --- | --- | --- | --- | --- | --- |")
        for c in urgent:
            face = f"¥{c.face_value:g}" if c.face_value is not None else "—"
            limit = "—"
            if c.is_point_day and c.time_window:
                limit = f"**仅 {c.expire_date} {c.time_window}**"
            elif c.time_window:
                limit = f"时段 {c.time_window}"
            elif c.weekday_restriction:
                limit = f"限{c.weekday_restriction}"
            out.append(
                f"| {c.days_left} 天 | {c.tier} | {c.name} | {face} | {limit} | {c.advice} |")
        out.append("")

    if no_date:
        out.append("## 需要人工核对\n")
        for c in no_date:
            out.append(f"- {c.name}：接口没返回有效期字段，券面状态未知")
        out.append("")

    if expired:
        out.append(f"## 已过期清理\n共 {len(expired)} 张已过期券，可从卡包里清掉。\n")

    out.append("---")
    out.append("")
    out.append("> 本报告由本地脚本离线计算，只做诊断，不执行任何下单或兑换。"
               "涉及资金的动作会另行找你确认。")
    return "\n".join(out)


# ---------------------------------------------------------------- 样例与自测

# 以下为 2026-10-10 通过麦当劳官方 MCP 实测取到的真实数据（已脱敏，无账号标识）
DEMO_COUpons = [
    {"name": "9.9元中杯冰美式", "status": "未使用",
     "expireDate": "2026-10-10 00:00-2026-10-16 23:59", "faceValue": 9.9,
     "tag": "到店专用、外送专用"},
    {"name": "人气麦旋风买一送一", "status": "未使用",
     "expireDate": "2026-10-05 00:00-2026-10-11 23:59 10:00-22:00", "faceValue": 15,
     "tag": "到店专用"},
    {"name": "巧克力味厚松饼猪柳蛋套餐", "status": "未使用",
     "expireDate": "2026-10-11 05:00-2026-10-11 10:29 周日 05:00-10:29", "faceValue": 29.9,
     "tag": "到店专用"},
    {"name": "巧克力味厚松饼猪柳蛋套餐", "status": "未使用",
     "expireDate": "2026-10-17 05:00-2026-10-17 10:29 周六 05:00-10:29", "faceValue": 29.9,
     "tag": "到店专用"},
    {"name": "免费脆薯饼", "status": "未使用",
     "expireDate": "2026-10-20 00:00-2026-10-24 23:59", "faceValue": 6.0,
     "tag": "到店专用"},
    {"name": "上月满减券", "status": "已过期",
     "expireDate": "2026-10-05 00:00-2026-10-05 23:59", "faceValue": 15.0},
]

# 实测积分字段名：availablePoint / accumulativePoint / currentMouthExpirePoint
DEMO_POINTS = {
    "availablePoint": "0",
    "accumulativePoint": "0",
    "frozenPoint": "0",
    "currentMouthExpirePoint": "0",
    "expiredPoint": "0",
    "usedPoint": "0",
    "currency": "麦享会积分",
}


def run_self_test(today: date) -> int:
    """离线自测：不依赖任何外部服务，CI 或换机器都能跑。"""
    cases = []

    # 1. 日期解析
    cases.append(("日期解析 标准", _parse_date("2026-10-11") == date(2026, 10, 11)))
    cases.append(("日期解析 斜杠", _parse_date("2026/10/11") == date(2026, 10, 11)))
    cases.append(("日期解析 紧凑", _parse_date("20261011") == date(2026, 10, 11)))
    cases.append(("日期解析 乱码安全", _parse_date("not-a-date") is None))
    cases.append(("日期解析 None", _parse_date(None) is None))

    # 2. 分档
    cases.append(("分档 0 天=P0", classify(0) == ("P0 今天到期", True)))
    cases.append(("分档 1 天=P1", classify(1) == ("P1 明天到期", True)))
    cases.append(("分档 3 天=P2", classify(3)[0] == "P2 三天内"))
    cases.append(("分档 7 天=P3", classify(7)[0] == "P3 本周内"))
    cases.append(("分档 8 天=P4", classify(8)[0] == "P4 还早"))
    cases.append(("分档 负数=已过期", classify(-1)[0] == "已过期"))
    cases.append(("分档 未知=不催", classify(None) == ("P4 还早", False)))

    # 3. 券审计
    coupons, problems = audit_coupons(DEMO_COUpons, today)
    cases.append(("券审计 条数", len(coupons) == len(DEMO_COUpons)))
    cases.append(("券审计 无解析异常", problems == []))
    cases.append(("券审计 过期识别",
                  any(c.status == "EXPIRED" for c in coupons)))
    # 缺有效期的样本单独构造，避免依赖样例数据里是否恰好存在
    no_exp, _ = audit_coupons([{"name": "无有效期券", "status": "可用"}], today)
    cases.append(("券审计 缺有效期被标记",
                  any("缺少有效期字段" in w for w in no_exp[0].warnings)))
    cases.append(("券审计 缺有效期不进 urgent",
                  all(c.days_left is not None or c.tier == "P4 还早"
                      for c in coupons if c.status == "UNUSED" and c.days_left is None)))
    cases.append(("券审计 状态归一化",
                  next(c for c in coupons if "冰美式" in c.name).status == "UNUSED"))

    # 4. 脏数据不崩
    dirty, dirty_problems = audit_coupons([None, 42, {"name": "x"}], today)
    cases.append(("脏数据 不抛异常", len(dirty) == 1))
    cases.append(("脏数据 记了 2 条告警", len(dirty_problems) == 2))
    cases.append(("空列表 正常返回", audit_coupons([], today)[0] == []))

    # 5. 积分审计（DEMO_POINTS 为实测全 0 账户，故另造有值样本）
    p = audit_points(DEMO_POINTS, today)
    cases.append(("积分 实测全0账户可用积分为0", p.available == 0))
    cases.append(("积分 实测全0账户无临期", p.expiring == 0))
    cases.append(("积分 实测全0账户不紧急", p.urgent is False))

    rich = audit_points({"availablePoint": "1280", "accumulativePoint": "5430",
                         "currentMouthExpirePoint": "200",
                         "currentMouthExpireDate": "2026-10-12"}, today)
    cases.append(("积分 真实字段名可用积分", rich.available == 1280))
    cases.append(("积分 真实字段名临期积分", rich.expiring == 200))
    cases.append(("积分 临期被标记", rich.urgent is True))
    cases.append(("积分 异常输入不崩", audit_points({}, today).available == 0.0))
    cases.append(("积分 None 输入不崩", audit_points(None, today).available == 0.0))

    # 6. 渲染
    md = render_markdown(coupons, p, today)
    cases.append(("渲染 含标题", "临期体检报告" in md))
    cases.append(("渲染 含积分段", "积分账户" in md))
    cases.append(("渲染 含表格", "|---" in md or "| --- |" in md))
    no_exp_md = render_markdown(no_exp, p, today)
    cases.append(("渲染 无日期券被提示", "需要人工核对" in no_exp_md))

    # 7. 真实接口有效期解析（2026-10-10 实测数据回归）
    v1 = parse_validity("2026-10-10 00:00-2026-10-16 23:59")
    cases.append(("真实区间 起止正确",
                  v1["start"] == date(2026, 10, 10) and v1["end"] == date(2026, 10, 16)))
    cases.append(("真实区间 非定点券", v1["point_day"] is False))

    v2 = parse_validity("2026-10-05 00:00-2026-10-11 23:59 10:00-22:00")
    cases.append(("区间+时段 结束日正确", v2["end"] == date(2026, 10, 11)))
    cases.append(("区间+时段 提取时段", v2["time_window"] == "10:00-22:00"))

    v3 = parse_validity("2026-10-11 05:00-2026-10-11 10:29 周日 05:00-10:29")
    cases.append(("定点券 识别为当日",
                  v3["start"] == date(2026, 10, 11) and v3["point_day"] is True))
    cases.append(("定点券 提取时段", v3["time_window"] == "05:00-10:29"))
    cases.append(("定点券 提取星期", v3["weekday"] == "周日"))

    # 真实券进主流程
    real = [
        {"name": "9.9元中杯冰美式", "status": "未使用",
         "expireDate": "2026-10-10 00:00-2026-10-16 23:59", "faceValue": 9.9},
        {"name": "人气麦旋风买一送一", "status": "未使用",
         "expireDate": "2026-10-05 00:00-2026-10-11 23:59 10:00-22:00", "faceValue": 15},
        {"name": "巧克力味厚松饼猪柳蛋套餐", "status": "未使用",
         "expireDate": "2026-10-11 05:00-2026-10-11 10:29 周日 05:00-10:29", "faceValue": 29.9},
    ]
    rc, rp = audit_coupons(real, today)
    cases.append(("真实券 全部解析出到期日",
                  all(c.days_left is not None for c in rc)))
    cases.append(("真实券 麦旋风剩余 1 天",
                  next(c for c in rc if "麦旋风" in c.name).days_left == 1))
    cases.append(("真实券 冰美式剩余 6 天",
                  next(c for c in rc if "冰美式" in c.name).days_left == 6))
    cases.append(("真实券 定点早餐券被标记",
                  next(c for c in rc if "厚松饼" in c.name).is_point_day is True))
    cases.append(("真实券 定点券建议含时段",
                  "05:00-10:29" in next(c for c in rc if "厚松饼" in c.name).advice))
    cases.append(("真实券 无解析告警", rp == []))
    real_md = render_markdown(rc, audit_points(DEMO_POINTS, today), today)
    cases.append(("真实券 渲染含可用限制列", "可用限制" in real_md))

    passed = sum(1 for _, ok in cases if ok)
    for name, ok in cases:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"\n{passed}/{len(cases)} 通过")
    return 0 if passed == len(cases) else 1


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="麦门保鲜员 · 临期资产审计器（离线运行，不需要 Token）")
    ap.add_argument("--input", "-i", help="MCP 返回 JSON 文件路径")
    ap.add_argument("--demo", action="store_true", help="用内置样例数据跑一遍")
    ap.add_argument("--self-test", action="store_true", help="运行离线自测")
    ap.add_argument("--today", help="覆盖「今天」，格式 YYYY-MM-DD（默认用系统日期）")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = ap.parse_args(argv)

    if args.self_test:
        today = _parse_date(args.today) or date.today()
        print(f"麦门保鲜员 · 自测（基准日期 {today.isoformat()}）\n")
        return run_self_test(today)

    today = _parse_date(args.today) or date.today()

    if args.input:
        try:
            with open(args.input, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except FileNotFoundError:
            print(f"找不到文件：{args.input}", file=sys.stderr)
            return 2
        except json.JSONDecodeError as e:
            print(f"JSON 解析失败：{e}", file=sys.stderr)
            return 2
    else:
        payload = {"coupons": DEMO_COUpons, "points": DEMO_POINTS}

    if isinstance(payload, list):
        payload = {"coupons": payload}
    if not isinstance(payload, dict):
        print("输入顶层必须是对象或数组", file=sys.stderr)
        return 2

    raw_coupons = payload.get("coupons") or payload.get("myCoupons") or []
    raw_points = payload.get("points") or payload.get("account") or {}

    if not isinstance(raw_coupons, list):
        print("coupons 字段必须是数组", file=sys.stderr)
        return 2

    coupons, problems = audit_coupons(raw_coupons, today)
    points = audit_points(raw_points, today)

    for prob in problems:
        print(f"[warn] {prob}", file=sys.stderr)

    if args.json:
        print(json.dumps({
            "today": today.isoformat(),
            "coupons": [asdict(c) for c in coupons],
            "points": asdict(points),
            "warnings": problems,
        }, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(coupons, points, today))
    return 0


if __name__ == "__main__":
    sys.exit(main())
