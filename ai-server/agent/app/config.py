"""Settings, read once from the environment.

Every value has a safe default and a hard range, so a typo in `.env` degrades to
the default (with a warning) instead of stopping the service.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import tzinfo, timezone
from functools import lru_cache
from typing import Mapping
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _text(env: Mapping[str, str], name: str, default: str) -> str:
    return (env.get(name) or "").strip() or default


def _int(env: Mapping[str, str], name: str, default: int, lo: int, hi: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(lo, min(hi, int(raw)))
    except ValueError:
        log.warning("%s=%r is not a whole number; using %d", name, raw, default)
        return default


def _float(env: Mapping[str, str], name: str, default: float, lo: float, hi: float) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(lo, min(hi, float(raw)))
    except ValueError:
        log.warning("%s=%r is not a number; using %s", name, raw, default)
        return default


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = (env.get(name) or "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    if raw:
        log.warning("%s=%r is not on/off; using %s", name, raw, "on" if default else "off")
    return default


def _ids(env: Mapping[str, str], name: str) -> frozenset[int]:
    out = set()
    for part in re.split(r"[,\s;]+", env.get(name) or ""):
        if part.lstrip("-").isdigit():
            out.add(int(part))
        elif part:
            log.warning("%s: ignoring %r (numeric Telegram user IDs only)", name, part)
    return frozenset(out)


@lru_cache(maxsize=8)
def _timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except Exception:  # unknown name or missing tz database
        log.warning("TZ=%r is not a known time zone; using UTC", name)
        return timezone.utc


@dataclass(frozen=True)
class Settings:
    llm_base_url: str = "http://llm:8080/v1"
    llm_api_key: str = ""
    llm_model: str = "assistant"
    agent_api_key: str = ""
    dashboard_password: str = ""
    searxng_url: str = "http://searxng:8080"
    sandbox_url: str = "http://sandbox:8070"
    sandbox_token: str = ""
    data_dir: str = "/data"
    workspace_dir: str = "/workspace"
    telegram_api_base: str = "https://api.telegram.org"
    telegram_bot_token: str = ""
    telegram_allowed_ids: frozenset[int] = frozenset()
    pairing_code: str = ""
    tz_name: str = "UTC"

    thinking: str = "auto"              # off | on | auto
    max_steps: int = 12                 # LLM calls per turn
    ctx_tokens: int = 14000             # prompt budget; keep below LLM_CTX / LLM_PARALLEL
    max_output_tokens: int = 1500       # max_tokens for agent calls
    think_tokens: int = 2048            # extra output allowance when thinking is on
    chat_max_tokens: int = 4096         # default max_tokens for pass-through chat
    tool_result_chars: int = 6000
    turn_timeout: float = 7200.0        # wall-clock limit for one interactive turn

    cpu_governor: bool = False
    cpu_baseline: float = 0.60
    background_rest_ratio: float = 0.5
    proc_stat_path: str = "/proc/stat"

    min_schedule_interval_min: int = 15
    max_schedules: int = 50
    enable_run_code: bool = True

    llm_connect_timeout: float = 10.0
    llm_read_timeout: float = 900.0
    llm_retry_seconds: float = 300.0    # how long to wait for a model that is still loading

    @property
    def tz(self) -> tzinfo:
        return _timezone(self.tz_name)

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "agent.db")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        e = os.environ if env is None else env
        d = cls()
        thinking = _text(e, "AGENT_THINKING", d.thinking).lower()
        if thinking not in ("off", "on", "auto"):
            log.warning("AGENT_THINKING=%r is not off/on/auto; using auto", thinking)
            thinking = "auto"
        return cls(
            llm_base_url=_text(e, "LLM_BASE_URL", d.llm_base_url).rstrip("/"),
            llm_api_key=_text(e, "LLM_API_KEY", ""),
            llm_model=_text(e, "LLM_MODEL", d.llm_model),
            agent_api_key=_text(e, "AGENT_API_KEY", ""),
            dashboard_password=(e.get("DASHBOARD_PASSWORD") or ""),
            searxng_url=_text(e, "SEARXNG_URL", d.searxng_url).rstrip("/"),
            sandbox_url=_text(e, "SANDBOX_URL", d.sandbox_url).rstrip("/"),
            sandbox_token=_text(e, "SANDBOX_TOKEN", ""),
            data_dir=_text(e, "AGENT_DATA_DIR", d.data_dir),
            workspace_dir=_text(e, "WORKSPACE_DIR", d.workspace_dir),
            telegram_api_base=_text(e, "TELEGRAM_API_BASE", d.telegram_api_base).rstrip("/"),
            telegram_bot_token=_text(e, "TELEGRAM_BOT_TOKEN", ""),
            telegram_allowed_ids=_ids(e, "TELEGRAM_ALLOWED_IDS"),
            pairing_code=_text(e, "PAIRING_CODE", ""),
            tz_name=_text(e, "TZ", d.tz_name),
            thinking=thinking,
            max_steps=_int(e, "AGENT_MAX_STEPS", d.max_steps, 1, 50),
            ctx_tokens=_int(e, "AGENT_CTX_TOKENS", d.ctx_tokens, 2000, 200_000),
            max_output_tokens=_int(e, "AGENT_MAX_OUTPUT_TOKENS", d.max_output_tokens, 64, 16_000),
            # LLM_THINK_BUDGET is the server-side budget; honour it when compose passes it.
            think_tokens=_int(e, "AGENT_THINK_TOKENS",
                              _int(e, "LLM_THINK_BUDGET", d.think_tokens, 0, 16_000), 0, 16_000),
            chat_max_tokens=_int(e, "AGENT_CHAT_MAX_TOKENS", d.chat_max_tokens, 64, 32_000),
            tool_result_chars=_int(e, "AGENT_TOOL_RESULT_CHARS", d.tool_result_chars, 500, 20_000),
            turn_timeout=_float(e, "AGENT_TURN_TIMEOUT", d.turn_timeout, 60, 86_400),
            cpu_governor=_bool(e, "CPU_GOVERNOR", d.cpu_governor),
            cpu_baseline=_float(e, "CPU_BASELINE", d.cpu_baseline, 0.05, 1.0),
            background_rest_ratio=_float(e, "BACKGROUND_REST_RATIO", d.background_rest_ratio, 0.0, 10.0),
            proc_stat_path=_text(e, "PROC_STAT_PATH", d.proc_stat_path),
            min_schedule_interval_min=_int(e, "MIN_SCHEDULE_INTERVAL_MIN", d.min_schedule_interval_min, 1, 10_080),
            max_schedules=_int(e, "MAX_SCHEDULES", d.max_schedules, 0, 1000),
            enable_run_code=_bool(e, "ENABLE_RUN_CODE", d.enable_run_code),
            llm_connect_timeout=_float(e, "LLM_CONNECT_TIMEOUT", d.llm_connect_timeout, 1, 120),
            llm_read_timeout=_float(e, "LLM_READ_TIMEOUT", d.llm_read_timeout, 900, 7200),
            llm_retry_seconds=_float(e, "LLM_RETRY_SECONDS", d.llm_retry_seconds, 0, 3600),
        )

    def secrets(self) -> list[str]:
        """Values that must never appear in logs."""
        return [s for s in (self.telegram_bot_token, self.llm_api_key, self.agent_api_key,
                            self.sandbox_token, self.dashboard_password) if len(s) >= 6]
