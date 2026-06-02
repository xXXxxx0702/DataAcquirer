"""Persisted server bookmarks (saved InfluxDB connections).

Lets the user keep several named connections (e.g. one per plant/site) and
switch between them quickly. Stored as a JSON list at ``config/bookmarks.json``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path


@dataclass
class ServerBookmark:
    name: str
    host: str
    port: int = 8086
    username: str = ""
    password: str = ""
    database: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "ServerBookmark":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


class BookmarkStore:
    """A name-keyed collection of :class:`ServerBookmark`, backed by a JSON file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._items: dict[str, ServerBookmark] = {}
        self.load()

    # ------------------------------------------------------------------ #
    def load(self) -> None:
        self._items = {}
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for entry in data:
            try:
                bm = ServerBookmark.from_dict(entry)
            except TypeError:
                continue
            if bm.name:
                self._items[bm.name] = bm

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = [asdict(bm) for bm in self._items.values()]
        self.path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # ------------------------------------------------------------------ #
    def names(self) -> list[str]:
        return list(self._items.keys())

    def get(self, name: str) -> ServerBookmark | None:
        return self._items.get(name)

    def upsert(self, bookmark: ServerBookmark) -> None:
        self._items[bookmark.name] = bookmark
        self.save()

    def remove(self, name: str) -> None:
        if name in self._items:
            del self._items[name]
            self.save()
