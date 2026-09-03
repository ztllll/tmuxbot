"""Environment-backed result-first IM presentation policy."""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Mapping


class PresentationMode(str, Enum):
    RESULT_ONLY = "result_only"
    COMPACT = "compact"
    VERBOSE = "verbose"


@dataclass(frozen=True, slots=True)
class IMPresentationPolicy:
    mode: PresentationMode = PresentationMode.COMPACT
    progress_delay_seconds: float = 4.0
    progress_update_interval_seconds: float = 2.0
    progress_max_steps: int = 3

    @property
    def progress_enabled(self) -> bool:
        return self.mode is not PresentationMode.RESULT_ONLY

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> "IMPresentationPolicy":
        env = os.environ if environ is None else environ
        raw_mode = env.get("TMUXBOT_IM_PRESENTATION", "compact").strip().lower()
        try:
            mode = PresentationMode(raw_mode)
        except ValueError:
            mode = PresentationMode.COMPACT
        delay_default = 0.0 if mode is PresentationMode.VERBOSE else 4.0
        return cls(
            mode=mode,
            progress_delay_seconds=_float_env(
                env, "TMUXBOT_IM_PROGRESS_DELAY", delay_default, minimum=0.0
            ),
            progress_update_interval_seconds=_float_env(
                env, "TMUXBOT_IM_PROGRESS_UPDATE_INTERVAL", 2.0, minimum=0.0
            ),
            progress_max_steps=_int_env(
                env, "TMUXBOT_IM_PROGRESS_MAX_STEPS", 3, minimum=1
            ),
        )


def _float_env(
    env: Mapping[str, str], name: str, default: float, *, minimum: float
) -> float:
    try:
        value = float(env.get(name, str(default)))
    except ValueError:
        return default
    return max(minimum, value)


def _int_env(
    env: Mapping[str, str], name: str, default: int, *, minimum: int
) -> int:
    try:
        value = int(env.get(name, str(default)))
    except ValueError:
        return default
    return max(minimum, value)
