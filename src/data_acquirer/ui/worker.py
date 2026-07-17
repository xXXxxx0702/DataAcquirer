"""Background worker that runs a :class:`DataPuller` off the UI thread.

Tkinter is single-threaded, so the long-running pull happens in a
``threading.Thread`` and communicates with the UI through a thread-safe
``queue.Queue``. The UI polls the queue with ``root.after`` and renders
messages (log lines, progress, completion, errors).
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Optional

from ..config import AcquireConfig
from ..core import DataPuller, PullCancelled


# --- Messages pushed from the worker thread to the UI thread ---
@dataclass
class LogMsg:
    text: str


@dataclass
class ProgressMsg:
    done: int
    total: int


@dataclass
class SegmentMsg:
    current: int
    total: int
    start: str
    end: str


@dataclass
class DoneMsg:
    rows: int
    output_path: str


@dataclass
class ErrorMsg:
    text: str


@dataclass
class CancelledMsg:
    pass


@dataclass
class CatalogMsg:
    catalog: dict  # {point_name: measurement}


class PullWorker:
    """Owns a worker thread, a message queue and a cancellation flag."""

    def __init__(self, config: AcquireConfig) -> None:
        self.config = config
        self.queue: "queue.Queue" = queue.Queue()
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        self._cancel.clear()
        self._thread = threading.Thread(target=self._run, name="pull-worker", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    # ------------------------------------------------------------------ #
    def _run(self) -> None:
        puller = DataPuller(
            self.config,
            log=lambda msg: self.queue.put(LogMsg(msg)),
            progress=lambda done, total: self.queue.put(ProgressMsg(done, total)),
            segment=lambda current, total, start, end: self.queue.put(
                SegmentMsg(current, total, start, end)
            ),
            is_cancelled=self._cancel.is_set,
        )
        try:
            result = puller.run()
            self.queue.put(DoneMsg(rows=len(result), output_path=self.config.output_path))
        except PullCancelled:
            self.queue.put(CancelledMsg())
        except Exception as exc:  # surfaced to the user in the log panel
            self.queue.put(ErrorMsg(f"{type(exc).__name__}: {exc}"))


class ConnectionTestWorker:
    """Runs ``test_connection`` off the UI thread (it can block on sockets)."""

    def __init__(self, config: AcquireConfig) -> None:
        self.config = config
        self.queue: "queue.Queue" = queue.Queue()
        self._thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="conn-test", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            version = DataPuller(self.config).test_connection()
            self.queue.put(LogMsg(f"连接成功 ✔  InfluxDB 版本: {version}"))
            self.queue.put(DoneMsg(rows=0, output_path=""))
        except Exception as exc:
            self.queue.put(ErrorMsg(f"连接失败: {type(exc).__name__}: {exc}"))


class CatalogWorker:
    """Fetches the ``{point: measurement}`` catalog off the UI thread."""

    def __init__(self, config: AcquireConfig) -> None:
        self.config = config
        self.queue: "queue.Queue" = queue.Queue()
        self._thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="catalog", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            puller = DataPuller(self.config, log=lambda msg: self.queue.put(LogMsg(msg)))
            catalog = puller.fetch_points()
            self.queue.put(CatalogMsg(catalog=catalog))
        except Exception as exc:
            self.queue.put(ErrorMsg(f"加载点位目录失败: {type(exc).__name__}: {exc}"))
