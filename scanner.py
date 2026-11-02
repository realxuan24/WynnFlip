"""WynnFlip V3: public-data market explorer and cautious deal screening.
All source prices are emerald integers; no player automation or trading.
"""
import hashlib
import json
import math
import os
import statistics
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

API = 'https://wynnventory.com/api/v2'
DATA = Path('data')
SITE = Path('site')
LE = 4096


def read(path, default):
    try:
        return json.loads(path.read_text('utf-8'))
    except (OSError, ValueError):
        return default


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False), encoding='utf-8')
    temp.replace(path)


def get(session, path, key, params=None):
    for attempt in range(4):
        resp = session.get(API + path, headers={'X-API-Key': key}, params=params, timeout=35)
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < 3:
            time.sleep(3 * (attempt + 1))
            continue
        resp.raise_for_status()
        return resp.json()


def number(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def int_emeralds(v):
    n = number(v)
    if n is None or n < 0 or not n.is_integer():
        return None
    return int(n)


def unit_price(row):
    total = int_emeralds(row.get('listing_price'))
    count = number(row.get('amount')) or 1
    if total is None or count <= 0:
        return None
    # Market may list stacks. Express unit price as a floating reference only.
    return total / count


def variant(row):
    return '|'.join(str(row.get(k) if row.get(k) is not None else '').lower() for k in
                    ('name', 'item_type', 'subtype', 'tier', 'unidentified', 'shiny_stat'))


def listing_id(row):
    if row.get('hash_code'):
        return str(row['hash_code'])
    keys = ('name', 'listing_price', 'amount', 'timestamp', 'stat_rolls', 'overall_roll', 'tier')
    return hashlib.sha256(json.dumps([row.get(k) for k in keys], default=str, sort_keys=True).encode()).hexdigest()[:24]


def similarity(a, b):
    aa, bb = a.get('stat_rolls'), b.get('stat_rolls')
    if isinstance(aa, dict) and isinstance(bb, dict):
        common = [abs(number(aa[k]) - number(bb[k])) for k in aa.keys() & bb.keys()
                  if number(aa[k]) is not None and number(bb[k]) is not None]
        if len(common) >= 2:
            return max(.03, math.exp(-statistics.mean(common)/16) * min(1, len(common)/4))
    x, y = number(a.get('overall_roll')), number(b.get('overall_roll'))
    if x is not None and y is not None:
        return max(.05, .5 * math.exp(-abs(x-y)/13))
    return .25 if not aa and not bb else .1


def classify(row, group, history):
    buy = unit_price(row)
    if buy is None or buy <= 0:
        return {'category': 'unpriced', 'warnings': ['Price unavailable']}
    comps = [(unit_price(p), similarity(row, p)) for p in group if listing_id(p) != listing_id(row)]
    comps = [(p, w) for p, w in comps if p is not None and p > 0 and w >= .12]
    if len(comps) < 4:
        return {'category': 'unrated', 'warnings': ['Not enough comparable listings'], 'comparables': len(comps)}
    peer_prices = [p for p, _ in comps]
    med = statistics.median(peer_prices)
    comps = [(p, w) for p, w in comps if .4 * med <= p <= 2.5 * med]
    if len(comps) < 4:
        return {'category': 'unrated', 'warnings': ['Comparables disagree'], 'comparables': len(comps)}
    comps.sort(key=lambda t: t[0])
    threshold = sum(w for _, w in comps) * .3
    total = 0
    floor = comps[-1][0]
    for p, w in comps:
        total += w
        if total >= threshold:
            floor = p
            break
    all_prices = [unit_price(p) for p in group]
    all_prices = [p for p in all_prices if p is not None and p > 0]
    center = statistics.median(all_prices)
    spread = min(1.5, 1.4826 * statistics.median(abs(p-center) for p in all_prices) / max(1, center))
    past = [number(h.get('median_ask_e')) for h in history]
    past = [p for p in past if p is not None and p > 0]
    anchor = statistics.median(past) if past else None
    changes = [abs(past[i]/past[i-1]-1) for i in range(1,len(past)) if past[i-1] > 0]
    volatility = statistics.median(changes) if changes else None
    base = min(floor, anchor*1.03) if anchor else floor
    risk = min(.42, .08 + .4*spread + (1.2*min(.15,volatility) if volatility is not None else .08))
    fair = max(0, math.floor(base*(1-risk)))
    profit = fair-buy
    roi = 100*profit/buy
    confidence = max(.05, min(.95, .18+.035*min(len(comps),12)+.03*min(len(past),8)-.35*spread-(min(.2,volatility) if volatility is not None else .1)))
    stability = 'unknown' if len(past)<3 else 'volatile' if volatility is not None and volatility>=.12 else 'moderate' if volatility is not None and volatility>=.045 else 'stable'
    warnings = []
    if len(past)<3: warnings.append('Limited history')
    if spread>.35: warnings.append('Wide price spread')
    if stability=='volatile': warnings.append('High volatility')
    if len(comps)<7: warnings.append('Few comparable offers')
    if not row.get('stat_rolls') and row.get('item_type')=='gear': warnings.append('Roll comparison incomplete')
    score = max(0, profit/LE) * math.sqrt(max(0, roi)/100) * confidence * (1-min(.8,spread)) * (1-min(.8,(volatility or .07)*3))
    if profit <= 0 or roi < 10:
        category = 'market'
    elif stability=='volatile' or spread>.35 or len(past)<3 or confidence<.45:
        category = 'watch'
    elif confidence>=.45 and profit >= .5*LE and roi>=25:
        category = 'candidate'
    else:
        category = 'potential'
    return dict(category=category, fair_e=fair, profit_e=round(profit), roi=round(roi,1),
                score=round(score,3), confidence=round(confidence,3), comparables=len(comps),
                history_points=len(past), dispersion=round(spread,3),
                volatility=round(volatility,3) if volatility is not None else None,
                stability=stability, warnings=warnings)


def process(listings, history, seen, now):
    groups = defaultdict(list)
    for row in listings:
        if row.get('name') and unit_price(row) is not None:
            groups[variant(row)].append(row)
    hist_index = defaultdict(list)
    cutoff = (datetime.now(timezone.utc)-timedelta(days=14)).isoformat()
    for h in history:
        if h.get('at', '') >= cutoff:
            hist_index[h.get('variant')].append(h)
    records = []
    previous = set(seen.get('ids', []))
    for key, rows in groups.items():
        past = hist_index[key]
        for row in rows:
            price = int_emeralds(row.get('listing_price'))
            if price is None:
                continue
            rid = listing_id(row)
            assessment = classify(row, rows, past)
            records.append(dict(id=rid, name=str(row.get('name') or ''), rarity=row.get('rarity'),
                                item_type=row.get('item_type'), subtype=row.get('subtype'), tier=row.get('tier'),
                                quantity=row.get('amount') or 1, price_e=price, unit_price_e=unit_price(row),
                                overall_roll=row.get('overall_roll'), stat_rolls=row.get('stat_rolls') if isinstance(row.get('stat_rolls'),dict) else {},
                                shiny=bool(row.get('shiny_stat')), timestamp=row.get('timestamp'),
                                new=bool(seen.get('initialized')) and rid not in previous, **assessment))
    records.sort(key=lambda v:(v.get('score',0),v.get('profit_e',0)),reverse=True)
    for key, rows in groups.items():
        vals = [unit_price(r) for r in rows]
        vals = [v for v in vals if v is not None and v > 0]
        if vals:
            history.append({'at':now,'variant':key,'name':rows[0].get('name'),
                            'median_ask_e':round(statistics.median(vals)), 'count':len(vals)})
    cutoff30=(datetime.now(timezone.utc)-timedelta(days=30)).isoformat()
    history=[h for h in history if h.get('at','')>=cutoff30][-45000:]
    return records,history


def main():
    key=os.environ.get('WYNNVENTORY_API_KEY','').strip()
    if not key:
        raise SystemExit('Missing WYNNVENTORY_API_KEY')
    session=requests.Session()
    max_pages=max(1,min(100,int(os.environ.get('MAX_PAGES','20'))))
    listings=[]
    complete=False
    total_pages=None
    pages=0
    for page in range(1,max_pages+1):
        payload=get(session,'/market/listings',key,{'page':page,'page_size':200,'sort':'timestamp_desc'})
        batch=payload.get('data')
        if not isinstance(batch,list):
            raise RuntimeError('Expected data array from market/listings')
        listings.extend(r for r in batch if isinstance(r,dict))
        pages=page
        pagination=payload.get('pagination') or {}
        total_pages=pagination.get('total_pages')
        if (total_pages is not None and page>=int(total_pages)) or len(batch)<200:
            complete=True
            break
        time.sleep(.3)
    now=datetime.now(timezone.utc).isoformat()
    history=read(DATA/'history.json',[])
    # V2 history stored LE; normalize it for V3 so previous scans remain useful.
    for h in history:
        if 'median_ask_e' not in h and h.get('median_ask_le') is not None:
            n=number(h['median_ask_le'])
            if n is not None: h['median_ask_e']=round(n*LE)
    seen=read(DATA/'seen.json',{'ids':[],'initialized':False})
    records,history=process(listings,history,seen,now)
    pool={"state":"unavailable","items":[],"updated_at":now}
    try:
        raw=get(session,'/lootpools/current/items',key)
        pool={"state":"available","data":raw.get('data'),"updated_at":now,
              "note":"Current pool only. Pool membership is not a price forecast."}
    except requests.RequestException as exc:
        pool['note']=f'Pool endpoint unavailable ({type(exc).__name__}); check read:lootpool scope.'
    status=dict(version='3.0',updated_at=now,scanned=len(listings),pages=pages,
                total_pages=total_pages,complete=complete,
                candidates=sum(r['category']=='candidate' for r in records),
                potential=sum(r['category']=='potential' for r in records),
                watch=sum(r['category']=='watch' for r in records),
                listings_exported=len(records),
                note='Asking prices, not confirmed sales. API observations can be stale or incomplete.')
    # Publish listing identity/rolls but never seller identities, API keys, or raw payloads.
    write(SITE/'market.json',dict(status=status,listings=records,
                                  history=history[-5000:],lootpool=pool))
    write(DATA/'seen.json',dict(ids=[listing_id(r) for r in listings],initialized=True))
    write(DATA/'history.json',history)
    write(DATA/'status.json',status)
    write(DATA/'lootpool.json',pool)
    print(f"Scan OK V3: {len(listings)} listings, {pages} pages, {status['candidates']} candidates, "
          f"{status['potential']} potential, {status['watch']} watch, complete={complete}")

if __name__ == '__main__':
    main()
