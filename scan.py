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
    "Accept": "*/*",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://www.nseindia.com/",
}
SESSION = requests.Session()
SESSION.headers.update(NSE_HEADERS)

GITHUB_TOKEN = os.environ.get("PAT_TOKEN", "")
GITHUB_USER  = "gauravmsm"
GITHUB_REPO  = "matrix181-backend"
RESULTS_PATH = "results/matrix181_results.json"

# ── S1: Mid/Small Cap ─────────────────────────────────────────────────────────
S1_MCAP_MIN      = 1500       # Cr
S1_MCAP_MAX      = 30000      # Cr
S1_RSI_MIN       = 45.0
S1_RSI_MAX       = 65.0
S1_VOL_RATIO     = 1.20       # 20D avg vol >= 1.20 × 50D avg vol
S1_DELIVERY_MIN  = 45.0       # 20D avg delivery >= 45%
S1_DELIVERY_GAP  = 3.0        # 20D delivery > 50D delivery + 3%
S1_TRADED_VALUE  = 7.5        # Cr
S1_FROM_LOW_MIN  = 1.10       # price 10% above 52W low
S1_FROM_LOW_MAX  = 1.40       # price 40% above 52W low
S1_NEAR_HIGH_PCT = 0.90       # close > 60D highest close × 0.90

# ── S2: Large/Mega Cap ────────────────────────────────────────────────────────
S2_MCAP_MIN      = 30000      # Cr
S2_RSI_MIN       = 45.0
S2_RSI_MAX       = 65.0
S2_VOL_RATIO     = 1.20       # 20D avg vol >= 1.20 × 50D avg vol
S2_DELIVERY_MIN  = 40.0       # 20D avg delivery >= 40%
S2_TRADED_VALUE  = 25.0       # Cr
S2_FROM_HIGH_MIN = 0.65       # within 35% of 52W high (price >= 52W_high * 0.65)
S2_FROM_HIGH_MAX = 0.95       # within 5%  of 52W high (price <= 52W_high * 0.95)

DAYS_DELIVERY = 25
DAYS_DETAIL   = 60
DAYS_SMA      = 210


def save_via_github_api(content_str):
    if not GITHUB_TOKEN: return False
    api_url = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{RESULTS_PATH}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    sha = None
    try:
        r = requests.get(api_url, headers=headers, timeout=15)
        if r.status_code == 200:
            sha = r.json().get("sha")
            print(f"  SHA: {sha[:8]}")
    except: pass
    payload = {"message": f"Auto scan {datetime.now().strftime('%Y-%m-%d %H:%M')}",
               "content": base64.b64encode(content_str.encode()).decode(), "branch": "main"}
    if sha: payload["sha"] = sha
    try:
        r = requests.put(api_url, headers=headers, json=payload, timeout=30)
        if r.status_code in (200, 201):
            print(f"  GitHub API OK — {r.status_code}"); return True
        print(f"  GitHub API FAILED — {r.status_code}"); return False
    except Exception as e:
        print(f"  GitHub API error: {e}"); return False

def get_trading_dates(n):
    dates, d = [], datetime.now()
    while len(dates) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5: dates.append(d)
    return dates

def fetch_full_bhavdata(date):
    d = date.strftime("%d%m%Y")
    url = f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{d}.csv"
    try:
        r = SESSION.get(url, timeout=20)
        if r.status_code == 200 and len(r.content) > 10000:
            from io import StringIO
            for enc in ["utf-8", "latin-1"]:
                try:
                    df = pd.read_csv(StringIO(r.content.decode(enc)))
                    df.columns = df.columns.str.strip().str.upper()
                    if "SYMBOL" in df.columns and "DELIV_PER" in df.columns:
                        return df
                except: continue
    except: pass
    return None

def fetch_52w_report(date):
    d = date.strftime("%d%m%Y")
    url = f"https://nsearchives.nseindia.com/content/CM_52_wk_High_low_{d}.csv"
    try:
        r = SESSION.get(url, timeout=20)
        if r.status_code == 200 and len(r.content) > 10000:
            from io import StringIO
            for enc in ["utf-8", "latin-1"]:
                try:
                    for skip in [0, 1, 2, 3]:
                        try:
                            df = pd.read_csv(StringIO(r.content.decode(enc)), skiprows=skip)
                            df.columns = (df.columns.str.strip().str.upper()
                                          .str.replace(" ","_").str.replace('"',''))
                            sym_col  = next((c for c in df.columns if "SYMBOL" in c), None)
                            high_col = next((c for c in df.columns if "52" in c and "HIGH" in c), None)
                            low_col  = next((c for c in df.columns if "52" in c and "LOW"  in c), None)
                            if sym_col and high_col and low_col:
                                df[high_col] = pd.to_numeric(df[high_col].astype(str).str.strip().str.replace('"',''), errors="coerce")
                                df[low_col]  = pd.to_numeric(df[low_col].astype(str).str.strip().str.replace('"',''),  errors="coerce")
                                result = {}
                                for _, row in df.iterrows():
                                    sym = str(row[sym_col]).strip().upper().replace('"','')
                                    h = row[high_col]; l = row[low_col]
                                    if pd.notna(h) and pd.notna(l) and h>0 and l>0:
                                        result[sym] = {"high": round(float(h),2), "low": round(float(l),2)}
                                if len(result) > 100:
                                    return result
                        except: continue
                except: continue
    except: pass
    return {}

