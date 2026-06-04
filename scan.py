"""
MATRIX 18.1 — NSE Dual Strategy Screener
=========================================
Data sources (all confirmed working from GitHub Actions):

1. StocksTraded.csv  (daily, from NSE all-reports)
   URL: https://nsearchives.nseindia.com/content/equities/StocksTraded.csv
   Cols: SYMBOL, SERIES, LTP, %CHNG, MKT CAP (₹ CRORES), VOLUME (LAKHS), VALUE (₹ CRORES)
   → MCap (exact), today's LTP, Volume, Value

2. BhavCopy ZIP  (all historical dates, 260 days)
   URL: https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip
   Cols: TCKRSYMB→SYMBOL, HGHPRIC→HIGH, LWPRIC→LOW, CLSPRIC→CLOSE,
         TTLTRADGVOL→VOLUME, TTLTRFVAL→VALUE_RS (rupees)
   → OHLCV for SMAs, 52W High/Low, Volume ratio

3. sec_bhavdata_full (recent ~25 days)
   URL: https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv
   Cols: SYMBOL, CLOSE_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, DELIV_QTY, DELIV_PER
   → Delivery % (only reliable source, ~25 days)
"""

import pandas as pd
import numpy as np
import requests, zipfile, io, json, os, base64
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

HDR = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "*/*", "Accept-Encoding": "gzip, deflate",
    "Referer": "https://www.nseindia.com/",
}
S = requests.Session()
S.headers.update(HDR)

PAT   = os.environ.get("PAT_TOKEN","")
GUSER = "gauravmsm"
GREPO = "matrix181-backend"
GPATH = "results/matrix181_results.json"

# ── Strategy Parameters ───────────────────────────────────────────────────────
S1_MCAP_MIN  = 1500;   S1_MCAP_MAX  = 30000   # Cr
S1_RSI_MIN   = 40.0;   S1_RSI_MAX   = 70.0
S1_VOL_RATIO = 1.20    # 20D/50D avg volume ratio (soft)
S1_DEL_MIN   = 45.0    # 20D avg delivery % (needs 10+ days)
S1_DEL_GAP   = 3.0     # 20D > 50D + 3% (needs 20+ days)
S1_TV_MIN    = 7.5     # Cr/day avg (soft)
S1_LOW_MIN   = 1.10    # price >= 52W_low * 1.10
S1_LOW_MAX   = 1.40    # price <= 52W_low * 1.40
S1_H60       = 0.90    # price >= 60D_high_close * 0.90

S2_MCAP_MIN  = 30000                           # Cr
S2_RSI_MIN   = 40.0;   S2_RSI_MAX   = 70.0
S2_VOL_RATIO = 1.20    # soft
S2_DEL_MIN   = 40.0    # needs 10+ days
S2_TV_MIN    = 25.0    # soft
S2_HIGH_MIN  = 0.65    # price >= 52W_high * 0.65 (within 35%)
S2_HIGH_MAX  = 0.95    # price <= 52W_high * 0.95

DAYS_BHAV = 260
DAYS_FULL =  60

def push_github(text):
    if not PAT: return False
    url = f"https://api.github.com/repos/{GUSER}/{GREPO}/contents/{GPATH}"
    h   = {"Authorization":f"token {PAT}","Accept":"application/vnd.github.v3+json"}
    sha = None
    try:
        r = requests.get(url,headers=h,timeout=15)
        if r.status_code==200: sha=r.json().get("sha")
    except: pass
    body = {"message":f"scan {datetime.now():%Y-%m-%d %H:%M}",
            "content":base64.b64encode(text.encode()).decode(),"branch":"main"}
    if sha: body["sha"]=sha
    try:
        r = requests.put(url,headers=h,json=body,timeout=30)
        ok = r.status_code in (200,201)
        print(f"  GitHub {'OK' if ok else 'FAIL'} ({r.status_code})")
        return ok
    except Exception as e:
        print(f"  GitHub error: {e}"); return False

def trdates(n):
    out,d=[],datetime.now()
    while len(out)<n:
        d-=timedelta(days=1)
        if d.weekday()<5: out.append(d)
    return out

