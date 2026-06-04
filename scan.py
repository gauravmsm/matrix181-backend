"""
MATRIX 18.1 — NSE Dual Strategy Screener
=========================================
Single data source: BhavCopy_NSE_CM ZIP (available all historical dates)
Columns used: SYMBOL, SERIES, OPEN, HIGH, LOW, CLOSE, TOTTRDQTY,
              TOTTRDVAL, DELIV_QTY, DELIV_PER

Derived from 252 days of bhavcopy:
  - 52W High/Low      → max(HIGH) / min(LOW) over 252 days
  - SMA 20/30/50/200  → rolling close averages
  - Vol ratio         → avg(vol 20D) / avg(vol 50D)
  - Delivery 20D/50D  → avg(DELIV_PER) over 20/50 days
  - Traded value      → avg(TOTTRDVAL/100) in Cr over 20D
  - MCap              → EQUITY_L: Close × PaidUp_Cr / FaceValue
"""

import pandas as pd
import numpy as np
import requests
import zipfile
import io
import json
import os
import base64
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "*/*", "Accept-Encoding": "gzip, deflate",
    "Referer": "https://www.nseindia.com/",
}
SESSION = requests.Session()
SESSION.headers.update(NSE_HEADERS)

GITHUB_TOKEN = os.environ.get("PAT_TOKEN", "")
GITHUB_USER  = "gauravmsm"
GITHUB_REPO  = "matrix181-backend"
RESULTS_PATH = "results/matrix181_results.json"

# ── Parameters ────────────────────────────────────────────────────────────────
S1_MCAP_MIN     = 1500;   S1_MCAP_MAX     = 30000
S1_RSI_MIN      = 40.0;   S1_RSI_MAX      = 70.0
S1_VOL_RATIO    = 1.20    # 20D/50D vol ratio (soft)
S1_DELIVERY_MIN = 45.0    # 20D avg delivery % (needs 10+ days)
S1_DELIVERY_GAP = 3.0     # 20D > 50D + 3% (needs 20+ days)
S1_TRADED_VALUE = 7.5     # Cr/day 20D avg (soft)
S1_FROM_LOW_MIN = 1.10;   S1_FROM_LOW_MAX = 1.40   # 52W low multiplier
S1_NEAR_HIGH    = 0.90    # close >= 60D_high × 0.90

S2_MCAP_MIN     = 30000
S2_RSI_MIN      = 40.0;   S2_RSI_MAX      = 70.0
S2_VOL_RATIO    = 1.20    # soft
S2_DELIVERY_MIN = 40.0    # needs 10+ days
S2_TRADED_VALUE = 25.0    # soft
S2_FROM_HIGH_MIN = 0.65;  S2_FROM_HIGH_MAX = 0.95  # within 5-35% of 52W high

DAYS = 260  # ~252 trading days in a year + buffer for holidays


# ── Helpers ───────────────────────────────────────────────────────────────────

