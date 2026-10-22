"""后处理：清洗 AI 腔、去格式、拆气泡、算延迟。"""

from __future__ import annotations

import random
import re

from .config import (
    BUBBLE_DELAY_CAP_MS,
    BUBBLE_DELAY_MAX_MS,
    BUBBLE_DELAY_MIN_MS,
    BUBBLE_PAD_MAX_MS,
    BUBBLE_PAD_MIN_MS,
    BUBBLE_SPLIT_AT,
    MAX_BUBBLES,
)
from .models import Bubble

_SLOP = [
    re.compile(p)
    for p in (
        r"作为一个?(大)?(语言)?模型",
        r"作为一个?\s*(AI|人工智能|助手|机器人|聊天机器人)",
        r"作为\s*(AI|人工智能)",
        r"我理解你的(感受|心情)",
        r"希望(这|以上|这些)?(对|能帮)",
        r"有什么(可以|能)帮(你|到)",
        r"我(会)?(一直|随时)(在|陪)着",
        r"抱抱你",
        r"总的来说|总而言之",
    )
]

_SENTENCE = re.compile(r"[^。！？!?～~…\n]+[。！？!?～~…]*")
_END_PUNCT = "。！？!?～~…，,"


def _is_slop(sentence: str) -> bool:
    return any(p.search(sentence) for p in _SLOP)


def clean(text: str) -> str:
    """去掉 Markdown、代码块、列表前缀、成段动作描写和 AI 腔整句。"""
    t = text.strip()
    if not t:
        return ""

    t = re.sub(r"```.*?```", "", t, flags=re.S)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    t = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", t)
    t = re.sub(r"`(.+?)`", r"\1", t)
    t = re.sub(r"^\s*[-*•]\s+", "", t, flags=re.M)
    t = re.sub(r"^\s*\d+[.、)]\s+", "", t, flags=re.M)

    # 动作/神态描写：微信里没人发"（看了眼讲台上的老师，低头打字）"。
    # 短的保留（（笑）、（叹气）是真人会发的），长的删掉。
    t = re.sub(r"[（(][^）)]{8,}[）)]", "", t)

    # 只删命中的整句，保留换行（换行是气泡分隔符，不能丢）
    if any(p.search(t) for p in _SLOP):
        t = _SENTENCE.sub(lambda m: "" if _is_slop(m.group(0)) else m.group(0), t)

    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"\n{2,}", "\n", t)
    return t.strip().strip("\"“”")


def _split_commas(text: str) -> list[str]:
    parts = re.split(r"(?<=[，,；;])", text)
    out: list[str] = []
    buf = ""
    for part in parts:
        if not part:
            continue
        if not buf or len(buf) + len(part) <= BUBBLE_SPLIT_AT:
            buf += part
        else:
            out.append(buf)
            buf = part
    if buf:
        out.append(buf)
    return out


def split_bubbles(text: str, max_bubbles: int = MAX_BUBBLES) -> list[str]:
    """把一段回复切成 1–3 条短气泡。"""
    t = clean(text)
    if not t:
        return []

    chunks = [c.strip() for c in re.split(r"\n+", t) if c.strip()]
    if len(chunks) == 1:
        chunks = [s.strip() for s in _SENTENCE.findall(chunks[0]) if s.strip()]

    expanded: list[str] = []
    for chunk in chunks:
        if len(chunk) <= BUBBLE_SPLIT_AT:
            expanded.append(chunk)
        else:
            expanded.extend(_split_commas(chunk))

    while len(expanded) > max_bubbles:
        i = min(range(len(expanded) - 1), key=lambda k: len(expanded[k]) + len(expanded[k + 1]))
        merged = expanded[i] + expanded[i + 1]
        if merged and merged[-1] not in _END_PUNCT:
            merged += "，"
        expanded[i : i + 2] = [merged]

    return [c for c in expanded if c.strip()]


def delay_for(text: str, rng: random.Random | None = None) -> int:
    rng = rng or random.Random()
    ms = len(text) * rng.randint(BUBBLE_DELAY_MIN_MS, BUBBLE_DELAY_MAX_MS)
    ms += rng.randint(BUBBLE_PAD_MIN_MS, BUBBLE_PAD_MAX_MS)
    return min(ms, BUBBLE_DELAY_CAP_MS)


def to_bubbles(text: str, rng: random.Random | None = None) -> list[Bubble]:
    rng = rng or random.Random()
    return [Bubble(part, delay_for(part, rng)) for part in split_bubbles(text)]
