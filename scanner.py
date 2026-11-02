"""WynnFlip V2: conservative market observer; never operates a game client."""
import hashlib
import json
import math
import os
import statistics as stats
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

API = 'https://wynnventory.com/api/v2'
LE = 4096
DATA = Path('data')
SITE = Path('site')


def load(path, default):
    try:
        return json.loads(path.read_text('utf-8'))
    except (OSError, ValueError):
        return default


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    tmp.replace(path)


def request(session, path, key, params=None):
    for attempt in range(4):
        response = session.get(API + path, headers={'X-API-Key': key}, params=params, timeout=35)
        if response.status_code in (429, 500, 502, 503, 504) and attempt != 3:
            time.sleep(min(20, 3 * (attempt + 1)))
            continue
        response.raise_for_status()
        return response.json()


def num(value, default=None):
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except (ValueError, TypeError):
        return default


def unit_price(row):
    price = num(row.get('listing_price'))
    quantity = num(row.get('amount'), 1)
    if price is None or price <= 0 or not quantity or quantity <= 0:
        return None
    return price / quantity / LE


def kind(row):
    return '|'.join(str(row.get(k) if row.get(k) is not None else '').lower() for k in
                    ('name', 'item_type', 'subtype', 'tier', 'unidentified', 'shiny_stat'))


def identity(row):
    if row.get('hash_code'):
        return str(row['hash_code'])
    fields = ['name','listing_price','amount','timestamp','stat_rolls','overall_roll','shiny_stat','tier']
    return hashlib.sha256(json.dumps([row.get(k) for k in fields],sort_keys=True,default=str).encode()).hexdigest()[:24]


def weighted_quantile(points, q):
    points = sorted((float(p), float(w)) for p,w in points if w > 0 and p > 0)
    if not points:
        return None
    total = sum(w for _,w in points)
    acc = 0
    for p,w in points:
        acc += w
        if acc >= q * total:
            return p
    return points[-1][0]


def similarity(a,b):
    aa, bb = a.get('stat_rolls') or {}, b.get('stat_rolls') or {}
    if isinstance(aa,dict) and isinstance(bb,dict):
        shared = set(aa) & set(bb)
        diffs = [abs(num(aa[k], 1000)-num(bb[k], 1000)) for k in shared]
        if len(diffs) >= 2:
            return max(.03, math.exp(-stats.mean(diffs)/16) * min(1,len(diffs)/4))
    x,y = num(a.get('overall_roll')),num(b.get('overall_roll'))
    if x is not None and y is not None:
        return max(.05, .5 * math.exp(-abs(x-y)/13))
    return .25 if not aa and not bb else .1


def recent_history(rows, name, hours=24*14):
    cutoff = datetime.now(timezone.utc)-timedelta(hours=hours)
    result=[]
    for h in rows:
        if h.get('variant') != name:
            continue
        try:
            t=datetime.fromisoformat(h['at'].replace('Z','+00:00'))
            if t >= cutoff:
                result.append(h)
        except (KeyError, ValueError, TypeError):
            pass
    return result


def valuation(row, peers, history):
    buy = unit_price(row)
    if buy is None:
        return None
    comps = [(unit_price(p), similarity(row,p)) for p in peers if identity(p)!=identity(row)]
    comps = [(p,w) for p,w in comps if p is not None and w >= .12]
    if len(comps) < 4:
        return None
    raw_med=stats.median(p for p,_ in comps)
    comps=[(p,w) for p,w in comps if raw_med*.4 <= p <= raw_med*2.5]
    if len(comps) < 4:
        return None
    # Prefer the cheapest comparable offers rather than anchoring on inflated asks.
    price_floor=weighted_quantile(comps,.30)
    prices=[unit_price(p) for p in peers]
    prices=[p for p in prices if p is not None]
    # Avoid price optimism with heavily dispersed peers.
    median=stats.median(prices)
    absdev=stats.median(abs(p-median) for p in prices)
    dispersion=min(1.5,1.4826*absdev/max(.001,median))
    past=[num(h.get('median_ask_le')) for h in history]
    past=[p for p in past if p and p>0]
    prior=stats.median(past) if past else None
    changes=[abs(past[i]/past[i-1]-1) for i in range(1,len(past)) if past[i-1]]
    volatility=stats.median(changes) if changes else None
    # Protect against artificially rising asking prices.
    safe_base=min(price_floor, prior*1.03) if prior else price_floor
    # Reserve margin for undercutting, listing costs and price uncertainty.
    risk=min(.42, .08 + .40*dispersion + (1.2*min(.15,volatility) if volatility is not None else .08))
    fair=safe_base*(1-risk)
    profit=fair-buy
    roi=100*profit/buy
    confidence=max(.05,min(.95,.18 + .035*min(len(comps),12) + .03*min(len(past),8) - .35*dispersion - (min(.2,volatility) if volatility is not None else .10)))
    stability='unknown' if len(past)<3 else ('high' if volatility is not None and volatility>=.12 else 'moderate' if volatility is not None and volatility>=.045 else 'stable')
    reasons=[]
    if len(past)<3: reasons.append('Insufficient price history')
    if dispersion>.35: reasons.append('Wide current asking-price spread')
    if stability=='high': reasons.append('High recent price volatility')
    if len(comps)<7: reasons.append('Few comparable listings')
    if not row.get('stat_rolls') and row.get('item_type')=='gear': reasons.append('Incomplete stat-roll comparison')
    # Ranking rewards consistency, not merely the biggest LE spread.
    risk_score=confidence * (1-min(.8,dispersion)) * (1-min(.8,(volatility or .07)*3))
    score=max(0,profit) * math.sqrt(max(0,roi)/100) * risk_score
    return dict(buy=buy, fair=fair, profit=profit, roi=roi, confidence=confidence,
                comps=len(comps), history_points=len(past), volatility=volatility,
                dispersion=dispersion, stability=stability, score=score, reasons=reasons)