def fetch_mcap_from_equity_list(close_map):
    url = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
    try:
        r = SESSION.get(url, timeout=20)
        if r.status_code != 200: return {}
        from io import StringIO
        for enc in ["utf-8", "latin-1", "cp1252"]:
            try:
                df = pd.read_csv(StringIO(r.content.decode(enc)))
                df.columns = df.columns.str.strip().str.upper()
                sym_col  = next((c for c in df.columns if "SYMBOL" in c), None)
                paid_col = next((c for c in df.columns if "PAID" in c), None)
                fv_col   = next((c for c in df.columns if "FACE" in c), None)
                if sym_col and paid_col and fv_col:
                    df[paid_col] = pd.to_numeric(df[paid_col], errors="coerce")
                    df[fv_col]   = pd.to_numeric(df[fv_col],   errors="coerce")
                    # Detect unit: sample median of paid_col
                    # If median < 10000 → likely in lakhs (old format)
                    # If median > 1e7  → likely in rupees, convert /1e5 to get lakhs
                    # If median > 1000 and < 1e7 → lakhs
                    sample_median = df[paid_col].dropna().median()
                    print(f"  EQUITY_L paid_col={paid_col} fv_col={fv_col} sample_median={sample_median:.0f}")
                    result = {}
                    for _, row in df.iterrows():
                        sym = str(row[sym_col]).strip().upper()
                        pu = row[paid_col]; fv = row[fv_col]; cl = close_map.get(sym)
                        if pd.notna(pu) and pu>0 and pd.notna(fv) and fv>0 and cl and cl>0:
                            # EQUITY_L PAID UP VALUE is in Crores of Rs
                            # MCap (Cr) = Close × PaidUp_Cr / FaceValue
                            mcap_cr = (cl * pu) / fv
                            if mcap_cr > 0:
                                result[sym] = round(mcap_cr, 2)
                    print(f"MCap: {len(result)} | Top5: {sorted(result.items(),key=lambda x:-x[1])[:5]}")
                    # Verify known stocks
                    for chk in ["RELIANCE","TCS","MRF","TATAMOTORS","ZOMATO"]:
                        if chk in result: print(f"  {chk}={result[chk]:.0f} Cr")
                    return result
            except: continue
    except Exception as e:
        print(f"EQUITY_L: {e}")
    return {}

def normalise_bhav(df):
    df = df.copy()
    df.columns = df.columns.str.strip().str.upper()
    rm = {}
    if "SYMBOL"  not in df.columns:
        for c in ["TCKRSYMB"]:
            if c in df.columns: rm[c]="SYMBOL"; break
    if "SERIES"  not in df.columns:
        for c in ["SCTYSRS"]:
            if c in df.columns: rm[c]="SERIES"; break
    if "CLOSE"   not in df.columns:
        for c in ["CLSPRIC","CLOSE_PRICE"]:
            if c in df.columns: rm[c]="CLOSE"; break
    if "HIGH"    not in df.columns:
        for c in ["HGHPRIC","HIGH_PRICE"]:
            if c in df.columns: rm[c]="HIGH"; break
    if "LOW"     not in df.columns:
        for c in ["LWPRIC","LOW_PRICE"]:
            if c in df.columns: rm[c]="LOW"; break
    if "VOLUME"  not in df.columns:
        for c in ["TTLTRADGVOL","TTL_TRD_QNTY","TOTTRDQTY"]:
            if c in df.columns: rm[c]="VOLUME"; break
    if "VALUE"   not in df.columns:
        for c in ["TTLNBOFTXSEXCTD","TURNOVER_LACS","TOTTRDVAL"]:
            if c in df.columns: rm[c]="VALUE"; break
    df.rename(columns=rm, inplace=True)
    return df

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
                if "SYMBOL" in df.columns: return df
        except: pass
    return None

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


# ── S1 Filter ─────────────────────────────────────────────────────────────────
s1_fdbg = {"mcap":0,"rsi":0,"sma30":0,"sma50":0,"dma_cross":0,"vol":0,"tv":0,"52w":0,"60dh":0,"del":0}

