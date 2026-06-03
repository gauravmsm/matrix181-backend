import pandas as pd
import numpy as np
import requests
import zipfile
import io
import json
import os
import base64
from datetime import datetime, timedelta

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

S1_MCAP_MIN     = 1500
S1_MCAP_MAX     = 30000
S1_DELIVERY_MIN = 45.0
S1_DELIVERY_GAP = 5.0
S1_VOL_RATIO    = 1.25
S1_RSI_MIN      = 45.0
S1_RSI_MAX      = 60.0
S1_FROM_LOW_MIN = 1.10
S1_FROM_LOW_MAX = 1.35
S1_TRADED_VALUE = 10.0

S2_MCAP_MIN      = 30000
S2_DELIVERY_MIN  = 40.0
S2_DELIVERY_GAP  = 3.0
S2_VOL_RATIO     = 1.20
S2_RSI_MIN       = 50.0
S2_RSI_MAX       = 65.0
S2_TRADED_VALUE  = 25.0
S2_FROM_HIGH_MAX = 0.85

DAYS_DETAIL = 60
DAYS_SMA    = 210

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

def get_session():
    try:
        SESSION.get("https://www.nseindia.com", timeout=10)
        print("NSE session ready")
    except: pass

def fetch_mcap_from_equity_list(close_map):
    url = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
    try:
        r = SESSION.get(url, timeout=20)
        print(f"  EQUITY_L → {r.status_code}")
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
                    result = {}
                    for _, row in df.iterrows():
                        sym = str(row[sym_col]).strip().upper()
                        pu  = row[paid_col]; fv = row[fv_col]; cl = close_map.get(sym)
                        if pd.notna(pu) and pu>0 and pd.notna(fv) and fv>0 and cl and cl>0:
                            result[sym] = round((cl*(pu*1e5/fv))/1e7, 2)
                    print(f"  MCap: {len(result)} | Top5: {sorted(result.items(),key=lambda x:-x[1])[:5]}")
                    return result
            except Exception as e:
                print(f"  EQUITY_L parse: {e}"); continue
    except Exception as e:
        print(f"  EQUITY_L error: {e}")
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
        for c in ["TTLNBOFTXSEXCTD","TTLBOFTXSEXCTD","TOTTRDVAL","TTLTRDDVAL"]:
            if c in df.columns: rm[c]="VALUE"; break
    # Map RSVD1 → DELIV_QTY (confirmed present in log)
    # Map RSVD2 → DELIV_PCT (may or may not have data)
    if "DELIV_QTY" not in df.columns and "RSVD1" in df.columns:
        rm["RSVD1"] = "DELIV_QTY"
    if "DELIV_PCT" not in df.columns and "RSVD2" in df.columns:
        rm["RSVD2"] = "DELIV_PCT"
    df.rename(columns=rm, inplace=True)
    return df

