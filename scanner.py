"""WynnFlip scheduled market observer. No Minecraft automation or paid services."""
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
DATA.mkdir(exist_ok=True)
LE = 4096


def fetch(session, path, key, params=None):
    for attempt in range(3):
        response = session.get(API + path, params=params, headers={'X-API-Key': key}, timeout=30)
        if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
            time.sleep(3 * (attempt + 1))
            continue
        response.raise_for_status()
        return response.json()


def identity(x):
    if x.get('hash_code'):
        return str(x['hash_code'])
    raw = json.dumps([x.get(k) for k in ('name', 'listing_price', 'amount', 'timestamp', 'stat_rolls', 'overall_roll')], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def variant(x):
    return '|'.join(str(x.get(k) or '').lower() for k in ('name', 'item_type', 'tier', 'unidentified', 'shiny_stat'))


def similarity(a, b):
    ar, br = a.get('stat_rolls') or {}, b.get('stat_rolls') or {}
    if isinstance(ar, dict) and isinstance(br, dict):
        common = set(ar) & set(br)
        distances = []
        for stat in common:
            try:
                distances.append(abs(float(ar[stat]) - float(br[stat])))
            except (TypeError, ValueError):
                pass
        if distances:
            return max(.05, math.exp(-statistics.mean(distances) / 22))
    try:
        return max(.05, .65 * math.exp(-abs(float(a['overall_roll']) - float(b['overall_roll'])) / 20))
    except (KeyError, ValueError, TypeError):
        return .2


def estimate(row, peers):
    try:
        buy = float(row['listing_price']) / max(1, int(row.get('amount') or 1))
    except (TypeError, ValueError, KeyError):
        return None
    if buy <= 0:
        return None
    valid = []
    for other in peers:
        try:
            unit = float(other['listing_price']) / max(1, int(other.get('amount') or 1))
        except (TypeError, ValueError, KeyError):
            continue
        if unit > 0 and identity(other) != identity(row):
            valid.append((unit, similarity(row, other)))
    if len(valid) < 3:
        return None
    unweighted_median = statistics.median(p for p, _ in valid)
    valid = [(p, w) for p, w in valid if unweighted_median / 4 <= p <= unweighted_median * 4]
    if len(valid) < 3:
        return None
    valid.sort()
    target = valid[-1][0]
    sofar, total = 0, sum(w for _, w in valid)
    for p, w in valid:
        sofar += w
        if sofar >= total / 2:
            target = p
            break
    fair = target * .88  # Conservative markdown from similar current asks
    confidence = min(.9, .2 + .11 * math.log1p(len(valid)) + .22 * sum(w for _, w in valid) / len(valid))
    return {'buy': buy, 'fair': fair, 'profit': fair - buy, 'roi': (fair / buy - 1) * 100, 'confidence': confidence, 'comparables': len(valid)}


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding='utf-8')


def main():
    key = os.environ.get('WYNNVENTORY_API_KEY', '').strip()
    if not key:
        raise SystemExit('Missing WYNNVENTORY_API_KEY secret')
    session = requests.Session()
    max_pages = max(1, min(100, int(os.environ.get('MAX_PAGES', '20'))))
    page_size = 200
    listings = []
    complete = False
    total_pages = None
    for page in range(1, max_pages + 1):
        payload = fetch(session, '/market/listings', key, {'page': page, 'page_size': page_size, 'sort': 'timestamp_desc'})
        batch = payload.get('data')
        if not isinstance(batch, list):
            raise RuntimeError('Unexpected market response: data is not a list')
        listings.extend(x for x in batch if isinstance(x, dict))
        pagination = payload.get('pagination') or {}
        # Published v2 API supplies pagination.total_pages; never assume one page if missing.
        total_pages = pagination.get('total_pages')
        if total_pages is not None and page >= int(total_pages):
            complete = True
            break
        if not batch or len(batch) < page_size:
            complete = True
            break
        time.sleep(.3)
    now = datetime.now(timezone.utc).isoformat()
    current_ids = {identity(x) for x in listings}
    previous = load_json(DATA / 'seen.json', {'ids': [], 'initialized': False})
    old_ids = set(previous.get('ids') or [])
    by_variant = defaultdict(list)
    for row in listings:
        if row.get('name'):
            by_variant[variant(row)].append(row)
    min_profit = float(os.environ.get('MIN_PROFIT_LE', '0.5')) * LE
    min_roi = float(os.environ.get('MIN_ROI_PERCENT', '30'))
    deals = []
    for rows in by_variant.values():
        for row in rows:
            if previous.get('initialized') and identity(row) in old_ids:
                continue
            est = estimate(row, rows)
            if not est or est['profit'] < min_profit or est['roi'] < min_roi:
                continue
            deals.append({'id': identity(row), 'name': row.get('name'), 'item_type': row.get('item_type'),
                          'rarity': row.get('rarity'), 'overall_roll': row.get('overall_roll'),
                          'stat_rolls': row.get('stat_rolls') or {}, 'timestamp': row.get('timestamp'),
                          'buy_le': round(est['buy'] / LE, 3), 'fair_le': round(est['fair'] / LE, 3),
                          'profit_le': round(est['profit'] / LE, 3), 'roi': round(est['roi'], 1),
                          'confidence': round(est['confidence'], 2), 'comparables': est['comparables']})
    deals.sort(key=lambda x: x['profit_le'], reverse=True)
    # Retain summarized snapshots rather than growing a repository with every full listing.
    history = load_json(DATA / 'history.json', [])
    for v, rows in by_variant.items():
        prices = []
        for r in rows:
            try:
                prices.append(float(r['listing_price']) / max(1, int(r.get('amount') or 1)) / LE)
            except (ValueError, TypeError, KeyError):
                pass
        if prices:
            history.append({'at': now, 'variant': v, 'name': rows[0].get('name'),
                            'median_ask_le': round(statistics.median(prices), 4), 'count': len(prices)})
    history = history[-25000:]
    lootpool = load_json(DATA / 'lootpool.json', {'error': 'No lootpool data yet'})
    try:
        lootpool = {'updated_at': now, 'data': fetch(session, '/lootpools/current/items', key).get('data')}
    except requests.RequestException as exc:
        lootpool = {'updated_at': now, 'error': 'Lootpool unavailable or missing scope', 'details': str(exc)[:150]}
    save_json(DATA / 'lootpool.json', lootpool)
    save_json(DATA / 'seen.json', {'ids': sorted(current_ids), 'initialized': True})
    save_json(DATA / 'history.json', history)
    status = {'updated_at': now, 'scanned': len(listings), 'pages': page,
              'complete': complete, 'total_pages': total_pages,
              'new_deals': len(deals) if previous.get('initialized') else 0,
              'first_run_baseline': not previous.get('initialized'),
              'note': 'Crowdsourced listing observations; not guaranteed live or complete.'}
    save_json(DATA / 'status.json', status)
    # Static output is PUBLIC if published: never include secrets or seller identities.
    save_json(Path('site') / 'market.json', {'status': status, 'deals': deals[:150],
             'recent_history': history[-1500:], 'lootpool': lootpool})
    print(f"Scanned {len(listings)} listings over {page} page(s); complete={complete}, new deals={status['new_deals']}")


if __name__ == '__main__':
    main()