def check_s1(ltp, mcap, rsi, high_52w, low_52w, close_60d_high,
             avg_d20, avg_d50, n_del,
             vol_ratio, avg_tv,
             sma30, sma45, sma50):
    if mcap is None or not (S1_MCAP_MIN <= mcap <= S1_MCAP_MAX):
        s1_fdbg["mcap"]+=1; return False
    if rsi is None or not (S1_RSI_MIN <= rsi <= S1_RSI_MAX):
        s1_fdbg["rsi"]+=1; return False
    # Close > 30 DMA
    if sma30 is None or ltp <= sma30:
        s1_fdbg["sma30"]+=1; return False
    # Close > 50 DMA (soft — skip if unavailable)
    if sma50 is not None and ltp <= sma50:
        s1_fdbg["sma50"]+=1; return False
    if vol_ratio is None or vol_ratio < S1_VOL_RATIO:
        s1_fdbg["vol"]+=1; return False
    if avg_tv is not None and avg_tv < S1_TRADED_VALUE:
        s1_fdbg["tv"]+=1; return False
    if low_52w and low_52w > 0:
        mult = ltp / low_52w
        if not (S1_FROM_LOW_MIN <= mult <= S1_FROM_LOW_MAX):
            s1_fdbg["52w"]+=1; return False
    if close_60d_high and close_60d_high > 0:
        if ltp < close_60d_high * S1_NEAR_HIGH_PCT:
            s1_fdbg["60dh"]+=1; return False
    if n_del >= 10 and avg_d20 is not None:
        if avg_d20 < S1_DELIVERY_MIN:
            s1_fdbg["del"]+=1; return False
        if avg_d50 is not None and (avg_d20 - avg_d50) < S1_DELIVERY_GAP:
            s1_fdbg["del"]+=1; return False
    return True


# ── S2 Filter ─────────────────────────────────────────────────────────────────
def check_s2(ltp, mcap, rsi, high_52w, low_52w,
             avg_d20, avg_d50, n_del,
             vol_ratio, avg_tv,
             sma20, sma50, sma200):
    if mcap is None or mcap < S2_MCAP_MIN: return False
    if rsi is None or not (S2_RSI_MIN <= rsi <= S2_RSI_MAX): return False
    # Close > 20 DMA
    if sma20 is None or ltp <= sma20: return False
    # Close > 50 DMA
    if sma50 is None or ltp <= sma50: return False
    # 20 DMA > 50 DMA (uptrend structure)
    if sma20 <= sma50: return False
    # Close > 200 DMA (soft)
    if sma200 is not None and ltp <= sma200: return False
    if vol_ratio is None or vol_ratio < S2_VOL_RATIO: return False
    if avg_tv is not None and avg_tv < S2_TRADED_VALUE: return False
    if high_52w and high_52w > 0:
        ratio = ltp / high_52w
        if not (S2_FROM_HIGH_MIN <= ratio <= S2_FROM_HIGH_MAX): return False
    if n_del >= 10 and avg_d20 is not None:
        if avg_d20 < S2_DELIVERY_MIN: return False
    return True


def calc_score_s1(avg_d20, avg_d50, vol_ratio, rsi, from_low_pct, avg_tv):
    s = 0.0
    if avg_d20 and avg_d20>=45:   s += min((avg_d20-45)/35*20, 20)
    if avg_d20 and avg_d50 and (avg_d20-avg_d50)>3:
        s += min((avg_d20-avg_d50-3)/12*20, 20)
    if vol_ratio and vol_ratio>=1.3: s += min((vol_ratio-1.3)/0.7*20, 20)
    if rsi: s += max(0, 20-abs(rsi-55)*1.0)
    if from_low_pct:
        if 15<=from_low_pct<=30:  s+=15
        elif 10<=from_low_pct<15: s+=10
        elif 30<from_low_pct<=40: s+=8
    if avg_tv and avg_tv>=7.5: s += min((avg_tv-7.5)/42.5*10, 10)
    return min(int(round(s)), 100)

def calc_score_s2(avg_d20, avg_d50, vol_ratio, rsi, ltp, sma20, sma50, sma200, avg_tv):
    s = 0.0
    if avg_d20 and avg_d20>=40:   s += min((avg_d20-40)/40*20, 20)
    if avg_d20 and avg_d50 and (avg_d20-avg_d50)>3:
        s += min((avg_d20-avg_d50-3)/12*20, 20)
    if vol_ratio and vol_ratio>=1.2: s += min((vol_ratio-1.2)/0.8*20, 20)
    if rsi: s += max(0, 20-abs(rsi-55)*1.0)
    ma = sum([1 if ltp and sma20  and ltp>sma20  else 0,
              1 if ltp and sma50  and ltp>sma50  else 0,
              1 if ltp and sma200 and ltp>sma200 else 0])
    s += ma * 5
    if avg_tv and avg_tv>=25: s += min((avg_tv-25)/75*10, 10)
    return min(int(round(s)), 100)