# ── Fetch StocksTraded.csv (MCap + today's data) ──────────────────────────────
def fetch_stocks_traded():
    """
    NSE daily StocksTraded.csv
    Cols: SYMBOL, SERIES, LTP, %CHNG, MKT CAP (₹ CRORES), VOLUME (LAKHS), VALUE (₹ CRORES)
    Returns dict: {SYMBOL: {mcap, ltp, chg, vol_lakhs, value_cr}}
    """
    url = "https://nsearchives.nseindia.com/content/equities/StocksTraded.csv"
    try:
        r = S.get(url, timeout=20)
        if r.status_code != 200:
            print(f"  StocksTraded: HTTP {r.status_code}"); return {}
        from io import StringIO
        df = pd.read_csv(StringIO(r.content.decode("utf-8")))
        df.columns = df.columns.str.strip().str.upper()
        # Keep EQ series only
        if "SERIES" in df.columns:
            df = df[df["SERIES"].astype(str).str.strip()=="EQ"].copy()
        # Clean numeric cols (remove commas)
        for col in df.columns:
            if col not in ["SYMBOL","SERIES"]:
                df[col] = pd.to_numeric(
                    df[col].astype(str).str.replace(",","").str.strip(),
                    errors="coerce"
                )
        sym_col  = "SYMBOL"
        mcap_col = next((c for c in df.columns if "MKT" in c and "CAP" in c), None)
        ltp_col  = next((c for c in df.columns if c in ["LTP","CLOSE","LASTPRICE"]), None)
        chg_col  = next((c for c in df.columns if "CHNG" in c or "CHANGE" in c), None)
        vol_col  = next((c for c in df.columns if "VOLUME" in c), None)
        val_col  = next((c for c in df.columns if "VALUE" in c), None)

        print(f"  StocksTraded cols: {list(df.columns)}")
        print(f"  Using: mcap={mcap_col} ltp={ltp_col} vol={vol_col} val={val_col}")
        print(f"  EQ rows: {len(df)}")

        out = {}
        for _,row in df.iterrows():
            sym = str(row[sym_col]).strip().upper()
            if not sym: continue
            mcap = float(row[mcap_col]) if mcap_col and pd.notna(row.get(mcap_col)) else None
            ltp  = float(row[ltp_col])  if ltp_col  and pd.notna(row.get(ltp_col))  else None
            chg  = float(row[chg_col])  if chg_col  and pd.notna(row.get(chg_col))  else 0.0
            vol  = float(row[vol_col])  if vol_col  and pd.notna(row.get(vol_col))  else None
            val  = float(row[val_col])  if val_col  and pd.notna(row.get(val_col))  else None
            out[sym] = {"mcap":mcap,"ltp":ltp,"chg":chg,
                        "vol_lakhs":vol,"value_cr":val}

        # Verify
        for chk in ["RELIANCE","TCS","MRF","TATAMOTORS","HDFCBANK"]:
            if chk in out:
                print(f"  {chk}: MCap={out[chk]['mcap']:,.0f} Cr | LTP={out[chk]['ltp']}")
        return out
    except Exception as e:
        print(f"  StocksTraded error: {e}"); return {}

