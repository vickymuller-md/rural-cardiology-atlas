from __future__ import annotations

import hashlib
import json
import io
import tempfile
import unittest
from pathlib import Path

from atlas_pipeline.errors import IntegrityError
from atlas_pipeline.streaming_nppes import StreamingNppesStore, iter_selected_source_rows
from atlas_pipeline.contracts import NPPES_ANALYTICAL_FIELDS
from tests.synthetic import make_npi, nppes_row


class StreamingNppesTests(unittest.TestCase):
    def test_identical_duplicate_reconciles_across_one_row_chunks(self):
        row = nppes_row(make_npi("300000001"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with StreamingNppesStore(root / "rows.sqlite") as store:
                audit = store.ingest_rows((row, dict(row)), chunk_rows=1)
                self.assertEqual(
                    audit.raw_rows,
                    audit.retained_unique_npis + audit.identical_duplicate_extra_rows,
                )
                self.assertEqual(audit.identical_duplicate_extra_rows, 1)
                outputs = store.finalize(
                    root / "out",
                    source_snapshot_id=hashlib.sha256(
                        b"identical-duplicate-nppes-source"
                    ).hexdigest(),
                )
            selected = list(iter_selected_source_rows(outputs.selected_path))
            self.assertEqual(len(selected), 1)
            self.assertEqual(selected[0]["NPI"], row["NPI"])
            receipt = json.loads(outputs.receipt_path.read_text())
            self.assertEqual(receipt["artifacts"]["nppes_selected"]["sha256"], outputs.selected_sha256)

    def test_conflicting_duplicate_across_chunks_fails_source_integrity(self):
        row = nppes_row(make_npi("300000002"))
        conflict = dict(row)
        conflict["Last Update Date"] = "06/02/2026"
        with tempfile.TemporaryDirectory() as tmp:
            with StreamingNppesStore(Path(tmp) / "rows.sqlite") as store:
                store.ingest_rows((row,), chunk_rows=1)
                with self.assertRaises(IntegrityError):
                    store.ingest_rows((conflict,), chunk_rows=1)
                self.assertEqual(store.audit().raw_rows, 1)

    def test_canonical_equivalent_but_raw_distinct_unicode_is_a_conflict(self):
        row = nppes_row(
            make_npi("300000003"),
            **{"Provider First Line Business Practice Location Address": "10 CAFÉ STREET"},
        )
        conflict = dict(row)
        conflict["Provider First Line Business Practice Location Address"] = (
            "10 CAFE\u0301 STREET"
        )
        with tempfile.TemporaryDirectory() as tmp:
            with StreamingNppesStore(Path(tmp) / "rows.sqlite") as store:
                store.ingest_rows((row,), chunk_rows=1)
                with self.assertRaisesRegex(IntegrityError, "conflicting duplicate"):
                    store.ingest_rows((conflict,), chunk_rows=1)

    def test_disk_store_retains_full_positional_source_only_for_eligible_rows(self):
        eligible = nppes_row(make_npi("300000004"))
        excluded = nppes_row(make_npi("300000005"), **{"Entity Type Code": "2"})
        with tempfile.TemporaryDirectory() as tmp:
            with StreamingNppesStore(Path(tmp) / "rows.sqlite") as store:
                store.ingest_rows((eligible, excluded))
                rows = store.connection.execute(
                    "SELECT bucket, selected_source_values FROM providers ORDER BY npi"
                ).fetchall()
                self.assertEqual(rows[0][0], "pre_geography_eligible")
                self.assertIsNotNone(rows[0][1])
                self.assertEqual(rows[1][0], "entity_type_not_individual")
                self.assertIsNone(rows[1][1])
                outputs = store.finalize(
                    Path(tmp) / "out",
                    source_snapshot_id=hashlib.sha256(
                        b"compact-nppes-source"
                    ).hexdigest(),
                )
            receipt = json.loads(outputs.receipt_path.read_text())
            self.assertEqual(
                receipt["database_schema"],
                "sqlite-providers-v2-compact-positional",
            )

    def test_scaled_stream_uses_disk_index_and_canonical_sorted_output(self):
        count = 5_000

        def rows():
            for index in range(count):
                yield nppes_row(make_npi(f"{400000000 + index:09d}"))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with StreamingNppesStore(root / "rows.sqlite") as store:
                audit = store.ingest_rows(rows(), chunk_rows=137)
                self.assertEqual((audit.raw_rows, audit.retained_unique_npis), (count, count))
                self.assertGreater((root / "rows.sqlite").stat().st_size, 0)
                outputs = store.finalize(
                    root / "out",
                    source_snapshot_id=hashlib.sha256(
                        b"scaled-nppes-source"
                    ).hexdigest(),
                )
            selected = iter_selected_source_rows(outputs.selected_path)
            first = next(selected)["NPI"]
            last = first
            observed = 1
            for row in selected:
                self.assertGreater(row["NPI"], last)
                last = row["NPI"]
                observed += 1
            self.assertEqual(observed, count)

    def test_csv_ingest_rejects_header_only_and_row_width_drift(self):
        header = ",".join(NPPES_ANALYTICAL_FIELDS) + "\n"
        with tempfile.TemporaryDirectory() as tmp:
            with StreamingNppesStore(Path(tmp) / "rows.sqlite") as store:
                with self.assertRaises(IntegrityError):
                    store.ingest_csv(io.StringIO(header), chunk_rows=1)
        with tempfile.TemporaryDirectory() as tmp:
            with StreamingNppesStore(Path(tmp) / "rows.sqlite") as store:
                with self.assertRaises(IntegrityError):
                    store.ingest_csv(io.StringIO(header + "too,few\n"), chunk_rows=1)


if __name__ == "__main__":
    unittest.main()
