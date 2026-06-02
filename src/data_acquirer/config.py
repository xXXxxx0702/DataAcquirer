"""Configuration model for a data-acquisition job.

A :class:`AcquireConfig` captures everything the original script hard-coded:
the InfluxDB connection, the time window, the chunking/offset behaviour, the
list of measure points and the CSV output path. It can be serialised to / from
JSON so users can save and reload presets from the UI.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path


@dataclass
class PointSpec:
    """A single measure point to pull.

    Attributes:
        name: The ``measurePoint`` tag value (the ``k`` in the original script).
        measurement: The InfluxDB measurement / "type" the point lives in,
            e.g. ``Float``, ``Bool``, ``Double`` (the ``v`` in the original).
        note: Optional human-readable description (kept for the operator only).
        enabled: Whether this point is included in the next pull.
    """

    name: str
    measurement: str = "Float"
    note: str = ""
    enabled: bool = True


@dataclass
class AcquireConfig:
    """Full configuration for one acquisition run."""

    # --- Connection ---
    host: str = "192.168.22.9"
    port: int = 30886
    username: str = "admin"
    password: str = "admin1234"
    database: str = "raw_tenant_industry_brain"

    # --- Time window (local time, "YYYY-MM-DD HH:MM:SS") ---
    start_time: str = "2026-05-30 15:00:00"
    end_time: str = "2026-06-01 15:00:00"

    # --- Behaviour ---
    chunk_hours: int = 24          # window size per query, like the original 24h loop
    utc_offset_hours: int = 8      # local-time <-> UTC offset applied before/after query
    value_field: str = "value"     # SELECT "<value_field>"
    measure_tag: str = "measurePoint"  # WHERE ("<measure_tag>" = '<point>')

    # --- Points & output ---
    points: list[PointSpec] = field(default_factory=list)
    output_path: str = "output/data.csv"

    # ------------------------------------------------------------------ #
    # Serialisation
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AcquireConfig":
        data = dict(data)
        points = [PointSpec(**p) for p in data.pop("points", [])]
        known = {f for f in cls.__dataclass_fields__ if f != "points"}
        kwargs = {k: v for k, v in data.items() if k in known}
        return cls(points=points, **kwargs)

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str | Path) -> "AcquireConfig":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)

    # ------------------------------------------------------------------ #
    # Validation
    # ------------------------------------------------------------------ #
    def enabled_points(self) -> list[PointSpec]:
        return [p for p in self.points if p.enabled and p.name.strip()]

    def validate(self) -> list[str]:
        """Return a list of human-readable problems (empty == valid)."""
        import pandas as pd

        errors: list[str] = []
        if not self.host.strip():
            errors.append("地址 (host) 不能为空")
        if not (0 < int(self.port) < 65536):
            errors.append("端口 (port) 必须在 1-65535 之间")
        if not self.database.strip():
            errors.append("数据库名 (database) 不能为空")

        start = pd.to_datetime(self.start_time, errors="coerce")
        end = pd.to_datetime(self.end_time, errors="coerce")
        if pd.isna(start):
            errors.append(f"起始时间格式无法解析: {self.start_time!r}")
        if pd.isna(end):
            errors.append(f"终止时间格式无法解析: {self.end_time!r}")
        if not pd.isna(start) and not pd.isna(end) and end <= start:
            errors.append("终止时间必须晚于起始时间")

        if self.chunk_hours <= 0:
            errors.append("分段小时数 (chunk_hours) 必须大于 0")
        if not self.enabled_points():
            errors.append("至少需要一个启用且非空的点位")
        if not self.output_path.strip():
            errors.append("输出文件路径不能为空")
        return errors