def extract_delivery(bhav_eq):
    """
    Extract delivery % from bhavcopy EQ rows.
    Strategy:
    1. DELIV_PCT (RSVD2) if values are 0-100 range
    2. DELIV_QTY (RSVD1) / VOLUME ratio
    3. Try RSVD3, RSVD4 as delivery qty fallback
    Returns dict {SYMBOL: delivery_pct}
    """
    if "SYMBOL" not in bhav_eq.columns:
        return {}

    syms = bhav_eq["SYMBOL"].astype(str).str.strip().str.upper()
    vol  = pd.to_numeric(bhav_eq.get("VOLUME", pd.Series()), errors="coerce") if "VOLUME" in bhav_eq.columns else pd.Series()

    # ── Method 1: DELIV_PCT directly (RSVD2) ─────────────────────────────────
    if "DELIV_PCT" in bhav_eq.columns:
        pct = pd.to_numeric(bhav_eq["DELIV_PCT"], errors="coerce")
        valid = (pct > 0) & (pct <= 100)
        if valid.sum() > 50:
            dd = {syms[i]: round(float(pct[i]),2)
                  for i in bhav_eq[valid].index
                  if str(syms[i]) not in ("", "NAN", "nan")}
            if dd:
                print(f"  Delivery via DELIV_PCT(RSVD2): {len(dd)} ✓ sample={list(dd.items())[:2]}")
                return dd

    # ── Method 2: DELIV_QTY / VOLUME (RSVD1 / VOLUME) ────────────────────────
    if "DELIV_QTY" in bhav_eq.columns and len(vol) > 0:
        dq = pd.to_numeric(bhav_eq["DELIV_QTY"], errors="coerce")
        # Reindex vol to match bhav_eq
        vol_r = vol.reindex(bhav_eq.index)
        mask  = (dq > 0) & (vol_r > 0)
        if mask.sum() > 50:
            ratio = (dq[mask] / vol_r[mask])
            med   = ratio.median()
            print(f"  RSVD1/VOL median={med:.4f} count={mask.sum()} sample_dq={dq[mask].head(3).tolist()} sample_vol={vol_r[mask].head(3).tolist()}")
            if 0 < med <= 1:
                pct  = (dq / vol_r * 100).round(2)
                vmask = (pct > 0) & (pct <= 100) & mask
                dd = {syms[i]: float(pct[i])
                      for i in bhav_eq[vmask].index
                      if str(syms[i]) not in ("", "NAN", "nan")}
                if dd:
                    print(f"  Delivery via RSVD1/VOL: {len(dd)} ✓")
                    return dd
            elif med > 1:
                # RSVD1 might be in different unit — try as absolute qty vs VOLUME
                print(f"  RSVD1 median ratio > 1 ({med:.2f}) — trying RSVD3, RSVD4")

    # ── Method 3: Try RSVD3, RSVD4 as delivery qty ────────────────────────────
    for rsvd in ["RSVD3", "RSVD4"]:
        if rsvd not in bhav_eq.columns or len(vol) == 0:
            continue
        dq    = pd.to_numeric(bhav_eq[rsvd], errors="coerce")
        vol_r = vol.reindex(bhav_eq.index)
        mask  = (dq > 0) & (vol_r > 0)
        if mask.sum() < 50:
            continue
        ratio = (dq[mask] / vol_r[mask])
        med   = ratio.median()
        print(f"  {rsvd}/VOL median={med:.4f} count={mask.sum()}")
        if 0 < med <= 1:
            pct   = (dq / vol_r * 100).round(2)
            vmask = (pct > 0) & (pct <= 100) & mask
            dd = {syms[i]: float(pct[i])
                  for i in bhav_eq[vmask].index
                  if str(syms[i]) not in ("", "NAN", "nan")}
            if dd:
                print(f"  Delivery via {rsvd}/VOL: {len(dd)} ✓")
                return dd

    print(f"  No delivery found in this bhavcopy")
    return {}

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

def get_trading_dates(n):
    dates, d = [], datetime.now()
    while len(dates) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5: dates.append(d)
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

def check_s1(ltp, mcap, rsi, from_low_mult, avg_d20, avg_d50,
             vol_ratio, avg_tv, sma45, sma150, del_avail, val_avail):
    if mcap is None or not (S1_MCAP_MIN <= mcap <= S1_MCAP_MAX): return False
    if val_avail and avg_tv is not None and avg_tv < S1_TRADED_VALUE: return False
    if rsi is None or not (S1_RSI_MIN <= rsi <= S1_RSI_MAX): return False
    if from_low_mult is None or not (S1_FROM_LOW_MIN <= from_low_mult <= S1_FROM_LOW_MAX): return False
    if sma45  is None or ltp <= sma45:  return False
    if sma150 is None or ltp <= sma150: return False
    if vol_ratio is None or vol_ratio < S1_VOL_RATIO: return False
    if del_avail:
        if avg_d20 is not None and avg_d20 < S1_DELIVERY_MIN: return False
        if avg_d20 is not None and avg_d50 is not None and (avg_d20-avg_d50) < S1_DELIVERY_GAP: return False
    return True

