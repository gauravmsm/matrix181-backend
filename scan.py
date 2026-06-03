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
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Referer": "https://www.nseindia.com/",
}
SESSION = requests.Session()
SESSION.headers.update(NSE_HEADERS)

GITHUB_TOKEN = os.environ.get("PAT_TOKEN", "")
GITHUB_USER  = "gauravmsm"
GITHUB_REPO  = "matrix181-backend"
RESULTS_PATH = "results/matrix181_results.json"

# ── STRATEGY 1: MID/SMALL CAP TURNAROUNDS (1500–30000 Cr) ────────────────────
S1_MCAP_MIN      = 1500
S1_MCAP_MAX      = 30000
S1_DELIVERY_MIN  = 45.0
S1_DELIVERY_GAP  = 5.0
S1_VOL_RATIO     = 1.25
S1_RSI_MIN       = 45.0
S1_RSI_MAX       = 60.0
S1_FROM_LOW_MIN  = 1.10   # Close >= 52W Low * 1.10
S1_FROM_LOW_MAX  = 1.35   # Close <= 52W Low * 1.35
S1_TRADED_VALUE  = 10.0   # Cr

# ── STRATEGY 2: MEGA/LARGE CAP BLUE-CHIP (>30000 Cr) ─────────────────────────
S2_MCAP_MIN      = 30000
S2_DELIVERY_MIN  = 40.0
S2_DELIVERY_GAP  = 3.0
S2_VOL_RATIO     = 1.20
S2_RSI_MIN       = 50.0
S2_RSI_MAX       = 65.0
S2_TRADED_VALUE  = 25.0   # Cr
S2_FROM_HIGH_MAX = 0.85   # Not within 15% of 52W high

# ── DATA FETCH SETTINGS ───────────────────────────────────────────────────────
DAYS_DETAIL = 60    # Full data: bhav + delivery + value (for S1 + S2 filters)
DAYS_SMA200 = 210   # Close-only data for SMA200 calculation

def save_via_github_api(content_str):
    if not GITHUB_TOKEN:
        print("PAT_TOKEN not set")
        return False
    api_url = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{RESULTS_PATH}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    sha = None
    try:
        r = requests.get(api_url, headers=headers, timeout=15)
        if r.status_code == 200:
            sha = r.json().get("sha")
            print(f"  SHA: {sha[:8]}")
    except Exception as e:
        print(f"  SHA error: {e}")
    payload = {
        "message": f"Auto scan {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "content": base64.b64encode(content_str.encode()).decode(),
        "branch":  "main",
    }
    if sha:
        payload["sha"] = sha
    try:
        r = requests.put(api_url, headers=headers, json=payload, timeout=30)
        if r.status_code in (200, 201):
            print(f"  GitHub API OK — {r.status_code}")
            return True
        print(f"  GitHub API FAILED — {r.status_code}")
        return False
    except Exception as e:
        print(f"  GitHub API error: {e}")
        return False

def get_session():
    try:
        SESSION.get("https://www.nseindia.com", timeout=10)
        print("NSE session ready")
    except:
        pass

def fetch_mcap_map():
    for url in [
        "https://nsearchives.nseindia.com/content/equities/MCAP.csv",
        "https://www.nseindia.com/content/equities/MCAP.csv",
    ]:
        try:
            r = SESSION.get(url, timeout=20)
            if r.status_code != 200:
                continue
            from io import StringIO
            for enc in ["utf-8", "latin-1", "cp1252"]:
                try:
                    df = pd.read_csv(StringIO(r.content.decode(enc)))
                    df.columns = df.columns.str.strip().str.upper()
                    sym_col  = next((c for c in df.columns if "SYMBOL" in c), None)
                    mcap_col = next((c for c in df.columns if any(k in c for k in
                                    ["MARKET","MCAP","CAP","CAPITALISATION","CAPITALIZATION"])), None)
                    if not mcap_col:
                        for c in reversed(df.columns):
                            try:
                                test = pd.to_numeric(df[c], errors="coerce").dropna()
                                if len(test) > 100 and test.median() > 100:
                                    mcap_col = c; break
                            except: pass
                    if sym_col and mcap_col:
                        df[mcap_col] = pd.to_numeric(df[mcap_col], errors="coerce")
                        result = {}
                        for _, row in df.iterrows():
                            sym = str(row[sym_col]).strip().upper()
                            cap = row[mcap_col]
                            if pd.notna(cap) and cap > 0:
                                result[sym] = round(float(cap), 2)
                        print(f"MCAP: {len(result)} symbols")
                        return result
                except: continue
        except: continue
    return {}

