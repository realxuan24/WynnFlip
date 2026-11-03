"""WynnFlip V13.0: durable market observations and cached item baselines."""
import os, time, json, statistics, threading, secrets, hmac, math
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
import requests
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import FileResponse, JSONResponse
from scanner import build, extract_listing_page, load_profiles, number, identification_status, identity, exact_ids, normalized_name, profile_quality, roll_numbers
import scanner as _scanner
from qualification import qualify_items

def price_e(row):
    """WynnVentory listing_price is a per-item ask, not a stack total.

    Do not divide by amount; that is quantity, not a divisor for price.
    """
    p = number(row.get('listing_price'))
    return round(p) if p is not None and p > 0 else None

# build() resolves this helper through the scanner module at execution time.
# Keep the deployed scanner.py file untouched.
_scanner.price_e = price_e

BASE = 'https://wynnventory.com'
ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / 'site'
SNAPSHOT = 'https://raw.githubusercontent.com/realxuan24/WynnFlip/main/site/market.json'
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
MEM = {}
LOCK = threading.Lock()

def cached(key, ttl, fn):
    with LOCK:
        old = MEM.get(key)
        if old and time.time()-old[0]<ttl: return old[1]
    value=fn()
    with LOCK: MEM[key]=(time.time(),value)
    return value

def get_json(session,url,api_key=None,params=None,timeout=9):
    r=session.get(url,headers={'X-API-Key':api_key} if api_key else {},params=params,timeout=timeout)
    r.raise_for_status()
    return r.json()

def records(payload):
    if isinstance(payload,list): return payload
    if isinstance(payload,dict):
        for k in ('rankings','items','results','data'):
            if k in payload:
                result=records(payload[k]);
                if result:return result
    return []

def baseline_entry(row):
    if not isinstance(row,dict):return None
    vals=[number(row.get(k)) for k in ('average_mid_80_percent_price','p50_price','average_p50_ema_price','average_price')]
    price=next((v for v in vals if v is not None and 0<v<1e13),None)
    if price is None:return None
    count=number(row.get('total_count'))
    if count is not None and count<2:return None
    if row.get('shiny') or row.get('shiny_stat'):return None
    return {'price':round(price),'count':count,'origin':'WynnVentory historical asking-price statistics'}

def index_data(session,key):
    def work():
        errors=[]
        for url,auth in ((BASE+'/api/v2/market/rankings',key),(BASE+'/api/trademarket/ranking',None)):
            try:
                data=records(get_json(session,url,auth,timeout=9))
                result={}
                for r in data:
                    if not isinstance(r,dict) or not r.get('name'):continue
                    b=baseline_entry(r)
                    if b: result[(str(r['name']).casefold().strip(),str(r.get('tier') or ''))]=b
                if result:return result,url,errors
                errors.append(url.split('/')[-1]+': no usable records')
            except (requests.RequestException,ValueError,TypeError) as e:
                errors.append(url.split('/')[-1]+': '+type(e).__name__)
        return {},'unavailable',errors
    return cached('index',1200,work)

PRICE_INDEX_KEY = 'wynnflip:v13:price_index'

def read_cached_price_index():
    result, error = redis_call([['GET', PRICE_INDEX_KEY]])
    if error:
        return {}, None, error
    try:
        doc = json.loads(result[0]) if result and result[0] else {}
        if not isinstance(doc, dict) or not isinstance(doc.get('entries'), dict):
            return {}, None, None
        entries = {}
        for raw_key, value in doc['entries'].items():
            if isinstance(value, dict) and number(value.get('price')) and '\x1f' in raw_key:
                name, tier = raw_key.split('\x1f', 1)
                entries[(name, tier)] = value
        return entries, float(doc.get('updated_at') or 0), None
    except (TypeError, ValueError):
        return {}, None, 'invalid price index cache'

def save_cached_price_index(entries):
    # Replaces only the price index key; never touches the user's market history.
    safe = {str(k[0])+'\x1f'+str(k[1]): v for k, v in entries.items()
            if isinstance(k, tuple) and isinstance(v, dict) and number(v.get('price'))}
    payload = json.dumps({'updated_at': time.time(), 'entries': safe}, separators=(',', ':'))
    result, error = redis_call([['SET', PRICE_INDEX_KEY, payload, 'EX', 86400 * 7]])
    return error

def item_price_key(name):
    # Names are normalized; no identification status is inferred for API-level aggregates.
    return 'wynnflip:v13:item_price:' + quote(str(name).casefold().strip(), safe='')

def cached_item_price(session, key, name):
    k = item_price_key(name)
    result, err = redis_call([['GET', k]]) if os.getenv('UPSTASH_REDIS_REST_URL') else (None, 'not configured')
    if not err and result and result[0]:
        try:
            record = json.loads(result[0])
            if isinstance(record, dict) and number(record.get('price')):
                return record, 'redis'
        except (TypeError, ValueError):
            pass
    record = per_item(session, key, name)
    if record and not err:
        redis_call([['SET', k, json.dumps(record, separators=(',', ':')), 'EX', 86400]])
    return record, 'upstream' if record else 'unavailable'

def snapshot(session):
    def work():
        try: payload=get_json(session,SNAPSHOT,timeout=6)
        except (requests.RequestException,ValueError):return []
        out=[]
        for row in (payload.get('listings',[]) if isinstance(payload,dict) else [])[:7500]:
            if not isinstance(row,dict) or not row.get('name'):continue
            p=number(row.get('price_e'))
            if p is None or p<=0:continue
            out.append({'name':row['name'],'item_type':row.get('item_type',''),'subtype':row.get('subtype',''),
             'tier':row.get('rarity',''),'listing_price':p,'hash_code':'archive:'+str(row.get('id') or len(out)),
             'stat_rolls':row.get('stat_rolls') if isinstance(row.get('stat_rolls'),dict) else {},
             'timestamp':row.get('listing_timestamp'),'unidentified':row.get('unidentified') if row.get('unidentified') is not None else (row.get('identification_status')=='unidentified' if row.get('identification_status') in ('unidentified','identified') else None),'shiny_stat':row.get('shiny_stat')})
        return out
    return cached('snapshot',900,work)

def per_item(session,key,name):
    def work():
        url=BASE+'/api/trademarket/item/'+quote(name,safe='')+'/price'
        try:
            response=get_json(session,url,key,timeout=5)
            if isinstance(response,dict):return baseline_entry(response)
        except (requests.RequestException,ValueError,TypeError):pass
        return None
    return cached('item:'+name.casefold(),1800,work)

def redis_call(commands):
    """Return (results, error). No secrets are included in diagnostics."""
    url=os.environ.get('UPSTASH_REDIS_REST_URL','').strip().rstrip('/')
    token=os.environ.get('UPSTASH_REDIS_REST_TOKEN','').strip()
    if not url or not token:return None,'not configured'
    try:
        res=requests.post(url+'/pipeline',headers={'Authorization':'Bearer '+token},json=commands,timeout=9)
        res.raise_for_status()
        data=res.json()
        if not isinstance(data,list) or len(data)!=len(commands):return None,'invalid Redis response'
        errors=[r.get('error') for r in data if isinstance(r,dict) and r.get('error')]
        if errors:return None,'Redis command failed: '+str(errors[0])[:120]
        return [r.get('result') for r in data],None
    except (requests.RequestException,ValueError,TypeError) as exc:
        return None,type(exc).__name__

def load_history():
    values,error=redis_call([['GET','wynnflip:v12:history']])
    if error:return {},error
    try:
        obj=json.loads(values[0]) if values[0] else {}
        if not isinstance(obj,dict):return {},'invalid stored history shape'
        return obj,None
    except (TypeError,ValueError):return {},'invalid stored history JSON'

# Atomic Redis Lua merge prevents multiple visitors from overwriting one another.
# Original observation IDs are stable, so repeat scans do not increase counts.
HISTORY_MERGE_LUA = """
local raw=redis.call('GET',KEYS[1])
local current={observations={}}
if raw then
  local ok,obj=pcall(cjson.decode,raw)
  if ok and type(obj)=='table' and type(obj.observations)=='table' then current=obj end
end
local obs=current.observations
local incoming=cjson.decode(ARGV[1])
for id,value in pairs(incoming) do
  if obs[id]==nil then obs[id]=value end
end
local count=0
for _ in pairs(obs) do count=count+1 end
-- A full 8,000-entry sort on every scan is expensive in Redis Lua.
-- Keep the same oldest-first retention policy, but prune only at capacity.
if count>8000 then
  local keyed={}
  for id,val in pairs(obs) do
    table.insert(keyed,{id=id,t=tonumber(val.observed_at or 0) or 0})
  end
  table.sort(keyed,function(a,b) if a.t==b.t then return a.id<b.id else return a.t>b.t end end)
  for j=8001,#keyed do obs[keyed[j].id]=nil end
  count=8000
end
redis.call('SET',KEYS[1],cjson.encode(current))
return count
"""

