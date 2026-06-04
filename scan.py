"""
MATRIX 18.1 — NSE Dual Strategy Screener
Data: BhavCopy ZIP × 260 days + sec_bhavdata_full × 25 days
Delivery: RSVD1/VOLUME×100 from bhavcopy (all 260 days)
          DELIV_PER direct from sec_bhavdata_full (recent 25 days)
52W:      max(HIGH)/min(LOW) from 260 days bhavcopy
MCap:     EQUITY_L — Close × PaidUp_Cr / FaceValue
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
S = requests.Session(); S.headers.update(HDR)

PAT   = os.environ.get("PAT_TOKEN","")
GUSER = "gauravmsm"
GREPO = "matrix181-backend"
GPATH = "results/matrix181_results.json"

# ── Parameters ────────────────────────────────────────────────────────────────
# S1 — Mid/Small Cap 1500–30000 Cr
S1_MCAP_MIN  = 1500;  S1_MCAP_MAX  = 30000
S1_RSI_MIN   = 40.0;  S1_RSI_MAX   = 70.0
S1_VOL_RATIO = 1.20   # 20D/50D (soft — skip if vol50 unavailable)
S1_DEL_MIN   = 45.0   # 20D avg delivery % — active when 10+ days available
S1_DEL_GAP   = 3.0    # 20D > 50D + 3%      — active when 20+ days available
S1_TV_MIN    = 7.5    # Cr/day avg 20D (soft)
S1_LOW_MIN   = 1.10   # price >= 52W_low × 1.10
S1_LOW_MAX   = 1.40   # price <= 52W_low × 1.40
S1_H60       = 0.90   # price >= 60D_high_close × 0.90

# S2 — Large/Mega Cap > 30000 Cr
S2_MCAP_MIN  = 30000
S2_RSI_MIN   = 40.0;  S2_RSI_MAX   = 70.0
S2_VOL_RATIO = 1.20   # soft
S2_DEL_MIN   = 40.0   # active when 10+ days
S2_TV_MIN    = 25.0   # soft
S2_HIGH_MIN  = 0.65   # price >= 52W_high × 0.65 (within 35%)
S2_HIGH_MAX  = 0.95   # price <= 52W_high × 0.95 (not at top)

DAYS_BHAV = 260  # ~1 year of bhavcopy
DAYS_FULL =  60  # sec_bhavdata_full — try 60 days (NSE keeps ~25 but attempt more)

def push_github(text):
    if not PAT: return False
    url = f"https://api.github.com/repos/{GUSER}/{GREPO}/contents/{GPATH}"
    h   = {"Authorization":f"token {PAT}","Accept":"application/vnd.github.v3+json"}
    sha = None
    try:
        r = requests.get(url,headers=h,timeout=15)
        if r.status_code==200: sha = r.json().get("sha")
    except: pass
    body = {"message":f"scan {datetime.now():%Y-%m-%d %H:%M}",
            "content":base64.b64encode(text.encode()).decode(),"branch":"main"}
    if sha: body["sha"] = sha
    try:
        r = requests.put(url,headers=h,json=body,timeout=30)
        ok = r.status_code in (200,201)
        print(f"  GitHub {'OK' if ok else 'FAIL'} ({r.status_code})")
        return ok
    except Exception as e:
        print(f"  GitHub error: {e}"); return False

def trdates(n):
    out, d = [], datetime.now()
    while len(out)<n:
        d -= timedelta(days=1)
        if d.weekday()<5: out.append(d)
    return out

# ── Fetch BhavCopy ZIP ────────────────────────────────────────────────────────
def fetch_bhav(date):
    """
    Returns EQ-filtered DataFrame with unified columns:
    SYMBOL, OPEN, HIGH, LOW, CLOSE, VOLUME, VALUE_L, DELIV_QTY, DATE
    VALUE_L = traded value in lakhs (TOTTRDVAL / VALUE column)
    DELIV_QTY = deliverable quantity (RSVD1)
    DELIV_PER computed as DELIV_QTY/VOLUME*100 after loading
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
            # Filter EQ series
            if "SERIES" in df.columns:
                df = df[df["SERIES"].astype(str).str.strip()=="EQ"].copy()
            # Rename to unified schema
            rn = {}
            for new, cands in [
                ("SYMBOL",   ["TCKRSYMB","FININSTRMID"]),
                ("OPEN",     ["OPNPRIC","OPEN_PRICE"]),
                ("HIGH",     ["HGHPRIC","HIGH_PRICE"]),
                ("LOW",      ["LWPRIC","LOW_PRICE"]),
                ("CLOSE",    ["CLSPRIC","CLOSE_PRICE"]),
                ("VOLUME",   ["TOTTRDQTY","TTL_TRD_QNTY","TTLTRADGVOL"]),
                ("VALUE_L",  ["TOTTRDVAL","TTLTRFVAL","VALUE","TURNOVER_LACS","TTLNBOFTXSEXCTD"]),
                ("DELIV_QTY",["RSVD1","DELIV_QTY"]),
            ]:
                if new not in df.columns:
                    for c in cands:
                        if c in df.columns: rn[c]=new; break
            df.rename(columns=rn, inplace=True)
            if "SYMBOL" not in df.columns: continue
            # Numeric
            for col in ["OPEN","HIGH","LOW","CLOSE","VOLUME","VALUE_L","DELIV_QTY"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            # Compute DELIV_PER from DELIV_QTY/VOLUME
            if "DELIV_QTY" in df.columns and "VOLUME" in df.columns:
                mask = (df["VOLUME"]>0) & df["DELIV_QTY"].notna()
                df.loc[mask,"DELIV_PER"] = (df.loc[mask,"DELIV_QTY"]/df.loc[mask,"VOLUME"]*100).round(2)
                df.loc[~mask,"DELIV_PER"] = np.nan
            df["DATE"] = date.strftime("%Y-%m-%d")
            return df
        except: pass
    return None

# ── Fetch sec_bhavdata_full (recent ~25 days, has direct DELIV_PER) ───────────
def fetch_full(date):
    """
    sec_bhavdata_full_DDMMYYYY.csv
    Cols: SYMBOL, SERIES, CLOSE_PRICE, TTL_TRD_QNTY, TURNOVER_LACS,
          DELIV_QTY, DELIV_PER  ← direct, authoritative
    """
    url = f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{date.strftime('%d%m%Y')}.csv"
    try:
        r = S.get(url, timeout=20)
        if r.status_code!=200 or len(r.content)<5000: return None
        from io import StringIO
        for enc in ["utf-8","latin-1"]:
            try:
                df = pd.read_csv(StringIO(r.content.decode(enc)))
                df.columns = df.columns.str.strip().str.upper()
                if "SYMBOL" not in df.columns: continue
                if "SERIES" in df.columns:
                    df = df[df["SERIES"].astype(str).str.strip()=="EQ"].copy()
                rn = {}
                for new, cands in [
                    ("CLOSE",    ["CLOSE_PRICE"]),
                    ("HIGH",     ["HIGH_PRICE"]),
                    ("LOW",      ["LOW_PRICE"]),
                    ("VOLUME",   ["TTL_TRD_QNTY"]),
                    ("VALUE_L",  ["TURNOVER_LACS"]),
                ]:
                    if new not in df.columns:
                        for c in cands:
                            if c in df.columns: rn[c]=new; break
                df.rename(columns=rn, inplace=True)
                for col in ["CLOSE","HIGH","LOW","VOLUME","VALUE_L","DELIV_QTY","DELIV_PER"]:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                df["DATE"] = date.strftime("%Y-%m-%d")
                return df
            except: continue
    except: pass
    return None

def fetch_mcap(close_map):
    """MCap Cr = Close × PaidUp_Cr / FaceValue. Verified: TCS~811K, MRF~127K"""
    url = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
    try:
        r = S.get(url, timeout=20)
        if r.status_code!=200: return {}
        from io import StringIO
        for enc in ["utf-8","latin-1","cp1252"]:
            try:
                df = pd.read_csv(StringIO(r.content.decode(enc)))
                df.columns = df.columns.str.strip().str.upper()
                sc = next((c for c in df.columns if "SYMBOL" in c),None)
                pc = next((c for c in df.columns if "PAID"   in c),None)
                fc = next((c for c in df.columns if "FACE"   in c),None)
                if not(sc and pc and fc): continue
                # Remove commas from numeric columns (Indian number format: 2,31,700)
                df[pc] = pd.to_numeric(df[pc].astype(str).str.replace(",","").str.strip(), errors="coerce")
                df[fc] = pd.to_numeric(df[fc].astype(str).str.replace(",","").str.strip(), errors="coerce")
                # Debug: print column names and key rows
                print(f"  EQUITY_L cols: {list(df.columns)}")
                print(f"  Using: sym={sc} paid={pc} face={fc}")
                for chksym in ["TCS","MRF","RELIANCE"]:
                    row = df[df[sc].astype(str).str.strip()==chksym]
                    if len(row):
                        pu = row.iloc[0][pc]; fv = row.iloc[0][fc]
                        cl = lat_cl.get(chksym,"N/A")
                        print(f"  {chksym}: paid_up={pu} face={fv} close={cl}")
                out = {}
                for _,row in df.iterrows():
                    sym = str(row[sc]).strip().upper()
                    pu=row[pc]; fv=row[fc]; cl=close_map.get(sym)
                    if pd.notna(pu) and pu>0 and pd.notna(fv) and fv>0 and cl and cl>0:
                        # PAID UP VALUE = total paid-up capital in Lakhs of Rs
                        # Shares = (pu_lakhs * 1e5) / face_value_per_share
                        # MCap Cr = Close * Shares / 1e7
                        shares  = (pu * 1e5) / fv
                        mcap_cr = (cl * shares) / 1e7
                        if mcap_cr > 0:
                            out[sym] = round(mcap_cr, 2)
                # Verify
                chk = {s:round(out[s]) for s in ["TCS","MRF","RELIANCE","INFY","HDFCBANK"] if s in out}
                print(f"  MCap: {len(out)} | verify: {chk}")
                return out
            except: continue
    except Exception as e:
        print(f"  EQUITY_L: {e}")
    return {}

def sma(s, n):
    c = s.dropna()
    return round(float(c.tail(n).mean()),2) if len(c)>=n else None

def rsi14(s):
    c = s.dropna()
    if len(c)<16: return None
    d = c.diff()
    g = d.clip(lower=0).rolling(14).mean()
    l = (-d.clip(upper=0)).rolling(14).mean()
    v = (100-100/(1+g/l.replace(0,np.nan))).iloc[-1]
    return round(float(v),1) if pd.notna(v) else None

def cavg(vals):
    v=[x for x in vals if x is not None and pd.notna(x)]
    return round(sum(v)/len(v),2) if v else None


# ── S1 Filter ─────────────────────────────────────────────────────────────────
def chk_s1(ltp, mcap, rsi_v, l52, h52, h60,
           d20, d50, nd, vr, tv, s30):
    # 1. MCap
    if mcap is None or not (S1_MCAP_MIN<=mcap<=S1_MCAP_MAX): return False
    # 2. RSI 40-70
    if rsi_v is None or not (S1_RSI_MIN<=rsi_v<=S1_RSI_MAX): return False
    # 3. Close > 30 DMA (hard)
    if s30 is None or ltp<=s30: return False
    # 4. Vol ratio >= 1.20 (soft — only if 50D data available)
    if vr is not None and vr<S1_VOL_RATIO: return False
    # 5. Traded value >= 7.5 Cr (soft)
    if tv is not None and tv<S1_TV_MIN: return False
    # 6. Price 10-40% above 52W low (soft)
    if l52 and l52>0:
        m = ltp/l52
        if not (S1_LOW_MIN<=m<=S1_LOW_MAX): return False
    # 7. Close >= 60D high close × 0.90 (soft)
    if h60 and ltp<h60*S1_H60: return False
    # 8. Delivery >= 45% (active only when 10+ days of data)
    if nd>=10 and d20 is not None:
        if d20<S1_DEL_MIN: return False
    # 9. 20D delivery > 50D delivery + 3% (active only when 20+ days of data)
    if nd>=20 and d20 is not None and d50 is not None:
        if (d20-d50)<S1_DEL_GAP: return False
    return True

# ── S2 Filter ─────────────────────────────────────────────────────────────────
def chk_s2(ltp, mcap, rsi_v, l52, h52,
           d20, d50, nd, vr, tv, s20, s50, s200):
    # 1. MCap > 30000
    if mcap is None or mcap<S2_MCAP_MIN: return False
    # 2. RSI 40-70
    if rsi_v is None or not (S2_RSI_MIN<=rsi_v<=S2_RSI_MAX): return False
    # 3. Close > 20 DMA (hard)
    if s20 is None or ltp<=s20: return False
    # 4. Close > 50 DMA (hard)
    if s50 is None or ltp<=s50: return False
    # 5. 20 DMA > 50 DMA (hard — uptrend structure)
    if s20<=s50: return False
    # 6. Close > 200 DMA (soft)
    if s200 is not None and ltp<=s200: return False
    # 7. Vol ratio >= 1.20 (soft)
    if vr is not None and vr<S2_VOL_RATIO: return False
    # 8. Traded value >= 25 Cr (soft)
    if tv is not None and tv<S2_TV_MIN: return False
    # 9. Price within 5-35% of 52W high (soft)
    if h52 and h52>0:
        ratio = ltp/h52
        if not (S2_HIGH_MIN<=ratio<=S2_HIGH_MAX): return False
    # 10. Delivery >= 40% (active when 10+ days)
    if nd>=10 and d20 is not None:
        if d20<S2_DEL_MIN: return False
    return True

def sc_s1(d20,d50,vr,rv,fp,tv):
    s=0.0
    if d20 and d20>=45: s+=min((d20-45)/35*25,25)
    if d20 and d50 and (d20-d50)>3: s+=min((d20-d50-3)/12*20,20)
    if vr  and vr>=1.2: s+=min((vr-1.2)/0.8*20,20)
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
    if vr  and vr>=1.2: s+=min((vr-1.2)/0.8*20,20)
    if rv: s+=max(0,20-abs(rv-55)*0.8)
    ma=sum([bool(ltp and s20 and ltp>s20),
            bool(ltp and s50 and ltp>s50),
            bool(ltp and s200 and ltp>s200)])
    s+=ma*5
    if tv and tv>=25: s+=min((tv-25)/75*10,10)
    return min(int(round(s)),100)

def grade(sc,st,d20,d50,vr,fp,nd):
    gap  = (d20-d50) if d20 and d50 else 0
    dok  = nd>=10 and d20 is not None
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

# ── Step 1: Download BhavCopy ZIPs in parallel (260 days) ─────────────────────
all_dates = trdates(DAYS_BHAV)
print(f"\nPhase 1: {len(all_dates)} days BhavCopy ZIP (parallel)...")

bhav_frames = {}  # date_str → DataFrame

def _bhav(d): return d, fetch_bhav(d)

with ThreadPoolExecutor(max_workers=10) as ex:
    futs = {ex.submit(_bhav,d):d for d in all_dates}
    done=0
    for fut in as_completed(futs):
        dt, df = fut.result()
        if df is None: continue
        ds = dt.strftime("%Y-%m-%d")
        bhav_frames[ds] = df
        done+=1
        if done%50==0: print(f"  {done}/{len(all_dates)} loaded")

print(f"  Loaded: {len(bhav_frames)} days")

# ── Step 2: sec_bhavdata_full for recent 25 days (authoritative DELIV_PER) ────
full_dates = trdates(DAYS_FULL)
print(f"\nPhase 2: {len(full_dates)} days sec_bhavdata_full (delivery override)...")

full_frames = {}  # date_str → DataFrame
def _full(d): return d, fetch_full(d)

with ThreadPoolExecutor(max_workers=6) as ex:
    futs = {ex.submit(_full,d):d for d in full_dates}
    for fut in as_completed(futs):
        dt, df = fut.result()
        if df is None: continue
        ds = dt.strftime("%Y-%m-%d")
        full_frames[ds] = df

print(f"  Loaded: {len(full_frames)} days with direct DELIV_PER")

# ── Step 3: Verify delivery data ──────────────────────────────────────────────
print("\nDelivery verification:")
# Check RSVD1 delivery from bhavcopy
bhav_del_days = 0
bhav_rsvd_zero = 0
for ds, df in bhav_frames.items():
    if "DELIV_PER" in df.columns:
        valid = (df["DELIV_PER"]>0).sum()
        if valid>100: bhav_del_days+=1
        else: bhav_rsvd_zero+=1
    elif "DELIV_QTY" in df.columns:
        valid = (df["DELIV_QTY"]>0).sum()
        if valid>100: bhav_del_days+=1
        else: bhav_rsvd_zero+=1

# Check sec_bhavdata_full delivery
full_del_days = 0
for ds, df in full_frames.items():
    if "DELIV_PER" in df.columns:
        valid = (df["DELIV_PER"]>0).sum()
        if valid>100: full_del_days+=1

print(f"  Bhavcopy (RSVD1/VOL): {bhav_del_days} days with delivery | {bhav_rsvd_zero} days RSVD1=0/missing")
print(f"  sec_bhavdata_full   : {full_del_days} days with delivery")

# Sample delivery check
if bhav_frames:
    sample_ds = sorted(bhav_frames.keys())[-1]
    sdf = bhav_frames[sample_ds]
    print(f"  Sample bhavcopy cols ({sample_ds}): {list(sdf.columns)}")
    if "DELIV_PER" in sdf.columns:
        sample = sdf[sdf["DELIV_PER"]>0][["SYMBOL","CLOSE","VOLUME","DELIV_QTY","DELIV_PER"]].head(3)
        print(f"  Sample delivery rows:\n{sample.to_string(index=False)}")
    else:
        print(f"  NO DELIV_PER in bhavcopy — RSVD1 columns: {[c for c in sdf.columns if 'RSVD' in c]}")

if full_frames:
    sample_ds2 = sorted(full_frames.keys())[-1]
    sdf2 = full_frames[sample_ds2]
    if "DELIV_PER" in sdf2.columns:
        s2 = sdf2[sdf2["DELIV_PER"]>0][["SYMBOL","CLOSE","DELIV_PER"]].head(3)
        print(f"  sec_bhavdata_full sample:\n{s2.to_string(index=False)}")

# ── Step 4: Merge — use full_frames delivery to override bhavcopy where available
# For each date: if full_frames has it, use its DELIV_PER; else use bhavcopy DELIV_PER
merged = {}  # date_str → DataFrame with best available DELIV_PER

for ds in bhav_frames:
    df = bhav_frames[ds].copy()
    if ds in full_frames:
        # Override DELIV_PER from sec_bhavdata_full (more authoritative)
        full_del = full_frames[ds][["SYMBOL","DELIV_PER"]].copy()
        full_del["SYMBOL"] = full_del["SYMBOL"].astype(str).str.strip().str.upper()
        full_del = full_del.rename(columns={"DELIV_PER":"DELIV_PER_FULL"})
        df["SYMBOL"] = df["SYMBOL"].astype(str).str.strip().str.upper()
        df = df.merge(full_del, on="SYMBOL", how="left")
        # Use full DELIV_PER where available, else keep bhavcopy computed one
        if "DELIV_PER_FULL" in df.columns:
            mask = df["DELIV_PER_FULL"].notna() & (df["DELIV_PER_FULL"]>0)
            df.loc[mask,"DELIV_PER"] = df.loc[mask,"DELIV_PER_FULL"]
            df.drop(columns=["DELIV_PER_FULL"], inplace=True)
    merged[ds] = df

# Also add any full_frames dates not in bhav_frames
for ds in full_frames:
    if ds not in merged:
        df = full_frames[ds].copy()
        df["SYMBOL"] = df["SYMBOL"].astype(str).str.strip().str.upper()
        merged[ds] = df

del_days_total = sum(1 for ds,df in merged.items()
                     if "DELIV_PER" in df.columns and (df["DELIV_PER"]>0).sum()>100)
print(f"\nMerged: {len(merged)} days | delivery in {del_days_total} days")

if len(merged)<20:
    out={"stocks":[],"count":0,"fetchedAt":datetime.now().isoformat(),"autoRun":True}
    push_github(json.dumps(out)); exit(0)

# ── Step 5: Build combined DataFrame ──────────────────────────────────────────
all_df = pd.concat(list(merged.values()), ignore_index=True)
for col in ["OPEN","HIGH","LOW","CLOSE","VOLUME","VALUE_L","DELIV_QTY","DELIV_PER"]:
    if col in all_df.columns:
        all_df[col] = pd.to_numeric(all_df[col], errors="coerce")
all_df["DATE"] = pd.to_datetime(all_df["DATE"], errors="coerce")
all_df["SYMBOL"] = all_df["SYMBOL"].astype(str).str.strip().str.upper()
all_df.sort_values(["SYMBOL","DATE"], inplace=True)
all_df.drop_duplicates(subset=["SYMBOL","DATE"], keep="last", inplace=True)

# ── Step 6: Latest closes for MCap ────────────────────────────────────────────
lat_ds = sorted(merged.keys())[-1]
lat_df = merged[lat_ds]
lat_cl = {}
if "CLOSE" in lat_df.columns and "SYMBOL" in lat_df.columns:
    for _,row in lat_df.dropna(subset=["CLOSE"]).iterrows():
        sym = str(row["SYMBOL"]).strip().upper()
        if row["CLOSE"]>0: lat_cl[sym]=float(row["CLOSE"])
print(f"\nLatest closes: {len(lat_cl)} from {lat_ds}")

# ── Step 7: MCap ──────────────────────────────────────────────────────────────
print("Loading MCap...")
mcap_map   = fetch_mcap(lat_cl)
mcap_avail = len(mcap_map)>0

symbols = all_df["SYMBOL"].dropna().unique()
print(f"\n{'='*50}")
print(f"Total EQ  : {len(symbols)}")
print(f"Days      : {len(merged)}")
print(f"Delivery  : {del_days_total} days")
print(f"MCap      : {len(mcap_map)}")

# ── Step 8: Screening loop ────────────────────────────────────────────────────
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
        ltp = round(float(closes.iloc[-1]),2)
        if ltp<=0: sk["close"]+=1; continue

        chg = round((ltp-float(closes.iloc[-2]))/float(closes.iloc[-2])*100,2) if len(closes)>=2 else 0.0

        # 52W High/Low from all available HIGH/LOW data
        h52 = round(float(sdf["HIGH"].dropna().max()),2) if "HIGH" in sdf.columns else ltp
        l52 = round(float(sdf["LOW"].dropna().min()),2)  if "LOW"  in sdf.columns else ltp
        fp  = round((ltp-l52)/l52*100,2) if l52>0 else None
        fh  = round((h52-ltp)/h52*100,2) if h52>0 else None
        h60 = round(float(closes.tail(60).max()),2)

        # MCap
        mcap = mcap_map.get(sym)
        if mcap_avail and mcap is None: sk["mcap"]+=1; continue

        # Volume
        vols  = sdf["VOLUME"].dropna() if "VOLUME" in sdf.columns else pd.Series(dtype=float)
        vl_td = int(vols.iloc[-1]) if len(vols)>0 else 0
        v20   = float(vols.tail(20).mean()) if len(vols)>=20 else None
        v50   = float(vols.tail(50).mean()) if len(vols)>=50 else None
        vr    = round(v20/v50,3) if v20 and v50 and v50>0 else None

        # Traded value
        # TOTTRDVAL / TTLTRFVAL (bhavcopy) = in Rupees → /1e7 = Cr
        # TURNOVER_LACS (sec_bhavdata_full)  = in Lakhs  → /100  = Cr
        tv = None
        if "VALUE_L" in sdf.columns:
            tvs = pd.to_numeric(sdf["VALUE_L"], errors="coerce").dropna()
            if len(tvs) >= 5:
                raw = float(tvs.tail(20).mean())
                # Detect unit: if median > 1e6 it's in rupees; if < 1e6 it's in lakhs
                if raw > 1e6:
                    tv = round(raw / 1e7, 2)   # rupees → Cr
                else:
                    tv = round(raw / 100, 2)   # lakhs  → Cr

        # SMAs
        s20  = sma(closes,20)
        s30  = sma(closes,30)  if len(closes)>=30  else None
        s50  = sma(closes,50)  if len(closes)>=50  else None
        s150 = sma(closes,150) if len(closes)>=150 else None
        s200 = sma(closes,200) if len(closes)>=200 else None
        rv   = rsi14(closes)

        # Delivery
        dvals=[]
        if "DELIV_PER" in sdf.columns:
            dp = pd.to_numeric(sdf["DELIV_PER"],errors="coerce")
            dvals=[float(x) for x in dp if pd.notna(x) and 0<x<=100]
        nd   = len(dvals)
        d20  = cavg(dvals[-20:]) if nd>=1  else None
        d50  = cavg(dvals[-50:]) if nd>=50 else None
        dlat = dvals[-1] if dvals else None

        # Strategy
        st=None
        if chk_s1(ltp,mcap,rv,l52,h52,h60,d20,d50,nd,vr,tv,s30):
            st="s1"
        elif chk_s2(ltp,mcap,rv,l52,h52,d20,d50,nd,vr,tv,s20,s50,s200):
            st="s2"

        if st is None:
            if mcap and mcap<=S1_MCAP_MAX: sk["s1"]+=1
            else: sk["s2"]+=1
            continue

        sc  = sc_s1(d20,d50,vr,rv,fp,tv) if st=="s1" else sc_s2(d20,d50,vr,rv,ltp,s20,s50,s200,tv)
        gr  = grade(sc,st,d20,d50,vr,fp,nd)
        if gr=="C": sk["gc"]+=1; continue

        cf,sigs,lbl = conf(d20,d50,dlat,vr,rv,fp,sc,gr,dvals[-20:],ltp,s20,s30,s50,s150,s200,st)
        if st=="s1": s1n+=1
        else: s2n+=1

        results.append({
            "symbol":sym,"strategy":st,
            "strategyLabel":"Mid/Small Cap" if st=="s1" else "Large/Mega Cap",
            "ltp":ltp,"change":chg,
            "high52w":h52,"low52w":l52,"fromLow":fp,"fromHigh":fh,
            "volume":vl_td,"mcap":round(mcap) if mcap else None,
            "tradedValueCr":tv,"rsi":rv,
            "sma20":s20,"sma30":s30,"sma50":s50,"sma150":s150,"sma200":s200,
            "volRatio":vr,
            "avgDelivery":d20,"avgDelivery50":d50,
            "deliveryTrend":round(d20-d50,2) if d20 and d50 else None,
            "deliveryToday":dlat,"deliveryDays":nd,
            "score":sc,"grade":gr,"daysOfData":len(sdf),
            "confidence":cf,"confidenceLabel":lbl,"confidenceBreakdown":sigs,
        })
    except Exception as e:
        print(f"  ERR {sym}: {e}")

print(f"\nResults : {len(results)} (S1:{s1n} S2:{s2n})")
print(f"Skipped : rows={sk['rows']} close={sk['close']} mcap={sk['mcap']} s1={sk['s1']} s2={sk['s2']} gc={sk['gc']}")
results.sort(key=lambda x:x["confidence"] or 0,reverse=True)

payload = {
    "stocks":results,"count":len(results),
    "s1Count":s1n,"s2Count":s2n,
    "fetchedAt":datetime.now().isoformat(),"autoRun":True,
    "daysOfData":len(merged),
    "dataAvailability":{
        "mcap":mcap_avail,
        "delivery":del_days_total>0,
        "deliveryDays":del_days_total,
        "sma200":len(merged)>=200,
    },
}
content = json.dumps(payload)
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