def save_github(content_str):
    if not GITHUB_TOKEN: return False
    url  = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{RESULTS_PATH}"
    hdrs = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    sha  = None
    try:
        r = requests.get(url, headers=hdrs, timeout=15)
        if r.status_code == 200: sha = r.json().get("sha")
    except: pass
    body = {"message": f"scan {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "content": base64.b64encode(content_str.encode()).decode(), "branch": "main"}
    if sha: body["sha"] = sha
    try:
        r = requests.put(url, headers=hdrs, json=body, timeout=30)
        ok = r.status_code in (200, 201)
        print(f"  GitHub {'OK' if ok else 'FAIL'} {r.status_code}")
        return ok
    except Exception as e:
        print(f"  GitHub error: {e}"); return False

def trading_dates(n):
    out, d = [], datetime.now()
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5: out.append(d)
    return out

def fetch_bhavcopy(date):
    """
    Download BhavCopy ZIP for any historical date.
    Returns DataFrame with unified columns:
    SYMBOL, SERIES, OPEN, HIGH, LOW, CLOSE, VOLUME, VALUE, DELIV_QTY, DELIV_PER
    """
    d    = date.strftime("%Y%m%d")
    dold = date.strftime("%d%b%Y").upper()
    urls = [
        f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d}_F_0000.csv.zip",
        f"https://www.nseindia.com/content/historical/EQUITIES/{date.year}/{date.strftime('%b').upper()}/cm{dold}bhav.csv.zip",
    ]
    for url in urls:
        try:
            r = SESSION.get(url, timeout=20)
            if r.status_code != 200: continue
            z  = zipfile.ZipFile(io.BytesIO(r.content))
            df = pd.read_csv(z.open(z.namelist()[0]))
            df.columns = df.columns.str.strip().str.upper()

            # Unified column mapping
            rename = {}
            for new, candidates in [
                ("SYMBOL",   ["TCKRSYMB","FININSTRMID"]),
                ("SERIES",   ["SCTYSRS"]),
                ("OPEN",     ["OPNPRIC","OPEN_PRICE"]),
                ("HIGH",     ["HGHPRIC","HIGH_PRICE"]),
                ("LOW",      ["LWPRIC","LOW_PRICE"]),
                ("CLOSE",    ["CLSPRIC","CLOSE_PRICE"]),
                ("VOLUME",   ["TOTTRDQTY","TTL_TRD_QNTY","TTLTRADGVOL"]),
                ("VALUE",    ["TOTTRDVAL","TTLTRFVAL","TURNOVER_LACS","TTLNBOFTXSEXCTD"]),
                ("DELIV_QTY",["DELIV_QTY"]),
                ("DELIV_PER",["DELIV_PER"]),
            ]:
                if new not in df.columns:
                    for c in candidates:
                        if c in df.columns: rename[c] = new; break
            df.rename(columns=rename, inplace=True)

            if "SYMBOL" not in df.columns: continue
            return df
        except: pass
    return None

def fetch_mcap(close_map):
    """
    MCap (Cr) = Close × PaidUp_Cr / FaceValue
    EQUITY_L PAID UP VALUE column is in Crores of Rs.
    Verified: TCS ~811066, MRF ~127040 ✓
    """
    url = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
    try:
        r = SESSION.get(url, timeout=20)
        if r.status_code != 200: return {}
        from io import StringIO
        for enc in ["utf-8","latin-1","cp1252"]:
            try:
                df = pd.read_csv(StringIO(r.content.decode(enc)))
                df.columns = df.columns.str.strip().str.upper()
                sc = next((c for c in df.columns if "SYMBOL" in c), None)
                pc = next((c for c in df.columns if "PAID"   in c), None)
                fc = next((c for c in df.columns if "FACE"   in c), None)
                if not (sc and pc and fc): continue
                df[pc] = pd.to_numeric(df[pc], errors="coerce")
                df[fc] = pd.to_numeric(df[fc], errors="coerce")
                out = {}
                for _, row in df.iterrows():
                    sym = str(row[sc]).strip().upper()
                    pu  = row[pc]; fv = row[fc]; cl = close_map.get(sym)
                    if pd.notna(pu) and pu>0 and pd.notna(fv) and fv>0 and cl and cl>0:
                        out[sym] = round((cl * pu) / fv, 2)
                chk = {s: out[s] for s in ["RELIANCE","TCS","MRF","INFY","HDFCBANK"] if s in out}
                print(f"  MCap: {len(out)} | checks: {chk}")
                return out
            except: continue
    except Exception as e:
        print(f"  EQUITY_L: {e}")
    return {}

def sma(s, n):
    c = s.dropna()
    return round(float(c.tail(n).mean()),2) if len(c)>=n else None

