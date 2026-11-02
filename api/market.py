"""WynnFlip V13.0: durable market observations and cached item baselines."""
import os, time, json, statistics, threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from scanner import build, extract_listing_page, load_profiles, number, identification_status, identity
import scanner as _scanner

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
local keyed={}
for id,val in pairs(obs) do
  table.insert(keyed,{id=id,t=tonumber(val.observed_at or 0) or 0})
end
table.sort(keyed,function(a,b) if a.t==b.t then return a.id<b.id else return a.t>b.t end end)
for j=8001,#keyed do obs[keyed[j].id]=nil end
redis.call('SET',KEYS[1],cjson.encode(current))
return math.min(#keyed,8000)
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
    v={name=r.name,first_seen=now,last_seen=now,scans_seen=1,initial_price=r.price,latest_price=r.price,price_changes=0,misses=0}
    entries[id]=v;new_count=new_count+1
  else
    -- Track observations only when genuinely processed by a new scan.
    if now>tonumber(v.last_seen or 0) then
      v.scans_seen=math.min(100000,tonumber(v.scans_seen or 1)+1)
      repeat_count=repeat_count+1
    end
    if r.price and v.latest_price and tonumber(r.price)~=tonumber(v.latest_price) then
      v.price_changes=math.min(100000,tonumber(v.price_changes or 0)+1)
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
local ordered={}
for id,v in pairs(entries) do
  table.insert(ordered,{id=id,t=tonumber(v.last_seen or 0)})
end
table.sort(ordered,function(a,b) if a.t==b.t then return a.id<b.id else return a.t>b.t end end)
for i=12001,#ordered do entries[ordered[i].id]=nil end
old.updated_at=now
redis.call('SET',KEYS[1],cjson.encode(old),'EX',86400*45)
return cjson.encode({new_count=new_count,repeat_count=repeat_count,tracked=math.min(#ordered,12000)})
"""

def lifecycle_input(rows):
    """Stable identifiers only; fabricated IDs must not become turnover evidence."""
    incoming={}
    for row in rows:
        listing_id=row.get('hash_code')
        price=price_e(row)
        if not listing_id or not row.get('name') or price is None or price<=0:
            continue
        listing_id=str(listing_id)
        if listing_id.startswith('archive:') or len(listing_id)>180:
            continue
        incoming[listing_id]={'name':str(row['name'])[:100], 'price':price}
    return incoming

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
        eligible=sum(last(v)>=current-7*86400 for v in group)
        missing=sum(int(number(v.get('misses')) or 0)>=2 for v in group) if complete else None
        # Unknown for incomplete scans: pagination absence is NOT turnover.
        stats[name]={'new_listings_7d':n7,'new_listings_30d':n30,
                     'reobserved_listings':retained,'recently_observed_listings':eligible,
                     'unobserved_complete_scans':missing,
                     'confidence':'early' if n30<5 else 'developing' if n30<20 else 'established',
                     'verified_sales':0,'sale_rate':None}
    for item in items:
        key=str(item.get('name') or '').casefold().strip()
        item['lifecycle']=stats.get(key,{'new_listings_7d':0,'new_listings_30d':0,
            'reobserved_listings':0,'recently_observed_listings':0,
            'unobserved_complete_scans':None,'confidence':'insufficient',
            'verified_sales':0,'sale_rate':None})
    return {'tracked_items':len(by_name),'entries_7d':sum(0<=current-float(number(e.get('first_seen')) or 0)<=7*86400 for e in entries.values() if isinstance(e,dict)),
            'reobserved':sum(int(number(e.get('scans_seen')) or 0)>=2 for e in entries.values() if isinstance(e,dict))}

def apply_baselines(items,baselines,session,key):
    filled=0; lookups=0; groups=defaultdict(list)
    for item in items:groups[str(item.get('name','')).strip().casefold()].append(item)
    # Per-item lookups only for limited number of distinct names, never hundreds per refresh.
    missing=[]
    for name,group in groups.items():
        if not name:continue
        if (name,'') not in baselines and not any(item.get('fair_e') for item in group):
            missing.append(name)
    for name in missing[:12]:
        record,_origin=cached_item_price(session,key,name)
        lookups+=1
        if record:baselines[(name,'')]=record
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



def add_tentative_price_ranges(items, historical_rows):
    """Expose tentative same-status asking ranges, never implied resale profits."""
    count = 0
    for item in items:
        item['tentative_range_e'] = None
        item['tentative_sample_count'] = 0
        if item.get('resale_estimate_e') is not None:
            continue
        offers = status_matched_offers(item, historical_rows)
        if not offers:
            continue
        prices = sorted(offers.values())
        # For two+ independent offers exclude egregious outliers where possible.
        if len(prices) >= 3:
            median = statistics.median(prices)
            trimmed = [p for p in prices if 0.4 * median <= p <= 2.5 * median]
            if len(trimmed) >= 2:
                prices = trimmed
        item['tentative_range_e'] = {'low': prices[0], 'high': prices[-1]}
        item['tentative_sample_count'] = len(prices)
        item['tentative_range_kind'] = ('UNID-only historical asking prices' if item.get('identification_status') == 'unidentified'
                                        else 'Same-item identified asks; rolls not adjusted')
        count += 1
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
    session=session or requests.Session();rows=[];pages=None
    for page in range(1,limit_pages+1):
        data=get_json(session,BASE+'/api/v2/market/listings',key,
         {'page':page,'page_size':page_size,'sort':'timestamp_desc'},timeout=12)
        batch,pages=extract_listing_page(data);rows.extend(batch)
        if len(batch)<page_size or (pages is not None and page>=int(pages)):break
    rows = deduplicate_recent(rows)
    if not rows:raise ValueError('No market listings returned')
    now=datetime.now(timezone.utc)
    # Only complete pagination is eligible for absence statistics.
    complete_scan=bool((pages is not None and page>=int(pages)) or len(batch)<page_size)
    reference=snapshot(session)
    stored,history_read_error=load_history()
    # Bounded historical distinct observations, stored as explicit comparison-only data.
    prior=stored.get('observations',{}) if isinstance(stored.get('observations'),dict) else {}
    historical_rows=list(prior.values())[:8000]
    profiles=load_profiles(ROOT/'profiles/item_weights.json')
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
    if configured and n_lookup and not index_write_error:
        index_write_error = save_cached_price_index(bases)
    matched_baselines=apply_status_matched_base_references(items,rows+reference+historical_rows)
    augment(items,reference+historical_rows)
    configured=bool(os.environ.get('UPSTASH_REDIS_REST_URL') and os.environ.get('UPSTASH_REDIS_REST_TOKEN'))
    saved_count=None;history_write_error=None
    if configured:
        saved_count,history_write_error=persist_history(rows)
    # Enabled means Redis acknowledged the write; merely having environment
    # variables configured is not sufficient evidence that history works.
    durable=configured and saved_count is not None and history_read_error is None
    if saved_count is not None:
        history_total=saved_count
    else:
        history_total=len(prior)
    apply_trade_costs(items)
    tentative_count=add_tentative_price_ranges(items,rows+reference+historical_rows)
    suppress_trivial_leads(items)
    assess_sellability(items,rows,reference+historical_rows)
    mark_observation_freshness(items,now)
    lifecycle_write_error=None
    lifecycle_counts=None
    lifecycle_entries={}
    lifecycle_read_error=None
    lifecycle_metrics={'tracked_items':0,'entries_7d':0,'reobserved':0}
    if configured:
        lifecycle_counts,lifecycle_write_error,_=update_lifecycle(rows,complete_scan,now.timestamp())
        lifecycle_entries,lifecycle_read_error=read_lifecycle()
        if lifecycle_read_error is None:
            lifecycle_metrics=lifecycle_summary(items,lifecycle_entries,now,complete_scan)
    items.sort(key=lambda x:x.get('listing_age_minutes') if x.get('listing_age_minutes') is not None else 1e10)
    return {'status':{'version':'14.0','updated_at':now.isoformat(),'scanned':len(rows),'exported':len(items),
       'recent_observations':sum(i.get('observation_freshness')=='recent' for i in items),
       'verification_needed':sum(i.get('observation_freshness')=='verify' for i in items),
       'stale_or_unknown':sum(i.get('observation_freshness') in ('stale','unknown') for i in items),
       'newest_observation_age_minutes':min((i['listing_age_minutes'] for i in items if i.get('listing_age_minutes') is not None),default=None),
       'reference_offers':len(reference),
       'lifecycle_tracked_listings':lifecycle_counts.get('tracked') if lifecycle_counts else len(lifecycle_entries),
       'lifecycle_new_on_scan':lifecycle_counts.get('new_count') if lifecycle_counts else None,
       'lifecycle_reobserved_on_scan':lifecycle_counts.get('repeat_count') if lifecycle_counts else None,
       'lifecycle_new_7d':lifecycle_metrics['entries_7d'],
       'lifecycle_reobserved':lifecycle_metrics['reobserved'],
       'lifecycle_complete_scan':complete_scan,
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
       'research_priority':sum(i.get('v13_recommendation')=='research_priority' and not i.get('low_value_filtered') for i in items),
       'speculative_leads':sum(i.get('v13_recommendation')=='speculative' and not i.get('low_value_filtered') for i in items),
       'research_candidates':sum(i.get('v12_signal')=='research' for i in items),
       'matched_profiles':sum(bool(i.get('wynnpool')) for i in items),
       'partial_scan':not complete_scan,
       'notes':'Lifecycle counts are observations, not sales. Incomplete scans never imply a listing disappeared.'},
       'listings':items}

@app.get('/api/health')
def health():
    return {'version':'14.0','key_configured':bool(os.getenv('WYNNVENTORY_API_KEY')),
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
