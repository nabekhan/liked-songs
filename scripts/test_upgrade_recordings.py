import copy
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import upgrade_recordings as u

A='00000000-0000-4000-8000-000000000001'
B='00000000-0000-4000-8000-000000000002'
X='00000000-0000-4000-8000-000000000003'
CID='00000000-0000-4000-8000-000000000004'
CONFIG={'collection_id':CID,'owner':'test-owner','user_agent':'LikedSongsTest/1.0 (https://example.org)'}


def recording(identifier=A, **kwargs):
    row={'id':identifier,'title':'Starboy','length':230000,'video':False,'artist-credit':[{'name':'The Weeknd','joinphrase':'','artist':{'id':'artist'}}],'disambiguation':'','isrcs':[],'relations':[]}
    row.update(kwargs)
    return row


def richer():
    return recording(B, **{'length':230453,'disambiguation':'explicit','isrcs':['ISRC1'],'genres':[{'name':'pop'}],'tags':[{'name':'pop','count':1}],'relations':[{'target-type':'work','work':{'id':'work'}},{'target-type':'url','url':{'resource':'https://example.org'}}]})


class MatchingTests(unittest.TestCase):
    def test_title_words_are_not_content_version_labels(self):
        for title in ('Dance Yrself Clean','So Fresh, So Clean','Clean Up Woman','Come Clean','A Clean Break (Let’s Work)'):
            self.assertEqual(u.content_version(recording(title=title)),'unspecified')
        for title in ('Starboy (clean)','Starboy [clean version]'):
            self.assertEqual(u.content_version(recording(title=title)),'clean')
        self.assertEqual(u.content_version(recording(disambiguation='explicit')),'explicit')

    def test_sparse_starboy_can_choose_richer_explicit_main(self):
        self.assertTrue(u.improvement(recording(),richer()))

    def test_clean_target_and_explicit_downgrade_rejected(self):
        self.assertFalse(u.identity_match(recording(),recording(B,disambiguation='clean')))
        self.assertFalse(u.identity_match(recording(disambiguation='explicit'),recording(B)))

    def test_live_to_main_is_user_preference_and_live_targets_are_rejected(self):
        for old in (recording(disambiguation='live in mexico',length=None), recording(title='Starboy (Live)',length=350000),recording(relations=[{'target-type':'work','work':{'id':'work'},'attributes':['live']}])):
            self.assertTrue(u.improvement(old,richer()))
        self.assertFalse(u.identity_match(recording(),recording(B,title='Starboy (Live)')))
        self.assertFalse(u.identity_match(recording(),recording(B,relations=[{'target-type':'work','work':{'id':'work'},'attributes':['live']}])) )

    def test_unlabeled_recording_found_only_on_live_releases_is_not_a_target(self):
        live_releases=[{'release-group':{'secondary-types':['Live']}}]
        candidate=recording(B,releases=live_releases,isrcs=['SAME'])
        self.assertTrue(u.is_live(candidate))
        self.assertFalse(u.identity_match(recording(isrcs=['SAME']),candidate))
        self.assertFalse(u.is_live(recording(releases=live_releases+[{'release-group':{'secondary-types':[]}}])))

    def test_studio_session_date_and_location_are_valid_main_recordings(self):
        old=recording(title='’Round Midnight',length=361160,disambiguation='live, 1955-07-17: Newport Jazz Festival',relations=[{'target-type':'work','work':{'id':'work'},'attributes':['live','instrumental']}])
        new=dict(richer(),title='’Round Midnight',length=426640,disambiguation='studio, 1953-01-30: WOR Studio, New York, US')
        new['relations'][0]['attributes']=['cover','instrumental']
        self.assertTrue(u.improvement(old,new))
        for suffix in ('; alternate take','; remix','; instrumental'):
            self.assertFalse(u.identity_match(old,dict(new,disambiguation=new['disambiguation']+suffix)))

    def test_conflicting_live_relationships_do_not_relax_studio_identity(self):
        old=recording(disambiguation='studio, 1968-11-19: big band recording',length=550000,relations=[{'target-type':'work','work':{'id':'work'},'attributes':[]},{'target-type':'work','work':{'id':'work'},'attributes':['live']}])
        new=dict(richer(),disambiguation='studio, 1951-07-23: quintet recording',length=180000)
        self.assertFalse(u.main_replacement(old))
        self.assertFalse(u.identity_match(old,new))
        self.assertFalse(u.main_replacement(recording(relations=[{'target-type':'work','work':{'id':'work'},'attributes':['live']}])) )
        self.assertFalse(u.main_replacement(recording(disambiguation='album / 12″ single version')))
        self.assertFalse(u.main_replacement(recording(disambiguation='studio, version 12a take 9 edit 1')))

    def test_main_preference_does_not_select_obscure_stubs(self):
        old=recording(disambiguation='live',length=350000);new=richer()
        stats={A:{'total_listen_count':625098,'total_user_count':50000},B:{'total_listen_count':1,'total_user_count':1}}
        self.assertFalse(u.improvement(old,new,100,1,stats))
        self.assertFalse(u.preferred_appearances(old,new,{},stats))
        stats[B]={'total_listen_count':100000,'total_user_count':10000}
        self.assertTrue(u.improvement(old,new,100,44,stats))
        stats[B]={'total_listen_count':None,'total_user_count':None}
        self.assertFalse(u.improvement(old,new,100,44,stats))

    def test_metadata_gain_does_not_override_known_popularity_drop(self):
        old=recording();new=dict(richer(),disambiguation='')
        stats={A:{'total_listen_count':6508,'total_user_count':1000},B:{'total_listen_count':2295,'total_user_count':500}}
        self.assertFalse(u.improvement(old,new,10,20,stats))
        self.assertFalse(u.preferred_appearances(old,new,{A:dict(old,releases=[{}]*10)},stats))

    def test_popular_album_extended_version_is_not_swapped_for_shorter_less_known_cut(self):
        old=recording(title='Starboy (extended version)',disambiguation='explicit',length=313786)
        new=dict(richer(),length=247800)
        stats={A:{'total_listen_count':288540,'total_user_count':30000},B:{'total_listen_count':257200,'total_user_count':20000}}
        self.assertFalse(u.improvement(old,new,64,12,stats))
        stats[B]={'total_listen_count':500000,'total_user_count':50000}
        self.assertTrue(u.improvement(old,new,64,12,stats))

    def test_acoustic_and_edits_become_main_but_remixes_keep_their_identity(self):
        for title in ('Starboy (Acoustic)','Starboy (Unplugged)','Starboy (Radio Edit)','Starboy (Single Version)','Starboy (Extended Version)','Starboy - Live at Austin','Starboy – Single Version'):
            self.assertTrue(u.improvement(recording(title=title,length=350000),richer()))
        self.assertTrue(u.improvement(recording(disambiguation='radio edit'),richer()))
        for title in ('Starboy (Remix)','Starboy (Demo)','Starboy (instrumental)','Starboy (sped up)'):
            self.assertFalse(u.identity_match(recording(title=title),richer()))
        self.assertFalse(u.identity_match(recording(disambiguation='stereo'),recording(B,disambiguation='mono')))
        for suffix in ('Remix','Remastered','Cover'):
            self.assertFalse(u.identity_match(recording(title='Song ('+suffix+')'),recording(B,title='Song ('+suffix+')')))
            self.assertTrue(u.identity_match(recording(title='Song ('+suffix+')',isrcs=['SAME']),recording(B,title='Song ('+suffix+')',isrcs=['SAME'])))
        for suffix in ('Acoustic','Radio Edit','Extended Version','Edit'):
            self.assertFalse(u.identity_match(recording(title='Song ('+suffix+')',isrcs=['SAME']),recording(B,title='Song ('+suffix+')',isrcs=['SAME'])))
            self.assertFalse(u.identity_match(recording(title='Song',isrcs=['SAME']),recording(B,title='Song ('+suffix+')',isrcs=['SAME'])))

    def test_popularity_can_outweigh_fewer_releases_but_not_wrong_identity(self):
        old=recording();new=richer()
        stats={A:{'total_listen_count':705,'total_user_count':225},B:{'total_listen_count':128212,'total_user_count':11499}}
        self.assertTrue(u.popularity_advantage(old,new,stats))
        self.assertTrue(u.preferred_appearances(old,dict(new,releases=[{}]),{A:dict(old,releases=[{}]*100)},stats))
        self.assertGreater(u.candidate_rank(new,stats),u.candidate_rank(old,stats))
        self.assertFalse(u.improvement(old,dict(new,title='Other Song'),1,100,stats))
        self.assertFalse(u.popularity_advantage(old,new,{B:stats[B]}))
        documented_old=dict(new,id=A,annotation='Detailed annotation')
        self.assertTrue(u.improvement(documented_old,new,10,20,stats))
        self.assertFalse(u.improvement(documented_old,new,10,20,{}))
        self.assertFalse(u.preferred_appearances(new,dict(old,releases=[{}]*200),{B:dict(new,releases=[{}])},stats))

    def test_full_source_cache_older_than_live_scan_is_refetched(self):
        with tempfile.TemporaryDirectory() as t,patch.object(u,'CACHE',Path(t)):
            full=Path(t)/'full-recordings'/(A+'.json')
            response=u.request_cache_path('/ws/2/recording/'+A,{'inc':u.sync.METADATA_INCLUDES,'fmt':'json'})
            for path in (full,response):
                u.save(path,recording());os.utime(path,(1,1))
            fresh=dict(richer(),id=A);api=SimpleNamespace(request=Mock(return_value=fresh))
            self.assertEqual(u.full_record(api,A,fresh_after='2026-10-14T00:00:00+00:00'),fresh)
            api.request.assert_called_once()
            self.assertEqual(u.full_record(api,A,fresh_after='2026-10-14T00:00:00+00:00'),fresh)
            api.request.assert_called_once()

    def test_title_artist_credit_order_and_video_guard(self):
        self.assertFalse(u.identity_match(recording(),recording(B,title='Other Song')))
        self.assertFalse(u.identity_match(recording(),recording(B,**{'artist-credit':[{'artist':{'id':'different'}}]})))
        self.assertFalse(u.identity_match(recording(),recording(B,video=True)))
        old=recording(**{'artist-credit':[{'artist':{'id':i}} for i in ('artist','second','third')]})
        new=recording(B,**{'artist-credit':[{'artist':{'id':i}} for i in ('artist','third','second')]})
        self.assertFalse(u.identity_match(old,new))

    def test_duration_and_missing_evidence_guard(self):
        self.assertFalse(u.identity_match(recording(),recording(B,length=250000)))
        self.assertFalse(u.identity_match(recording(length=None),richer()))
        self.assertTrue(u.identity_match(recording(length=None,isrcs=['ISRC1']),richer()))

    def test_work_conflict_and_identifier_loss_rejected(self):
        self.assertFalse(u.improvement(recording(relations=[{'target-type':'work','work':{'id':'other-work'}}]),richer()))
        self.assertFalse(u.improvement(recording(isrcs=['ISRC1']),recording(B,genres=[{'name':'pop'}])))

    def test_release_count_uses_all_search_releases_and_explicit_total_when_available(self):
        self.assertEqual(u.release_count({'releases':[{}]*208}),208)
        self.assertEqual(u.release_count({'release-count':300,'releases':[{}]*25}),300)

    def test_fresh_source_metadata_can_cancel_stale_snapshot_replacement(self):
        with tempfile.TemporaryDirectory() as t, patch.object(u,'CACHE',Path(t)):
            old=recording();candidate=richer();fresh=copy.deepcopy(candidate);fresh['id']=A;fresh['annotation']='Additional verified source information'
            source={'collection_id':CID,'collection_owner':'test-owner','scanned_at':'2026-10-14T00:00:00+00:00','recordings':[old]}
            u.save(Path(t)/'search-candidates.json',{'source_scanned_at':'2026-10-14T00:00:00+00:00','complete_search':True,'query_fingerprint':u.query_fingerprint(source),'audited':1,'recordings':[dict(old,releases=[{}]),dict(candidate,releases=[{}]*100)]})
            with patch.object(u,'cached_request',side_effect=[fresh,candidate]) as fetch:
                u.plan(SimpleNamespace(request_count=0),source)
                self.assertEqual(fetch.call_count,2)
            result=json.loads((Path(t)/'plan.json').read_text())
            self.assertEqual(result['counts'],{'no_confident_improvement':1})

    def test_main_recording_does_not_move_to_less_widely_released_entry(self):
        old=recording();candidate=richer()
        evidence={A:dict(old,releases=[{'id':str(i)} for i in range(100)])}
        self.assertFalse(u.preferred_appearances(old,dict(candidate,releases=[{'id':'one'}]),evidence))
        self.assertTrue(u.preferred_appearances(old,dict(candidate,releases=[{'id':str(i)} for i in range(200)]),evidence))

    def test_ranked_selection_falls_back_after_work_conflict_and_stops_after_valid_match(self):
        with tempfile.TemporaryDirectory() as t,patch.object(u,'CACHE',Path(t)):
            old=recording(relations=[{'target-type':'work','work':{'id':'work'}}])
            good=dict(richer(),disambiguation='');bad=dict(richer(),id=X,relations=[{'target-type':'work','work':{'id':'different'}}])
            third=dict(richer(),id='00000000-0000-4000-8000-000000000005',disambiguation='')
            source={'collection_id':CID,'collection_owner':'test-owner','scanned_at':'2026-10-14T00:00:00+00:00','recordings':[old]}
            rows=[dict(old,releases=[{}]),dict(bad,releases=[{}]*100),dict(good,releases=[{}]*80),dict(third,releases=[{}]*70)]
            u.save(Path(t)/'search-candidates.json',{'source_scanned_at':source['scanned_at'],'complete_search':True,'query_fingerprint':u.query_fingerprint(source),'audited':1,'recordings':rows})
            u.save(Path(t)/'full-recordings'/(A+'.json'),old)
            with patch.object(u,'cached_request',side_effect=[bad,good]) as fetch:
                u.plan(SimpleNamespace(request_count=0),source)
            result=json.loads((Path(t)/'plan.json').read_text())
            self.assertEqual(result['decisions'][0]['replacement']['id'],B)
            self.assertEqual(fetch.call_count,2)

    def test_batch_enrichment_uses_small_release_instead_of_large_box_set(self):
        with tempfile.TemporaryDirectory() as t,patch.object(u,'CACHE',Path(t)):
            second='00000000-0000-4000-8000-000000000005'
            old1=recording();old2=recording(X,title='Other Song')
            releases=[{'id':'large-box','track-count':1000},{'id':'small-release','track-count':2}]
            new1=dict(richer(),releases=releases);new2=dict(richer(),id=second,title='Other Song',releases=releases)
            source={'scanned_at':'2026-10-14T00:00:00+00:00','recordings':[old1,old2],'latest_live_ids':[A,X]}
            u.save(Path(t)/'search-candidates.json',{'complete_search':True,'query_fingerprint':u.query_fingerprint(source),'recordings':[old1,old2,new1,new2]})
            u.save(Path(t)/'full-collection-metadata-scan.json',{'source_scanned_at':source['scanned_at']})
            for row in (old1,old2):u.save(Path(t)/'full-recordings'/(row['id']+'.json'),row)
            response={'recording-count':2,'recordings':[new1,new2]}
            with patch.object(u,'cached_request',return_value=response) as fetch:
                u.enrich(SimpleNamespace(config=CONFIG,request_count=0),source)
            self.assertEqual(fetch.call_count,1)
            self.assertEqual(fetch.call_args.args[2]['release'],'small-release')
            self.assertTrue((Path(t)/'full-recordings'/(B+'.json')).exists())
            self.assertTrue((Path(t)/'full-recordings'/(second+'.json')).exists())

    def test_overlapping_search_pages_are_repaired_with_disjoint_mbid_ranges(self):
        with tempfile.TemporaryDirectory() as t,patch.object(u,'CACHE',Path(t)):
            lower=[recording(str(u.uuid.UUID(int=i))) for i in range(1,76)]
            upper=[recording(str(u.uuid.UUID(int=(1<<127)+i))) for i in range(1,76)]
            all_rows=lower+upper
            def response(api,path,params):
                query=params['query']
                if 'rid:[' in query:
                    bounds=query.split('rid:[',1)[1].split(']',1)[0].split(' TO ')
                    lo,hi=(u.uuid.UUID(v).int for v in bounds)
                    rows=[r for r in all_rows if lo<=u.uuid.UUID(r['id']).int<=hi]
                    return {'count':len(rows),'offset':0,'recordings':rows}
                # The second page repeats 25 IDs and omits 25 others.
                rows=lower+upper[:25] if params['offset']==0 else lower[:25]+upper[25:50]
                return {'count':150,'offset':params['offset'],'recordings':rows}
            source={'scanned_at':'snapshot','recordings':[recording()]}
            with patch.object(u,'cached_request',side_effect=response) as fetch:
                u.search(SimpleNamespace(request_count=0),source)
            result=json.loads((Path(t)/'search-candidates.json').read_text())
            self.assertEqual({r['id'] for r in result['recordings']},{r['id'] for r in all_rows})
            self.assertTrue(result['complete_search'])
            self.assertEqual(result['search_validation']['repaired_queries'],1)
            self.assertEqual(fetch.call_count,4)


