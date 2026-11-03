"""WynnFlip lifecycle storage v2: bounded independent Redis shards.

Never deletes or overwrites the legacy lifecycle key. Each EVAL handles only
one shard instead of repeatedly decoding/sorting/rewriting a multi-megabyte blob.
"""
import json
import zlib

SHARDS = 16
PREFIX = "wynnflip:v172:lifecycle:"
LEGACY = "wynnflip:v14:listing_lifecycle"

SHARD_LUA = r"""
local raw=redis.call('GET',KEYS[1])
local obj={entries={}}
if raw then
  local ok,parsed=pcall(cjson.decode,raw)
  if ok and type(parsed)=='table' and type(parsed.entries)=='table' then obj=parsed end
end
local entries=obj.entries
local scan=cjson.decode(ARGV[1]); local now=tonumber(ARGV[2])
local added=0; local repeat_count=0
for id,r in pairs(scan) do
  local v=entries[id]
  if type(v)~='table' then
    entries[id]={name=r.name,first_seen=now,last_seen=now,scans_seen=1,initial_price=r.price,latest_price=r.price,price_changes=0,price_reductions=0,misses=0}
    added=added+1
  else
    local previous=tonumber(v.last_seen or 0) or 0
    if now>previous then
      v.scans_seen=math.min(100000,(tonumber(v.scans_seen) or 1)+1)
      repeat_count=repeat_count+1
    end
    local previous_price=tonumber(v.latest_price)
    local price=tonumber(r.price)
    if previous_price and price and previous_price~=price then
      v.price_changes=math.min(100000,(tonumber(v.price_changes) or 0)+1)
      if price<previous_price then
        v.price_reductions=math.min(100000,(tonumber(v.price_reductions) or 0)+1)
      end
    end
    v.last_seen=math.max(now,previous);v.latest_price=r.price;v.misses=0
  end
end
local count=0
for id,v in pairs(entries) do
  -- Inactive offers are retained for at most 45 days.
  if tonumber(v.last_seen or 0)<now-45*86400 then entries[id]=nil else count=count+1 end
end
if count>1200 then
  local ordered={}
  for id,v in pairs(entries) do
    ordered[#ordered+1]={id=id,t=tonumber(v.last_seen or 0)}
  end
  table.sort(ordered,function(a,b)
    if a.t==b.t then return a.id<b.id else return a.t>b.t end
  end)
  for i=1201,#ordered do entries[ordered[i].id]=nil end
  count=1200
end
obj.updated_at=now
redis.call('SET',KEYS[1],cjson.encode(obj),'EX',86400*45)
return cjson.encode({new_count=added,repeat_count=repeat_count,tracked=count})
"""

def shard_for(identifier):
    return zlib.crc32(str(identifier).encode("utf-8")) % SHARDS

def shard_key(index):
    return PREFIX + str(index)

def update(rows, observed_at, redis_call, lifecycle_input):
    incoming = lifecycle_input(rows)
    if not incoming:
        return None, "no stable identifiers", {}
    buckets = {}
    for ident, row in incoming.items():
        index = shard_for(ident)
        buckets.setdefault(index, {})[ident] = row
    totals = {"new_count":0, "repeat_count":0, "tracked":0}
    errors = []
    # Keep batches small: large pipelines can also hit Upstash execution limits.
    indices = sorted(buckets)
    for start in range(0, len(indices), 4):
        selected = indices[start:start+4]
        commands = [
            ["EVAL", SHARD_LUA, 1, shard_key(index),
             json.dumps(buckets[index], separators=(",", ":")), str(int(observed_at))]
            for index in selected
        ]
        results, error = redis_call(commands)
        if error:
            errors.append(error)
            continue
        for result in results:
            try:
                counts = json.loads(result)
                for key in totals:
                    totals[key] += int(counts.get(key, 0))
            except (TypeError, ValueError, AttributeError):
                errors.append("invalid lifecycle shard response")
    if errors:
        return None, "; ".join(dict.fromkeys(errors))[:240], incoming
    return totals, None, incoming

def read(redis_call):
    merged = {}
    errors = []
    for start in range(0, SHARDS, 4):
        indices = list(range(start, min(start+4, SHARDS)))
        results, error = redis_call([["GET", shard_key(i)] for i in indices])
        if error:
            errors.append(error)
            continue
        for raw in results:
            if not raw:
                continue
            try:
                doc = json.loads(raw)
                entries = doc.get("entries")
                if isinstance(entries, dict):
                    merged.update(entries)
            except (TypeError, ValueError, AttributeError):
                errors.append("invalid lifecycle shard JSON")
    # Read-only compatibility: preserve 45-day legacy observations without
    # repeatedly running expensive EVAL scripts against the legacy blob.
    results, legacy_error = redis_call([["GET", LEGACY]])
    if legacy_error:
        if not merged:
            errors.append(legacy_error)
    elif results and results[0]:
        try:
            legacy = json.loads(results[0]).get("entries", {})
            if isinstance(legacy, dict):
                for ident, row in legacy.items():
                    current = merged.get(ident)
                    if current is None or float(row.get("last_seen") or 0) > float(current.get("last_seen") or 0):
                        merged[ident] = row
        except (TypeError, ValueError, AttributeError):
            if not merged:
                errors.append("invalid legacy lifecycle JSON")
    return merged, ("; ".join(dict.fromkeys(errors))[:240] if errors else None)