def normalise_bhav(df):
    df = df.copy()
    df.columns = df.columns.str.strip().str.upper()
    rm = {}
    if "SYMBOL"  not in df.columns:
        for c in ["TCKRSYMB","FININSTRMID","FININSTRMNNM"]:
            if c in df.columns: rm[c]="SYMBOL"; break
    if "SERIES"  not in df.columns:
        for c in ["SCTYSRS"]:
            if c in df.columns: rm[c]="SERIES"; break
    if "CLOSE"   not in df.columns:
        for c in ["CLSPRIC"]:
            if c in df.columns: rm[c]="CLOSE"; break
    if "HIGH"    not in df.columns:
        for c in ["HGHPRIC"]:
            if c in df.columns: rm[c]="HIGH"; break
    if "LOW"     not in df.columns:
        for c in ["LWPRIC"]:
            if c in df.columns: rm[c]="LOW"; break
    if "VOLUME"  not in df.columns:
        for c in ["TTLTRADGVOL","TOTTRDQTY","TTL_TRD_QNTY"]:
            if c in df.columns: rm[c]="VOLUME"; break
    if "VALUE"   not in df.columns:
        for c in ["TTLBOFTXSEXCTD","TOTTRDVAL","TTLTRDDVAL"]:
            if c in df.columns: rm[c]="VALUE"; break
    if "DELIV_QTY"      not in df.columns and "RSVD1" in df.columns:
        rm["RSVD1"] = "DELIV_QTY"
    if "DELIV_PCT_BHAV" not in df.columns and "RSVD2" in df.columns:
        rm["RSVD2"] = "DELIV_PCT_BHAV"
    df.rename(columns=rm, inplace=True)
    return df

def try_extract_delivery_from_bhav(bhav_df):
    result = {}
    if "SYMBOL" not in bhav_df.columns:
        return result
    if "DELIV_PCT_BHAV" in bhav_df.columns:
        col   = pd.to_numeric(bhav_df["DELIV_PCT_BHAV"], errors="coerce")
        valid = col[(col > 0) & (col <= 100)]
        if len(valid) > 10:
            for idx in valid.index:
                sym = str(bhav_df.at[idx,"SYMBOL"]).strip().upper()
                result[sym] = round(float(valid[idx]), 2)
            print(f"  Delivery RSVD2: {len(result)}")
            return result
    if "DELIV_QTY" in bhav_df.columns and "VOLUME" in bhav_df.columns:
        dq   = pd.to_numeric(bhav_df["DELIV_QTY"], errors="coerce")
        vol  = pd.to_numeric(bhav_df["VOLUME"],     errors="coerce")
        mask = (dq > 0) & (vol > 0)
        if mask.sum() > 10:
            pct = (dq / vol * 100).round(2)
            for idx in bhav_df[mask].index:
                sym = str(bhav_df.at[idx,"SYMBOL"]).strip().upper()
                p   = pct[idx]
                if 0 < p <= 100:
                    result[sym] = float(p)
            if result:
                print(f"  Delivery RSVD1/VOL: {len(result)}")
                return result
    return result