def persist_history(rows):
    incoming={}
    now=time.time()
    for r in rows:
        p=number(r.get('listing_price'))
        if not r.get('name') or p is None or p<=0:continue
        ident=str(r.get('hash_code') or (str(r.get('name'))+':'+str(p)+':'+str(r.get('timestamp'))))
        incoming[ident]={'name':r['name'],'listing_price':p,'tier':r.get('tier'),
            'stat_rolls':r.get('stat_rolls') if isinstance(r.get('stat_rolls'),dict) else {},
            'hash_code':ident,'timestamp':r.get('timestamp'),'unidentified':r.get('unidentified'),'item_type':r.get('item_type'),'subtype':r.get('subtype'),'shiny_stat':r.get('shiny_stat'),'amount':r.get('amount',1),'observed_at':now}
    if not incoming:return None,'no valid observations to save'
    result,error=redis_call([['EVAL',HISTORY_MERGE_LUA,1,'wynnflip:v12:history',json.dumps(incoming,separators=(',',':'))]])
    return (int(result[0]) if result and result[0] is not None else None),error

# V14 lifecycle cache is separate from V12 pricing history (never overwritten).
LIFECYCLE_KEY = 'wynnflip:v14:listing_lifecycle'
LIFECYCLE_LUA = r"""
local raw=redis.call('GET',KEYS[1])
local old={entries={}}
if raw then
  local ok,v=pcall(cjson.decode,raw)
  if ok and type(v)=='table' and type(v.entries)=='table' then old=v end
end
local entries=old.entries
local now=tonumber(ARGV[1]);local scan=cjson.decode(ARGV[2])
local complete=ARGV[3]=='1'
local seen={}
local new_count=0;local repeat_count=0
for id,r in pairs(scan) do
  seen[id]=true
  local v=entries[id]
  if type(v)~='table' then
    v={name=r.name,first_seen=now,last_seen=now,scans_seen=1,initial_price=r.price,latest_price=r.price,price_changes=0,price_reductions=0,misses=0}
    entries[id]=v;new_count=new_count+1
  else
    -- Track observations only when genuinely processed by a new scan.
    if now>tonumber(v.last_seen or 0) then
      v.scans_seen=math.min(100000,tonumber(v.scans_seen or 1)+1)
      repeat_count=repeat_count+1
    end
    if r.price and v.latest_price and tonumber(r.price)~=tonumber(v.latest_price) then
      v.price_changes=math.min(100000,tonumber(v.price_changes or 0)+1)
      if tonumber(r.price)<tonumber(v.latest_price) then
        v.price_reductions=math.min(100000,tonumber(v.price_reductions or 0)+1)
      end
    end
    v.latest_price=r.price;v.last_seen=math.max(now,tonumber(v.last_seen or 0));v.misses=0
  end
end
if complete then
  for id,v in pairs(entries) do
    if not seen[id] and type(v)=='table' and tonumber(v.last_seen or 0)<now then
      v.misses=math.min(100,tonumber(v.misses or 0)+1)
    end
  end
end
local count=0
for _ in pairs(entries) do count=count+1 end
-- Sorting is only required when the 12,000-ID lifecycle store overflows.
if count>12000 then
  local ordered={}
  for id,v in pairs(entries) do
    table.insert(ordered,{id=id,t=tonumber(v.last_seen or 0) or 0})
  end
  table.sort(ordered,function(a,b) if a.t==b.t then return a.id<b.id else return a.t>b.t end end)
  for i=12001,#ordered do entries[ordered[i].id]=nil end
  count=12000
end
old.updated_at=now
redis.call('SET',KEYS[1],cjson.encode(old),'EX',86400*45)
return cjson.encode({new_count=new_count,repeat_count=repeat_count,tracked=count})
"""

def lifecycle_identifier(row):
    """Use only provider-defined listing IDs, never approximate price/name fingerprints."""
    for key in ('hash_code', 'listing_id', 'listingId', 'market_listing_id', 'marketListingId'):
        value=row.get(key)
        if isinstance(value,(str,int)) and not isinstance(value,bool):
            result=str(value).strip()
            if result and len(result)<=180 and not result.startswith('archive:'):
                return result
    return None


def lifecycle_input(rows):
    """Stable provider identifiers only; no fabricated IDs or implied sales."""
    incoming={}
    for row in rows:
        if not isinstance(row,dict): continue
        listing_id=lifecycle_identifier(row)
        price=price_e(row)
        if not listing_id or not row.get('name') or price is None or price<=0:
            continue
        incoming[listing_id]={'name':str(row['name'])[:100], 'price':price}
    return incoming


def fetch_lifecycle_identifiers(session,key,limit_pages=5,page_size=100):
    """Read legacy feed with documented hash_code when v2 omits stable IDs.

    Independent from the v2 pricing feed: failures never affect normal scanning.
    """
    rows=[]
    try:
        for page in range(1,limit_pages+1):
            payload=get_json(session,BASE+'/api/trademarket/listings',key,
                {'page':page,'page_size':page_size,'sort':'timestamp_desc'},timeout=8)
            batch,pages=extract_listing_page(payload)
            if not batch:break
            rows.extend(batch)
            if len(batch)<page_size or (pages is not None and page>=int(pages)):break
        n=len(lifecycle_input(rows))
        return (rows if n else []),('legacy v1 market listings' if n else 'legacy response had no stable IDs')
    except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
        return [],'legacy lookup unavailable: '+type(exc).__name__


def update_lifecycle(rows, complete, observed_at):
    incoming=lifecycle_input(rows)
    if not incoming:
        return None,'no stable identifiers',{}
    result,err=redis_call([['EVAL', LIFECYCLE_LUA,1,LIFECYCLE_KEY,
                            str(int(observed_at)),json.dumps(incoming,separators=(',',':')),
                            '1' if complete else '0']])
    if err:return None,err,{}
    try:
        counts=json.loads(result[0]);return counts,None,incoming
    except (TypeError,ValueError,IndexError):return None,'invalid lifecycle update',{}

def read_lifecycle():
    result,error=redis_call([['GET',LIFECYCLE_KEY]])
    if error:return {},error
    try:
        value=json.loads(result[0]) if result and result[0] else {}
        entries=value.get('entries',{}) if isinstance(value,dict) else {}
        return entries if isinstance(entries,dict) else {},None
    except (TypeError,ValueError):return {},'invalid lifecycle JSON'

def lifecycle_summary(items,entries,now,complete):
    """7/30d listing entry and observed persistence; never call removals sales."""
    current=now.timestamp()
    by_name=defaultdict(list)
    for key,r in entries.items():
        if not isinstance(r,dict):continue
        name=str(r.get('name') or '').casefold().strip()
        if name:by_name[name].append(r)
    stats={}
    for name,group in by_name.items():
        def last(v):return float(number(v.get('last_seen')) or 0)
        def first(v):return float(number(v.get('first_seen')) or 0)
        n7=sum(0<=current-first(v)<=7*86400 for v in group)
        n30=sum(0<=current-first(v)<=30*86400 for v in group)
        retained=sum(int(number(v.get('scans_seen')) or 0)>=2 for v in group)
        reductions=sum(int(number(v.get('price_reductions')) or 0) for v in group)
        changes=sum(int(number(v.get('price_changes')) or 0) for v in group)
        persistent=sum(int(number(v.get('scans_seen')) or 0)>=3 for v in group)
        span_hours=[max(0,(last(v)-first(v))/3600) for v in group if int(number(v.get('scans_seen')) or 0)>=2]
        longest=round(max(span_hours),1) if span_hours else None
        eligible=sum(last(v)>=current-7*86400 for v in group)
        missing=sum(int(number(v.get('misses')) or 0)>=2 for v in group) if complete else None
        # Unknown for incomplete scans: pagination absence is NOT turnover.
        stats[name]={'new_listings_7d':n7,'new_listings_30d':n30,
                     'reobserved_listings':retained,'recently_observed_listings':eligible,
                     'price_reductions':reductions,'price_changes':changes,
                     'persistent_listings':persistent,'longest_observed_hours':longest,
                     'unobserved_complete_scans':missing,
                     'confidence':'early' if n30<5 else 'developing' if n30<20 else 'established',
                     'verified_sales':0,'sale_rate':None}
    for item in items:
        key=str(item.get('name') or '').casefold().strip()
        item['lifecycle']=stats.get(key,{'new_listings_7d':0,'new_listings_30d':0,
            'reobserved_listings':0,'recently_observed_listings':0,
            'price_reductions':0,'price_changes':0,'persistent_listings':0,'longest_observed_hours':None,
            'unobserved_complete_scans':None,'confidence':'insufficient',
            'verified_sales':0,'sale_rate':None})
    return {'tracked_items':len(by_name),'entries_30d':sum(0<=current-float(number(e.get('first_seen')) or 0)<=30*86400 for e in entries.values() if isinstance(e,dict)),
            'price_changes':sum(int(number(e.get('price_changes')) or 0) for e in entries.values() if isinstance(e,dict)),
            'price_reductions':sum(int(number(e.get('price_reductions')) or 0) for e in entries.values() if isinstance(e,dict)),
            'persistent':sum(int(number(e.get('scans_seen')) or 0)>=3 for e in entries.values() if isinstance(e,dict)),
            'entries_7d':sum(0<=current-float(number(e.get('first_seen')) or 0)<=7*86400 for e in entries.values() if isinstance(e,dict)),
            'reobserved':sum(int(number(e.get('scans_seen')) or 0)>=2 for e in entries.values() if isinstance(e,dict))}

