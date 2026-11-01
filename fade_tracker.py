#!/usr/bin/env python3
import json, os
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR") or HERE
FADE_TRACK_FILE = os.path.join(DATA_DIR, "fade_track.json")

PEAK_THRESHOLD = float(os.environ.get("FADE_PEAK_THRESHOLD", "20.0"))
GIVEBACK_MIN = float(os.environ.get("FADE_GIVEBACK_MIN", "15.0"))
MAX_AGE_H = float(os.environ.get("FADE_MAX_AGE_H", "48.0"))
PRUNE_S = MAX_AGE_H * 3600.0

def _age_s(ts):
    try:
        t = datetime.fromisoformat(str(ts).replace('Z','+00:00') if 'Z' in str(ts) else str(ts))
        if t.tzinfo is None: t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds()
    except Exception:
        return float('inf')

def load_json(p):
    try:
        with open(p) as f: return json.load(f)
    except Exception: return None

def save_json(p, obj):
    tmp = p + '.tmp'
    with open(tmp,'w') as f: json.dump(obj, f, indent=2, default=str)
    os.replace(tmp,p)

def update(discovery_payload=None):
    import discover
    if discovery_payload is None: discovery_payload = discover.get_movers()
    track = load_json(FADE_TRACK_FILE) or {"updated_at": None, "samples": {}}
    if "samples" not in track: track["samples"] = {}
    ts = discovery_payload.get('generated_at') or datetime.now(timezone.utc).isoformat()
    for g in discovery_payload.get('gainers', []):
        sym = g.get('mexc_symbol') or g.get('symbol')
        if not sym: continue
        last=g.get('last'); hi=g.get('high_24h'); lo=g.get('low_24h')
        range_pos=None
        try:
            if hi is not None and lo is not None and hi!=lo:
                range_pos=(float(last)-float(lo))/(float(hi)-float(lo))*100.0
        except Exception: pass
        rec = {"ts":ts,"chg_24h":g.get('change_24h_pct'),"last":last,"hi":hi,"lo":lo,"range_pos":range_pos,"funding":g.get('funding_rate')}
        track["samples"].setdefault(sym, []).append(rec)
    for sym in list(track["samples"].keys()):
        lst=[s for s in track["samples"][sym] if _age_s(s.get('ts'))<PRUNE_S]
        if not lst: del track["samples"][sym]
        else: track["samples"][sym]=lst
    track["updated_at"]=datetime.now(timezone.utc).isoformat()
    save_json(FADE_TRACK_FILE, track)
    return track

def detect_fades(track=None):
    if track is None: track=load_json(FADE_TRACK_FILE)
    fades=[]
    for sym, samples in (track or {}).get('samples', {}).items():
        if len(samples)<2: continue
        # find peak
        peak=-1e18
        for s in samples:
            try:
                c=float(s.get('chg_24h')) if s.get('chg_24h') is not None else -1e19
            except Exception: c=-1e19
            if c>peak: peak=c
        if peak < PEAK_THRESHOLD: continue
        cur=samples[-1]
        try: ccur=float(cur.get('chg_24h')) if cur.get('chg_24h') is not None else None
        except Exception: ccur=None
        if ccur is None: continue
        giveback=peak-ccur
        flag=False
        if giveback >= GIVEBACK_MIN: flag=True
        elif peak>=0 and ccur < 0: flag=True
        elif ccur < peak*0.5: flag=True
        if not flag: continue
        fades.append({"symbol":sym,"peak_24h":round(peak,2),"cur_24h":round(ccur,2),"giveback":round(giveback,2),"range_pos":round(cur.get('range_pos') or -1,1),"ts":cur.get('ts')})
    fades.sort(key=lambda x:-x['giveback'])
    return fades

if __name__=="__main__":
    import argparse
    ap=argparse.ArgumentParser(description="Fade tracker")
    ap.add_argument("cmd", choices=["update","scan"])
    args=ap.parse_args()
    if args.cmd=="update": update()
    import json
    print(json.dumps({"fades": detect_fades()}, indent=2, default=str))
