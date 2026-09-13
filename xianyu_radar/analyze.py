"""分析引擎：把商品列表变成可决策的结论。

这是本项目的核心。不做花哨的图表，只回答四个能指导决策的问题：

1. 价格分布长什么样？（定多少价）
2. 供给结构是什么？（红海还是蓝海）
3. 哪个价格带供给少需求强？（机会分）
4. 头部是谁，靠什么赢的？（能不能打）

关于「机会分」
    机会分 = 需求强度 / (供给量 + 1)

它不是玄学，就是**需求供给比**。一个价格带如果有很多人在"想要"，
但没几个卖家在卖，那就是空档。反之如果供给多但没人问，就是伪需求。
"""

from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

# ---------------------------------------------------------------- 供给分类

# 供给类型识别规则：(类型名, 正则)
# 顺序有意义——先匹配到的胜出，所以"定制服务"要排在"教程"前面，
# 因为「工作流定制教程」这种标题两种特征都有，但它是服务生意。
SUPPLY_RULES: list[tuple[str, str]] = [
    ("定制/服务", r"定制|代做|接单|代运营|帮做|外包|代写|代画|搭建|部署|实施"
                  r"|落地|陪跑|代采|数据处理服务|咨询服务|指导|维护|代跑|私有化"),
    ("账号/卡密", r"账号|小号|卡密|会员|解锁|破解|订阅|代充|充值|激活码"
                  r"|独享|共享号|首充|首次码|年费|永久会员"),
    ("工具/软件", r"软件|工具|神器|系统|脚本|插件|客户端|安装包|一键"
                  r"|本地部署|绿色版|永久版|源码|exe|app"),
    ("教程/资料", r"教程|课程|资料|合集|素材|网盘|电子版|文档|笔记|训练营"
                  r"|教学|课件|pdf|电子书|模板包|案例库|手册|学习|全套|秒发|自动发货"),
]

# 参数化关键词：区分泛词与精准词，用于判断搜索意图
INTENT_BOOSTERS = ("落地", "定制", "代做", "外包", "实施", "搭建", "部署", "接单")
INTENT_GENERIC = ("ai", "ai工具", "自动化", "效率", "工作流")


@dataclass
class Sentiment:
    """供给结构统计。"""

    counts: Counter = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def ratio(self, kind: str) -> float:
        return self.counts.get(kind, 0) / self.total if self.total else 0.0

    def dominant(self) -> tuple[str, float]:
        if not self.counts:
            return ("", 0.0)
        k, n = self.counts.most_common(1)[0]
        return (k, n / self.total if self.total else 0.0)


def classify_supply(title: str) -> str:
    """判断一条商品属于哪种供给类型。"""
    t = (title or "").lower()
    for name, pat in SUPPLY_RULES:
        if re.search(pat, t):
            return name
    return "其他"


def classify_sentiment(titles: Iterable[str]) -> Sentiment:
    s = Sentiment()
    for t in titles:
        s.counts[classify_supply(t)] += 1
    return s


# ---------------------------------------------------------------- 价格带

@dataclass
class Band:
    """一个价格带的供给与需求统计。"""

    low: float
    high: float
    count: int = 0
    want_total: int = 0
    want_median: float = 0.0
    samples: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        if self.high >= 1e8:
            return f"≥{self.low:g}"
        return f"{self.low:g}-{self.high:g}"

    @property
    def opportunity(self) -> float:
        """机会分 = 需求强度 / (供给量 + 1)。"""
        demand = self.want_median if self.want_median else (self.want_total / max(self.count, 1))
        return round(demand / (self.count + 1), 2)


DEFAULT_BANDS: list[tuple[float, float]] = [
    (0, 5), (5, 10), (10, 30), (30, 50),
    (50, 100), (100, 200), (200, 500), (500, 1e9),
]


def build_bands(rows: Sequence[Any], bands: Sequence[tuple[float, float]] = DEFAULT_BANDS,
                ) -> list[Band]:
    out: list[Band] = []
    for low, high in bands:
        b = Band(low, high)
        for r in rows:
            p = _get(r, "price")
            if p is None:
                continue
            p = float(p)
            if low <= p < high:
                b.count += 1
                b.want_total += int(_get(r, "want_num") or 0)
                if len(b.samples) < 5:
                    b.samples.append(str(_get(r, "title") or "")[:60])
        if b.count:
            wants = [int(_get(r, "want_num") or 0) for r in rows
                     if _get(r, "price") is not None
                     and low <= float(_get(r, "price")) < high]
            b.want_median = statistics.median(wants) if wants else 0.0
        out.append(b)
    return out


