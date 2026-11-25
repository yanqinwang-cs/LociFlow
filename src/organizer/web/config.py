from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from organizer.web.root_admission import admit_root

_MINIMUM_SECRET_LENGTH = 32
_LAUNCH_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,}")


def _session_secret() -> str:
    return secrets.token_urlsafe(48)


def _launch_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class WebAppConfig:
    root: Path
    session_secret: str | None = None
    launch_token: str | None = None
    testing: bool = False

    def __post_init__(self) -> None:
        admitted_root = admit_root(self.root)
        object.__setattr__(self, "root", admitted_root.path)
        if self.session_secret is None:
            object.__setattr__(self, "session_secret", _session_secret())
        if self.launch_token is None:
            object.__setattr__(self, "launch_token", _launch_token())

        if (
            not isinstance(self.session_secret, str)
            or len(self.session_secret) < _MINIMUM_SECRET_LENGTH
        ):
            raise ValueError("session secret must contain at least 32 characters")
        if (
            not isinstance(self.launch_token, str)
            or _LAUNCH_TOKEN_PATTERN.fullmatch(self.launch_token) is None
        ):
            raise ValueError(
                "launch token must contain at least 32 URL-safe characters"
            )

    @property
    def allowed_hosts(self) -> tuple[str, ...]:
        if self.testing:
            return ("127.0.0.1", "localhost", "testserver")
        return ("127.0.0.1", "localhost")