def fetch_delivery_dat(date):
    d1 = date.strftime("%d%m%Y")
    d2 = date.strftime("%Y%m%d")
    for url in [
        f"https://nsearchives.nseindia.com/archives/equities/deliveries/MTO_{d1}.DAT",
        f"https://www.nseindia.com/archives/equities/deliveries/MTO_{d1}.DAT",
        f"https://nsearchives.nseindia.com/content/cm/MTO_{d2}.csv.zip",
        f"https://nsearchives.nseindia.com/content/cm/MTO_{d1}.csv.zip",
    ]:
        try:
            r = SESSION.get(url, timeout=15)
            if r.status_code != 200: continue
            if url.endswith(".zip"):
                z  = zipfile.ZipFile(io.BytesIO(r.content))
                df = pd.read_csv(z.open(z.namelist()[0]))
                df.columns = df.columns.str.strip().str.upper()
                sym_c = next((c for c in df.columns if "SYMBOL" in c), None)
                trd_c = next((c for c in df.columns if "TRAD" in c and "QTY" in c), None)
                del_c = next((c for c in df.columns if "DELIV" in c and "QTY" in c), None)
                ser_c = next((c for c in df.columns if "SERIES" in c or "SRS" in c), None)
                if sym_c and trd_c and del_c:
                    df[trd_c] = pd.to_numeric(df[trd_c], errors="coerce")
                    df[del_c] = pd.to_numeric(df[del_c], errors="coerce")
                    if ser_c: df = df[df[ser_c]=="EQ"]
                    df["DELIVERY_PCT"] = (df[del_c]/df[trd_c]*100).round(2)
                    out = df[[sym_c,"DELIVERY_PCT"]].copy()
                    out.columns = ["SYMBOL","DELIVERY_PCT"]
                    return out
            else:
                rows = []
                for line in r.text.strip().split("\n"):
                    if line.startswith("#") or not line.strip(): continue
                    p = [x.strip() for x in line.split(",")]
                    if len(p) >= 6:
                        rows.append({"SYMBOL":p[2],"SERIES":p[3],
                                     "TRADED_QTY":safe_float(p[4]),
                                     "DELIVERABLE_QTY":safe_float(p[5])})
                if rows:
                    df = pd.DataFrame(rows)
                    df = df[df["SERIES"]=="EQ"]
                    df["DELIVERY_PCT"] = (df["DELIVERABLE_QTY"]/df["TRADED_QTY"]*100).round(2)
                    return df[["SYMBOL","DELIVERY_PCT"]]
        except: continue
    return None

def fetch_bhavcopy(date):
    d = date.strftime("%d%b%Y").upper()
    for u in [
        f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{date.strftime('%Y%m%d')}_F_0000.csv.zip",
        f"https://www.nseindia.com/content/historical/EQUITIES/{date.year}/{date.strftime('%b').upper()}/cm{d}bhav.csv.zip",
    ]:
        try:
            r = SESSION.get(u, timeout=20)
            if r.status_code == 200:
                z  = zipfile.ZipFile(io.BytesIO(r.content))
                df = pd.read_csv(z.open(z.namelist()[0]))
                df = normalise_bhav(df)
                if "SYMBOL" in df.columns:
                    return df
        except: pass
    return None

def fetch_bhavcopy_close_only(date):
    """Lightweight fetch — only SYMBOL + CLOSE + SERIES for SMA200."""
    df = fetch_bhavcopy(date)
    if df is not None and "SYMBOL" in df.columns and "CLOSE" in df.columns:
        cols = ["SYMBOL","CLOSE"]
        if "SERIES" in df.columns: cols.append("SERIES")
        return df[cols].copy()
    return None

def fetch_detail(date):
    """Full fetch: bhav + delivery."""
    ds    = date.strftime("%Y-%m-%d")
    bhav  = fetch_bhavcopy(date)
    deliv = fetch_delivery_dat(date)
    if (deliv is None or len(deliv)==0) and bhav is not None:
        d = try_extract_delivery_from_bhav(bhav)
        if d:
            deliv = pd.DataFrame(list(d.items()), columns=["SYMBOL","DELIVERY_PCT"])
    return ds, bhav, deliv

def fetch_close_only(date):
    """Light fetch: close only for SMA200."""
    ds = date.strftime("%Y-%m-%d")
    return ds, fetch_bhavcopy_close_only(date)

def safe_float(x):
    try: return float(str(x).replace(",",""))
    except: return 0.0

def get_trading_dates(n):
    dates, d = [], datetime.now()
    while len(dates) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            dates.append(d)
    return dates

def calc_rsi(closes, period=14):
    if len(closes) < period+2: return None
    delta = closes.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    rsi   = 100 - (100/(1+rs))
    val   = rsi.iloc[-1]
    return round(float(val),1) if pd.notna(val) else None

def calc_sma(series, period):
    clean = series.dropna()
    if len(clean) < period: return None
    return round(float(clean.tail(period).mean()),2)

def calc_avg(vals):
    vals = [v for v in vals if v is not None and not np.isnan(v)]
    return round(sum(vals)/len(vals),2) if vals else None

