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

MCAP_MIN_CR         = 1500
DELIVERY_20D_MIN    = 45.0
DELIVERY_TREND_GAP  = 5.0
VOL_RATIO_MIN       = 1.25
RSI_MIN             = 45.0
RSI_MAX             = 60.0
FROM_LOW_MIN        = 10.0
FROM_LOW_MAX        = 35.0
TRADED_VALUE_MIN_CR = 10.0

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
            print(f"  GitHub API save OK — {r.status_code}")
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
    urls = [
        "https://nsearchives.nseindia.com/content/equities/MCAP.csv",
        "https://www.nseindia.com/content/equities/MCAP.csv",
    ]
    for url in urls:
        try:
            r = SESSION.get(url, timeout=20)
            if r.status_code != 200:
                print(f"  MCAP HTTP {r.status_code}")
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
                                    mcap_col = c
                                    break
                            except:
                                pass
                    if sym_col and mcap_col:
                        df[mcap_col] = pd.to_numeric(df[mcap_col], errors="coerce")
                        result = {}
                        for _, row in df.iterrows():
                            sym = str(row[sym_col]).strip().upper()
                            cap = row[mcap_col]
                            if pd.notna(cap) and cap > 0:
                                result[sym] = round(float(cap), 2)
                        print(f"MCAP loaded: {len(result)} symbols")
                        return result
                except:
                    continue
        except Exception as e:
            print(f"  MCAP error: {e}")
    return {}

def normalise_bhav(df):
    """Normalise both old and new NSE bhavcopy formats."""
    df = df.copy()
    df.columns = df.columns.str.strip().str.upper()

    # Step 1: Rename known columns one at a time — no duplicate targets
    rename_map = {}

    # SYMBOL
    if "SYMBOL" not in df.columns:
        for c in ["TCKRSYMB", "FININSTRMID", "FININSTRMNNM"]:
            if c in df.columns:
                rename_map[c] = "SYMBOL"
                break

    # SERIES
    if "SERIES" not in df.columns:
        for c in ["SCTYSRS"]:
            if c in df.columns:
                rename_map[c] = "SERIES"
                break

    # CLOSE
    if "CLOSE" not in df.columns:
        for c in ["CLSPRIC"]:
            if c in df.columns:
                rename_map[c] = "CLOSE"
                break

    # HIGH
    if "HIGH" not in df.columns:
        for c in ["HGHPRIC"]:
            if c in df.columns:
                rename_map[c] = "HIGH"
                break

    # LOW
    if "LOW" not in df.columns:
        for c in ["LWPRIC"]:
            if c in df.columns:
                rename_map[c] = "LOW"
                break

    # VOLUME — pick first available
    if "VOLUME" not in df.columns:
        for c in ["TTLTRADGVOL", "TOTTRDQTY", "TTL_TRD_QNTY"]:
            if c in df.columns:
                rename_map[c] = "VOLUME"
                break

    # VALUE (traded value) — pick first available
    if "VALUE" not in df.columns:
        for c in ["TTLBOFTXSEXCTD", "TOTTRDVAL", "TTLTRDDVAL"]:
            if c in df.columns:
                rename_map[c] = "VALUE"
                break

    # DATE — only if DATE not already present
    if "DATE" not in df.columns:
        for c in ["BIZDT", "TRADDT", "TRDDT"]:
            if c in df.columns:
                rename_map[c] = "DATE"
                break

    df.rename(columns=rename_map, inplace=True)
    return df

def fetch_bhavcopy(date):
    d    = date.strftime("%d%b%Y").upper()
    urls = [
        f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{date.strftime('%Y%m%d')}_F_0000.csv.zip",
        f"https://www.nseindia.com/content/historical/EQUITIES/{date.year}/{date.strftime('%b').upper()}/cm{d}bhav.csv.zip",
    ]
    for u in urls:
        try:
            r = SESSION.get(u, timeout=20)
            if r.status_code == 200:
                z  = zipfile.ZipFile(io.BytesIO(r.content))
                df = pd.read_csv(z.open(z.namelist()[0]))
                df = normalise_bhav(df)
                if "SYMBOL" in df.columns:
                    return df
        except:
            pass
    return None