def liquidity_evidence(item):
    """Cautious liquidity-risk assessment; no absence/sale or time-to-sale inference.

    Historical lifetimes are lower bounds on observed presence, not sale durations.
    All activity by name aggregates different rolls and must be labeled as such.
    """
    l=item.get('lifecycle') or {}
    m=item.get('sellability') or {}
    tracked=int(number(l.get('new_listings_30d')) or 0)
    repeated=int(number(l.get('reobserved_listings')) or 0)
    persistent=int(number(l.get('persistent_listings')) or 0)
    reductions=int(number(l.get('price_reductions')) or 0)
    hours=number(l.get('longest_observed_hours'))
    cheaper=int(number(m.get('cheaper_asks')) or 0)
    # Only mark a higher-risk market when meaningful persistent inventory or
    # competitor evidence is present. Never award a 'fast selling' label.
    enough=tracked>=5 and repeated>=3
    high_competition=cheaper>=4
    persistent_concern=bool(hours is not None and hours>=48 and persistent>=3)
    repricing_concern=bool(reductions>=3 and repeated>=5)
    if high_competition or (enough and (persistent_concern or repricing_concern)):
        label='Higher resale risk'
        reasons=[]
        if high_competition:reasons.append(f'{cheaper} significantly cheaper competing asks')
        if persistent_concern:reasons.append(f'{persistent} listings repeatedly seen; longest observed span {hours:.1f}h')
        if repricing_concern:reasons.append(f'{reductions} observed asking-price reductions')
    elif enough:
        label='Some liquidity evidence'
        reasons=[f'{tracked} distinct IDs tracked over 30d; {repeated} reobserved']
        if cheaper:reasons.append(f'{cheaper} cheaper competing asks')
    else:
        label='Insufficient liquidity evidence'
        reasons=['Too few independently tracked listing IDs across scans']
        if high_competition:reasons.append(f'{cheaper} cheaper competing asks')
    return {'label':label,'confidence':('developing' if enough else 'insufficient'),
            'reasons':reasons,'tracked_30d':tracked,'reobserved':repeated,
            'persistent':persistent,'observed_price_reductions':reductions,
            'longest_observed_hours':hours,'cheaper_competitors':cheaper,
            'possible_removals':None,'confirmed_sales':None,'estimated_sale_hours':None,
            'note':'Name-wide supply-side indicators, not buyer demand; incomplete scans cannot prove removals or sales.'}


# WynnFlip V17 — transparent ID evidence. Never turn asking prices into verified sales.
# Public weights are optional, and can be absent due to upstream changes.
WEIGHTS_URL = 'https://raw.githubusercontent.com/szVM3K/builderwynncraft/main/src/item-weights.json'
WEIGHTS_FALLBACK_URL = 'https://cdn.jsdelivr.net/gh/szVM3K/builderwynncraft@main/src/item-weights.json'
WEIGHTS_KEY = 'wynnflip:v17:community_weight_profiles'

def normalize_weight_profiles(raw):
    """Accept validated community profiles without guessing missing ID weights.

    Supports item -> profile -> signed weights, the common [{name,weights}]
    profile shape, and direct item -> signed weights. Source keys are normalized
    only for matching, not silently translated to different identifications.
    """
    result={}
    if isinstance(raw,dict) and isinstance(raw.get('items'),dict):raw=raw['items']
    if not isinstance(raw,dict):return result
    for item,entry in raw.items():
        if isinstance(entry,list):
            choices={str(x.get('name') or x.get('profile') or 'Main'):x for x in entry if isinstance(x,dict)}
        elif isinstance(entry,dict):
            direct=entry.get('weights',entry)
            choices={'Main':direct} if direct and all(isinstance(v,(int,float)) and not isinstance(v,bool) for v in direct.values()) else entry
        else:continue
        profiles={}
        for label,record in choices.items():
            if not isinstance(record,dict):continue
            weights=record.get('weights',record)
            if not isinstance(weights,dict):continue
            cleaned={}
            for key,weight in weights.items():
                if not isinstance(weight,(int,float)) or isinstance(weight,bool) or not math.isfinite(weight) or not 0<abs(weight)<=1:
                    cleaned={};break
                clean_key=normalized_name(key)
                if not clean_key or clean_key in cleaned:cleaned={};break
                cleaned[clean_key]=float(weight)
            if cleaned and .90<=sum(abs(x) for x in cleaned.values())<=1.10:
                profiles[str(label)]=cleaned
        if profiles:result[normalized_name(item)]=profiles
    return result

def _community_profiles_uncached(session):
    """Read-through Redis cache avoids repeated upstream requests from serverless cold starts."""
    local=load_profiles(ROOT/'profiles/item_weights.json')
    if len(local)>=10:return local,'bundled',None
    cached_weights,error=redis_call([['GET', WEIGHTS_KEY]])
    if not error and cached_weights and cached_weights[0]:
        try:
            payload=json.loads(cached_weights[0]); profiles=normalize_weight_profiles(payload)
            if len(profiles)>=10:return profiles,'Redis community profiles',None
        except (TypeError,ValueError):pass
    errors=[]
    for url in (WEIGHTS_URL,WEIGHTS_FALLBACK_URL):
        try:
            response=get_json(session,url,timeout=4)
            profiles=normalize_weight_profiles(response)
            if len(profiles)>=10:
                if not error:redis_call([['SET',WEIGHTS_KEY,json.dumps(response,separators=(',',':')),'EX',86400*7]])
                return profiles,'Wynnpool-derived community profiles',None
            errors.append('unrecognized community profile format')
        except (requests.RequestException,ValueError,TypeError) as exc:
            errors.append(type(exc).__name__)
    return local,'unavailable','; '.join(errors)

def community_profiles(session):
    return cached('v17:community_profiles',1800,lambda:_community_profiles_uncached(session))

def v17_roll_evidence(items, live_rows, history_rows, profiles):
    """Same item/variant/status, valid shared roll percentage similarity.

    Prices stay as observed asking references, not sale predictions. An
    unweighted roll match is a comparison, NOT a desirability ranking.
    """
    groups=defaultdict(dict)
    for row in live_rows+history_rows:
        if not isinstance(row,dict):continue
        observed=normalized_observation_time(row.get('timestamp'))
        if observed is None or observed>time.time()+300 or time.time()-observed>14*86400:continue
        name=normalized_name(row.get('name'))
        p=price_e(row)
        if not name or not p or row.get('shiny_stat'):continue
        # Only explicitly identified rows with valid stat percentages.
        if identification_status(row) != 'identified':continue
        rolls={normalized_name(k):v for k,v in roll_numbers(row).items() if v is not None}
        if len(rolls)<2:continue
        key=identity(row)
        groups[name][key]=(p,rolls,row)
    for item in items:
        result={'status':'unavailable','method':'none','community_profile':None,'weighted_score':None,
                'important_ids':[], 'matched_offer_count':0,'matched_asking_range_e':None,
                'official_item_definitions':'On demand; open item details','note':'No verified sales or build popularity.'}
        item['v17_roll_intel']=result
        if item.get('identification_status')!='identified' or item.get('shiny_stat'):continue
        own_ids=item.get('exact_ids') or []
        own={normalized_name(x.get('api_name') or x.get('name')):number(x.get('roll_pct')) for x in own_ids if isinstance(x,dict)}
        own={k:v for k,v in own.items() if k and v is not None and 0<=v<=100}
        if len(own)<2:continue
        # Use the original provider-supplied stat rolls for profile quality.
        own_raw=next((r for r in live_rows if identity(r)==str(item.get('id'))),None)
        if own_raw is None:
            own_raw=next((r for r in live_rows if normalized_name(r.get('name'))==normalized_name(item.get('name')) and price_e(r)==number(item.get('price_e'))),None)
        profile=profile_quality(own_raw,profiles) if own_raw else None
        if profile:
            result['community_profile']=profile.get('profile')
            result['weighted_score']=profile.get('score')
            result['important_ids']=profile.get('important_ids') or []
        name=normalized_name(item.get('name'))
        candidates=[]
        for price,rr,row in groups[name].values():
            if own_raw is not None and identity(row)==identity(own_raw):continue
            # Only same rarity, subtype, and status where provider identifies those fields.
            if item.get('rarity') and row.get('tier') and str(row.get('tier')).casefold()!=str(item.get('rarity')).casefold():continue
            if item.get('subtype') and row.get('subtype') and str(row.get('subtype')).casefold()!=str(item.get('subtype')).casefold():continue
            if item.get('item_type') and row.get('item_type') and str(row.get('item_type')).casefold()!=str(item.get('item_type')).casefold():continue
            shared=set(own)&set(rr)
            if len(shared)<2:continue
            if profile:
                weights={x['stat']:abs(float(x['weight'])) for x in result['important_ids']}
                common=[k for k in shared if k in weights]
                coverage=sum(weights[k] for k in common)
                if coverage<.60:continue
                distance=sum(abs(own[k]-rr[k])*weights[k] for k in common)/coverage
                cutoff=18
                mode='community-weighted roll similarity'
            else:
                if len(shared)<3:continue
                distance=statistics.mean(abs(own[k]-rr[k]) for k in shared)
                cutoff=12
                mode='unweighted roll similarity'
            if distance<=cutoff:candidates.append(price)
        if candidates:
            result['status']='matched_asking_evidence' if len(candidates)>=5 and max(candidates)/max(1,min(candidates))<=2 else 'thin_matching_evidence'
            result['method']=mode
            result['matched_offer_count']=len(candidates)
            if len(candidates)>=3 and max(candidates)/max(1,min(candidates))<=2:
                ordered=sorted(candidates)
                result['matched_asking_range_e']={'low':ordered[max(0,int((len(ordered)-1)*.25))],
                         'median':round(statistics.median(ordered)),
                         'high':ordered[min(len(ordered)-1,int((len(ordered)-1)*.75))]}
        elif profile:
            result['status']='weighted_score_only'
            result['method']='community-weighted roll score, no matched asking prices'
        else:
            result['status']='insufficient_roll_evidence'
            result['method']='no verified community weight profile'
    return sum(x['v17_roll_intel']['matched_asking_range_e'] is not None for x in items)