def check_strategy1(ltp, mcap, rsi, from_low_mult,
                    avg_d20, avg_d50, vol_ratio, avg_tv,
                    sma30, del_avail, val_avail):
    if mcap is None or not (S1_MCAP_MIN <= mcap <= S1_MCAP_MAX): return False
    if val_avail and (avg_tv is None or avg_tv < S1_TRADED_VALUE): return False
    if rsi is None or not (S1_RSI_MIN <= rsi <= S1_RSI_MAX): return False
    if from_low_mult is None or not (S1_FROM_LOW_MIN <= from_low_mult <= S1_FROM_LOW_MAX): return False
    if sma30 is None or ltp <= sma30: return False
    if vol_ratio is None or vol_ratio < S1_VOL_RATIO: return False
    if del_avail:
        if avg_d20 is not None and avg_d20 < S1_DELIVERY_MIN: return False
        if avg_d20 is not None and avg_d50 is not None and (avg_d20-avg_d50) < S1_DELIVERY_GAP: return False
    return True

def check_strategy2(ltp, mcap, rsi, high_52w,
                    avg_d20, avg_d50, vol_ratio, avg_tv,
                    sma20, sma50, sma200, del_avail, val_avail):
    if mcap is None or mcap < S2_MCAP_MIN: return False
    if val_avail and (avg_tv is None or avg_tv < S2_TRADED_VALUE): return False
    if rsi is None or not (S2_RSI_MIN <= rsi <= S2_RSI_MAX): return False
    if high_52w and ltp > high_52w * S2_FROM_HIGH_MAX: return False
    if sma20  is None or ltp <= sma20:  return False
    if sma50  is None or ltp <= sma50:  return False
    if sma200 is None or ltp <= sma200: return False
    if vol_ratio is None or vol_ratio < S2_VOL_RATIO: return False
    if del_avail:
        if avg_d20 is not None and avg_d20 < S2_DELIVERY_MIN: return False
        if avg_d20 is not None and avg_d50 is not None and (avg_d20-avg_d50) < S2_DELIVERY_GAP: return False
    return True

def calc_score_s1(avg_d20, avg_d50, vol_ratio, rsi, from_low_pct, avg_tv):
    s = 0.0
    if avg_d20 and avg_d20>=45:  s += min((avg_d20-45)/35*20, 20)
    if avg_d20 and avg_d50 and (avg_d20-avg_d50)>5:
        s += min((avg_d20-avg_d50-5)/15*20, 20)
    if vol_ratio and vol_ratio>=1.25: s += min((vol_ratio-1.25)/0.75*20, 20)
    if rsi: s += max(0, 20-abs(rsi-52.5)*1.2)
    if from_low_pct:
        if 15<=from_low_pct<=25:   s+=15
        elif 10<=from_low_pct<15:  s+=10
        elif 25<from_low_pct<=35:  s+=8
    if avg_tv and avg_tv>=10: s += min((avg_tv-10)/40*10, 10)
    return min(int(round(s)), 100)

def calc_score_s2(avg_d20, avg_d50, vol_ratio, rsi,
                  ltp, sma20, sma50, sma200, avg_tv):
    s = 0.0
    if avg_d20 and avg_d20>=40:  s += min((avg_d20-40)/40*20, 20)
    if avg_d20 and avg_d50 and (avg_d20-avg_d50)>3:
        s += min((avg_d20-avg_d50-3)/12*20, 20)
    if vol_ratio and vol_ratio>=1.20: s += min((vol_ratio-1.20)/0.80*20, 20)
    if rsi: s += max(0, 20-abs(rsi-57.5)*1.0)
    ma = sum([1 if ltp and sma20  and ltp>sma20  else 0,
              1 if ltp and sma50  and ltp>sma50  else 0,
              1 if ltp and sma200 and ltp>sma200 else 0])
    s += ma * 5
    if avg_tv and avg_tv>=25: s += min((avg_tv-25)/75*10, 10)
    return min(int(round(s)), 100)

def grade_setup(score, strategy, avg_d20, avg_d50, vol_ratio, from_low_pct, del_avail):
    gap = (avg_d20-avg_d50) if avg_d20 and avg_d50 else 0
    if strategy=="s1":
        if del_avail:
            if score>=82 and avg_d20 and avg_d20>=60 and gap>=10 and vol_ratio>=1.5  and from_low_pct and 15<=from_low_pct<=25: return "A+"
            if score>=68 and avg_d20 and avg_d20>=52 and gap>=5  and vol_ratio>=1.35 and from_low_pct and from_low_pct<=30:     return "A"
        else:
            if score>=82 and vol_ratio>=1.5  and from_low_pct and 15<=from_low_pct<=25: return "A+"
            if score>=68 and vol_ratio>=1.35 and from_low_pct and from_low_pct<=30:      return "A"
        if score>=52: return "B+"
        if score>=38: return "B"
    else:
        if del_avail:
            if score>=82 and avg_d20 and avg_d20>=55 and gap>=8  and vol_ratio>=1.5:  return "A+"
            if score>=68 and avg_d20 and avg_d20>=48 and gap>=3  and vol_ratio>=1.35: return "A"
        else:
            if score>=82 and vol_ratio>=1.5:  return "A+"
            if score>=68 and vol_ratio>=1.35: return "A"
        if score>=52: return "B+"
        if score>=38: return "B"
    return "C"