def fetch_delivery(date):
    d   = date.strftime("%d%m%Y")
    url = f"https://nsearchives.nseindia.com/archives/equities/deliveries/MTO_{d}.DAT"
    try:
        r = SESSION.get(url, timeout=20)
        if r.status_code == 200:
            rows = []
            for line in r.text.strip().split("\n"):
                if line.startswith("#") or not line.strip():
                    continue
                p = [x.strip() for x in line.split(",")]
                if len(p) >= 6:
                    rows.append({
                        "SYMBOL":          p[2],
                        "SERIES":          p[3],
                        "TRADED_QTY":      safe_float(p[4]),
                        "DELIVERABLE_QTY": safe_float(p[5]),
                    })
            if rows:
                df = pd.DataFrame(rows)
                df = df[df["SERIES"] == "EQ"]
                df["DELIVERY_PCT"] = (df["DELIVERABLE_QTY"] / df["TRADED_QTY"] * 100).round(2)
                return df[["SYMBOL", "DELIVERY_PCT"]]
    except Exception as e:
        print(f"  Delivery {date.strftime('%Y-%m-%d')}: {e}")
    return None

def fetch_one(date):
    ds = date.strftime("%Y-%m-%d")
    return ds, fetch_bhavcopy(date), fetch_delivery(date)

def safe_float(x):
    try:
        return float(str(x).replace(",", ""))
    except:
        return 0.0

def get_trading_dates(n=60):
    dates, d = [], datetime.now()
    while len(dates) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            dates.append(d)
    return dates

def calc_rsi(closes, period=14):
    if len(closes) < period + 2:
        return None
    delta = closes.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    rsi   = 100 - (100 / (1 + rs))
    val   = rsi.iloc[-1]
    return round(float(val), 1) if pd.notna(val) else None

def calc_sma(series, period):
    clean = series.dropna()
    if len(clean) < period:
        return None
    return round(float(clean.tail(period).mean()), 2)

def calc_avg(vals):
    vals = [v for v in vals if v is not None and not np.isnan(v)]
    return round(sum(vals) / len(vals), 2) if vals else None

def calc_score(avg_del_20, avg_del_50, vol_ratio, rsi,
               from_low, close, sma30, sma9, sma20, traded_value_cr):
    score = 0
    if avg_del_20 is not None and avg_del_20 >= 45:
        score += min((avg_del_20 - 45) / 35 * 20, 20)
    if avg_del_20 is not None and avg_del_50 is not None:
        gap = avg_del_20 - avg_del_50
        if gap > 5:
            score += min((gap - 5) / 15 * 20, 20)
    if vol_ratio is not None and vol_ratio >= 1.25:
        score += min((vol_ratio - 1.25) / 0.75 * 20, 20)
    if rsi is not None:
        score += max(0, 20 - abs(rsi - 52.5) * 1.2)
    if from_low is not None:
        if 15 <= from_low <= 25:   score += 15
        elif 10 <= from_low < 15:  score += 10
        elif 25 < from_low <= 35:  score += 8
    if close and sma30 and close > sma30:  score += 8
    if sma9 and sma20 and sma9 > sma20:   score += 7
    if traded_value_cr is not None and traded_value_cr >= 10:
        score += min((traded_value_cr - 10) / 40 * 10, 10)
    return min(int(round(score)), 100)

def grade_setup(score, rsi, avg_del_20, avg_del_50,
                vol_ratio, from_low, delivery_available):
    del_gap = 0
    if avg_del_20 is not None and avg_del_50 is not None:
        del_gap = avg_del_20 - avg_del_50
    if delivery_available:
        if score>=82 and avg_del_20 and avg_del_20>=60 and del_gap>=10 and vol_ratio>=1.5  and 15<=from_low<=25: return "A+"
        if score>=68 and avg_del_20 and avg_del_20>=52 and del_gap>=5  and vol_ratio>=1.35 and from_low<=30:     return "A"
    else:
        if score >= 82 and vol_ratio >= 1.5  and 15 <= from_low <= 25: return "A+"
        if score >= 68 and vol_ratio >= 1.35 and from_low <= 30:        return "A"
    if score >= 52 and from_low <= 32: return "B+"
    if score >= 38:                    return "B"
    return "C"

