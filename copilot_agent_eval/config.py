"""Configuration loading with ${ENV_VAR} expansion."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _expand_env(value: Any) -> Any:
    """Recursively expand ${VAR} references in strings inside a data structure."""
    if isinstance(value, str):
        def _repl(match: re.Match) -> str:
            return os.environ.get(match.group(1), "")
        return _ENV_PATTERN.sub(_repl, value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


@dataclass
class DirectLineConfig:
    secret: str
    endpoint: str = "https://directline.botframework.com/v3/directline"
    locale: str = "en-US"
    user: dict = field(default_factory=lambda: {"id": "dl_test_user", "name": "Test User"})
    channel_data: dict = field(default_factory=dict)


@dataclass
class AuthConfig:
    enabled: bool = True
    headless: bool = False
    manual: bool = True
    username: str = ""
    password: str = ""
    user_data_dir: str = ".auth-profile"
    code_submit_mode: str = "auto"  # auto | invoke | message
    login_timeout_seconds: int = 300
    # Max number of distinct OAuthCards to sign in to per turn (e.g. bot Entra
    # auth + an MCP connection auth = 2). Safety cap against redirect loops.
    max_signin_rounds: int = 5
    # Connection-manager (MCP/connector consent) behaviour.
    # False (default) = pure manual: open the page and never click any button;
    # the human clicks Connect / Submit themselves and closes the browser
    # window when done. Set true only for unattended runs with a warm SSO
    # profile where auto-clicking Allow/Connect is safe.
    auto_click_consent: bool = False


@dataclass
class RunnerConfig:
    interval_seconds: float = 5.0
    max_wait_seconds: float = 60.0
    quiet_seconds: float = 3.0
    polling_interval_seconds: float = 1.0
    concurrency: int = 1
    session_mode: str = "continuous"  # isolated | continuous
    token_refresh_margin_seconds: int = 120


@dataclass
class AssertionConfig:
    fail_on_error_phrases: list[str] = field(default_factory=list)
    # Default per-case latency SLA. Dataverse aggregation queries commonly
    # take 30-60s; override per case via the CSV max_latency_ms column.
    default_max_latency_ms: int = 120000


@dataclass
class ReportConfig:
    output_dir: str = "reports"
    formats: list[str] = field(default_factory=lambda: ["excel", "html", "junit"])
    save_raw_activities: bool = True


@dataclass
class AppConfig:
    directline: DirectLineConfig
    auth: AuthConfig
    runner: RunnerConfig
    assertions: AssertionConfig
    report: ReportConfig

    @classmethod
    def load(cls, path: str | Path) -> "AppConfig":
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        raw = _expand_env(raw)

        dl = raw.get("directline", {})
        if not dl.get("secret"):
            raise ValueError(
                "directline.secret is empty. Set it in config.yaml or the "
                "DL_SECRET environment variable."
            )
        return cls(
            directline=DirectLineConfig(
                secret=dl["secret"],
                endpoint=dl.get("endpoint", DirectLineConfig.endpoint).rstrip("/"),
                locale=dl.get("locale", "en-US"),
                user=dl.get("user", {"id": "dl_test_user", "name": "Test User"}),
                channel_data=dl.get("channel_data", {}),
            ),
            auth=AuthConfig(**{**AuthConfig().__dict__, **raw.get("auth", {})}),
            runner=RunnerConfig(**{**RunnerConfig().__dict__, **raw.get("runner", {})}),
            assertions=AssertionConfig(
                **{**AssertionConfig().__dict__, **raw.get("assertions", {})}
            ),
            report=ReportConfig(**{**ReportConfig().__dict__, **raw.get("report", {})}),
        )