def calc_confidence(avg_d20, avg_d50, del_today, vol_ratio,
                    rsi, from_low_pct, score, grade, del_vals,
                    ltp, sma20, sma30, sma50, sma200, strategy):
    sig = {}
    dq = 0
    if del_vals and len(del_vals)>=3:
        thresh = 50 if strategy=="s1" else 45
        dq = min(sum(1 for d in del_vals if d>=thresh)/len(del_vals)*20, 20)
        if del_today and avg_d20 and del_today>avg_d20: dq=min(dq+3,20)
    elif avg_d20 and avg_d20>=45: dq=10
    sig["delivery_quality"] = round(dq,1)

    dt = 0
    if avg_d20 and avg_d50 and avg_d20>avg_d50:
        dt = min((avg_d20-avg_d50)/10*15, 15)
    sig["delivery_trend"] = round(dt,1)

    vc = 0
    if vol_ratio:
        if vol_ratio>=2.0:    vc=20
        elif vol_ratio>=1.75: vc=16
        elif vol_ratio>=1.5:  vc=12
        elif vol_ratio>=1.25: vc=7
        elif vol_ratio>=1.20: vc=5
    sig["volume_expansion"] = round(vc,1)

    if strategy=="s1":
        rq = 15 if rsi and 50<=rsi<=57 else 12 if rsi and 47<=rsi<=60 else 8 if rsi and 45<=rsi<=60 else 0
    else:
        rq = 15 if rsi and 55<=rsi<=62 else 12 if rsi and 52<=rsi<=65 else 8 if rsi and 50<=rsi<=65 else 0
    sig["rsi_zone"] = round(rq,1)

    rr = 0
    if strategy=="s1" and from_low_pct:
        if 15<=from_low_pct<=25:   rr=15
        elif 10<=from_low_pct<15:  rr=11
        elif 25<from_low_pct<=30:  rr=9
        elif 30<from_low_pct<=35:  rr=6
    elif strategy=="s2":
        ma = sum([1 if ltp and sma20  and ltp>sma20  else 0,
                  1 if ltp and sma50  and ltp>sma50  else 0,
                  1 if ltp and sma200 and ltp>sma200 else 0])
        rr = ma * 5
    sig["price_position"] = round(rr,1)

    ta = 0
    if strategy=="s1":
        if ltp and sma30 and ltp>sma30: ta=10
    else:
        if ltp and sma50  and ltp>sma50:  ta+=5
        if ltp and sma200 and ltp>sma200: ta+=5
    sig["trend_alignment"] = round(ta,1)

    gb = {"A+":10,"A":8,"B+":5,"B":2}.get(grade,0)
    sig["grade_bonus"] = gb

    total = min(int(round(dq+dt+vc+rq+rr+ta+gb)), 100)
    label = ("VERY HIGH" if total>=85 else "HIGH" if total>=70 else
             "MODERATE"  if total>=55 else "LOW"  if total>=40 else "VERY LOW")
    return total, sig, label


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

