from __future__ import annotations

import queue
import unittest
from unittest.mock import mock_open, patch

import pandas as pd

from data_acquirer.config import AcquireConfig, PointSpec
from data_acquirer.core.puller import DataPuller
from data_acquirer.ui.worker import DoneMsg, ProgressMsg, PullWorker, SegmentMsg


class _FakeClient:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class SegmentCallbackTests(unittest.TestCase):
    def test_segment_is_announced_before_each_completed_window(self) -> None:
        events: list[tuple] = []
        client = _FakeClient()
        config = AcquireConfig(
            start_time="2026-01-01 00:00:00",
            end_time="2026-01-01 03:00:00",
            chunk_hours=1,
            points=[PointSpec(name="P1")],
            output_path="ignored.csv",
        )
        puller = DataPuller(
            config,
            segment=lambda current, total, start, end: events.append(
                ("segment", current, total, start, end)
            ),
            # Keeping this callback at exactly two arguments verifies the
            # existing progress(done, total) API remains compatible.
            progress=lambda done, total: events.append(("progress", done, total)),
        )

        with (
            patch.object(puller, "_client", return_value=client),
            patch.object(puller, "_pull_window", return_value=pd.DataFrame()),
            patch("builtins.open", mock_open()),
            patch.object(pd.DataFrame, "to_csv", autospec=True),
        ):
            puller.run()

        self.assertEqual(
            [(event[0], event[1], event[2]) for event in events],
            [
                ("segment", 1, 3),
                ("progress", 1, 3),
                ("segment", 2, 3),
                ("progress", 2, 3),
                ("segment", 3, 3),
                ("progress", 3, 3),
            ],
        )
        self.assertEqual(
            events[0][3:],
            ("2026-01-01 00:00:00", "2026-01-01 01:00:00"),
        )
        self.assertEqual(
            events[4][3:],
            ("2026-01-01 02:00:00", "2026-01-01 03:00:00"),
        )
        self.assertTrue(client.closed)


class PullWorkerMessageTests(unittest.TestCase):
    def test_worker_enqueues_segment_progress_and_done_messages(self) -> None:
        class FakePuller:
            def __init__(self, config, **callbacks) -> None:
                self.callbacks = callbacks

            def run(self) -> pd.DataFrame:
                self.callbacks["segment"](2, 4, "window-start", "window-end")
                self.callbacks["progress"](2, 4)
                return pd.DataFrame(index=range(7))

        worker = PullWorker(AcquireConfig(output_path="output.csv"))
        with patch("data_acquirer.ui.worker.DataPuller", FakePuller):
            worker._run()

        messages = []
        while True:
            try:
                messages.append(worker.queue.get_nowait())
            except queue.Empty:
                break

        self.assertEqual(
            messages,
            [
                SegmentMsg(2, 4, "window-start", "window-end"),
                ProgressMsg(2, 4),
                DoneMsg(7, "output.csv"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