def check_s2(ltp, mcap, rsi, high_52w, avg_d20, avg_d50,
             vol_ratio, avg_tv, sma20, sma50, sma200, del_avail, val_avail):
    if mcap is None or mcap < S2_MCAP_MIN: return False
    if val_avail and avg_tv is not None and avg_tv < S2_TRADED_VALUE: return False
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
    if avg_d20 and avg_d20>=45:   s += min((avg_d20-45)/35*20, 20)
    if avg_d20 and avg_d50 and (avg_d20-avg_d50)>5:
        s += min((avg_d20-avg_d50-5)/15*20, 20)
    if vol_ratio and vol_ratio>=1.25: s += min((vol_ratio-1.25)/0.75*20, 20)
    if rsi: s += max(0, 20-abs(rsi-52.5)*1.2)
    if from_low_pct:
        if 15<=from_low_pct<=25:  s+=15
        elif 10<=from_low_pct<15: s+=10
        elif 25<from_low_pct<=35: s+=8
    if avg_tv and avg_tv>=10: s += min((avg_tv-10)/40*10, 10)
    return min(int(round(s)), 100)

def calc_score_s2(avg_d20, avg_d50, vol_ratio, rsi, ltp, sma20, sma50, sma200, avg_tv):
    s = 0.0
    if avg_d20 and avg_d20>=40:   s += min((avg_d20-40)/40*20, 20)
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

def calc_confidence(avg_d20, avg_d50, del_today, vol_ratio, rsi,
                    from_low_pct, score, grade, del_vals,
                    ltp, sma20, sma45, sma50, sma150, sma200, strategy):
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
        if 15<=from_low_pct<=25:  rr=15
        elif 10<=from_low_pct<15: rr=11
        elif 25<from_low_pct<=30: rr=9
        elif 30<from_low_pct<=35: rr=6
    elif strategy=="s2":
        ma = sum([1 if ltp and sma20  and ltp>sma20  else 0,
                  1 if ltp and sma50  and ltp>sma50  else 0,
                  1 if ltp and sma200 and ltp>sma200 else 0])
        rr = ma * 5
    sig["price_position"] = round(rr,1)
    ta = 0
    if strategy=="s1":
        if ltp and sma45  and ltp>sma45:  ta+=5
        if ltp and sma150 and ltp>sma150: ta+=5
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
print("="*60)

get_session()

detail_dates = get_trading_dates(DAYS_DETAIL)
print(f"\nPhase 1: {len(detail_dates)} days (sequential)...")

bhavcopy_list = []
delivery_map  = {}
value_map     = {}
latest_close  = {}
cols_printed  = False
del_method    = None  # track which method worked

for i, date in enumerate(detail_dates):
    try:
        bhav = fetch_bhavcopy(date)
        ds   = date.strftime("%Y-%m-%d")
        if bhav is None: continue

        bhav["DATE"] = ds
        bhavcopy_list.append(bhav)

        if not cols_printed:
            print(f"  Cols: {list(bhav.columns)}")
            cols_printed = True

        # EQ filter
        eq_mask = bhav["SERIES"].astype(str).str.strip() == "EQ" if "SERIES" in bhav.columns else pd.Series([True]*len(bhav), index=bhav.index)
        bhav_eq = bhav[eq_mask].copy()

        # ── Extract delivery from bhavcopy RSVD columns ───────────────────────
        # Only run full probe on first few days to find the method
        if del_method is None or i < 3:
            dd = extract_delivery(bhav_eq)
        else:
            # Fast path once method is known
            dd = extract_delivery_fast(bhav_eq, del_method)

        if dd:
            delivery_map[ds] = dd
            if del_method is None:
                del_method = "found"

        # ── Value ─────────────────────────────────────────────────────────────
        if "VALUE" in bhav_eq.columns and "SYMBOL" in bhav_eq.columns:
            v_num = pd.to_numeric(bhav_eq["VALUE"], errors="coerce")
            syms  = bhav_eq["SYMBOL"].astype(str).str.strip().str.upper()
            vd = {}
            for idx in bhav_eq.index:
                v = v_num[idx] if idx in v_num.index else np.nan
                if pd.notna(v) and v > 0:
                    vd[syms[idx]] = float(v)/100
            if vd: value_map[ds] = vd

        # ── Latest closes for MCap ────────────────────────────────────────────
        if i == 0 and "CLOSE" in bhav_eq.columns and "SYMBOL" in bhav_eq.columns:
            cl   = pd.to_numeric(bhav_eq["CLOSE"], errors="coerce")
            syms = bhav_eq["SYMBOL"].astype(str).str.strip().str.upper()
            for idx in bhav_eq.index:
                c = cl[idx] if idx in cl.index else np.nan
                if pd.notna(c) and c > 0:
                    latest_close[syms[idx]] = float(c)

        if (i+1) % 10 == 0:
            print(f"  {i+1}/{len(detail_dates)} bhav:{len(bhavcopy_list)} del:{len(delivery_map)} val:{len(value_map)}")

    except Exception as e:
        print(f"  Error {date}: {e}")

