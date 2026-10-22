"""lorebook 触发与预算。"""

from app.lorebook import Entry, Lorebook, estimate_tokens


def _book():
    return Lorebook.from_book(
        {
            "token_budget": 400,
            "entries": [
                {
                    "keys": ["林夏"],
                    "content": "她叫林夏，20 岁。",
                    "constant": True,
                    "insertion_order": 100,
                    "priority": 80,
                },
                {"keys": ["猫", "年糕"], "content": "她养的橘猫叫年糕。", "priority": 90},
                {"keys": ["打印机"], "content": "她在书店兼职。", "priority": 50},
            ],
        }
    )


def test_constant_always_present():
    hits = _book().match("今天天气不错")
    assert [e.content for e in hits] == ["她叫林夏，20 岁。"]


def test_keyword_hit():
    hits = _book().match("我家猫又胖了")
    assert any("年糕" in e.content for e in hits)


def test_priority_ordering():
    hits = _book().match("猫和打印机")
    contents = [e.content for e in hits]
    assert contents.index("她养的橘猫叫年糕。") < contents.index("她在书店兼职。")


def test_disabled_entry_ignored():
    book = _book()
    book.entries[1].enabled = False
    hits = book.match("猫")
    assert all("年糕" not in e.content for e in hits)


def test_budget_keeps_at_least_one():
    book = Lorebook([Entry(content="很长的设定" * 20, constant=True)], token_budget=1)
    assert len(book.match("")) == 1


def test_format_empty():
    assert Lorebook.format([]) == ""


def test_estimate_tokens_chinese():
    assert estimate_tokens("你好吗") >= 2
