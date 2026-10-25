"""只读体检：看真实库里最近发生了什么（不写任何东西）。

用法：
    .\\.venv\\Scripts\\python.exe experiments/audit_db.py
"""

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "aigirl.db"


def main() -> int:
    if not DB.exists():
        print(f"没有数据库：{DB}")
        return 1

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    print("=== 表规模 ===")
    for table in (
        "messages", "memories", "episodes", "summaries", "rollups",
        "experiences", "proactive_log", "heartbeat_log",
    ):
        try:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table:16} {count}")
        except sqlite3.Error as exc:
            print(f"  {table:16} ERR {exc}")

    print("\n=== 最近对话 ===")
    rows = conn.execute(
        "SELECT created_at, role, substr(content, 1, 30) AS c FROM messages ORDER BY id DESC LIMIT 6"
    ).fetchall()
    for row in reversed(rows):
        who = "他" if row["role"] == "user" else "她"
        print(f"  {row['created_at'][:16]} {who}：{row['c']}")

    print("\n=== 记忆（最近 8 条）===")
    rows = conn.execute(
        "SELECT id, kind, importance, status, substr(content,1,34) AS c FROM memories ORDER BY id DESC LIMIT 8"
    ).fetchall()
    for row in rows:
        flag = "" if row["status"] == "active" else f"[{row['status']}]"
        print(f"  [{row['id']}] {row['kind']}/{row['importance']} {flag}{row['c']}")

    print("\n=== 经历 ===")
    rows = conn.execute(
        "SELECT id, status, occurred_at, share_after, substr(content,1,28) AS c"
        " FROM experiences ORDER BY id DESC LIMIT 6"
    ).fetchall()
    for row in rows:
        print(f"  [{row['id']}] {row['status']:8} 发生于 {row['occurred_at'][:16]}：{row['c']}")

    print("\n=== 心跳（最近 6 次）===")
    rows = conn.execute(
        "SELECT at, action, reason, motivation, gate FROM heartbeat_log ORDER BY id DESC LIMIT 6"
    ).fetchall()
    for row in rows:
        print(f"  {row['at'][:16]} {row['action']:8} {row['reason'] or ''} ({row['gate']}, {row['motivation']})")

    print("\n=== 主动消息 ===")
    rows = conn.execute(
        "SELECT sent_at, kind, slot FROM proactive_log ORDER BY id DESC LIMIT 5"
    ).fetchall()
    for row in rows:
        print(f"  {row['sent_at'][:16]} {row['kind']} {row['slot']}")

    print("\n=== state ===")
    for row in conn.execute("SELECT key, value FROM state ORDER BY key"):
        print(f"  {row['key']} = {str(row['value'])[:60]}")

    today = datetime.now().strftime("%Y-%m-%d")
    sent_today = conn.execute(
        "SELECT COUNT(*) FROM proactive_log WHERE sent_at LIKE ?", (f"{today}%",)
    ).fetchone()[0]
    print(f"\n今天（{today}）主动消息：{sent_today} 条")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
