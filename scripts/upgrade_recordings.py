#!/usr/bin/env python3
"""Audit sparse collection entries and replace only well-supported matches."""
import argparse
from collections import Counter
import csv
import datetime as dt
import gzip
import hashlib
import heapq
import io
import json
import math
from pathlib import Path
import re
import socket
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET

import sync

CACHE = sync.ROOT / '.cache/recording-audit'
SCRIPT_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
REMASTER_LABEL = re.compile(r'\b(?:(?:19|20)\d{2}[\s-]+)?(?:digital(?:ly)?[\s-]+)?re[- ]?master(?:ed|ing)?(?:\s+(?:version|edition))?(?:\s+(?:in\s+)?(?:19|20)\d{2})?(?:\s+(?:version|edition))?\b',re.I)


def save(path, data):
    sync.atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def request_cache_path(path,params):
    key = hashlib.sha256(json.dumps([path, params], sort_keys=True).encode()).hexdigest()
    return CACHE / 'requests' / (key + '.json')


def cached_request(api, path, params):
    dest = request_cache_path(path,params)
    if dest.exists(): return json.loads(dest.read_text())
    result = api.request(path, params)
    save(dest, result)
    return result


def artist_ids(row):
    return [c['artist']['id'] for c in row.get('artist-credit', []) if c.get('artist', {}).get('id')]


def works(row):
    return {r['work']['id'] for r in row.get('relations', []) if r.get('target-type') == 'work' and r.get('work')}


def content_version(row):
    # Song titles such as "Dance Yrself Clean" are not version labels.
    qualifiers = [row.get('disambiguation', '')] + re.findall(r'[\[(]([^\])]+)[\])]', row.get('title',''))
    text = ','.join(qualifiers)
    if re.search(r'(?:^|[,;/])\s*(?:clean|censored|edited for content)(?:\s+(?:album\s+)?(?:version|edit|lyrics))?\s*(?:$|[,;/])', text, re.I): return 'clean'
    if re.search(r'(?:^|[,;/])\s*(?:explicit|uncensored)(?:\s+(?:album\s+)?(?:version|edit|lyrics))?\s*(?:$|[,;/])', text, re.I): return 'explicit'
    return 'unspecified'


