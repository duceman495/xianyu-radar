"""搜索抓取：调用闲鱼网页版搜索接口，解析成 Item 列表。

只读取公开在售商品的展示字段（标题/价格/地区/想要数/卖家昵称），
不触碰任何联系方式。

关于响应结构（这是踩坑最多的地方）
------------------------------------
`mtop.taobao.idlemtopsearch.pc.search` 返回的 `resultList` 里每一项形如：

    {"data": {"item": {"main": {
        "clickParam": {"args": {...}},      # 结构化字段都在这里
        "exContent": {
            "richTitle": [{type:"Text", data:{text: "标题"}}],
            "fishTags": {"r3": {"tagList": [{"data":{"content":"1371人想要"}}]}},
            "userNickName": "...",
            "area": "...",
        },
        "targetUrl": "...",
    }}}}

注意：**标题不是一个平铺字段**，而是藏在 `richTitle` 数组里；
**想要数和信用也不是数字字段**，而是 `fishTags` 里的中文文本。
所以解析要分两层：先取结构化字段，再从展示文本里兜底提取。
"""

from __future__ import annotations

import html
import re
import time
from typing import Any, Callable

from .mtop import MtopClient, ParseError, RadarError
from .store import Item

SEARCH_API = "mtop.taobao.idlemtopsearch.pc.search"

# 请求间隔（秒）——刻意放慢，避免触发风控
DEFAULT_SLEEP = 1.2

_TAG_RE = re.compile(r"<[^>]+>")
_WANT_RE = re.compile(r"(\d[\d,]*)\s*人想要")

# 卖家信用等级文本 -> 数值。闲鱼用文字描述，不是数字。
CREDIT_LEVELS = {
    "信用极好": 7, "信用优秀": 6, "信用良好": 5,
    "信用较好": 4, "信用一般": 3, "信用较低": 2, "信用差": 1,
}


def _clean(text: Any) -> str:
    """去掉高亮标签，还原 HTML 实体。"""
    if text is None:
        return ""
    return html.unescape(_TAG_RE.sub("", str(text))).strip()


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").replace("¥", "").strip())
    except (TypeError, ValueError):
        return None


