import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from greybot_control.audit_import import import_available
from greybot_control.store import Store


class AuditImportTests(unittest.TestCase):
    def test_import_deduplicates_gateway_history_and_excludes_arbitrary_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "db")
            store.append("gateway:old:1", "1", "GUILD_AUDIT_LOG_ENTRY_CREATE", "2", {"id": "100"})
            class API:
                async def request(self, *args):
                    return {"audit_log_entries": [
                        {"id": "101", "user_id": "2", "target_id": "3", "action_type": 20,
                         "reason": "excluded private text", "changes": [{"key": "name", "new_value": "excluded private text"}]},
                        {"id": "100", "action_type": 20}]}
            cfg = SimpleNamespace(guild_id="1")
            first = asyncio.run(import_available(cfg, store, API()))
            second = asyncio.run(import_available(cfg, store, API()))
            self.assertEqual(first["imported"], 1)
            self.assertEqual(second["imported"], 0)
            self.assertTrue(first["available_history_exhausted"])
            self.assertNotIn("excluded private text", str(store.events("1")))
            self.assertTrue(store.verify())