# Official ranges are fetched only when a user asks, not on every market scan.
@app.get('/api/roll-definition/{item_name}')
def roll_definition(item_name:str):
    if not item_name or len(item_name)>100 or '/' in item_name:return JSONResponse({'available':False,'reason':'Invalid name'},status_code=400)
    def fetch():
        with requests.Session() as sess:
            try:
                payload=get_json(sess,'https://api.wynncraft.com/v3/item/search/'+quote(item_name,safe=''),timeout=6)
                if not isinstance(payload,list):return {'available':False,'reason':'Unrecognized official response'}
                matching=next((r for r in payload if isinstance(r,dict) and normalized_name(r.get('displayName'))==normalized_name(item_name)),None)
                if not matching:return {'available':False,'reason':'No exact official item match'}
                return {'available':True,'item':matching.get('displayName'),'identifications':matching.get('identifications',{}),
                        'source':'Official Wynncraft API v3','note':'Official ranges describe possible ID rolls, not resale prices.'}
            except (requests.RequestException,ValueError,TypeError):return {'available':False,'reason':'Official API temporarily unavailable'}
    return cached('official:'+normalized_name(item_name),3600,fetch)

def apply_baselines(items,baselines,session,key):
    filled=0; lookups=0; groups=defaultdict(list)
    for item in items:groups[str(item.get('name','')).strip().casefold()].append(item)
    # Per-item lookups only for limited number of distinct names, never hundreds per refresh.
    missing=[]
    for name,group in groups.items():
        if not name:continue
        if (name,'') not in baselines and not any(item.get('fair_e') for item in group):
            missing.append(name)
    selected=missing[:12]
    # Independent per-item requests (including independent Redis cache lookups).
    # Use a separate Session per worker; do not share requests.Session across threads.
    def price_worker(name):
        with requests.Session() as worker_session:
            return cached_item_price(worker_session,key,name)[0]
    if selected:
        with ThreadPoolExecutor(max_workers=min(4,len(selected))) as executor:
            tasks={executor.submit(price_worker,name):name for name in selected}
            results={}
            for future in as_completed(tasks):
                name=tasks[future]
                # cached_item_price already handles unavailable upstream data, but
                # a failed optional lookup must not abort a successful market scan.
                try:results[name]=future.result()
                except (requests.RequestException,ValueError,TypeError):results[name]=None
        for name in selected:
            lookups+=1
            if results.get(name):baselines[(name,'')]=results[name]
    for item in items:
        if item.get('fair_e') is not None:
            item['valuation_source']='roll_comparables' if item.get('identification_status')=='identified' else 'unid_comparables' if item.get('identification_status')=='unidentified' else 'unknown';
            if item['valuation_source']=='unknown': item['fair_e']=None
            continue
        keyname=str(item.get('name') or '').strip().casefold()
        if not keyname or item.get('identification_status') != 'identified' or item.get('shiny_stat'):continue
        record=baselines.get((keyname,str(item.get('item_tier') or ''))) or baselines.get((keyname,''))
        if not record:continue
        price=round(record['price']);buy=number(item.get('price_e'))
        if not buy or buy<=0:continue
        item['fair_e']=price
        item['valuation_source']='item_baseline_not_roll_adjusted'
        item['baseline_sample_count']=record.get('count')
        # Baseline discount is a *screening signal*, not a realizable profit estimate.
        item['baseline_discount_pct']=round(100*(price-buy)/price,1) if price else None
        item['profit_e']=None;item['roi']=None;item['category']='unrated'
        item['warnings']=list(item.get('warnings') or [])+['Item baseline is an asking-price statistic; not exact-roll resale value.']
        filled+=1
    return filled,lookups


def apply_unidentified_comparables(items, raw_rows):
    """Compute conservative UNID-only estimates from independent offers."""
    count = 0
    for item in items:
        if item.get('identification_status') != 'unidentified':
            continue
        offers = status_matched_offers(item, raw_rows)
        prices = sorted(offers.values())
        if len(prices) < 3:
            continue
        median = statistics.median(prices)
        trimmed = sorted(p for p in prices if 0.4 * median <= p <= 2.5 * median)
        if len(trimmed) < 3:
            continue
        reference_price = round(trimmed[int((len(trimmed) - 1) * 0.35)] * 0.9)
        if reference_price <= 0:
            continue
        buy = number(item.get('price_e'))
        item.update(fair_e=reference_price, valuation_source='unid_comparables',
                    comparables=len(trimmed), baseline_sample_count=len(trimmed),
                    warnings=list(item.get('warnings') or []) +
                    ['UNID-only observed asking prices, not confirmed sales.'])
        if buy and buy > 0:
            item['baseline_discount_pct'] = round(100 * (reference_price - buy) / reference_price, 1)
        count += 1
    return count


def status_matched_offers(item, rows):
    """Return distinct same-item, same-ID-status asking prices.

    V12 archives store rarity in `tier`; other feeds can store it in
    `rarity`, with `tier` blank. Missing rarity is not proof of a mismatch,
    but explicit contradictory rarities are excluded. Unknown ID status
    is never treated as UNID.
    """
    target_status = item.get('identification_status')
    if target_status not in ('identified', 'unidentified'):
        return {}
    norm = lambda value: str(value or '').strip().casefold()
    target_name = norm(item.get('name'))
    target_tier = norm(item.get('rarity') or item.get('item_tier'))
    target_type = norm(item.get('item_type'))
    target_subtype = norm(item.get('subtype'))
    target_shiny = norm(item.get('shiny_stat'))
    own_id = str(item.get('id') or '')
    result = {}
    for row in rows:
        if identification_status(row) != target_status:
            continue
        if norm(row.get('name')) != target_name or not target_name:
            continue
        if norm(row.get('item_type')) != target_type:
            continue
        if norm(row.get('subtype')) != target_subtype:
            continue
        if norm(row.get('shiny_stat')) != target_shiny:
            continue
        other_tier = norm(row.get('rarity') or row.get('tier'))
        if target_tier and other_tier and target_tier != other_tier:
            continue
        unique_id = identity(row)
        if unique_id == own_id:
            continue
        p = price_e(row)
        if p is not None and p > 0:
            result[unique_id] = p
    return result


def apply_status_matched_base_references(items, historical_rows):
    """Populate *general base references*, never roll-specific resale prices."""
    filled = 0
    for item in items:
        if item.get('fair_e') is not None:
            continue
        prices = sorted(status_matched_offers(item, historical_rows).values())
        if len(prices) < 3:
            continue
        med = statistics.median(prices)
        filtered = sorted(v for v in prices if 0.4 * med <= v <= 2.5 * med)
        if len(filtered) < 3:
            continue
        ref = round(filtered[int((len(filtered) - 1) * 0.35)])
        if ref <= 0:
            continue
        buy = number(item.get('price_e'))
        item['fair_e'] = ref
        item['valuation_source'] = 'status_matched_base_only'
        item['baseline_sample_count'] = len(filtered)
        item['baseline_discount_pct'] = round(100 * (ref - buy) / ref, 1) if buy and buy > 0 else None
        item['profit_e'] = None
        item['roi'] = None
        item['category'] = 'unrated'
        item['warnings'] = list(item.get('warnings') or []) + [
            'Same-item/status asking-price reference; NOT a roll-adjusted resale estimate.'
        ]
        filled += 1
    return filled


def augment(items,reference):
    # Transparent signals; no speculative claim of sales or profit/hour.
    groups=defaultdict(list)
    for r in reference:
        p=number(r.get('listing_price'))
        if p and p>0:groups[str(r.get('name','')).casefold()].append(p)
    for item in items:
        name=str(item.get('name','')).casefold(); vals=groups.get(name,[])
        baseline=number(item.get('fair_e')); price=number(item.get('price_e'))
        same=sum(1 for v in vals if baseline and .7*baseline<=v<=1.3*baseline)
        discount=100*(baseline-price)/baseline if baseline and price else None
        has_rolls=item.get('valuation_source') in ('roll_comparables','unid_comparables')
        sample=item.get('comparables',0) if has_rolls else item.get('baseline_sample_count') or len(vals)
        sample=number(sample,0) or 0
        if len(vals)>=5:
            med=statistics.median(vals)
            mad=statistics.median(abs(v-med) for v in vals)
            volatility=round(100*mad/med,1) if med else None
        else:volatility=None
        risks=[]
        if sample<4:risks.append('thin evidence')
        if volatility is not None and volatility>45:risks.append('wide historical price dispersion')
        if discount is not None and discount>80:risks.append('extreme discount: verify variant and roll quality')
        if item.get('cheaper_competitors') and item['cheaper_competitors']>2:risks.append('competing cheaper listings')
        if not has_rolls:risks.append('no status-matched resale estimate')
        if item.get('listing_age_minutes') is None:risks.append('timestamp unavailable')
        strength='very limited' if sample<3 else 'limited' if sample<8 else 'moderate'
        item['market_intel']={'baseline_discount_pct':round(discount,1) if discount is not None else None,
            'sample':int(sample),'history_similar_prices':same,'spread_proxy_pct':volatility,'evidence':strength,
            'risks':risks,'confirmed_sales':False}
        # Split screening candidates from evidence-qualified roll comparisons.
        if has_rolls and sample>=3 and number(item.get('roi'),0)>=25 and number(item.get('profit_e'),0)>0 and len(risks)<=1:
            item['v12_signal']='roll_opportunity'
        elif (not has_rolls and sample>=3 and discount is not None and 20<=discount<=80 and len(risks)<=2):
            item['v12_signal']='research'
        else:item['v12_signal']='market'
    return items