# ---------------------------------------------------------------- 总览

@dataclass
class Overview:
    n: int = 0
    price_median: float = 0.0
    price_mean: float = 0.0
    price_p25: float = 0.0
    price_p75: float = 0.0
    price_min: float = 0.0
    price_max: float = 0.0
    price_stdev: float = 0.0
    want_median: float = 0.0
    want_total: int = 0
    free_ship_ratio: float = 0.0
    bands: list[Band] = field(default_factory=list)
    sentiment: Sentiment = field(default_factory=Sentiment)
    top_wanted: list[Any] = field(default_factory=list)
    top_sellers: list[tuple[str, int, float]] = field(default_factory=list)
    areas: list[tuple[str, int]] = field(default_factory=list)
    anomalies: list[Any] = field(default_factory=list)
    credibility: dict[str, float] = field(default_factory=dict)

    @property
    def best_band(self) -> Band | None:
        """机会分最高的价格带（要求有实际需求）。"""
        cands = [b for b in self.bands if b.count and b.want_total > 0]
        return max(cands, key=lambda b: b.opportunity) if cands else None

    @property
    def crowded(self) -> bool:
        """是否红海：最大价格带的供给占比超过 50%。"""
        if not self.bands:
            return False
        total = sum(b.count for b in self.bands) or 1
        return max(b.count for b in self.bands) / total > 0.5


def analyze(rows: Sequence[Any]) -> Overview:
    """对一批商品做完整分析。rows 可以是 sqlite3.Row 或 dict 或 Item。"""
    o = Overview(n=len(rows))
    if not rows:
        return o

    prices = sorted(float(_get(r, "price")) for r in rows if _get(r, "price"))
    wants = [int(_get(r, "want_num") or 0) for r in rows]

    if prices:
        o.price_median = statistics.median(prices)
        o.price_mean = statistics.mean(prices)
        o.price_min, o.price_max = prices[0], prices[-1]
        o.price_stdev = statistics.pstdev(prices) if len(prices) > 1 else 0.0
        if len(prices) >= 4:
            o.price_p25 = statistics.quantiles(prices, n=4)[0]
            o.price_p75 = statistics.quantiles(prices, n=4)[2]
        else:
            o.price_p25, o.price_p75 = prices[0], prices[-1]

    if wants:
        o.want_median = statistics.median(wants)
        o.want_total = sum(wants)

    ships = [1 for r in rows if _get(r, "free_ship")]
    o.free_ship_ratio = len(ships) / len(rows)

    o.bands = build_bands(rows)
    o.sentiment = classify_sentiment([str(_get(r, "title") or "") for r in rows])

    o.top_wanted = sorted(rows, key=lambda r: -(int(_get(r, "want_num") or 0)))[:10]

    sc: dict[str, list] = defaultdict(list)
    for r in rows:
        name = str(_get(r, "seller") or "")
        if name:
            sc[name].append(r)
    o.top_sellers = sorted(
        ((n, len(v),
          statistics.median([float(_get(x, "price") or 0) for x in v]))
         for n, v in sc.items() if len(v) >= 2),
        key=lambda x: -x[1])[:8]

    ac: Counter = Counter()
    for r in rows:
        a = str(_get(r, "area") or "").strip()
        if a:
            ac[re.sub(r"^[^\u4e00-\u9fa5]+", "", a)] += 1
    o.areas = ac.most_common(10)

    # 异常低价：低于 P25 的一半，可能是标价陷阱
    if prices and o.price_p25:
        floor = o.price_p25 * 0.5
        o.anomalies = [r for r in rows
                       if _get(r, "price") and float(_get(r, "price")) < floor][:10]

    # 供给成熟度：有明确"凭证"描述的比例
    cred_pat = re.compile(r"我|本人|大厂|博主|总监|年经验|万粉|团队|认证|经历|案例|服务过|从业|专业")
    with_cred = sum(1 for r in rows if cred_pat.search(str(_get(r, "title") or "")))
    o.credibility = {
        "with_credential_ratio": round(with_cred / len(rows), 3),
        "seller_concentration": round(
            len(o.top_sellers) / max(len(sc), 1), 3),
    }
    return o


