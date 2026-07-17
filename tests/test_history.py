from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from data_acquirer.history import RunHistoryStore, RunRecord


def _record(index: int) -> RunRecord:
    return RunRecord(
        started_at=f"2026-07-17T10:00:{index:02d}",
        status="success",
        duration_seconds=index + 0.5,
        rows=index * 10,
        output_path=f"output/{index}.csv",
        point_count=index,
        config={"host": "localhost", "password": ""},
    )


class RunHistoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = (
            Path(__file__).resolve().parent
            / f".run_history_test_{os.getpid()}.json"
        )
        self.temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        self.path.unlink(missing_ok=True)
        self.temporary.unlink(missing_ok=True)

    def tearDown(self) -> None:
        self.path.unlink(missing_ok=True)
        self.temporary.unlink(missing_ok=True)

    def test_round_trip_keeps_newest_records_and_limit(self) -> None:
        store = RunHistoryStore(self.path, limit=2)
        store.append(_record(1))
        store.append(_record(2))
        store.append(_record(3))

        loaded = RunHistoryStore(self.path, limit=2)
        self.assertEqual(
            [record.output_path for record in loaded.records],
            ["output/3.csv", "output/2.csv"],
        )
        self.assertEqual(loaded.records[0].duration_seconds, 3.5)

    def test_corrupt_file_is_treated_as_empty_and_clear_persists(self) -> None:
        self.path.write_text("{not-json", encoding="utf-8")
        store = RunHistoryStore(self.path)
        self.assertEqual(store.records, [])

        store.append(_record(1))
        store.clear()
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), [])


if __name__ == "__main__":
    unittest.main()
