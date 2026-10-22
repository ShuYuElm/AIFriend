"""把 Markdown 人设稿转成 persona.yaml。

用法：
    python -m app.persona_md config/persona.local.md -o config/persona.local.yaml

Markdown 写法（完整模板见 docs/persona-template.md）：

    name: 林夏
    user_nickname: 阿川
    stage: 暧昧（还没表白）
    known_since: 2026-07-19

    ## description
    她是干什么的、住哪、喜欢什么……

    ## personality
    怎么说话、什么脾气……

    ## lorebook
    - 书店, 兼职, 上班 | 她在书店做全职店员
    - !林夏 | 林夏 24 岁

规则：
  - 顶部 `key: value` 写短字段（name / user_nickname / stage / known_since）
  - `## 字段名` 之后到下一个 `##` 之间是正文，原样保留换行
  - `## lorebook` 每行一条：`关键词1, 关键词2 | 内容`；行首加 `!` 表示常驻
  - 这个工具只做格式转换。缺字段会明确报错，不会替你编。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

TEXT_FIELDS = (
    "creator_notes",
    "description",
    "personality",
    "scenario",
    "system_prompt",
    "post_history_instructions",
    "first_mes",
    "mes_example",
)
SHORT_FIELDS = ("name", "user_nickname", "stage", "known_since")
KNOWN_SECTIONS = set(TEXT_FIELDS) | {"alternate_greetings", "lorebook"}


def parse_markdown(text: str) -> dict[str, Any]:
    """Markdown → 人设卡数据结构（还没做完整性校验）。"""
    text = text.lstrip("\ufeff")  # 记事本/PowerShell 可能带 BOM
    data: dict[str, Any] = {}
    section: str | None = None
    buffer: list[str] = []
    greetings: list[str] = []
    entries: list[dict[str, Any]] = []

    def flush() -> None:
        if section is None or section not in KNOWN_SECTIONS:
            return
        body = "\n".join(buffer).strip("\n").strip()
        if section == "lorebook":
            entries.extend(_parse_lorebook(body))
        elif section == "alternate_greetings":
            greetings.extend(
                line.lstrip("-*+ ").strip()
                for line in body.splitlines()
                if line.strip().startswith(("-", "*", "+"))
            )
        elif body:
            data[section] = body

    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            flush()
            section = line[3:].strip().lower()
            buffer = []
            continue
        stripped = line.strip()
        # 以 > 开头的是给自己看的说明，以 <!-- 开头的是注释，都不进正文
        if stripped.startswith((">", "<!--")):
            continue
        if section is None:
            if not stripped or stripped.startswith("#"):
                continue
            if ":" in stripped:
                key, _, value = stripped.partition(":")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and value:
                    data[key] = value
            continue
        buffer.append(line)
    flush()

    if greetings:
        data["alternate_greetings"] = greetings
    if entries:
        data["character_book"] = {
            "scan_depth": 4,
            "token_budget": 400,
            "entries": entries,
        }
    return data


def _parse_lorebook(body: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    order = 100
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("-", "*", "+")):
            continue
        item = stripped.lstrip("-*+ ").strip()
        const = item.startswith("!")
        if const:
            item = item[1:].strip()
        keys_part, _, content = item.partition("|")
        keys = [k.strip() for k in keys_part.split(",") if k.strip()]
        content = content.strip()
        if not keys or not content:
            continue
        entries.append(
            {
                "keys": keys,
                "content": content,
                "enabled": True,
                "constant": const,
                "insertion_order": order,
                "priority": 80,
            }
        )
        order += 10
    return entries


def _q(value: str) -> str:
    """JSON 字符串就是合法的 YAML 双引号标量。"""
    return json.dumps(str(value), ensure_ascii=False)


def render_yaml(data: dict[str, Any]) -> str:
    lines: list[str] = [
        "# 由 python -m app.persona_md 从 Markdown 生成，不要手改本文件。",
        "# 改人设请编辑对应的 .md，再重新生成。",
        "",
        "spec: aigirl_card_v1",
    ]
    for key in SHORT_FIELDS:
        value = str(data.get(key) or "").strip()
        if value:
            lines.append(f"{key}: {_q(value)}")

    for key in TEXT_FIELDS:
        value = str(data.get(key) or "").strip()
        if not value:
            continue
        lines.append(f"{key}: |")
        lines.extend(f"  {line}" if line else "" for line in value.splitlines())

    greetings = data.get("alternate_greetings") or []
    if greetings:
        lines.append("alternate_greetings:")
        lines.extend(f"  - {_q(g)}" for g in greetings)

    book = data.get("character_book") or {}
    entries = book.get("entries") or []
    if entries:
        lines.append("character_book:")
        lines.append(f"  scan_depth: {int(book.get('scan_depth', 4))}")
        lines.append(f"  token_budget: {int(book.get('token_budget', 400))}")
        lines.append("  entries:")
        for entry in entries:
            keys = ", ".join(_q(k) for k in entry["keys"])
            lines.append(f"    - keys: [{keys}]")
            lines.append(f"      content: {_q(entry['content'])}")
            lines.append(f"      enabled: {str(bool(entry.get('enabled', True))).lower()}")
            lines.append(f"      constant: {str(bool(entry.get('constant', False))).lower()}")
            lines.append(f"      insertion_order: {int(entry.get('insertion_order', 100))}")
            lines.append(f"      priority: {int(entry.get('priority', 80))}")
    return "\n".join(lines) + "\n"


def _is_placeholder(value: Any) -> bool:
    text = str(value or "").strip()
    return not text or "___" in text or text.upper().startswith("TODO")


def convert(md_text: str) -> tuple[dict[str, Any], list[str]]:
    """返回（卡片数据，缺什么字段）。___ 和 TODO 视为没填。"""
    data = parse_markdown(md_text)
    data.setdefault("spec", "aigirl_card_v1")
    data.setdefault("stage", "熟悉")
    from .persona import REQUIRED_FIELDS

    missing = [k for k in REQUIRED_FIELDS if _is_placeholder(data.get(k))]
    return data, missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Markdown 人设稿 → persona.yaml")
    parser.add_argument("source", help="Markdown 文件")
    parser.add_argument("-o", "--output", help="输出 YAML（默认同名 .yaml）")
    parser.add_argument("--check", action="store_true", help="只校验，不写文件")
    args = parser.parse_args(argv)

    src = Path(args.source)
    if not src.exists():
        print(f"找不到文件：{src}")
        return 2

    text = src.read_text(encoding="utf-8")
    data, missing = convert(text)

    unknown = [
        line[3:].strip()
        for line in text.splitlines()
        if line.startswith("## ") and line[3:].strip().lower() not in KNOWN_SECTIONS
    ]
    if unknown:
        print("这些小节不会被识别（检查拼写）：" + "、".join(unknown))

    if missing:
        print("缺少这些字段，无法生成：")
        for key in missing:
            print(f"  - {key}")
        print("\n人设内容必须由你写，工具不会替你编。模板见 docs/persona-template.md")
        return 1

    out = Path(args.output) if args.output else src.with_suffix(".yaml")
    rendered = render_yaml(data)
    if args.check:
        print(rendered)
        return 0

    out.write_text(rendered, encoding="utf-8")
    print(f"已生成 {out}（{len(rendered.splitlines())} 行）")
    print("改完记得重启服务，或等下一轮自动热重载。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
