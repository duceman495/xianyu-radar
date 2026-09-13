"""解析与存储层测试。用合成响应，不联网。

响应结构参考真实的 `mtop.taobao.idlemtopsearch.pc.search` 返回：
标题在 richTitle 的 Text 片段里，想要数/信用在 fishTags 的中文标签里，
结构化字段在 clickParam.args 里。
"""

from __future__ import annotations

import pytest

from xianyu_radar.mtop import ParseError
from xianyu_radar.spider import (
    CREDIT_LEVELS,
    parse_card,
    parse_search_response,
)
from xianyu_radar.store import Item, Store


# ------------------------------------------------------------ 构造器

def make_card(
    item_id="123456",
    title="测试商品标题",
    price="99",
    want="511人想要",
    credit="卖家信用极好",
    area="上海",
    seller="某卖家",
    seller_id="seller_abc",
    free_ship=True,
    publish_ms=1788772849000,
    extra_tags=None,
):
    """构造一张符合真实结构的卡片。"""
    tag_list = []
    if want:
        tag_list.append({"data": {"content": want, "labelId": "9"}})
    if credit:
        tag_list.append({"data": {"content": credit, "labelId": "919"}})
    if free_ship:
        tag_list.append({"data": {"content": "freeShippingIcon",
                                  "url": "https://gw.alicdn.com/x.png"}})
    for t in (extra_tags or []):
        tag_list.append({"data": {"content": t}})

    return {"data": {"item": {"main": {
        "clickParam": {"args": {
            "item_id": item_id, "id": item_id,
            "price": price, "displayPrice": price,
            "wantNum": "", "seller_id": seller_id,
            "publishTime": str(publish_ms), "tag": "freeship" if free_ship else "",
        }},
        "exContent": {
            "richTitle": [
                {"type": "Image", "data": {"url": "https://x.png", "width": 29.7}},
                {"type": "Text", "data": {"text": title}},
            ],
            "fishTags": {"r3": {"tagList": tag_list}},
            "userNickName": seller,
            "area": area,
        },
        "targetUrl": f"https://www.goofish.com/item?id={item_id}",
    }}}}


# ------------------------------------------------------------ 新版结构解析

def test_parse_new_structure():
    items = parse_search_response({"resultList": [make_card()]})
    assert len(items) == 1
    it = items[0]
    assert it.item_id == "123456"
    assert it.title == "测试商品标题"          # 取自 richTitle 的 Text 段
    assert it.price == 99.0
    assert it.want_num == 511
    assert it.credit_level == 7                # "信用极好" -> 7
    assert it.area == "上海"
    assert it.seller == "某卖家"
    assert it.free_ship is True
    assert it.publish_time == "2026-09-07"     # 毫秒时间戳转换
    assert it.url.endswith("id=123456")


def test_title_ignores_image_segments():
    """标题只取 Text 段，不应把图片 URL 混进去。"""
    card = make_card(title="真正的标题")
    items = parse_search_response({"resultList": [card]})
    assert items[0].title == "真正的标题"
    assert "alicdn" not in items[0].title


def test_price_from_richtext_when_args_missing():
    """args 里没价格时，从 price 富文本数组拼。"""
    card = make_card()
    main = card["data"]["item"]["main"]
    main["clickParam"]["args"].pop("price")
    main["clickParam"]["args"].pop("displayPrice")
    main["exContent"]["price"] = [{"type": "sign", "text": "¥"},
                                  {"type": "integer", "text": "1"},
                                  {"type": "decimal", "text": ".99"}]
    assert parse_search_response({"resultList": [card]})[0].price == 1.99


def test_want_from_tag_when_args_empty():
    card = make_card(want="3,141人想要")
    main = card["data"]["item"]["main"]
    main["clickParam"]["args"]["wantNum"] = ""
    assert parse_search_response({"resultList": [card]})[0].want_num == 3141


def test_want_prefers_args_over_tag():
    card = make_card(want="99人想要")
    card["data"]["item"]["main"]["clickParam"]["args"]["wantNum"] = "5000"
    assert parse_search_response({"resultList": [card]})[0].want_num == 5000


@pytest.mark.parametrize("text,expected", [
    ("卖家信用极好", 7), ("卖家信用优秀", 6), ("卖家信用良好", 5),
    ("百分百好评", None), ("未知标签", None),
])
def test_credit_level_mapping(text, expected):
    card = make_card(credit=text)
    assert parse_search_response({"resultList": [card]})[0].credit_level == expected


def test_credit_missing_when_no_tag():
    card = make_card(credit=None)
    assert parse_search_response({"resultList": [card]})[0].credit_level is None


def test_target_url_fallback_for_id():
    card = make_card()
    main = card["data"]["item"]["main"]
    main["clickParam"]["args"].pop("item_id")
    main["clickParam"]["args"].pop("id")
    main["exContent"].pop("itemId", None)
    assert parse_search_response({"resultList": [card]})[0].item_id == "123456"


# ------------------------------------------------------------ 兼容旧结构

def test_parse_legacy_flat_card():
    """老版本把字段直接放在 cardData 上。"""
    card = {"cardData": {"itemId": "999", "title": "旧结构商品",
                         "price": 12.5, "wantNum": 33, "area": "北京"}}
    items = parse_search_response({"resultList": [card]})
    assert items[0].item_id == "999"
    assert items[0].title == "旧结构商品"
    assert items[0].price == 12.5
    assert items[0].want_num == 33


# ------------------------------------------------------------ 健壮性