def grade_setup(score, strategy, avg_d20, avg_d50, vol_ratio, from_low_pct, n_del):
    gap    = (avg_d20-avg_d50) if avg_d20 and avg_d50 else 0
    del_ok = n_del >= 10 and avg_d20 is not None
    if strategy=="s1":
        if del_ok:
            if score>=82 and avg_d20>=60 and gap>=8  and vol_ratio>=1.5  and from_low_pct and 15<=from_low_pct<=30: return "A+"
            if score>=68 and avg_d20>=52 and gap>=3  and vol_ratio>=1.35 and from_low_pct and from_low_pct<=35:     return "A"
        else:
            if score>=82 and vol_ratio>=1.5  and from_low_pct and 15<=from_low_pct<=30: return "A+"
            if score>=68 and vol_ratio>=1.35 and from_low_pct and from_low_pct<=35:      return "A"
        if score>=52: return "B+"
        if score>=38: return "B"
    else:
        if del_ok:
            if score>=82 and avg_d20>=55 and gap>=8  and vol_ratio>=1.5:  return "A+"
            if score>=68 and avg_d20>=48 and gap>=3  and vol_ratio>=1.35: return "A"
        else:
            if score>=82 and vol_ratio>=1.5:  return "A+"
            if score>=68 and vol_ratio>=1.35: return "A"
        if score>=52: return "B+"
        if score>=38: return "B"
    return "C"

def calc_confidence(avg_d20, avg_d50, del_today, vol_ratio, rsi, from_low_pct,
                    score, grade, del_vals, ltp, sma20, sma30, sma45, sma50, sma150, sma200, strategy):
    sig = {}
    # Delivery quality (0–20)
    dq = 0
    if del_vals and len(del_vals)>=3:
        thresh = 50 if strategy=="s1" else 45
        dq = min(sum(1 for d in del_vals if d>=thresh)/len(del_vals)*20, 20)
        if del_today and avg_d20 and del_today>avg_d20: dq=min(dq+3,20)
    elif avg_d20 and avg_d20>=45: dq=10
    sig["delivery_quality"] = round(dq,1)
    # Delivery trend (0–15)
    dt = 0
    if avg_d20 and avg_d50 and avg_d20>avg_d50:
        dt = min((avg_d20-avg_d50)/10*15, 15)
    sig["delivery_trend"] = round(dt,1)
    # Volume expansion (0–20)
    vc = 0
    if vol_ratio:
        if vol_ratio>=2.0:    vc=20
        elif vol_ratio>=1.75: vc=16
        elif vol_ratio>=1.5:  vc=12
        elif vol_ratio>=1.3:  vc=8
        elif vol_ratio>=1.2:  vc=5
    sig["volume_expansion"] = round(vc,1)
    # RSI zone (0–15)
    rq = 15 if rsi and 50<=rsi<=60 else 12 if rsi and 47<=rsi<=63 else 8 if rsi and 45<=rsi<=65 else 0
    sig["rsi_zone"] = round(rq,1)
    # Price position (0–15)
    rr = 0
    if strategy=="s1" and from_low_pct:
        if 15<=from_low_pct<=30:  rr=15
        elif 10<=from_low_pct<15: rr=11
        elif 30<from_low_pct<=40: rr=8
    elif strategy=="s2":
        ma = sum([1 if ltp and sma20  and ltp>sma20  else 0,
                  1 if ltp and sma50  and ltp>sma50  else 0,
                  1 if ltp and sma200 and ltp>sma200 else 0])
        rr = ma * 5
    sig["price_position"] = round(rr,1)
    # Trend alignment (0–10)
    ta = 0
    if strategy=="s1":
        if ltp and sma30 and sma50 and ltp>sma30 and sma30>sma50: ta=10
        elif ltp and sma30 and ltp>sma30: ta=5
    else:
        if ltp and sma20 and sma50 and ltp>sma20 and sma20>sma50: ta=10
        elif ltp and sma20 and ltp>sma20: ta=5
    sig["trend_alignment"] = round(ta,1)
    # Grade bonus (0–10)
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
print("="*60)
print("\nS1 Filters: MCap 1500-30K | RSI 45-65 | Close>20/50DMA | 20DMA>50DMA")
print("            Vol≥1.30x | Del≥45% | Del20>Del50+3% | TV≥7.5Cr")
print("            Price 10-40% above 52WLow | Close>60D-High×0.90")
print("S2 Filters: MCap>30K | RSI 45-65 | Close>20/50/200DMA | 50DMA>200DMA")
print("            Vol≥1.20x | Del≥40% | TV≥25Cr | Price 5-35% below 52WHigh")

trading_dates = get_trading_dates(DAYS_DELIVERY)
delivery_map  = {}
value_map     = {}
latest_close  = {}
bhavcopy_full = []

# ── Step 1: 25 days sec_bhavdata_full in parallel ─────────────────────────────
print(f"\nDownloading {DAYS_DELIVERY} days sec_bhavdata_full...")

def _fetch_full(date):
    return date, fetch_full_bhavdata(date)