# ── Fetch BhavCopy ZIP ────────────────────────────────────────────────────────
def fetch_bhav(date):
    """
    Returns EQ DataFrame with cols: SYMBOL, OPEN, HIGH, LOW, CLOSE, VOLUME, VALUE_RS, DATE
    VALUE_RS = TTLTRFVAL (in rupees — /1e7 = Cr)
    """
    d    = date.strftime("%Y%m%d")
    dold = date.strftime("%d%b%Y").upper()
    for url in [
        f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d}_F_0000.csv.zip",
        f"https://www.nseindia.com/content/historical/EQUITIES/{date.year}/{date.strftime('%b').upper()}/cm{dold}bhav.csv.zip",
    ]:
        try:
            r = S.get(url, timeout=20)
            if r.status_code != 200: continue
            z  = zipfile.ZipFile(io.BytesIO(r.content))
            df = pd.read_csv(z.open(z.namelist()[0]))
            df.columns = df.columns.str.strip().str.upper()
            # EQ series
            sc = next((c for c in df.columns if c in ["SCTYSRS","SERIES"]),None)
            if sc: df = df[df[sc].astype(str).str.strip()=="EQ"].copy()
            # Rename to unified schema
            rn = {}
            for new,cands in [
                ("SYMBOL", ["TCKRSYMB","FININSTRMID","SYMBOL"]),
                ("OPEN",   ["OPNPRIC","OPEN"]),
                ("HIGH",   ["HGHPRIC","HIGH"]),
                ("LOW",    ["LWPRIC","LOW"]),
                ("CLOSE",  ["CLSPRIC","CLOSE"]),
                ("VOLUME", ["TTLTRADGVOL","TOTTRDQTY","VOLUME"]),
                ("VALUE_RS",["TTLTRFVAL","TOTTRDVAL","VALUE"]),
            ]:
                if new not in df.columns:
                    for c in cands:
                        if c in df.columns: rn[c]=new; break
            df.rename(columns=rn, inplace=True)
            if "SYMBOL" not in df.columns: continue
            for col in ["OPEN","HIGH","LOW","CLOSE","VOLUME","VALUE_RS"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            df["DATE"] = date.strftime("%Y-%m-%d")
            return df
        except: pass
    return None

# ── Fetch sec_bhavdata_full (delivery data) ───────────────────────────────────
def fetch_full(date):
    """
    Returns EQ DataFrame with SYMBOL, CLOSE, VOLUME, DELIV_PER
    Only available for recent ~25 days.
    """
    url = f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{date.strftime('%d%m%Y')}.csv"
    try:
        r = S.get(url, timeout=20)
        if r.status_code!=200 or len(r.content)<5000: return None
        from io import StringIO
        df = pd.read_csv(StringIO(r.content.decode("latin-1")))
        df.columns = df.columns.str.strip().str.upper()
        if "SERIES" in df.columns:
            df = df[df["SERIES"].astype(str).str.strip()=="EQ"].copy()
        rn = {}
        for new,cands in [
            ("SYMBOL", ["SYMBOL"]),
            ("CLOSE",  ["CLOSE_PRICE"]),
            ("VOLUME", ["TTL_TRD_QNTY"]),
        ]:
            if new not in df.columns:
                for c in cands:
                    if c in df.columns: rn[c]=new; break
        df.rename(columns=rn, inplace=True)
        for col in ["CLOSE","VOLUME","DELIV_QTY","DELIV_PER"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df["DATE"] = date.strftime("%Y-%m-%d")
        return df
    except: return None

def sma(s,n):
    c=s.dropna()
    return round(float(c.tail(n).mean()),2) if len(c)>=n else None

def rsi14(s):
    c=s.dropna()
    if len(c)<16: return None
    d=c.diff()
    g=d.clip(lower=0).rolling(14).mean()
    l=(-d.clip(upper=0)).rolling(14).mean()
    v=(100-100/(1+g/l.replace(0,np.nan))).iloc[-1]
    return round(float(v),1) if pd.notna(v) else None

def cavg(vals):
    v=[x for x in vals if x is not None and pd.notna(x)]
    return round(sum(v)/len(v),2) if v else None

# ── Filters ───────────────────────────────────────────────────────────────────
def chk_s1(ltp,mcap,rv,l52,h52,h60,d20,d50,nd,vr,tv,s30):
    if mcap is None or not (S1_MCAP_MIN<=mcap<=S1_MCAP_MAX): return False
    if rv is None or not (S1_RSI_MIN<=rv<=S1_RSI_MAX): return False
    if s30 is None or ltp<=s30: return False
    if vr is not None and vr<S1_VOL_RATIO: return False
    if tv is not None and tv<S1_TV_MIN: return False
    if l52 and l52>0:
        m=ltp/l52
        if not (S1_LOW_MIN<=m<=S1_LOW_MAX): return False
    if h60 and ltp<h60*S1_H60: return False
    if nd>=10 and d20 is not None:
        if d20<S1_DEL_MIN: return False
    if nd>=20 and d20 is not None and d50 is not None:
        if (d20-d50)<S1_DEL_GAP: return False
    return True

def chk_s2(ltp,mcap,rv,l52,h52,d20,d50,nd,vr,tv,s20,s50,s200):
    if mcap is None or mcap<S2_MCAP_MIN: return False
    if rv is None or not (S2_RSI_MIN<=rv<=S2_RSI_MAX): return False
    if s20 is None or ltp<=s20: return False
    if s50 is None or ltp<=s50: return False
    if s20<=s50: return False
    if s200 is not None and ltp<=s200: return False
    if vr is not None and vr<S2_VOL_RATIO: return False
    if tv is not None and tv<S2_TV_MIN: return False
    if h52 and h52>0:
        ratio=ltp/h52
        if not (S2_HIGH_MIN<=ratio<=S2_HIGH_MAX): return False
    if nd>=10 and d20 is not None:
        if d20<S2_DEL_MIN: return False
    return True

def sc_s1(d20,d50,vr,rv,fp,tv):
    s=0.0
    if d20 and d20>=45: s+=min((d20-45)/35*25,25)
    if d20 and d50 and (d20-d50)>3: s+=min((d20-d50-3)/12*20,20)
    if vr and vr>=1.2: s+=min((vr-1.2)/0.8*20,20)
    if rv: s+=max(0,20-abs(rv-55)*0.8)
    if fp:
        if 15<=fp<=30: s+=15
        elif 10<=fp<15: s+=10
        elif 30<fp<=40: s+=8
    if tv and tv>=7.5: s+=min((tv-7.5)/42.5*10,10)
    return min(int(round(s)),100)

def sc_s2(d20,d50,vr,rv,ltp,s20,s50,s200,tv):
    s=0.0
    if d20 and d20>=40: s+=min((d20-40)/40*25,25)
    if d20 and d50 and (d20-d50)>3: s+=min((d20-d50-3)/12*20,20)
    if vr and vr>=1.2: s+=min((vr-1.2)/0.8*20,20)
    if rv: s+=max(0,20-abs(rv-55)*0.8)
    ma=sum([bool(ltp and s20 and ltp>s20),
            bool(ltp and s50 and ltp>s50),
            bool(ltp and s200 and ltp>s200)])
    s+=ma*5
    if tv and tv>=25: s+=min((tv-25)/75*10,10)
    return min(int(round(s)),100)

def grade(sc,st,d20,d50,vr,fp,nd):
    gap=(d20-d50) if d20 and d50 else 0
    dok=nd>=10 and d20 is not None
    if st=="s1":
        if dok:
            if sc>=82 and d20>=60 and gap>=8 and vr and vr>=1.5 and fp and 15<=fp<=30: return "A+"
            if sc>=68 and d20>=52 and gap>=3 and vr and vr>=1.35 and fp and fp<=35:    return "A"
        else:
            if sc>=82 and vr and vr>=1.5 and fp and 15<=fp<=30: return "A+"
            if sc>=68 and vr and vr>=1.35 and fp and fp<=35:    return "A"
        if sc>=52: return "B+"
        if sc>=38: return "B"
    else:
        if dok:
            if sc>=82 and d20>=55 and gap>=8 and vr and vr>=1.5:  return "A+"
            if sc>=68 and d20>=48 and gap>=3 and vr and vr>=1.35: return "A"
        else:
            if sc>=82 and vr and vr>=1.5:  return "A+"
            if sc>=68 and vr and vr>=1.35: return "A"
        if sc>=52: return "B+"
        if sc>=38: return "B"
    return "C"

def conf(d20,d50,dlat,vr,rv,fp,sc,gr,dvals,ltp,s20,s30,s50,s150,s200,st):
    sig={}
    dq=0
    if dvals and len(dvals)>=3:
        thr=50 if st=="s1" else 45
        dq=min(sum(1 for x in dvals if x>=thr)/len(dvals)*20,20)
        if dlat and d20 and dlat>d20: dq=min(dq+3,20)
    elif d20 and d20>=45: dq=10
    sig["delivery_quality"]=round(dq,1)
    dt=min((d20-d50)/10*15,15) if d20 and d50 and d20>d50 else 0
    sig["delivery_trend"]=round(dt,1)
    vc=(20 if vr and vr>=2 else 16 if vr and vr>=1.75 else 12 if vr and vr>=1.5
        else 8 if vr and vr>=1.3 else 5 if vr and vr>=1.2 else 0)
    sig["volume_expansion"]=round(vc,1)
    rq=(15 if rv and 50<=rv<=62 else 12 if rv and 45<=rv<=65
        else 8 if rv and 40<=rv<=70 else 0)
    sig["rsi_zone"]=round(rq,1)
    rr=0
    if st=="s1" and fp:
        if 15<=fp<=30: rr=15
        elif 10<=fp<15: rr=11
        elif 30<fp<=40: rr=8
    elif st=="s2":
        rr=sum([bool(ltp and s20 and ltp>s20),
                bool(ltp and s50 and ltp>s50),
                bool(ltp and s200 and ltp>s200)])*5
    sig["price_position"]=round(rr,1)
    ta=0
    if st=="s1":
        if ltp and s30 and ltp>s30: ta+=5
        if ltp and s50 and ltp>s50: ta+=5
    else:
        if ltp and s20 and s50 and ltp>s20 and s20>s50: ta=10
        elif ltp and s20 and ltp>s20: ta=5
    sig["trend_alignment"]=round(ta,1)
    gb={"A+":10,"A":8,"B+":5,"B":2}.get(gr,0)
    sig["grade_bonus"]=gb
    tot=min(int(round(dq+dt+vc+rq+rr+ta+gb)),100)
    lbl=("VERY HIGH" if tot>=85 else "HIGH" if tot>=70
         else "MODERATE" if tot>=55 else "LOW" if tot>=40 else "VERY LOW")
    return tot,sig,lbl


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

print("="*60)
print("MATRIX 18.1 — DUAL STRATEGY SCAN")
print(f"Started : {datetime.now():%Y-%m-%d %H:%M:%S}")
print(f"PAT     : {'SET' if PAT else 'NOT SET'}")
print("="*60)

# ── Step 1: StocksTraded.csv — MCap + today's data ────────────────────────────
print("\nFetching StocksTraded.csv (MCap + LTP)...")
stocks_today = fetch_stocks_traded()
print(f"  Loaded: {len(stocks_today)} EQ stocks")
s1_eligible = sum(1 for v in stocks_today.values() if v["mcap"] and S1_MCAP_MIN<=v["mcap"]<=S1_MCAP_MAX)
s2_eligible = sum(1 for v in stocks_today.values() if v["mcap"] and v["mcap"]>S2_MCAP_MIN)
print(f"  S1 eligible (MCap 1500-30K): {s1_eligible}")
print(f"  S2 eligible (MCap >30K):     {s2_eligible}")

# ── Step 2: BhavCopy ZIP — 260 days OHLCV ────────────────────────────────────
all_dates = trdates(DAYS_BHAV)
print(f"\nPhase 1: {len(all_dates)} days BhavCopy ZIP (parallel)...")
bhav_frames = {}

def _bhav(d): return d, fetch_bhav(d)

with ThreadPoolExecutor(max_workers=10) as ex:
    futs = {ex.submit(_bhav,d):d for d in all_dates}
    done=0
    for fut in as_completed(futs):
        dt,df=fut.result()
        if df is None: continue
        ds=dt.strftime("%Y-%m-%d")
        bhav_frames[ds]=df
        done+=1
        if done%50==0: print(f"  {done}/{len(all_dates)} loaded")

print(f"  Loaded: {len(bhav_frames)} days")

# ── Step 3: sec_bhavdata_full — delivery data ─────────────────────────────────
full_dates = trdates(DAYS_FULL)
print(f"\nPhase 2: {len(full_dates)} days sec_bhavdata_full (delivery)...")
full_frames = {}

def _full(d): return d, fetch_full(d)

with ThreadPoolExecutor(max_workers=6) as ex:
    futs={ex.submit(_full,d):d for d in full_dates}
    for fut in as_completed(futs):
        dt,df=fut.result()
        if df is None: continue
        ds=dt.strftime("%Y-%m-%d")
        full_frames[ds]=df

del_days=len(full_frames)
print(f"  Loaded: {del_days} days with DELIV_PER")
if full_frames:
    s=sorted(full_frames.keys())[-1]
    df_s=full_frames[s]
    if "DELIV_PER" in df_s.columns:
        valid=(df_s["DELIV_PER"]>0).sum()
        sample=df_s[df_s["DELIV_PER"]>0][["SYMBOL","CLOSE","DELIV_PER"]].head(3)
        print(f"  Latest ({s}): {valid} stocks with delivery")
        print(f"  Sample:\n{sample.to_string(index=False)}")

# ── Step 4: Build combined DataFrame ─────────────────────────────────────────
all_df = pd.concat(list(bhav_frames.values()), ignore_index=True)
for col in ["OPEN","HIGH","LOW","CLOSE","VOLUME","VALUE_RS"]:
    if col in all_df.columns:
        all_df[col] = pd.to_numeric(all_df[col], errors="coerce")
all_df["DATE"] = pd.to_datetime(all_df["DATE"])
all_df["SYMBOL"] = all_df["SYMBOL"].astype(str).str.strip().str.upper()
all_df.sort_values(["SYMBOL","DATE"], inplace=True)
all_df.drop_duplicates(subset=["SYMBOL","DATE"], keep="last", inplace=True)

# Merge delivery into all_df
if full_frames:
    full_df = pd.concat(list(full_frames.values()), ignore_index=True)
    full_df["SYMBOL"] = full_df["SYMBOL"].astype(str).str.strip().str.upper()
    full_df["DATE"]   = pd.to_datetime(full_df["DATE"])
    if "DELIV_PER" in full_df.columns:
        full_df["DELIV_PER"] = pd.to_numeric(full_df["DELIV_PER"], errors="coerce")
    del_merge = full_df[["SYMBOL","DATE","DELIV_PER"]].copy()
    all_df = all_df.merge(del_merge, on=["SYMBOL","DATE"], how="left")

# Only screen stocks present in StocksTraded (have MCap today)
symbols_with_mcap = set(stocks_today.keys())
symbols_in_bhav   = set(all_df["SYMBOL"].unique())
symbols = list(symbols_with_mcap & symbols_in_bhav)

print(f"\n{'='*50}")
print(f"BhavCopy days : {len(bhav_frames)}")
print(f"Delivery days : {del_days}")
print(f"Stocks today  : {len(stocks_today)}")
print(f"In BhavCopy   : {len(symbols_in_bhav)}")
print(f"Screening     : {len(symbols)} stocks (have MCap + history)")

if len(symbols) < 10:
    out={"stocks":[],"count":0,"fetchedAt":datetime.now().isoformat(),"autoRun":True}
    push_github(json.dumps(out)); exit(0)

# ── Step 5: Screening loop ────────────────────────────────────────────────────
results=[]; s1n=s2n=proc=0
sk={k:0 for k in ["rows","close","mcap","s1","s2","gc"]}

for sym in symbols:
    try:
        sdf  = all_df[all_df["SYMBOL"]==sym].copy()
        proc+=1
        if proc%500==0:
            print(f"  {proc}/{len(symbols)} res={len(results)} s1={s1n} s2={s2n}")

        if len(sdf)<20: sk["rows"]+=1; continue
        closes = sdf["CLOSE"].dropna()
        if len(closes)<20: sk["close"]+=1; continue

        # Use StocksTraded for today's LTP (most accurate) else bhav close
        st_data = stocks_today.get(sym,{})
        ltp   = st_data.get("ltp") or round(float(closes.iloc[-1]),2)
        chg   = st_data.get("chg",0.0)
        mcap  = st_data.get("mcap")
        tv_today = st_data.get("value_cr")  # Cr from StocksTraded

        if mcap is None: sk["mcap"]+=1; continue
        ltp = round(float(ltp),2)

        # 52W from 260 days of HIGH/LOW
        h52 = round(float(sdf["HIGH"].dropna().max()),2) if "HIGH" in sdf.columns else ltp
        l52 = round(float(sdf["LOW"].dropna().min()),2)  if "LOW"  in sdf.columns else ltp
        fp  = round((ltp-l52)/l52*100,2) if l52>0 else None
        fh  = round((h52-ltp)/h52*100,2) if h52>0 else None
        h60 = round(float(closes.tail(60).max()),2)

        # Volume ratio (20D/50D from bhav)
        vols  = sdf["VOLUME"].dropna() if "VOLUME" in sdf.columns else pd.Series(dtype=float)
        vol_td = int(vols.iloc[-1]) if len(vols)>0 else 0
        v20   = float(vols.tail(20).mean()) if len(vols)>=20 else None
        v50   = float(vols.tail(50).mean()) if len(vols)>=50 else None
        vr    = round(v20/v50,3) if v20 and v50 and v50>0 else None

        # Traded value — use today's from StocksTraded, else compute from bhav
        tv = tv_today
        if tv is None and "VALUE_RS" in sdf.columns:
            tvs = sdf["VALUE_RS"].dropna()
            if len(tvs)>=5:
                raw = float(tvs.tail(20).mean())
                tv  = round(raw/1e7, 2)  # rupees → Cr

        # SMAs
        s20  = sma(closes,20)
        s30  = sma(closes,30)  if len(closes)>=30  else None
        s50  = sma(closes,50)  if len(closes)>=50  else None
        s150 = sma(closes,150) if len(closes)>=150 else None
        s200 = sma(closes,200) if len(closes)>=200 else None
        rv   = rsi14(closes)

        # Delivery from sec_bhavdata_full (merged into all_df)
        dvals=[]
        if "DELIV_PER" in sdf.columns:
            dp=pd.to_numeric(sdf["DELIV_PER"],errors="coerce")
            dvals=[float(x) for x in dp if pd.notna(x) and 0<x<=100]
        nd   = len(dvals)
        d20  = cavg(dvals[-20:]) if nd>=1  else None
        d50  = cavg(dvals[-50:]) if nd>=50 else None
        dlat = dvals[-1] if dvals else None

        # Strategy check
        st=None
        if chk_s1(ltp,mcap,rv,l52,h52,h60,d20,d50,nd,vr,tv,s30):
            st="s1"
        elif chk_s2(ltp,mcap,rv,l52,h52,d20,d50,nd,vr,tv,s20,s50,s200):
            st="s2"

        if st is None:
            if mcap<=S1_MCAP_MAX: sk["s1"]+=1
            else: sk["s2"]+=1
            continue

        score = sc_s1(d20,d50,vr,rv,fp,tv) if st=="s1" else sc_s2(d20,d50,vr,rv,ltp,s20,s50,s200,tv)
        gr    = grade(score,st,d20,d50,vr,fp,nd)
        if gr=="C": sk["gc"]+=1; continue

        cf,sigs,lbl = conf(d20,d50,dlat,vr,rv,fp,score,gr,dvals[-20:],
                           ltp,s20,s30,s50,s150,s200,st)
        if st=="s1": s1n+=1
        else: s2n+=1

        results.append({
            "symbol":sym,"strategy":st,
            "strategyLabel":"Mid/Small Cap" if st=="s1" else "Large/Mega Cap",
            "ltp":ltp,"change":chg,
            "high52w":h52,"low52w":l52,"fromLow":fp,"fromHigh":fh,
            "volume":vol_td,"mcap":round(mcap),"tradedValueCr":tv,
            "rsi":rv,"sma20":s20,"sma30":s30,"sma50":s50,"sma150":s150,"sma200":s200,
            "volRatio":vr,
            "avgDelivery":d20,"avgDelivery50":d50,
            "deliveryTrend":round(d20-d50,2) if d20 and d50 else None,
            "deliveryToday":dlat,"deliveryDays":nd,
            "score":score,"grade":gr,"daysOfData":len(sdf),
            "confidence":cf,"confidenceLabel":lbl,"confidenceBreakdown":sigs,
        })
    except Exception as e:
        print(f"  ERR {sym}: {e}")

print(f"\nResults : {len(results)} (S1:{s1n} S2:{s2n})")
print(f"Skipped : rows={sk['rows']} close={sk['close']} mcap={sk['mcap']} s1={sk['s1']} s2={sk['s2']} gc={sk['gc']}")
results.sort(key=lambda x:x["confidence"] or 0,reverse=True)

payload={
    "stocks":results,"count":len(results),
    "s1Count":s1n,"s2Count":s2n,
    "fetchedAt":datetime.now().isoformat(),"autoRun":True,
    "daysOfData":len(bhav_frames),
    "dataAvailability":{
        "mcap":len(stocks_today)>0,
        "delivery":del_days>0,"deliveryDays":del_days,
        "sma200":len(bhav_frames)>=200,
    },
}
content=json.dumps(payload)
os.makedirs("results",exist_ok=True)
with open("results/matrix181_results.json","w") as f: f.write(content)
print(f"Saved : {os.path.getsize('results/matrix181_results.json')} bytes")
print(f"API   : {'OK' if push_github(content) else 'FAIL'}")
print("="*60)
print(f"DONE {len(results)} | S1:{s1n} S2:{s2n}")
print(f"A+:{sum(1 for r in results if r['grade']=='A+')} "
      f"A:{sum(1 for r in results if r['grade']=='A')} "
      f"B+:{sum(1 for r in results if r['grade']=='B+')} "
      f"B:{sum(1 for r in results if r['grade']=='B')}")
print("="*60)