def assess_sellability(items, recent_rows, history_rows):
    """Observed market activity proxy, NOT buyer demand or confirmed sales.

    Distinct listing identities prevent repeated scans inflating activity.
    Penalizes thin markets, excessive cheaper competition and unstable asks.
    Only compares the same item/status/variant when identifying competition.
    """
    reference = list(recent_rows) + list(history_rows)
    groups = defaultdict(dict)
    for r in reference:
        status = identification_status(r)
        if status not in ('identified', 'unidentified'):
            continue
        price = price_e(r)
        if not price or price <= 0:
            continue
        key = (str(r.get('name') or '').strip().casefold(), status,
               str(r.get('item_type') or '').strip().casefold(),
               str(r.get('subtype') or '').strip().casefold(),
               str(r.get('shiny_stat') or '').strip().casefold())
        if not key[0]:
            continue
        groups[key][identity(r)] = float(price)
    for item in items:
        key = (str(item.get('name') or '').strip().casefold(),
               item.get('identification_status'),
               str(item.get('item_type') or '').strip().casefold(),
               str(item.get('subtype') or '').strip().casefold(),
               str(item.get('shiny_stat') or '').strip().casefold())
        distinct = groups.get(key, {})
        offers = sorted(distinct.values())
        count = len(offers)
        price = number(item.get('price_e')) or 0
        cheaper = sum(1 for v in offers if v < price * .97) if price else 0
        if count >= 4:
            median = statistics.median(offers)
            mad = statistics.median(abs(v - median) for v in offers)
            dispersion = mad / median if median else 1
        else:
            dispersion = None
        # More listings = better *observability*, not automatically stronger demand.
        score = min(38, 11 * max(0, count - 1))
        score += 22 if count >= 6 and dispersion is not None and dispersion < .25 else 0
        score += 12 if count >= 4 and dispersion is not None and dispersion < .5 else 0
        score -= min(40, cheaper * 10)
        if dispersion is not None and dispersion > .6:
            score -= 18
        score = max(0, min(100, round(score)))
        if count < 3:
            label = 'Unknown / thin market'
        elif cheaper >= 4 or (dispersion is not None and dispersion > .75):
            label = 'High resale risk'
        elif count >= 6 and cheaper <= 1 and dispersion is not None and dispersion < .3:
            label = 'More active / stable asks'
        else:
            label = 'Uncertain demand'
        item['sellability'] = {'score':score, 'label':label,
            'unique_observed_offers':count,'cheaper_asks':cheaper,
            'price_dispersion_pct':round(100*dispersion,1) if dispersion is not None else None,
            'verified_sales':False,
            'note':'Listings are offers; this cannot measure real sales or resale speed.'}
        # Do not promote low-demand reference-only bargains as recommendations.
        if item.get('v12_signal') in ('research','roll_opportunity'):
            if label in ('Unknown / thin market','High resale risk'):
                item['v13_recommendation'] = 'speculative'
            elif label == 'More active / stable asks':
                item['v13_recommendation'] = 'research_priority'
            else:
                item['v13_recommendation'] = 'speculative'
        else:
            item['v13_recommendation'] = 'market'
    return items


def suppress_trivial_leads(items):
    """Keep cheap/bulk listings searchable, but not in the flip board.

    Guard against misleading percentage discounts on near-zero-value items.
    The listing price is per unit; never divide by quantity.
    """
    min_total = 4096  # One liquid emerald (LE), adjustable through an env var.
    try:
        min_total = max(0, int(os.getenv('WYNNFLIP_MIN_LEAD_PRICE_E', '4096')))
    except ValueError:
        pass
    for item in items:
        price = number(item.get('price_e')) or 0
        qty = number(item.get('quantity')) or 1
        per_unit = price
        name = str(item.get('name', '')).casefold().strip()
        trivial = price < min_total or per_unit <= 64
        # Common bulk consumable: even a large stack doesn't merit an
        # apparent flip derived from an inflated aggregate price reference.
        if name in {'corkian amplifier i', 'corkian amplifier 1', 'corkian amplifier 1s'}:
            trivial = True
        if trivial:
            item['low_value_filtered'] = True
            item['low_value_reason'] = (
                'Low-value or bulk item: hidden from opportunities; still in Market Explorer.'
            )
            item['v12_signal'] = 'market'
        else:
            item['low_value_filtered'] = False
    return items

def apply_trade_costs(items):
    """Estimate take-home sale proceeds and purchase cost in emeralds.

    The wiki documents a 5% buyer-side market charge, rather than a
    separate seller tax. A displayed buyer-inclusive sale price therefore
    pays the seller approximately sale_price / 1.05. WynnVentory's price
    representation isn't guaranteed, so the assumption is explicit and
    configurable without changing the public API key or frontend.
    """
    fee = 0.05
    # Default: WynnVentory prices represent displayed buyer-facing market
    # prices. Set to 'false' only if verified to be untaxed seller asks.
    prices_include_tax = os.getenv('WYNNFLIP_PRICES_INCLUDE_BUYER_TAX', 'true').strip().lower() not in ('0','false','no')
    for item in items:
        buy = number(item.get('price_e'))
        fair = number(item.get('fair_e'))
        item['market_buyer_fee_pct'] = 5.0
        item['price_assumption'] = 'buyer_inclusive' if prices_include_tax else 'seller_ask'
        qty = max(1, int(number(item.get('quantity')) or 1))
        item['unit_price_e'] = round(buy) if buy and buy > 0 else None
        item['stack_total_e'] = round(buy * qty) if buy and buy > 0 else None
        item['purchase_total_e'] = (round(buy * qty) if prices_include_tax else round(buy * qty * (1 + fee))) if buy and buy > 0 else None
        if item.get('valuation_source') in ('roll_comparables','unid_comparables') and fair and buy and buy > 0:
            # If the reference includes the fee charged to the resale buyer,
            # subtract that fee to estimate what the seller receives.
            resale_take_home = round(fair * qty / (1 + fee)) if prices_include_tax else round(fair * qty)
            purchase_total = item['purchase_total_e']
            possible = resale_take_home - purchase_total
            item['resale_estimate_e'] = round(fair)  # per unit, like historical comparisons
            item['resale_stack_total_e'] = round(fair * qty)
            item['resale_take_home_e'] = resale_take_home
            item['selling_transaction_fee_e'] = max(0, round(fair * qty) - resale_take_home) if prices_include_tax else 0
            item['potential_net_profit_e'] = possible
            item['potential_net_roi_pct'] = round(100 * possible / purchase_total, 1) if purchase_total else None
            item['price_kind'] = 'unidentified_asking_reference' if item.get('valuation_source')=='unid_comparables' else 'conservative_roll_asking_reference'
            if possible <= 0 or (item['potential_net_roi_pct'] is not None and item['potential_net_roi_pct'] < 25):
                if item.get('v12_signal') == 'roll_opportunity':
                    item['v12_signal'] = 'market'
        else:
            item['potential_net_profit_e'] = None
            item['potential_net_roi_pct'] = None
            item['resale_estimate_e'] = None
            item['resale_stack_total_e'] = None
            item['resale_take_home_e'] = None
            item['selling_transaction_fee_e'] = None
            item['price_kind'] = 'baseline_only' if fair else 'unknown'
    return items



def verified_comparable_rows(item, rows, *, max_age_days=14):
    """Recent listing observations only; old archived or malformed rows cannot
    be used to justify a prominent market reference. ID keys deduplicate offers.
    A missing timestamp or id is treated as missing evidence, not a market price.
    """
    target=item.get('identification_status')
    if target not in ('identified','unidentified'):return []
    target_name=normalized_name(item.get('name'))
    if not target_name:return []
    now=time.time()
    results={}
    def norm(x):return str(x or '').strip().casefold()
    for row in rows:
        if not isinstance(row,dict) or normalized_name(row.get('name'))!=target_name:continue
        if identification_status(row)!=target:continue
        if norm(row.get('shiny_stat'))!=norm(item.get('shiny_stat')):continue
        if norm(row.get('item_type'))!=norm(item.get('item_type')):continue
        if norm(row.get('subtype'))!=norm(item.get('subtype')):continue
        other_tier=norm(row.get('rarity') or row.get('tier'))
        own_tier=norm(item.get('rarity') or item.get('item_tier'))
        if other_tier and own_tier and other_tier!=own_tier:continue
        # Reject old rows (including archived rows with uncertain observation times).
        observed=normalized_observation_time(row.get('timestamp'))
        if observed is None or observed>now+300 or now-observed>max_age_days*86400:continue
        unique=identity(row)
        if not unique or str(unique)==str(item.get('id') or ''):continue
        price=price_e(row)
        if not price or price>1e12:continue
        old=results.get(unique)
        if old is None or observed>old[0]:results[unique]=(observed,price, row)
    return [(uid,*record) for uid,record in results.items()]