class ApplicationTests(unittest.TestCase):
    def prepare(self,root):
        u.save(root/'plan.json',{'collection_id':CID,'owner':'test-owner','policy_sha256':u.SCRIPT_SHA256,'decisions':[{'outcome':'replace','original':{'id':A},'replacement':{'id':B}}]})

    def membership(self,ids):
        return {},[{'id':i} for i in ids],set(ids)

    def test_older_policy_plan_is_rejected_before_any_collection_access(self):
        with tempfile.TemporaryDirectory() as t,patch.object(u,'CACHE',Path(t)):
            self.prepare(Path(t));p=json.loads((Path(t)/'plan.json').read_text())
            p['policy_sha256']='older-code';u.save(Path(t)/'plan.json',p)
            with patch.object(u,'live_membership') as read,patch.object(u,'write_collection') as write:
                with self.assertRaisesRegex(RuntimeError,'older audit rules'):u.apply_plan(None,CONFIG)
                read.assert_not_called();write.assert_not_called()

    def test_targets_are_added_and_verified_before_sources_are_removed(self):
        with tempfile.TemporaryDirectory() as t,patch.object(u,'CACHE',Path(t)):
            self.prepare(Path(t));tracker=Mock()
            reads=Mock(side_effect=[self.membership([A,X]),self.membership([A,B,X]),self.membership([B,X])]);writes=Mock()
            tracker.attach_mock(reads,'read');tracker.attach_mock(writes,'write')
            with patch.object(u,'live_membership',reads),patch.object(u,'write_collection',writes):
                result=u.apply_plan(None,CONFIG)
            self.assertEqual([c[0] for c in tracker.mock_calls],['read','write','read','write','read'])
            self.assertEqual(writes.call_args_list[0].args[2:],('PUT',[B]))
            self.assertEqual(writes.call_args_list[1].args[2:],('DELETE',[A]))
            self.assertEqual(result['after_ids'],[B,X]);self.assertEqual(result['stage'],'verified')

    def test_missing_target_prevents_any_source_removal(self):
        with tempfile.TemporaryDirectory() as t,patch.object(u,'CACHE',Path(t)):
            self.prepare(Path(t))
            with patch.object(u,'live_membership',side_effect=[self.membership([A,X]),self.membership([A,X])]),patch.object(u,'write_collection') as write:
                with self.assertRaisesRegex(RuntimeError,'original likes have not been removed'):u.apply_plan(None,CONFIG)
                self.assertEqual([c.args[2] for c in write.call_args_list],['PUT'])

    def test_user_removed_source_is_not_reintroduced(self):
        with tempfile.TemporaryDirectory() as t,patch.object(u,'CACHE',Path(t)):
            self.prepare(Path(t))
            with patch.object(u,'live_membership',side_effect=[self.membership([X]),self.membership([X]),self.membership([X])]),patch.object(u,'write_collection') as write:
                result=u.apply_plan(None,CONFIG)
                write.assert_not_called();self.assertEqual(result['skipped_no_longer_liked'],[A])

    def test_collection_scope_mismatch_blocks_all_network_calls(self):
        with tempfile.TemporaryDirectory() as t,patch.object(u,'CACHE',Path(t)):
            self.prepare(Path(t))
            with patch.object(u,'live_membership') as read:
                with self.assertRaisesRegex(RuntimeError,'owner mismatch'):u.apply_plan(None,dict(CONFIG,owner='other-owner'))
                read.assert_not_called()

    def test_resume_reconciles_successful_write_before_journal_checkpoint(self):
        with tempfile.TemporaryDirectory() as t,patch.object(u,'CACHE',Path(t)):
            root=Path(t);self.prepare(root)
            plan={A:B}
            import hashlib
            signature=hashlib.sha256(json.dumps(sorted(plan.items())).encode()).hexdigest()
            journal={'collection_id':CID,'owner':CONFIG['owner'],'plan_signature':signature,'stage':'prepared','before_ids':[A,X],'replacements':plan,'skipped_no_longer_liked':[],'added_batches':[],'removed_batches':[]}
            u.save(root/'application.json',journal)
            with patch.object(u,'live_membership',side_effect=[self.membership([A,B,X]),self.membership([A,B,X]),self.membership([B,X])]),patch.object(u,'write_collection') as write:
                result=u.apply_plan(None,CONFIG)
                self.assertEqual([c.args[2] for c in write.call_args_list],['DELETE'])
                self.assertEqual(result['stage'],'verified')
            u.save(root/'application.json',journal)
            with patch.object(u,'live_membership',side_effect=[self.membership([A,X]),self.membership([A,B,X]),self.membership([B,X])]),patch.object(u,'write_collection') as write:
                result=u.apply_plan(None,CONFIG)
                self.assertEqual([c.args[2] for c in write.call_args_list],['PUT','DELETE'])
                self.assertEqual(result['stage'],'verified')
            journal.update(stage='targets_verified');u.save(root/'application.json',journal)
            with patch.object(u,'live_membership',side_effect=[self.membership([B,X]),self.membership([B,X])]),patch.object(u,'write_collection') as write:
                result=u.apply_plan(None,CONFIG);write.assert_not_called();self.assertEqual(result['stage'],'verified')

    def test_resume_rechecks_owner_and_missing_targets_before_deletion(self):
        with tempfile.TemporaryDirectory() as t,patch.object(u,'CACHE',Path(t)):
            root=Path(t);self.prepare(root)
            import hashlib
            signature=hashlib.sha256(json.dumps([(A,B)]).encode()).hexdigest()
            journal={'collection_id':CID,'owner':'other','plan_signature':signature,'stage':'targets_verified','before_ids':[A,X],'replacements':{A:B},'skipped_no_longer_liked':[],'added_batches':[],'removed_batches':[]}
            u.save(root/'application.json',journal)
            with patch.object(u,'live_membership') as read:
                with self.assertRaisesRegex(RuntimeError,'different application journal'):u.apply_plan(None,CONFIG)
                read.assert_not_called()
            journal['owner']=CONFIG['owner'];u.save(root/'application.json',journal)
            with patch.object(u,'live_membership',return_value=self.membership([A,X])),patch.object(u,'write_collection') as write:
                with self.assertRaisesRegex(RuntimeError,'disappeared'):u.apply_plan(None,CONFIG)
                write.assert_not_called()

    def test_lost_target_restores_original_like_instead_of_leaving_both_absent(self):
        with tempfile.TemporaryDirectory() as t,patch.object(u,'CACHE',Path(t)):
            self.prepare(Path(t))
            with patch.object(u,'live_membership',side_effect=[self.membership([A,X]),self.membership([A,B,X]),self.membership([X]),self.membership([A,X])]),patch.object(u,'write_collection') as write:
                with self.assertRaisesRegex(RuntimeError,'original likes were restored'):u.apply_plan(None,CONFIG)
                self.assertEqual([c.args[2:] for c in write.call_args_list],[('PUT',[B]),('DELETE',[A]),('PUT',[A])])
                self.assertEqual(json.loads((Path(t)/'application.json').read_text())['stage'],'recovered')

    def test_collection_write_retries_socket_read_timeout(self):
        response=io.BytesIO(b'<metadata><message><text>OK</text></message></metadata>');response.status=200
        api=SimpleNamespace(auth={'access_token':'test-token','expires_at':float('inf')},last_request=0,request_count=0)
        with patch.object(u.urllib.request,'urlopen',side_effect=[u.socket.timeout('read timed out'),response]) as send,patch.object(u.sync.time,'sleep'):
            u.write_collection(api,CONFIG,'PUT',[B])
        self.assertEqual(send.call_count,2)

    def test_collection_write_has_user_agent_and_safe_uuid_path(self):
        response=io.BytesIO(b'<metadata><message><text>OK</text></message></metadata>');response.status=200
        api=SimpleNamespace(auth={'access_token':'test-token','expires_at':float('inf')},last_request=0,request_count=0)
        with patch.object(u.urllib.request,'urlopen',return_value=response) as send,patch.object(u.sync.time,'sleep'):
            u.write_collection(api,CONFIG,'PUT',[B])
        request=send.call_args.args[0]
        self.assertEqual(request.method,'PUT');self.assertEqual(request.get_header('User-agent'),CONFIG['user_agent'])
        self.assertIn('/collection/'+CID+'/recordings/'+B,request.full_url)
        with self.assertRaises(ValueError):u.write_collection(api,CONFIG,'PUT',['not-an-id'])


if __name__=='__main__':unittest.main()
