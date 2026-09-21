import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from greybot_control import featured as f
from greybot_control.discord_api import Denied
from greybot_control.store import Store

SOCIAL, MEMES, FEATURED, STAFF = '500', '501', '502', '600'


def star(message='9', member='11', channel=MEMES, kind='MESSAGE_REACTION_ADD'):
    return {'t': kind, 'd': {'guild_id': '1', 'channel_id': channel, 'message_id': message,
                             'user_id': member, 'emoji': {'id': None, 'name': f.STAR},
                             'member': {'user': {'id': member, 'bot': False}}}}


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name)/'test.sqlite3')
        f.install(self.store)
        self.cfg = SimpleNamespace(guild_id='1', client_id='9', enforce=True,
                                   featured_channel_id=FEATURED, feature_category_id=SOCIAL,
                                   feature_threshold=3)

    def delivery(self):
        with self.store.connection() as db:
            return [dict(r) for r in db.execute('SELECT message,state,target,stars FROM feature_delivery')]

    def votes(self, message='9'):
        with self.store.connection() as db:
            return db.execute('SELECT COUNT(*) n FROM feature_nominations WHERE message=?',
                              (message,)).fetchone()['n']


class FeatureTests(Base):
    def test_a_member_votes_once_however_they_cast_it(self):
        for _ in range(3):
            f.observe(self.cfg, self.store, star(member='11'))
        self.assertEqual(self.votes(), 1)
        self.assertEqual(self.delivery(), [])
        f.observe(self.cfg, self.store, star(member='12'))
        self.assertEqual(self.votes(), 2)
        self.assertEqual(self.delivery(), [])

    def test_the_threshold_queues_the_post_exactly_once(self):
        for member in ('11', '12', '13', '14'):
            f.observe(self.cfg, self.store, star(member=member))
        self.assertEqual(self.votes(), 4)
        self.assertEqual([(d['message'], d['state'], d['stars']) for d in self.delivery()], [('9', 'pending', 3)])

    def test_bots_other_emoji_and_other_guilds_are_ignored(self):
        packet = star()
        for bad in ({**packet, 'd': {**packet['d'], 'guild_id': '99'}},
                    {**packet, 'd': {**packet['d'], 'emoji': {'id': None, 'name': '🔥'}}},
                    {**packet, 'd': {**packet['d'], 'emoji': {'id': '77', 'name': 'custom'}}},
                    {**packet, 'd': {**packet['d'], 'member': {'user': {'id': '11', 'bot': True}}}},
                    {**packet, 't': 'MESSAGE_CREATE'}):
            f.observe(self.cfg, self.store, bad)
        self.assertEqual(self.votes(), 0)

    def test_taking_the_star_back_counts_until_it_is_featured(self):
        f.observe(self.cfg, self.store, star(member='11'))
        f.observe(self.cfg, self.store, star(member='11', kind='MESSAGE_REACTION_REMOVE'))
        self.assertEqual(self.votes(), 0)
        for member in ('11', '12', '13'):
            f.observe(self.cfg, self.store, star(member=member))
        f.observe(self.cfg, self.store, star(member='11', kind='MESSAGE_REACTION_REMOVE'))
        self.assertEqual(self.votes(), 3)  # Queued already; the vote no longer comes back out.

    def test_disabled_featuring_records_nothing(self):
        for cfg in (SimpleNamespace(**{**vars(self.cfg), 'enforce': False}),
                    SimpleNamespace(**{**vars(self.cfg), 'featured_channel_id': ''})):
            f.observe(cfg, self.store, star())
        self.assertEqual(self.votes(), 0)


class MenuTests(Base):
    def menu(self, channel=MEMES, parent=SOCIAL, author='42', member='11'):
        return {'type': 2, 'guild_id': '1', 'application_id': '9', 'id': '77', 'token': 't',
                'member': {'user': {'id': member, 'bot': False}},
                'channel': {'id': channel, 'parent_id': parent},
                'data': {'name': 'Feature this post', 'type': 3, 'target_id': '9',
                         'resolved': {'messages': {'9': {'id': '9', 'author': {'id': author}}}}}}

    def test_the_menu_casts_the_same_vote_and_reports_the_count(self):
        f.observe(self.cfg, self.store, star(member='11'))
        answer = f.receive(self.cfg, self.store, self.menu(member='11'))
        self.assertEqual(self.votes(), 1)  # Same member, same vote.
        self.assertIn('**1** of **3**', answer['data']['content'])
        self.assertEqual(answer['data']['flags'], 64)
        f.receive(self.cfg, self.store, self.menu(member='12'))
        answer = f.receive(self.cfg, self.store, self.menu(member='13'))
        self.assertEqual(self.votes(), 3)
        self.assertIn('going to', answer['data']['content'])
        self.assertEqual([d['state'] for d in self.delivery()], ['pending'])

    def test_the_menu_refuses_outside_the_social_category_and_on_greybots_own_posts(self):
        for packet in (self.menu(parent=STAFF), self.menu(channel=FEATURED, parent=SOCIAL),
                       self.menu(author='9')):
            self.assertRaises(Denied, f.receive, self.cfg, self.store, packet)
        self.assertEqual(self.votes(), 0)

    def test_an_already_queued_post_is_not_voted_on_again(self):
        for member in ('11', '12', '13'):
            f.observe(self.cfg, self.store, star(member=member))
        answer = f.receive(self.cfg, self.store, self.menu(member='14'))
        self.assertIn('already on its way', answer['data']['content'])
        self.assertEqual(self.votes(), 3)