with ThreadPoolExecutor(max_workers=6) as ex:
    futures = {ex.submit(_fetch_full, d): d for d in trading_dates}
    for future in as_completed(futures):
        date, df = future.result()
        if df is None: continue
        ds = date.strftime("%Y-%m-%d")
        eq = df[df["SERIES"].astype(str).str.strip()=="EQ"].copy() if "SERIES" in df.columns else df.copy()
        # Delivery
        if "DELIV_PER" in eq.columns and "SYMBOL" in eq.columns:
            dpct = pd.to_numeric(eq["DELIV_PER"], errors="coerce")
            syms = eq["SYMBOL"].astype(str).str.strip().str.upper()
            mask = (dpct > 0) & (dpct <= 100)
            if mask.sum() > 100:
                delivery_map[ds] = {syms[i]: round(float(dpct[i]),2) for i in eq[mask].index}
        # Value
        val_col = next((c for c in ["TURNOVER_LACS","VALUE","TOTTRDVAL"] if c in eq.columns), None)
        if val_col and "SYMBOL" in eq.columns:
            v_num = pd.to_numeric(eq[val_col], errors="coerce")
            syms  = eq["SYMBOL"].astype(str).str.strip().str.upper()
            vd = {syms[i]: float(v_num[i])/100
                  for i in eq.index if pd.notna(v_num[i] if i in v_num.index else np.nan) and v_num[i]>0}
            if vd: value_map[ds] = vd
        # Build latest_close from ALL delivery days (merge, keep latest)
        if "CLOSE_PRICE" in eq.columns and "SYMBOL" in eq.columns:
            cl   = pd.to_numeric(eq["CLOSE_PRICE"], errors="coerce")
            syms = eq["SYMBOL"].astype(str).str.strip().str.upper()
            for i in eq.index:
                c = cl[i] if i in cl.index else np.nan
                sym_i = syms[i]
                # Only update if not already set (first = most recent since we iterate newest first)
                if pd.notna(c) and c > 0 and sym_i not in latest_close:
                    latest_close[sym_i] = float(c)
        df2 = normalise_bhav(df); df2["DATE"] = ds
        bhavcopy_full.append(df2)

del_days_count = len(delivery_map)
print(f"  Delivery days: {del_days_count} | Value days: {len(value_map)} | Closes: {len(latest_close)}")
if delivery_map:
    s = sorted(delivery_map.keys())[-1]
    print(f"  Latest delivery ({s}): {list(delivery_map[s].items())[:3]}")

# ── Step 2: 52W report ────────────────────────────────────────────────────────
print("\nFetching 52W High Low report...")
w52_map = {}
for date in get_trading_dates(5):
    w52_map = fetch_52w_report(date)
    if w52_map:
        print(f"  52W: {len(w52_map)} ✓ sample: {list(w52_map.items())[:2]}")
        break
if not w52_map:
    print("  52W: not available (will use close-data fallback)")

# ── Step 3: MCap ──────────────────────────────────────────────────────────────
print(f"\nLoading MCap ({len(latest_close)} closes)...")
mcap_map   = fetch_mcap_from_equity_list(latest_close)
mcap_avail = len(mcap_map) > 0

# ── Step 4: 60-day bhavcopy ───────────────────────────────────────────────────
detail_dates = get_trading_dates(DAYS_DETAIL)
loaded_dates = {b["DATE"].iloc[0] for b in bhavcopy_full if len(b)>0 and "DATE" in b.columns}
bhav_regular = []
cols_printed  = False
print(f"\nPhase 1: {len(detail_dates)} detail days...")
for i, date in enumerate(detail_dates):
    ds = date.strftime("%Y-%m-%d")
    if ds in loaded_dates: continue
    try:
        bhav = fetch_bhavcopy(date)
        if bhav is None: continue
        bhav["DATE"] = ds
        bhav_regular.append(bhav)
        if not cols_printed:
            print(f"  Bhav cols: {list(bhav.columns)}")
            cols_printed = True
        eq_mask = bhav["SERIES"].astype(str).str.strip()=="EQ" if "SERIES" in bhav.columns else pd.Series([True]*len(bhav),index=bhav.index)
        bhav_eq = bhav[eq_mask].copy()
        # Extract delivery from bhavcopy RSVD1/VOLUME (RSVD1 = deliverable qty)
        if ds not in delivery_map and "RSVD1" in bhav_eq.columns and "VOLUME" in bhav_eq.columns and "SYMBOL" in bhav_eq.columns:
            dq   = pd.to_numeric(bhav_eq["RSVD1"],  errors="coerce")
            vol  = pd.to_numeric(bhav_eq["VOLUME"], errors="coerce")
            syms = bhav_eq["SYMBOL"].astype(str).str.strip().str.upper()
            mask = (dq > 0) & (vol > 0)
            if mask.sum() > 50:
                ratio = (dq[mask] / vol[mask]).median()
                if 0 < ratio <= 1:  # confirms it's a qty ratio
                    pct = (dq / vol * 100).round(2)
                    vmask = (pct > 0) & (pct <= 100) & (dq > 0) & (vol > 0)
                    dd = {syms[i]: float(pct[i]) for i in bhav_eq[vmask].index if str(syms[i]) not in ("","NAN")}
                    if dd:
                        delivery_map[ds] = dd
        if ds not in value_map and "VALUE" in bhav_eq.columns and "SYMBOL" in bhav_eq.columns:
            v_num = pd.to_numeric(bhav_eq["VALUE"], errors="coerce")
            syms  = bhav_eq["SYMBOL"].astype(str).str.strip().str.upper()
            vd = {syms[i]: float(v_num[i])/100 for i in bhav_eq.index
                  if pd.notna(v_num[i] if i in v_num.index else np.nan) and v_num[i]>0}
            if vd: value_map[ds] = vd
        if (i+1) % 10 == 0:
            print(f"  {i+1}/{len(detail_dates)} loaded:{len(bhav_regular)+len(bhavcopy_full)} val:{len(value_map)}")
    except Exception as e:
        print(f"  Error {date}: {e}")

