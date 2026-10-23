"""Whisper hallucination guard."""
import re

HALLUCINATIONS = [
    "谢谢观看", "感谢观看", "请不吝点赞", "订阅转发", "点赞订阅", "字幕由", "amara.org",
    "字幕志愿者", "中文字幕", "thanks for watching", "thank you for watching",
    "please subscribe", "subtitles by", "ご視聴ありがとうございました",
    "ご視聴ありがとうございます", "チャンネル登録", "ありがとうございました",
]


# Short stock phrases Whisper emits on noise; dropped only when they sit outside VAD speech
# (exact match, so a real "thank you" spoken inside a speech range survives).
HALLUCINATION_EXACT = {"thankyou", "thanks", "bye", "byebye", "谢谢", "謝謝", "谢谢大家", "ありがとう"}


def _norm(s: str) -> str:
    return re.sub(r"[\s\W_]+", "", s.lower())


def is_hallucination(text: str) -> bool:
    t = _norm(text)
    if not t:
        return True
    return any(_norm(h) in t for h in HALLUCINATIONS) or t in HALLUCINATION_EXACT


def filter_segments(segments, speech_ranges):
    """Drop known hallucination strings when the segment lies (mostly) outside speech."""
    out = []
    for s in segments:
        if is_hallucination(s["text"]):
            ov = sum(max(0.0, min(s["end"], b) - max(s["start"], a)) for a, b in speech_ranges)
            dur = max(s["end"] - s["start"], 1e-6)
            if ov / dur < 0.5:
                continue
        out.append(s)
    return out