def test_skips_broken_cards_without_failing():
    data = {"resultList": [
        make_card(item_id="1", title="好数据"),
        {"data": {"item": {"main": {}}}},        # 无 id，跳过
        None,                                     # 无效，跳过
        "垃圾",                                    # 无效，跳过
        make_card(item_id="2", title="也好"),
    ]}
    assert [i.item_id for i in parse_search_response(data)] == ["1", "2"]


def test_dedup_within_response():
    data = {"resultList": [make_card(item_id="7"), make_card(item_id="7")]}
    assert len(parse_search_response(data)) == 1


def test_html_unescape_and_tag_strip():
    card = make_card(title="A&amp;B <em>高亮</em>")
    assert parse_search_response({"resultList": [card]})[0].title == "A&B 高亮"


def test_parse_raises_when_structure_changed():
    """结构不对必须报错，不能静默返回空。"""
    with pytest.raises(ParseError):
        parse_search_response({"unexpected": 1})
    with pytest.raises(ParseError):
        parse_search_response("not a dict")     # type: ignore[arg-type]
    with pytest.raises(ParseError):
        parse_search_response({"resultList": "不是列表"})


def test_empty_result_list_is_valid():
    """空结果列表是合法的（关键词真的没商品）。"""
    assert parse_search_response({"resultList": []}) == []


def test_parse_card_directly():
    assert parse_card(make_card(item_id="55")).item_id == "55"
    assert parse_card(None) is None
    assert parse_card({}) is None


def test_publish_time_seconds_passthrough():
    """秒级时间戳也应正确处理。"""
    card = make_card(publish_ms=1788772849)      # 秒
    assert parse_search_response({"resultList": [card]})[0].publish_time == "2026-09-07"


def test_tags_collected():
    card = make_card(extra_tags=["9天内降价", "1人想要"])
    tags = parse_search_response({"resultList": [card]})[0].tags
    assert "9天内降价" in tags


# ------------------------------------------------------------ 存储

@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def test_store_batch_and_items(store):
    bid = store.new_batch("键盘", pages=2)
    n = store.add_items(bid, "键盘", [
        Item(item_id="a", title="键盘甲", price=100, want_num=5),
        Item(item_id="b", title="键盘乙", price=200, want_num=8),
    ])
    store.finish_batch(bid, n)
    rows = store.items("键盘")
    assert len(rows) == 2
    batches = store.batches("键盘")
    assert batches[0]["batch_id"] == bid
    assert batches[0]["item_count"] == 2


def test_store_dedup_within_batch(store):
    bid = store.new_batch("kw")
    store.add_items(bid, "kw", [Item(item_id="x", title="旧", price=1)])
    store.add_items(bid, "kw", [Item(item_id="x", title="新", price=2)])
    rows = store.items("kw", bid)
    assert len(rows) == 1
    assert rows[0]["title"] == "新"


def test_store_items_defaults_to_latest_batch(store):
    import time
    b1 = store.new_batch("kw")
    store.add_items(b1, "kw", [Item(item_id="old", title="旧批次", price=1)])
    time.sleep(0.005)                    # 确保微秒时间戳不同
    b2 = store.new_batch("kw")
    store.add_items(b2, "kw", [Item(item_id="new", title="新批次", price=2)])
    assert [r["item_id"] for r in store.items("kw")] == ["new"]


def test_store_known_ids(store):
    bid = store.new_batch("kw")
    store.add_items(bid, "kw", [Item(item_id="a"), Item(item_id="b")])
    assert store.known_item_ids("kw") == {"a", "b"}


def test_store_price_history(store):
    import time
    b1 = store.new_batch("kw")
    store.add_items(b1, "kw", [Item(item_id="a", title="甲", price=100)])
    time.sleep(0.005)
    b2 = store.new_batch("kw")
    store.add_items(b2, "kw", [Item(item_id="a", title="甲", price=80)])
    hist = store.history("a")
    assert len(hist) == 2
    assert {r["price"] for r in hist} == {100.0, 80.0}


def test_store_stats(store):
    bid = store.new_batch("kw")
    store.add_items(bid, "kw", [Item(item_id="a"), Item(item_id="b")])
    st = store.stats()
    assert st["batches"] == 1
    assert st["unique_items"] == 2
    assert st["keywords"] == 1


def test_batch_ids_unique_within_same_second(store):
    """同一秒内连续创建批次不能撞 ID。"""
    ids = {store.new_batch("kw") for _ in range(20)}
    assert len(ids) == 20


def test_items_all_merges_batches_and_dedups(store):
    """合并批次分析：跨批次按 item_id 去重，取最新记录。"""
    import time
    b1 = store.new_batch("kw")
    store.add_items(b1, "kw", [
        Item(item_id="a", title="甲-旧", price=100),
        Item(item_id="b", title="乙", price=200),
    ])
    time.sleep(0.005)
    b2 = store.new_batch("kw")
    store.add_items(b2, "kw", [
        Item(item_id="a", title="甲-新", price=88),   # 同 id，价格变了
        Item(item_id="c", title="丙", price=300),
    ])

    rows = store.items_all("kw")
    ids = sorted(r["item_id"] for r in rows)
    assert ids == ["a", "b", "c"]                     # 去重后 3 条
    a = [r for r in rows if r["item_id"] == "a"][0]
    assert a["price"] == 88                            # 取最新批次的值


def test_items_all_vs_latest_batch(store):
    """items_all 应比单批次看到更多样本。"""
    import time
    b1 = store.new_batch("kw")
    store.add_items(b1, "kw", [Item(item_id="x", price=1)])
    time.sleep(0.005)
    b2 = store.new_batch("kw")
    store.add_items(b2, "kw", [Item(item_id="y", price=2)])

    assert len(store.items("kw")) == 1        # 只看最新批次
    assert len(store.items_all("kw")) == 2    # 合并后 2 条