# ── Step 5: Extra closes for SMA150/200 ──────────────────────────────────────
extra_dates = get_trading_dates(DAYS_SMA)[DAYS_DETAIL:]
print(f"\nPhase 2: {len(extra_dates)} close-only days...")
extra_list = []
for i, date in enumerate(extra_dates):
    try:
        bhav = fetch_bhavcopy(date)
        if bhav is not None:
            bhav["DATE"] = date.strftime("%Y-%m-%d")
            cols = ["SYMBOL","CLOSE","DATE"]
            if "SERIES" in bhav.columns: cols.append("SERIES")
            extra_list.append(bhav[cols].copy())
        if (i+1) % 40 == 0:
            print(f"  {i+1}/{len(extra_dates)} loaded:{len(extra_list)}")
    except: pass

# ── Build dataframes ───────────────────────────────────────────────────────────
all_frames  = bhavcopy_full + bhav_regular
actual_days = len(all_frames)
del_avail   = del_days_count > 0
val_avail   = len(value_map) > 0

print(f"\n{'='*40}")
print(f"Bhavcopy : {actual_days}")
print(f"Delivery : {del_days_count} days {'✓' if del_avail else '✗'}")
print(f"Value    : {len(value_map)} {'✓' if val_avail else '✗'}")
print(f"MCap     : {len(mcap_map)} {'✓' if mcap_avail else '✗'}")
print(f"52W      : {len(w52_map)} {'✓' if w52_map else '✗'}")
print(f"SMA days : {len(extra_list)+actual_days}")

if actual_days < 20:
    out = {"stocks":[],"count":0,"fetchedAt":datetime.now().isoformat(),"autoRun":True}
    c   = json.dumps(out)
    os.makedirs("results",exist_ok=True)
    with open("results/matrix181_results.json","w") as f: f.write(c)
    save_via_github_api(c); exit(0)

all_bhav = pd.concat(all_frames, ignore_index=True)
for col in ["CLOSE","VOLUME","HIGH","LOW"]:
    if col in all_bhav.columns:
        all_bhav[col] = pd.to_numeric(all_bhav[col], errors="coerce")
all_bhav["DATE"] = pd.to_datetime(all_bhav["DATE"], errors="coerce")
if "SERIES" in all_bhav.columns:
    all_bhav = all_bhav[all_bhav["SERIES"]=="EQ"]
all_bhav.sort_values(["SYMBOL","DATE"], inplace=True)
all_bhav.drop_duplicates(subset=["SYMBOL","DATE"], keep="last", inplace=True)

detail_closes = all_bhav[["SYMBOL","DATE","CLOSE"]].copy()
if extra_list:
    extra_df = pd.concat(extra_list, ignore_index=True)
    extra_df["CLOSE"] = pd.to_numeric(extra_df["CLOSE"], errors="coerce")
    extra_df["DATE"]  = pd.to_datetime(extra_df["DATE"],  errors="coerce")
    if "SERIES" in extra_df.columns:
        extra_df = extra_df[extra_df["SERIES"]=="EQ"]
    all_closes = pd.concat([detail_closes, extra_df[["SYMBOL","DATE","CLOSE"]]], ignore_index=True)
else:
    all_closes = detail_closes
all_closes.drop_duplicates(subset=["SYMBOL","DATE"], keep="last", inplace=True)
all_closes.sort_values(["SYMBOL","DATE"], inplace=True)

symbols = all_bhav["SYMBOL"].dropna().unique()
print(f"Total EQ : {len(symbols)}")

