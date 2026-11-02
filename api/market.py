"""WynnFlip V12. Market facts are asking prices, not recorded sales."""
import os, time, json, statistics, threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from scanner import build, extract_listing_page, load_profiles, number

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
             'timestamp':row.get('listing_timestamp'),'unidentified':row.get('unidentified'),'shiny_stat':row.get('shiny_stat')})
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

def store_request(op,key,value=None):
    url=os.environ.get('UPSTASH_REDIS_REST_URL','').rstrip('/')
    token=os.environ.get('UPSTASH_REDIS_REST_TOKEN','')
    if not (url and token):return None
    args=[op,key]+([] if value is None else [value])
    try:
        r=requests.post(url+'/pipeline',headers={'Authorization':'Bearer '+token},json=[args],timeout=4)
        r.raise_for_status(); data=r.json()
        if isinstance(data,list) and data:return data[0].get('result')
    except (requests.RequestException,ValueError,TypeError): pass
    return None

def load_history():
    raw=store_request('GET','wynnflip:v12:history')
    try:
        data=json.loads(raw) if raw else {}
        return data if isinstance(data,dict) else {}
    except (TypeError,ValueError):return {}

def persist_history(data):
    # Best-effort snapshot: suitable for small projects, not atomic under concurrent writers.
    store_request('SET','wynnflip:v12:history',json.dumps(data,separators=(',',':')))

def apply_baselines(items,baselines,session,key):
    filled=0; lookups=0; groups=defaultdict(list)
    for item in items:groups[str(item.get('name','')).strip().casefold()].append(item)
    # Per-item lookups only for limited number of distinct names, never hundreds per refresh.
    missing=[]
    for name,group in groups.items():
        if not name:continue
        if (name,'') not in baselines and not any(item.get('fair_e') for item in group):
            missing.append(name)
    for name in missing[:8]:
        record=per_item(session,key,name)
        lookups+=1
        if record:baselines[(name,'')]=record
    for item in items:
        if item.get('fair_e') is not None:
            item['valuation_source']='roll_comparables';continue
        keyname=str(item.get('name') or '').strip().casefold()
        if not keyname or item.get('unidentified') or item.get('shiny_stat'):continue
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
        has_rolls=item.get('valuation_source')=='roll_comparables'
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
        if not has_rolls:risks.append('no roll-adjusted resale estimate')
        if item.get('listing_age_minutes') is None:risks.append('timestamp unavailable')
        strength='very limited' if sample<3 else 'limited' if sample<8 else 'moderate'
        item['market_intel']={'baseline_discount_pct':round(discount,1) if discount is not None else None,
            'sample':int(sample),'history_similar_prices':same,'spread_proxy_pct':volatility,'evidence':strength,
            'risks':risks,'confirmed_sales':False}
        # Split screening candidates from evidence-qualified roll comparisons.
        if has_rolls and sample>=5 and number(item.get('roi'),0)>=25 and number(item.get('profit_e'),0)>0 and len(risks)<=1:
            item['v12_signal']='roll_opportunity'
        elif (not has_rolls and sample>=3 and discount is not None and 20<=discount<=80 and len(risks)<=2):
            item['v12_signal']='research'
        else:item['v12_signal']='market'
    return items

def fetch_market(key,session=None,limit_pages=2,page_size=100):
    session=session or requests.Session();rows=[];pages=None
    for page in range(1,limit_pages+1):
        data=get_json(session,BASE+'/api/v2/market/listings',key,
         {'page':page,'page_size':page_size,'sort':'timestamp_desc'},timeout=12)
        batch,pages=extract_listing_page(data);rows.extend(batch)
        if len(batch)<page_size or (pages is not None and page>=int(pages)):break
    if not rows:raise ValueError('No market listings returned')
    now=datetime.now(timezone.utc)
    reference=snapshot(session)
    stored=load_history()
    # Bounded historical distinct observations, stored as explicit comparison-only data.
    prior=stored.get('observations',{}) if isinstance(stored.get('observations'),dict) else {}
    historical_rows=list(prior.values())[:3000]
    profiles=load_profiles(ROOT/'profiles/item_weights.json')
    items,_,_=build(rows,{'listings':{}},{},now,profiles,reference_rows=reference+historical_rows)
    bases,source,errors=index_data(session,key)
    bases=dict(bases)
    n_lookup=0
    n_baseline,n_lookup=apply_baselines(items,bases,session,key)
    augment(items,reference+historical_rows)
    # Update durable history only when Redis is configured, up to 3,000 unique offers.
    durable=bool(os.environ.get('UPSTASH_REDIS_REST_URL') and os.environ.get('UPSTASH_REDIS_REST_TOKEN'))
    if durable:
        for r in rows:
            p=number(r.get('listing_price'))
            if not r.get('name') or not p or p<=0:continue
            ident=str(r.get('hash_code') or (str(r.get('name'))+str(p)+str(r.get('timestamp'))))
            if ident not in prior:prior[ident]={'name':r['name'],'listing_price':p,'tier':r.get('tier'),
                  'stat_rolls':r.get('stat_rolls') if isinstance(r.get('stat_rolls'),dict) else {},'hash_code':ident,
                  'timestamp':r.get('timestamp')}
        prior=dict(list(prior.items())[-3000:])
        persist_history({'observations':prior})
    items.sort(key=lambda x:x.get('listing_age_minutes') if x.get('listing_age_minutes') is not None else 1e10)
    return {'status':{'version':'12','updated_at':now.isoformat(),'scanned':len(rows),'exported':len(items),
       'reference_offers':len(reference),'durable_history_offers':len(prior),'persistent_history':durable,
       'baseline_items':len(bases),'baseline_estimates':n_baseline,'baseline_source':source,
       'baseline_errors':errors,'individual_price_lookups':n_lookup,
       'roll_opportunities':sum(i.get('v12_signal')=='roll_opportunity' for i in items),
       'research_candidates':sum(i.get('v12_signal')=='research' for i in items),
       'matched_profiles':sum(bool(i.get('wynnpool')) for i in items),
       'partial_scan':not ((pages is not None and page>=int(pages)) or len(batch)<page_size),
       'notes':'Asking-price comparisons only; sales, turnover and availability unverified.'},
       'listings':items}

@app.get('/api/health')
def health():
    return {'version':'12','key_configured':bool(os.getenv('WYNNVENTORY_API_KEY')),
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
