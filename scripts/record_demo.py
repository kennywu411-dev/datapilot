"""录 README 用的演示视频：无头 Chrome 自动操作界面，用 CDP 录屏，再用 ffmpeg 合成 MP4 和 GIF。

流程：提问 → 流式步骤 → SQL → 图表 → 追问 → 评测看板。
和 scripts/screenshots.py 一样不需要人工操作；界面改了可以一键重录。

前提：后端和前端已启动。新问题会真实调用模型（Sonnet 每题约 $0.005）；
只想试录流程、不花钱时，用模拟模型另起一套服务：
  DATAPILOT_MODEL=simulated DATAPILOT_CORS_ORIGINS=http://localhost:3001 \\
    uv run uvicorn datapilot.api:app --port 8001
  NEXT_PUBLIC_API_BASE=http://localhost:8001 npm --prefix web run dev -- -p 3001
  uv run python scripts/record_demo.py --base http://localhost:3001 --name demo-trial

用法：uv run python scripts/record_demo.py            # 输出 docs/images/demo.mp4 和 demo.gif
"""

from __future__ import annotations

import argparse
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
PORT = 9334
W, H = 1440, 900

QUESTIONS = [
    "各出版商的超级英雄数量，前 8 名是哪些？",
    "那只看 Marvel Comics，男女各多少人？占比如何？",
]


class Recorder:
    """最小的 CDP 客户端：一个后台任务读消息，命令按 id 配对，录屏帧单独收集并立即确认。"""

    def __init__(self, ws, frame_dir: Path):
        self.ws = ws
        self.n = 0
        self.pending: dict[int, asyncio.Future] = {}
        self.frames: list[tuple[float, Path]] = []
        self.frame_dir = frame_dir
        self.reader = asyncio.create_task(self._read())

    async def _read(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            if "id" in msg and msg["id"] in self.pending:
                self.pending.pop(msg["id"]).set_result(msg)
            elif msg.get("method") == "Page.screencastFrame":
                p = msg["params"]
                path = self.frame_dir / f"{len(self.frames):05d}.jpg"
                path.write_bytes(base64.b64decode(p["data"]))
                self.frames.append((p["metadata"]["timestamp"], path))
                await self.ws.send(
                    json.dumps(
                        {
                            "id": 0,
                            "method": "Page.screencastFrameAck",
                            "params": {"sessionId": p["sessionId"]},
                        }
                    )
                )

    async def call(self, method: str, **params):
        self.n += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[self.n] = fut
        await self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        msg = await fut
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

    async def wait_for(self, expr: str, timeout: float = 90):
        start = time.time()
        while time.time() - start < timeout:
            if await self.js(expr):
                return
            await asyncio.sleep(0.3)
        raise TimeoutError(expr)


async def type_question(rec: Recorder, text: str):
    await rec.js("document.querySelector('textarea#q').focus()")
    for ch in text:  # 逐字输入，看起来像真人在打字
        await rec.call("Input.insertText", text=ch)
        await asyncio.sleep(0.06)
    await asyncio.sleep(0.5)
    await rec.js("document.querySelector('button[aria-label=\"发送\"]').click()")


async def ask(rec: Recorder, text: str, turn: int):
    await type_question(rec, text)
    await rec.wait_for(
        f"document.querySelectorAll('article').length >= {turn} && "
        f"[...document.querySelectorAll('article')][{turn - 1}].innerText.includes('$')"
    )
    await asyncio.sleep(3.0)  # 停留看图表


async def smooth_scroll(rec: Recorder, total: int, seconds: float):
    steps = int(seconds * 20)
    for _ in range(steps):
        await rec.js(f"window.scrollBy(0, {total / steps})")
        await asyncio.sleep(seconds / steps)


async def record(base: str, frame_dir: Path) -> list[tuple[float, Path]]:
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
            raise RuntimeError(f"Chrome 调试端口 {PORT} 10 秒内没起来")
        targets = json.loads(urllib.request.urlopen(f"http://localhost:{PORT}/json").read())
        ws_url = next(t["webSocketDebuggerUrl"] for t in targets if t["type"] == "page")
        async with websockets.connect(ws_url, max_size=64 * 2**20) as ws:
            rec = Recorder(ws, frame_dir)
            await rec.call("Page.enable")
            await rec.call("Runtime.enable")
            await rec.call(
                "Emulation.setDeviceMetricsOverride",
                width=W,
                height=H,
                deviceScaleFactor=1,
                mobile=False,
            )
            await rec.call("Page.navigate", url=base)
            await asyncio.sleep(2)
            await rec.js("localStorage.setItem('dp-theme', 'dark')")
            await rec.call("Page.navigate", url=base)
            await rec.wait_for(
                "document.querySelector('select') && document.querySelector('select').value"
            )
            await asyncio.sleep(1)

            await rec.call(
                "Page.startScreencast", format="jpeg", quality=85, maxWidth=W, maxHeight=H
            )
            await asyncio.sleep(1.5)
            for i, q in enumerate(QUESTIONS, 1):
                await ask(rec, q, i)
            await rec.call("Page.navigate", url=f"{base}/eval")
            await asyncio.sleep(3)
            await smooth_scroll(rec, 1800, 6)
            await asyncio.sleep(2)
            await rec.call("Page.stopScreencast")
            rec.reader.cancel()
            return rec.frames
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        shutil.rmtree(profile, ignore_errors=True)


def encode(frames: list[tuple[float, Path]], name: str, work: Path) -> None:
    """录屏帧只在画面变化时产生，间隔不均匀：用 concat 清单按真实时间戳给每帧设时长。"""
    lines = []
    for (t, path), (t_next, _) in zip(
        frames, frames[1:] + [(frames[-1][0] + 1.0, None)], strict=True
    ):
        lines += [f"file '{path}'", f"duration {max(t_next - t, 0.001):.3f}"]
    lines.append(f"file '{frames[-1][1]}'")
    listing = work / "frames.txt"
    listing.write_text("\n".join(lines))
    mp4, gif = OUT / f"{name}.mp4", OUT / f"{name}.gif"
    common = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(listing),
    ]
    subprocess.run(
        common
        + [
            "-vf",
            "fps=30,scale=1280:-2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "28",
            "-movflags",
            "+faststart",
            str(mp4),
        ],
        check=True,
    )
    subprocess.run(
        common
        + [
            "-vf",
            "fps=8,scale=960:-1:flags=lanczos,split[a][b];"
            "[a]palettegen=max_colors=96[p];[b][p]paletteuse=dither=bayer",
            str(gif),
        ],
        check=True,
    )
    for f in (mp4, gif):
        print(f"已保存 {f.relative_to(ROOT)}（{f.stat().st_size / 2**20:.1f} MB）")


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--base", default="http://localhost:3000")
    p.add_argument("--name", default="demo", help="输出文件名（不含扩展名）")
    args = p.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="dp-demo-"))
    try:
        frames = asyncio.run(record(args.base, work))
        print(f"录到 {len(frames)} 帧，{frames[-1][0] - frames[0][0]:.1f} 秒")
        encode(frames, args.name, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
