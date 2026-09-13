"""命令行入口。

    python -m xianyu_radar <command>

命令：
    doctor    只读自检
    login     扫码登录
    search    抓取搜索
    analyze   生成报告
    compare   多关键词对比
    trend     多批次趋势
    history   单商品价格轨迹
    export    导出 CSV / JSON
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from pathlib import Path

from . import __version__
from .analyze import analyze, render_report
from .login import LoginError, STATE_PATH, has_state, load_cookies, login_with_browser
from .mtop import MtopClient, RadarError, SessionExpired
from .spider import Spider
from .store import DEFAULT_DATA_DIR, Store

REPORT_DIR = DEFAULT_DATA_DIR / "reports"


# ---------------------------------------------------------------- 输出工具

def p(msg: str = "") -> None:
    print(msg, flush=True)


def die(msg: str, code: int = 1) -> None:
    print(f"错误：{msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def make_client() -> MtopClient:
    try:
        cookies = load_cookies()
    except LoginError as e:
        die(str(e))
    return MtopClient(cookies)


# ---------------------------------------------------------------- 命令

def cmd_doctor(args: argparse.Namespace) -> int:
    """只读自检：不联网、不写文件、不读凭据内容。"""
    checks: list[dict] = []

    v = sys.version_info
    checks.append({
        "name": "python", "ok": v >= (3, 10),
        "detail": f"Python {v.major}.{v.minor}.{v.micro}（需要 >= 3.10）",
    })

    for mod in ("httpx", "playwright"):
        try:
            __import__(mod)
            checks.append({"name": mod, "ok": True, "detail": "已安装"})
        except ImportError:
            checks.append({"name": mod, "ok": False, "detail": f"未安装：pip install {mod}"})

    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        checks.append({"name": "playwright-api", "ok": True, "detail": "可导入"})
    except Exception as e:
        checks.append({"name": "playwright-api", "ok": False, "detail": str(e)[:80]})

    ok_state = has_state()
    checks.append({
        "name": "login-state", "ok": ok_state,
        "detail": f"{STATE_PATH}（{'存在' if ok_state else '缺失，请运行 login'}）",
    })

    checks.append({
        "name": "data-dir", "ok": True,
        "detail": str(DEFAULT_DATA_DIR),
    })

    all_ok = all(c["ok"] for c in checks)
    out = {
        "ok": all_ok,
        "version": __version__,
        "checks": checks,
        "next_action": _next_action(checks),
    }
    p(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if all_ok else 1


def _next_action(checks: list[dict]) -> dict:
    for c in checks:
        if not c["ok"]:
            name = c["name"]
            if name == "python":
                return {"code": "upgrade-python", "message": "请升级到 Python 3.10+"}
            if name in ("httpx", "playwright"):
                return {"code": "install-deps",
                        "message": "pip install -r requirements.txt"}
            if name == "login-state":
                return {"code": "login-required",
                        "message": "运行 python -m xianyu_radar login 扫码登录"}
    return {"code": "ready", "message": "环境正常，可以开始抓取"}


def cmd_login(args: argparse.Namespace) -> int:
    try:
        login_with_browser(timeout=args.timeout, quiet=args.quiet)
    except LoginError as e:
        die(str(e))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    kw = args.keyword
    client = make_client()
    spider = Spider(client, sleep=args.sleep)
    pages_done = 0

    def on_page(page: int, items: list, err: str) -> None:
        nonlocal pages_done
        if err:
            p(f"  第 {page} 页失败：{err}")
            return
        if items:
            pages_done += 1
            p(f"  第 {page} 页：{len(items)} 条")

    p(f"关键词：{kw}")
    items = spider.search(kw, pages=args.pages, on_page=on_page)
    client.close()

    if not items:
        p("没有抓到结果。可能原因：登录态过期 / 关键词无商品 / 触发风控")
        return 1

    with Store(args.db) as store:
        batch_id = store.new_batch(kw, pages=pages_done)
        store.add_items(batch_id, kw, items)
        store.finish_batch(batch_id, len(items))
        st = store.stats()

    p(f"批次：{batch_id} · {len(items)} 条")
    p(f"累计：{st.get('unique_items')} 个商品 / {st.get('keywords')} 个关键词")
    p(f"下一步：python -m xianyu_radar analyze \"{kw}\"")
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    kw = args.keyword
    with Store(args.db) as store:
        if getattr(args, "all", False):
            rows = store.items_all(kw)
            batches = store.batches(kw, limit=1)
            if batches and rows:
                meta0 = dict(batches[0])
                meta0["batch_id"] = f"全部批次合并（{len(batches)}）"
                batches = [meta0]
        else:
            rows = store.items(kw)
            batches = store.batches(kw, limit=1)
    if not rows:
        die(f"没有「{kw}」的数据，请先运行 search")

    meta = dict(batches[0]) if batches else {}
    o = analyze(rows)
    md = render_report(kw, o, meta)
    p(md)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in kw)
    out = REPORT_DIR / f"{safe}.md"
    out.write_text(md, encoding="utf-8")
    p(f"\n[saved] {out}")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    kws = [k.strip() for k in args.keywords.split(",") if k.strip()]
    if len(kws) < 2:
        die("请用逗号分隔至少 2 个关键词")
    rows_out = []
    with Store(args.db) as store:
        for kw in kws:
            rows = store.items_all(kw)   # 合并批次，避免小样本误判
            if not rows:
                p(f"  [跳过] {kw}：无数据")
                continue
            o = analyze(rows)
            rows_out.append((kw, o))
    if not rows_out:
        die("没有任何一个关键词有数据")

    # 按"机会分最高的价格带"排序，而不是按播放/想要总量
    p("| 关键词 | 样本 | 中位价 | 中位想要 | 最佳机会带 | 机会分 | 服务型占比 |")
    p("|---|---|---|---|---|---|---|")
    for kw, o in sorted(rows_out, key=lambda x: -(x[1].best_band.opportunity if x[1].best_band else 0)):
        bb = o.best_band
        p(f"| {kw} | {o.n} | {o.price_median:.1f} | {o.want_median:.0f} "
          f"| {bb.label if bb else '—'} | {bb.opportunity if bb else 0} "
          f"| {o.sentiment.ratio('定制/服务')*100:.0f}% |")
    return 0


def cmd_trend(args: argparse.Namespace) -> int:
    kw = args.keyword
    with Store(args.db) as store:
        bs = store.batches(kw, limit=args.limit)
        if len(bs) < 2:
            die(f"「{kw}」只有 {len(bs)} 个批次，至少需要 2 个才能看趋势")
        p(f"# 趋势：{kw}\n")
        p("| 批次 | 时间 | 样本 | 中位价 | 中位想要 |")
        p("|---|---|---|---|---|")
        for b in reversed(bs):
            rows = store.items(kw, b["batch_id"])
            if not rows:
                continue
            prices = sorted(float(r["price"]) for r in rows if r["price"])
            wants = [r["want_num"] or 0 for r in rows]
            p(f"| {b['batch_id']} | {b['created_at'][:16]} | {len(rows)} "
              f"| {statistics.median(prices) if prices else 0:.1f} "
              f"| {statistics.median(wants) if wants else 0:.0f} |")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    with Store(args.db) as store:
        rows = store.history(args.needle, limit=args.limit)
    if not rows:
        p("没有找到匹配的价格记录。")
        return 1
    p("| 时间 | 价格 | 标题 |")
    p("|---|---|---|")
    for r in rows:
        p(f"| {r['seen_at'][:16]} | {r['price']} | {(r['title'] or '')[:44]} |")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    with Store(args.db) as store:
        if args.keyword:
            rows = store.items(args.keyword)
            safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in args.keyword)
        else:
            rows = store.conn.execute("SELECT * FROM items").fetchall()
            safe = "all"
    if not rows:
        die("没有可导出的数据")

    if args.format in ("csv", "both"):
        fp = outdir / f"{safe}.csv"
        cols = ["item_id", "keyword", "title", "price", "area", "seller",
                "credit_level", "want_num", "publish_time", "url"]
        with fp.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for r in rows:
                w.writerow([r[c] if c in r.keys() else "" for c in cols])
        p(f"[csv] {fp}")

    if args.format in ("json", "both"):
        fp = outdir / f"{safe}.json"
        fp.write_text(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=1),
                      encoding="utf-8")
        p(f"[json] {fp}")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """抓一次，只报告相对历史的新商品。"""
    kw = args.keyword
    with Store(args.db) as store:
        known = store.known_item_ids(kw)

    client = make_client()
    spider = Spider(client, sleep=args.sleep)
    items = spider.search(kw, pages=args.pages)
    client.close()

    fresh = [it for it in items if it.item_id not in known]
    if items:
        with Store(args.db) as store:
            batch_id = store.new_batch(kw, pages=args.pages)
            store.add_items(batch_id, kw, items)
            store.finish_batch(batch_id, len(items))

    p(f"抓取 {len(items)} 条，其中**新增 {len(fresh)} 条**")
    if fresh:
        p("\n--- NEW_ITEMS_JSON ---")
        p(json.dumps([{
            "title": it.title, "price": it.price, "area": it.area,
            "want_num": it.want_num, "url": it.url,
        } for it in fresh], ensure_ascii=False, indent=1))
        p("--- END_NEW_ITEMS_JSON ---")
    return 0


# ---------------------------------------------------------------- 入口

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="xianyu_radar",
        description="闲鱼行情雷达：采集 → 分析 → 报告",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python -m xianyu_radar login
  python -m xianyu_radar search "机械键盘" --pages 2
  python -m xianyu_radar analyze "机械键盘"
  python -m xianyu_radar compare "键盘,鼠标,显示器"
""")
    ap.add_argument("--version", action="version", version=f"xianyu-radar {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=None, help="数据库路径（默认 ~/.xianyu-radar/radar.db）")
    common.add_argument("--sleep", type=float, default=1.2,
                        help="请求间隔秒数（默认 1.2，越大越安全）")

    s = sub.add_parser("doctor", help="环境自检（只读）")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("login", help="扫码登录")
    s.add_argument("--timeout", type=float, default=300.0)
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("search", help="抓取搜索", parents=[common])
    s.add_argument("keyword")
    s.add_argument("--pages", type=int, default=1)
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("analyze", help="生成行情报告", parents=[common])
    s.add_argument("keyword")
    s.add_argument("--all", action="store_true",
                   help="合并所有批次分析（样本更多，结论更稳，推荐）")
    s.set_defaults(func=cmd_analyze)

    s = sub.add_parser("compare", help="多关键词对比", parents=[common])
    s.add_argument("keywords", help="逗号分隔")
    s.set_defaults(func=cmd_compare)

    s = sub.add_parser("trend", help="多批次趋势", parents=[common])
    s.add_argument("keyword")
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(func=cmd_trend)

    s = sub.add_parser("history", help="单商品价格轨迹", parents=[common])
    s.add_argument("needle", help="商品 ID 或标题片段")
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(func=cmd_history)

    s = sub.add_parser("export", help="导出数据", parents=[common])
    s.add_argument("--keyword", default=None)
    s.add_argument("--format", choices=["csv", "json", "both"], default="both")
    s.add_argument("--outdir", default=".")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("watch", help="抓取并只报告新增", parents=[common])
    s.add_argument("keyword")
    s.add_argument("--pages", type=int, default=1)
    s.set_defaults(func=cmd_watch)

    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except SessionExpired as e:
        die(f"{e}\n提示：登录态约 7 天过期，请重新运行 login")
    except RadarError as e:
        die(str(e))
    except KeyboardInterrupt:
        p("\n已中断")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