def rsi(s, n=14):
    c = s.dropna()
    if len(c) < n+2: return None
    d = c.diff()
    g = d.clip(lower=0).rolling(n).mean()
    l = (-d.clip(upper=0)).rolling(n).mean()
    v = (100 - 100/(1+g/l.replace(0,np.nan))).iloc[-1]
    return round(float(v),1) if pd.notna(v) else None

def avg(vals):
    v = [x for x in vals if x is not None and not np.isnan(x)]
    return round(sum(v)/len(v),2) if v else None


# ── Filters ───────────────────────────────────────────────────────────────────

def s1_ok(ltp, mcap, rsi_v, low52, high52, h60,
          d20, d50, nd, vr, tv, s30, s50):
    if mcap is None or not (S1_MCAP_MIN<=mcap<=S1_MCAP_MAX): return False
    if rsi_v is None or not (S1_RSI_MIN<=rsi_v<=S1_RSI_MAX): return False
    if s30 is None or ltp<=s30: return False                        # close > 30DMA
    if s50 is not None and ltp<=s50: return False                   # close > 50DMA (soft)
    if vr is not None and vr<S1_VOL_RATIO: return False             # vol ratio (soft)
    if tv is not None and tv<S1_TRADED_VALUE: return False          # traded value (soft)
    if low52 and low52>0:                                           # 52W range
        m = ltp/low52
        if not (S1_FROM_LOW_MIN<=m<=S1_FROM_LOW_MAX): return False
    if h60 and ltp < h60*S1_NEAR_HIGH: return False                 # near 60D high
    if nd>=10 and d20 is not None:                                  # delivery
        if d20<S1_DELIVERY_MIN: return False
        if nd>=20 and d50 is not None and (d20-d50)<S1_DELIVERY_GAP: return False
    return True

def s2_ok(ltp, mcap, rsi_v, low52, high52,
          d20, d50, nd, vr, tv, s20, s50, s200):
    if mcap is None or mcap<S2_MCAP_MIN: return False
    if rsi_v is None or not (S2_RSI_MIN<=rsi_v<=S2_RSI_MAX): return False
    if s20 is None or ltp<=s20: return False                        # close > 20DMA
    if s50 is None or ltp<=s50: return False                        # close > 50DMA
    if s20<=s50: return False                                       # 20DMA > 50DMA
    if s200 is not None and ltp<=s200: return False                 # close > 200DMA (soft)
    if vr is not None and vr<S2_VOL_RATIO: return False             # vol ratio (soft)
    if tv is not None and tv<S2_TRADED_VALUE: return False          # traded value (soft)
    if high52 and high52>0:                                         # 52W range
        ratio = ltp/high52
        if not (S2_FROM_HIGH_MIN<=ratio<=S2_FROM_HIGH_MAX): return False
    if nd>=10 and d20 is not None:                                  # delivery
        if d20<S2_DELIVERY_MIN: return False
    return True

def score_s1(d20,d50,vr,rsi_v,fl_pct,tv):
    s = 0.0
    if d20 and d20>=45:   s+=min((d20-45)/35*20,20)
    if d20 and d50 and (d20-d50)>3: s+=min((d20-d50-3)/12*20,20)
    if vr  and vr>=1.2:  s+=min((vr-1.2)/0.8*20,20)
    if rsi_v: s+=max(0,20-abs(rsi_v-55)*0.8)
    if fl_pct:
        if 15<=fl_pct<=30: s+=15
        elif 10<=fl_pct<15: s+=10
        elif 30<fl_pct<=40: s+=8
    if tv and tv>=7.5: s+=min((tv-7.5)/42.5*10,10)
    return min(int(round(s)),100)

