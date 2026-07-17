"""Persistent run history used by the desktop toolbar."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class RunRecord:
    started_at: str
    status: str
    duration_seconds: float
    rows: int
    output_path: str
    point_count: int
    config: dict
    message: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "RunRecord":
        return cls(
            started_at=str(data.get("started_at", "")),
            status=str(data.get("status", "unknown")),
            duration_seconds=float(data.get("duration_seconds", 0)),
            rows=int(data.get("rows", 0)),
            output_path=str(data.get("output_path", "")),
            point_count=int(data.get("point_count", 0)),
            config=dict(data.get("config") or {}),
            message=str(data.get("message", "")),
        )


class RunHistoryStore:
    """Small JSON-backed list of the most recent acquisition runs."""

    def __init__(self, path: str | Path, limit: int = 50) -> None:
        self.path = Path(path)
        self.limit = limit
        self._records: list[RunRecord] = []
        self.load()

    @property
    def records(self) -> list[RunRecord]:
        return list(self._records)

    def load(self) -> None:
        self._records = []
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for item in payload if isinstance(payload, list) else []:
            try:
                self._records.append(RunRecord.from_dict(item))
            except (TypeError, ValueError):
                continue
        self._records = self._records[: self.limit]

    def append(self, record: RunRecord) -> None:
        self._records.insert(0, record)
        self._records = self._records[: self.limit]
        self._save()

    def clear(self) -> None:
        self._records = []
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = [asdict(record) for record in self._records]
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)
