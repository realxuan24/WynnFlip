"""Optional community-weight import. A changed upstream schema must never break scans.
Source: szVM3K/builderwynncraft (documents attribution to Wynnpool).
No generated or guessed weights; accept only recognisable normalized profiles.
"""
import json
from pathlib import Path
import requests

URL='https://raw.githubusercontent.com/szVM3K/builderwynncraft/main/src/item-weights.json'
DEST=Path('profiles/item_weights.json')

def numeric_weights(obj):
    if not isinstance(obj,dict): return None
    out={}
    for k,v in obj.items():
        if not isinstance(v,(int,float)) or isinstance(v,bool) or not (-1<=v<=1) or v==0:
            return None
        out[str(k)]=float(v)
    if out and .9 <= sum(abs(v) for v in out.values()) <= 1.1:
        return out
    return None

def normalize(data):
    # Accept item -> profile -> ID:weight, or item -> ID:weight, or
    # item -> array of {name, weights}. Reject unknown shapes.
    if not isinstance(data,dict): return {}
    result={}
    for name, entry in data.items():
        profiles={}
        direct=numeric_weights(entry)
        if direct: profiles['Main']=direct
        elif isinstance(entry,dict):
            for pname, obj in entry.items():
                if isinstance(obj,dict):
                    w=numeric_weights(obj.get('weights',obj))
                    if w: profiles[str(pname)]=w
        elif isinstance(entry,list):
            for obj in entry:
                if isinstance(obj,dict):
                    w=numeric_weights(obj.get('weights',{}))
                    if w: profiles[str(obj.get('name') or obj.get('profile') or 'Main')]=w
        if profiles:result[str(name)]=profiles
    return result

def main():
    try:
        response=requests.get(URL,timeout=15,headers={'User-Agent':'WynnFlip community profile importer'})
        response.raise_for_status()
        normalized=normalize(response.json())
        if len(normalized)<10:
            print('Wynnpool import skipped: upstream schema not recognized sufficiently; keeping cached profiles.')
            return
        DEST.parent.mkdir(parents=True,exist_ok=True)
        DEST.write_text(json.dumps(normalized,indent=2,ensure_ascii=False)+'\n')
        print(f'Imported profiles for {len(normalized)} item names (unverified ID-key alignment).')
    except (requests.RequestException,ValueError,TypeError) as exc:
        print(f'Wynnpool import unavailable ({type(exc).__name__}); keeping cached profiles.')
if __name__=='__main__':main()