def add_tentative_price_ranges(items, historical_rows):
    """Only disclose cautious, auditable SAME-STATUS ASKING benchmarks.

    No estimate when evidence is thin, stale or internally contradictory.
    The price model never extrapolates or manufactures a 'realistic' price.
    """
    count=0
    for item in items:
        item['tentative_range_e']=None
        item['tentative_sample_count']=0
        item['tentative_status']='insufficient_evidence'
        item['tentative_reason']='Fewer than 5 independent recent comparable offers'
        item['tentative_comparables']=[]
        if item.get('resale_estimate_e') is not None:continue
        matches=verified_comparable_rows(item,historical_rows)
        if len(matches)<5:continue
        prices=sorted(record[2] for record in matches)
        median=statistics.median(prices)
        buy=number(item.get('price_e'))
        # An extraordinary gap, such as the reported Temerity 84x reference,
        # needs independent verification before a price is put on the main card.
        # This is a presentation safety gate, never an estimate/cap on true value.
        if buy and median / buy > 8:
            item['tentative_reason']='Extraordinary gap (>8x listing price): requires direct market verification'
            continue
        # Historical price scale disagreement is itself uncertainty; do not
        # arbitrarily stretch/cap observations into an attractive estimate.
        inliers=[record for record in matches if .65*median<=record[2]<=1.6*median]
        if len(inliers)<5:
            item['tentative_reason']='Recent offers disagree too much to estimate a range'
            continue
        ordered=sorted(x[2] for x in inliers)
        lo=ordered[(len(ordered)-1)//4]
        hi=ordered[(3*(len(ordered)-1))//4]
        if lo<=0 or hi/lo>1.7:
            item['tentative_reason']='Comparable prices are too inconsistent'
            continue
        # NEVER call this a verified resale value: these are asking prices.
        item['tentative_range_e']={'low':lo,'high':hi,'median':round(statistics.median(ordered))}
        item['tentative_sample_count']=len(inliers)
        item['tentative_status']='recent_consistent_asking_evidence'
        item['tentative_reason']='Recent, independent asking-price references; not matched sales'
        item['tentative_range_kind']=('Recent UNID asking references' if item.get('identification_status')=='unidentified'
                                       else 'Recent identified asking references; not roll-adjusted')
        item['tentative_comparables']=[{'id':str(u)[:100],'price_e':p,'observed_at':datetime.fromtimestamp(t,timezone.utc).isoformat()}
                                      for u,t,p,_ in sorted(inliers,key=lambda x:x[2])[:12]]
        count+=1
    return count

def normalized_observation_time(value):
    """Normalize API observation timestamps; do not assert in-game creation time."""
    from scanner import timestamp as parse_timestamp
    dt = parse_timestamp(value)
    return dt.timestamp() if dt else None


def deduplicate_recent(rows):
    """Keep one record per listing ID, retaining the newest valid observation."""
    from scanner import identity
    selected = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = identity(row)
        stamp = normalized_observation_time(row.get('timestamp'))
        if key not in selected or (stamp is not None and (selected[key][0] is None or stamp > selected[key][0])):
            selected[key] = (stamp, row)
    result = [v[1] for v in selected.values()]
    result.sort(key=lambda row: (normalized_observation_time(row.get('timestamp')) is None,
                                  -(normalized_observation_time(row.get('timestamp')) or 0)))
    return result


def mark_observation_freshness(items, now):
    now_s = now.timestamp()
    for item in items:
        ts = normalized_observation_time(item.get('listing_timestamp'))
        # Future timestamps indicate bad metadata, not an infinitely fresh listing.
        if ts is None or ts > now_s + 90:
            item['observation_freshness'] = 'unknown'
            item['listing_age_minutes'] = None
            item['eligible_fresh_opportunity'] = False
            continue
        age = max(0, (now_s - ts) / 60)
        item['listing_age_minutes'] = round(age, 1)
        item['observation_freshness'] = 'recent' if age <= 5 else 'verify' if age <= 15 else 'stale'
        item['eligible_fresh_opportunity'] = age <= 5
    return items


def fetch_market(key,session=None,limit_pages=5,page_size=100):
    started=time.perf_counter()
    timings={}
    session=session or requests.Session();rows=[];pages=None
    # Discover page count from the first response. Only fetch the remaining
    # bounded pages concurrently; assemble them in original page order.
    data=get_json(session,BASE+'/api/v2/market/listings',key,
         {'page':1,'page_size':page_size,'sort':'timestamp_desc'},timeout=12)
    first,pages=extract_listing_page(data)
    rows.extend(first)
    if len(first)>=page_size and (pages is None or int(pages)>1) and limit_pages>1:
        last_page=min(limit_pages,int(pages)) if pages is not None else limit_pages
        def listing_worker(page):
            with requests.Session() as worker_session:
                response=get_json(worker_session,BASE+'/api/v2/market/listings',key,
                     {'page':page,'page_size':page_size,'sort':'timestamp_desc'},timeout=12)
                batch,_=extract_listing_page(response)
                return batch
        with ThreadPoolExecutor(max_workers=min(4,last_page-1)) as executor:
            futures={executor.submit(listing_worker,p):p for p in range(2,last_page+1)}
            batches={}
            for future in as_completed(futures):
                batches[futures[future]]=future.result()
        for page in range(2,last_page+1):
            batch=batches[page]
            rows.extend(batch)
            if len(batch)<page_size:break
    timings['listings']=round(time.perf_counter()-started,2)
    rows = deduplicate_recent(rows)
    if not rows:raise ValueError('No market listings returned')
    now=datetime.now(timezone.utc)
    # Only complete pagination is eligible for absence statistics.
    complete_scan=bool((pages is not None and page>=int(pages)) or len(batch)<page_size)
    reference=snapshot(session)
    stored,history_read_error=load_history()
    timings['reference_and_history']=round(time.perf_counter()-started-sum(timings.values()),2)
    # Bounded historical distinct observations, stored as explicit comparison-only data.
    prior=stored.get('observations',{}) if isinstance(stored.get('observations'),dict) else {}
    historical_rows=list(prior.values())[:8000]
    profiles,weights_source,weights_error=community_profiles(session)
    items,_,_=build(rows,{'listings':{}},{},now,profiles,reference_rows=reference+historical_rows)
    unid_estimates=apply_unidentified_comparables(items,rows+reference+historical_rows)
    configured=bool(os.environ.get('UPSTASH_REDIS_REST_URL') and os.environ.get('UPSTASH_REDIS_REST_TOKEN'))
    stored_bases, indexed_at, cache_error = read_cached_price_index() if configured else ({}, None, 'not configured')
    if stored_bases and indexed_at and time.time() - indexed_at < 1200:
        bases, source, errors = stored_bases, 'Redis item price index (fresh)', []
        index_write_error = None
    else:
        fresh_bases, source, errors = index_data(session,key)
        bases = dict(stored_bases)
        bases.update(fresh_bases)
        index_write_error = save_cached_price_index(bases) if configured and fresh_bases else None
        if not fresh_bases and stored_bases:
            source = 'Redis item price index (stale fallback)'
    bases=dict(bases)
    n_lookup=0
    n_baseline,n_lookup=apply_baselines(items,bases,session,key)
    timings['valuation_and_price_lookups']=round(time.perf_counter()-started-sum(timings.values()),2)
    if configured and n_lookup and not index_write_error:
        index_write_error = save_cached_price_index(bases)
    matched_baselines=apply_status_matched_base_references(items,rows+reference+historical_rows)
    augment(items,reference+historical_rows)
    configured=bool(os.environ.get('UPSTASH_REDIS_REST_URL') and os.environ.get('UPSTASH_REDIS_REST_TOKEN'))
    # Independent Redis writes can run concurrently. Lifecycle updates need
    # only their own listing-ID feed, not the price-history write result.
    lifecycle_rows=rows
    lifecycle_source="v2 market listings"
    if configured and not lifecycle_input(rows):
        lifecycle_rows,lifecycle_source=fetch_lifecycle_identifiers(session,key)
    lifecycle_write_error=None
    lifecycle_counts=None
    lifecycle_entries={}
    lifecycle_read_error=None
    lifecycle_metrics={'tracked_items':0,'entries_7d':0,'entries_30d':0,'reobserved':0,'price_changes':0,'price_reductions':0,'persistent':0}
    saved_count=None
    history_write_error=None
    def lifecycle_work():
        counts,error,_=update_lifecycle(lifecycle_rows,False,now.timestamp())
        entries,read_error=read_lifecycle()
        return counts,error,entries,read_error
    history_start=time.perf_counter()
    if configured:
        with ThreadPoolExecutor(max_workers=2) as executor:
            history_future=executor.submit(persist_history,rows)
            lifecycle_future=executor.submit(lifecycle_work)
            saved_count,history_write_error=history_future.result()
            lifecycle_counts,lifecycle_write_error,lifecycle_entries,lifecycle_read_error=lifecycle_future.result()
        if lifecycle_read_error is None:
            lifecycle_metrics=lifecycle_summary(items,lifecycle_entries,now,complete_scan)
    timings['history_and_lifecycle']=round(time.perf_counter()-history_start,2)
    # Enabled means Redis acknowledged the write, not just that keys exist.
    durable=configured and saved_count is not None and history_read_error is None
    history_total=saved_count if saved_count is not None else len(prior)
    apply_trade_costs(items)
    tentative_count=add_tentative_price_ranges(items,rows+reference+historical_rows)
    suppress_trivial_leads(items)
    assess_sellability(items,rows,reference+historical_rows)
    mark_observation_freshness(items,now)
    trends=trend_summaries(stored,lifecycle_entries,now.timestamp())
    roll_matches=v17_roll_evidence(items,rows,historical_rows,profiles)
    for item in items:
        item['v16_trend']=trends.get(str(item.get('name') or '').strip().casefold(), {'asking_price_trend':'insufficient'})
        item['v16_scenarios']=conservative_scenarios(item)
        item['v161_liquidity']=liquidity_evidence(item)
    qualified_flips = qualify_items(items)
    items.sort(key=lambda x:x.get('listing_age_minutes') if x.get('listing_age_minutes') is not None else 1e10)
    timings['total']=round(time.perf_counter()-started,2)
    return {'status':{'version':'17.2','scan_timings_seconds':timings,'updated_at':now.isoformat(),'scanned':len(rows),'exported':len(items),
       'recent_observations':sum(i.get('observation_freshness')=='recent' for i in items),
       'verification_needed':sum(i.get('observation_freshness')=='verify' for i in items),
       'stale_or_unknown':sum(i.get('observation_freshness') in ('stale','unknown') for i in items),
       'newest_observation_age_minutes':min((i['listing_age_minutes'] for i in items if i.get('listing_age_minutes') is not None),default=None),
       'reference_offers':len(reference),
       'lifecycle_tracked_listings':lifecycle_counts.get('tracked') if lifecycle_counts else len(lifecycle_entries),
       'lifecycle_eligible_scan_ids':len(lifecycle_input(lifecycle_rows)),
       'lifecycle_identifier_source':lifecycle_source,
       'lifecycle_new_on_scan':lifecycle_counts.get('new_count') if lifecycle_counts else None,
       'lifecycle_reobserved_on_scan':lifecycle_counts.get('repeat_count') if lifecycle_counts else None,
       'lifecycle_new_7d':lifecycle_metrics['entries_7d'],
       'lifecycle_reobserved':lifecycle_metrics['reobserved'],
       'lifecycle_new_30d':lifecycle_metrics['entries_30d'],
       'lifecycle_price_changes':lifecycle_metrics['price_changes'],
       'lifecycle_price_reductions':lifecycle_metrics['price_reductions'],
       'lifecycle_persistent':lifecycle_metrics['persistent'],
       'lifecycle_read_status':'ok' if lifecycle_read_error is None and configured else 'unavailable',
       'lifecycle_complete_scan':False,
       'lifecycle_write_error':lifecycle_write_error,'lifecycle_read_error':lifecycle_read_error,
       'lifecycle_available':configured and lifecycle_write_error is None and lifecycle_read_error is None,
       'pricing_index_cached_items':len(bases),'pricing_index_read_error':cache_error,'pricing_index_write_error':index_write_error,'durable_history_offers':history_total,'persistent_history':durable,
       'history_configured':configured,'history_read_error':history_read_error,
       'history_write_error':history_write_error,
       'unidentified_resale_estimates':unid_estimates,'tentative_price_ranges':tentative_count,'baseline_items':len(bases),'baseline_estimates':n_baseline,'status_matched_base_estimates':matched_baselines,'baseline_source':source,
       'baseline_errors':errors,'individual_price_lookups':n_lookup,
       'price_reference_coverage':sum(x.get('fair_e') is not None for x in items),
       'price_reference_coverage_pct':round(100*sum(x.get('fair_e') is not None for x in items)/len(items),1) if items else 0,
       'resale_coverage':sum(x.get('price_kind') in ('conservative_roll_asking_reference','unidentified_asking_reference') for x in items),
       'tentative_range_coverage':sum(bool(x.get('tentative_range_e')) for x in items),
       'no_price_evidence':sum(x.get('fair_e') is None and not x.get('tentative_range_e') for x in items),
       'low_value_suppressed':sum(bool(i.get('low_value_filtered')) for i in items),
       'roll_opportunities':sum(i.get('v12_signal')=='roll_opportunity' for i in items),
       'v172_qualified_flips':qualified_flips,
       'research_priority':sum(i.get('v13_recommendation')=='research_priority' and not i.get('low_value_filtered') for i in items),
       'speculative_leads':sum(i.get('v13_recommendation')=='speculative' and not i.get('low_value_filtered') for i in items),
       'research_candidates':sum(i.get('v12_signal')=='research' for i in items),
       'matched_profiles':sum(bool(i.get('wynnpool')) for i in items),
       'v17_weight_source':weights_source,'v17_weight_error':weights_error,
       'v17_profiles_loaded':len(profiles),'v17_roll_comparable_items':roll_matches,
       'partial_scan':not complete_scan,
       'notes':'Lifecycle counts are observations, not sales. Incomplete scans never imply a listing disappeared.'},
       'listings':items}


# V16 analysis is derived from previously persisted observations, never sale records.
def trend_summaries(history, lifecycle, now_s=None):
    """7d versus preceding 7d snapshots, retaining absence of evidence explicitly."""
    now_s = now_s or time.time()
    periods = defaultdict(lambda: [[], []])
    observations = history.get('observations', {}) if isinstance(history, dict) else {}
    for r in observations.values():
        if not isinstance(r, dict): continue
        name = str(r.get('name') or '').strip().casefold()
        price = number(r.get('listing_price'))
        seen = number(r.get('observed_at'))
        if not name or price is None or price <= 0 or seen is None: continue
        days = (now_s-seen)/86400
        if 0 <= days < 14:
            periods[name][0 if days < 7 else 1].append(float(price))
    entries = lifecycle if isinstance(lifecycle, dict) else {}
    newcomers = defaultdict(lambda: [0,0])
    for r in entries.values():
        if not isinstance(r, dict): continue
        name = str(r.get('name') or '').strip().casefold()
        seen = number(r.get('first_seen'))
        if not name or seen is None: continue
        days = (now_s-seen)/86400
        if 0 <= days < 14:
            newcomers[name][0 if days < 7 else 1] += 1
    result={}
    for name in periods.keys() | newcomers.keys():
        current, previous = periods[name]
        now_new, prev_new = newcomers[name]
        enough = len(current)>=5 and len(previous)>=5
        median_now = round(statistics.median(current)) if current else None
        median_before = round(statistics.median(previous)) if previous else None
        delta = round(100*(median_now/median_before-1),1) if enough and median_before else None
        # Never call observed arrivals a global supply estimate: scan coverage varies.
        label = ('insufficient' if not enough else 'falling' if delta < -10 else 'rising' if delta > 10 else 'stable')
        result[name] = {'current_7d_offers':len(current),'previous_7d_offers':len(previous),
                        'median_ask_current_e':median_now,'median_ask_previous_e':median_before,
                        'asking_price_change_pct':delta,'asking_price_trend':label,
                        'observed_new_ids_7d':now_new,'observed_new_ids_prior_7d':prev_new,
                        'supply_comparison_supported':prev_new>=5 and now_new>=5,
                        'notes':'Asking prices and first-observed listings, not completed sales or total supply.'}
    return result


def conservative_scenarios(item):
    """Quantile-style scenarios are *asking-price* sensitivities, not sale forecasts."""
    buy=number(item.get('price_e'))
    estimate=number(item.get('resale_estimate_e'))
    low=None
    supported = item.get('price_kind') in ('conservative_roll_asking_reference','unidentified_asking_reference')
    if not supported or buy is None or estimate is None or estimate<=0:return {'supported':False,'reason':'No roll/UNID-matched resale asking reference'}
    comps = int(number(item.get('comparables')) or 0)
    if comps < 3:return {'supported':False,'reason':'Too few comparable offers for scenarios'}
    # Sensitivity bands around an asking reference, not probabilistic intervals.
    lower=round(estimate*0.75)
    upper=round(estimate*1.10)
    qty=max(1,int(number(item.get('quantity')) or 1))
    fee=1.05 if item.get('price_assumption') == 'buyer_inclusive' else 1.0
    purchase=number(item.get('purchase_total_e'))
    if purchase is None:return {'supported':False,'reason':'Missing purchase cost'}
    profits=[round(ref*qty/fee-purchase) for ref in (lower,estimate,upper)]
    return {'supported':True,'kind':'asking_price_sensitivity_not_sale_prediction',
            'low_e':lower,'reference_e':round(estimate),'high_e':upper,
            'net_profit_low_e':profits[0],'net_profit_reference_e':profits[1],
            'net_profit_high_e':profits[2],'comparable_count':comps,
            'warning':'Illustrative 75%-110% sensitivity around matched asking reference; not a verified sale-price range.'}

# V15 lootrun intelligence runs independently of the market scan and pricing keys.
def pool_names(payload):
    """Extract only explicit item-name fields; do not mistake regions for items.

    WynnVentory's documentation does not promise one fixed processed JSON shape.
    Return sorted names and an extraction confidence flag.
    """
    names=set()
    def walk(value, depth=0, item_context=False):
        if depth>9:return
        if isinstance(value,list):
            for v in value[:3000]:walk(v,depth+1,item_context)
        elif isinstance(value,dict):
            for k in ('name','item_name','itemName'):
                name=value.get(k)
                if isinstance(name,str) and 1<len(name)<90 and name.strip():
                    # Require a plausible item descriptor, not a region/week label.
                    if any(t in value for t in ('rarity','type','item_type','itemType','amount','quantity','tier','category')) or item_context:
                        names.add(name.strip())
            for key,v in value.items():
                if key in ('name','item_name','itemName','description','lore','icon','timestamp','date','created_at'):
                    continue
                if isinstance(v,(dict,list)):
                    walk(v,depth+1,item_context or key in ('items','rewards','mythics','lootpool_items','lootPoolItems'))
    walk(payload)
    return sorted(names,key=str.casefold)

def lootpool_fetch(session,key):
    now=datetime.now(timezone.utc)
    week=now.isocalendar()
    result={'version':'17.1','iso_year':week.year,'iso_week':week.week,'updated_at':now.isoformat(),
            'available':False,'items':[],'added':[],'removed':[],
            'source':'WynnVentory weekly lootpool','error':None,'historical_comparison':'unavailable',
            'warning':'Lootpool presence indicates potential incoming supply, not sales or future prices.'}
    if not key:
        result['error']='WYNNVENTORY_API_KEY is not configured';return result
    headers={'X-API-Key':key}
    def read(path):
        r=session.get(BASE+path,headers=headers,timeout=8)
        if r.status_code in (401,403):return None,'Missing read:lootpool permission (HTTP '+str(r.status_code)+')'
        if r.status_code==404:return None,'Lootpool data not published for this week'
        r.raise_for_status()
        return r.json(),None
    try:
        current,error=read('/api/lootpool/items')
        if error:
            result['error']=error;return result
        names=pool_names(current)
        if not names:
            result['error']='No recognisable item names in lootpool response (format may differ)'
            return result
        result.update(available=True,items=names,source='WynnVentory processed current lootpool')
        # Historical comparison is optional and must never compromise the current-week view.
        previous=now.date().fromordinal(now.date().toordinal()-7).isocalendar()
        old,old_error=read('/api/lootpool/'+str(previous.year)+'/'+str(previous.week))
        if old_error is None:
            old_names=pool_names(old)
            if old_names:
                cur={x.casefold():x for x in names}; prior={x.casefold():x for x in old_names}
                result['added']=[cur[k] for k in sorted(cur.keys()-prior.keys())]
                result['removed']=[prior[k] for k in sorted(prior.keys()-cur.keys())]
                result['historical_comparison']='previous week compared'
            else:result['historical_comparison']='previous week format unknown'
        else:result['historical_comparison']=old_error
    except (requests.RequestException,ValueError,TypeError) as e:
        result['error']='Lootpool feed failed ('+type(e).__name__+')'
    return result

# V16.1: protected, uncached HTTP trigger for cron-job.org.
# Redis SET NX EX makes overlap prevention work across separate Vercel workers.
CRON_LOCK_KEY = 'wynnflip:v161:cron:lock'
CRON_STATUS_KEY = 'wynnflip:v161:cron:last_success'
CRON_LOCK_SECONDS = 180
CRON_RELEASE_LUA = "if redis.call('GET',KEYS[1]) == ARGV[1] then return redis.call('DEL',KEYS[1]) else return 0 end"


def cron_storage_status():
    values, error = redis_call([['GET', CRON_STATUS_KEY]])
    if error:
        return {'available': False, 'error': 'cloud status unavailable'}
    try:
        record = json.loads(values[0]) if values and values[0] else None
        return {'available': True, 'last_success': record}
    except (ValueError, TypeError, IndexError):
        return {'available': False, 'error': 'invalid status record'}


@app.get('/api/cron/status')
def cron_status():
    # Never expose tokens, scheduler headers, listing IDs or Redis credentials.
    return JSONResponse(cron_storage_status(), headers={'Cache-Control': 'no-store'})


@app.post('/api/cron/scan')
def cron_scan(x_wynnflip_cron_secret: str | None = Header(default=None)):
    expected = os.getenv('WYNNFLIP_CRON_SECRET', '').strip()
    if not expected:
        return JSONResponse({'error': 'Scheduled scan is not configured'}, status_code=503,
                            headers={'Cache-Control': 'no-store'})
    if not x_wynnflip_cron_secret or not hmac.compare_digest(x_wynnflip_cron_secret, expected):
        return JSONResponse({'error': 'Unauthorized'}, status_code=401,
                            headers={'Cache-Control': 'no-store'})
    key = os.getenv('WYNNVENTORY_API_KEY', '').strip()
    if not key or not (os.getenv('UPSTASH_REDIS_REST_URL') and os.getenv('UPSTASH_REDIS_REST_TOKEN')):
        return JSONResponse({'error': 'Market API or cloud history storage not configured'}, status_code=503,
                            headers={'Cache-Control': 'no-store'})
    nonce = secrets.token_hex(20)
    acquired, err = redis_call([['SET', CRON_LOCK_KEY, nonce, 'NX', 'EX', CRON_LOCK_SECONDS]])
    if err:
        return JSONResponse({'error': 'Cannot acquire Redis scan lock'}, status_code=503,
                            headers={'Cache-Control': 'no-store'})
    if not acquired or acquired[0] != 'OK':
        return JSONResponse({'status': 'already_running'}, status_code=200,
                            headers={'Cache-Control': 'no-store'})
    try:
        # Direct call: bypass the CDN and in-process response cache entirely.
        data = fetch_market(key)
        status = data.get('status', {})
        if (not status.get('persistent_history') or not status.get('lifecycle_available')
                or status.get('history_write_error') or status.get('lifecycle_write_error')):
            return JSONResponse({'error': 'History or lifecycle write unverified'}, status_code=503,
                                headers={'Cache-Control': 'no-store'})
        record = {'at': status.get('updated_at'),
                  'listing_count': status.get('scanned'),
                  'history_count': status.get('durable_history_offers'),
                  'lifecycle_count': status.get('lifecycle_tracked_listings'),
                  'seconds': status.get('scan_timings_seconds', {}).get('total')}
        _, status_err = redis_call([['SET', CRON_STATUS_KEY,
                                     json.dumps(record, separators=(',', ':'))]])
        if status_err:
            return JSONResponse({'error': 'History saved but status record failed'}, status_code=503,
                                headers={'Cache-Control': 'no-store'})
        return JSONResponse({'status': 'ok', **record}, headers={'Cache-Control': 'no-store'})
    except requests.HTTPError:
        return JSONResponse({'error': 'Upstream market HTTP failure'}, status_code=502,
                            headers={'Cache-Control': 'no-store'})
    except (requests.RequestException, ValueError, TypeError, KeyError, IndexError):
        return JSONResponse({'error': 'Market scan failed'}, status_code=502,
                            headers={'Cache-Control': 'no-store'})
    finally:
        # Ownership-aware release: never unlock another invocation's lease.
        redis_call([['EVAL', CRON_RELEASE_LUA, 1, CRON_LOCK_KEY, nonce]])


@app.get('/api/lootpool')
def lootpool():
    key=os.environ.get('WYNNVENTORY_API_KEY','')
    return cached('v15:lootpool',600,lambda:lootpool_fetch(requests.Session(),key))

@app.get('/api/health')
def health():
    return {'version':'15.2','key_configured':bool(os.getenv('WYNNVENTORY_API_KEY')),
            'persistent_history_configured':bool(os.getenv('UPSTASH_REDIS_REST_URL') and os.getenv('UPSTASH_REDIS_REST_TOKEN'))}

@app.get('/api/market')
@app.get('/api/market.py')
def market():
    key=os.getenv('WYNNVENTORY_API_KEY','').strip()
    if not key:return JSONResponse({'error':'WYNNVENTORY_API_KEY missing'},status_code=503)
    try:return JSONResponse(fetch_market(key),headers={'Cache-Control':'public, s-maxage=30, stale-while-revalidate=30'})
    except requests.HTTPError as e:
        code=e.response.status_code if e.response is not None else None
        return JSONResponse({'error':'Upstream WynnVentory HTTP '+str(code)},status_code=503 if code==429 else 502,headers={'Cache-Control':'no-store'})
    except (requests.RequestException,ValueError,TypeError,KeyError) as e:
        return JSONResponse({'error':'Market data failure','detail':type(e).__name__},status_code=502,headers={'Cache-Control':'no-store'})

@app.get('/')
def home():return FileResponse(SITE/'index.html',media_type='text/html')

@app.get('/{asset:path}')
def static_file(asset:str):
    p=(SITE/asset).resolve()
    if not p.is_relative_to(SITE.resolve()) or not p.is_file():raise HTTPException(404)
    return FileResponse(p)
