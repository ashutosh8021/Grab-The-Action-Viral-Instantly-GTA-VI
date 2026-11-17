"""Auto-detect clip-worthy moments from Twitch chat.

When something crazy happens on stream, chat explodes: message rate spikes and fills
with KEKW / OMEGALUL / "CLIP IT". We compare the last few seconds against the
recent baseline and fire a HypeEvent when it jumps.
"""
from __future__ import annotations

import json
import random
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Deque, List, Optional, Tuple

# Messages containing these count extra. Case-insensitive, whole-word.
HYPE_TOKENS = {
    "clip": 4, "clipit": 4, "clipped": 3,
    "pog": 2, "pogchamp": 2, "poggers": 2, "pogu": 2,
    "kekw": 2, "omegalul": 2, "lul": 1, "lmao": 1, "lmfao": 2, "lol": 1,
    "monkas": 2, "pausechamp": 2, "wtf": 2, "omg": 2, "holy": 2,
    "w": 1, "www": 1, "ez": 1, "gg": 1, "insane": 2, "no way": 2,
}
# YouTube chat is emoji-heavy, so reactions count too.
HYPE_EMOJI = {
    "😂": 2, "🤣": 2, "💀": 2, "😭": 1, "🤯": 2, "😱": 2, "😳": 1, "🔥": 1,
    "‼": 1, "❗": 1, "🐐": 1, "👀": 1, "🫨": 2, "🤡": 1,
}
_TOKEN_RE = re.compile(r"[a-z]+")


def message_weight(text: str) -> float:
    low = text.lower()
    words = _TOKEN_RE.findall(low)
    w = 1.0 + sum(HYPE_TOKENS.get(t, 0) for t in set(words))
    if "no way" in low:
        w += HYPE_TOKENS["no way"]
    w += sum(v for e, v in HYPE_EMOJI.items() if e in text)
    return min(w, 6.0)  # one spammy message can't trigger a clip alone


@dataclass
class HypeEvent:
    at: float            # wall-clock time of detection
    score: float         # weighted msgs/sec in the window
    baseline: float      # weighted msgs/sec before it
    top_words: List[Tuple[str, int]] = field(default_factory=list)

    @property
    def ratio(self) -> float:
        return self.score / max(self.baseline, 0.01)

    def describe(self) -> str:
        words = " ".join(w for w, _ in self.top_words[:3])
        return f"chat x{self.ratio:.1f} ({words})" if words else f"chat x{self.ratio:.1f}"


class HypeDetector:
    """Pure logic, no networking: feed messages, poll for events."""

    def __init__(
        self,
        window: float = 8.0,
        baseline_window: float = 90.0,
        sensitivity: float = 2.5,
        min_score: float = 1.5,
        cooldown: float = 45.0,
    ):
        self.window = window
        self.baseline_window = baseline_window
        self.sensitivity = sensitivity
        self.min_score = min_score
        self.cooldown = cooldown
        self._msgs: Deque[Tuple[float, float, str]] = deque()
        self._last_fire = -1e18
        self.started = None  # type: Optional[float]

    def feed(self, text: str, at: Optional[float] = None) -> None:
        at = time.time() if at is None else at
        if self.started is None:
            self.started = at
        self._msgs.append((at, message_weight(text), text))

    def rates(self, now: float) -> Tuple[float, float]:
        horizon = now - self.window - self.baseline_window
        while self._msgs and self._msgs[0][0] < horizon:
            self._msgs.popleft()
        cut = now - self.window
        recent = sum(w for t, w, _ in self._msgs if t >= cut)
        older = sum(w for t, w, _ in self._msgs if t < cut)
        span = min(self.baseline_window, max(cut - (self.started or cut), 1.0))
        return recent / self.window, older / span

    def check(self, now: Optional[float] = None) -> Optional[HypeEvent]:
        now = time.time() if now is None else now
        if self.started is None or now - self.started < self.window + 20:
            return None  # need some baseline first
        if now - self._last_fire < self.cooldown:
            return None
        score, baseline = self.rates(now)
        if score < self.min_score or score < self.sensitivity * max(baseline, 0.05):
            return None
        self._last_fire = now
        words = Counter()
        for t, _, text in self._msgs:
            if t >= now - self.window:
                words.update(w for w in set(_TOKEN_RE.findall(text.lower())) if w in HYPE_TOKENS)
                words.update(e for e in HYPE_EMOJI if e in text)
        return HypeEvent(now, score, baseline, words.most_common(5))


