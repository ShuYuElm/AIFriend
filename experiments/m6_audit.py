"""M6 验收实测：A1–A8 跑一遍真模型，输出报告。

用法：
    python experiments/m6_audit.py

会用一个独立的库（data/m6audit.db），不碰你的真实聊天记录。
跑一次约 30 次模型调用（¥0.3 左右），3 分钟以内。
"""

from __future__ import annotations

import asyncio
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app.heartbeat as hbmod  # noqa: E402
from app.chat import ChatEngine  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.heartbeat import Heartbeat  # noqa: E402
from app.life import load_life  # noqa: E402
from app.llm import build_provider  # noqa: E402
from app.lorebook import Lorebook  # noqa: E402
from app.persona import PersonaStore  # noqa: E402
from app.profile import ProfileStore  # noqa: E402

DB_PATH = ROOT / "data" / "m6audit.db"
TZ = timezone(timedelta(hours=8))


def dt(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    """带时区的时间，避免和库里的 aware 时间戳相减报错。"""
    return datetime(year, month, day, hour, minute, tzinfo=TZ)

# 为了在几分钟里模拟一天，把闸门放松（只影响本脚本）
hbmod.MOTIVATION_THRESHOLD = 0.25
hbmod.MIN_PROACTIVE_GAP_MIN = 15
hbmod.RECENT_CHAT_MIN = 3


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def set(self, value: datetime) -> None:
        self.value = value


RESULTS: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, "PASS" if ok else "FAIL", detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} — {detail}")


def _norm(text: str) -> str:
    return re.sub(r"[^\u4e00-\u9fff]", "", text)


def repetition(replies: list[str]) -> tuple[float, float, float]:
    """返回（完全重复率，最高二元组重合度，问句结尾比例）。"""
    norms = [_norm(r) for r in replies if _norm(r)]
    if not norms:
        return 0.0, 0.0, 0.0
    unique = len(set(norms))
    dup_rate = 1 - unique / len(norms)

    def grams(s: str) -> set[str]:
        return {s[i : i + 2] for i in range(len(s) - 1)}

    worst = 0.0
    for i in range(len(norms)):
        for j in range(i + 1, len(norms)):
            a, b = grams(norms[i]), grams(norms[j])
            if a and b:
                worst = max(worst, len(a & b) / min(len(a), len(b)))
    question_rate = sum(1 for r in replies if r.strip().endswith(("？", "?"))) / len(replies)
    return dup_rate, worst, question_rate