class SeedStarTests(Base):
    def menu(self, member='11'):
        return {'type': 2, 'guild_id': '1', 'application_id': '9', 'id': '77', 'token': 't',
                'member': {'user': {'id': member, 'bot': False}},
                'channel': {'id': MEMES, 'parent_id': SOCIAL},
                'data': {'name': 'Feature this post', 'type': 3, 'target_id': '9',
                         'resolved': {'messages': {'9': {'id': '9', 'author': {'id': '42'}}}}}}

    def marks(self):
        with self.store.connection() as db:
            return {r['message']: r['state'] for r in
                    db.execute('SELECT message,state FROM feature_marks')}

    def api(self, calls, reactions=()):
        class API:
            async def request(self, method, path, **kwargs):
                calls.append((method, path))
                return {'id': '9', 'reactions': list(reactions)} if method == 'GET' else None
        return API()

    def test_the_menu_puts_greybots_star_on_the_post_so_others_can_click_it(self):
        calls = []
        f.receive(self.cfg, self.store, self.menu())
        self.assertEqual(self.marks(), {'9': 'pending'})
        asyncio.run(f.marks_tick(self.cfg, self.store, self.api(calls)))
        self.assertIn(('PUT', f'/channels/{MEMES}/messages/9/reactions/%E2%AD%90/@me'), calls)
        self.assertEqual(self.marks(), {'9': 'seeded'})

    def test_no_seed_when_a_member_already_starred_it(self):
        calls = []
        f.receive(self.cfg, self.store, self.menu())
        reactions = [{'emoji': {'id': None, 'name': f.STAR}, 'count': 1, 'me': False}]
        asyncio.run(f.marks_tick(self.cfg, self.store, self.api(calls, reactions)))
        self.assertEqual([m for m, _ in calls], ['GET'])
        self.assertEqual(self.marks(), {'9': 'done'})

    def test_greybot_takes_its_star_back_once_a_member_stars_it(self):
        calls = []
        f.receive(self.cfg, self.store, self.menu())
        asyncio.run(f.marks_tick(self.cfg, self.store, self.api(calls)))
        f.observe(self.cfg, self.store, star(member='12'))
        self.assertEqual(self.marks(), {'9': 'clearing'})
        calls.clear()
        asyncio.run(f.marks_tick(self.cfg, self.store, self.api(calls)))
        self.assertEqual(calls, [('DELETE', f'/channels/{MEMES}/messages/9/reactions/%E2%AD%90/@me')])
        self.assertEqual(self.marks(), {'9': 'done'})

    def test_a_star_reaction_alone_never_seeds_anything(self):
        calls = []
        f.observe(self.cfg, self.store, star(member='11'))
        asyncio.run(f.marks_tick(self.cfg, self.store, self.api(calls)))
        self.assertEqual(calls, [])
        self.assertEqual(self.marks(), {})


class CardTests(Base):
    MESSAGE = {'id': '9', 'content': 'look at this', 'timestamp': '2026-09-21T12:00:00+00:00',
               'author': {'id': '42', 'username': 'someone', 'avatar': 'a'*32},
               'attachments': [{'content_type': 'image/png', 'url': 'https://cdn/x.png'}]}

    def api(self, calls, parent=SOCIAL, fail=''):
        message = self.MESSAGE

        class API:
            async def request(self, method, path, **kwargs):
                calls.append((method, path, kwargs.get('body')))
                if method == fail:
                    raise RuntimeError('Uncertain write')
                if method == 'POST':
                    return {'id': '70'}
                if method == 'PUT':
                    return None
                return ({'id': MEMES, 'name': 'memes', 'parent_id': parent}
                        if path == f'/channels/{MEMES}' else message)

        return API()

    def queue(self):
        for member in ('11', '12', '13'):
            f.observe(self.cfg, self.store, star(member=member))

    def test_the_card_carries_the_author_the_image_and_a_jump_link(self):
        calls = []
        self.queue()
        for _ in range(3):
            asyncio.run(f.tick(self.cfg, self.store, self.api(calls)))
        posts = [body for method, _, body in calls if method == 'POST']
        self.assertEqual(len(posts), 1)
        embed = posts[0]['embeds'][0]
        self.assertEqual(embed['image']['url'], 'https://cdn/x.png')
        self.assertEqual(embed['description'], 'look at this')
        self.assertIn(f'/channels/1/{MEMES}/9', embed['fields'][0]['value'])
        self.assertEqual(embed['footer']['text'], f'{f.STAR} 3 · #memes')
        self.assertTrue(posts[0]['enforce_nonce'])
        self.assertEqual([(d['state'], d['target']) for d in self.delivery()], [('featured', '70')])

    def test_a_post_nominated_outside_the_category_never_features(self):
        calls = []
        self.queue()
        asyncio.run(f.tick(self.cfg, self.store, self.api(calls, parent=STAFF)))
        self.assertEqual([m for m, _, _ in calls if m == 'POST'], [])
        self.assertEqual([d['state'] for d in self.delivery()], ['ineligible'])

    def test_an_uncertain_post_never_features_twice(self):
        calls = []
        self.queue()
        for _ in range(2):
            try:
                asyncio.run(f.tick(self.cfg, self.store, self.api(calls, fail='POST')))
            except RuntimeError:
                pass
        self.assertEqual([d['state'] for d in self.delivery()], ['unknown'])

    def test_a_deleted_post_settles_without_a_card(self):
        calls = []
        self.queue()

        class API:
            async def request(self, method, path, **kwargs):
                calls.append((method, path, None))
                if path == f'/channels/{MEMES}':
                    return {'id': MEMES, 'name': 'memes', 'parent_id': SOCIAL}
                raise Denied('gone')

        asyncio.run(f.tick(self.cfg, self.store, API()))
        self.assertEqual([m for m, _, _ in calls if m == 'POST'], [])
        self.assertEqual([d['state'] for d in self.delivery()], ['gone'])


if __name__ == '__main__':
    unittest.main()
