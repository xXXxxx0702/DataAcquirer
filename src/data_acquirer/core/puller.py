"""InfluxDB (v1 / InfluxQL) data puller.

This is a refactor of the original ``loadDataV1`` script's ``pull_data`` and
``core`` functions into a reusable, cancellable, progress-reporting class so it
can be driven from a GUI (or any other front end).

Behaviour preserved from the original:
  * proxies are disabled before importing/using the influxdb client;
  * query times are shifted by ``utc_offset_hours`` before querying and shifted
    back afterwards (the data is stored in UTC, the operator thinks in local time);
  * the full range is walked in ``chunk_hours`` windows;
  * duplicate timestamps are dropped and the index is sorted;
  * results for all points are concatenated column-wise and written to CSV.
"""

from __future__ import annotations

import datetime
import os
from typing import Callable, Optional

import pandas as pd

from ..config import AcquireConfig, PointSpec

# Progress / log callbacks. ``progress(done, total)`` and ``log(message)``.
ProgressCb = Callable[[int, int], None]
LogCb = Callable[[str], None]


class PullCancelled(Exception):
    """Raised internally when the caller requests cancellation."""


def _disable_proxies() -> None:
    """Mirror the original script: never route InfluxDB traffic via a proxy."""
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ[var] = ""
    os.environ["NO_PROXY"] = "*"


class DataPuller:
    def __init__(
        self,
        config: AcquireConfig,
        *,
        log: Optional[LogCb] = None,
        progress: Optional[ProgressCb] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.cfg = config
        self._log = log or (lambda msg: None)
        self._progress = progress or (lambda done, total: None)
        self._is_cancelled = is_cancelled or (lambda: False)

    # ------------------------------------------------------------------ #
    def _check_cancel(self) -> None:
        if self._is_cancelled():
            raise PullCancelled()

    def _client(self):
        # Imported lazily so the rest of the package (and the UI) can load even
        # when the influxdb package is not yet installed.
        from influxdb import InfluxDBClient

        _disable_proxies()
        return InfluxDBClient(
            self.cfg.host,
            int(self.cfg.port),
            self.cfg.username,
            self.cfg.password,
            self.cfg.database,
            proxies={"http": None, "https": None},
        )

    # ------------------------------------------------------------------ #
    def test_connection(self) -> str:
        """Ping the server and return its version string (raises on failure)."""
        client = self._client()
        try:
            version = client.ping()
            dbs = [d.get("name") for d in client.get_list_database()]
            if self.cfg.database not in dbs:
                raise RuntimeError(
                    f"连接成功，但数据库 {self.cfg.database!r} 不存在。"
                    f" 可用数据库: {dbs}"
                )
            return version
        finally:
            client.close()

    # ------------------------------------------------------------------ #
    def fetch_points(self) -> dict[str, str]:
        """Discover the available points from the DB catalog.

        Walks every measurement and reads the distinct values of the
        ``measure_tag`` (``measurePoint``) tag, returning a mapping of
        ``{point_name: measurement}`` for type-ahead suggestions in the UI.
        """
        client = self._client()
        try:
            catalog: dict[str, str] = {}
            measurements = [
                row["name"] for row in client.query("SHOW MEASUREMENTS").get_points()
            ]
            for measurement in measurements:
                self._check_cancel()
                sql = f'SHOW TAG VALUES FROM "{measurement}" WITH KEY = "{self.cfg.measure_tag}"'
                for row in client.query(sql).get_points():
                    value = row.get("value")
                    # First measurement a point appears in wins (points are
                    # normally unique to one measurement/type).
                    if value and value not in catalog:
                        catalog[value] = measurement
            self._log(
                f"已加载点位目录: {len(catalog)} 个点位，来自 {len(measurements)} 个 measurement"
            )
            return catalog
        finally:
            client.close()

    # ------------------------------------------------------------------ #
    def _pull_window(self, client, points: list[PointSpec], start: str, end: str) -> pd.DataFrame:
        """Pull one time window for all points; returns a wide DataFrame."""
        offset = datetime.timedelta(hours=self.cfg.utc_offset_hours)
        q_start = pd.to_datetime(start) - offset
        q_end = pd.to_datetime(end) - offset

        sql_format = (
            'SELECT "{value}" FROM "{measurement}" '
            "WHERE (\"{tag}\" = '{point}') "
            "AND time >= '{start}' AND time < '{end}'"
        )

        frames = []
        for point in points:
            self._check_cancel()
            sql = sql_format.format(
                value=self.cfg.value_field,
                measurement=point.measurement,
                tag=self.cfg.measure_tag,
                point=point.name,
                start=q_start,
                end=q_end,
            )
            result = client.query(sql)
            df = pd.DataFrame(result.get_points())
            if df.empty:
                self._log(f"  警告: {point.name} 在该时段没有返回数据，跳过")
                continue

            df.set_index("time", inplace=True)
            try:
                df.index = pd.to_datetime(df.index, utc=True)
            except Exception:
                df.index = pd.to_datetime(
                    df.index, format="%Y-%m-%dT%H:%M:%S.%fZ", utc=True, errors="coerce"
                )
            df = df[~df.index.duplicated()]
            df.sort_index(inplace=True)
            df.columns = [point.name]
            frames.append(df)

        if not frames:
            return pd.DataFrame()

        merged = pd.concat(frames, axis=1)
        merged.index = merged.index + offset  # shift back to local time
        return merged

    # ------------------------------------------------------------------ #
    def run(self) -> pd.DataFrame:
        """Execute the full chunked pull and write the CSV. Returns the data."""
        errors = self.cfg.validate()
        if errors:
            raise ValueError("配置无效:\n  - " + "\n  - ".join(errors))

        points = self.cfg.enabled_points()
        start_time = self.cfg.start_time
        end_time = self.cfg.end_time
        chunk = datetime.timedelta(hours=self.cfg.chunk_hours)

        # Pre-compute the chunk boundaries so we can report meaningful progress.
        boundaries: list[tuple[str, str]] = []
        cursor = pd.to_datetime(start_time)
        final = pd.to_datetime(end_time)
        while cursor < final:
            nxt = min(cursor + chunk, final)
            boundaries.append((str(cursor), str(nxt)))
            cursor = nxt
        total = len(boundaries)

        self._log(
            f"开始拉取: {len(points)} 个点位, {total} 个时间分段 "
            f"({start_time} ~ {end_time})"
        )

        client = self._client()
        all_frames: list[pd.DataFrame] = []
        try:
            for i, (win_start, win_end) in enumerate(boundaries, start=1):
                self._check_cancel()
                self._log(f"[{i}/{total}] {win_start}  ->  {win_end}")
                df = self._pull_window(client, points, win_start, win_end)
                if not df.empty:
                    df.sort_index(inplace=True)
                    all_frames.append(df)
                self._progress(i, total)
        finally:
            client.close()

        out_path = self.cfg.output_path
        out_dir = os.path.dirname(out_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        if not all_frames:
            self._log(f"警告: 没有获取到任何数据，将写出空文件 {out_path}")
            result = pd.DataFrame()
        else:
            result = pd.concat(all_frames)
            result = result[~result.index.duplicated()]
            result.sort_index(inplace=True)

        result.to_csv(out_path, encoding="utf-8-sig")
        self._log(f"完成: 共 {len(result)} 行，已保存到 {out_path}")
        return result
