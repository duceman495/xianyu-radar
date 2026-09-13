"""登录态管理。

设计原则：**脚本永远不代填任何凭据**。
`login` 命令只是打开一个浏览器窗口，让本人扫码；
脚本只负责在检测到登录成功后把 cookie 存下来。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .store import DEFAULT_DATA_DIR

STATE_PATH = DEFAULT_DATA_DIR / "storage_state.json"
LOGIN_COOKIE = "unb"          # 闲鱼登录成功后会出现这个 cookie
XIANYU_HOME = "https://www.goofish.com/"


class LoginError(Exception):
    pass


def save_state(cookies: list[dict[str, Any]], path: Path = STATE_PATH) -> Path:
    """保存登录态，并收紧文件权限（仅本人可读）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"cookies": cookies, "origins": []}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def load_cookies(path: Path = STATE_PATH) -> list[dict[str, Any]]:
    """读取登录 cookie。"""
    if not path.exists():
        raise LoginError(f"未找到登录态文件：{path}\n请先运行：python -m xianyu_radar login")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise LoginError(f"登录态文件损坏：{e}") from e
    cookies = data.get("cookies") or []
    names = {c.get("name") for c in cookies}
    if LOGIN_COOKIE not in names:
        raise LoginError("登录态里没有登录 cookie，可能已过期，请重新 login")
    return cookies


def has_state(path: Path = STATE_PATH) -> bool:
    """只检查文件存在性，不读取内容。"""
    return path.is_file()


def login_with_browser(timeout: float = 300.0, quiet: bool = False) -> Path:
    """打开浏览器让本人扫码。仅此命令会启动浏览器。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise LoginError(
            "需要 playwright：pip install playwright && playwright install chromium"
        ) from e

    def log(msg: str) -> None:
        if not quiet:
            print(msg, flush=True)

    log("正在打开浏览器，请扫码登录闲鱼（脚本不会代填任何账号密码）…")
    with sync_playwright() as p:
        browser = None
        for kwargs in ({"channel": "chrome", "headless": False},
                       {"headless": False}):
            try:
                browser = p.chromium.launch(**kwargs)
                break
            except Exception:
                continue
        if browser is None:
            raise LoginError("无法启动浏览器，请安装 Chrome 或 playwright install chromium")

        ctx = browser.new_context(
            viewport={"width": 1280, "height": 860},
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/151.0.0.0 Safari/537.36"),
        )
        page = ctx.new_page()
        page.goto(XIANYU_HOME, wait_until="domcontentloaded", timeout=60000)

        deadline = time.time() + timeout
        ok = False
        while time.time() < deadline:
            try:
                names = {c["name"] for c in ctx.cookies()}
            except Exception:
                break
            if LOGIN_COOKIE in names:
                time.sleep(2.0)  # 等其他 cookie 落地
                ok = True
                break
            time.sleep(1.0)

        if not ok:
            browser.close()
            raise LoginError(f"等待超时（{timeout:.0f}s）未检测到登录，请重试")

        cookies = ctx.cookies()
        path = save_state(cookies)
        browser.close()

    log(f"登录成功，登录态已保存到 {path}")
    log("提示：请立刻跑一次 1 页搜索验证抓取可用。")
    return path
