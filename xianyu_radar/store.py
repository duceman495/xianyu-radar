"""数据模型与本地存储（SQLite）。

设计原则：
- 只存公开可得的商品字段，绝不存联系方式
- 按批次（batch）组织，便于做趋势对比
- 同一商品重复抓到只更新不重复插
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

DEFAULT_DATA_DIR = Path.home() / ".xianyu-radar"

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id    TEXT PRIMARY KEY,
    keyword     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    pages       INTEGER DEFAULT 0,
    item_count  INTEGER DEFAULT 0,
    note        TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS items (
    item_id       TEXT NOT NULL,
    batch_id      TEXT NOT NULL,
    keyword       TEXT NOT NULL,
    title         TEXT,
    price         REAL,
    area          TEXT,
    seller        TEXT,
    credit_level  INTEGER,
    condition     TEXT,
    free_ship     INTEGER,
    publish_time  TEXT,
    want_num      INTEGER DEFAULT 0,
    url           TEXT,
    raw           TEXT,
    seen_at       TEXT NOT NULL,
    PRIMARY KEY (item_id, batch_id)
);

CREATE INDEX IF NOT EXISTS idx_items_kw    ON items(keyword);
CREATE INDEX IF NOT EXISTS idx_items_batch ON items(batch_id);
CREATE INDEX IF NOT EXISTS idx_items_price ON items(price);

-- 跨批次的价格轨迹（同一商品在不同批次的价格）
CREATE TABLE IF NOT EXISTS price_history (
    item_id    TEXT NOT NULL,
    title      TEXT,
    price      REAL,
    seen_at    TEXT NOT NULL,
    batch_id   TEXT
);
CREATE INDEX IF NOT EXISTS idx_ph_item ON price_history(item_id);
"""


@dataclass
class Item:
    """一条在售商品。字段仅限公开信息。"""

    item_id: str
    title: str = ""
    price: float | None = None
    area: str = ""
    seller: str = ""
    seller_id: str = ""
    credit_level: int | None = None
    condition: str | None = None
    free_ship: bool = False
    publish_time: str = ""
    want_num: int = 0
    url: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def price_or_zero(self) -> float:
        return float(self.price or 0)

    def to_row(self, batch_id: str, keyword: str) -> tuple:
        return (
            self.item_id, batch_id, keyword, self.title, self.price, self.area,
            self.seller, self.credit_level, self.condition,
            1 if self.free_ship else 0, self.publish_time, self.want_num,
            self.url, json.dumps({"tags": self.tags}, ensure_ascii=False),
            time.strftime("%Y-%m-%dT%H:%M:%S"),
        )


class Store:
    """SQLite 存储层。"""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_DATA_DIR / "radar.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------- 批次 ----------

    def new_batch(self, keyword: str, pages: int = 0) -> str:
        # 带微秒，避免同一秒内连续创建时 batch_id 碰撞
        stamp = time.strftime("%Y%m%d_%H%M%S")
        micro = f"{int(time.time() * 1_000_000) % 1_000_000:06d}"
        batch_id = f"{keyword}_{stamp}_{micro}"
        self.conn.execute(
            "INSERT OR REPLACE INTO batches (batch_id, keyword, created_at, pages) "
            "VALUES (?,?,?,?)",
            (batch_id, keyword, time.strftime("%Y-%m-%dT%H:%M:%S"), pages),
        )
        self.conn.commit()
        return batch_id

    def finish_batch(self, batch_id: str, item_count: int) -> None:
        self.conn.execute(
            "UPDATE batches SET item_count=? WHERE batch_id=?", (item_count, batch_id))
        self.conn.commit()

    def batches(self, keyword: str | None = None, limit: int = 30) -> list[sqlite3.Row]:
        if keyword:
            cur = self.conn.execute(
                "SELECT * FROM batches WHERE keyword=? "
                "ORDER BY created_at DESC, batch_id DESC LIMIT ?",
                (keyword, limit))
        else:
            cur = self.conn.execute(
                "SELECT * FROM batches ORDER BY created_at DESC, batch_id DESC LIMIT ?", (limit,))
        return cur.fetchall()

    # ---------- 商品 ----------

    def add_items(self, batch_id: str, keyword: str, items: Iterable[Item]) -> int:
        rows = [it.to_row(batch_id, keyword) for it in items]
        self.conn.executemany(
            "INSERT OR REPLACE INTO items "
            "(item_id,batch_id,keyword,title,price,area,seller,credit_level,"
            "condition,free_ship,publish_time,want_num,url,raw,seen_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows)
        self.conn.executemany(
            "INSERT INTO price_history (item_id,title,price,seen_at,batch_id) "
            "VALUES (?,?,?,?,?)",
            [(it.item_id, it.title, it.price,
              time.strftime("%Y-%m-%dT%H:%M:%S"), batch_id) for it in items])
        self.conn.commit()
        return len(rows)

    def items(self, keyword: str, batch_id: str | None = None) -> list[sqlite3.Row]:
        if batch_id:
            cur = self.conn.execute(
                "SELECT * FROM items WHERE keyword=? AND batch_id=?", (keyword, batch_id))
        else:
            cur = self.conn.execute(
                "SELECT * FROM items WHERE keyword=? AND batch_id=("
                "  SELECT batch_id FROM batches WHERE keyword=? "
                "  ORDER BY created_at DESC, batch_id DESC LIMIT 1)",
                (keyword, keyword))
        return cur.fetchall()

    def items_all(self, keyword: str) -> list[sqlite3.Row]:
        """合并该关键词**所有批次**的商品，按 item_id 去重，保留最新的记录。

        为什么需要这个：单批次往往只有 1 页 30 条，样本不足会让
        机会分给出误导性结论（本项目的测试就是从这个真实 bug 来的）。
        合并多批次能显著提高置信度。
        """
        cur = self.conn.execute(
            "SELECT i.* FROM items i "
            "JOIN ("
            "  SELECT item_id, MAX(seen_at) AS latest "
            "  FROM items WHERE keyword=? GROUP BY item_id"
            ") m ON i.item_id = m.item_id AND i.seen_at = m.latest "
            "WHERE i.keyword=?",
            (keyword, keyword))
        rows = cur.fetchall()
        # 同一 item_id 在同一秒的多个批次可能都命中，再去一次重
        seen: set[str] = set()
        out: list[sqlite3.Row] = []
        for r in rows:
            if r["item_id"] not in seen:
                seen.add(r["item_id"])
                out.append(r)
        return out

    def history(self, needle: str, limit: int = 50) -> list[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM price_history WHERE item_id=? OR title LIKE ? "
            "ORDER BY seen_at DESC LIMIT ?", (needle, f"%{needle}%", limit))
        return cur.fetchall()

    def known_item_ids(self, keyword: str) -> set[str]:
        cur = self.conn.execute("SELECT DISTINCT item_id FROM items WHERE keyword=?", (keyword,))
        return {r["item_id"] for r in cur.fetchall()}

    def stats(self) -> dict[str, Any]:
        try:
            nb = self.conn.execute("SELECT COUNT(*) c FROM batches").fetchone()["c"]
            ni = self.conn.execute("SELECT COUNT(DISTINCT item_id) c FROM items").fetchone()["c"]
            nk = self.conn.execute("SELECT COUNT(DISTINCT keyword) c FROM items").fetchone()["c"]
        except sqlite3.Error:
            return {}
        return {"batches": nb, "unique_items": ni, "keywords": nk, "db": str(self.path)}

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