print("="*60)
print("MATRIX 18.1 — DUAL STRATEGY SCAN")
print(f"Started  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print(f"PAT_TOKEN: {'SET' if GITHUB_TOKEN else 'NOT SET'}")
print(f"Detail days : {DAYS_DETAIL} | SMA200 days: {DAYS_SMA200}")
print("="*60)

get_session()
print("\nLoading MCap...")
mcap_map = fetch_mcap_map()

# ── PHASE 1: Fetch 60 days of full data ───────────────────────────────────────
detail_dates = get_trading_dates(DAYS_DETAIL)
print(f"\nPhase 1: Fetching {len(detail_dates)} detail days (bhav+delivery+value)...")

bhavcopy_list = []
delivery_map  = {}
value_map     = {}
completed     = 0
cols_printed  = False

with ThreadPoolExecutor(max_workers=5) as ex:
    futures = {ex.submit(fetch_detail, d): d for d in detail_dates}
    for future in as_completed(futures):
        try:
            ds, bhav, deliv = future.result()
            if bhav is not None:
                bhav["DATE"] = ds
                bhavcopy_list.append(bhav)
                if not cols_printed:
                    print(f"  Cols: {list(bhav.columns)}")
                    cols_printed = True
                if "VALUE" in bhav.columns and "SYMBOL" in bhav.columns:
                    bhav_v = bhav[["SYMBOL","VALUE"]].copy()
                    bhav_v["VALUE"] = pd.to_numeric(bhav_v["VALUE"], errors="coerce")
                    vd = {}
                    for _, row in bhav_v.iterrows():
                        sym = str(row["SYMBOL"]).strip().upper()
                        val = row["VALUE"]
                        if pd.notna(val) and val>0: vd[sym]=float(val)/100
                    if vd: value_map[ds] = vd
            if deliv is not None and len(deliv)>0:
                delivery_map[ds] = dict(zip(deliv["SYMBOL"], deliv["DELIVERY_PCT"]))
            completed += 1
            if completed % 10 == 0:
                print(f"  {completed}/{len(detail_dates)} bhav:{len(bhavcopy_list)} deliv:{len(delivery_map)} val:{len(value_map)}")
        except Exception as e:
            completed += 1

# ── PHASE 2: Fetch 150 more days of close-only for SMA200 ─────────────────────
# We already have 60 days — need 150 more to reach 210 total
extra_dates = get_trading_dates(DAYS_SMA200)[DAYS_DETAIL:]  # days 61-210
print(f"\nPhase 2: Fetching {len(extra_dates)} close-only days for SMA200...")

extra_close_list = []
completed2 = 0

with ThreadPoolExecutor(max_workers=8) as ex:
    futures2 = {ex.submit(fetch_close_only, d): d for d in extra_dates}
    for future in as_completed(futures2):
        try:
            ds, df_close = future.result()
            if df_close is not None:
                df_close["DATE"] = ds
                extra_close_list.append(df_close)
            completed2 += 1
            if completed2 % 30 == 0:
                print(f"  {completed2}/{len(extra_dates)} close-only days loaded")
        except:
            completed2 += 1

print(f"  Close-only days loaded: {len(extra_close_list)}")

actual_days = len(bhavcopy_list)
del_avail   = len(delivery_map) > 0
val_avail   = len(value_map) > 0

print(f"\nBhavcopy : {actual_days}")
print(f"Delivery : {len(delivery_map)} {'✓' if del_avail else '✗ skipped'}")
print(f"Value    : {len(value_map)} {'✓' if val_avail else '✗ skipped'}")
print(f"MCap     : {len(mcap_map)} {'✓' if mcap_map else '✗ skipped'}")
print(f"SMA200   : {len(extra_close_list)+actual_days} total close days")
if del_avail:
    s = list(delivery_map.keys())[0]
    print(f"  Sample delivery ({s}): {list(delivery_map[s].items())[:3]}")

if actual_days < 20:
    out = {"stocks":[],"count":0,"fetchedAt":datetime.now().isoformat(),"autoRun":True,"daysOfData":actual_days}
    c   = json.dumps(out)
    os.makedirs("results",exist_ok=True)
    with open("results/matrix181_results.json","w") as f: f.write(c)
    save_via_github_api(c); exit(0)

# ── Build detail dataframe ─────────────────────────────────────────────────────
all_bhav = pd.concat(bhavcopy_list, ignore_index=True)
if "SYMBOL" not in all_bhav.columns:
    print("FATAL: SYMBOL missing"); exit(1)

for col in ["CLOSE","VOLUME","HIGH","LOW"]:
    if col in all_bhav.columns:
        all_bhav[col] = pd.to_numeric(all_bhav[col], errors="coerce")
all_bhav["DATE"] = pd.to_datetime(all_bhav["DATE"], errors="coerce")
if "SERIES" in all_bhav.columns:
    all_bhav = all_bhav[all_bhav["SERIES"]=="EQ"]
all_bhav.sort_values(["SYMBOL","DATE"], inplace=True)

# ── Build extended close dataframe (for SMA200) ────────────────────────────────
# Combine detail closes + extra closes
detail_closes = all_bhav[["SYMBOL","DATE","CLOSE"]].copy()
if extra_close_list:
    extra_df = pd.concat(extra_close_list, ignore_index=True)
    if "SYMBOL" in extra_df.columns and "CLOSE" in extra_df.columns:
        extra_df["CLOSE"] = pd.to_numeric(extra_df["CLOSE"], errors="coerce")
        extra_df["DATE"]  = pd.to_datetime(extra_df["DATE"], errors="coerce")
        if "SERIES" in extra_df.columns:
            extra_df = extra_df[extra_df["SERIES"]=="EQ"]
        all_closes = pd.concat([detail_closes, extra_df[["SYMBOL","DATE","CLOSE"]]], ignore_index=True)
    else:
        all_closes = detail_closes
else:
    all_closes = detail_closes

all_closes.sort_values(["SYMBOL","DATE"], inplace=True)

symbols = all_bhav["SYMBOL"].dropna().unique()
print(f"Total EQ : {len(symbols)}")

results   = []
processed = 0
s1_count  = 0
s2_count  = 0
sk = {k:0 for k in ["rows","close","no_mcap","s1_skip","s2_skip","grade_c"]}

for symbol in symbols:
    try:
        sdf  = all_bhav[all_bhav["SYMBOL"]==symbol].copy()
        sdf_c = all_closes[all_closes["SYMBOL"]==symbol].copy()
        processed += 1
        if processed % 500 == 0:
            print(f"  {processed}/{len(symbols)} q:{len(results)} s1:{s1_count} s2:{s2_count}")

        if len(sdf) < 20: sk["rows"]+=1; continue
        closes  = sdf["CLOSE"].dropna()
        volumes = sdf["VOLUME"].dropna()
        if len(closes) < 20: sk["close"]+=1; continue
        ltp = float(closes.iloc[-1])
        if ltp <= 0: sk["close"]+=1; continue

        # All closes including extended (for SMA200)
        all_c = sdf_c["CLOSE"].dropna()

        ltp       = round(ltp,2)
        prev      = float(closes.iloc[-2]) if len(closes)>=2 else ltp
        change    = round((ltp-prev)/prev*100,2) if prev else 0
        high_52w  = round(float(sdf["HIGH"].max()),2) if "HIGH" in sdf.columns else ltp
        low_52w   = round(float(sdf["LOW"].min()),2)  if "LOW"  in sdf.columns else ltp
        from_low_mult = round(ltp/low_52w,4)          if low_52w  else None
        from_low_pct  = round((ltp-low_52w)/low_52w*100,2)   if low_52w  else None
        from_high_pct = round((high_52w-ltp)/high_52w*100,2) if high_52w else None
        vol_today = int(volumes.iloc[-1]) if len(volumes) else 0
        sym_u = str(symbol).strip().upper()

        mcap = mcap_map.get(sym_u)
        if mcap_map and mcap is None:
            sk["no_mcap"]+=1; continue

        sorted_dates = sorted(sdf["DATE"].dropna().tolist())
        avg_tv = None
        if val_avail:
            tv_list = []
            for dr in sorted_dates[-20:]:
                try:
                    ds2 = pd.Timestamp(dr).strftime("%Y-%m-%d")
                    tv  = value_map.get(ds2,{}).get(sym_u)
                    if tv: tv_list.append(float(tv))
                except: pass
            avg_tv = calc_avg(tv_list)

        # SMAs — use all_c for 200 period
        sma9   = calc_sma(closes, 9)
        sma20  = calc_sma(closes, 20)
        sma30  = calc_sma(closes, 30)
        sma50  = calc_sma(closes, 50)  if len(closes)>=50  else None
        sma200 = calc_sma(all_c,  200) if len(all_c)>=200  else None

        rsi = calc_rsi(closes)

        v20 = float(volumes.tail(20).mean()) if len(volumes)>=20 else None
        v50 = float(volumes.tail(50).mean()) if len(volumes)>=50 else None
        vol_ratio = round(v20/v50,3) if (v20 and v50 and v50>0) else None

        del_all = []
        for dr in sorted_dates[-60:]:
            try:
                ds2 = pd.Timestamp(dr).strftime("%Y-%m-%d")
                dp  = delivery_map.get(ds2,{}).get(str(symbol))
                if dp: del_all.append(float(dp))
            except: pass
        del_20  = del_all[-20:] if len(del_all)>=20 else del_all
        del_50  = del_all[-50:] if len(del_all)>=50 else del_all
        avg_d20 = calc_avg(del_20)
        avg_d50 = calc_avg(del_50)
        lat_del = del_20[-1] if del_20 else None

        # Strategy check
        strategy = None
        if check_strategy1(ltp, mcap, rsi, from_low_mult,
                           avg_d20, avg_d50, vol_ratio, avg_tv,
                           sma30, del_avail, val_avail):
            strategy = "s1"
        elif check_strategy2(ltp, mcap, rsi, high_52w,
                             avg_d20, avg_d50, vol_ratio, avg_tv,
                             sma20, sma50, sma200, del_avail, val_avail):
            strategy = "s2"

        if strategy is None:
            if mcap and mcap <= S1_MCAP_MAX: sk["s1_skip"]+=1
            else: sk["s2_skip"]+=1
            continue

        if strategy=="s1":
            score = calc_score_s1(avg_d20,avg_d50,vol_ratio,rsi,from_low_pct,avg_tv)
        else:
            score = calc_score_s2(avg_d20,avg_d50,vol_ratio,rsi,ltp,sma20,sma50,sma200,avg_tv)

        grade = grade_setup(score,strategy,avg_d20,avg_d50,vol_ratio,from_low_pct,del_avail)
        if grade=="C": sk["grade_c"]+=1; continue

        conf,sigs,clabel = calc_confidence(
            avg_d20,avg_d50,lat_del,vol_ratio,rsi,from_low_pct,
            score,grade,del_20,ltp,sma20,sma30,sma50,sma200,strategy)

        if strategy=="s1": s1_count+=1
        else: s2_count+=1

        results.append({
            "symbol":              str(symbol),
            "strategy":            strategy,
            "strategyLabel":       "Mid/Small Cap" if strategy=="s1" else "Large/Mega Cap",
            "ltp":                 ltp,
            "change":              change,
            "high52w":             high_52w,
            "low52w":              low_52w,
            "fromLow":             from_low_pct,
            "fromHigh":            from_high_pct,
            "volume":              vol_today,
            "mcap":                round(mcap,0) if mcap else None,
            "tradedValueCr":       round(avg_tv,2) if avg_tv else None,
            "rsi":                 rsi,
            "sma9":                sma9,
            "sma20":               sma20,
            "sma30":               sma30,
            "sma50":               sma50,
            "sma200":              sma200,
            "volRatio":            vol_ratio,
            "avgDelivery":         avg_d20,
            "avgDelivery50":       avg_d50,
            "deliveryTrend":       round(avg_d20-avg_d50,2) if avg_d20 and avg_d50 else None,
            "deliveryToday":       lat_del,
            "score":               score,
            "grade":               grade,
            "daysOfData":          len(sdf),
            "confidence":          conf,
            "confidenceLabel":     clabel,
            "confidenceBreakdown": sigs,
        })
    except Exception as e:
        print(f"  Err {symbol}: {e}")

print(f"\nResults: {len(results)} (S1:{s1_count} S2:{s2_count})")
print(f"rows:{sk['rows']} close:{sk['close']} no_mcap:{sk['no_mcap']} s1:{sk['s1_skip']} s2:{sk['s2_skip']} gc:{sk['grade_c']}")

results.sort(key=lambda x: x["confidence"] or 0, reverse=True)

output = {
    "stocks":     results,
    "count":      len(results),
    "s1Count":    s1_count,
    "s2Count":    s2_count,
    "fetchedAt":  datetime.now().isoformat(),
    "autoRun":    True,
    "daysOfData": actual_days,
    "dataAvailability": {
        "mcap":     len(mcap_map)>0,
        "delivery": del_avail,
        "value":    val_avail,
        "sma200":   len(extra_close_list)>0,
    },
}
content = json.dumps(output)
os.makedirs("results",exist_ok=True)
with open("results/matrix181_results.json","w") as f: f.write(content)
print(f"\nSaved: {os.path.getsize('results/matrix181_results.json')} bytes ✓")
print(f"API  : {'SUCCESS' if save_via_github_api(content) else 'FAILED'}")
print("\n"+"="*60)
print(f"DONE — {len(results)} stocks | S1:{s1_count} S2:{s2_count}")
print(f"A+:{sum(1 for r in results if r['grade']=='A+')} "
      f"A:{sum(1 for r in results if r['grade']=='A')} "
      f"B+:{sum(1 for r in results if r['grade']=='B+')} "
      f"B:{sum(1 for r in results if r['grade']=='B')}")
print(f"VH:{sum(1 for r in results if r['confidenceLabel']=='VERY HIGH')} "
      f"H:{sum(1 for r in results if r['confidenceLabel']=='HIGH')}")
print("="*60)
