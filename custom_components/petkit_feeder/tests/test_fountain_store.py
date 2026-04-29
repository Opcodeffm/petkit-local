"""Tests for the cloud-block helpers in fountain_store."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fountain_store as fs  # noqa: E402


class CloudFromStoredTest(unittest.TestCase):
    def test_returns_none_on_empty(self):
        self.assertIsNone(fs.cloud_from_stored(None))
        self.assertIsNone(fs.cloud_from_stored({}))

    def test_returns_none_when_no_cloud_key(self):
        self.assertIsNone(fs.cloud_from_stored({"fountains": {}}))

    def test_returns_none_when_token_missing(self):
        self.assertIsNone(fs.cloud_from_stored({"cloud": {"region": "DE"}}))

    def test_extracts_token_and_metadata(self):
        out = fs.cloud_from_stored(
            {
                "cloud": {
                    "token": "deadbeef",
                    "user_id": "999999998",
                    "region": "DE",
                    "created_at": "2026-04-28T06:24Z",
                    "last_refresh_at": "2026-04-28T08:24Z",
                }
            }
        )
        self.assertEqual(out["token"], "deadbeef")
        self.assertEqual(out["user_id"], "999999998")
        self.assertEqual(out["region"], "DE")

    def test_defaults_for_missing_optional_fields(self):
        out = fs.cloud_from_stored({"cloud": {"token": "x"}})
        self.assertEqual(out["token"], "x")
        self.assertEqual(out["region"], "DE")
        self.assertEqual(out["user_id"], "")
        self.assertEqual(out["created_at"], "")


class CloudToStoredTest(unittest.TestCase):
    def test_minimal(self):
        out = fs.cloud_to_stored(token="abc")
        self.assertEqual(out["token"], "abc")
        self.assertEqual(out["region"], "DE")

    def test_full(self):
        out = fs.cloud_to_stored(
            token="abc",
            user_id="42",
            region="CN",
            created_at="2026-01-01T00:00Z",
            last_refresh_at="2026-04-28T08:24Z",
        )
        self.assertEqual(out["region"], "CN")
        self.assertEqual(out["last_refresh_at"], "2026-04-28T08:24Z")


class MergeTest(unittest.TestCase):
    def test_preserves_fountains(self):
        existing = {"fountains": {"aabbcc112233": {"id": 1}}}
        out = fs.merge_cloud_into_store_data(
            existing, fs.cloud_to_stored(token="abc")
        )
        self.assertEqual(out["fountains"], {"aabbcc112233": {"id": 1}})
        self.assertEqual(out["cloud"]["token"], "abc")

    def test_creates_fountains_block_if_missing(self):
        out = fs.merge_cloud_into_store_data(None, fs.cloud_to_stored(token="abc"))
        self.assertEqual(out["fountains"], {})
        self.assertEqual(out["cloud"]["token"], "abc")

    def test_overwrites_existing_cloud(self):
        existing = {"fountains": {}, "cloud": {"token": "OLD"}}
        out = fs.merge_cloud_into_store_data(
            existing, fs.cloud_to_stored(token="NEW")
        )
        self.assertEqual(out["cloud"]["token"], "NEW")

    def test_does_not_mutate_input(self):
        existing = {"fountains": {"x": {}}, "cloud": {"token": "OLD"}}
        fs.merge_cloud_into_store_data(existing, fs.cloud_to_stored(token="NEW"))
        # input unchanged
        self.assertEqual(existing["cloud"]["token"], "OLD")


class ClearTest(unittest.TestCase):
    def test_removes_cloud_keeps_fountains(self):
        existing = {"fountains": {"x": {"id": 1}}, "cloud": {"token": "abc"}}
        out = fs.clear_cloud_from_store_data(existing)
        self.assertNotIn("cloud", out)
        self.assertEqual(out["fountains"], {"x": {"id": 1}})

    def test_safe_on_missing_cloud(self):
        out = fs.clear_cloud_from_store_data({"fountains": {}})
        self.assertNotIn("cloud", out)


if __name__ == "__main__":
    unittest.main()
