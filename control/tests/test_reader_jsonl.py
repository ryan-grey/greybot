import json
import tempfile
import unittest
from pathlib import Path
from greybot_control.reader import import_jsonl
from greybot_control.store import Store


class ReaderJsonlTests(unittest.TestCase):
    def test_live_records_preserved_and_import_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "state" / "db")
            source = root / "messages.jsonl"
            row = {"id": "10", "guild": "1", "channel": "2", "author": "3", "content": "old", "deleted": False, "observed": 100}
            source.write_text(json.dumps(row)+"\n"+json.dumps({**row,"id":"11"})+"\n")
            store.index_message("1", "10", "2", "3", "new", True)
            self.assertEqual(import_jsonl(store, source, "1"), 1)
            self.assertEqual(import_jsonl(store, source, "1"), 0)
            self.assertEqual(store.message("1", "10")["content"], "new")
            self.assertEqual(store.message("1", "11")["observed"], 100)

    def test_other_guild_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "messages.jsonl"
            source.write_text(json.dumps({"guild":"other"})+"\n")
            with self.assertRaises(ValueError):
                import_jsonl(Store(root / "state" / "db"), source, "1")
