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
import mplus_records
import mplus_record_board

NOW=datetime(2026,9,15,14,tzinfo=timezone.utc)
START,END=mplus.week_window(NOW)
PEOPLE=[{"name":n,"realm":"Test Realm","region":"us","class":"Mage"} for n in ("Aster","Birch","Cedar","Dawn","Ember")]
KEYS=[mplus.character_key(p) for p in PEOPLE]
SEASONS=[{'slug':'season-test','name':'MN Season 2','is_main_season':True,
          'starts':{'us':'2026-08-18T15:00:00Z'},'ends':{'us':'2030-01-01T00:00:00Z'}}]


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
    def lease(self,key='LEASE'):return 'test'
    def release(self,k,key='LEASE'):pass
    def after(self,p,cursor,limit=50):
        return [(k,v) for k,v in sorted(self.data.items()) if k.startswith(p) and k>cursor][:limit]


class MythicTests(unittest.TestCase):
    def test_every_category_has_unique_positions_and_characters(self):
        scores=[{**mplus.person(p),'score':3000} for p in PEOPLE]
        summary=mplus.summarize([run(1,guild=5),run(2,guild=5,level=11)],
            {k:{'start':100,'end':200,'season_start':'a','season_end':'a'} for k in KEYS},START,END,
            overall_scores=scores+scores)
        for key,rows in summary['boards'].items():
            self.assertEqual(len(rows),5,key)
            self.assertEqual(len({r['key'] for r in rows}),5,key)
            self.assertEqual([r['rank'] for r in rows],list(range(1,6)),key)
        self.assertEqual([r['value'] for r in summary['boards']['guild_highest']],[11]*5)

    def test_archive_gain_week_four_and_live_only_week_five(self):
        repo=Repo();seasons=copy.deepcopy(SEASONS);seasons[0]['slug']='season-mn-2'
        repo.put('SEASONS',{'items':seasons,'region':'us'})
        repo.put('ROSTER',{'members':[mplus.person(p) for p in PEOPLE]})
        r=run(completed=NOW-timedelta(days=1));r['season']='season-mn-2'
        repo.put('RUN#2026-09-14#1',r)
        repo.put('ARCHIVED_SCORE#2026-09-08',{'season':'season-mn-2','at':'2026-09-08T07:41:38Z','scores':{KEYS[0]:2000}})
        repo.put('SCORE#2026-09-15#'+KEYS[0],{'key':KEYS[0],'at':(NOW-timedelta(minutes=10)).isoformat(),'score':2200.5,'season':'season-mn-2'})
        week4=mplus_collect.weekly_data(repo,NOW)
        self.assertEqual(week4['boards']['score'][0]['value'],200.5)
        self.assertIn('approximate',week4['score_note'])
        self.assertEqual(dict(mplus_presentation.categories(week4))['score'],'Archived IO gain')
        next_week=NOW+timedelta(days=7);r=run(2);r['completed']=(next_week-timedelta(days=1)).isoformat();r['observed']=next_week.isoformat();r['season']='season-mn-2'
        repo.put('RUN#2026-09-21#2',r)
        repo.put('SCORE#2026-09-22#'+KEYS[0],{'key':KEYS[0],'at':(next_week-timedelta(minutes=10)).isoformat(),'score':2300.5,'season':'season-mn-2'})
        week5=mplus_collect.weekly_data(repo,next_week)
        self.assertEqual(week5['boards']['score'][0]['value'],100.0)
        self.assertEqual(dict(mplus_presentation.categories(week5))['score'],'Weekly IO gain')
        del repo.data['SCORE#2026-09-15#'+KEYS[0]]
        self.assertEqual(mplus_collect.weekly_data(repo,next_week)['boards']['score'],[])

    def test_record_alert_names_both_guild_groups(self):
        old=run(guild=2);new=run(2,guild=3,level=11)
        old['roster'][0]['name']='PreviousTank'
        body=mplus_records.payload(new,old,'https://discord.com/channels/1/2/3')
        self.assertIn('Aster, Birch, Cedar',body['embeds'][0]['description'])
        prior=next(f for f in body['embeds'][0]['fields'] if f['name']=='Previous guild record holders')
        self.assertEqual(prior['value'],'PreviousTank, Birch')
        self.assertNotIn('Dawn',str(body))

    def test_overall_io_includes_members_without_any_guild_run_and_caps_page(self):
        scores=[{**mplus.person(PEOPLE[0]),'key':str(i),'name':f'RankedCharacter{i:02}',
                 'score':3000-i} for i in range(25)]
        summary=mplus.summarize([],{},START,END,overall_scores=scores)
        self.assertEqual(len(summary['boards']['overall']),25)
        self.assertEqual(summary['boards']['overall'][0]['value'],3000)
        self.assertEqual(summary['boards']['score'],[])
        page=mplus_presentation.page(summary,'Test')
        self.assertIn('RankedCharacter19',page)
        self.assertNotIn('RankedCharacter20',page)
        self.assertEqual(mplus.display_value('overall',summary['boards']['overall'][0]),'3,000.0')

    def test_overall_io_requires_fresh_same_season_end_sample_only(self):
        repo=Repo();repo.put('SEASONS',{'items':SEASONS,'region':'us'})
        repo.put('ROSTER',{'members':[mplus.person(p) for p in PEOPLE]})
        for i in range(3):
            repo.put(f'SCORE#{END.date()}#{KEYS[i]}',{'key':KEYS[i],'at':(END-timedelta(minutes=5 if i!=1 else 65)).isoformat(),
                     'season':'season-test' if i!=2 else 'season-old','score':3000})
        result=mplus_collect.weekly_data(repo,NOW)
        self.assertEqual([p['key'] for p in result['boards']['overall']],[KEYS[0]])

    def test_record_page_escapes_names_and_links_actual_runs(self):
        r=run();r['roster'][0]['name']='<script>alert(1)</script>'
        output=mplus_record_board.page([r],'Test','Season','https://example.test/card.png',NOW)
        self.assertIn('&lt;script&gt;',output)
        self.assertNotIn('<script>alert',output)
        self.assertIn('http-equiv="refresh" content="60"',output)
        self.assertIn(r['url'],output)
        self.assertIn('--c-dark:',output)

    def test_board_creation_pin_retry_and_edit_reuses_message(self):
        repo=Repo();repo.put('SEASONS',{'items':SEASONS})
        state={'best':{'test':run()}}
        cfg={'guild_region':'us','guild_name':'Test','discord_guild_id':'123',
             'bot_token':'test','recap_page_url':'https://example.test'}
        with patch('mplus_record_board.render',return_value=b'png'), patch('mplus_record_board.artwork',return_value={}), patch('handler.publish_bytes'), \
             patch('mplus_record_board.discord.post_to',return_value=SimpleNamespace(message_id='456')) as post, \
             patch('mplus_record_board.request',side_effect=TimeoutError) as request:
            with self.assertRaises(TimeoutError):mplus_record_board.sync(repo,cfg,'789',state,NOW)
            self.assertEqual(repo.get('RECORD_BOARD#789')['message'],'456')
            request.side_effect=None
            url=mplus_record_board.sync(repo,cfg,'789',state,NOW)
            self.assertEqual(url,'https://discord.com/channels/123/789/456')
            self.assertEqual(post.call_count,1)
            request.reset_mock()
            mplus_record_board.sync(repo,cfg,'789',state,NOW)
            request.assert_not_called()
            state['best']['test']['level']=11
            mplus_record_board.sync(repo,cfg,'789',state,NOW)
            self.assertEqual(request.call_args.args[1],'PATCH')
            self.assertEqual(post.call_count,1)

    def test_record_refresh_precedes_announcement_and_links_pin(self):
        r=run(completed=NOW-timedelta(minutes=1))
        repo=Repo();repo.put('RECORDS',{'best':{},'since':(NOW-timedelta(hours=1)).isoformat(),'cursor':mplus_records.QUEUE})
        repo.put(mplus_records.QUEUE+'1',r)
        calls=[]
        def sync(*args):calls.append('refresh');return 'https://discord.com/channels/1/2/3'
        def post(*args,**kwargs):
            self.assertEqual(calls[-1],'refresh')
            self.assertIn('/1/2/3',args[1]['content'])
            calls.append('post');return SimpleNamespace(message_id='4')
        with patch.dict(os.environ,{'MPLUS_RECORD_BOARD_ENABLED':'1'}),patch('mplus_record_board.sync',side_effect=sync):
            mplus_records.process(repo,{'bot_token':'test'},'2',NOW,post=post)
        self.assertEqual(calls,['refresh','refresh','post'])

    def test_board_ambiguous_creation_never_duplicates(self):
        repo=Repo();repo.put('SEASONS',{'items':SEASONS});repo.put('RECORD_BOARD#789',{'state':'sending'})
        with self.assertRaises(RuntimeError):
            mplus_record_board.sync(repo,{'guild_region':'us'},'789',{'best':{'test':run()}},NOW)

    def test_record_improvement_rules_and_delivery_once(self):
        base=run(level=10);best=mplus_records.record_key(base)
        repo=Repo();repo.put('RECORDS',{'best':{best:base},'since':(NOW-timedelta(hours=2)).isoformat(),
                                      'cursor':mplus_records.QUEUE})
        fresh=NOW-timedelta(hours=1)
        lower=run(2,level=9,completed=fresh)
        equal=run(3,level=10,completed=fresh)
        faster=run(4,level=10,completed=fresh);faster['elapsed_ms']=900
        higher=run(5,level=11,completed=fresh)
        untimed=run(6,level=12,timed=False,completed=fresh)
        outsider=run(7,guild=1,level=13,completed=fresh)
        for i,r in enumerate((lower,equal,faster,higher,untimed,outsider)):
            repo.put(mplus_records.QUEUE+str(i),r)
        post=Mock(return_value=SimpleNamespace(message_id='123'))
        cfg={'bot_token':'test'}
        self.assertEqual(mplus_records.process(repo,cfg,'123',NOW,post=post)['record_alerts'],2)
        mplus_records.process(repo,cfg,'123',NOW,post=post)
        self.assertEqual(post.call_count,2)
        self.assertEqual(repo.get('RECORDS')['best'][best]['level'],11)
        payload=post.call_args.args[1]
        self.assertIn('Aster, Birch',payload['embeds'][0]['description'])
        self.assertNotIn('Cedar',payload['embeds'][0]['description'])
        self.assertEqual(payload['allowed_mentions'],{'parse':[]})
        self.assertEqual(mplus_records.timer(123456),'2:03.456')

    def test_record_baseline_is_quiet_and_historical_results_stay_quiet(self):
        repo=Repo();post=Mock()
        self.assertEqual(mplus_records.process(repo,{},'123',NOW,post=post)['skipped'],'warming_record_baseline')
        repo.put('COLLECTOR',{'roster_size':5,'record_baseline_profiles':5})
        baseline=run();repo.put('RUN#old',baseline)
        self.assertTrue(mplus_records.process(repo,{},'123',NOW,post=post)['baseline_ready'])
        late=run(2,level=15)
        repo.put(mplus_records.QUEUE+(NOW+timedelta(seconds=1)).isoformat()+'#2',late)
        mplus_records.process(repo,{},'123',NOW+timedelta(seconds=2),post=post)
        self.assertFalse(post.called)
        self.assertEqual(repo.get('RECORDS')['best'][mplus_records.record_key(late)]['level'],15)

    def test_record_ambiguous_send_is_not_retried(self):
        repo=Repo();repo.put('RECORDS',{'best':{},'since':(NOW-timedelta(days=2)).isoformat(),'cursor':mplus_records.QUEUE})
        r=run();repo.put(mplus_records.QUEUE+'1',r)
        post=Mock(side_effect=TimeoutError('ambiguous'))
        with self.assertRaises(TimeoutError):mplus_records.process(repo,{'bot_token':'test'},'123',NOW,post=post)
        self.assertEqual(repo.get('RECORD_POST#'+r['id'])['state'],'needs_review')
        mplus_records.process(repo,{'bot_token':'test'},'123',NOW,post=post)
        self.assertEqual(post.call_count,1)

    def test_collector_deduplicates_and_resumes_pending_character(self):
        repo=Repo()
        repo.put('SEASONS',{'at':NOW.isoformat(),'region':'us','items':SEASONS})
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
        repo.put('SEASONS',{'at':NOW.isoformat(),'region':'us','items':SEASONS})
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
        summary['season']=mplus.season_week(SEASONS,'us',START,END)
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

    def test_season_week_launch_current_and_rollover(self):
        start,end=mplus.week_window(datetime(2026,8,25,14,tzinfo=timezone.utc))
        self.assertEqual(mplus.season_week(SEASONS,'us',start,end)['week'],1)
        self.assertEqual(mplus.season_week(SEASONS,'us',START,END)['week'],4)
        special={**SEASONS[0],'slug':'season-special','is_main_season':False,
                 'starts':{'us':'2026-09-08T15:00:00Z'}}
        self.assertEqual(mplus.season_week(SEASONS+[special],'us',START,END)['week'],4)
        next_season={**SEASONS[0],'slug':'season-next','name':'MN Season 3',
                     'starts':{'us':'2026-09-08T15:00:00Z'}}
        self.assertEqual(mplus.season_week(SEASONS+[next_season],'us',START,END)['week'],1)
        with self.assertRaises(ValueError):mplus.season_week(SEASONS,'eu',START,END)
        s=mplus.summarize([run()],{},START,END);s['season']=mplus.season_week(SEASONS,'us',START,END)
        self.assertIn('Week #4',mplus_presentation.label(s))
        self.assertIn('Week #4',mplus_presentation.page(s,'Example Guild'))
        self.assertIn('Week #4',mplus_presentation.discord_post(s,'Example Guild','https://example.org')['embeds'][0]['title'])

    def test_season_metadata_collected_without_dungeon_payloads(self):
        repo=Repo();repo.put('ROSTER',{'at':NOW.isoformat(),'members':[mplus.person(PEOPLE[0])]})
        source=Mock(side_effect=[{'seasons':[{**SEASONS[0],'dungeons':['unneeded']} ]},{}])
        mplus_collect.collect(repo,{'guild_region':'us'},NOW,source=source)
        self.assertEqual(repo.get('SEASONS')['items'],SEASONS)

    def test_minimum_members_dedupe_and_counts(self):
        a=run();b=run(2,guild=1);c=run(3,guild=5);d=run(4,guild=5,timed=False)
        s=mplus.summarize([a,a,b,c,d],{},START,END)
        self.assertEqual(s['timed_count'],2)
        self.assertEqual(s['guild_count'],1)
        self.assertEqual(s['boards']['ten'][0]['value'],2)
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

    def test_ties_keep_rank(self):
        s=mplus.summarize([run(guild=5)],{},START,END)
        self.assertEqual([r['rank'] for r in s['boards']['ten']],list(range(1,6)))
        text=mplus_presentation.page(s,'Example Guild')
        self.assertIn('Ember',text)
        payload=mplus_presentation.discord_post(s,'Example Guild','https://example.org/recap/')
        self.assertEqual(len(payload['embeds'][0]['fields']),6)
        self.assertEqual(len(payload['embeds'][0]['fields'][3]['value'].splitlines()),3)
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