def calc_confidence(avg_del_20, avg_del_50, del_today, vol_ratio,
                    rsi, from_low, score, grade, del_vals,
                    close, sma30, sma9, sma20):
    sig = {}
    dq = 0
    if del_vals and len(del_vals) >= 3:
        dq = min(sum(1 for d in del_vals if d >= 50) / len(del_vals) * 15, 15)
        if del_today and avg_del_20 and del_today > avg_del_20:
            dq = min(dq + 3, 15)
    elif avg_del_20 and avg_del_20 >= 50:
        dq = 8
    sig["delivery_quality"] = round(dq, 1)

    dt = 0
    if avg_del_20 and avg_del_50 and avg_del_20 > avg_del_50:
        dt = min((avg_del_20 - avg_del_50) / 10 * 15, 15)
    sig["delivery_trend"] = round(dt, 1)

    vc = 0
    if vol_ratio:
        if vol_ratio >= 2.0:    vc = 20
        elif vol_ratio >= 1.75: vc = 16
        elif vol_ratio >= 1.5:  vc = 12
        elif vol_ratio >= 1.25: vc = 7
    sig["volume_expansion"] = round(vc, 1)

    rq = 0
    if rsi:
        rq = 15 if 50<=rsi<=57 else 12 if 47<=rsi<=60 else 8 if 45<=rsi<=60 else 0
    sig["rsi_zone"] = round(rq, 1)

    rr = 0
    if from_low is not None:
        if 15<=from_low<=25:   rr = 15
        elif 10<=from_low<15:  rr = 11
        elif 25<from_low<=30:  rr = 9
        elif 30<from_low<=35:  rr = 6
    sig["price_position"] = round(rr, 1)

    ta = 0
    if close and sma30 and close > sma30: ta += 5
    if sma9 and sma20 and sma9 > sma20:  ta += 5
    sig["trend_alignment"] = round(ta, 1)

    gb = {"A+": 10, "A": 8, "B+": 5, "B": 2}.get(grade, 0)
    sig["grade_bonus"] = gb

    total = min(int(round(dq + dt + vc + rq + rr + ta + gb)), 100)
    label = ("VERY HIGH" if total>=85 else "HIGH" if total>=70 else
             "MODERATE"  if total>=55 else "LOW"  if total>=40 else "VERY LOW")
    return total, sig, label


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

