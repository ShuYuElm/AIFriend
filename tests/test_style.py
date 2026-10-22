"""后处理与拆气泡。"""

import random

from app.models import Bubble
from app.style import clean, delay_for, split_bubbles, to_bubbles


def test_clean_removes_markdown_and_lists():
    assert clean("**你好**\n- 第一\n- 第二") == "你好\n第一\n第二"


def test_clean_preserves_newlines():
    # 换行是气泡分隔符，clean 不能把它吃掉
    assert clean("你猜\n刚在宿舍") == "你猜\n刚在宿舍"


def test_clean_strips_long_action_narration():
    # 微信里不会发这种动作描写
    assert clean("（看了眼讲台上的老师，低头打字）上课呢") == "上课呢"


def test_clean_keeps_short_gestures():
    # 但（笑）、（叹气）这种真人会发
    assert clean("（笑）你少来") == "（笑）你少来"


def test_clean_drops_ai_slop_sentences():
    text = "作为一个 AI，我不能这样。你吃了吗"
    assert clean(text) == "你吃了吗"


def test_clean_strips_wrapping_quotes():
    assert clean("“你吃了吗”") == "你吃了吗"


def test_split_single_short_reply():
    assert split_bubbles("嗯") == ["嗯"]


def test_split_by_sentence():
    assert split_bubbles("又加？饭吃了没") == ["又加？", "饭吃了没"]


def test_split_by_newline():
    assert split_bubbles("又加？\n饭吃了没") == ["又加？", "饭吃了没"]


def test_split_caps_at_three():
    text = "第一句。第二句。第三句。第四句。第五句。"
    assert len(split_bubbles(text)) == 3


def test_split_long_sentence_by_comma():
    text = "我今天下午去书店兼职的时候碰到一个同学，他文件发了三次还是不对，最后还怪打印出来的版式有问题"
    bubbles = split_bubbles(text)
    assert 1 <= len(bubbles) <= 3
    assert all(len(b) <= 60 for b in bubbles)


def test_split_empty_returns_empty():
    assert split_bubbles("   ") == []


def test_delay_bounds():
    rng = random.Random(1)
    d = delay_for("你好", rng)
    assert 200 <= d <= 6000
    assert d >= len("你好") * 40


def test_to_bubbles_keeps_order():
    rng = random.Random(2)
    bubbles = to_bubbles("又加？\n饭吃了没", rng)
    assert isinstance(bubbles[0], Bubble)
    assert [b.text for b in bubbles] == ["又加？", "饭吃了没"]
    assert all(b.delay_ms > 0 for b in bubbles)
