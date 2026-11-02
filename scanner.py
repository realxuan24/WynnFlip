"""WynnFlip V8: GitHub-only, read-only WynnVentory market observer.
Listing presence is NOT proof of in-game availability.
"""
import hashlib
import json
import math
import os
import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

API = 'https://wynnventory.com/api/v2'
DATA = Path('data')
SITE = Path('site')
LE = 4096


def load(path, fallback):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return fallback


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(obj, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    temp.replace(path)


def get(session, endpoint, key, params=None):
    for attempt in range(4):
        r = session.get(API + endpoint, headers={'X-API-Key': key}, params=params, timeout=35)
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
            time.sleep(3 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()


def number(value, default=None):
    try:
        n = float(value)
        return n if math.isfinite(n) else default
    except (ValueError, TypeError):
        return default


def price_e(row):
    amount = number(row.get('listing_price'))
    quantity = number(row.get('amount'), 1)
    if amount is None or amount <= 0 or not quantity or quantity <= 0:
        return None
    unit = round(amount / quantity)
    return unit if unit > 0 else None


def identity(row):
    if row.get('hash_code'):
        return str(row['hash_code'])
    # Fallback is not a guaranteed unique trade-market listing identity.
    keys = ('name', 'listing_price', 'amount', 'timestamp', 'stat_rolls', 'overall_roll', 'tier')
    canonical = json.dumps([row.get(k) for k in keys], sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:24]


def variant(row):
    return '|'.join(str(row.get(k) or '').lower() for k in
                    ('name', 'item_type', 'subtype', 'tier', 'unidentified', 'shiny_stat'))


def timestamp(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e12: v /= 1000
        try:
            return datetime.fromtimestamp(v, timezone.utc)
        except (ValueError, OverflowError):
            return None
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def age_minutes(value, now):
    dt = timestamp(value)
    return max(0, round((now - dt).total_seconds() / 60, 1)) if dt else None


def exact_ids(row):
    """Normalize WynnVentory nested stat_rolls; preserve actual in-game ID values."""
    raw = row.get('stat_rolls') or {}
    entries = []
    if isinstance(raw, list):
        source = [(None, e) for e in raw]
    elif isinstance(raw, dict):
        source = list(raw.items())
    else:
        return []
    for key, entry in source:
        if isinstance(entry, dict):
            stat = entry.get('statActualValue') or {}
            stat_type = stat.get('statType') or {}
            name = entry.get('displayName') or stat_type.get('displayName') or key
            api = entry.get('apiName') or stat_type.get('apiName') or key or name
            actual = number(entry.get('statRoll'))
            if actual is None: actual = number(stat.get('value'))
            pct = number(entry.get('rollPercentage'))
            if pct is not None and not 0 <= pct <= 100: pct = None
            possible = entry.get('possibleValues') or {}
            span = entry.get('statRange') or possible.get('range') or {}
            fixed = bool(span.get('fixed',False)) or span.get('low') == span.get('high')
            unit = entry.get('unit') or stat_type.get('unit') or ''
        else:
            name, api, actual, pct, fixed, unit = key, key, number(entry), number(entry), False, ''
            if pct is not None and not 0 <= pct <= 100: pct = None
        if not name or actual is None: continue
        entries.append({'name':str(name),'api_name':str(api or name),'value':actual,
                        'roll_pct':None if fixed else pct,'fixed':bool(fixed),'unit':str(unit)})
    return entries


def roll_numbers(row):
    """Return valid percentage rolls by canonical API ID name."""
    return {x['api_name']:x['roll_pct'] for x in exact_ids(row)
            if x['roll_pct'] is not None and not x['fixed']}


def rank_correlation(pairs):
    """Spearman-like rank correlation; 0 means no observable relation."""
    def ranks(seq):
        order = sorted(range(len(seq)), key=lambda i: seq[i])
        result = [0.0] * len(seq)
        i = 0
        while i < len(seq):
            j = i + 1
            while j < len(seq) and seq[order[j]] == seq[order[i]]:
                j += 1
            avg = (i + j - 1) / 2
            for k in range(i, j): result[order[k]] = avg
            i = j
        return result
    x, y = ranks([v for v, _ in pairs]), ranks([v for _, v in pairs])
    mx, my = statistics.mean(x), statistics.mean(y)
    numerator = sum((a-mx)*(b-my) for a,b in zip(x,y))
    denom = math.sqrt(sum((a-mx)**2 for a in x)*sum((b-my)**2 for b in y))
    return numerator/denom if denom else 0.0


def learn_id_weights(peers):
    """Conservative *asking-price associations*, not true stat desirability.

    Only learn from sufficiently varied current offers. Never infer weight from
    tiny samples or virtually identical prices.
    """
    priced = [(r, price_e(r)) for r in peers if price_e(r) is not None]
    if len(priced) < 12 or len(set(price for _,price in priced)) < 5:
        return {}, {}
    # Ignore extreme asking-price outliers before learning associations.
    values = sorted(v for _,v in priced)
    low, high = values[max(0,int(len(values)*.1))], values[min(len(values)-1,int(len(values)*.9))]
    fields = set().union(*(roll_numbers(r).keys() for r,_ in priced))
    weights, evidence = {}, {}
    for field in fields:
        pairs = [(roll_numbers(row)[field], math.log(price)) for row,price in priced
                 if low <= price <= high and field in roll_numbers(row) and price > 0]
        if len(pairs) < 10 or len(set(x for x,_ in pairs)) < 5 or len(set(y for _,y in pairs)) < 5:
            continue
        association = rank_correlation(pairs)
        # Treat strength as a similarity weight, not a price multiplier.
        # Only appreciable associations matter and caps prevent dominating.
        if abs(association) < .25: continue
        weight = min(1.8, 1.0 + abs(association)*.8)
        weights[field] = round(weight, 3)
        evidence[field] = {'association':round(association,3), 'sample_size':len(pairs),
                           'direction':'higher asking prices' if association>0 else 'lower asking prices'}
    return weights, evidence



# Wynnpool community profiles are optional. No profile => no invented ID score.
PROFILE_PATH = Path('profiles/item_weights.json')

def normalized_name(value):
    return ''.join(c for c in str(value or '').lower() if c.isalnum())

def load_profiles(path=PROFILE_PATH):
    """Expected normalized format: {item: {profile: {id_name: signed_weight}}}."""
    raw = load(path, {})
    if not isinstance(raw, dict):
        return {}
    valid = {}
    for item, profiles in raw.items():
        if not isinstance(profiles, dict):
            continue
        entries = {}
        for name, ids in profiles.items():
            if not isinstance(ids, dict):
                continue
            weights = {normalized_name(k): number(v) for k, v in ids.items()}
            weights = {k: w for k, w in weights.items() if k and w is not None and -1 <= w <= 1 and w != 0}
            if weights and .90 <= sum(abs(w) for w in weights.values()) <= 1.10:
                entries[str(name)] = weights
        if entries:
            valid[normalized_name(item)] = entries
    return valid

def profile_quality(row, profiles):
    """Report profile fit only when API roll percentages are plausibly 0..100.
    A negative weight prefers a *lower* roll. No LE premium is inferred.
    """
    choices = profiles.get(normalized_name(row.get('name')), {})
    rolls = {normalized_name(k): v for k, v in roll_numbers(row).items()}
    scored = []
    for profile, weights in choices.items():
        shared = [(key, weight, rolls[key]) for key, weight in weights.items() if key in rolls]
        coverage = sum(abs(weight) for _, weight, _ in shared)
        if coverage < .75 or any(not (0 <= v <= 100) for _, _, v in shared):
            continue
        score = sum((v if weight > 0 else 100-v)*abs(weight) for _, weight, v in shared)/coverage
        scored.append({'profile':profile, 'score':round(score,1), 'coverage':round(coverage,2),
                       'important_ids':[{'stat':key,'weight':round(w,4),'roll':v,
                                         'prefer':'higher' if w>0 else 'lower'}
                                        for key,w,v in sorted(shared, key=lambda x:-abs(x[1]))[:6]]})
    return max(scored, key=lambda x:x['score']) if scored else None

def profile_similarity(a, b, profile):
    if not profile:
        return None
    ids = profile.get('important_ids') or []
    aa = {normalized_name(k):v for k,v in roll_numbers(a).items()}
    bb = {normalized_name(k):v for k,v in roll_numbers(b).items()}
    matched = [(abs(float(x['weight'])), abs(aa[x['stat']]-bb[x['stat']])) for x in ids
               if x['stat'] in aa and x['stat'] in bb]
    if not matched or sum(w for w,_ in matched) < .60:
        return None
    difference = sum(w*d for w,d in matched)/sum(w for w,_ in matched)
    return max(.01, math.exp(-difference/13))

def comparable(a, b, id_weights=None):
    aa, bb = roll_numbers(a), roll_numbers(b)
    shared = set(aa) & set(bb)
    if len(shared) >= 2:
        weights = id_weights or {}
        distances = [abs(aa[k]-bb[k])*weights.get(k,1.0) for k in shared]
        divisor = sum(weights.get(k,1.0) for k in shared)
        difference = sum(distances)/max(divisor,1e-9)
        return max(.01,math.exp(-difference/16)*min(1,len(shared)/4))
    x, y = number(a.get('overall_roll')), number(b.get('overall_roll'))
    if x is not None and y is not None:
        return .5*math.exp(-abs(x-y)/13)
    return .25 if not aa and not bb else .05


def estimate(row, peers, historical, id_weights=None, id_evidence=None, profile=None):
    buy = price_e(row)
    if buy is None or buy <= 0: return None
    prices = [price_e(p) for p in peers if price_e(p) is not None]
    def similarity(peer):
        if profile:
            return profile_similarity(row, peer, profile) or 0.0
        return comparable(row, peer, id_weights)
    comps = sorted((price_e(p), similarity(p)) for p in peers
                   if identity(p) != identity(row) and price_e(p) is not None
                   and similarity(p) >= (.45 if profile else .12))
    if len(comps) < 4: return None
    med = statistics.median(v for v,_ in comps)
    comps = [(v,w) for v,w in comps if med*.4 <= v <= med*2.5]
    if len(comps) < 4: return None
    weighted = sorted(comps, key=lambda x:x[0]); total=sum(w for _,w in weighted); running=0
    floor=weighted[-1][0]
    for value, weight in weighted:
        running += weight
        if running >= total*.30:
            floor=value;break
    market_median=statistics.median(prices)
    dispersion=min(1.5, 1.4826*statistics.median(abs(v-market_median) for v in prices)/max(1,market_median))
    past=[number(h.get('median_e')) for h in historical if number(h.get('median_e'),0)>0]
    anchor=statistics.median(past) if past else None
    movements=[abs(past[i]/past[i-1]-1) for i in range(1,len(past)) if past[i-1]>0]
    volatility=statistics.median(movements) if movements else None
    conservative_base=min(floor,anchor*1.03) if anchor else floor
    haircut=min(.5,.10 + .35*dispersion + (1.2*min(.2,volatility) if volatility is not None else .1))
    fair=max(0, math.floor(conservative_base*(1-haircut)))
    profit=fair-buy
    roi = 100 * profit / buy  # buy is guaranteed positive above
    conf=max(.05,min(.95,.18+.035*min(12,len(comps))+.03*min(8,len(past))-.35*dispersion-(min(.2,volatility) if volatility is not None else .1)))
    high_risk=dispersion>.35 or (volatility is not None and volatility >= .12)
    # Do not call a listing a strong candidate based solely on asking prices.
    # Genuine buyer demand / completed transactions are not verified.
    # No confirmed-sale feed exists, so never declare an item a guaranteed buy.
    # Require roll-matched comparable offers AND meaningful headroom before 'potential'.
    competing_cheaper = sum(1 for v, _ in comps if v <= fair)
    strict_evidence = bool(profile and len(comps) >= 7 and conf >= .50 and
                           len(past) >= 3 and not high_risk and competing_cheaper <= 2)
    if profit > 0 and roi >= 25 and strict_evidence:
        category='potential'
    elif profit > 0 and roi >= 15:
        category='watch'
    else:
        category='unrated'
    score=max(0,profit)/LE * max(0,roi/100)**.5 * conf * (1-min(.8,dispersion)) * (1-min(.8,(volatility or .07)*3))
    warnings=[]
    if len(past)<3: warnings.append('Limited history')
    if high_risk: warnings.append('Unstable comparable prices')
    if len(comps)<7: warnings.append('Few comparable listings')
    if competing_cheaper>2: warnings.append('Multiple competing listings at or below conservative resale estimate')
    if not profile: warnings.append('Wynnpool profile unavailable or roll format unverified')
    warnings.append('Buyer demand and completed sales unverified')
    return dict(fair_e=fair,profit_e=profit,roi=round(roi,1),confidence=round(conf,3),
                category=category,score=round(score,3),comparables=len(comps),
                cheaper_competitors=competing_cheaper, resale_evidence='unverified',
                volatility=round(volatility,4) if volatility is not None else None,
                stability='volatile' if high_risk else ('unknown' if len(past)<3 else 'stable'),
                warnings=warnings, id_pricing={'method':'listing-price association only',
                'weighted_ids':sorted(({'stat':k,'weight':w,**(id_evidence or {}).get(k,{})}
                 for k,w in (id_weights or {}).items()), key=lambda z:-z['weight'])[:8],
                'verified_sales':False})


def build(listings, previous, history, now, profiles=None):
    profiles = profiles or {}
    old = previous.get('listings', {}) if isinstance(previous,dict) else {}
    grouped=defaultdict(list)
    for row in listings:
        if row.get('name') and price_e(row) is not None:
            grouped[variant(row)].append(row)
    output=[]; new_seen={}; present=set()
    for group, rows in grouped.items():
        group_history=(history.get(group) or [])[-80:]
        id_weights,id_evidence=learn_id_weights(rows)
        for r in rows:
            ident=identity(r)
            if ident in present: continue
            present.add(ident)
            first=(old.get(ident) or {}).get('first_seen') or now.isoformat()
            last=now.isoformat()  # observed in current API response, NOT confirmed in-game
            listing_age=age_minutes(r.get('timestamp'),now)
            observation_age=0
            profile=profile_quality(r, profiles)
            analysis=estimate(r,rows,group_history,id_weights,id_evidence,profile)
            item=dict(id=ident,name=r.get('name'),rarity=r.get('tier') or '',
                      item_type=r.get('item_type') or '',subtype=r.get('subtype') or '',
                      quantity=r.get('amount') or 1,price_e=price_e(r),
                      stat_rolls=roll_numbers(r),exact_ids=exact_ids(r),overall_roll=r.get('overall_roll'),
                      first_seen=first,last_observed=last,
                      listing_timestamp=r.get('timestamp'),listing_age_minutes=listing_age,
                      observed_age_minutes=observation_age,
                      category='unrated',fair_e=None,profit_e=None,roi=None,score=0,
                      id_pricing={'method':'insufficient evidence','weighted_ids':[],'verified_sales':False},
                      wynnpool=profile,
                      confidence=None,stability='unknown',warnings=[],
                      comparables=0,cheaper_competitors=None,resale_evidence='unverified')
            if analysis: item.update(analysis)
            item['wynnpool']=profile
            item['meta']={'profile_source':'community weights (if matched)',
                          'build_relevance':'profile-supported' if profile else 'unverified',
                          'profile_verified_current_patch':False}
            item['demand']={'level':'unknown', 'verified_sales':0,
                            'reason':'No verified completed-sale feed; partial market scans cannot establish turnover.'}
            # Without a trustworthy listing timestamp, freshness cannot be asserted.
            if listing_age is None:
                item['freshness']='unknown'
            elif listing_age <= 5:
                item['freshness']='recent'
            elif listing_age <= 15:
                item['freshness']='caution'
            else:
                item['freshness']='stale'
            output.append(item)
            new_seen[ident]={'first_seen':first,'last_seen':last}
        values=[price_e(r) for r in rows if price_e(r) is not None]
        if values:
            history.setdefault(group,[]).append({'at':now.isoformat(),'median_e':round(statistics.median(values))})
            history[group]=history[group][-80:]
    # Previous listings NOT observed this scan are not proven sold; do not publish as active.
    return output,new_seen,history


def extract_listing_page(payload):
    if isinstance(payload,list):return payload, None
    if not isinstance(payload,dict):raise ValueError('Unexpected listing response')
    rows=payload.get('listings') or payload.get('data') or payload.get('results') or []
    if isinstance(rows,dict):rows=rows.get('listings') or rows.get('items') or []
    if not isinstance(rows,list):raise ValueError('No listing array found')
    pagination=payload.get('pagination') or payload.get('meta') or {}
    pages=pagination.get('total_pages') or payload.get('total_pages') if isinstance(pagination,dict) else payload.get('total_pages')
    return rows, pages


def main():
    key=os.environ.get('WYNNVENTORY_API_KEY','').strip()
    if not key:raise RuntimeError('Missing WYNNVENTORY_API_KEY GitHub secret')
    now=datetime.now(timezone.utc)
    old=load(DATA/'observed.json', {'listings':{}})
    history=load(DATA/'history_v4.json',{})
    session=requests.Session()
    rows=[];total_pages=None
    max_pages=max(1,min(100,int(os.environ.get('MAX_PAGES','20'))))
    page_size=200
    for page in range(1,max_pages+1):
        payload=get(session,'/market/listings',key,{'page':page,'page_size':page_size,'sort':'timestamp_desc'})
        items,pages=extract_listing_page(payload)
        if pages is not None:total_pages=pages
        rows.extend(items)
        if not items or len(items)<page_size:break
        if total_pages is not None and page>=int(total_pages):break
    if not rows:raise RuntimeError('Scanner returned zero listings; refusing to publish blank data')
    profiles=load_profiles()
    output,seen,history=build(rows,old,history,now,profiles)
    loot={'state':'unavailable','note':'Lootpool data unavailable'}
    try:
        pool=get(session,'/lootpools/current/items',key)
        loot={'state':'available','data':pool}
    except (requests.RequestException,ValueError) as err:
        loot['note']=f'Lootpool lookup unavailable ({type(err).__name__})'
    status={'version':'8','updated_at':now.isoformat(),'scanned':len(rows),'exported':len(output),
            'pages':page,'total_pages':total_pages,'matched_profiles':sum(bool(x.get('wynnpool')) for x in output),
            'complete':bool((total_pages and page>=int(total_pages)) or len(items)<page_size),
            'note':'Observed API listings, not confirmed in-game availability; timestamps may be missing.'}
    save(DATA/'observed.json', {'listings':seen})
    save(DATA/'history_v4.json',history)
    save(SITE/'market.json',{'status':status,'listings':output,'lootpool':loot})
    print('Scan OK V8:',status['scanned'],'observed;',len(output),'exported;',status['complete'],'complete')


if __name__ == '__main__':main()