"""Offline eligibility, boundary, dedupe, collection and publication presentation tests."""
import copy
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch, Mock
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
import mplus
import mplus_collect
import mplus_presentation

NOW=datetime(2026,9,15,14,tzinfo=timezone.utc)
START,END=mplus.week_window(NOW)
PEOPLE=[{"name":n,"realm":"Test Realm","region":"us","class":"Mage"} for n in ("Aster","Birch","Cedar","Dawn","Ember")]
KEYS=[mplus.character_key(p) for p in PEOPLE]


def run(identity=1, guild=2, level=10, timed=True, completed=None):
    raw={"season":"season-test","status":"finished","keystone_run_id":identity,
         "mythic_level":level,"clear_time_ms":1000 if timed else 3000,"keystone_time_ms":2000,
         "completed_at":(completed or END-timedelta(days=1)).isoformat(),"dungeon":{"name":"Test Dungeon"},
         "roster":[{"character":p} for p in PEOPLE]}
    return mplus.normalize_run(raw,KEYS[:guild],NOW)


class Repo:
    def __init__(self):self.data={}
    def get(self,k):return self.data.get(k)
    def put(self,k,v,once=False):
        if once and k in self.data:return False
        self.data[k]=v;return True
    def prefix(self,p):return [v for k,v in self.data.items() if k.startswith(p)]
    def lease(self):return 'test'
    def release(self,k):pass