# Print first mid-cap stock we find in data for diagnostics
print("Sample stocks:")
shown = 0
for sym in symbols:
    if shown >= 3: break
    mc = mcap_map.get(str(sym).upper())
    if mc and S1_MCAP_MIN <= mc <= S1_MCAP_MAX:
        sdf_t = all_bhav[all_bhav["SYMBOL"]==sym]
        sdf_ct = all_closes[all_closes["SYMBOL"]==sym]
        cl_t = sdf_t["CLOSE"].dropna()
        vc_t = sdf_t["VOLUME"].dropna()
        ac_t = sdf_ct["CLOSE"].dropna()
        if len(cl_t) < 20: continue
        ltp_t = round(float(cl_t.iloc[-1]),2)
        s30_t = calc_sma(cl_t,30)
        s50_t = calc_sma(cl_t,50)
        v20_t = float(vc_t.tail(20).mean()) if len(vc_t)>=20 else None
        v50_t = float(vc_t.tail(50).mean()) if len(vc_t)>=50 else None
        vr_t  = round(v20_t/v50_t,2) if v20_t and v50_t else None
        rsi_t = calc_rsi(cl_t)
        yr_t  = ac_t.tail(252) if len(ac_t)>=100 else ac_t
        l52_t = round(float(yr_t.min()),2) if len(yr_t)>0 else None
        h52_t = round(float(yr_t.max()),2) if len(yr_t)>0 else None
        h60_t = round(float(cl_t.tail(60).max()),2)
        print(f"  SYM={sym} ltp={ltp_t} mc={round(mc)} rsi={rsi_t} s30={s30_t} s50={s50_t} vr={vr_t} l52={l52_t} h52={h52_t} h60={h60_t}")
        shown += 1

results  = []
s1_count = s2_count = processed = 0
sk = {k:0 for k in ["rows","close","mcap","s1","s2","gc"]}

for symbol in symbols:
    try:
        sdf   = all_bhav[all_bhav["SYMBOL"]==symbol].copy()
        sdf_c = all_closes[all_closes["SYMBOL"]==symbol].copy()
        processed += 1
        if processed % 500 == 0:
            dbg_str = " ".join(f"{k}={v}" for k,v in s1_fdbg.items())
            print(f"  DBG@{processed}: {dbg_str}")

        if len(sdf)<20: sk["rows"]+=1; continue
        closes  = sdf["CLOSE"].dropna()
        volumes = sdf["VOLUME"].dropna()
        if len(closes)<20: sk["close"]+=1; continue
        ltp = float(closes.iloc[-1])
        if ltp<=0: sk["close"]+=1; continue

        all_c  = sdf_c["CLOSE"].dropna()
        ltp    = round(ltp,2)
        prev   = float(closes.iloc[-2]) if len(closes)>=2 else ltp
        change = round((ltp-prev)/prev*100,2) if prev else 0
        sym_u  = str(symbol).strip().upper()

        # 52W
        if w52_map and sym_u in w52_map:
            high_52w = w52_map[sym_u]["high"]
            low_52w  = w52_map[sym_u]["low"]
        elif len(all_c) >= 100:
            yr       = all_c.tail(252)
            high_52w = round(float(yr.max()),2)
            low_52w  = round(float(yr.min()),2)
        else:
            high_52w = None
            low_52w  = None

        from_low_pct  = round((ltp-low_52w)/low_52w*100,2)   if low_52w  and low_52w>0 else None
        from_high_pct = round((high_52w-ltp)/high_52w*100,2) if high_52w and high_52w>0 else None
        vol_today     = int(volumes.iloc[-1]) if len(volumes) else 0

        # 60D highest close for S1 near-high filter
        close_60d_high = round(float(closes.tail(60).max()),2) if len(closes)>=20 else None

        mcap = mcap_map.get(sym_u)
        if mcap_avail and mcap is None: sk["mcap"]+=1; continue

        sorted_dates = sorted(sdf["DATE"].dropna().tolist())

        # Traded value
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

        # SMAs
        sma9   = calc_sma(closes,9)
        sma20  = calc_sma(closes,20)
        sma30  = calc_sma(closes,30)  if len(closes)>=30  else None
        sma45  = calc_sma(closes,45)  if len(closes)>=45  else None
        sma50  = calc_sma(closes,50)  if len(closes)>=50  else None
        sma150 = calc_sma(all_c,150)  if len(all_c)>=150  else None
        sma200 = calc_sma(all_c,200)  if len(all_c)>=200  else None
        rsi    = calc_rsi(closes)

        # Volume ratio
        v20 = float(volumes.tail(20).mean()) if len(volumes)>=20 else None
        v50 = float(volumes.tail(50).mean()) if len(volumes)>=50 else None
        vol_ratio = round(v20/v50,3) if (v20 and v50 and v50>0) else None

        # Delivery
        del_all = []
        for dr in sorted_dates[-60:]:
            try:
                ds2 = pd.Timestamp(dr).strftime("%Y-%m-%d")
                dp  = delivery_map.get(ds2,{}).get(sym_u)
                if dp: del_all.append(float(dp))
            except: pass
        del_20  = del_all[-20:] if len(del_all)>=20 else del_all
        del_50  = del_all[-50:] if len(del_all)>=50 else del_all
        avg_d20 = calc_avg(del_20)
        avg_d50 = calc_avg(del_50)
        lat_del = del_20[-1] if del_20 else None
        n_del   = len(del_20)

        # Print first mid-cap stock details
        if not hasattr(check_s1, '_printed') and mcap and S1_MCAP_MIN <= mcap <= S1_MCAP_MAX:
            check_s1._printed = True
            print(f'MC {sym_u} ltp={ltp} mc={round(mcap)} rsi={rsi} s30={sma30} s50={sma50} vr={vol_ratio} tv={avg_tv} l52={low_52w} h60={close_60d_high} d20={avg_d20} nd={n_del}')

        # Strategy
        strategy = None
        if check_s1(ltp,mcap,rsi,high_52w,low_52w,close_60d_high,
                    avg_d20,avg_d50,n_del,vol_ratio,avg_tv,sma30,sma45,sma50):
            strategy = "s1"
        elif check_s2(ltp,mcap,rsi,high_52w,low_52w,
                      avg_d20,avg_d50,n_del,vol_ratio,avg_tv,sma20,sma50,sma200):
            strategy = "s2"

        if strategy is None:
            if mcap and mcap<=S1_MCAP_MAX: sk["s1"]+=1
            else: sk["s2"]+=1
            continue

        score = calc_score_s1(avg_d20,avg_d50,vol_ratio,rsi,from_low_pct,avg_tv) if strategy=="s1" \
                else calc_score_s2(avg_d20,avg_d50,vol_ratio,rsi,ltp,sma20,sma50,sma200,avg_tv)
        grade = grade_setup(score,strategy,avg_d20,avg_d50,vol_ratio,from_low_pct,n_del)
        if grade=="C": sk["gc"]+=1; continue

        conf,sigs,clabel = calc_confidence(
            avg_d20,avg_d50,lat_del,vol_ratio,rsi,from_low_pct,
            score,grade,del_20,ltp,sma20,sma30,sma45,sma50,sma150,sma200,strategy)

        if strategy=="s1": s1_count+=1
        else: s2_count+=1

        results.append({
            "symbol":       str(symbol),
            "strategy":     strategy,
            "strategyLabel":"Mid/Small Cap" if strategy=="s1" else "Large/Mega Cap",
            "ltp":          ltp,
            "change":       change,
            "high52w":      high_52w,
            "low52w":       low_52w,
            "fromLow":      from_low_pct,
            "fromHigh":     from_high_pct,
            "volume":       vol_today,
            "mcap":         round(mcap,0) if mcap else None,
            "tradedValueCr":round(avg_tv,2) if avg_tv else None,
            "rsi":          rsi,
            "sma9":         sma9,
            "sma20":        sma20,
            "sma30":        sma30,
            "sma45":        sma45,
            "sma50":        sma50,
            "sma150":       sma150,
            "sma200":       sma200,
            "volRatio":     vol_ratio,
            "avgDelivery":  avg_d20,
            "avgDelivery50":avg_d50,
            "deliveryTrend":round(avg_d20-avg_d50,2) if avg_d20 and avg_d50 else None,
            "deliveryToday":lat_del,
            "deliveryDays": n_del,
            "score":        score,
            "grade":        grade,
            "daysOfData":   len(sdf),
            "confidence":   conf,
            "confidenceLabel":  clabel,
            "confidenceBreakdown": sigs,
        })
    except Exception as e:
        print(f"  Err {symbol}: {e}")

