"""Command line: `gtavi live @handle`, `gtavi vod <url> --start 1:02:03 --end 1:02:40`, `gtavi ui`."""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from .clipper import FORMATS, download_section
from .session import ClipSession
from .sources import NotLiveError, parse_source


def parse_time(text: str) -> float:
    """'90', '1:30', '01:02:03', '5m30s', '1h2m' -> seconds."""
    text = text.strip().lower()
    if re.fullmatch(r"[\d.]+", text):
        return float(text)
    if ":" in text:
        total = 0.0
        for part in text.split(":"):
            total = total * 60 + float(part)
        return total
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", text)
    if m and any(m.groups()):
        h, mi, s = (float(g) if g else 0.0 for g in m.groups())
        return h * 3600 + mi * 60 + s
    raise argparse.ArgumentTypeError(f"can't read time {text!r}")


def cmd_live(a: argparse.Namespace) -> None:
    src = parse_source(a.stream)
    print(f"… looking up {src.label}")
    try:
        s = ClipSession(
            src, Path(a.data), a.buffer, a.engine, a.quality,
            auto_clip=a.auto, auto_fmt=a.format, sensitivity=a.sensitivity, tags=a.tags,
            on_clip=lambda c: print(f"\n  ✂  saved {c.path}  [{c.reason}, {c.duration}s]\n     shorts text: {c.kit}"),
        )
    except NotLiveError as e:
        sys.exit(f"✖ {e}")
    if s.source.title:
        print(f"  {s.source.title}  ({s.source.share_url})")
    print(f"▶ buffering {s.source.label} (last {a.buffer}s kept){' + auto-clip on chat hype' if a.auto else ''}")
    print("  [Enter] clip last %ss   [m] mark   [c <secs>] custom length   [q] quit\n" % a.length)
    s.start()

    def announce() -> None:
        while not s.recorder.now and s.running:
            time.sleep(0.5)
        if s.running:
            print("  ● recording, clips available now")
        while s.running:
            time.sleep(1)
        if s.recorder.error:
            print(f"  ✖ stream stopped: {s.recorder.error}")

    threading.Thread(target=announce, daemon=True).start()
    try:
        while True:
            cmd = input().strip().lower()
            if cmd == "q":
                break
            if not s.running:
                print(f"  ✖ recorder stopped: {s.recorder.error}")
                break
            if cmd == "m":
                print(f"  ⚑ marked at {s.mark():.0f}s")
                continue
            secs = a.length
            if cmd.startswith("c "):
                secs = parse_time(cmd[2:])
            print(f"  … clipping last {secs:.0f}s (buffered {s.recorder.buffered:.0f}s)")
            s.clip_last(secs, a.format, a.fast, caption=a.caption).add_done_callback(
                lambda f: f.exception() and print(f"  ✖ {f.exception()}")
            )
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        print("stopping…")
        time.sleep(0.2)
        s.stop()


def cmd_vod(a: argparse.Namespace) -> None:
    out = Path(a.out or f"gtavi_data/clips/vod_{int(time.time())}_{a.format}.mp4")
    print(f"✂ downloading {a.start:.0f}s → {a.end:.0f}s …")
    download_section(a.url, a.start, a.end, out, a.format, caption=a.caption)
    print(f"saved {out}")


def cmd_ui(a: argparse.Namespace) -> None:
    app = Path(__file__).resolve().parent / "app.py"
    sys.exit(subprocess.call([sys.executable, "-m", "streamlit", "run", str(app), *a.extra]))


def main(argv=None) -> None:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles may not do emoji: degrade, don't crash
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(prog="gtavi", description="Turn YouTube live moments into Shorts, instantly.")
    sub = p.add_subparsers(dest="cmd", required=True)

    live = sub.add_parser("live", help="buffer a live stream and clip from the terminal")
    live.add_argument("stream", help="YouTube @handle or live URL (also twitch.tv/x, kick.com/x, ...)")
    live.add_argument("--buffer", type=int, default=300, help="seconds of replay kept (default 300)")
    live.add_argument("--length", type=parse_time, default=30.0, help="default clip length (default 30s)")
    live.add_argument("--format", choices=list(FORMATS), default="vertical-blur")
    live.add_argument("--caption", default="", help='hook text burned on top, e.g. "WAIT FOR IT"')
    live.add_argument("--tags", nargs="*", default=[], help="hashtags for the Shorts text, e.g. gta6 gaming")
    live.add_argument("--fast", action="store_true", help="no re-encode (instant, keyframe-accurate)")
    live.add_argument("--auto", action="store_true", help="auto-clip when live chat goes crazy")
    live.add_argument("--sensitivity", type=float, default=2.5, help="hype threshold, lower = more clips")
    live.add_argument("--engine", choices=["streamlink", "yt-dlp"], default="streamlink")
    live.add_argument("--quality", default="best")
    live.add_argument("--data", default="gtavi_data")
    live.set_defaults(func=cmd_live)

    vod = sub.add_parser("vod", help="clip a time range from a VOD / YouTube video")
    vod.add_argument("url")
    vod.add_argument("--start", type=parse_time, required=True)
    vod.add_argument("--end", type=parse_time, required=True)
    vod.add_argument("--format", choices=list(FORMATS), default="vertical-blur")
    vod.add_argument("--caption", default="")
    vod.add_argument("--out")
    vod.set_defaults(func=cmd_vod)

    ui = sub.add_parser("ui", help="open the Streamlit web app (extra flags go to streamlit, e.g. --server.port 8080)")
    ui.set_defaults(func=cmd_ui)

    a, extra = p.parse_known_args(argv)
    if extra and a.cmd != "ui":
        p.error(f"unrecognized arguments: {' '.join(extra)}")
    a.extra = extra
    a.func(a)


if __name__ == "__main__":
    main()