def extract_delivery_fast(bhav_eq, method):
    """Fast delivery extraction once method is known."""
    return extract_delivery(bhav_eq)

# ── MCap ──────────────────────────────────────────────────────────────────────
print(f"\nLoading MCap ({len(latest_close)} closes)...")
mcap_map   = fetch_mcap_from_equity_list(latest_close)
mcap_avail = len(mcap_map) > 0

# ── Phase 2: Extra close days for SMA150/200 ─────────────────────────────────
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

actual_days = len(bhavcopy_list)
del_avail   = len(delivery_map) > 0
val_avail   = len(value_map) > 0

print(f"\n{'='*40}")
print(f"Bhavcopy : {actual_days}")
print(f"Delivery : {len(delivery_map)} {'✓' if del_avail else '✗ SKIPPED'}")
print(f"Value    : {len(value_map)} {'✓' if val_avail else '✗ SKIPPED'}")
print(f"MCap     : {len(mcap_map)} {'✓' if mcap_avail else '✗ SKIPPED'}")
print(f"SMA days : {len(extra_list)+actual_days}")
if del_avail:
    s = list(delivery_map.keys())[0]
    print(f"  Del sample ({s}): {list(delivery_map[s].items())[:3]}")

if actual_days < 20:
    out = {"stocks":[],"count":0,"fetchedAt":datetime.now().isoformat(),"autoRun":True}
    c   = json.dumps(out)
    os.makedirs("results",exist_ok=True)
    with open("results/matrix181_results.json","w") as f: f.write(c)
    save_via_github_api(c); exit(0)

# ── Build dataframes ───────────────────────────────────────────────────────────
all_bhav = pd.concat(bhavcopy_list, ignore_index=True)
for col in ["CLOSE","VOLUME","HIGH","LOW"]:
    if col in all_bhav.columns:
        all_bhav[col] = pd.to_numeric(all_bhav[col], errors="coerce")
all_bhav["DATE"] = pd.to_datetime(all_bhav["DATE"], errors="coerce")
if "SERIES" in all_bhav.columns:
    all_bhav = all_bhav[all_bhav["SERIES"]=="EQ"]
all_bhav.sort_values(["SYMBOL","DATE"], inplace=True)

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
all_closes.sort_values(["SYMBOL","DATE"], inplace=True)

symbols = all_bhav["SYMBOL"].dropna().unique()
print(f"Total EQ : {len(symbols)}")

results   = []
s1_count  = 0
s2_count  = 0
processed = 0
sk = {k:0 for k in ["rows","close","mcap","s1","s2","gc"]}

