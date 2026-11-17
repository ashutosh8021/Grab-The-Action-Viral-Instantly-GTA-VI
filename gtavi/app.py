"""GTAVI Streamlit app — watch a YouTube live stream, hit a button, get a ready-to-post Short.

Run:  gtavi ui   (or: streamlit run gtavi/app.py)
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Dict

import streamlit as st

from gtavi.clipper import FORMATS, SHORTS_MAX_SECONDS, download_section, shorts_kit
from gtavi.cli import parse_time
from gtavi.session import ClipSession
from gtavi.sources import NotLiveError, parse_source

DATA = Path("gtavi_data")

st.set_page_config(page_title="GTAVI · live → Shorts", page_icon="✂️", layout="wide")


def iframe(src: str, height: int) -> None:
    if hasattr(st, "iframe"):  # streamlit >= 1.52
        st.iframe(src, height=height)
    else:
        import streamlit.components.v1 as components

        components.iframe(src, height=height)


@st.cache_resource
def sessions() -> Dict[str, ClipSession]:
    """Lives for the whole server process, so recording survives page reruns."""
    return {}


def host() -> str:
    try:
        return (st.context.headers.get("host") or "localhost").split(":")[0]
    except Exception:
        return "localhost"


def fmt_secs(s: float) -> str:
    s = int(s)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def show_kit(kit_path: str) -> None:
    p = Path(kit_path)
    if p.exists():
        st.code(p.read_text(encoding="utf-8"), language=None)


def show_clip_result(fut) -> None:
    try:
        info = fut.result(timeout=300)
    except Exception as e:  # noqa: BLE001 - surface any ffmpeg / buffer error to the user
        st.error(f"Clip failed: {e}")
        return
    st.success(f"Saved `{info.path}` ({info.duration}s)")
    v, k = st.columns([1, 1]) if info.fmt.startswith("vertical") else (st.container(), st.container())
    with v:
        st.video(info.path)
    with k:
        st.caption("Paste into YouTube Studio:")
        show_kit(info.kit)


# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.title("✂️ GTAVI")
    st.caption("YouTube live → Shorts, instantly")

    stream = st.text_input("YouTube @handle or live URL", placeholder="@MrBeast  ·  youtube.com/live/…  ·  youtu.be/…",
                           help="Twitch (twitch.tv/name) and Kick (kick.com/name) links work too.")
    buffer_min = st.slider("Replay buffer (minutes)", 1, 30, 5,
                           help="How far back you can clip. ~1 GB/hour at 1080p60 while running.")
    tags = st.text_input("Hashtags for your Shorts", "gaming live", help="Space separated, # optional")
    with st.expander("🤖 Auto-clip when chat explodes", expanded=True):
        auto = st.toggle("Auto-clip on chat hype", value=False,
                         help="Watches live chat for spikes in messages, 😂💀🤯, 'NO WAY', 'CLIP IT'…")
        sens = st.slider("Sensitivity (lower = more clips)", 1.5, 5.0, 2.5, 0.1)
        pre = st.slider("Seconds before the hype", 5, 60, 25,
                        help="YouTube chat lags the video a bit, so grab plenty before")
        post = st.slider("Seconds after", 0, 20, 5)
        auto_fmt = st.selectbox("Auto-clip format", list(FORMATS), format_func=FORMATS.get)
    with st.expander("Advanced"):
        engine = st.selectbox("Stream engine", ["streamlink", "yt-dlp"],
                              help="Switch to yt-dlp if streamlink can't open a stream.")
        quality = st.text_input("Quality", "best", help="e.g. best, 1080p60, 720p, 480p")

    if st.button("▶ Start buffering", type="primary", use_container_width=True, disabled=not stream):
        try:
            src = parse_source(stream)
            reg = sessions()
            if src.label in reg and reg[src.label].running:
                st.info("Already recording that one.")
                st.session_state.active = src.label
            else:
                with st.spinner("Finding the live stream…"):
                    s = ClipSession(src, DATA, buffer_min * 60, engine, quality, auto, pre, post, auto_fmt,
                                    sens, tags=tags.split())
                s.start()
                reg[s.source.label] = s
                st.session_state.active = s.source.label
        except NotLiveError as e:
            st.warning(f"📴 {e}")
        except Exception as e:  # noqa: BLE001
            st.error(str(e))

    reg = sessions()
    if reg:
        st.divider()
        st.subheader("Streams")
        for label, s in list(reg.items()):
            c1, c2 = st.columns([4, 1])
            dot = "🔴" if s.running else "⚫"
            if c1.button(f"{dot} {s.source.title or label}"[:40], key=f"pick-{label}", use_container_width=True):
                st.session_state.active = label
            if c2.button("✖", key=f"stop-{label}", help="Stop & discard buffer"):
                s.stop()
                del reg[label]
                if st.session_state.get("active") == label:
                    st.session_state.active = None
                st.rerun()

live_tab, vod_tab, gallery_tab = st.tabs(["🔴 Live", "🎞 Clip a past video", "📁 My Shorts"])

# ------------------------------------------------------------------ live
with live_tab:
    active = st.session_state.get("active")
    s = sessions().get(active) if active else None
    if not s:
        st.markdown(
            "### Paste a YouTube channel in the sidebar and hit **Start buffering**\n"
            "GTAVI keeps a rolling replay of the live stream on your disk. When something happens, "
            "hit a clip button and get the **last N seconds as a vertical Short**, with a hook caption "
            "and a ready-to-paste title, description and credit link.\n\n"
            "Turn on **auto-clip** and it clips by itself whenever live chat goes crazy."
        )
    else:
        s.auto_clip, s.auto_pre, s.auto_post, s.auto_fmt = auto, pre, post, auto_fmt
        s.detector.sensitivity = sens
        s.tags = tags.split()

        if s.source.title:
            st.markdown(f"#### 🔴 [{s.source.title}]({s.source.share_url})")
        player, chat = st.columns([3, 1])
        with player:
            embed = s.source.embed_url(host())
            if embed:
                iframe(embed, height=470)
            else:
                st.info("No embeddable player for this site. Recording still works, watch it in another tab.")
        with chat:
            chat_url = s.source.chat_embed_url(host())
            if chat_url:
                iframe(chat_url, height=470)

        c_fmt, c_cap, c_fast = st.columns([2, 2, 1])
        fmt = c_fmt.selectbox("Format", list(FORMATS), format_func=FORMATS.get, key="live-fmt")
        caption = c_cap.text_input("Hook caption (optional)", placeholder="WAIT FOR IT 😳", key="live-cap",
                                   help="Big text burned onto the top of the Short")
        fast = c_fast.toggle("⚡ Instant", disabled=fmt != "original" or bool(caption),
                             help="Original format only: no re-encode, done in <1s, cuts snap to keyframes.")

        lengths = (15, 30, 45, 60, 90, SHORTS_MAX_SECONDS)
        cols = st.columns(len(lengths) + 1)
        for col, secs in zip(cols, lengths):
            label = f"✂ {secs}s" if secs < 60 else f"✂ {secs // 60}m" if secs % 60 == 0 else f"✂ {secs}s"
            if col.button(label, use_container_width=True, type="primary" if secs == 30 else "secondary",
                          help=f"Clip the last {secs} seconds"):
                with st.spinner("Cutting your Short…"):
                    show_clip_result(s.clip_last(secs, fmt, fast, caption=caption))
        if cols[-1].button("⚑ Mark", use_container_width=True, help="Drop a marker now, clip around it later"):
            st.toast(f"Marked at {fmt_secs(s.mark())}")

        if s.markers:
            with st.expander(f"⚑ Markers ({len(s.markers)})", expanded=True):
                for i, t in enumerate(reversed(s.markers)):
                    m1, m2 = st.columns([3, 1])
                    ago = s.recorder.now - t
                    gone = t - 20 < s.recorder.oldest
                    m1.write(f"Marker at {fmt_secs(t)} · {fmt_secs(ago)} ago" + (" · *partly out of buffer*" if gone else ""))
                    if m2.button("✂ −20s / +10s", key=f"mk-{i}-{t}"):
                        with st.spinner("Cutting…"):
                            show_clip_result(s.clip_range(t - 20, t + 10, fmt, fast, reason="marker", caption=caption))

        @st.fragment(run_every=2)
        def status() -> None:
            r = s.recorder
            a, b, c, d = st.columns(4)
            a.metric("Recorder", "🔴 live" if r.running else "stopped")
            b.metric("Buffered", fmt_secs(r.buffered), help=f"max {fmt_secs(r.buffer_seconds)}")
            if s.chat:
                score, base = s.detector.rates(time.time())
                c.metric("Chat msgs", s.chat.message_count, help="since you started buffering")
                d.metric("Hype", f"x{score / max(base, 0.05):.1f}", help=f"{score:.1f}/s vs {base:.1f}/s baseline")
            if r.error:
                st.error(f"Stream stopped:\n\n```\n{r.error}\n```")
            elif r.running and r.buffered == 0:
                st.info("Connecting to stream… first segments arrive in a few seconds.")
            for e in s.errors[-3:]:
                st.warning(e)
            if s.events:
                st.caption("Recent hype: " + " · ".join(
                    f"{datetime.fromtimestamp(e.at):%H:%M:%S} {e.describe()}" for e in s.events[-5:][::-1]))
            new = [c for c in s.clips if c.reason.startswith("hype")][-1:]
            if new:
                st.caption(f"Latest auto-clip: `{Path(new[0].path).name}` → see **My Shorts**")

        status()

# ------------------------------------------------------------------ vod
with vod_tab:
    st.markdown("Clip any time range from a past livestream or video. YouTube, Twitch VODs and more "
                "(anything yt-dlp supports). Only that section is downloaded.")
    url = st.text_input("Video URL", key="vod-url", placeholder="https://www.youtube.com/watch?v=…")
    v1, v2, v3 = st.columns(3)
    start = v1.text_input("Start", "0:00", help="1:23:45, 83:20, 5m30s, 330")
    end = v2.text_input("End", "0:30")
    vfmt = v3.selectbox("Format", list(FORMATS), format_func=FORMATS.get, key="vod-fmt")
    vcap = st.text_input("Hook caption (optional)", key="vod-cap")
    if st.button("✂ Make Short", type="primary", disabled=not url):
        try:
            t0, t1 = parse_time(start), parse_time(end)
            if vfmt.startswith("vertical") and t1 - t0 > SHORTS_MAX_SECONDS:
                st.warning(f"Shorts can be at most {SHORTS_MAX_SECONDS}s; trimming to that.")
                t1 = t0 + SHORTS_MAX_SECONDS
            dest = DATA / "clips" / "vod" / f"{datetime.now():%Y%m%d-%H%M%S}_{vfmt}.mp4"
            with st.spinner("Downloading just that section…"):
                download_section(url, t0, t1, dest, vfmt, caption=vcap)
            src = parse_source(url)
            kit = shorts_kit(dest, "", f"{src.share_url}{'?t=' + str(int(t0)) if src.video_id else ''}",
                             tags=tags.split())
            st.success(f"Saved `{dest}`")
            a, b = st.columns(2)
            a.video(str(dest))
            with b:
                show_kit(str(kit))
        except Exception as e:  # noqa: BLE001
            st.error(str(e))

# ------------------------------------------------------------------ gallery
with gallery_tab:
    files = sorted((DATA / "clips").glob("**/*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        st.info("No clips yet.")
    else:
        st.caption(f"{len(files)} clips in `{(DATA / 'clips').resolve()}` · upload at studio.youtube.com")
        grid = st.columns(4)
        for i, f in enumerate(files[:12]):
            with grid[i % 4]:
                st.video(str(f))
                st.caption(f"**{f.parent.name}** · {f.stem}")
                kit = f.with_suffix(".txt")
                if kit.exists():
                    with st.popover("📋 Title & description", use_container_width=True):
                        show_kit(str(kit))
                g1, g2 = st.columns(2)
                with open(f, "rb") as fh:
                    g1.download_button("⬇", fh, file_name=f.name, key=f"dl-{f}", use_container_width=True)
                if g2.button("🗑", key=f"del-{f}", use_container_width=True):
                    f.unlink(missing_ok=True)
                    kit.unlink(missing_ok=True)
                    st.rerun()
