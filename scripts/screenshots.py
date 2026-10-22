"""用本机 Chrome 无头模式截 README 用的高清图（2 倍像素密度），通过 Chrome DevTools 协议驱动。

前提：后端（:8000）和前端（:3000）已启动。截图用的问题都问过，会命中缓存，不花钱。
用法：uv run python scripts/screenshots.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "images"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 9333
BASE = "http://localhost:3000"
W, H, SCALE = 1440, 900, 2

QUESTIONS = [
    "各出版商的超级英雄数量，前 8 名是哪些？",
    "那只看 Marvel Comics，男女各多少人？占比如何？",
]


class Page:
    def __init__(self, ws):
        self.ws = ws
        self.n = 0

    async def call(self, method: str, **params):
        self.n += 1
        my_id = self.n
        await self.ws.send(json.dumps({"id": my_id, "method": method, "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == my_id:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    async def js(self, expr: str):
        r = await self.call(
            "Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True
        )
        if "exceptionDetails" in r:
            raise RuntimeError(f"页面脚本出错：{r['exceptionDetails'].get('text')} {expr[:80]}")
        return r.get("result", {}).get("value")

    async def goto(self, url: str, settle: float = 3.0):
        await self.call("Page.navigate", url=url)
        await asyncio.sleep(settle)

    async def wait_for(self, expr: str, timeout: float = 60):
        start = time.time()
        while time.time() - start < timeout:
            if await self.js(expr):
                return
            await asyncio.sleep(0.5)
        raise TimeoutError(expr)

    async def shot(self, name: str):
        r = await self.call("Page.captureScreenshot", format="png")
        (OUT / name).write_bytes(base64.b64decode(r["data"]))
        print("已保存", (OUT / name).relative_to(ROOT))


async def ask(page: Page, question: str, turn: int):
    # React 受控输入：用原生 setter 赋值再派发 input 事件，然后点发送
    await page.js(
        "(() => { const t = document.querySelector('textarea#q');"
        "const set = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;"
        f"set.call(t, {json.dumps(question)});"
        "t.dispatchEvent(new Event('input', {bubbles: true}));"
        "})()"
    )
    await asyncio.sleep(0.3)
    await page.js("document.querySelector('button[aria-label=\"发送\"]').click()")
    # 等到第 turn 轮出现耗时 / 花费那一行（done 事件之后才渲染）
    await page.wait_for(
        f"document.querySelectorAll('article').length >= {turn} && "
        f"[...document.querySelectorAll('article')][{turn - 1}].innerText.includes('$')"
    )
    await asyncio.sleep(1.5)  # 等图表入场动画结束


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    profile = tempfile.mkdtemp(prefix="dp-chrome-")
    proc = subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            f"--remote-debugging-port={PORT}",
            f"--user-data-dir={profile}",
            "--hide-scrollbars",
            "--no-first-run",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://localhost:{PORT}/json/version")
                break
            except OSError:
                time.sleep(0.2)
        else:
            raise RuntimeError(
                f"Chrome 调试端口 {PORT} 10 秒内没起来，检查 {CHROME} 是否存在、端口是否被占用"
            )
        targets = json.loads(urllib.request.urlopen(f"http://localhost:{PORT}/json").read())
        ws_url = next(t["webSocketDebuggerUrl"] for t in targets if t["type"] == "page")
        async with websockets.connect(ws_url, max_size=64 * 2**20) as ws:
            page = Page(ws)
            await page.call("Page.enable")
            await page.call("Runtime.enable")
            await page.call(
                "Emulation.setDeviceMetricsOverride",
                width=W,
                height=H,
                deviceScaleFactor=SCALE,
                mobile=False,
            )
            await page.goto(BASE)
            await page.js("localStorage.setItem('dp-theme', 'dark')")
            await page.goto(BASE)
            await page.wait_for(
                "document.querySelector('select') && document.querySelector('select').value"
            )
            for i, q in enumerate(QUESTIONS, 1):
                await ask(page, q, i)
            await page.shot("chat.png")

            await page.goto(f"{BASE}/eval", settle=4)
            await page.shot("eval.png")
            # 失败分析：展开 q115 的单题回放
            await page.js(
                "(() => { const b = [...document.querySelectorAll('li button')]"
                ".find(x => x.innerText.includes('q115')); b.scrollIntoView({block: 'start'});"
                " window.scrollBy(0, -80); b.click(); })()"
            )
            await asyncio.sleep(1.5)
            await page.shot("eval-failure.png")
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