# ---------------------------------------------------------------- 报告

def render_report(keyword: str, o: Overview, meta: dict[str, Any] | None = None) -> str:
    """生成 Markdown 报告。"""
    meta = meta or {}
    L: list[str] = []
    L.append(f"# 闲鱼行情雷达：{keyword}\n")
    L.append(f"> 样本 **{o.n}** 条"
             + (f" · 批次 `{meta.get('batch_id')}`" if meta.get("batch_id") else "")
             + (f" · 抓取于 {meta.get('created_at')}" if meta.get("created_at") else ""))
    L.append("")

    if not o.n:
        L.append("没有数据。请先运行 `search`。")
        return "\n".join(L)

    # 1 价格
    L.append("## 1 价格概况\n")
    L.append("| 指标 | 数值 |")
    L.append("|---|---|")
    for k, v in [("中位数", o.price_median), ("平均数", o.price_mean),
                 ("P25", o.price_p25), ("P75", o.price_p75),
                 ("最低", o.price_min), ("最高", o.price_max),
                 ("标准差", o.price_stdev)]:
        L.append(f"| {k} | {v:,.2f} |")
    L.append("")

    # 2 供给结构
    L.append("## 2 供给结构（你在跟谁竞争）\n")
    s = o.sentiment
    L.append("| 供给类型 | 数量 | 占比 |")
    L.append("|---|---|---|")
    for k, n in s.counts.most_common():
        L.append(f"| {k} | {n} | {n/s.total*100:.1f}% |")
    dom, ratio = s.dominant()
    L.append("")
    L.append(f"**主导供给**：{dom}（{ratio*100:.0f}%）")
    if s.ratio("定制/服务") < 0.15:
        L.append(f"\n> 💡 服务型供给只占 **{s.ratio('定制/服务')*100:.1f}%**，"
                 f"大部分人在卖资料/账号/工具。**卖「帮你做完」的空间比卖「东西」大。**")
    L.append("")

    # 3 机会分
    L.append("## 3 价格带机会分 ⭐ 本报告的核心\n")
    L.append("> `机会分 = 需求强度 / (供给量 + 1)` — 高分代表**有人在找但没人在卖**\n")
    L.append("| 价格带 | 供给 | 占比 | 累计想要 | 中位想要 | **机会分** |")
    L.append("|---|---|---|---|---|---|")
    total_supply = sum(b.count for b in o.bands) or 1
    ranked = sorted([b for b in o.bands if b.count],
                    key=lambda b: -b.opportunity)
    for b in o.bands:
        if not b.count:
            continue
        star = " ⭐" if b is o.best_band else ""
        L.append(f"| {b.label} | {b.count} | {b.count/total_supply*100:.0f}% "
                 f"| {b.want_total} | {b.want_median:.0f} | **{b.opportunity}**{star} |")
    if o.best_band:
        bb = o.best_band
        L.append("")
        L.append(f"**最佳机会：{bb.label} 元**（机会分 {bb.opportunity}，"
                 f"供给仅 {bb.count} 条，累计 {bb.want_total} 人想要）")
    empty = [b.label for b in o.bands if not b.count]
    if empty:
        L.append(f"\n**空白价格带**（无人供给）：{', '.join(empty)}")
    if o.crowded:
        L.append("\n⚠️ 供给高度集中在单一价格带，这是**红海**特征，"
                 "建议错位定价而不是贴着打。")
    L.append("")

    # 4 头部
    L.append("## 4 需求头部（谁在赢，靠什么赢）\n")
    cred_pat = re.compile(r"我|本人|大厂|博主|总监|年经验|万粉|团队|认证|经历|案例|从业|专业")
    L.append("| 想要 | 价格 | 有个人凭证 | 标题 |")
    L.append("|---|---|---|---|")
    for r in o.top_wanted:
        title = str(_get(r, "title") or "")[:44]
        cred = "✅" if cred_pat.search(title) else "—"
        L.append(f"| {int(_get(r,'want_num') or 0)} | "
                 f"{_get(r,'price')} | {cred} | {title} |")
    cred_ratio = o.credibility.get("with_credential_ratio", 0)
    L.append("")
    if cred_ratio >= 0.3:
        L.append(f"**{cred_ratio*100:.0f}%** 的商品在卖点里带个人凭证"
                 f"（“我做过/我是…”）。**这个赛道吃信任，纯功能描述会输。**")
    else:
        L.append(f"只有 {cred_ratio*100:.0f}% 带个人凭证。"
                 f"**信任竞争还没开始——先建立凭证的能吃到溢价。**")
    L.append("")

    # 5 卖家
    if o.top_sellers:
        L.append("## 5 批量铺货卖家（≥2 件同词）\n")
        L.append("| 卖家 | 挂牌数 | 中位价 |")
        L.append("|---|---|---|")
        for n, c, m in o.top_sellers:
            L.append(f"| {n[:18]} | {c} | {m:.2f} |")
        L.append("\n> 挂牌数是**同质化信号**：一个人挂很多条，说明他在测款/铺量，"
                 "而不是靠单品打爆。这种对手通常不打价格战，靠数量。\n")

    # 6 其他
    L.append("## 6 其他\n")
    if o.areas:
        L.append("**地区分布**：" + " · ".join(f"{a} {c}" for a, c in o.areas))
    L.append(f"\n**包邮比例**：{o.free_ship_ratio*100:.0f}%")
    L.append(f"**想要数中位数**：{o.want_median:.0f}（需求热度基准线）")
    if o.anomalies:
        L.append(f"\n### ⚠️ 异常低价 {len(o.anomalies)} 条")
        L.append("低于 P25 一半，可能是标价陷阱（低价引流后加价），交易前要求说明实价：\n")
        for r in o.anomalies[:5]:
            L.append(f"- ¥{_get(r,'price')} {str(_get(r,'title') or '')[:50]}")
    L.append("")

    # 7 结论
    L.append("## 7 结论与建议\n")
    L.extend(_conclusions(keyword, o))
    return "\n".join(L)