def analyze(listings, history, previous, now):
    grouped=defaultdict(list)
    for row in listings:
        if row.get('name') and unit_price(row) is not None:
            grouped[kind(row)].append(row)
    min_profit=num(os.environ.get('MIN_PROFIT_LE'), .5)
    min_roi=num(os.environ.get('MIN_ROI_PERCENT'),25)
    min_conf=num(os.environ.get('MIN_CONFIDENCE'),.45)
    out=[]
    prev=set(previous.get('ids') or [])
    for variant, rows in grouped.items():
        past=recent_history(history,variant)
        for row in rows:
            v=valuation(row,rows,past)
            if v is None or v['profit'] < min_profit or v['roi'] < min_roi:
                continue
            if v['confidence'] < min_conf: continue
            flags=v['reasons']
            # A one-off huge price spread should not become the top-rated flip.
            category=('watch' if v['stability']=='high' or v['dispersion']>.35 or v['history_points']<3 else 'candidate')
            out.append(dict(id=identity(row), name=row.get('name'), rarity=row.get('rarity'),
                item_type=row.get('item_type'), overall_roll=row.get('overall_roll'),
                stat_rolls=row.get('stat_rolls') or {}, timestamp=row.get('timestamp'),
                buy_le=round(v['buy'],3), fair_le=round(v['fair'],3),
                profit_le=round(v['profit'],3), roi=round(v['roi'],1),
                confidence=round(v['confidence'],2), comparables=v['comps'],
                history_points=v['history_points'], dispersion=round(v['dispersion'],3),
                volatility=round(v['volatility'],3) if v['volatility'] is not None else None,
                stability=v['stability'], score=round(v['score'],3),
                category=category, warnings=flags, newly_seen=bool(previous.get('initialized')) and identity(row) not in prev))
    out.sort(key=lambda d:(d['category']=='candidate',d['score']),reverse=True)
    return grouped,out


def main():
    key=os.environ.get('WYNNVENTORY_API_KEY','').strip()
    if not key: raise SystemExit('Missing WYNNVENTORY_API_KEY GitHub secret')
    session=requests.Session()
    max_pages=max(1,min(100,int(os.environ.get('MAX_PAGES','20'))))
    page_size=200
    listings=[];complete=False;total_pages=None
    for page in range(1,max_pages+1):
        response=request(session,'/market/listings',key,{'page':page,'page_size':page_size,'sort':'timestamp_desc'})
        batch=response.get('data')
        if not isinstance(batch,list): raise RuntimeError('Expected data array for market listings')
        listings.extend(r for r in batch if isinstance(r,dict))
        pg=response.get('pagination') or {}
        total_pages=pg.get('total_pages')
        if total_pages is not None and page>=int(total_pages): complete=True;break
        if not batch or len(batch)<page_size:complete=True;break
        time.sleep(.3)
    now=datetime.now(timezone.utc).isoformat()
    history=load(DATA/'history.json',[])
    previous=load(DATA/'seen.json',{'ids':[],'initialized':False})
    grouped,deals=analyze(listings,history,previous,now)
    for variant,rows in grouped.items():
        prices=[unit_price(r) for r in rows]
        prices=[p for p in prices if p is not None]
        if prices:
            history.append(dict(at=now,variant=variant,name=rows[0].get('name'),
                                median_ask_le=round(stats.median(prices),4),count=len(prices)))
    cutoff=(datetime.now(timezone.utc)-timedelta(days=30)).isoformat()
    history=[h for h in history if h.get('at','')>=cutoff][-45000:]
    lootpool=load(DATA/'lootpool.json',{})
    try:
        response=request(session,'/lootpools/current/items',key)
        lootpool=dict(updated_at=now,data=response.get('data'))
    except requests.RequestException:
        lootpool=dict(updated_at=now,error='Lootpool unavailable or key lacks permission')
    status=dict(updated_at=now,scanned=len(listings),pages=page,total_pages=total_pages,
                complete=complete,first_run_baseline=not previous.get('initialized'),
                candidates=sum(d['category']=='candidate' for d in deals),
                watchlist=sum(d['category']=='watch' for d in deals),
                new_deals=sum(d['newly_seen'] for d in deals),
                note='Asking prices, not completed sales. May be stale or incomplete.')
    # Never publish credentials or seller identities. Do not publish raw observed listing data.
    save(DATA/'seen.json',dict(ids=sorted({identity(r) for r in listings}),initialized=True))
    save(DATA/'history.json',history)
    save(DATA/'lootpool.json',lootpool)
    save(DATA/'status.json',status)
    save(SITE/'market.json',dict(status=status,deals=deals[:250],recent_history=history[-3000:],lootpool=lootpool))
    print(f"Scan OK: {len(listings)} listings, {page} pages, {status['candidates']} candidates, {status['watchlist']} watch, complete={complete}")

if __name__=='__main__': main()
