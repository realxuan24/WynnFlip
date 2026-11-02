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
    roi = 100 * profit / buy
    conf=max(.05,min(.95,.18+.035*min(12,len(comps))+.03*min(8,len(past))-.35*dispersion-(min(.2,volatility) if volatility is not None else .1)))
    high_risk=dispersion>.35 or (volatility is not None and volatility >= .12)
    if profit > 0 and roi >= 25 and conf >= .45 and len(past)>=3 and not high_risk:
        category='candidate'
    elif profit > 0 and roi >= 15:
        category='watch' if high_risk else 'potential'
    else:
        category='unrated'
    score=max(0,profit)/LE * max(0,roi/100)**.5 * conf * (1-min(.8,dispersion)) * (1-min(.8,(volatility or .07)*3))
    warnings=[]
    if len(past)<3: warnings.append('Limited history')
    if high_risk: warnings.append('Unstable comparable prices')
    if len(comps)<7: warnings.append('Few comparable listings')
    return dict(fair_e=fair,profit_e=profit,roi=round(roi,1),confidence=round(conf,3),
                category=category,score=round(score,3),comparables=len(comps),
                volatility=round(volatility,4) if volatility is not None else None,
                stability='volatile' if high_risk else ('unknown' if len(past)<3 else 'stable'),
                warnings=warnings)


def build(listings, previous, history, now):
    old = previous.get('listings', {}) if isinstance(previous,dict) else {}
    grouped=defaultdict(list)
    for row in listings:
        if row.get('name') and price_e(row) is not None:
            grouped[variant(row)].append(row)
    output=[]; new_seen={}; present=set()
    for group, rows in grouped.items():
        group_history=(history.get(group) or [])[-80:]
        for r in rows:
            ident=identity(r)
            if ident in present: continue
            present.add(ident)
            first=(old.get(ident) or {}).get('first_seen') or now.isoformat()
            last=now.isoformat()  # observed in current API response, NOT confirmed in-game
            listing_age=age_minutes(r.get('timestamp'),now)
            observation_age=0
            analysis=estimate(r,rows,group_history)
            item=dict(id=ident,name=r.get('name'),rarity=r.get('tier') or '',
                      item_type=r.get('item_type') or '',subtype=r.get('subtype') or '',
                      quantity=r.get('amount') or 1,price_e=price_e(r),
                      stat_rolls=r.get('stat_rolls') or {},overall_roll=r.get('overall_roll'),
                      first_seen=first,last_observed=last,
                      listing_timestamp=r.get('timestamp'),listing_age_minutes=listing_age,
                      observed_age_minutes=observation_age,
                      category='unrated',fair_e=None,profit_e=None,roi=None,score=0,
                      confidence=None,stability='unknown',warnings=[])
            if analysis: item.update(analysis)
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
    output,seen,history=build(rows,old,history,now)
    loot={'state':'unavailable','note':'Lootpool data unavailable'}
    try:
        pool=get(session,'/lootpools/current/items',key)
        loot={'state':'available','data':pool}
    except (requests.RequestException,ValueError) as err:
        loot['note']=f'Lootpool lookup unavailable ({type(err).__name__})'
    status={'version':'4','updated_at':now.isoformat(),'scanned':len(rows),'exported':len(output),
            'pages':page,'total_pages':total_pages,
            'complete':bool((total_pages and page>=int(total_pages)) or len(items)<page_size),
            'note':'Observed API listings, not confirmed in-game availability; timestamps may be missing.'}
    save(DATA/'observed.json', {'listings':seen})
    save(DATA/'history_v4.json',history)
    save(SITE/'market.json',{'status':status,'listings':output,'lootpool':loot})
    print('Scan OK V4:',status['scanned'],'observed;',len(output),'exported;',status['complete'],'complete')


if __name__ == '__main__':main()