def _conclusions(keyword: str, o: Overview) -> list[str]:
    """把统计翻译成可执行建议。"""
    out: list[str] = []
    s = o.sentiment
    dom, ratio = s.dominant()

    if ratio > 0.6:
        out.append(f"1. **{dom}占 {ratio*100:.0f}%，是绝对红海。** 不要正面进入，"
                   f"要么做差异化（服务化/垂直化），要么换词。")
    else:
        out.append(f"1. 供给比较分散（最大一类仅 {ratio*100:.0f}%），"
                   f"还有细分空间。")

    if o.best_band:
        bb = o.best_band
        out.append(f"2. **定价建议：{bb.label} 元。** 机会分 {bb.opportunity} 是当前最高"
                   f"——供给只有 {bb.count} 条，而累计 {bb.want_total} 人想要。")

    if s.ratio("定制/服务") < 0.15:
        out.append(f"3. **服务化机会明显**：只有 {s.ratio('定制/服务')*100:.1f}% 在卖"
                   f"“帮你做完”。资料/账号是零边际成本生意，你打不过；"
                   f"交付人力是别人不愿做的，那才是你的位置。")

    cred_ratio = o.credibility.get("with_credential_ratio", 0)
    if cred_ratio < 0.35:
        out.append(f"4. **先用凭证撬动**：只有 {cred_ratio*100:.0f}% 卖家有个人背书。"
                   f"把你真实的经历/案例写进标题，转化率的杠杆在这里，不在价格。")
    else:
        out.append(f"4. 竞品普遍带凭证（{cred_ratio*100:.0f}%），"
                   f"你也必须有，否则纯功能描述会被比下去。")

    if o.want_median and o.price_median:
        out.append(f"5. 参考基线：这个关键词的**中位价 ¥{o.price_median:.0f}**、"
                   f"**中位想要 {o.want_median:.0f}**。低于这条线的价格不值得做。")

    return out


# ---------------------------------------------------------------- 工具

def _get(row: Any, key: str, default: Any = None) -> Any:
    """从 sqlite3.Row / dict / dataclass 中统一取值。"""
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, default)