for symbol in symbols:
    try:
        sdf   = all_bhav[all_bhav["SYMBOL"]==symbol].copy()
        sdf_c = all_closes[all_closes["SYMBOL"]==symbol].copy()
        processed += 1
        if processed % 500 == 0:
            print(f"  {processed}/{len(symbols)} q:{len(results)} s1:{s1_count} s2:{s2_count}")

        if len(sdf)<20: sk["rows"]+=1; continue
        closes  = sdf["CLOSE"].dropna()
        volumes = sdf["VOLUME"].dropna()
        if len(closes)<20: sk["close"]+=1; continue
        ltp = float(closes.iloc[-1])
        if ltp<=0: sk["close"]+=1; continue

        all_c     = sdf_c["CLOSE"].dropna()
        ltp       = round(ltp,2)
        prev      = float(closes.iloc[-2]) if len(closes)>=2 else ltp
        change    = round((ltp-prev)/prev*100,2) if prev else 0
        high_52w  = round(float(sdf["HIGH"].max()),2) if "HIGH" in sdf.columns else ltp
        low_52w   = round(float(sdf["LOW"].min()),2)  if "LOW"  in sdf.columns else ltp
        from_low_mult = round(ltp/low_52w,4)                 if low_52w  else None
        from_low_pct  = round((ltp-low_52w)/low_52w*100,2)   if low_52w  else None
        from_high_pct = round((high_52w-ltp)/high_52w*100,2) if high_52w else None
        vol_today = int(volumes.iloc[-1]) if len(volumes) else 0
        sym_u = str(symbol).strip().upper()

        mcap = mcap_map.get(sym_u)
        if mcap_avail and mcap is None:
            sk["mcap"]+=1; continue

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

        sma9   = calc_sma(closes,9)
        sma20  = calc_sma(closes,20)
        sma45  = calc_sma(closes,45)  if len(closes)>=45  else None
        sma50  = calc_sma(closes,50)  if len(closes)>=50  else None
        sma150 = calc_sma(all_c,150)  if len(all_c)>=150  else None
        sma200 = calc_sma(all_c,200)  if len(all_c)>=200  else None
        rsi    = calc_rsi(closes)

        v20 = float(volumes.tail(20).mean()) if len(volumes)>=20 else None
        v50 = float(volumes.tail(50).mean()) if len(volumes)>=50 else None
        vol_ratio = round(v20/v50,3) if (v20 and v50 and v50>0) else None

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

        strategy = None
        if check_s1(ltp,mcap,rsi,from_low_mult,avg_d20,avg_d50,
                    vol_ratio,avg_tv,sma45,sma150,del_avail,val_avail):
            strategy = "s1"
        elif check_s2(ltp,mcap,rsi,high_52w,avg_d20,avg_d50,
                      vol_ratio,avg_tv,sma20,sma50,sma200,del_avail,val_avail):
            strategy = "s2"

        if strategy is None:
            if mcap and mcap<=S1_MCAP_MAX: sk["s1"]+=1
            else: sk["s2"]+=1
            continue

        score = calc_score_s1(avg_d20,avg_d50,vol_ratio,rsi,from_low_pct,avg_tv) if strategy=="s1" \
                else calc_score_s2(avg_d20,avg_d50,vol_ratio,rsi,ltp,sma20,sma50,sma200,avg_tv)
        grade = grade_setup(score,strategy,avg_d20,avg_d50,vol_ratio,from_low_pct,del_avail)
        if grade=="C": sk["gc"]+=1; continue

        conf,sigs,clabel = calc_confidence(
            avg_d20,avg_d50,lat_del,vol_ratio,rsi,from_low_pct,
            score,grade,del_20,ltp,sma20,sma45,sma50,sma150,sma200,strategy)

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
            "sma45":               sma45,
            "sma50":               sma50,
            "sma150":              sma150,
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

print(f"\nResults  : {len(results)} (S1:{s1_count} S2:{s2_count})")
print(f"Skipped  : rows:{sk['rows']} close:{sk['close']} mcap:{sk['mcap']} s1:{sk['s1']} s2:{sk['s2']} gc:{sk['gc']}")
results.sort(key=lambda x: x["confidence"] or 0, reverse=True)

output = {
    "stocks": results, "count": len(results),
    "s1Count": s1_count, "s2Count": s2_count,
    "fetchedAt": datetime.now().isoformat(), "autoRun": True,
    "daysOfData": actual_days,
    "dataAvailability": {
        "mcap": mcap_avail, "delivery": del_avail,
        "value": val_avail, "sma200": len(extra_list)>0
    },
}
content = json.dumps(output)
os.makedirs("results",exist_ok=True)
with open("results/matrix181_results.json","w") as f: f.write(content)
print(f"\nSaved: {os.path.getsize('results/matrix181_results.json')} bytes ✓")
print(f"API  : {'SUCCESS' if save_via_github_api(content) else 'FAILED'}")
print("\n"+"="*60)
print(f"DONE — {len(results)} | S1:{s1_count} S2:{s2_count}")
print(f"A+:{sum(1 for r in results if r['grade']=='A+')} A:{sum(1 for r in results if r['grade']=='A')} B+:{sum(1 for r in results if r['grade']=='B+')} B:{sum(1 for r in results if r['grade']=='B')}")
print("="*60)
