"""Figure out what the user pasted: a YouTube @handle, a live URL, a Twitch/Kick link..."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import parse_qs, urlparse


def tool_env() -> Dict[str, str]:
    """Env for child processes: make sure tools installed next to this Python (deno, streamlink,
    yt-dlp) are on PATH even when the venv isn't activated."""
    env = dict(os.environ)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    return env


@dataclass(frozen=True)
class Source:
    platform: str             # "youtube" | "twitch" | "kick" | "other"
    url: str                  # full URL handed to streamlink / yt-dlp
    channel: Optional[str]    # twitch/kick channel, youtube @handle or channel id
    video_id: Optional[str] = None  # youtube only, filled by resolve()
    title: Optional[str] = None

    @property
    def label(self) -> str:
        if self.platform == "youtube":
            return f"youtube:{self.channel or self.video_id or self.url}"
        return f"{self.platform}:{self.channel}" if self.channel else self.url

    @property
    def share_url(self) -> str:
        if self.platform == "youtube" and self.video_id:
            return f"https://youtu.be/{self.video_id}"
        return self.url

    def embed_url(self, parent: str = "localhost") -> Optional[str]:
        """URL for an <iframe> player, or None if the platform can't be embedded."""
        if self.platform == "youtube" and self.video_id:
            return f"https://www.youtube.com/embed/{self.video_id}?autoplay=1&mute=1"
        if self.platform == "twitch" and self.channel:
            return f"https://player.twitch.tv/?channel={self.channel}&parent={parent}&muted=true"
        if self.platform == "kick" and self.channel:
            return f"https://player.kick.com/{self.channel}?muted=true"
        return None

    def chat_embed_url(self, parent: str = "localhost") -> Optional[str]:
        if self.platform == "youtube" and self.video_id:
            return f"https://www.youtube.com/live_chat?v={self.video_id}&embed_domain={parent}&dark_theme=1"
        if self.platform == "twitch" and self.channel:
            return f"https://www.twitch.tv/embed/{self.channel}/chat?parent={parent}&darkpopout"
        return None


_HANDLE = re.compile(r"^@?[A-Za-z0-9_.\-]{3,30}$")


def _yt(url: str, channel: Optional[str] = None, video_id: Optional[str] = None) -> Source:
    return Source("youtube", url, channel, video_id)


def parse_source(text: str) -> Source:
    """YouTube first: `@handle`, `handle`, youtube.com/@x, /live/<id>, watch?v=, youtu.be/...
    Also takes twitch.tv/x, kick.com/x and any other URL streamlink / yt-dlp understands."""
    text = text.strip()
    if not text:
        raise ValueError("Enter a YouTube @handle or stream URL")

    # "@name" is always a handle; a bare word without dots is too (dots mean a domain like kick.com)
    if (text.startswith("@") and "/" not in text and " " not in text) or (_HANDLE.match(text) and "." not in text):
        handle = text if text.startswith("@") else "@" + text
        return _yt(f"https://www.youtube.com/{handle}/live", handle)

    if "://" not in text:
        text = "https://" + text
    u = urlparse(text)
    host = (u.hostname or "").lower()
    for prefix in ("www.", "m.", "music."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    parts = [p for p in u.path.split("/") if p]

    if host == "youtu.be" and parts:
        return _yt(f"https://www.youtube.com/watch?v={parts[0]}", None, parts[0])
    if host.endswith("youtube.com"):
        vid = parse_qs(u.query).get("v", [None])[0]
        if not vid and len(parts) >= 2 and parts[0] in ("live", "shorts", "embed"):
            vid = parts[1]
        if vid:
            return _yt(f"https://www.youtube.com/watch?v={vid}", None, vid)
        if parts and parts[0].startswith("@"):
            return _yt(f"https://www.youtube.com/{parts[0]}/live", parts[0])
        if len(parts) >= 2 and parts[0] in ("channel", "c", "user"):
            return _yt(f"https://www.youtube.com/{parts[0]}/{parts[1]}/live", parts[1])
        return _yt(text)
    if host.endswith("twitch.tv") and parts:
        name = parts[0].lower()
        return Source("twitch", f"https://www.twitch.tv/{name}", name)
    if host.endswith("kick.com") and parts:
        name = parts[0].lower()
        return Source("kick", f"https://kick.com/{name}", name)
    return Source("other", text, None)


class NotLiveError(RuntimeError):
    pass


def resolve(source: Source, timeout: float = 60) -> Source:
    """For YouTube: turn @handle/live into the actual live video id + title, and check it's live."""
    if source.platform != "youtube":
        return source
    proc = subprocess.run(
        [sys.executable, "-m", "yt_dlp", "--quiet", "--no-warnings", "--no-playlist", "--skip-download",
         "--print", "%(id)s\t%(live_status)s\t%(uploader_id|)s\t%(title)s", source.url],
        capture_output=True, text=True, timeout=timeout, env=tool_env(),
    )
    out = proc.stdout.strip().splitlines()
    if proc.returncode != 0 or not out:
        err = proc.stderr.strip().splitlines()
        msg = err[-1] if err else "could not load this YouTube page"
        if "404" in msg:
            raise NotLiveError(f"Couldn't find {source.channel or source.url} on YouTube. Check the handle.")
        if "not currently live" in msg:
            raise NotLiveError(f"{source.channel or 'That channel'} isn't live right now.")
        if "will begin" in msg or "Premieres" in msg:
            raise NotLiveError(msg.split(": ")[-1])
        raise NotLiveError(f"YouTube: {msg.replace('ERROR: ', '')}")
    vid, status, uploader, title = (out[-1].split("\t") + ["", "", "", ""])[:4]
    if status not in ("is_live", "post_live"):
        raise NotLiveError(f"“{title}” isn't live right now ({status}). Use the VOD clipper tab for past videos.")
    channel = source.channel or (uploader or None)
    title = re.sub(r"\s*\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", "", title)  # yt-dlp appends a timestamp to live titles
    return replace(source, url=f"https://www.youtube.com/watch?v={vid}", video_id=vid, title=title,
                   channel=channel)
