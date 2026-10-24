"""配置与常量。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---- 生成参数 ----
TEMPERATURE = 0.9
MAX_TOKENS = 300
LLM_TIMEOUT_S = 60.0
LLM_RETRIES = 2
LLM_RETRY_BACKOFF_S = 0.8

# ---- 遗忘 ----
FORGET_DAYS = 90
FORGET_MAX_IMPORTANCE = 2

# ---- 上下文预算 ----
RECENT_MESSAGES = 30
# 条数和字数谁先到就按谁裁：30 条短句约 900 字，但对方一次发 200 字长文时，
# 30 条能到 6000 字，光靠条数兜不住。
RECENT_CHARS = 4000
RECENT_MIN_MESSAGES = 8
LOREBLOOK_MAX_ENTRIES = 3

# ---- 气泡 ----
MAX_BUBBLES = 3
BUBBLE_SPLIT_AT = 45
BUBBLE_DELAY_MIN_MS = 40
BUBBLE_DELAY_MAX_MS = 80
BUBBLE_PAD_MIN_MS = 200
BUBBLE_PAD_MAX_MS = 600
BUBBLE_DELAY_CAP_MS = 6000

# ---- 记忆（M2 用）----
MEMORY_MAX_CHARS = 60
RECALL_TOP_K = 8
RECENCY_HALF_LIFE_DAYS = 14
EXTRACT_EVERY_N_USER_MSGS = 6

# ---- 滚动归纳（M6）----
ROLLUP_EVERY_N_USER_MSGS = 10
ROLLUP_MAX_CHARS = 300
ROLLUP_MAX_MESSAGES = 60
ROLLUP_KEEP = 3

# ---- 心跳（M3 用）----
HEARTBEAT_INTERVAL_S = 90
DAILY_PROACTIVE_CAP = 4
MIN_PROACTIVE_GAP_MIN = 90
UNANSWERED_STOP = 2
MOTIVATION_THRESHOLD = 0.55


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    tz: str
    db_path: Path
    persona_path: Path
    life_path: Path
    profile_path: Path
    seed_path: Path
    world_path: Path = ROOT / "config" / "world.yaml"
    search_enabled: bool = True
    prompts_path: Path = ROOT / "config" / "prompts.yaml"

    @property
    def mock_mode(self) -> bool:
        return not self.llm_api_key.strip()


def config_file(name: str) -> Path:
    """config/<name>.local.yaml 存在就用它，否则用仓库里的示例。

    个人的她/他的配置都放在 *.local.yaml，不进版本库；
    仓库里只有示例，clone 下来就能跑（Mock 模式）。
    """
    local = ROOT / "config" / f"{name}.local.yaml"
    return local if local.exists() else ROOT / "config" / f"{name}.yaml"


def load_settings(env_file: Path | None = None) -> Settings:
    _load_dotenv(env_file or ROOT / ".env")
    return Settings(
        llm_base_url=os.environ.get("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/"),
        llm_api_key=os.environ.get("LLM_API_KEY", ""),
        llm_model=os.environ.get("LLM_MODEL", "deepseek-flash"),
        tz=os.environ.get("TZ", "Asia/Shanghai"),
        db_path=Path(os.environ.get("DB_PATH", str(ROOT / "data" / "aigirl.db"))),
        persona_path=config_file("persona"),
        life_path=config_file("life"),
        profile_path=config_file("user_profile"),
        seed_path=config_file("seed_memories"),
        world_path=config_file("world"),
        prompts_path=config_file("prompts"),
        search_enabled=os.environ.get("SEARCH_ENABLED", "1").strip().lower()
        not in ("0", "false", "no", "off"),
    )