def score_s2(d20,d50,vr,rsi_v,ltp,s20,s50,s200,tv):
    s = 0.0
    if d20 and d20>=40:   s+=min((d20-40)/40*20,20)
    if d20 and d50 and (d20-d50)>3: s+=min((d20-d50-3)/12*20,20)
    if vr  and vr>=1.2:  s+=min((vr-1.2)/0.8*20,20)
    if rsi_v: s+=max(0,20-abs(rsi_v-55)*0.8)
    ma = sum([ltp and s20  and ltp>s20,
              ltp and s50  and ltp>s50,
              ltp and s200 and ltp>s200])
    s+=ma*5
    if tv and tv>=25: s+=min((tv-25)/75*10,10)
    return min(int(round(s)),100)

def grade(score,strat,d20,d50,vr,fl_pct,nd):
    gap   = (d20-d50) if d20 and d50 else 0
    delok = nd>=10 and d20 is not None
    if strat=="s1":
        if dekok := delock if False else delock:  pass  # just reference delock
        if delock := delock if False else delock: pass
        pass
    pass

def grade(score, strat, d20, d50, vr, fl_pct, nd):
    gap   = (d20-d50) if d20 and d50 else 0
    dekok = nd>=10 and d20 is not None
    if strat=="s1":
        if dekok:
            if score>=82 and d20>=60 and gap>=8  and vr and vr>=1.5 and fl_pct and 15<=fl_pct<=30: return "A+"
            if score>=68 and d20>=52 and gap>=3  and vr and vr>=1.35 and fl_pct and fl_pct<=35:    return "A"
        else:
            if score>=82 and vr and vr>=1.5  and fl_pct and 15<=fl_pct<=30: return "A+"
            if score>=68 and vr and vr>=1.35 and fl_pct and fl_pct<=35:     return "A"
        if score>=52: return "B+"
        if score>=38: return "B"
    else:
        if dekok:
            if score>=82 and d20>=55 and gap>=8  and vr and vr>=1.5:  return "A+"
            if score>=68 and d20>=48 and gap>=3  and vr and vr>=1.35: return "A"
        else:
            if score>=82 and vr and vr>=1.5:  return "A+"
            if score>=68 and vr and vr>=1.35: return "A"
        if score>=52: return "B+"
        if score>=38: return "B"
    return "C"

def confidence(d20,d50,dlat,vr,rsi_v,fl_pct,sc,gr,dvals,ltp,s20,s30,s50,s150,s200,strat):
    sig = {}
    dq = 0
    if dvals and len(dvals)>=3:
        thr = 50 if strat=="s1" else 45
        dq  = min(sum(1 for x in dvals if x>=thr)/len(dvals)*20,20)
        if dlat and d20 and dlat>d20: dq=min(dq+3,20)
    elif d20 and d20>=45: dq=10
    sig["delivery_quality"]=round(dq,1)
    dt = min((d20-d50)/10*15,15) if d20 and d50 and d20>d50 else 0
    sig["delivery_trend"]=round(dt,1)
    vc = 20 if vr and vr>=2 else 16 if vr and vr>=1.75 else 12 if vr and vr>=1.5 else 8 if vr and vr>=1.3 else 5 if vr and vr>=1.2 else 0
    sig["volume_expansion"]=round(vc,1)
    rq = 15 if rsi_v and 50<=rsi_v<=62 else 12 if rsi_v and 45<=rsi_v<=65 else 8 if rsi_v and 40<=rsi_v<=70 else 0
    sig["rsi_zone"]=round(rq,1)
    rr = 0
    if strat=="s1" and fl_pct:
        if 15<=fl_pct<=30: rr=15
        elif 10<=fl_pct<15: rr=11
        elif 30<fl_pct<=40: rr=8
    elif strat=="s2":
        rr = sum([ltp and s20 and ltp>s20, ltp and s50 and ltp>s50, ltp and s200 and ltp>s200])*5
    sig["price_position"]=round(rr,1)
    ta = 0
    if strat=="s1":
        if ltp and s30 and ltp>s30: ta+=5
        if ltp and s50 and ltp>s50: ta+=5
    else:
        if ltp and s20 and s50 and ltp>s20 and s20>s50: ta=10
        elif ltp and s20 and ltp>s20: ta=5
    sig["trend_alignment"]=round(ta,1)
    gb = {"A+":10,"A":8,"B+":5,"B":2}.get(gr,0)
    sig["grade_bonus"]=gb
    tot   = min(int(round(dq+dt+vc+rq+rr+ta+gb)),100)
    label = "VERY HIGH" if tot>=85 else "HIGH" if tot>=70 else "MODERATE" if tot>=55 else "LOW" if tot>=40 else "VERY LOW"
    return tot, sig, label


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