class TwitchChat:
    """Anonymous, read-only Twitch IRC client. No login or API key needed."""

    HOST, PORT = "irc.chat.twitch.tv", 6667

    def __init__(self, channel: str, on_message: Callable[[str, str], None]):
        self.channel = channel.lower().lstrip("#")
        self.on_message = on_message
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.connected = False
        self.message_count = 0

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="gtavi-chat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        backoff = 1
        while not self._stop.is_set():
            try:
                self._session()
                backoff = 1
            except OSError:
                self.connected = False
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30)

    def _session(self) -> None:
        with socket.create_connection((self.HOST, self.PORT), timeout=10) as sock:
            sock.settimeout(1.0)
            nick = f"justinfan{random.randint(10000, 99999)}"
            sock.sendall(f"NICK {nick}\r\nJOIN #{self.channel}\r\n".encode())
            self.connected = True
            buf = b""
            last_rx = time.time()
            while not self._stop.is_set():
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    if time.time() - last_rx > 360:
                        raise OSError("chat idle, reconnecting")
                    continue
                if not chunk:
                    raise OSError("chat disconnected")
                last_rx = time.time()
                buf += chunk
                *lines, buf = buf.split(b"\r\n")
                for raw in lines:
                    line = raw.decode("utf-8", "ignore")
                    if line.startswith("PING"):
                        sock.sendall(line.replace("PING", "PONG", 1).encode() + b"\r\n")
                        continue
                    parsed = parse_privmsg(line)
                    if parsed:
                        self.message_count += 1
                        self.on_message(*parsed)
        self.connected = False


def parse_privmsg(line: str) -> Optional[Tuple[str, str]]:
    """':nick!nick@nick.tmi.twitch.tv PRIVMSG #chan :hello' -> ('nick', 'hello')"""
    if " PRIVMSG " not in line or not line.startswith(":"):
        return None
    prefix, _, rest = line[1:].partition(" PRIVMSG ")
    _, _, text = rest.partition(" :")
    return prefix.split("!", 1)[0], text


class YouTubeChat:
    """Read-only YouTube live chat, no API key: yt-dlp streams the chat to a JSON-lines file
    as it happens and we tail it."""

    def __init__(self, video_url: str, on_message: Callable[[str, str], None]):
        self.video_url = video_url
        self.on_message = on_message
        self._stop = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self.connected = False
        self.message_count = 0

    def start(self) -> None:
        self._stop.clear()
        threading.Thread(target=self._loop, name="gtavi-ytchat", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()

    def _loop(self) -> None:
        from .sources import tool_env

        backoff = 2
        while not self._stop.is_set():
            started = time.time()
            with tempfile.TemporaryDirectory(prefix="gtavi-chat-") as tmp:
                self._proc = subprocess.Popen(
                    [sys.executable, "-m", "yt_dlp", "--quiet", "--no-warnings", "--skip-download",
                     "--write-subs", "--sub-langs", "live_chat", "-o", f"{tmp}/chat", self.video_url],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=tool_env(),
                )
                self._tail(Path(tmp), started)
                if self._proc.poll() is None:
                    self._proc.terminate()
            self.connected = False
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 60)

    def _tail(self, tmp: Path, started: float) -> None:
        pos = 0
        while not self._stop.is_set():
            files = sorted(tmp.glob("chat.live_chat.json*"), key=lambda p: len(p.name))
            path = files[0] if files else None
            if path is not None:
                self.connected = True
                with open(path, "rb") as f:
                    f.seek(pos)
                    chunk = f.read()
                # only consume complete lines
                end = chunk.rfind(b"\n") + 1
                pos += end
                for line in chunk[:end].splitlines():
                    for user, text, ts in parse_youtube_chat_line(line):
                        if ts >= started - 5:  # skip the backlog YouTube sends on join
                            self.message_count += 1
                            self.on_message(user, text)
            if self._proc.poll() is not None:
                return
            self._stop.wait(1.0)


def parse_youtube_chat_line(line: bytes) -> List[Tuple[str, str, float]]:
    """One yt-dlp live_chat JSON line -> [(author, text, unix_ts)] for normal chat messages."""
    try:
        data = json.loads(line)
    except ValueError:
        return []
    out = []
    actions = data.get("replayChatItemAction", {}).get("actions", [])
    for a in actions:
        r = a.get("addChatItemAction", {}).get("item", {}).get("liveChatTextMessageRenderer")
        if not r:
            continue
        parts = []
        for run in r.get("message", {}).get("runs", []):
            if "text" in run:
                parts.append(run["text"])
            elif "emoji" in run:
                e = run["emoji"]
                eid = e.get("emojiId", "")
                # standard emoji have the character itself as id; channel emotes use :shortcut:
                parts.append(eid if len(eid) <= 4 else (e.get("shortcuts") or [""])[0])
        author = r.get("authorName", {}).get("simpleText", "")
        ts = int(r.get("timestampUsec", 0)) / 1e6
        out.append((author, "".join(parts), ts))
    return out


def make_chat(source, on_message: Callable[[str, str], None]):
    """Chat reader for a resolved Source, or None if that platform isn't supported."""
    if source.platform == "youtube" and source.video_id:
        return YouTubeChat(source.share_url, on_message)
    if source.platform == "twitch" and source.channel:
        return TwitchChat(source.channel, on_message)
    return None