class MythicTests(unittest.TestCase):
    def test_collector_deduplicates_and_resumes_pending_character(self):
        repo=Repo()
        repo.put('ROSTER',{'at':NOW.isoformat(),'members':[mplus.person(p) for p in PEOPLE]})
        raw={"season":"season-test","status":"finished","keystone_run_id":1,
             "mythic_level":12,"clear_time_ms":1000,"keystone_time_ms":2000,
             "completed_at":(NOW-timedelta(hours=1)).isoformat(),"dungeon":{"name":"Test"},
             "roster":[{"character":p} for p in PEOPLE]}
        profile={'mythic_plus_scores_by_season':[{'season':'season-test','scores':{'all':1234}}],
                 'mythic_plus_recent_runs':[{'url':'https://raider.io/mythic-plus-runs/season-test/1',
                                            'completed_at':raw['completed_at']}]}
        source=Mock(side_effect=lambda path,**kw: copy.deepcopy(raw if path.endswith('run-details') else profile))
        with patch('mplus_collect.time.monotonic',side_effect=[0,0,36]):
            mplus_collect.collect(repo,{},NOW,source=source)
        self.assertEqual(repo.get('COLLECTOR')['cursor'],0)
        self.assertEqual(repo.prefix('RUN#'),[])
        result=mplus_collect.collect(repo,{},NOW,source=source)
        self.assertEqual(result['new_runs'],1)
        self.assertEqual(len(repo.prefix('RUN#')),1)
        self.assertEqual(len(repo.prefix('SCORE#')),5)
        self.assertEqual(sum(c.args[0].endswith('run-details') for c in source.call_args_list),1)

    def test_failed_profile_preserves_score_and_advances_cursor(self):
        repo=Repo();repo.put('ROSTER',{'at':NOW.isoformat(),'members':[mplus.person(PEOPLE[0])]})
        key=f'SCORE#{(END+timedelta(days=7)).date()}#{KEYS[0]}'
        repo.put(key,{'score':55})
        result=mplus_collect.collect(repo,{},NOW,source=Mock(side_effect=TimeoutError()))
        self.assertEqual(result['errors'],1)
        self.assertEqual(repo.get(key),{'score':55})
        self.assertEqual(len(repo.prefix('ERROR#')),1)

    def test_publication_dry_duplicate_and_ambiguous_failure(self):
        # Offline clients only: every storage and network boundary is replaced.
        with patch.dict(os.environ,{'AWS_ACCESS_KEY_ID':'testing','AWS_SECRET_ACCESS_KEY':'testing',
                                    'AWS_DEFAULT_REGION':'us-east-1','AWS_EC2_METADATA_DISABLED':'true'}):
            import mplus_service
        summary=mplus.summarize([run()],{},START,END)
        summary['collector_at']=NOW.isoformat()
        cfg={'discord_guild_id':'test','guild_name':'Example Guild','bot_token':'test',
             'recap_page_url':'https://example.org','recap_page_bucket':'test'}
        repo=Repo();upload=Mock();post=Mock(return_value=SimpleNamespace(message_id='123'))
        with patch.dict(os.environ,{'MPLUS_ENABLED':'1','MPLUS_CHANNEL_ID':'123','MPLUS_SCORE_POLICY':'overall_for_participants'}), \
             patch.object(mplus_service.mplus_store,'Repository',return_value=repo), \
             patch.object(mplus_service.mplus_collect,'weekly_data',return_value=summary), \
             patch.object(mplus_service.mplus_presentation,'card',return_value=b'png'), \
             patch.object(mplus_service.discord,'post_to',post), \
             patch.dict(sys.modules,{'handler':SimpleNamespace(publish_bytes=upload)}):
            mplus_service.handle({'mode':'mplus_recap','dry':True},cfg,NOW)
            self.assertFalse(upload.called);self.assertFalse(post.called);self.assertEqual(repo.data,{})
            self.assertTrue(mplus_service.handle({'mode':'mplus_recap'},cfg,NOW)['posted'])
            self.assertEqual(mplus_service.handle({'mode':'mplus_recap'},cfg,NOW)['skipped'],'already_claimed')
            self.assertEqual(post.call_count,1);self.assertEqual(upload.call_count,2)
            repo.data.clear();post.side_effect=TimeoutError('ambiguous')
            with self.assertRaises(TimeoutError):mplus_service.handle({'mode':'mplus_recap'},cfg,NOW)
            self.assertEqual(repo.get('POST#'+summary['end'][:10])['state'],'needs_review')
            mplus_service.handle({'mode':'mplus_recap'},cfg,NOW)
            self.assertEqual(post.call_count,2)
            repo.data.clear();summary['collector_at']=(NOW-timedelta(hours=2)).isoformat()
            with self.assertRaises(RuntimeError):mplus_service.handle({'mode':'mplus_recap'},cfg,NOW)
            self.assertEqual(repo.data,{})

    def test_minimum_members_dedupe_and_counts(self):
        a=run();b=run(2,guild=1);c=run(3,guild=5);d=run(4,guild=5,timed=False)
        s=mplus.summarize([a,a,b,c,d],{},START,END)
        self.assertEqual(s['timed_count'],2)
        self.assertEqual(s['guild_count'],1)
        self.assertEqual(s['boards']['timed'][0]['value'],2)
        self.assertEqual(len(s['boards']['guild_timed']),5)
        self.assertEqual(len(s['runs']),3)

    def test_ten_is_inclusive(self):
        s=mplus.summarize([run(1,level=9),run(2,level=10),run(3,level=11)],{},START,END)
        self.assertEqual(s['boards']['ten'][0]['value'],2)
        self.assertEqual(s['boards']['highest'][0]['value'],11)

    def test_interval_half_open_and_future_rejected(self):
        s=mplus.summarize([run(1,completed=START),run(2,completed=END),run(3,completed=START-timedelta(seconds=1))],{},START,END)
        self.assertEqual(s['timed_count'],1)
        with self.assertRaises(ValueError):run(completed=NOW+timedelta(seconds=1))

    def test_incomplete_duplicate_and_naive_roster_timestamp_rejected(self):
        raw={"status":"finished","season":"season-test","roster":[]}
        with self.assertRaises(ValueError):mplus.normalize_run(raw,KEYS,NOW)
        raw['roster']=[{'character':PEOPLE[0]}]*5
        with self.assertRaises(ValueError):mplus.normalize_run(raw,KEYS,NOW)
        with self.assertRaises(ValueError):mplus.stamp('2026-09-01T00:00:00')

    def test_first_membership_observation_is_frozen(self):
        first=run(guild=2);later=run(guild=5);later['observed']=(NOW+timedelta(hours=1)).isoformat()
        s=mplus.summarize([later,first],{},START,END)
        self.assertEqual(s['guild_count'],0)

    def test_score_missing_and_season_change_are_not_zero(self):
        pair={KEYS[0]:{'start':100,'end':125,'season_start':'a','season_end':'a'},
              KEYS[1]:{'start':0,'end':500,'season_start':'old','season_end':'new'}}
        s=mplus.summarize([run()],pair,START,END)
        self.assertEqual(s['boards']['score'][0]['value'],25)
        self.assertEqual(s['score_unavailable'],1)
        self.assertEqual(mplus.summarize([run()],{},START,END)['boards']['score'],[])

    def test_nonparticipant_cannot_win_score(self):
        s=mplus.summarize([run()],{KEYS[4]:{'start':0,'end':9999,'season_start':'a','season_end':'a'}},START,END)
        self.assertEqual(s['boards']['score'],[])

    def test_ties_keep_rank_and_full_page_keeps_everyone(self):
        s=mplus.summarize([run(guild=5)],{},START,END)
        self.assertEqual([r['rank'] for r in s['boards']['timed']],[1]*5)
        text=mplus_presentation.page(s,'Example Guild')
        self.assertIn('Ember',text)
        payload=mplus_presentation.discord_post(s,'Example Guild','https://example.org/recap/')
        self.assertEqual(len(payload['embeds'][0]['fields']),6)
        self.assertEqual(len(payload['embeds'][0]['fields'][2]['value'].splitlines()),3)
        image_payload=mplus_presentation.discord_post(s,'Example Guild','https://example.org/recap/',
                                                      'https://example.org/recap/card.png')
        self.assertNotIn('fields',image_payload['embeds'][0])
        self.assertIn('image',image_payload['embeds'][0])

    def test_dst_keeps_tuesday_ten_local(self):
        a,b=mplus.week_window(datetime(2026,3,10,14,tzinfo=timezone.utc))
        self.assertEqual((a.astimezone(mplus.EASTERN).hour,b.astimezone(mplus.EASTERN).hour),(10,10))
        self.assertEqual((b-a).total_seconds()/3600,167)
        a,b=mplus.week_window(datetime(2026,11,3,15,tzinfo=timezone.utc))
        self.assertEqual((b-a).total_seconds()/3600,169)
        _,before=mplus.week_window(NOW-timedelta(seconds=1))
        self.assertEqual(before,START)

    def test_html_and_discord_escape_source_strings(self):
        r=run();r['dungeon']='<script>alert(1)</script>'
        r['roster'][0]['name']='@everyone <img src=x>'
        s=mplus.summarize([r],{},START,END)
        p=mplus_presentation.page(s,'<Guild>')
        self.assertNotIn('<script>',p);self.assertNotIn('<img src=x>',p)
        self.assertEqual(mplus_presentation.safe_url('javascript:alert(1)'), '')
        d=mplus_presentation.discord_post(s,'Guild','https://example.org/')
        self.assertEqual(d['allowed_mentions'],{'parse':[]})
        self.assertNotIn('@everyone',str(d))

    def test_snapshot_staleness_and_weekly_reads(self):
        repo=Repo();r=run();repo.put('RUN#'+r['completed'][:10]+'#'+r['id'],r)
        for boundary,score in ((START,100),(END,200)):
            repo.put(f'SCORE#{boundary.date()}#{KEYS[0]}',{'key':KEYS[0],'score':score,'season':'a','at':(boundary-timedelta(minutes=10)).isoformat()})
        self.assertEqual(mplus_collect.weekly_data(repo,NOW)['boards']['score'][0]['value'],100)
        repo.data[f'SCORE#{START.date()}#{KEYS[0]}']['at']=(START-timedelta(hours=2)).isoformat()
        self.assertEqual(mplus_collect.weekly_data(repo,NOW)['boards']['score'],[])

    def test_image_renders_using_existing_raid_fonts(self):
        image=mplus_presentation.card(mplus.summarize([run(guild=5)],{},START,END),'Example Guild')
        self.assertIsNotNone(image)
        self.assertTrue(image.startswith(b'\x89PNG'))


if __name__=='__main__':unittest.main()