def normalized(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', value).casefold() if c.isalnum())


def version_suffix(title):
    return re.search(r'\s[-–—]\s((?:live(?:$|\s(?:at|in|on|\d))|acoustic\b|unplugged\b|radio\s+(?:edit|version|mix)\b|single\s+(?:version|edit|mix)\b|extended\s+(?:version|mix|edit)\b).*)$',title,re.I)


def version_labels(row):
    labels=[row.get('disambiguation','')]+re.findall(r'[\[(]([^\])]+)[\])]',row.get('title',''))
    suffix=version_suffix(row.get('title',''))
    if suffix:labels.append(suffix.group(1))
    return labels


def is_live(row):
    labels = version_labels(row)
    release_groups=[r.get('release-group',{}) for r in row.get('releases',[])]
    exclusively_live_releases=bool(release_groups) and all('Live' in group.get('secondary-types',[]) for group in release_groups)
    return (any(re.search(r'\blive\b', label, re.I) for label in labels)
            or any('live' in r.get('attributes',[]) for r in row.get('relations',[]) if r.get('target-type')=='work')
            or exclusively_live_releases)


def is_acoustic(row):
    labels=version_labels(row)
    return any(re.search(r'\b(?:acoustic|unplugged)\b',label,re.I) for label in labels) or any('acoustic' in r.get('attributes',[]) for r in row.get('relations',[]) if r.get('target-type')=='work')


def main_substitution(row):
    return (is_acoustic(row) or is_live(row) or is_edit(row)) and not is_remix(row)


def main_replacement(row):
    """Require a clear version label before changing performance or duration.

    Search stays broad, but a stray/mixed live work relationship is not enough
    to reinterpret an otherwise ordinary studio recording as a live version.
    """
    if not main_substitution(row):return False
    labels=version_labels(row)
    if any(re.match(r'^studio\b',label,re.I) for label in labels):return False
    if any(re.search(r'\b(?:acoustic|unplugged)\b',label,re.I) for label in labels):return True
    if is_edit(row):
        album_label=any(re.search(r'\balbum\b',label,re.I) for label in labels)
        definite_edit=any(re.search(r'\b(?:edit|edited|extended|shortened)\b',label,re.I) for label in labels)
        return not album_label or definite_edit
    return any(re.search(r'\blive\b',label,re.I) for label in labels)


def is_edit(row):
    labels=version_labels(row)
    return any(re.search(r'\b(?:edit|edited|extended|shortened|(?:radio|single|7[″\"]?)\s+(?:version|mix))\b',label,re.I) for label in labels)


def is_remix(row):
    labels = [without_remaster(label) for label in version_labels(row)]
    # A generic 7-inch/radio/single mix is an edit; a named mix keeps its identity.
    labels = [label for label in labels if not re.fullmatch(r'\s*(?:radio|single|7[″\"]?)\s+mix\s*',label,re.I)]
    return any(re.search(r'\b(?:remix|mix)\b', label, re.I) for label in labels)


def without_remaster(value):
    return REMASTER_LABEL.sub('',value).strip(' ,;:/-–—')


def search_title(row):
    title = row.get('title', '')
    def clean_qualifier(match):
        if not REMASTER_LABEL.search(match.group(2)):return match.group(0)
        remaining=without_remaster(match.group(2))
        return match.group(1)+remaining+match.group(3) if remaining else ' '
    title=re.sub(r'([\[(])([^\])]+)([\])])',clean_qualifier,title)
    suffix=re.search(r'\s+[-–—]\s+(.+)$',title)
    if suffix and REMASTER_LABEL.search(suffix.group(1)):
        remaining=without_remaster(suffix.group(1))
        title=title[:suffix.start()]+(' - '+remaining if remaining else '')
    title = re.sub(r'\s*[\[(](?:explicit|clean|uncensored|censored)(?: version)?[\])]\s*', ' ', title, flags=re.I)
    title = re.sub(r'\s*[\[(](?:feat\.?|featuring)\s+[^\])]+[\])]\s*', ' ', title, flags=re.I)
    if main_substitution(row):
        suffix=version_suffix(title)
        if suffix:title=title[:suffix.start()]
        title = re.sub(r'\s*[\[(]live\b[^\])]*[\])]\s*', ' ', title, flags=re.I)
        title = re.sub(r'\s*[\[(](?:acoustic|unplugged)\b[^\])]*[\])]\s*', ' ', title, flags=re.I)
        title = re.sub(r'\s*[\[(](?:radio|single|album|extended|shortened|edit|7[″\"]?)\b[^\])]*[\])]\s*', ' ', title, flags=re.I)
    return title.strip()


def base_title(row):
    return normalized(search_title(row))


def sparse(row):
    return not row.get('isrcs') or (not works(row) and not (row.get('genres') or row.get('tags')))


def audit_rows(source):
    primary=[r for r in source['recordings'] if sparse(r) or content_version(r)=='clean']
    selected={r['id'] for r in primary}
    # Append extra live entries so previously cached search batches stay valid.
    return primary+[r for r in source['recordings'] if r['id'] not in selected and (is_live(r) or is_acoustic(r) or is_edit(r))]


def version_context(row):
    text = without_remaster(row.get('disambiguation', '')).casefold()
    # A studio session date/location describes the recording; it is not an
    # alternate mix. Keep actual mix/take/other variant labels restrictive.
    if re.match(r'^studio\b',text) and not specific_variant(row,include_performance=False):return ''
    text = re.sub(r'\b(?:explicit|clean|uncensored|censored|unedited)(?:\s+version)?\b', '', text)
    text = re.sub(r'\b(?:original\s+)?(?:album|studio)\s+version\b', '', text)
    return normalized(text)


def specific_variant(row,include_performance=True):
    labels=[without_remaster(label) for label in version_labels(row)]
    labels=[re.sub(r'\b(?:explicit|clean|censored|uncensored)(?:\s+version)?\b','',label,flags=re.I) for label in labels]
    labels=[re.sub(r'\b(?:original\s+)?(?:album|studio)\s+version\b','',label,flags=re.I) for label in labels]
    pattern=r'\b(?:remix|mix|acoustic|demo|instrumental|karaoke|edit|extended|version|cover|sped|slowed|nightcore|mono|stereo|alternate|take)\b'
    return any(re.search(pattern,label,re.I) for label in labels) or (include_performance and any(set(r.get('attributes',[]))&{'instrumental','partial','medley'} for r in row.get('relations',[]) if r.get('target-type')=='work'))


def identity_match(source, candidate):
    if source['id'] == candidate['id']: return False
    if base_title(source) != base_title(candidate): return False
    old_artists, new_artists = artist_ids(source), artist_ids(candidate)
    if not old_artists or not new_artists or old_artists[0] != new_artists[0]: return False
    if not set(old_artists) <= set(new_artists): return False
    if [a for a in new_artists if a in old_artists] != old_artists: return False
    if bool(source.get('video')) != bool(candidate.get('video')): return False
    # The user explicitly prefers the main studio record over accidental live
    # likes. Remix identities remain separate and never lose their mix label.
    if is_live(candidate) or is_acoustic(candidate) or is_edit(candidate): return False
    to_main = main_replacement(source)
    if to_main:
        if version_context(candidate) or is_remix(candidate): return False
    elif version_context(source) != version_context(candidate): return False
    if content_version(candidate) == 'clean': return False
    if content_version(source) in ('explicit', 'clean') and content_version(candidate) != 'explicit': return False
    a, b = source.get('length'), candidate.get('length')
    shared_isrc = set(source.get('isrcs', [])) & set(candidate.get('isrcs', []))
    if to_main: return True
    # Generic labels such as "Remix" or "Acoustic" do not identify a specific
    # performance. Keep these likes unless an identifier supports the match.
    if (specific_variant(source) or specific_variant(candidate)) and not shared_isrc:return False
    if not a or not b: return bool(shared_isrc)
    return abs(a-b) <= min(3000, max(1000, a * .015))


def quality(row):
    relations = row.get('relations', [])
    return (4 * bool(row.get('isrcs')) + 4 * bool(works(row))
            + 3 * any(r.get('target-type') == 'url' for r in relations)
            + 2 * bool(row.get('genres')) + bool(row.get('tags'))
            + min(3, sum(r.get('target-type') == 'artist' for r in relations) * .3)
            + bool(row.get('first-release-date')) + bool(row.get('length'))
            + bool(row.get('annotation')))


def improvement(source, candidate, source_appearances=None, candidate_appearances=None, popularity=None):
    if not identity_match(source, candidate): return False
    old_works, new_works = works(source), works(candidate)
    if old_works and not old_works & new_works: return False
    if source.get('isrcs') and not candidate.get('isrcs'): return False
    if not popularity_compatible(source,candidate,popularity or {},candidate_appearances):return False
    delta = quality(candidate) - quality(source)
    if main_replacement(source):return quality(candidate)>=8
    prefers_explicit = content_version(candidate) == 'explicit' and content_version(source) != 'explicit'
    much_more_widely_released=(source_appearances is not None and candidate_appearances is not None
                              and candidate_appearances>=max(5,source_appearances*3))
    popular = popularity_advantage(source,candidate,popularity or {})
    return (delta >= 3 or (prefers_explicit and delta >= 0)
            or (much_more_widely_released and delta>=0)
            or (popular and quality(candidate)>=8 and delta>=-3))


def popularity_compatible(source,candidate,popularity,candidate_appearances=None):
    if not popularity and candidate_appearances is None:return True
    old=popularity.get(source['id'],{}).get('total_listen_count')
    new=popularity.get(candidate['id'],{}).get('total_listen_count')
    if main_replacement(source):
        if old is not None and old>=100:
            performance=any(re.search(r'\b(?:live|acoustic|unplugged)\b',label,re.I) for label in version_labels(source))
            explicit=content_version(candidate)=='explicit' and content_version(source)!='explicit'
            divisor=10 if performance else (3 if explicit else 1)
            return new is not None and new>=max(100,old/divisor)
        return (new is not None and new>=100) or (candidate_appearances is not None and candidate_appearances>=5)
    if old is None or old<100:return True
    if new is None:return False
    explicit=content_version(candidate)=='explicit' and content_version(source)!='explicit'
    return new>=old/(3 if explicit else 1)


def popularity_advantage(source,candidate,popularity):
    old=popularity.get(source['id'],{});new=popularity.get(candidate['id'],{})
    a,b=old.get('total_listen_count'),new.get('total_listen_count')
    return a is not None and b is not None and b>=max(100,a*3) and (new.get('total_user_count') or 0)>=max(10,(old.get('total_user_count') or 0)*2)


def candidate_rank(row,popularity):
    stats=popularity.get(row['id'],{})
    return (content_version(row)=='explicit',stats.get('total_listen_count') or 0,stats.get('total_user_count') or 0,release_count(row),search_quality(row),row['id'])


def read_popularity():
    path=CACHE/'popularity.json'
    return json.loads(path.read_text())['recordings'] if path.exists() else {}


def search_quality(row):
    count = row.get('release-count', len(row.get('releases', [])))
    return (4 * bool(row.get('isrcs')) + 2 * bool(row.get('tags'))
            + 2 * (content_version(row) == 'explicit')
            + math.log2(1 + int(count or 0)))


def release_count(row):
    if row.get('release-count') is not None:return int(row['release-count'])
    releases=row.get('releases',[])
    return len({r['id'] for r in releases}) if releases and all('id' in r for r in releases) else len(releases)


def preferred_appearances(source,candidate,search_by_id,popularity=None):
    if not popularity_compatible(source,candidate,popularity or {},release_count(candidate)):return False
    if main_substitution(source) or content_version(source)=='clean':return True
    if popularity_advantage(candidate,source,popularity or {}) and not (content_version(candidate)=='explicit' and content_version(source)!='explicit'):return False
    if popularity_advantage(source,candidate,popularity or {}):return True
    original=search_by_id.get(source['id'])
    return original is not None and release_count(candidate)>=release_count(original)


def label(row):
    return {'id':row['id'], 'title':row['title'], 'artist':''.join(c.get('name', c.get('artist',{}).get('name','')) + c.get('joinphrase','') for c in row.get('artist-credit',[])), 'length_ms':row.get('length'), 'version':row.get('disambiguation',''), 'isrcs':row.get('isrcs',[]), 'works':sorted(works(row)), 'genres':[g['name'] for g in row.get('genres',[])], 'quality':quality(row), 'url':'https://musicbrainz.org/recording/' + row['id']}


def scan(api, config):
    destination = CACHE / 'source.json'
    if destination.exists():
        data = json.loads(destination.read_text())
        if data['collection_id'] != config['collection_id']: raise RuntimeError('Audit collection mismatch.')
        print('Using retained audit metadata.', flush=True)
        return data
    with gzip.open(CACHE / 'original-collection.json.gz', 'rt', encoding='utf-8') as source:
        original = json.load(source)
    if original['collection_id'] != config['collection_id'] or original['collection_owner'] != config['owner']:
        raise RuntimeError('Original snapshot belongs to another collection.')
    metadata, membership = sync.read_collection(api, config, full_metadata=False)
    previous = {r['id']: r for r in original['recordings']}
    rows = []
    for row in membership:
        if row['id'] in previous: rows.append(previous[row['id']])
        else: rows.append(cached_request(api, '/ws/2/recording/' + row['id'], {'inc': sync.METADATA_INCLUDES, 'fmt': 'json'}))
    data = {'collection_id': config['collection_id'], 'collection_owner': config['owner'], 'scanned_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'metadata_refreshed_at': original['metadata_refreshed_at'], 'collection_metadata': metadata, 'recordings': rows}
    save(destination, data)
    save(CACHE / 'before-recording-ids.json', {'collection_id': config['collection_id'], 'scanned_at': data['scanned_at'], 'ids': sorted(r['id'] for r in rows)})
    print(json.dumps({'live_count':len(rows), 'sparse':sum(sparse(r) for r in rows), 'clean':sum(content_version(r)=='clean' for r in rows), 'api_requests':api.request_count}), flush=True)
    return data


def search_clause(row):
    ids = artist_ids(row)
    if not ids: return None
    title = re.sub(r'([+\-!(){}\[\]^"~*?:\\/])', r'\\\1', search_title(row))
    query = f'(arid:{ids[0]} AND recording:"{title}"'
    if row.get('length') and not main_substitution(row):
        query += f' AND dur:[{max(0,row["length"]-3000)} TO {row["length"]+3000}]'
    return query + ')'


def query_fingerprint(source):
    return hashlib.sha256(json.dumps([(r['id'],search_clause(r)) for r in audit_rows(source)]).encode()).hexdigest()


def partition_search(api,query,lower=0,upper=(1<<128)-1,initial=None):
    """Use disjoint MBID ranges when score-sorted result pages overlap."""
    if initial is None:
        bounded=f'({query}) AND rid:[{uuid.UUID(int=lower)} TO {uuid.UUID(int=upper)}]'
        initial=cached_request(api,'/ws/2/recording',{'query':bounded,'fmt':'json','limit':100,'offset':0})
    count=initial['count'];rows=initial.get('recordings',[])
    if count<=100:
        found={r['id']:r for r in rows}
        if len(found)!=count or any(not lower<=uuid.UUID(i).int<=upper for i in found):raise RuntimeError('Incomplete or invalid partitioned search results.')
        return found
    if lower==upper:raise RuntimeError('Recording search has duplicate index documents for one MBID.')
    middle=(lower+upper)//2
    left=partition_search(api,query,lower,middle)
    right=partition_search(api,query,middle+1,upper)
    return dict(left,**right)


def search(api, source):
    rows = audit_rows(source)
    clauses = [(r['id'],q) for r in rows if (q := search_clause(r))]
    candidates = {}
    truncated=[];validation={'queries_checked':0,'repaired_queries':0}
    def read_group(group):
        base = ' OR '.join(q for _,q in group)
        params={'query':base,'fmt':'json','limit':100,'offset':0}
        first=request_cache_path('/ws/2/recording',params)
        complete=False
        if first.exists():
            count=json.loads(first.read_text())['count']
            complete=count<=500 and all(request_cache_path('/ws/2/recording',dict(params,offset=i)).exists() for i in range(0,max(1,count),100))
        # Completed older cache batches remain reusable. New queries exclude
        # labeled live/clean targets, which can never win this user's policy.
        query=base if complete else '('+base+') AND NOT comment:live AND NOT comment:clean'
        offset = 0;found={};initial=None
        while True:
            result = cached_request(api, '/ws/2/recording', {'query':query, 'fmt':'json', 'limit':100, 'offset':offset})
            if initial is None:initial=result
            # MusicBrainz search exposes at most 500 hits per query. Split
            # broad batches instead of requesting an invalid offset.
            if not offset and result['count']>500 and len(group)>1:
                middle=len(group)//2
                read_group(group[:middle]);read_group(group[middle:]);return
            page = result.get('recordings', [])
            for row in page:found[row['id']]=row
            offset += len(page)
            if offset >= result['count']: break
            if not page: raise RuntimeError('Incomplete recording search pagination.')
            if offset>=500:
                break
        validation['queries_checked']+=1
        if len(found)!=initial['count']:
            validation['repaired_queries']+=1
            found=partition_search(api,query,initial=initial)
        candidates.update(found)
    for start in range(0, len(clauses), 15):
        read_group(clauses[start:start+15])
        save(CACHE/'search-progress.json',{'searched':min(start+15,len(clauses)),'total':len(clauses),'candidates':len(candidates),'api_requests':api.request_count})
        if start % 150 == 0: print(f'Searched {min(start+15,len(clauses))}/{len(clauses)} entries; {len(candidates)} candidates; {api.request_count} API requests.', flush=True)
    save(CACHE / 'search-candidates.json', {'source_scanned_at':source['scanned_at'], 'query_fingerprint':query_fingerprint(source),'complete_search':True,'search_validation':validation,'audited':len(rows), 'truncated_sources':truncated,'recordings':list(candidates.values())})
    print(json.dumps({'audited':len(rows), 'search_candidates':len(candidates), 'api_requests':api.request_count}), flush=True)


def full_record(api, identifier, fresh_after=None):
    path=CACHE/'full-recordings'/(identifier+'.json')
    cutoff=dt.datetime.fromisoformat(fresh_after).timestamp() if fresh_after else 0
    if path.exists() and path.stat().st_mtime>=cutoff:return json.loads(path.read_text())
    endpoint='/ws/2/recording/'+identifier;params={'inc':sync.METADATA_INCLUDES,'fmt':'json'}
    response_cache=request_cache_path(endpoint,params)
    if cutoff and response_cache.exists() and response_cache.stat().st_mtime<cutoff:
        row=api.request(endpoint,params);save(response_cache,row)
    else:row=cached_request(api,endpoint,params)
    if row['id']==identifier: save(path,row)
    return row


def shortlist(source,possible,search_by_id,popularity):
    ordered=sorted((r for r in possible if identity_match(source,r) and preferred_appearances(source,r,search_by_id,popularity)),key=lambda r:candidate_rank(r,popularity),reverse=True)
    return ordered[:3]


def fetch_popularity(config,source):
    searched=json.loads((CACHE/'search-candidates.json').read_text())
    ids=sorted({r['id'] for r in source['recordings']}|{r['id'] for r in searched['recordings']})
    existing=read_popularity();pending=[i for i in ids if i not in existing]
    for start in range(0,len(pending),1000):
        batch=pending[start:start+1000]
        for attempt in range(6):
            req=urllib.request.Request('https://api.listenbrainz.org/1/popularity/recording',data=json.dumps({'recording_mbids':batch}).encode(),headers={'User-Agent':config['user_agent'],'Content-Type':'application/json'})
            try:
                with urllib.request.urlopen(req,timeout=60) as response: result=json.load(response)
                break
            except urllib.error.HTTPError as exc:
                if exc.code not in (429,500,502,503,504) or attempt==5:raise
                sync.time.sleep(min(30,max(2,int(exc.headers.get('X-RateLimit-Reset-In','2')))))
        if not isinstance(result,list) or {r['recording_mbid'] for r in result}!=set(batch):raise RuntimeError('Incomplete ListenBrainz popularity response.')
        existing.update({r['recording_mbid']:r for r in result})
        save(CACHE/'popularity.json',{'fetched_at':dt.datetime.now(dt.timezone.utc).isoformat(),'source':'https://api.listenbrainz.org/1/popularity/recording','recordings':existing})
        print(f'Popularity: {min(start+1000,len(pending))}/{len(pending)} recordings.',flush=True)
        sync.time.sleep(1.1)


def enrich(api, source):
    searched=json.loads((CACHE/'search-candidates.json').read_text())
    if searched.get('query_fingerprint')!=query_fingerprint(source) or not searched.get('complete_search'):
        search(api,source);fetch_popularity(api.config,source)
        searched=json.loads((CACHE/'search-candidates.json').read_text())
    popularity=read_popularity()
    index={};by_id={r['id']:r for r in searched['recordings']}
    for row in searched['recordings']:
        artists=artist_ids(row)
        if artists:index.setdefault((artists[0],base_title(row)),[]).append(row)
    wanted=set()
    current=set(source.get('latest_live_ids',[]))
    for old in audit_rows(source):
        if current and old['id'] not in current:continue
        artists=artist_ids(old)
        possible=index.get((artists[0],base_title(old)),[]) if artists else []
        possible=shortlist(old,possible,by_id,popularity)
        if possible:wanted.add(old['id']);wanted.add(possible[0]['id'])
    # Read the audit's current originals together, rather than issuing one
    # lookup per liked recording. This is a one-time audit, not scheduled polling.
    marker=CACHE/'full-collection-metadata-scan.json'
    if not marker.exists() or json.loads(marker.read_text())['source_scanned_at']!=source['scanned_at']:
        _,current_rows=sync.read_collection(api,api.config,full_metadata=True)
        for row in current_rows:
            if row['id'] in wanted:save(CACHE/'full-recordings'/(row['id']+'.json'),row)
        save(marker,{'source_scanned_at':source['scanned_at'],'fetched_at':dt.datetime.now(dt.timezone.utc).isoformat(),'count':len(current_rows)})
    pending={i for i in wanted if not(CACHE/'full-recordings'/(i+'.json')).exists()}
    releases={};release_pages={}
    for identifier in pending:
        for release in by_id.get(identifier,{}).get('releases',[]):
            releases.setdefault(release['id'],set()).add(identifier)
            release_pages[release['id']]=max(release_pages.get(release['id'],1),math.ceil(int(release.get('track-count') or 100)/100))
    heap=[(-len(ids)/release_pages[rid],-len(ids),rid) for rid,ids in releases.items() if len(ids)>release_pages[rid] and len(ids)>=2];heapq.heapify(heap)
    browsed=0
    while heap:
        previous,_,rid=heapq.heappop(heap);coverage=releases[rid]&pending
        if len(coverage)<2 or len(coverage)<=release_pages[rid]:continue
        density=len(coverage)/release_pages[rid]
        if density<-previous:
            heapq.heappush(heap,(-density,-len(coverage),rid));continue
        offset=0;found=set()
        while True:
            result=cached_request(api,'/ws/2/recording',{'release':rid,'inc':sync.METADATA_INCLUDES,'fmt':'json','limit':100,'offset':offset})
            page=result.get('recordings',[])
            for row in page:
                if row['id'] in wanted:
                    save(CACHE/'full-recordings'/(row['id']+'.json'),row);found.add(row['id'])
            offset+=len(page)
            if offset>=result['recording-count']:break
            if not page:raise RuntimeError('Incomplete release browse.')
            remaining=coverage-found
            if not remaining or math.ceil((result['recording-count']-offset)/100)>=len(remaining):break
        pending-=found;browsed+=1
        if browsed%10==0:
            progress={'wanted':len(wanted),'enriched':len(wanted)-len(pending),'release_browses':browsed,'api_requests':api.request_count}
            save(CACHE/'enrichment-progress.json',progress);print(json.dumps(progress),flush=True)
    # Where several wanted recordings belong to one artist, a bounded browse
    # can avoid many individual lookups. Stop after the first page unless the
    # complete catalog is demonstrably cheaper than separate target requests.
    artists={}
    for identifier in pending:
        ids=artist_ids(by_id.get(identifier,{}))
        if ids:artists.setdefault(ids[0],set()).add(identifier)
    for artist,ids in sorted(artists.items(),key=lambda pair:len(pair[1]),reverse=True):
        ids &= pending
        if len(ids)<8:continue
        offset=0;total=None
        while True:
            result=cached_request(api,'/ws/2/recording',{'artist':artist,'inc':sync.METADATA_INCLUDES,'fmt':'json','limit':100,'offset':offset})
            page=result.get('recordings',[]);total=result['recording-count']
            for row in page:
                if row['id'] in wanted:
                    save(CACHE/'full-recordings'/(row['id']+'.json'),row);pending.discard(row['id'])
            offset+=len(page)
            if offset>=total:break
            if not page:raise RuntimeError('Incomplete artist browse.')
            if math.ceil((total-offset)/100)>=len(ids&pending)/2:break
        print(json.dumps({'artist_browse':artist,'remaining_individual_lookups':len(pending),'api_requests':api.request_count}),flush=True)
    save(CACHE/'enrichment-progress.json',{'wanted':len(wanted),'enriched':len(wanted)-len(pending),'release_browses':browsed,'remaining_individual_lookups':len(pending),'api_requests':api.request_count})
    print('Batch enrichment finished; remaining individual records will be fetched only when needed.',flush=True)


def reconcile(api,config,source):
    metadata,membership,ids=live_membership(api,config)
    known={r['id'] for r in source['recordings']}
    additions=[]
    for row in membership:
        if row['id'] not in known:additions.append(full_record(api,row['id']))
    source['recordings'].extend(additions)
    source.update(scanned_at=dt.datetime.now(dt.timezone.utc).isoformat(),collection_metadata=metadata,latest_live_ids=sorted(ids),membership_verified=True)
    save(CACHE/'source.json',source)
    save(CACHE/'before-recording-ids.json',{'collection_id':config['collection_id'],'scanned_at':source['scanned_at'],'ids':sorted(ids)})
    print(json.dumps({'live_count':len(ids),'new_entries_added_to_audit':len(additions),'historical_rows_no_longer_liked':len(known-ids)}),flush=True)


def plan(api, source):
    searched = json.loads((CACHE / 'search-candidates.json').read_text())
    if searched['source_scanned_at'] != source['scanned_at']: raise RuntimeError('Candidate search belongs to another scan.')
    if not searched.get('complete_search'):raise RuntimeError('Search result completeness has not been verified.')
    if searched.get('query_fingerprint') != query_fingerprint(source):raise RuntimeError('Candidate search does not cover the current matching rules.')
    if searched['audited']!=len(audit_rows(source)):raise RuntimeError('Candidate search does not cover the current audit policy.')
    popularity=read_popularity()
    originals = {r['id']:r for r in source['recordings']}
    full = dict(originals)
    search_by_id={r['id']:r for r in searched['recordings']}
    index = {}
    for row in searched['recordings']:
        artists = artist_ids(row)
        if artists: index.setdefault((artists[0], base_title(row)), []).append(row)
    decisions = []
    rows = audit_rows(source)
    for i, old in enumerate(rows):
        if source.get('latest_live_ids') and old['id'] not in source['latest_live_ids']:
            decisions.append({'original':label(old),'outcome':'no_longer_liked','considered':0});continue
        if old['id'] in searched.get('truncated_sources',[]):
            decisions.append({'original':label(old),'outcome':'search_limit_requires_review','considered':0});continue
        artists = artist_ids(old)
        possible = index.get((artists[0],base_title(old)), []) if artists else []
        possible = shortlist(old,possible,search_by_id,popularity)
        if possible:
            fresh=full_record(api,old['id'],fresh_after=source['scanned_at'])
            if fresh['id']!=old['id']:
                decisions.append({'original':label(old),'outcome':'source_merged','considered':len(possible)})
                continue
            old=fresh;originals[old['id']]=old;full[old['id']]=old
            possible=[r for r in possible if identity_match(old,r) and preferred_appearances(old,r,search_by_id,popularity)]
        evaluated = []
        source_appearances=release_count(search_by_id[old['id']]) if old['id'] in search_by_id else None
        for row in possible:
            record = full_record(api,row['id'])
            if record['id'] != row['id']: continue
            full[row['id']] = record
            candidate = full[row['id']]
            if improvement(old,candidate,source_appearances,release_count(row),popularity) and preferred_appearances(old,row,search_by_id,popularity):
                # Follow the user's popularity preference: select the first
                # compatible entry in rank order. Fetch lower-ranked versions
                # only when a preferred candidate fails full validation.
                evaluated.append(candidate);break
        evidence = {r['id']:r for r in possible}
        evaluated.sort(key=lambda r:candidate_rank(evidence.get(r['id'],r),popularity),reverse=True)
        original_label=dict(label(old),popularity=popularity.get(old['id'],{}),release_appearances=release_count(search_by_id[old['id']]) if old['id'] in search_by_id else None)
        decision = {'original':original_label, 'outcome':'no_confident_improvement', 'considered':len(possible)}
        if evaluated:
            best = evaluated[0]
            reason = 'main_studio_preference' if main_replacement(old) else ('more_popular_main_recording' if popularity_advantage(old,best,popularity) else ('explicit_preference' if content_version(best)=='explicit' and content_version(old)!='explicit' else ('main_recording_preference' if quality(best)-quality(old)<3 else 'better_documented_main_recording')))
            decision.update(outcome='replace', reason=reason, replacement=dict(label(best),popularity=popularity.get(best['id'],{}),release_appearances=release_count(evidence.get(best['id'],best))))
        if not old.get('length') and not old.get('isrcs') and not main_replacement(old): decision['outcome']='missing_identity_evidence'
        decisions.append(decision)
        if i % 100 == 0:
            counts = Counter(d['outcome'] for d in decisions)
            print(f'Evaluated {i+1}/{len(rows)}: {dict(counts)}; {api.request_count} API requests.',flush=True)
            save(CACHE / 'plan-progress.json', {'evaluated':i+1,'total':len(rows),'counts':dict(counts),'api_requests':api.request_count})
    selected = {d['original']['id']:d['replacement']['id'] for d in decisions if d['outcome']=='replace'}
    release_counts={r['id']:release_count(r) for r in searched['recordings']}
    for decision in decisions:
        if decision['outcome'] != 'replace': continue
        old_id = decision['original']['id']; target = selected[old_id]; seen = {old_id}
        while target in selected:
            if target in seen: raise RuntimeError('Replacement cycle detected.')
            seen.add(target); target = selected[target]
        source_count=release_count(search_by_id[old_id]) if old_id in search_by_id else None
        if improvement(originals[old_id], full[target],source_count,release_counts.get(target),popularity):
            decision['replacement'] = dict(label(full[target]),popularity=popularity.get(target,{}),release_appearances=release_counts.get(target,0))
        else: decision.update(outcome='ambiguous_chain')
    result = {'collection_id':source['collection_id'], 'owner':source['collection_owner'], 'source_scanned_at':source['scanned_at'], 'planned_at':dt.datetime.now(dt.timezone.utc).isoformat(), 'preference':'explicit', 'counts':dict(Counter(d['outcome'] for d in decisions)), 'decisions':decisions}
    result['preferences']={'main_studio':True,'avoid_versions':['live','acoustic','radio/single/extended edits'],'preserve_named_remixes':True,'ranking':['explicit','ListenBrainz total listens','ListenBrainz unique listeners','release appearances','metadata']}
    result['preferences']['ignore_remaster_labels']=True
    result['preferences']['popularity_safeguards']={'ordinary_or_edit_known_source':'no decrease in listens for sources with at least 100 listens','explicit_upgrade':'at least one third of source listens','clearly_labeled_live_or_acoustic_substitution':'at least 100 listens and one tenth of substantial source listens; otherwise 100 listens or five release appearances','relationship_only_version_flags':'insufficient to change performance or relax duration'}
    result['policy_sha256']=SCRIPT_SHA256
    save(CACHE / 'plan.json', result)
    save(CACHE / 'replacement-metadata.json', {d['replacement']['id']:full[d['replacement']['id']] for d in decisions if d['outcome']=='replace'})
    print(json.dumps(result['counts']),flush=True)


def write_collection(api, config, method, ids):
    if method not in ('PUT','DELETE') or not ids or len(ids)>100: raise ValueError('Invalid collection operation.')
    ids = [str(uuid.UUID(value)) for value in ids]
    cid = str(uuid.UUID(config['collection_id']))
    if not api.auth.get('access_token') or api.auth.get('expires_at',0) <= sync.time.time(): api.refresh()
    path = f'/ws/2/collection/{cid}/recordings/' + ';'.join(ids)
    url = sync.BASE + path + '?' + urllib.parse.urlencode({'client':'MusicBrainzLikedSongsAudit-1.0'})
    for attempt in range(6):
        sync.time.sleep(max(0,sync.random.uniform(1.1,3.0)-(sync.time.monotonic()-api.last_request)))
        headers = {'User-Agent':config['user_agent'],'Authorization':'Bearer '+api.auth['access_token'],'Accept':'application/xml'}
        api.last_request = sync.time.monotonic(); api.request_count += 1
        try:
            request = urllib.request.Request(url, headers=headers, method=method)
            with urllib.request.urlopen(request,timeout=45) as response:
                payload = response.read()
                if response.status not in (200,204): raise RuntimeError('Collection write was not acknowledged.')
            if payload:
                root = ET.fromstring(payload)
                messages = [n.text.strip() for n in root.iter() if n.tag.rsplit('}',1)[-1]=='text' and n.text]
                if 'OK' not in messages: raise RuntimeError('Collection response did not confirm success.')
            return
        except urllib.error.HTTPError as exc:
            if exc.code==401 and attempt==0:
                api.refresh(); continue
            if exc.code not in (429,500,502,503,504): raise RuntimeError(f'Collection {method} failed: HTTP {exc.code}.') from None
            delay=exc.headers.get('Retry-After','')
            sync.time.sleep(min(60,int(delay) if delay.isdigit() else 2**attempt))
        except (urllib.error.URLError,TimeoutError,socket.timeout): sync.time.sleep(min(30,2**attempt))
    raise RuntimeError(f'Collection {method} failed after retries.')


def chunks(values, size=100):
    values=sorted(values)
    for i in range(0,len(values),size): yield values[i:i+size]


def live_membership(api, config):
    metadata, rows = sync.read_collection(api,config,full_metadata=False)
    return metadata, rows, {r['id'] for r in rows}


def restore_sources(api,config,journal,current):
    required=set(journal['sources_to_restore'])
    for batch in chunks(required-current):write_collection(api,config,'PUT',batch)
    _,_,verified=live_membership(api,config)
    if not required<=verified:raise RuntimeError('Original-like recovery is incomplete; resume from the retained application journal.')
    journal['stage']='recovered';save(CACHE/'application.json',journal)
    raise RuntimeError('Replacement targets disappeared; affected original likes were restored and verified. Reconcile the plan before continuing.')


def apply_plan(api, config):
    result=json.loads((CACHE/'plan.json').read_text())
    if result['collection_id']!=config['collection_id'] or result['owner']!=config['owner']:
        raise RuntimeError('Plan collection or owner mismatch.')
    if result.get('policy_sha256')!=SCRIPT_SHA256:raise RuntimeError('Plan uses older audit rules; regenerate it before changing collection membership.')
    proposed={d['original']['id']:d['replacement']['id'] for d in result['decisions'] if d['outcome']=='replace'}
    signature=hashlib.sha256(json.dumps(sorted(proposed.items())).encode()).hexdigest()
    journal_path=CACHE/'application.json'
    resumed_current=None
    if journal_path.exists():
        journal=json.loads(journal_path.read_text())
        if journal['plan_signature']!=signature or journal['collection_id']!=config['collection_id'] or journal['owner']!=config['owner']:
            raise RuntimeError('A different application journal already exists.')
        if journal['stage']=='verified':
            print('This exact replacement plan was already applied and verified.'); return journal
        if journal['stage'] in ('prepared','targets_verified','recovery_required'):
            _,_,resumed_current=live_membership(api,config)
    else:
        metadata, rows, before=live_membership(api,config)
        active={old:new for old,new in proposed.items() if old in before}
        if set(active)&set(active.values()): raise RuntimeError('Unresolved replacement chain.')
        journal={'collection_id':config['collection_id'],'owner':config['owner'],'plan_signature':signature,'started_at':dt.datetime.now(dt.timezone.utc).isoformat(),'stage':'prepared','before_ids':sorted(before),'replacements':active,'skipped_no_longer_liked':sorted(set(proposed)-before),'added_batches':[],'removed_batches':[]}
        save(journal_path,journal)
    pairs=journal['replacements']; before=set(journal['before_ids']); targets=set(pairs.values())
    if journal['stage']=='recovery_required':restore_sources(api,config,journal,resumed_current)
    if journal['stage']=='recovered':raise RuntimeError('Original likes were recovered; reconcile this plan before continuing.')
    if journal['stage']=='prepared':
        for batch in chunks(targets-(resumed_current if resumed_current is not None else before)):
            if batch in journal['added_batches']: continue
            write_collection(api,config,'PUT',batch)
            journal['added_batches'].append(batch); save(journal_path,journal)
        journal['stage']='targets_added'; save(journal_path,journal)
    if journal['stage']=='targets_added':
        metadata, rows, current=live_membership(api,config)
        if not targets<=current: raise RuntimeError('Replacement targets missing; original likes have not been removed.')
        resumed_current=current
        journal['stage']='targets_verified'; save(journal_path,journal)
    if journal['stage']=='targets_verified':
        if resumed_current is not None and not targets<=resumed_current:
            raise RuntimeError('A replacement target disappeared; no further source removal is allowed.')
        pending=set(pairs) if resumed_current is None else set(pairs)&resumed_current
        for batch in chunks(pending):
            if batch in journal['removed_batches']: continue
            write_collection(api,config,'DELETE',batch)
            journal['removed_batches'].append(batch); save(journal_path,journal)
        journal['stage']='sources_removed'; save(journal_path,journal)
    if journal['stage']=='sources_removed':
        metadata, rows, after=live_membership(api,config)
        if not targets<=after:
            lost=targets-after
            restore={old for old,new in pairs.items() if new in lost and old not in after}
            journal.update(stage='recovery_required',missing_targets=sorted(lost),sources_to_restore=sorted(restore));save(journal_path,journal)
            restore_sources(api,config,journal,after)
        if set(pairs)&after: raise RuntimeError('Final source-removal verification failed; retain journal for recovery.')
        expected=(before-set(pairs))|targets
        journal.update(stage='verified',verified_at=dt.datetime.now(dt.timezone.utc).isoformat(),after_ids=sorted(after),concurrent_additions=sorted(after-expected),concurrent_removals=sorted(expected-after))
        save(journal_path,journal)
        save(CACHE/'after-membership.json',{'metadata':metadata,'recordings':rows})
    print(json.dumps({'stage':journal['stage'],'replacements':len(pairs),'before':len(before),'after':len(journal.get('after_ids',[])),'skipped_no_longer_liked':len(journal['skipped_no_longer_liked']),'concurrent_additions':len(journal.get('concurrent_additions',[])),'concurrent_removals':len(journal.get('concurrent_removals',[]))}),flush=True)
    return journal


def export_results(api, config, source):
    journal=json.loads((CACHE/'application.json').read_text())
    if journal['stage']!='verified': raise RuntimeError('Cannot export unverified collection changes.')
    after=json.loads((CACHE/'after-membership.json').read_text())
    known={r['id']:r for r in source['recordings']}
    known.update(json.loads((CACHE/'replacement-metadata.json').read_text()))
    rows=[]
    for row in after['recordings']:
        if row['id'] in known: rows.append(known[row['id']])
        else: rows.append(cached_request(api,'/ws/2/recording/'+row['id'],{'inc':sync.METADATA_INCLUDES,'fmt':'json'}))
    if {r['id'] for r in rows}!=set(journal['after_ids']): raise RuntimeError('Export does not match verified live collection.')
    metadata=after['metadata']
    sync.write_snapshot(metadata,sorted(rows,key=lambda r:r['id']),dict(config,metadata_refreshed_at=source['metadata_refreshed_at']))
    status={'checked_at':journal['verified_at'],'count':len(rows),'added':len(set(journal['after_ids'])-set(journal['before_ids'])),'removed':len(set(journal['before_ids'])-set(journal['after_ids'])),'metadata_refreshed':False,'metadata_fetched':len(set(journal['replacements'].values())-set(journal['before_ids'])),'metadata_reused':len(rows)-len(set(journal['replacements'].values())-set(journal['before_ids'])),'content_changed':True,'recording_replacements':len(journal['replacements'])}
    for path in (sync.ROOT/'data/sync-status.json',sync.ROOT/'site/sync-status.json'): save(path,status)
    result=json.loads((CACHE/'plan.json').read_text())
    report=dict(result,application={k:v for k,v in journal.items() if k not in ('before_ids','after_ids','added_batches','removed_batches')})
    save(sync.ROOT/'data/recording-replacement-audit.json',report)
    output=io.StringIO(newline='')
    fields=['title','artist','reason','original_recording_mbid','replacement_recording_mbid','original_listens','replacement_listens','original_listeners','replacement_listeners','original_release_appearances','replacement_release_appearances','original_version','replacement_version','original_length_ms','replacement_length_ms','original_url','replacement_url']
    writer=csv.DictWriter(output,fieldnames=fields);writer.writeheader()
    for decision in result['decisions']:
        old=decision['original']
        if old['id'] not in journal['replacements']:continue
        new=decision['replacement']
        values={'title':old['title'],'artist':old['artist'],'reason':decision['reason'],'original_recording_mbid':old['id'],'replacement_recording_mbid':new['id'],'original_release_appearances':old.get('release_appearances'),'replacement_release_appearances':new.get('release_appearances'),'original_version':old.get('version'),'replacement_version':new.get('version'),'original_length_ms':old.get('length_ms'),'replacement_length_ms':new.get('length_ms'),'original_url':old['url'],'replacement_url':new['url']}
        for prefix,row in (('original',old),('replacement',new)):
            values[prefix+'_listens']=row.get('popularity',{}).get('total_listen_count')
            values[prefix+'_listeners']=row.get('popularity',{}).get('total_user_count')
        writer.writerow({k:sync.csv_cell(v) for k,v in values.items()})
    sync.atomic_write(sync.ROOT/'data/recording-replacements.csv','\ufeff'+output.getvalue())
    review=io.StringIO(newline='')
    writer=csv.DictWriter(review,fieldnames=['title','artist','version','version_types','outcome','recording_mbid','url']);writer.writeheader()
    before={r['id']:r for r in source['recordings']}
    for decision in result['decisions']:
        old=decision['original'];row=before.get(old['id'],{})
        cached=CACHE/'full-recordings'/(old['id']+'.json')
        if cached.exists():row=json.loads(cached.read_text())
        live_label=any(re.search(r'\blive\b',label,re.I) for label in version_labels(row))
        types=[name for name,yes in [('edit',is_edit(row)),('live' if live_label else 'live metadata flag',is_live(row)),('acoustic',is_acoustic(row)),('clean',content_version(row)=='clean')] if yes]
        if old['id'] not in journal['after_ids'] or old['id'] in journal['replacements'] or not types:continue
        writer.writerow({k:sync.csv_cell(v) for k,v in {'title':old['title'],'artist':old['artist'],'version':old['version'],'version_types':'; '.join(types),'outcome':decision['outcome'],'recording_mbid':old['id'],'url':old['url']}.items()})
    sync.atomic_write(sync.ROOT/'data/recording-versions-to-review.csv','\ufeff'+review.getvalue())
    sync.atomic_gzip(sync.ROOT/'data/recording-replacement-backup.json.gz',json.dumps({'collection_id':journal['collection_id'],'before_ids':journal['before_ids'],'replacements':journal['replacements'],'started_at':journal['started_at']}))
    print('Exported verified collection and replacement audit; retained the two-month metadata freshness timestamp.',flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['bootstrap', 'scan', 'search','popularity','enrich','reconcile', 'plan','apply','export'])
    args = parser.parse_args()
    config = json.loads((sync.ROOT / 'config.json').read_text())
    if args.phase == 'bootstrap':
        with gzip.open(CACHE / 'original-collection.json.gz', 'rt', encoding='utf-8') as handle: original = json.load(handle)
        if original['collection_id'] != config['collection_id'] or original['collection_owner'] != config['owner']:
            raise RuntimeError('Original snapshot belongs to another collection.')
        data = {'collection_id':config['collection_id'], 'collection_owner':config['owner'], 'scanned_at':original['synced_at'], 'metadata_refreshed_at':original['metadata_refreshed_at'], 'collection_metadata':original['collection_metadata'], 'recordings':original['recordings'], 'membership_verified':False}
        save(CACHE / 'source.json', data)
        print('Initialized read-only audit from retained metadata; live membership will be revalidated before changes.')
        return
    api = sync.MusicBrainz(config)
    source = scan(api, config)
    if args.phase == 'search': search(api, source)
    if args.phase == 'popularity': fetch_popularity(config,source)
    if args.phase == 'enrich': enrich(api,source)
    if args.phase == 'reconcile': reconcile(api,config,source)
    if args.phase == 'plan': plan(api, source)
    if args.phase == 'apply': apply_plan(api,config)
    if args.phase == 'export': export_results(api,config,source)


if __name__ == '__main__':
    try: main()
    except Exception as exc:
        print(f'Audit stopped: {type(exc).__name__}: {exc}', file=sys.stderr)
        sys.exit(1)
