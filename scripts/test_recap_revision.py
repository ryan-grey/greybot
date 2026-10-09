"""A corrected recap says what changed, on the card, the page and the message, and edits."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import discord
import recap_card
import recap_page

REVISION = {"date": "2026-10-09", "changes": [
    "Source log changed from the guild log, which stopped after the first boss, to a "
    "raider's full log of the same night.",
    "Kills: 1 before, 2 now."]}
SUMMARY = {"bosses": ["Nek'zali the Soulcoiler"], "raiders": 18, "missing": []}


class RevisionTests(unittest.TestCase):
    def test_card_draws_the_note_and_grows_to_fit_it(self):
        plain = recap_card.render(SUMMARY, guild_name="Prog Raid", night_text="Tuesday")
        revised = recap_card.render(SUMMARY, guild_name="Prog Raid", night_text="Tuesday",
                                    revision=REVISION)
        if plain is None:
            self.skipTest("Pillow is not installed")
        from io import BytesIO
        from PIL import Image
        self.assertIsNotNone(revised)
        self.assertGreater(Image.open(BytesIO(revised)).height,
                           Image.open(BytesIO(plain)).height)

    def test_page_carries_the_dated_note(self):
        html = recap_page.render("Prog Raid", "The Venomous Abyss", "Tuesday, October 6",
                                 ["Nek'zali the Soulcoiler"], [], [], revision=REVISION)
        self.assertIn("Updated 2026-10-09", html)
        self.assertIn("Kills: 1 before, 2 now.", html)
        self.assertNotIn("What changed", recap_page.render(
            "Prog Raid", "The Venomous Abyss", "Tuesday, October 6",
            ["Nek'zali the Soulcoiler"], [], []))

    def test_message_repeats_the_note_as_text_and_pings_nobody(self):
        payload = discord.recap_embed("Prog Raid", "The Venomous Abyss", "Tuesday", SUMMARY,
                                      card_url="https://example.invalid/card.png",
                                      revision=REVISION)
        field = payload["embeds"][0]["fields"][-1]
        self.assertEqual(field["name"], "Updated 2026-10-09 · What changed")
        self.assertIn("Kills: 1 before, 2 now.", field["value"])
        self.assertEqual(payload["allowed_mentions"], {"parse": []})

    def test_a_correction_edits_the_existing_message(self):
        class Response:
            status = 200

            def read(self):
                return b'{"id": "42", "channel_id": "7"}'

            def __enter__(self):
                return self

            def __exit__(self, *unused):
                return False

        with patch.object(discord.urllib.request, "urlopen", return_value=Response()) as urlopen:
            discord.edit_in({"bot_token": "t", "channel": "7"}, "42", {"embeds": []})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "PATCH")
        self.assertTrue(request.full_url.endswith("/channels/7/messages/42"))
        self.assertEqual(json.loads(request.data), {"embeds": []})
        with self.assertRaises(discord.DiscordError):
            discord.edit_in({"webhook": "https://example.invalid"}, "42", {})


if __name__ == "__main__":
    unittest.main()
