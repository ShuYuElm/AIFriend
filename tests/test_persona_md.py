"""Markdown 人设稿 → persona.yaml（app/persona_md.py）。"""

import yaml

from app.persona import parse_persona
from app.persona_md import convert, parse_markdown, render_yaml

SAMPLE = """\
name: 林夏
user_nickname: 阿川
stage: 暧昧（还没表白）
known_since: 2026-07-19

## description
24 岁，成都一家书店的店员。

## personality
句子短，爱连发。

## scenario
三个月前在读书群认识。

## system_prompt
你是林夏，书店店员。

## post_history_instructions
只发短消息。

## first_mes
今天书店人少

## alternate_greetings
- 醒了吗
- 外面下雨

## mes_example
<user>在吗
<char>在

## lorebook
- 书店, 上班 | 她在书店做全职店员
- !林夏 | 林夏 24 岁
"""


def test_parse_top_fields():
    data = parse_markdown(SAMPLE)
    assert data["name"] == "林夏"
    assert data["user_nickname"] == "阿川"
    assert data["known_since"] == "2026-07-19"


def test_parse_sections_keep_multiline():
    data = parse_markdown(SAMPLE)
    assert data["description"] == "24 岁，成都一家书店的店员。"
    assert "只发短消息" in data["post_history_instructions"]


def test_parse_greetings_and_lorebook():
    data = parse_markdown(SAMPLE)
    assert data["alternate_greetings"] == ["醒了吗", "外面下雨"]

    entries = data["character_book"]["entries"]
    assert [e["constant"] for e in entries] == [False, True]
    assert entries[0]["keys"] == ["书店", "上班"]
    assert entries[1]["content"] == "林夏 24 岁"


def test_convert_reports_missing_fields():
    data, missing = convert("name: 只有名字\n")
    assert "description" in missing
    assert data["spec"] == "aigirl_card_v1"


def test_render_is_valid_yaml_and_parses_as_card():
    data, missing = convert(SAMPLE)
    assert missing == []

    text = render_yaml(data)
    loaded = yaml.safe_load(text)

    assert loaded["spec"] == "aigirl_card_v1"
    assert loaded["character_book"]["entries"][0]["keys"] == ["书店", "上班"]

    card = parse_persona(loaded)  # 生成的东西必须能被真实加载器接受
    assert card.name == "林夏"
    assert card.first_mes == "今天书店人少"
    assert len(card.book["entries"]) == 2


def test_unknown_section_is_ignored():
    data = parse_markdown("name: x\n## 不存在的字段\n内容\n## description\n正文\n")
    assert "不存在的字段" not in data
    assert data["description"] == "正文"