def _to_int(v: Any) -> int:
    try:
        return int(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------- 解析

def parse_search_response(data: dict[str, Any]) -> list[Item]:
    """把搜索响应解析成 Item 列表。

    对缺字段保持宽容：单条解析失败只跳过该条，不让整批失败。
    但整体结构不对（接口变了）时必须报错，避免静默返回空结果。
    """
    if not isinstance(data, dict):
        raise ParseError("搜索响应不是 dict")

    if "resultList" not in data and "cardList" not in data:
        raise ParseError(
            f"搜索响应结构不符合预期（缺少 resultList/cardList）: {list(data)[:8]}")

    card_list = data.get("resultList") or data.get("cardList") or []
    if not isinstance(card_list, list):
        raise ParseError("resultList/cardList 不是列表")

    items: list[Item] = []
    seen: set[str] = set()
    for card in card_list:
        try:
            it = parse_card(card)
        except Exception:
            continue
        if it and it.item_id and it.item_id not in seen:
            seen.add(it.item_id)
            items.append(it)
    return items


def parse_card(card: Any) -> Item | None:
    """解析一张商品卡片。兼容新旧两种结构。"""
    if not isinstance(card, dict):
        return None

    # 新版：data.item.main；旧版/其他：cardData 或卡片本身
    main: dict[str, Any] = {}
    node = card.get("data")
    if isinstance(node, dict):
        item_node = node.get("item")
        if isinstance(item_node, dict) and isinstance(item_node.get("main"), dict):
            main = item_node["main"]
    if not main:
        cd = card.get("cardData")
        main = cd if isinstance(cd, dict) else card

    args = ((main.get("clickParam") or {}).get("args")
            if isinstance(main.get("clickParam"), dict) else None) or {}
    ex = main.get("exContent") if isinstance(main.get("exContent"), dict) else {}

    # ---- item_id ----
    item_id = ""
    for src, keys in ((args, ("item_id", "id")), (ex, ("itemId",)),
                      (main, ("itemId", "id"))):
        for k in keys:
            if src.get(k):
                item_id = str(src[k])
                break
        if item_id:
            break
    if not item_id:
        m = re.search(r"id=(\d+)", str(main.get("targetUrl") or ""))
        if m:
            item_id = m.group(1)
    if not item_id:
        return None

    # ---- 标题：优先 richTitle 里的 Text 片段 ----
    title = ""
    rich = ex.get("richTitle")
    if isinstance(rich, list):
        parts = []
        for seg in rich:
            if isinstance(seg, dict):
                d = seg.get("data")
                if isinstance(d, dict) and d.get("text"):
                    parts.append(str(d["text"]))
        title = " ".join(parts).strip()
    if not title:
        for src, keys in ((ex, ("title", "titleSpan")), (args, ("title",)),
                          (main, ("title",))):
            v = src.get(keys[0]) if keys else None
            if isinstance(v, dict):
                v = v.get("text")
            if v:
                title = _clean(v)
                break
    title = _clean(title)

    # ---- 价格 ----
    price = _to_float(args.get("displayPrice") or args.get("price"))
    if price is None:
        price = _to_float(ex.get("soldPrice"))
    if price is None:
        # 旧结构：价格直接在 main/cardData 上
        price = _to_float(main.get("price") or main.get("displayPrice"))
    if price is None:
        price = _price_from_ex(ex)

    # ---- 想要数：结构化字段优先，再从标签文本抠 ----
    want = _to_int(args.get("wantNum"))
    if not want:
        want = _to_int(main.get("wantNum") or main.get("wantCount"))
    if not want:
        want = _want_from_tags(ex)

    # ---- 卖家 ----
    seller = _clean(ex.get("userNickName") or args.get("userNickName")
                    or main.get("userNickName") or main.get("nick") or "")
    seller_id = str(args.get("seller_id") or ex.get("sellerId") or "")
    if not seller_id:
        j = ex.get("jump2XianYuHao")
        if isinstance(j, dict):
            cp = j.get("clickParam")
            if isinstance(cp, dict):
                seller_id = str((cp.get("args") or {}).get("user_id") or "")

    # ---- 信用等级：中文文本 -> 数值 ----
    credit = _credit_from_tags(ex)

    # ---- 地区 ----
    area = _clean(ex.get("area") or ex.get("city") or main.get("area") or "")

    # ---- 包邮 ----
    free_ship = bool(args.get("tag") == "freeship" or args.get("tagname") == "包邮"
                     or _tag_contains(ex, "freeShippingIcon") or _tag_contains(ex, "包邮"))

    # ---- 发布时间：毫秒时间戳 ----
    publish_time = ""
    pt = args.get("publishTime")
    if pt:
        try:
            ts = int(pt)
            if ts > 10**12:      # 毫秒
                ts //= 1000
            publish_time = time.strftime("%Y-%m-%d", time.localtime(ts))
        except (TypeError, ValueError):
            publish_time = str(pt)

    # ---- 标签 ----
    tags = _tag_texts(ex)

    return Item(
        item_id=item_id, title=title, price=price, area=area,
        seller=seller, seller_id=seller_id, credit_level=credit,
        condition=ex.get("stuffStatus") or args.get("stuffStatus"),
        free_ship=free_ship, publish_time=publish_time, want_num=want,
        url=f"https://www.goofish.com/item?id={item_id}",
        tags=tags,
    )


def _iter_tag_data(ex: dict[str, Any]) -> list[dict[str, Any]]:
    """遍历 fishTags 里所有标签的 data 节点。"""
    out: list[dict[str, Any]] = []
    ft = ex.get("fishTags")
    if not isinstance(ft, dict):
        return out
    for group in ft.values():
        if not isinstance(group, dict):
            continue
        for tag in (group.get("tagList") or []):
            if isinstance(tag, dict) and isinstance(tag.get("data"), dict):
                out.append(tag["data"])
    # priceTag / 其他标签数组也一并收进来
    for key in ("priceTag", "rankTag", "extraTags"):
        for tag in (ex.get(key) or []):
            if isinstance(tag, dict) and isinstance(tag.get("data"), dict):
                out.append(tag["data"])
    return out


def _want_from_tags(ex: dict[str, Any]) -> int:
    for d in _iter_tag_data(ex):
        m = _WANT_RE.search(str(d.get("content") or ""))
        if m:
            return _to_int(m.group(1))
    return 0


def _credit_from_tags(ex: dict[str, Any]) -> int | None:
    for d in _iter_tag_data(ex):
        content = str(d.get("content") or "")
        for text, lvl in CREDIT_LEVELS.items():
            if text in content:
                return lvl
    return None


def _tag_contains(ex: dict[str, Any], needle: str) -> bool:
    for d in _iter_tag_data(ex):
        if needle in str(d.get("content") or "") or needle in str(d.get("url") or ""):
            return True
    return False


def _tag_texts(ex: dict[str, Any]) -> list[str]:
    out = []
    for d in _iter_tag_data(ex):
        c = str(d.get("content") or "").strip()
        if c and not c.startswith("http"):
            out.append(c)
    return out


def _price_from_ex(ex: dict[str, Any]) -> float | None:
    """从 price 富文本数组里拼出价格，如 [{"text":"¥"},{"text":"2"}]。"""
    segs = ex.get("price")
    if not isinstance(segs, list):
        return None
    buf = "".join(str(s.get("text", "")) for s in segs if isinstance(s, dict))
    m = re.search(r"(\d+(?:\.\d+)?)", buf.replace(",", ""))
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------- 抓取

class Spider:
    """搜索抓取器。一个实例复用一个 HTTP 会话。"""

    def __init__(self, client: MtopClient, sleep: float = DEFAULT_SLEEP) -> None:
        self.client = client
        self.sleep = max(0.0, sleep)
        self._last_call = 0.0

    def _throttle(self) -> None:
        """保证两次请求之间至少间隔 sleep 秒。"""
        gap = time.time() - self._last_call
        if gap < self.sleep:
            time.sleep(self.sleep - gap)
        self._last_call = time.time()

    def search_page(self, keyword: str, page: int = 1) -> list[Item]:
        """抓一页搜索结果。"""
        self._throttle()
        payload = {
            "pageNumber": page,
            "keyword": keyword,
            "fromFilter": False,
            "rowsPerPage": 30,
            "sortValue": "",
            "sortField": "",
            "customDistance": "",
            "gps": "",
            "propValueStr": "{}",
            "customGps": "",
            "searchReqFromPage": "pcSearch",
            "extraFilterValue": "{}",
            "userPositionJson": "{}",
        }
        data = self.client.call(SEARCH_API, payload)
        return parse_search_response(data)

    def search(self, keyword: str, pages: int = 1,
               on_page: Callable[[int, list[Item], str], None] | None = None,
               ) -> list[Item]:
        """抓多页并按 item_id 去重。"""
        seen: set[str] = set()
        out: list[Item] = []
        for p in range(1, pages + 1):
            try:
                batch = self.search_page(keyword, p)
            except RadarError as e:
                if on_page:
                    on_page(p, [], str(e))
                break
            fresh = [it for it in batch if it.item_id not in seen]
            seen.update(it.item_id for it in fresh)
            out.extend(fresh)
            if on_page:
                on_page(p, fresh, "")
            if not batch:
                break
        return out
