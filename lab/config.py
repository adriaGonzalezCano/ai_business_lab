"""Runtime configuration (from environment). Business rules live in the DB, not here."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("LAB_DATA_DIR", "data")))
    db_path: Path | None = None
    owner_token: str = ""
    host: str = field(default_factory=lambda: os.environ.get("LAB_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.environ.get("LAB_PORT", "8000")))
    anthropic_api_key: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", ""))
    anthropic_base_url: str = field(
        default_factory=lambda: os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    )
    user_agent: str = field(
        default_factory=lambda: os.environ.get(
            "LAB_USER_AGENT", "AIBusinessLabResearchBot/0.1 (+owner-operated; respects robots.txt)"
        )
    )
    run_worker: bool = field(default_factory=lambda: os.environ.get("LAB_RUN_WORKER", "1") == "1")

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        if self.db_path is None:
            self.db_path = Path(os.environ.get("LAB_DB_PATH", str(self.data_dir / "lab.sqlite3")))
        self.db_path = Path(self.db_path)
        if not self.owner_token:
            self.owner_token = os.environ.get("LAB_OWNER_TOKEN", "")

    def ensure_owner_token(self) -> str:
        """Use LAB_OWNER_TOKEN if set; otherwise create one once and keep it in the data dir (0600)."""
        if self.owner_token:
            return self.owner_token
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.data_dir / "owner_token"
        if path.exists():
            self.owner_token = path.read_text().strip()
        else:
            self.owner_token = secrets.token_urlsafe(32)
            path.write_text(self.owner_token)
            try:
                path.chmod(0o600)
            except OSError:  # e.g. Windows
                pass
        return self.owner_token
