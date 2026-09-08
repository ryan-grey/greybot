import copy
import json
from pathlib import Path
import tempfile
import unittest

from greybot_control.raid_import import import_events, load_export
from greybot_control.store import Store


class RaidImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "state" / "control.sqlite3")
        self.event = {"id": "11", "serverId": "1", "channelId": "2", "leaderId": "3",
                      "title": "Example raid", "startTime": 100, "closingTime": 100,
                      "classes": [], "advancedSettings": {"allowed_roles": ["4"]},
                      "signUps": [{"userId": "5", "specName": "Holy", "className": "Priest",
                                   "note": "Running late", "status": "primary", "position": 1}],
                      "unknown_future_field": {"preserve": True}}

    def test_lossless_idempotent_and_no_live_work(self):
        self.assertEqual(import_events(self.store, "1", [self.event])["imported"], 1)
        self.assertEqual(import_events(self.store, "1", [self.event])["unchanged"], 1)
        rows = self.store.events("1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.store.details(rows[0]), self.event)
        self.assertEqual(self.store.jobs("1"), [])

    def test_refresh_preserves_both_roster_versions(self):
        import_events(self.store, "1", [self.event])
        updated = copy.deepcopy(self.event)
        updated["signUps"][0]["note"] = "On time"
        import_events(self.store, "1", [updated])
        snapshots = [self.store.details(row) for row in self.store.events("1")]
        self.assertEqual(snapshots, [updated, self.event])

    def test_wrong_guild_or_malformed_batch_writes_nothing(self):
        for changed in ({"serverId": "9"}, {"signUps": [{}]}, {"closingTime": "tomorrow"}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                import_events(self.store, "1", [self.event, {**self.event, **changed}])
        self.assertEqual(self.store.events("1"), [])

    def test_inventory_reconciliation(self):
        export = self.root / "export"
        export.mkdir()
        (export / "11.json").write_text(json.dumps(self.event))
        self.assertEqual(load_export(export, "1", ["11"]), [self.event])
        with self.assertRaises(ValueError):
            load_export(export, "1", ["11", "12"])
        (export / "11.json").rename(export / "12.json")
        with self.assertRaises(ValueError):
            load_export(export, "1")

    def test_missing_archived_detail_is_not_silently_accepted(self):
        import_events(self.store, "1", [self.event])
        with self.store.connection() as db:
            db.execute("DROP TRIGGER details_no_delete")
            db.execute("DELETE FROM event_details")
        with self.assertRaises(ValueError):
            import_events(self.store, "1", [self.event])