async def main() -> None:
    if DB_PATH.exists():
        for suffix in ("", "-wal", "-shm"):
            Path(str(DB_PATH) + suffix).unlink(missing_ok=True)

    settings = load_settings()
    db = Database(DB_PATH)
    provider = build_provider(settings)
    persona_store = PersonaStore(settings.persona_path)
    profile_store = ProfileStore(settings.profile_path)
    persona = persona_store.load()
    life = load_life(settings.life_path)
    clock = Clock(dt(2026, 10, 21, 10, 0))

    engine = ChatEngine(
        settings=settings,
        provider=provider,
        db=db,
        persona_store=persona_store,
        profile_store=profile_store,
        lorebook=Lorebook.from_book(persona.book),
        life=life,
        clock=clock,
    )
    await engine.startup()

    heartbeat = Heartbeat(
        settings=settings,
        db=db,
        provider=provider,
        persona_store=persona_store,
        profile_store=profile_store,
        life=life,
        memory=engine.memory,
        experiences=engine.experiences,
        world=engine.world,
        clock=clock,
    )

    print("=" * 62)
    print("A1 · 长期记忆（第 1 天说的事，第 3 天还记不记得）")
    print("=" * 62)

    day1 = [
        "导师催我周五交初稿，我得赶一下",
        "我生日七月七号，你要记着",
        "我妈下周三来成都看我",
        "我最近开始健身了，一周三次",
        "今天实验室跑到十点",
        "对了，我换了个新手机",
    ]
    clock.set(dt(2026, 10, 21, 10, 0))
    for i, text in enumerate(day1):
        clock.set(dt(2026, 10, 21, 10, 0) + timedelta(minutes=i * 3))
        bubbles = await engine.handle(text)
        print(f"  你 > {text}")
        print(f"  她 > {' / '.join(b.text for b in bubbles)}")
    await engine.drain()

    memories = await db.list_memories(20)
    print(f"\n  抽取到 {len(memories)} 条记忆（含 3 条种子）：")
    for m in memories[:8]:
        print(f"    - [{m.kind}] {m.content}")

    # 塞 30 条填充消息，把第 1 天挤出 30 条窗口
    clock.set(dt(2026, 10, 22, 21, 0))
    for i in range(15):
        stamp = (dt(2026, 10, 22, 21, 0) + timedelta(minutes=i)).isoformat()
        await db.insert_message("user", f"填充{i}", "cli", stamp)
        await db.insert_message("assistant", f"嗯{i}", "cli", stamp)
    print(f"\n  已把第 1 天挤出窗口（当前 {await db.count_messages()} 条消息）")

    clock.set(dt(2026, 10, 23, 10, 0))
    probes = [
        ("我这周五要交什么来着", ["初稿", "论文"]),
        ("我生日几号", ["七", "7"]),
        ("我妈什么时候来", ["下周三", "周三"]),
    ]
    hits = 0
    for text, expects in probes:
        clock.set(clock.value + timedelta(minutes=2))
        bubbles = await engine.handle(text)
        reply = "".join(b.text for b in bubbles)
        ok = any(e in reply for e in expects)
        hits += int(ok)
        print(f"  你 > {text}")
        print(f"  她 > {reply}   {'✓' if ok else '✗'}")
    check("A1 长期记忆", hits >= 2, f"3 个跨天问题答对 {hits} 个")

    print()
    print("=" * 62)
    print("A2 / A7 · 主动频率 与 她会选择不说")
    print("=" * 62)

    sent = 0
    skipped_self = 0
    for hour in (8, 11, 14, 18, 20, 22):
        clock.set(dt(2026, 10, 24, hour, 10))
        result = await heartbeat.tick()
        action = result.get("action")
        gate = result.get("gate", "")
        if action == "sent":
            sent += 1
            print(f"  {hour:02d}:10 发出：{' / '.join(result.get('bubbles', []))}")
        else:
            if gate == "self":
                skipped_self += 1
            print(f"  {hour:02d}:10 跳过（{gate}）：{result.get('reason', '')}")

    check("A2 主动频率", 1 <= sent <= 4, f"模拟一天发出 {sent} 条（要求 1–4）")

    # A7 要单独测：上面几次是被"连发未回"闸门挡住的，不算她自己选择不说。
    # 换一天（上限从 0 开始），把未回计数清零，给她几个"没什么正当理由"的时刻。
    self_skips = 0
    tries = 0
    for hour, minute in ((12, 40), (15, 40), (18, 40), (21, 40)):
        clock.set(dt(2026, 10, 26, hour, minute))
        await db.set_state("proactive_unanswered", 0)
        result = await heartbeat.tick()
        tries += 1
        if result.get("action") == "skipped" and result.get("gate") == "self":
            self_skips += 1
            print(f"  10-26 {hour:02d}:{minute:02d} 她选择不说：{result.get('reason')}")
        elif result.get("action") == "sent":
            print(f"  10-26 {hour:02d}:{minute:02d} 她说：{' / '.join(result.get('bubbles', []))}")
        else:
            print(f"  10-26 {hour:02d}:{minute:02d} 被闸门拦下（{result.get('gate')}）")

    check("A7 她会不说", self_skips >= 1, f"{tries} 次机会里她主动选择不发 {self_skips} 次")

    print()
    print("=" * 62)
    print("A3 / A4 · 连续 12 轮的重复率与气泡节奏")
    print("=" * 62)

    clock.set(dt(2026, 10, 25, 15, 0))
    topics = [
        "今天好累啊", "食堂的菜越来越难吃了", "你晚上一般干嘛", "我想换个键盘",
        "最近有什么好看的剧", "我室友天天打游戏", "周末想去爬山", "你觉得我该剪头发吗",
        "今天地铁好挤", "我有点想吃火锅", "你平时几点睡", "在吗",
    ]
    replies: list[str] = []
    bubble_counts: list[int] = []
    for i, text in enumerate(topics):
        clock.set(dt(2026, 10, 25, 15, 0) + timedelta(minutes=i * 2))
        bubbles = await engine.handle(text)
        replies.append("".join(b.text for b in bubbles))
        bubble_counts.append(len(bubbles))
        print(f"  你 > {text}")
        print(f"  她 > {' / '.join(b.text for b in bubbles)}")

    await engine.drain()
    dup, overlap, question_rate = repetition(replies)
    check("A3 不重复", dup == 0 and overlap < 0.6, f"完全重复 {dup:.0%}，最高重合 {overlap:.0%}，问句结尾 {question_rate:.0%}")
    ok_bubbles = all(1 <= n <= 3 for n in bubble_counts)
    check("A4 气泡节奏", ok_bubbles, f"每条回复 {min(bubble_counts)}–{max(bubble_counts)} 个气泡")

    print()
    print("=" * 62)
    print("A5 · 冷落两天后说重话")
    print("=" * 62)

    clock.set(dt(2026, 10, 28, 21, 0))
    bubbles = await engine.handle("最近挺烦的，你别老找我")
    harsh = "".join(b.text for b in bubbles)
    print(f"  你 > 最近挺烦的，你别老找我")
    print(f"  她 > {harsh}")

    clock.set(dt(2026, 10, 28, 21, 5))
    bubbles = await engine.handle("对不起，我语气不太好")
    soft = "".join(b.text for b in bubbles)
    print(f"  你 > 对不起，我语气不太好")
    print(f"  她 > {soft}")

    reset_markers = ("我们认识吗", "你是谁", "初次见面", "作为一个AI", "作为AI")
    coherent = not any(m in (harsh + soft) for m in reset_markers)
    check("A5 抗崩", coherent, "被说重话后没有重置人格")

    print()
    print("=" * 62)
    print("A8 · 延迟分享")
    print("=" * 62)

    today = clock.value.date().isoformat()
    # 当天可能抽到"0 件事"，重试几次直到有
    for _ in range(8):
        await engine.experiences.ensure_today(clock.value)
        if await db.experiences_for_day(today):
            break
    rows = await db.experiences_for_day(today)
    # 分享有 30% 的随机概率，测"机制"时先固定住，否则四次要约里可能一次都不触发
    engine.experiences.pick_for_reply = lambda due, rng: due[0] if due else None
    due = [r for r in rows if str(r["share_after"]) <= clock.value.isoformat()]
    if due:
        clock.set(clock.value + timedelta(minutes=3))
        bubbles = await engine.handle("今天过得怎么样")
        reply = "".join(b.text for b in bubbles)
        print(f"  今天她经历的事：{[r['content'][:24] for r in rows]}")
        print(f"  你 > 今天过得怎么样")
        print(f"  她 > {reply}")
        from app.experiences import mentions as _mentions

        # 设计上她只有 30% 的概率接这次分享，所以问几次再判
        hit = False
        for attempt in range(4):
            if attempt:
                clock.set(clock.value + timedelta(minutes=2))
                await db.set_state("proactive_unanswered", 0)
                bubbles = await engine.handle("今天过得怎么样")
                reply = "".join(b.text for b in bubbles)
                print(f"  你 > 今天过得怎么样（第 {attempt + 1} 次）")
                print(f"  她 > {reply}")
            if any(_mentions(reply, r["content"]) for r in due):
                hit = True
                break
        check("A8 延迟分享", hit, "问到今天时她 naturally 提到了当天的事")
    else:
        check("A8 延迟分享", False, f"今天 {len(rows)} 条经历，暂无可分享的（时间不到）")

    print()
    print("=" * 62)
    print("汇总")
    print("=" * 62)
    for name, status, detail in RESULTS:
        print(f"  {status:<4} {name:<12} {detail}")

    passed = sum(1 for _, s, _ in RESULTS if s == "PASS")
    print(f"\n  {passed}/{len(RESULTS)} 通过")

    await provider.aclose()
    db.close()


if __name__ == "__main__":
    asyncio.run(main())