print("="*60)
print("MATRIX 18.1 — DUAL STRATEGY SCAN")
print(f"Started : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print(f"PAT     : {'SET' if GITHUB_TOKEN else 'NOT SET'}")
print("="*60)
print("Data    : BhavCopy ZIP × 260 days (single source)")
print("         → OHLCV + DELIV_PER + VALUE all from bhavcopy")
print("         → 52W high/low = max/min of 252D HIGH/LOW")
print("         → MCap = EQUITY_L: Close × PaidUp_Cr / FaceValue")

# ── Download 260 days of bhavcopy in parallel ─────────────────────────────────
dates = trading_dates(DAYS)
print(f"\nDownloading {len(dates)} days bhavcopy (parallel, 10 workers)...")

frames = {}   # date_str → DataFrame

def _dl(date):
    return date, fetch_bhavcopy(date)

with ThreadPoolExecutor(max_workers=10) as ex:
    futs = {ex.submit(_dl, d): d for d in dates}
    done = 0
    for fut in as_completed(futs):
        date, df = fut.result()
        if df is None: continue
        ds = date.strftime("%Y-%m-%d")
        # Filter EQ series
        eq = df[df["SERIES"].astype(str).str.strip()=="EQ"].copy() if "SERIES" in df.columns else df.copy()
        # Numeric
        for col in ["OPEN","HIGH","LOW","CLOSE","VOLUME","VALUE","DELIV_QTY","DELIV_PER"]:
            if col in eq.columns:
                eq[col] = pd.to_numeric(eq[col], errors="coerce")
        eq["DATE"] = ds
        frames[ds] = eq
        done += 1
        if done % 50 == 0:
            print(f"  {done}/{len(dates)} loaded")

print(f"  Total: {len(frames)} days loaded")
if len(frames) < 20:
    out = {"stocks":[],"count":0,"fetchedAt":datetime.now().isoformat(),"autoRun":True}
    save_github(json.dumps(out)); exit(0)

# ── Check delivery availability ───────────────────────────────────────────────
del_days = 0
for ds, df in frames.items():
    if "DELIV_PER" in df.columns and (df["DELIV_PER"]>0).sum() > 100:
        del_days += 1
print(f"  Days with delivery data: {del_days}")

# First date's columns for reference
sample_ds = sorted(frames.keys())[-1]
print(f"  Latest date : {sample_ds}")
print(f"  Columns     : {list(frames[sample_ds].columns)}")
sample_row = frames[sample_ds].dropna(subset=["CLOSE"]).iloc[0]
print(f"  Sample row  : { {k:v for k,v in sample_row.items() if k in ['SYMBOL','CLOSE','VOLUME','DELIV_PER','VALUE']} }")

# ── Build combined dataframe ───────────────────────────────────────────────────
all_df = pd.concat(list(frames.values()), ignore_index=True)
all_df["DATE"] = pd.to_datetime(all_df["DATE"])
all_df.sort_values(["SYMBOL","DATE"], inplace=True)
all_df.drop_duplicates(subset=["SYMBOL","DATE"], keep="last", inplace=True)

# ── Latest closes for MCap ────────────────────────────────────────────────────
lat_df       = frames[sample_ds]
latest_close = {}
if "CLOSE" in lat_df.columns and "SYMBOL" in lat_df.columns:
    for _, row in lat_df.dropna(subset=["CLOSE"]).iterrows():
        sym = str(row["SYMBOL"]).strip().upper()
        if row["CLOSE"] > 0: latest_close[sym] = float(row["CLOSE"])
print(f"\nLatest closes: {len(latest_close)}")

# ── MCap ──────────────────────────────────────────────────────────────────────
print("Loading MCap...")
mcap_map   = fetch_mcap(latest_close)
mcap_avail = len(mcap_map) > 0

symbols = all_df["SYMBOL"].dropna().unique()
print(f"\nTotal EQ symbols: {len(symbols)}")
print(f"Days of data    : {len(frames)}")
print(f"Delivery days   : {del_days}")

# ── Screening loop ────────────────────────────────────────────────────────────
results  = []
s1n = s2n = proc = 0
sk = {k:0 for k in ["rows","close","mcap","s1","s2","gc"]}

for sym in symbols:
    try:
        sdf  = all_df[all_df["SYMBOL"]==sym].copy()
        proc += 1
        if proc % 500 == 0:
            print(f"  {proc}/{len(symbols)} results={len(results)} s1={s1n} s2={s2n}")

        if len(sdf) < 20: sk["rows"]+=1; continue
        closes = sdf["CLOSE"].dropna()
        if len(closes) < 20: sk["close"]+=1; continue
        ltp = float(closes.iloc[-1])
        if ltp <= 0: sk["close"]+=1; continue

        sym_u  = str(sym).strip().upper()
        ltp    = round(ltp,2)
        change = round((ltp - float(closes.iloc[-2]))/float(closes.iloc[-2])*100,2) if len(closes)>=2 else 0.0

        # 52W from 252D of HIGH/LOW in bhavcopy
        if "HIGH" in sdf.columns and "LOW" in sdf.columns:
            h52 = round(float(sdf["HIGH"].dropna().max()),2)
            l52 = round(float(sdf["LOW"].dropna().min()),2)
        else:
            h52 = round(float(closes.max()),2)
            l52 = round(float(closes.min()),2)

        fl_pct  = round((ltp-l52)/l52*100,2)  if l52>0 else None
        fh_pct  = round((h52-ltp)/h52*100,2)  if h52>0 else None
        h60     = round(float(closes.tail(60).max()),2) if len(closes)>=20 else None

        # MCap
        mcap = mcap_map.get(sym_u)
        if mcap_avail and mcap is None: sk["mcap"]+=1; continue

        # Volume
        vols   = sdf["VOLUME"].dropna() if "VOLUME" in sdf.columns else pd.Series()
        vol_td = int(vols.iloc[-1]) if len(vols)>0 else 0
        v20    = float(vols.tail(20).mean()) if len(vols)>=20 else None
        v50    = float(vols.tail(50).mean()) if len(vols)>=50 else None
        vr     = round(v20/v50,3) if v20 and v50 and v50>0 else None

        # Traded value (TOTTRDVAL in lakhs → /100 = Cr)
        tv_col = "VALUE" if "VALUE" in sdf.columns else None
        avg_tv = None
        if tv_col:
            tv_s = pd.to_numeric(sdf[tv_col], errors="coerce").dropna()
            if len(tv_s) >= 5:
                avg_tv = round(float(tv_s.tail(20).mean())/100, 2)

        # SMAs
        s20  = sma(closes,20)
        s30  = sma(closes,30)  if len(closes)>=30  else None
        s50  = sma(closes,50)  if len(closes)>=50  else None
        s150 = sma(closes,150) if len(closes)>=150 else None
        s200 = sma(closes,200) if len(closes)>=200 else None
        rsi_v = rsi(closes)

        # Delivery
        dvals = []
        if "DELIV_PER" in sdf.columns:
            dp = pd.to_numeric(sdf["DELIV_PER"], errors="coerce")
            dvals = [float(x) for x in dp if pd.notna(x) and 0<x<=100]
        d20   = avg(dvals[-20:]) if len(dvals)>=1  else None
        d50   = avg(dvals[-50:]) if len(dvals)>=50 else None
        dlat  = dvals[-1] if dvals else None
        nd    = min(len(dvals), 20)

        # Strategy
        strat = None
        if s1_ok(ltp,mcap,rsi_v,l52,h52,h60,d20,d50,nd,vr,avg_tv,s30,s50):
            strat = "s1"
        elif s2_ok(ltp,mcap,rsi_v,l52,h52,d20,d50,nd,vr,avg_tv,s20,s50,s200):
            strat = "s2"

        if strat is None:
            if mcap and mcap<=S1_MCAP_MAX: sk["s1"]+=1
            else: sk["s2"]+=1
            continue

        sc = score_s1(d20,d50,vr,rsi_v,fl_pct,avg_tv) if strat=="s1" \
             else score_s2(d20,d50,vr,rsi_v,ltp,s20,s50,s200,avg_tv)
        gr = grade(sc,strat,d20,d50,vr,fl_pct,nd)
        if gr=="C": sk["gc"]+=1; continue

        cf,sigs,clbl = confidence(d20,d50,dlat,vr,rsi_v,fl_pct,sc,gr,dvals[-20:],
                                   ltp,s20,s30,s50,s150,s200,strat)
        if strat=="s1": s1n+=1
        else: s2n+=1

        results.append({
            "symbol":str(sym), "strategy":strat,
            "strategyLabel":"Mid/Small Cap" if strat=="s1" else "Large/Mega Cap",
            "ltp":ltp, "change":change,
            "high52w":h52, "low52w":l52,
            "fromLow":fl_pct, "fromHigh":fh_pct,
            "volume":vol_td, "mcap":round(mcap,0) if mcap else None,
            "tradedValueCr":avg_tv,
            "rsi":rsi_v, "sma20":s20, "sma30":s30, "sma50":s50, "sma150":s150, "sma200":s200,
            "volRatio":vr,
            "avgDelivery":d20, "avgDelivery50":d50,
            "deliveryTrend":round(d20-d50,2) if d20 and d50 else None,
            "deliveryToday":dlat, "deliveryDays":nd,
            "score":sc, "grade":gr, "daysOfData":len(sdf),
            "confidence":cf, "confidenceLabel":clbl, "confidenceBreakdown":sigs,
        })
    except Exception as e:
        print(f"  ERR {sym}: {e}")

print(f"\nResults : {len(results)} (S1:{s1n} S2:{s2n})")
print(f"Skipped : rows={sk['rows']} close={sk['close']} mcap={sk['mcap']} s1={sk['s1']} s2={sk['s2']} gc={sk['gc']}")
results.sort(key=lambda x: x["confidence"] or 0, reverse=True)

out = {
    "stocks":results, "count":len(results),
    "s1Count":s1n, "s2Count":s2n,
    "fetchedAt":datetime.now().isoformat(), "autoRun":True,
    "daysOfData":len(frames),
    "dataAvailability":{"mcap":mcap_avail,"delivery":del_days>0,
                        "deliveryDays":del_days,"sma200":len(frames)>=200},
}
content = json.dumps(out)
os.makedirs("results",exist_ok=True)
with open("results/matrix181_results.json","w") as f: f.write(content)
print(f"Saved : {os.path.getsize('results/matrix181_results.json')} bytes")
print(f"API   : {'OK' if save_github(content) else 'FAIL'}")
print("="*60)
print(f"DONE {len(results)} | S1:{s1n} S2:{s2n}")
print(f"A+:{sum(1 for r in results if r['grade']=='A+')} "
      f"A:{sum(1 for r in results if r['grade']=='A')} "
      f"B+:{sum(1 for r in results if r['grade']=='B+')} "
      f"B:{sum(1 for r in results if r['grade']=='B')}")
print("="*60)