print(f"\nS1 filter debug (final): {s1_fdbg}")
print(f"Results  : {len(results)} (S1:{s1_count} S2:{s2_count})")
print(f"Skipped  : rows:{sk['rows']} close:{sk['close']} mcap:{sk['mcap']} s1:{sk['s1']} s2:{sk['s2']} gc:{sk['gc']}")
results.sort(key=lambda x: x["confidence"] or 0, reverse=True)

output = {
    "stocks":    results,
    "count":     len(results),
    "s1Count":   s1_count,
    "s2Count":   s2_count,
    "fetchedAt": datetime.now().isoformat(),
    "autoRun":   True,
    "daysOfData":actual_days,
    "dataAvailability": {
        "mcap":         mcap_avail,
        "delivery":     del_avail,
        "deliveryDays": del_days_count,
        "value":        val_avail,
        "sma200":       len(extra_list)>0,
        "w52":          len(w52_map)>0,
    },
}
content = json.dumps(output)
os.makedirs("results",exist_ok=True)
with open("results/matrix181_results.json","w") as f: f.write(content)
print(f"\nSaved: {os.path.getsize('results/matrix181_results.json')} bytes ✓")
print(f"API  : {'SUCCESS' if save_via_github_api(content) else 'FAILED'}")
print("\n"+"="*60)
print(f"DONE — {len(results)} | S1:{s1_count} S2:{s2_count}")
print(f"A+:{sum(1 for r in results if r['grade']=='A+')} "
      f"A:{sum(1 for r in results if r['grade']=='A')} "
      f"B+:{sum(1 for r in results if r['grade']=='B+')} "
      f"B:{sum(1 for r in results if r['grade']=='B')}")
print("="*60)
