"""分析引擎的单元测试。这些是项目的核心逻辑，必须有测试覆盖。"""

from __future__ import annotations

import pytest

from xianyu_radar.analyze import (
    Band,
    analyze,
    build_bands,
    classify_sentiment,
    classify_supply,
    render_report,
)
from xianyu_radar.store import Item


def mk(title="测试商品", price=10.0, want=0, seller="卖家A", area="北京",
       free_ship=True, credit=5):
    return Item(item_id=str(hash((title, price, seller)) % 10**10), title=title,
                price=price, want_num=want, seller=seller, area=area,
                free_ship=free_ship, credit_level=credit)


# ------------------------------------------------------------ 供给分类

@pytest.mark.parametrize("title,expected", [
    ("工作流自动化定制开发", "定制/服务"),
    ("Python爬虫接单服务", "定制/服务"),
    ("代做PPT 接单", "定制/服务"),
    ("影刀RPA创业版账号 小号", "账号/卡密"),
    ("爱笔思画X永久会员解锁", "账号/卡密"),
    ("Excel自动化工具 软件", "工具/软件"),
    ("剪映教程 全套课程 网盘", "教程/资料"),
    ("二手手机 九成新", "其他"),
    ("", "其他"),
])
def test_classify_supply(title, expected):
    assert classify_supply(title) == expected


def test_service_beats_tutorial_when_both_present():
    """「工作流定制教程」两种特征都有，但它是服务生意，应归为服务。"""
    assert classify_supply("工作流定制教程") == "定制/服务"


def test_classify_sentiment_ratio():
    s = classify_sentiment([
        "定制开发服务", "代做接单", "搭建部署",     # 3 服务
        "教程合集", "课程资料", "网盘秒发",         # 3 教程
        "普通二手物品",                              # 1 其他
    ])
    assert s.total == 7
    assert s.counts["定制/服务"] == 3
    assert s.counts["教程/资料"] == 3
    assert s.ratio("定制/服务") == pytest.approx(3 / 7)
    assert s.dominant()[0] in ("定制/服务", "教程/资料")


# ------------------------------------------------------------ 价格带与机会分

def test_build_bands_buckets_correctly():
    rows = [mk(price=1), mk(price=3), mk(price=7), mk(price=15), mk(price=80)]
    bands = build_bands(rows)
    got = {b.label: b.count for b in bands if b.count}
    assert got["0-5"] == 2
    assert got["5-10"] == 1
    assert got["10-30"] == 1
    assert got["50-100"] == 1


def test_opportunity_score_formula():
    """机会分 = 中位想要 / (供给 + 1)。"""
    b = Band(0, 10, count=4, want_total=100, want_median=25.0)
    assert b.opportunity == pytest.approx(25.0 / 5)


def test_opportunity_prefers_scarce_high_demand():
    """供给少、需求高的价格带，机会分应高于供给多、需求低的。"""
    scarce = Band(0, 10, count=2, want_total=200, want_median=100.0)
    crowded = Band(10, 20, count=50, want_total=50, want_median=1.0)
    assert scarce.opportunity > crowded.opportunity


def test_best_band_ignores_zero_demand():
    """没有需求的价格带不该被选为最佳机会。"""
    rows = [mk(price=1, want=0) for _ in range(5)] + [mk(price=60, want=50)]
    o = analyze(rows)
    assert o.best_band is not None
    assert o.best_band.want_total > 0


# ------------------------------------------------------------ 总览统计

def test_analyze_basic_stats():
    rows = [mk(price=p, want=w) for p, w in
            [(1, 0), (2, 10), (3, 20), (4, 30), (100, 40)]]
    o = analyze(rows)
    assert o.n == 5
    assert o.price_median == 3
    assert o.price_min == 1
    assert o.price_max == 100
    assert o.want_median == 20
    assert o.want_total == 100


def test_analyze_handles_none_prices():
    """价格缺失不应导致崩溃。"""
    rows = [mk(price=None), mk(price=10), mk(price=None)]
    o = analyze(rows)
    assert o.n == 3
    assert o.price_median == 10


def test_analyze_empty():
    o = analyze([])
    assert o.n == 0
    assert o.best_band is None
    assert "没有数据" in render_report("空", o)


def test_crowded_detection():
    """单一价格带占比 >50% 判定为红海。"""
    many = [mk(price=3) for _ in range(20)]
    few = [mk(price=200) for _ in range(2)]
    assert analyze(many + few).crowded is True

    spread = [mk(price=1), mk(price=20), mk(price=100), mk(price=400)]
    assert analyze(spread).crowded is False


def test_top_sellers_requires_multiple_listings():
    rows = [mk(seller="铺货王", price=5) for _ in range(3)] + [mk(seller="散户", price=9)]
    o = analyze(rows)
    names = [s[0] for s in o.top_sellers]
    assert "铺货王" in names
    assert "散户" not in names     # 只挂 1 条不算铺货


def test_anomaly_detection_flags_suspicious_low():
    rows = [mk(price=100) for _ in range(6)] + [mk(price=1)]
    o = analyze(rows)
    assert any(r.price == 1 for r in o.anomalies)


def test_credibility_ratio():
    rows = [mk(title="大厂产品总监定制开发服务"),
            mk(title="普通定制开发服务"),
            mk(title="本人从业十年接单"),
            mk(title="代做服务")]
    o = analyze(rows)
    assert 0 < o.credibility["with_credential_ratio"] < 1


# ------------------------------------------------------------ 报告渲染

def test_report_contains_sections():
    rows = [mk(title="定制服务A", price=1, want=100),
            mk(title="教程合集", price=2, want=5),
            mk(title="工具软件", price=80, want=3)]
    md = render_report("测试词", analyze(rows))
    for section in ["# 闲鱼行情雷达：测试词", "## 1 价格概况", "## 2 供给结构",
                    "## 3 价格带机会分", "## 4 需求头部", "## 7 结论与建议"]:
        assert section in md


def test_report_flags_service_gap():
    """服务型占比低时，报告应给出服务化建议。"""
    rows = [mk(title="教程合集") for _ in range(10)] + [mk(title="定制服务")]
    md = render_report("测试", analyze(rows))
    assert "服务化机会明显" in md or "服务型供给只占" in md


def test_report_shows_opportunity_star():
    rows = [mk(price=1, want=1) for _ in range(10)] + [mk(price=80, want=99)]
    md = render_report("测试", analyze(rows))
    assert "⭐" in md


# ------------------------------------------------------------ 数据模型

def test_item_price_or_zero():
    assert mk(price=None).price_or_zero == 0.0
    assert mk(price=12.5).price_or_zero == 12.5


def test_item_row_roundtrip():
    it = mk(title="甲", price=3.5)
    row = it.to_row("batch1", "kw1")
    assert row[0] == it.item_id
    assert row[1] == "batch1"
    assert row[2] == "kw1"
    assert row[4] == 3.5
