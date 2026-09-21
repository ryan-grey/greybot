import asyncio
from datetime import datetime
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import unittest.mock

from greybot_control import log_routing as r
from greybot_control.discord_api import Denied
from greybot_control.store import Store

EMBED = {'type': 'rich', 'title': 'Zatrekaz started a new report', 'provider': {'name': 'Warcraft Logs'},
         'url': 'https://www.warcraftlogs.com/reports/RQXZvWMd6GPhjmc9', 'description': 'Saturday run'}


def eastern(*parts):
    return datetime(*parts, tzinfo=r.ZONE)


class WindowTests(unittest.TestCase):
    def test_progression_nights_and_their_day_of_grace_stay(self):
        self.assertTrue(r.prog_night(eastern(2026, 9, 15, 22, 30)))   # Tuesday raid.
        self.assertTrue(r.prog_night(eastern(2026, 9, 16, 0, 45)))    # Past midnight, same night.
        self.assertTrue(r.prog_night(eastern(2026, 9, 16, 20, 0)))    # Inside the day of grace.
        self.assertTrue(r.prog_night(eastern(2026, 9, 17, 21, 2)))    # Thursday raid.
        self.assertFalse(r.prog_night(eastern(2026, 9, 17, 12, 0)))   # Tuesday's grace expired.
        self.assertFalse(r.prog_night(eastern(2026, 9, 19, 0, 30)))   # Thursday's expires at Saturday.
        self.assertFalse(r.prog_night(eastern(2026, 9, 14, 20, 0)))   # Monday is nobody's raid night.

    def test_only_saturday_reports_outside_the_grace_move(self):
        self.assertTrue(r.misrouted(eastern(2026, 9, 19, 21, 30)))    # Saturday night.
        self.assertTrue(r.misrouted(eastern(2026, 9, 19, 14, 32)))    # Saturday afternoon.
        self.assertTrue(r.misrouted(eastern(2026, 9, 20, 1, 15)))     # Ran past midnight.
        self.assertFalse(r.misrouted(eastern(2026, 9, 20, 9, 0)))     # Sunday proper.
        self.assertFalse(r.misrouted(eastern(2026, 9, 15, 22, 30)))   # Tuesday progression.
        self.assertFalse(r.misrouted(eastern(2026, 9, 14, 20, 0)))    # Monday alt run.


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name)/'test.sqlite3')
        r.install(self.store)
        self.cfg = SimpleNamespace(guild_id='1', enforce=True, prog_logs_channel_id='2', sat_logs_channel_id='3')
        self.packet = {'t': 'MESSAGE_CREATE', 'd': {'id': '9', 'guild_id': '1', 'channel_id': '2',
                       'webhook_id': '7', 'timestamp': '2026-09-19T21:30:00-04:00'}}

    def pending(self):
        with self.store.connection() as db:
            return [dict(row) for row in db.execute('SELECT message,state,target FROM log_route_delivery')]

    def test_only_saturday_integration_posts_in_that_channel_are_recorded(self):
        for packet in (self.packet,
                       {**self.packet, 'd': {**self.packet['d'], 'timestamp': '2026-09-15T22:30:00-04:00'}},
                       {**self.packet, 'd': {**self.packet['d'], 'id': '10', 'webhook_id': None}},
                       {**self.packet, 'd': {**self.packet['d'], 'id': '11', 'channel_id': '4'}},
                       {**self.packet, 'd': {**self.packet['d'], 'id': '12', 'guild_id': '99'}},
                       {**self.packet, 't': 'MESSAGE_UPDATE'}):
            r.observe(self.cfg, self.store, packet)
        self.assertEqual([row['message'] for row in self.pending()], ['9'])

    def test_disabled_routing_records_nothing(self):
        for cfg in (SimpleNamespace(guild_id='1', enforce=False, prog_logs_channel_id='2', sat_logs_channel_id='3'),
                    SimpleNamespace(guild_id='1', enforce=True, prog_logs_channel_id='', sat_logs_channel_id='3')):
            r.observe(cfg, self.store, self.packet)
        self.assertEqual(self.pending(), [])

    def api(self, calls, saturday=(), fail=''):
        original = {'id': '9', 'content': '', 'embeds': [EMBED], 'webhook_id': '7'}

        class API:
            async def request(self, method, path, **kwargs):
                calls.append((method, path, kwargs.get('body')))
                if method == fail:
                    raise RuntimeError('Uncertain write')
                if method == 'POST':
                    return {'id': '50'}
                return list(saturday) if path.startswith('/channels/3/') else original

        return API()

    def test_report_is_published_before_the_original_is_removed(self):
        calls = []
        r.observe(self.cfg, self.store, self.packet)
        for _ in range(3):
            asyncio.run(r.tick(self.cfg, self.store, self.api(calls)))
        self.assertEqual([(method, path) for method, path, _ in calls],
                         [('GET', '/channels/2/messages/9'), ('GET', '/channels/3/messages?limit=50'),
                          ('POST', '/channels/3/messages'), ('DELETE', '/channels/2/messages/9')])
        body = calls[2][2]
        self.assertEqual(body['embeds'][0]['title'], EMBED['title'])
        self.assertNotIn('provider', body['embeds'][0])
        self.assertEqual(body['embeds'][0]['footer']['text'], 'greyBot · moved from the progression log channel')
        self.assertTrue(body['enforce_nonce'])
        self.assertEqual(self.pending(), [{'message': '9', 'state': 'moved', 'target': '50'}])

    def test_a_report_already_in_the_saturday_channel_is_only_removed(self):
        calls = []
        r.observe(self.cfg, self.store, self.packet)
        asyncio.run(r.tick(self.cfg, self.store, self.api(calls, saturday=[{'id': '44', 'content': EMBED['url']}])))
        self.assertNotIn('POST', [method for method, _, _ in calls])
        self.assertEqual(self.pending(), [{'message': '9', 'state': 'moved', 'target': '44'}])

    def test_a_claim_that_published_nothing_resumes_after_a_restart(self):
        r.observe(self.cfg, self.store, self.packet)
        with self.store.connection() as db:
            db.execute("UPDATE log_route_delivery SET state='moving'")
            db.execute("INSERT INTO log_route_delivery VALUES(?,?,?,?,?)", ('1', '8', 'moving', '50', 0))
        r.install(self.store)
        self.assertEqual({row['message']: row['state'] for row in self.pending()},
                         {'9': 'pending', '8': 'moving'})

    def test_failed_post_never_publishes_twice_and_failed_delete_leaves_a_duplicate(self):
        for failing, state in (('POST', 'unknown'), ('DELETE', 'duplicated')):
            with self.store.connection() as db:
                db.execute('DELETE FROM log_route_delivery')
            calls = []
            r.observe(self.cfg, self.store, self.packet)
            for _ in range(2):
                try:
                    asyncio.run(r.tick(self.cfg, self.store, self.api(calls, fail=failing)))
                except RuntimeError:
                    pass
            self.assertEqual(len([1 for method, _, _ in calls if method == 'POST']), 1)
            self.assertEqual(self.pending(), [{'message': '9', 'state': state,
                                               'target': '' if failing == 'POST' else '50'}])

    def test_deleted_original_and_non_report_posts_settle_without_publishing(self):
        for result, state in ((Denied('gone'), 'gone'), ({'id': '9', 'content': 'raid starts soon', 'embeds': []}, 'skipped')):
            with self.store.connection() as db:
                db.execute('DELETE FROM log_route_delivery')
            posts = []

            class API:
                async def request(self, method, path, **kwargs):
                    if method == 'POST':
                        posts.append(path)
                        return {'id': '50'}
                    if isinstance(result, Exception):
                        raise result
                    return result

            r.observe(self.cfg, self.store, self.packet)
            asyncio.run(r.tick(self.cfg, self.store, API()))
            self.assertEqual(posts, [])
            self.assertEqual(self.pending()[0]['state'], state)

    def test_configuration_is_rejected_unless_both_log_channels_are_set(self):
        from greybot_control.config import Config
        base = {'GREYBOT_GUILD_ID': '1', 'GREYBOT_CLIENT_ID': '9', 'GREYBOT_STATE_DIR': self.temp.name}
        for extra in ({'GREYBOT_PROG_LOGS_CHANNEL_ID': '2'},
                      {'GREYBOT_PROG_LOGS_CHANNEL_ID': '2', 'GREYBOT_SAT_LOGS_CHANNEL_ID': '2'},
                      {'GREYBOT_PROG_LOGS_CHANNEL_ID': 'prog', 'GREYBOT_SAT_LOGS_CHANNEL_ID': '3'}):
            with unittest.mock.patch.dict('os.environ', {**base, **extra}, clear=True):
                self.assertRaises(ValueError, Config.from_env)
        with unittest.mock.patch.dict('os.environ', {**base, 'GREYBOT_PROG_LOGS_CHANNEL_ID': '2',
                                                     'GREYBOT_SAT_LOGS_CHANNEL_ID': '3'}, clear=True):
            self.assertEqual((Config.from_env().prog_logs_channel_id, Config.from_env().sat_logs_channel_id), ('2', '3'))


if __name__ == '__main__':
    unittest.main()
