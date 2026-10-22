"""联网搜索：触发判断、渲染、缓存、限额。"""

from datetime import datetime

from app.db import Database
from app.search import MAX_RESULTS, SearchClient, needs_search, render

NOW = datetime(2026, 10, 20, 18, 0)


def _payload():
    return {
        "results": [
            {"title": "曼联主场 0-2 落败", "summary": "曼联在主场以 0:2 不敌对手，联赛排名下滑。", "url": "https://x"},
            {"title": "英超积分榜更新", "summary": "本轮过后积分榜变化：" + "很长" * 200, "url": "https://y"},
        ]
    }


class FakeFetch:
    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    async def __call__(self, url, headers, payload):
        self.calls += 1
        if self.fail:
            raise RuntimeError("网络挂了")
        return _payload()


def _client(db, fetch, **kwargs) -> SearchClient:
    return SearchClient(db, "https://api.deepseek.com", "sk-test", "Asia/Shanghai", fetcher=fetch, **kwargs)


# ------------------------------------------------------------------ 触发判断

def test_needs_search_positive():
    assert needs_search("曼联最近比赛怎么样")
    assert needs_search("曼联最近踢得怎么样")
    assert needs_search("今天有什么新闻吗")
    assert needs_search("帮我查一下明天的天气")


def test_needs_search_negative():
    assert not needs_search("今天好累")
    assert not needs_search("嗯")
    assert not needs_search("我想你了")


# ------------------------------------------------------------------ 渲染

def test_render_wraps_and_truncates():
    text = render(_payload()["results"])
    assert "刷手机看到" in text
    assert "曼联主场 0-2 落败" in text
    assert len(text) < 800  # 长摘要被截断
    assert text.count("\n- ") <= MAX_RESULTS


def test_render_empty():
    assert render([]) == ""


# ------------------------------------------------------------------ 服务

async def test_search_caches(tmp_path):
    db = Database(tmp_path / "s.db")
    fetch = FakeFetch()
    client = _client(db, fetch)

    first = await client.search("曼联比赛", NOW)
    second = await client.search("曼联比赛", NOW)

    assert first and first == second
    assert fetch.calls == 1
    db.close()


async def test_search_daily_cap(tmp_path):
    db = Database(tmp_path / "s.db")
    fetch = FakeFetch()
    client = _client(db, fetch, daily_cap=1)

    assert await client.search("曼联比赛", NOW)
    assert await client.search("别的新闻", NOW) == []
    assert fetch.calls == 1
    db.close()


async def test_search_failure_degrades(tmp_path):
    db = Database(tmp_path / "s.db")
    client = _client(db, FakeFetch(fail=True))
    assert await client.search("曼联比赛", NOW) == []
    db.close()


async def test_disabled_never_fetches(tmp_path):
    db = Database(tmp_path / "s.db")
    fetch = FakeFetch()
    client = _client(db, fetch, enabled=False)
    assert await client.for_message("今天有什么新闻吗", NOW) == ""
    assert fetch.calls == 0
    db.close()


async def test_for_message_only_when_needed(tmp_path):
    db = Database(tmp_path / "s.db")
    fetch = FakeFetch()
    client = _client(db, fetch)

    assert await client.for_message("今天好累", NOW) == ""
    assert fetch.calls == 0

    text = await client.for_message("曼联最近比赛怎么样", NOW)
    assert "曼联主场" in text
    assert fetch.calls == 1
    db.close()
