import pytest

from gtavi.cli import parse_time
import json

from gtavi.clipper import shorts_kit, video_filter
from gtavi.hype import HypeDetector, message_weight, parse_privmsg, parse_youtube_chat_line
from gtavi.sources import parse_source


@pytest.mark.parametrize("text,platform,channel,vid,url", [
    ("@LofiGirl", "youtube", "@LofiGirl", None, "https://www.youtube.com/@LofiGirl/live"),
    ("@a_very_long_handle_over_thirty_chars", "youtube", "@a_very_long_handle_over_thirty_chars", None, None),
    ("MrBeast", "youtube", "@MrBeast", None, "https://www.youtube.com/@MrBeast/live"),
    ("youtube.com/@some.name", "youtube", "@some.name", None, "https://www.youtube.com/@some.name/live"),
    ("https://www.youtube.com/@x_y/streams", "youtube", "@x_y", None, "https://www.youtube.com/@x_y/live"),
    ("https://www.youtube.com/channel/UCabc", "youtube", "UCabc", None, "https://www.youtube.com/channel/UCabc/live"),
    ("https://www.youtube.com/watch?v=abc123XYZ01", "youtube", None, "abc123XYZ01", None),
    ("https://youtu.be/abc123XYZ01?si=x", "youtube", None, "abc123XYZ01", None),
    ("https://www.youtube.com/live/abc123XYZ01?feature=share", "youtube", None, "abc123XYZ01", None),
    ("m.youtube.com/watch?v=abc123XYZ01", "youtube", None, "abc123XYZ01", None),
    ("twitch.tv/KaiCenat", "twitch", "kaicenat", None, "https://www.twitch.tv/kaicenat"),
    ("https://kick.com/adinross", "kick", "adinross", None, "https://kick.com/adinross"),
    ("kick.com/adinross", "kick", "adinross", None, "https://kick.com/adinross"),
    ("https://example.com/stream.m3u8", "other", None, None, "https://example.com/stream.m3u8"),
])
def test_parse_source(text, platform, channel, vid, url):
    s = parse_source(text)
    assert (s.platform, s.channel, s.video_id) == (platform, channel, vid)
    if url:
        assert s.url == url


def test_youtube_embeds():
    s = parse_source("https://youtu.be/abc123XYZ01")
    assert s.embed_url() == "https://www.youtube.com/embed/abc123XYZ01?autoplay=1&mute=1"
    assert "live_chat?v=abc123XYZ01&embed_domain=example.org" in s.chat_embed_url("example.org")
    assert s.share_url == "https://youtu.be/abc123XYZ01"


def test_parse_youtube_chat_line():
    line = json.dumps({"replayChatItemAction": {"actions": [
        {"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {
            "message": {"runs": [{"text": "NO WAY "}, {"emoji": {"emojiId": "😂", "shortcuts": [":joy:"]}},
                                 {"emoji": {"emojiId": "UCxyz/abcdef", "shortcuts": [":custom-hype:"]}}]},
            "authorName": {"simpleText": "@fan"}, "timestampUsec": "1794919195000000"}}}},
        {"addChatItemAction": {"item": {"giftMessageViewModel": {}}}},
    ]}}).encode()
    assert parse_youtube_chat_line(line) == [("@fan", "NO WAY 😂:custom-hype:", 1794919195.0)]
    assert parse_youtube_chat_line(b"not json") == []


def test_emoji_counts_as_hype():
    assert message_weight("😂😂💀") > message_weight("nice")


def test_caption_filter(tmp_path):
    f = video_filter("vertical-blur", tmp_path / "caption.txt", lines=2)
    assert f.count("drawtext=") == 2 and "caption1.txt" in f and f.endswith("[v]")
    assert video_filter("original") is None
    assert "drawtext" in video_filter("original", tmp_path / "c.txt")


def test_shorts_kit(tmp_path):
    clip = tmp_path / "x.mp4"
    kit = shorts_kit(clip, "GTA 6 LAUNCH STREAM | day 1 2026-11-19 23:01", "https://youtu.be/abc", "hype chat x5", ["gta6", "#gaming"])
    text = kit.read_text(encoding="utf-8")
    assert "GTA 6 LAUNCH STREAM 😳 #shorts" in text
    assert "https://youtu.be/abc" in text
    assert "#shorts #gta6 #gaming" in text


def test_twitch_embed_has_parent():
    assert "parent=example.org" in parse_source("twitch.tv/xqc").embed_url("example.org")


@pytest.mark.parametrize("text,secs", [
    ("90", 90), ("1:30", 90), ("1:02:03", 3723), ("5m30s", 330), ("1h", 3600), ("2.5", 2.5),
])
def test_parse_time(text, secs):
    assert parse_time(text) == secs


def test_privmsg():
    line = ":bob!bob@bob.tmi.twitch.tv PRIVMSG #xqc :KEKW no way"
    assert parse_privmsg(line) == ("bob", "KEKW no way")
    assert parse_privmsg("PING :tmi.twitch.tv") is None


def test_hype_tokens_weigh_more():
    assert message_weight("CLIP IT") > message_weight("hello there")
    assert message_weight("clip " * 50) <= 6


def test_hype_fires_on_spike_then_cools_down():
    d = HypeDetector(window=8, sensitivity=2.5, min_score=1.5, cooldown=45)
    t = 1000.0
    for i in range(120):  # two quiet minutes: 1 msg / 2s
        d.feed("hi", t + i * 2)
    now = t + 240
    assert d.check(now) is None
    for i in range(80):  # chat explodes
        d.feed("KEKW NO WAY", now + i * 0.1)
    ev = d.check(now + 8)
    assert ev is not None and ev.ratio > 2.5
    assert ev.top_words[0][0] in {"kekw", "no", "way"}
    for i in range(80):
        d.feed("KEKW", now + 9 + i * 0.1)
    assert d.check(now + 17) is None  # cooldown


def test_hype_needs_baseline():
    d = HypeDetector()
    for i in range(50):
        d.feed("POG", 0 + i * 0.1)
    assert d.check(5) is None


def test_clean_caption():
    from gtavi.clipper import clean_caption
    assert clean_caption("wait for it 😳🔥  this is   crazy!") == "wait for it this is crazy!"
    assert clean_caption("café niño") == "café niño"


def test_ui_passes_streamlit_flags(monkeypatch):
    import gtavi.cli as cli
    seen = {}
    monkeypatch.setattr(cli.subprocess, "call", lambda cmd: seen.setdefault("cmd", cmd) and 0)
    with pytest.raises(SystemExit):
        cli.main(["ui", "--server.address=0.0.0.0", "--server.port=8501"])
    assert seen["cmd"][-2:] == ["--server.address=0.0.0.0", "--server.port=8501"]
    assert seen["cmd"][-3].endswith("app.py")