print("=" * 60)
print("MATRIX 18.1 — PRUDENCE-X TITAN — DAILY SCAN")
print(f"Started  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print(f"PAT_TOKEN: {'SET' if GITHUB_TOKEN else 'NOT SET'}")
print("=" * 60)

get_session()

print("\nLoading MCap...")
mcap_map = fetch_mcap_map()

trading_dates = get_trading_dates(60)
print(f"\nFetching {len(trading_dates)} trading days...")

bhavcopy_list = []
delivery_map  = {}
value_map     = {}
completed     = 0
cols_printed  = False

with ThreadPoolExecutor(max_workers=6) as ex:
    futures = {ex.submit(fetch_one, d): d for d in trading_dates}
    for future in as_completed(futures):
        try:
            ds, bhav, deliv = future.result()
            if bhav is not None:
                bhav["DATE"] = ds  # Always set DATE from the date string — avoids duplicate key issue
                bhavcopy_list.append(bhav)

                if not cols_printed:
                    print(f"  Cols: {list(bhav.columns)}")
                    cols_printed = True

                # Traded value
                val_col = next((c for c in ["VALUE"] if c in bhav.columns), None)
                if val_col and "SYMBOL" in bhav.columns:
                    bhav_v = bhav[["SYMBOL", val_col]].copy()
                    bhav_v[val_col] = pd.to_numeric(bhav_v[val_col], errors="coerce")
                    vd = {}
                    for _, row in bhav_v.iterrows():
                        sym = str(row["SYMBOL"]).strip().upper()
                        val = row[val_col]
                        if pd.notna(val) and val > 0:
                            vd[sym] = float(val) / 100  # lakhs to crores
                    if vd:
                        value_map[ds] = vd

            if deliv is not None:
                try:
                    delivery_map[ds] = dict(zip(deliv["SYMBOL"], deliv["DELIVERY_PCT"]))
                except:
                    pass
            completed += 1
            if completed % 10 == 0:
                print(f"  {completed}/{len(trading_dates)} bhav:{len(bhavcopy_list)} deliv:{len(delivery_map)} val:{len(value_map)}")
        except Exception as e:
            completed += 1
            print(f"  fetch error: {e}")

actual_days               = len(bhavcopy_list)
delivery_available_global = len(delivery_map) > 0
value_available_global    = len(value_map) > 0

print(f"\nBhavcopy : {actual_days}")
print(f"Delivery : {len(delivery_map)} {'✓' if delivery_available_global else '✗ skipped'}")
print(f"Value    : {len(value_map)} {'✓' if value_available_global else '✗ skipped'}")
print(f"MCap     : {len(mcap_map)} {'✓' if mcap_map else '✗ skipped'}")

if actual_days < 20:
    out = {"stocks":[],"count":0,"fetchedAt":datetime.now().isoformat(),"autoRun":True,"daysOfData":actual_days}
    c   = json.dumps(out)
    os.makedirs("results", exist_ok=True)
    with open("results/matrix181_results.json","w") as f: f.write(c)
    save_via_github_api(c)
    exit(0)

# ── Build dataframe ────────────────────────────────────────────────────────────
all_bhav = pd.concat(bhavcopy_list, ignore_index=True)

if "SYMBOL" not in all_bhav.columns:
    print("FATAL: SYMBOL missing")
    exit(1)

for col in ["CLOSE","VOLUME","HIGH","LOW"]:
    if col in all_bhav.columns:
        all_bhav[col] = pd.to_numeric(all_bhav[col], errors="coerce")

# DATE is already set as date string — convert to datetime
all_bhav["DATE"] = pd.to_datetime(all_bhav["DATE"], errors="coerce")

if "SERIES" in all_bhav.columns:
    all_bhav = all_bhav[all_bhav["SERIES"] == "EQ"]

all_bhav.sort_values(["SYMBOL","DATE"], inplace=True)
symbols = all_bhav["SYMBOL"].dropna().unique()
print(f"Total EQ : {len(symbols)}")

# ── Grade every symbol ─────────────────────────────────────────────────────────
results   = []
processed = 0
skipped   = {k:0 for k in ["rows","close","mcap","val","rsi_calc","rsi_range","52w","trend","vol","del","grade_c"]}

for symbol in symbols:
    try:
        sdf = all_bhav[all_bhav["SYMBOL"] == symbol].copy()
        processed += 1
        if processed % 500 == 0:
            print(f"  {processed}/{len(symbols)} qualifying:{len(results)}")

        if len(sdf) < 20:
            skipped["rows"] += 1; continue

        closes  = sdf["CLOSE"].dropna()
        volumes = sdf["VOLUME"].dropna()

        if len(closes) < 20:
            skipped["close"] += 1; continue

        ltp = float(closes.iloc[-1])
        if ltp <= 0:
            skipped["close"] += 1; continue

        ltp       = round(ltp, 2)
        prev      = float(closes.iloc[-2]) if len(closes) >= 2 else ltp
        change    = round((ltp-prev)/prev*100, 2) if prev else 0
        high_52w  = round(float(sdf["HIGH"].max()), 2) if "HIGH" in sdf.columns else ltp
        low_52w   = round(float(sdf["LOW"].min()),  2) if "LOW"  in sdf.columns else ltp
        from_low  = round((ltp-low_52w)/low_52w*100,   2) if low_52w  else None
        from_high = round((high_52w-ltp)/high_52w*100, 2) if high_52w else None
        vol_today = int(volumes.iloc[-1]) if len(volumes) else 0
        sym_upper = str(symbol).strip().upper()

        # F1: MCap (soft)
        mcap = mcap_map.get(sym_upper)
        if mcap_map and (mcap is None or mcap < MCAP_MIN_CR):
            skipped["mcap"] += 1; continue

        # F2: Traded value (soft)
        avg_traded_value = None
        sorted_dates = sorted(sdf["DATE"].dropna().tolist())
        if value_available_global:
            tv_list = []
            for dr in sorted_dates[-20:]:
                try:
                    ds2 = pd.Timestamp(dr).strftime("%Y-%m-%d")
                    tv  = value_map.get(ds2, {}).get(sym_upper)
                    if tv: tv_list.append(float(tv))
                except: pass
            avg_traded_value = calc_avg(tv_list)
            if avg_traded_value is not None and avg_traded_value < TRADED_VALUE_MIN_CR:
                skipped["val"] += 1; continue

        # F3: RSI (hard)
        rsi = calc_rsi(closes)
        if rsi is None:
            skipped["rsi_calc"] += 1; continue
        if not (RSI_MIN <= rsi <= RSI_MAX):
            skipped["rsi_range"] += 1; continue

        # F4: 52W range (hard)
        if from_low is None or not (FROM_LOW_MIN <= from_low <= FROM_LOW_MAX):
            skipped["52w"] += 1; continue

        # F5: Trend (hard)
        sma9  = calc_sma(closes, 9)
        sma20 = calc_sma(closes, 20)
        sma30 = calc_sma(closes, 30)
        sma50 = calc_sma(closes, 50) if len(closes) >= 50 else None
        if sma30 is None or ltp <= sma30:
            skipped["trend"] += 1; continue

        # F6: Volume ratio (hard)
        v20 = float(volumes.tail(20).mean()) if len(volumes) >= 20 else None
        v50 = float(volumes.tail(50).mean()) if len(volumes) >= 50 else None
        vol_ratio = round(v20/v50, 3) if (v20 and v50 and v50 > 0) else None
        if vol_ratio is None or vol_ratio < VOL_RATIO_MIN:
            skipped["vol"] += 1; continue

        # F7: Delivery (soft)
        del_all = []
        if delivery_available_global:
            for dr in sorted_dates[-60:]:
                try:
                    ds2 = pd.Timestamp(dr).strftime("%Y-%m-%d")
                    dp  = delivery_map.get(ds2, {}).get(str(symbol))
                    if dp: del_all.append(float(dp))
                except: pass

        del_20 = del_all[-20:] if len(del_all) >= 20 else del_all
        del_50 = del_all[-50:] if len(del_all) >= 50 else del_all
        avg_d20 = calc_avg(del_20)
        avg_d50 = calc_avg(del_50)
        lat_del = del_20[-1] if del_20 else None

        if delivery_available_global:
            if avg_d20 is not None and avg_d20 < DELIVERY_20D_MIN:
                skipped["del"] += 1; continue
            if avg_d20 is not None and avg_d50 is not None and (avg_d20 - avg_d50) < DELIVERY_TREND_GAP:
                skipped["del"] += 1; continue

        # Score & grade
        score = calc_score(avg_d20, avg_d50, vol_ratio, rsi,
                           from_low, ltp, sma30, sma9, sma20, avg_traded_value)
        grade = grade_setup(score, rsi, avg_d20, avg_d50,
                            vol_ratio, from_low, delivery_available_global)
        if grade == "C":
            skipped["grade_c"] += 1; continue

        conf, sigs, clabel = calc_confidence(
            avg_d20, avg_d50, lat_del, vol_ratio,
            rsi, from_low, score, grade, del_20,
            ltp, sma30, sma9, sma20
        )

        results.append({
            "symbol":              str(symbol),
            "ltp":                 ltp,
            "change":              change,
            "high52w":             high_52w,
            "low52w":              low_52w,
            "fromLow":             from_low,
            "fromHigh":            from_high,
            "volume":              vol_today,
            "mcap":                round(mcap, 0) if mcap else None,
            "tradedValueCr":       round(avg_traded_value, 2) if avg_traded_value else None,
            "rsi":                 rsi,
            "sma9":                sma9,
            "sma20":               sma20,
            "sma30":               sma30,
            "sma50":               sma50,
            "volRatio":            vol_ratio,
            "avgDelivery":         avg_d20,
            "avgDelivery50":       avg_d50,
            "deliveryTrend":       round(avg_d20-avg_d50, 2) if avg_d20 and avg_d50 else None,
            "deliveryToday":       lat_del,
            "score":               score,
            "grade":               grade,
            "daysOfData":          len(sdf),
            "confidence":          conf,
            "confidenceLabel":     clabel,
            "confidenceBreakdown": sigs,
        })

    except Exception as e:
        print(f"  Error {symbol}: {e}")

print(f"\nSkip summary:")
print(f"  rows     : {skipped['rows']}")
print(f"  close    : {skipped['close']}")
print(f"  mcap     : {skipped['mcap']} {'(active)' if mcap_map else '(skipped)'}")
print(f"  value    : {skipped['val']} {'(active)' if value_available_global else '(skipped)'}")
print(f"  rsi_calc : {skipped['rsi_calc']}")
print(f"  rsi_range: {skipped['rsi_range']}")
print(f"  52w      : {skipped['52w']}")
print(f"  trend    : {skipped['trend']}")
print(f"  volume   : {skipped['vol']}")
print(f"  delivery : {skipped['del']} {'(active)' if delivery_available_global else '(skipped)'}")
print(f"  grade_c  : {skipped['grade_c']}")
print(f"  QUALIFY  : {len(results)}")

results.sort(key=lambda x: x["confidence"] or 0, reverse=True)

output = {
    "stocks":    results,
    "count":     len(results),
    "fetchedAt": datetime.now().isoformat(),
    "autoRun":   True,
    "daysOfData": actual_days,
    "dataAvailability": {
        "mcap": len(mcap_map)>0, "delivery": delivery_available_global, "value": value_available_global
    },
}
content = json.dumps(output)
os.makedirs("results", exist_ok=True)
with open("results/matrix181_results.json","w") as f: f.write(content)
print(f"\nSaved: {os.path.getsize('results/matrix181_results.json')} bytes ✓")
print(f"API  : {'SUCCESS' if save_via_github_api(content) else 'FAILED'}")

print("\n" + "="*60)
print(f"DONE — {len(results)} stocks")
print(f"A+:{sum(1 for r in results if r['grade']=='A+')} A:{sum(1 for r in results if r['grade']=='A')} B+:{sum(1 for r in results if r['grade']=='B+')} B:{sum(1 for r in results if r['grade']=='B')}")
print(f"VH:{sum(1 for r in results if r['confidenceLabel']=='VERY HIGH')} H:{sum(1 for r in results if r['confidenceLabel']=='HIGH')}")
print("="*60)
