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

# ── FILTER CONSTANTS ──────────────────────────────────────────────────────────
MCAP_MIN_CR         = 1500    # Market cap minimum in crores
DELIVERY_20D_MIN    = 45.0    # 20D avg delivery minimum %
DELIVERY_TREND_GAP  = 5.0     # 20D must exceed 50D by this many %
VOL_RATIO_MIN       = 1.25    # 20D avg volume / 50D avg volume
RSI_MIN             = 45.0    # RSI lower bound
RSI_MAX             = 60.0    # RSI upper bound
FROM_LOW_MIN        = 10.0    # Min distance from 52W low %
FROM_LOW_MAX        = 35.0    # Max distance from 52W low %
TRADED_VALUE_MIN_CR = 10.0    # 20D avg traded value in crores

def save_via_github_api(content_str):
    if not GITHUB_TOKEN:
        print("PAT_TOKEN not set — skipping API save")
        return False
    api_url = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{RESULTS_PATH}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    sha = None
    try:
        r = requests.get(api_url, headers=headers, timeout=15)
        if r.status_code == 200:
            sha = r.json().get("sha")
            print(f"  Existing SHA: {sha[:8]}")
        elif r.status_code == 404:
            print("  File does not exist — will create")
    except Exception as e:
        print(f"  SHA fetch error: {e}")
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
        else:
            print(f"  GitHub API FAILED — {r.status_code}: {r.text[:200]}")
            return False
    except Exception as e:
        print(f"  GitHub API error: {e}")
        return False

def get_session():
    try:
        SESSION.get("https://www.nseindia.com", timeout=10)
        print("NSE session ready")
    except Exception as e:
        print(f"Session warmup: {e}")

# ── MARKET CAP from NSE MCAP.csv ──────────────────────────────────────────────

def fetch_mcap_map():
    urls = [
        "https://nsearchives.nseindia.com/content/equities/MCAP.csv",
        "https://www.nseindia.com/content/equities/MCAP.csv",
    ]
    for url in urls:
        try:
            r = SESSION.get(url, timeout=20)
            if r.status_code == 200:
                from io import StringIO
                # Try reading with different encodings
                for enc in ["utf-8", "latin-1", "cp1252"]:
                    try:
                        df = pd.read_csv(StringIO(r.content.decode(enc)))
                        df.columns = df.columns.str.strip().str.upper()
                        print(f"  MCAP columns: {list(df.columns)}")

                        # Find symbol column
                        sym_col = None
                        for c in df.columns:
                            if "SYMBOL" in c or "NAME" in c:
                                sym_col = c
                                break

                        # Find mcap column — look for numeric column with large values
                        mcap_col = None
                        for c in df.columns:
                            if any(k in c for k in ["MARKET","MCAP","CAP","CAPITALISATION","CAPITALIZATION"]):
                                mcap_col = c
                                break

                        if not mcap_col:
                            # Try finding by position — mcap is usually last numeric column
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
                            mcap_map = {}
                            for _, row in df.iterrows():
                                sym = str(row[sym_col]).strip().upper()
                                cap = row[mcap_col]
                                if pd.notna(cap) and cap > 0:
                                    mcap_map[sym] = round(float(cap), 2)
                            print(f"MCAP loaded: {len(mcap_map)} symbols from {url}")
                            return mcap_map
                        else:
                            print(f"  Could not identify sym_col={sym_col} mcap_col={mcap_col}")
                    except Exception as e:
                        continue
            else:
                print(f"  MCAP HTTP {r.status_code} from {url}")
        except Exception as e:
            print(f"  MCAP error from {url}: {e}")
    print("MCAP unavailable — MCap filter will be skipped")
    return {}

def normalise_bhav(df):
    df.columns = df.columns.str.strip().str.upper()
    rename = {
        "TCKRSYMB":       "SYMBOL",
        "SCTYSRS":        "SERIES",
        "CLSPRIC":        "CLOSE",
        "HGHPRIC":        "HIGH",
        "LWPRIC":         "LOW",
        "TTLTRADGVOL":    "VOLUME",
        "TRADDT":         "DATE",
        "OPNPRIC":        "OPEN",
        "LASTPRIC":       "LAST",
        "TOTTRDQTY":      "VOLUME",
        "TTL_TRD_QNTY":   "VOLUME",
        "TOTTRDVAL":      "VALUE",
        "TTLTRDDVAL":     "VALUE",
        "TOTALTRADES":    "TRADES",
        "NO_OF_TRADES":   "TRADES",
    }
    df.rename(columns={k: v for k, v in rename.items() if k in df.columns}, inplace=True)

    # Debug first bhavcopy columns
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
        except Exception as e:
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
                df["DELIVERY_PCT"] = (
                    df["DELIVERABLE_QTY"] / df["TRADED_QTY"] * 100
                ).round(2)
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

def get_trading_dates(n=55):
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

    # Delivery quality (20 pts) — optional
    if avg_del_20 is not None and avg_del_20 >= 45:
        score += min((avg_del_20 - 45) / 35 * 20, 20)

    # Delivery trend gap (20 pts) — optional
    if avg_del_20 is not None and avg_del_50 is not None:
        gap = avg_del_20 - avg_del_50
        if gap > 5:
            score += min((gap - 5) / 15 * 20, 20)

    # Volume expansion (20 pts) — required
    if vol_ratio is not None and vol_ratio >= 1.25:
        score += min((vol_ratio - 1.25) / 0.75 * 20, 20)

    # RSI zone (20 pts) — required
    if rsi is not None:
        score += max(0, 20 - abs(rsi - 52.5) * 1.2)

    # 52W distance sweet spot (15 pts) — required
    if from_low is not None:
        if 15 <= from_low <= 25:   score += 15
        elif 10 <= from_low < 15:  score += 10
        elif 25 < from_low <= 35:  score += 8

    # Trend alignment (15 pts) — required
    if close and sma30 and close > sma30:   score += 8
    if sma9 and sma20 and sma9 > sma20:    score += 7

    # Traded value bonus (10 pts) — optional
    if traded_value_cr is not None and traded_value_cr >= 10:
        score += min((traded_value_cr - 10) / 40 * 10, 10)

    return min(int(round(score)), 100)

def grade_setup(score, rsi, avg_del_20, avg_del_50,
                vol_ratio, from_low, close, sma30, sma9, sma20,
                delivery_available):

    del_gap = 0
    if avg_del_20 is not None and avg_del_50 is not None:
        del_gap = avg_del_20 - avg_del_50

    # A+ — best of everything
    if delivery_available:
        if (score >= 82 and avg_del_20 is not None and avg_del_20 >= 60
                and del_gap >= 10 and vol_ratio >= 1.5
                and 15 <= from_low <= 25):
            return "A+"
    else:
        if score >= 82 and vol_ratio >= 1.5 and 15 <= from_low <= 25:
            return "A+"

    # A
    if delivery_available:
        if (score >= 68 and avg_del_20 is not None and avg_del_20 >= 52
                and del_gap >= 5 and vol_ratio >= 1.35 and from_low <= 30):
            return "A"
    else:
        if score >= 68 and vol_ratio >= 1.35 and from_low <= 30:
            return "A"

    # B+
    if score >= 52 and from_low <= 32:
        return "B+"

    # B
    if score >= 38:
        return "B"

    return "C"

def calc_confidence(avg_del_20, avg_del_50, del_today, vol_ratio,
                    rsi, from_low, from_high, score, grade,
                    del_vals, close, sma30, sma9, sma20, traded_value_cr):
    sig = {}

    # Delivery quality (15 pts)
    dq = 0
    if del_vals and len(del_vals) >= 3:
        high_del = sum(1 for d in del_vals if d >= 50)
        dq = min(high_del / len(del_vals) * 15, 15)
        if del_today and avg_del_20 and del_today > avg_del_20:
            dq = min(dq + 3, 15)
    elif avg_del_20 and avg_del_20 >= 50:
        dq = 8
    sig["delivery_quality"] = round(dq, 1)

    # Delivery trend (15 pts)
    dt = 0
    if avg_del_20 and avg_del_50 and avg_del_20 > avg_del_50:
        gap = avg_del_20 - avg_del_50
        dt  = min(gap / 10 * 15, 15)
    sig["delivery_trend"] = round(dt, 1)

    # Volume expansion (20 pts)
    vc = 0
    if vol_ratio:
        if vol_ratio >= 2.0:    vc = 20
        elif vol_ratio >= 1.75: vc = 16
        elif vol_ratio >= 1.5:  vc = 12
        elif vol_ratio >= 1.25: vc = 7
    sig["volume_expansion"] = round(vc, 1)

    # RSI zone (15 pts)
    rq = 0
    if rsi:
        rq = 15 if 50<=rsi<=57 else 12 if 47<=rsi<=60 else 8 if 45<=rsi<=60 else 0
    sig["rsi_zone"] = round(rq, 1)

    # 52W position (15 pts)
    rr = 0
    if from_low is not None:
        if 15 <= from_low <= 25:  rr = 15
        elif 10 <= from_low < 15: rr = 11
        elif 25 < from_low <= 30: rr = 9
        elif 30 < from_low <= 35: rr = 6
    sig["price_position"] = round(rr, 1)

    # Trend alignment (10 pts)
    ta = 0
    if close and sma30 and close > sma30: ta += 5
    if sma9 and sma20 and sma9 > sma20:  ta += 5
    sig["trend_alignment"] = round(ta, 1)

    # Grade bonus (10 pts)
    gb = {"A+": 10, "A": 8, "B+": 5, "B": 2}.get(grade, 0)
    sig["grade_bonus"] = gb

    total = min(int(round(dq + dt + vc + rq + rr + ta + gb)), 100)
    label = ("VERY HIGH" if total >= 85 else "HIGH"    if total >= 70 else
             "MODERATE"  if total >= 55 else "LOW"     if total >= 40 else "VERY LOW")
    return total, sig, label


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

print("=" * 60)
print("MATRIX 18.1 — PRUDENCE-X TITAN — DAILY SCAN")
print(f"Started  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print(f"PAT_TOKEN: {'SET' if GITHUB_TOKEN else 'NOT SET'}")
print()
print("FILTERS (applied only when data available):")
print(f"  MCap           > {MCAP_MIN_CR} Cr       [skipped if unavailable]")
print(f"  Traded Value   > {TRADED_VALUE_MIN_CR} Cr        [skipped if unavailable]")
print(f"  Delivery 20D   >= {DELIVERY_20D_MIN}%      [skipped if unavailable]")
print(f"  Delivery trend > 50D + {DELIVERY_TREND_GAP}%  [skipped if unavailable]")
print(f"  Volume ratio   > {VOL_RATIO_MIN}x        [HARD filter]")
print(f"  RSI            {RSI_MIN} - {RSI_MAX}    [HARD filter]")
print(f"  Close          > 30 DMA      [HARD filter]")
print(f"  52W Low dist   {FROM_LOW_MIN}% - {FROM_LOW_MAX}%  [HARD filter]")
print("=" * 60)

get_session()

# ── Load MCap ─────────────────────────────────────────────────────────────────
print("\nLoading NSE market cap data...")
mcap_map = fetch_mcap_map()

# ── Fetch bhavcopy + delivery ─────────────────────────────────────────────────
trading_dates = get_trading_dates(55)
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
                bhav["DATE"] = ds
                bhavcopy_list.append(bhav)

                # Print columns once for debugging
                if not cols_printed:
                    print(f"  Bhavcopy columns: {list(bhav.columns)}")
                    cols_printed = True

                # Build traded value map
                # Try multiple possible column names for traded value
                val_col = None
                for vc in ["VALUE", "TOTTRDVAL", "TTLTRDDVAL", "TRADED_VALUE"]:
                    if vc in bhav.columns:
                        val_col = vc
                        break

                if val_col and "SYMBOL" in bhav.columns:
                    bhav[val_col] = pd.to_numeric(bhav[val_col], errors="coerce")
                    val_dict = {}
                    for _, row in bhav[["SYMBOL", val_col]].iterrows():
                        sym = str(row["SYMBOL"]).strip().upper()
                        val = row[val_col]
                        if pd.notna(val) and val > 0:
                            # NSE value is in lakhs — convert to crores
                            val_dict[sym] = float(val) / 100
                    if val_dict:
                        value_map[ds] = val_dict

            if deliv is not None:
                try:
                    delivery_map[ds] = dict(zip(deliv["SYMBOL"], deliv["DELIVERY_PCT"]))
                except:
                    pass
            completed += 1
            if completed % 10 == 0:
                print(f"  {completed}/{len(trading_dates)} — bhav:{len(bhavcopy_list)} deliv:{len(delivery_map)} val:{len(value_map)}")
        except Exception as e:
            completed += 1

actual_days = len(bhavcopy_list)
delivery_available_global = len(delivery_map) > 0
value_available_global    = len(value_map) > 0

print(f"\nBhavcopy days    : {actual_days}")
print(f"Delivery days    : {len(delivery_map)} {'✓' if delivery_available_global else '✗ — delivery filter SKIPPED'}")
print(f"Value days       : {len(value_map)} {'✓' if value_available_global else '✗ — traded value filter SKIPPED'}")
print(f"MCap symbols     : {len(mcap_map)} {'✓' if mcap_map else '✗ — MCap filter SKIPPED'}")

if actual_days < 20:
    output  = {"stocks": [], "count": 0, "fetchedAt": datetime.now().isoformat(),
               "autoRun": True, "daysOfData": actual_days}
    content = json.dumps(output)
    os.makedirs("results", exist_ok=True)
    with open("results/matrix181_results.json", "w") as f:
        f.write(content)
    save_via_github_api(content)
    print("Not enough bhavcopy data")
    exit(0)

# ── Build dataframe ────────────────────────────────────────────────────────────
all_bhav = pd.concat(bhavcopy_list, ignore_index=True)

if "SYMBOL" not in all_bhav.columns:
    print("FATAL: SYMBOL column missing")
    exit(1)

for col in ["CLOSE", "VOLUME", "HIGH", "LOW"]:
    if col in all_bhav.columns:
        all_bhav[col] = pd.to_numeric(all_bhav[col], errors="coerce")

if "DATE" in all_bhav.columns:
    all_bhav["DATE"] = pd.to_datetime(all_bhav["DATE"], errors="coerce")

if "SERIES" in all_bhav.columns:
    all_bhav = all_bhav[all_bhav["SERIES"] == "EQ"]

all_bhav.sort_values(["SYMBOL", "DATE"], inplace=True)
symbols = all_bhav["SYMBOL"].dropna().unique()
print(f"Total EQ symbols : {len(symbols)}")

# ── Grade every symbol ─────────────────────────────────────────────────────────
results   = []
processed = 0
skipped   = {
    "rows": 0, "close": 0, "mcap": 0, "traded_value": 0,
    "rsi_calc": 0, "rsi_range": 0, "52w": 0, "trend": 0,
    "volume": 0, "delivery": 0, "grade_c": 0
}

for symbol in symbols:
    try:
        sdf = all_bhav[all_bhav["SYMBOL"] == symbol].copy()
        processed += 1
        if processed % 500 == 0:
            print(f"  {processed}/{len(symbols)} — qualifying: {len(results)}")

        if len(sdf) < 20:
            skipped["rows"] += 1
            continue

        closes  = sdf["CLOSE"].dropna()  if "CLOSE"  in sdf.columns else pd.Series([], dtype=float)
        volumes = sdf["VOLUME"].dropna() if "VOLUME" in sdf.columns else pd.Series([], dtype=float)

        if len(closes) < 20:
            skipped["close"] += 1
            continue

        ltp = float(closes.iloc[-1])
        if ltp <= 0:
            skipped["close"] += 1
            continue

        ltp       = round(ltp, 2)
        prev      = float(closes.iloc[-2]) if len(closes) >= 2 else ltp
        change    = round((ltp - prev) / prev * 100, 2) if prev else 0
        high_52w  = round(float(sdf["HIGH"].max()), 2) if "HIGH" in sdf.columns else ltp
        low_52w   = round(float(sdf["LOW"].min()),  2) if "LOW"  in sdf.columns else ltp
        from_low  = round((ltp - low_52w)  / low_52w  * 100, 2) if low_52w  else None
        from_high = round((high_52w - ltp) / high_52w * 100, 2) if high_52w else None
        vol_today = int(volumes.iloc[-1]) if len(volumes) else 0
        sym_upper = str(symbol).strip().upper()

        # ── FILTER 1: Market Cap (skip if data unavailable) ───────────────────
        mcap = mcap_map.get(sym_upper)
        if mcap_map and (mcap is None or mcap < MCAP_MIN_CR):
            skipped["mcap"] += 1
            continue

        # ── FILTER 2: Traded Value (skip filter if data unavailable) ─────────
        traded_vals = []
        sorted_dates = sorted(sdf["DATE"].dropna().tolist()) if "DATE" in sdf.columns else []
        if value_available_global:
            for dr in sorted_dates[-20:]:
                try:
                    ds2 = pd.Timestamp(dr).strftime("%Y-%m-%d")
                    tv  = value_map.get(ds2, {}).get(sym_upper)
                    if tv is not None:
                        traded_vals.append(float(tv))
                except:
                    pass
            avg_traded_value = calc_avg(traded_vals)
            if avg_traded_value is not None and avg_traded_value < TRADED_VALUE_MIN_CR:
                skipped["traded_value"] += 1
                continue
        else:
            avg_traded_value = None  # not available — skip this filter

        # ── FILTER 3: RSI 45-60 (HARD) ───────────────────────────────────────
        rsi = calc_rsi(closes)
        if rsi is None:
            skipped["rsi_calc"] += 1
            continue
        if not (RSI_MIN <= rsi <= RSI_MAX):
            skipped["rsi_range"] += 1
            continue

        # ── FILTER 4: 52W distance 10%-35% (HARD) ────────────────────────────
        if from_low is None or not (FROM_LOW_MIN <= from_low <= FROM_LOW_MAX):
            skipped["52w"] += 1
            continue

        # ── FILTER 5: Close > 30 DMA (HARD) ──────────────────────────────────
        sma9  = calc_sma(closes, 9)
        sma20 = calc_sma(closes, 20)
        sma30 = calc_sma(closes, 30)
        sma50 = calc_sma(closes, 50) if len(closes) >= 50 else None

        if sma30 is None or ltp <= sma30:
            skipped["trend"] += 1
            continue

        # ── FILTER 6: Volume 20D > 1.25x 50D (HARD) ──────────────────────────
        vol_20    = float(volumes.tail(20).mean()) if len(volumes) >= 20 else None
        vol_50    = float(volumes.tail(50).mean()) if len(volumes) >= 50 else None
        vol_ratio = round(vol_20 / vol_50, 3) if (vol_20 and vol_50 and vol_50 > 0) else None

        if vol_ratio is None or vol_ratio < VOL_RATIO_MIN:
            skipped["volume"] += 1
            continue

        # ── FILTER 7: Delivery (skip filter if data unavailable) ─────────────
        del_vals_all = []
        if delivery_available_global:
            for dr in sorted_dates[-55:]:
                try:
                    ds2 = pd.Timestamp(dr).strftime("%Y-%m-%d")
                    dp  = delivery_map.get(ds2, {}).get(str(symbol))
                    if dp is not None:
                        del_vals_all.append(float(dp))
                except:
                    pass

        del_vals_20 = del_vals_all[-20:] if len(del_vals_all) >= 20 else del_vals_all
        del_vals_50 = del_vals_all[-50:] if len(del_vals_all) >= 50 else del_vals_all

        avg_del_20 = calc_avg(del_vals_20)
        avg_del_50 = calc_avg(del_vals_50)
        latest_del = del_vals_20[-1] if del_vals_20 else None

        if delivery_available_global:
            # Only apply delivery filters if data exists
            if avg_del_20 is not None and avg_del_20 < DELIVERY_20D_MIN:
                skipped["delivery"] += 1
                continue
            if (avg_del_20 is not None and avg_del_50 is not None
                    and (avg_del_20 - avg_del_50) < DELIVERY_TREND_GAP):
                skipped["delivery"] += 1
                continue

        # ── Score & Grade ──────────────────────────────────────────────────────
        score = calc_score(avg_del_20, avg_del_50, vol_ratio, rsi,
                           from_low, ltp, sma30, sma9, sma20, avg_traded_value)
        grade = grade_setup(score, rsi, avg_del_20, avg_del_50,
                            vol_ratio, from_low, ltp, sma30, sma9, sma20,
                            delivery_available_global)

        if grade == "C":
            skipped["grade_c"] += 1
            continue

        conf, sigs, clabel = calc_confidence(
            avg_del_20, avg_del_50, latest_del, vol_ratio,
            rsi, from_low, from_high, score, grade,
            del_vals_20, ltp, sma30, sma9, sma20, avg_traded_value
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
            "avgDelivery":         avg_del_20,
            "avgDelivery50":       avg_del_50,
            "deliveryTrend":       round(avg_del_20 - avg_del_50, 2) if avg_del_20 and avg_del_50 else None,
            "deliveryToday":       latest_del,
            "score":               score,
            "grade":               grade,
            "daysOfData":          len(sdf),
            "confidence":          conf,
            "confidenceLabel":     clabel,
            "confidenceBreakdown": sigs,
        })

    except Exception as e:
        print(f"  Error {symbol}: {e}")
        continue

print(f"\nSkip summary:")
print(f"  Too few rows      : {skipped['rows']}")
print(f"  No close/zero     : {skipped['close']}")
print(f"  MCap < {MCAP_MIN_CR}Cr      : {skipped['mcap']}  {'(filter active)' if mcap_map else '(filter skipped)'}")
print(f"  Value < {TRADED_VALUE_MIN_CR}Cr       : {skipped['traded_value']}  {'(filter active)' if value_available_global else '(filter skipped)'}")
print(f"  RSI calc fail     : {skipped['rsi_calc']}")
print(f"  RSI out of range  : {skipped['rsi_range']}")
print(f"  52W out of range  : {skipped['52w']}")
print(f"  Trend fail        : {skipped['trend']}")
print(f"  Volume < {VOL_RATIO_MIN}x     : {skipped['volume']}")
print(f"  Delivery fail     : {skipped['delivery']}  {'(filter active)' if delivery_available_global else '(filter skipped)'}")
print(f"  Grade C           : {skipped['grade_c']}")
print(f"  QUALIFYING        : {len(results)}")

results.sort(key=lambda x: (x["confidence"] or 0), reverse=True)

output = {
    "stocks":    results,
    "count":     len(results),
    "fetchedAt": datetime.now().isoformat(),
    "autoRun":   True,
    "daysOfData": actual_days,
    "dataAvailability": {
        "mcap":     len(mcap_map) > 0,
        "delivery": delivery_available_global,
        "value":    value_available_global,
    },
    "filters": {
        "mcap_cr":         f"> {MCAP_MIN_CR} Cr",
        "delivery_20d":    f">= {DELIVERY_20D_MIN}%",
        "delivery_trend":  f"20D > 50D + {DELIVERY_TREND_GAP}%",
        "volume_ratio":    f"20D > {VOL_RATIO_MIN}x 50D",
        "rsi":             f"{RSI_MIN} - {RSI_MAX}",
        "trend":           "Close > 30 DMA",
        "from_52w_low":    f"{FROM_LOW_MIN}% - {FROM_LOW_MAX}%",
        "traded_value_cr": f"> {TRADED_VALUE_MIN_CR} Cr",
    }
}
content = json.dumps(output)

os.makedirs("results", exist_ok=True)
with open("results/matrix181_results.json", "w") as f:
    f.write(content)
print(f"\nSaved : results/matrix181_results.json ({len(content)} bytes)")
print(f"Verify: {os.path.getsize('results/matrix181_results.json')} bytes ✓")

print("\nSaving via GitHub API...")
api_ok = save_via_github_api(content)
print(f"API   : {'SUCCESS' if api_ok else 'FAILED'}")

print("\n" + "=" * 60)
print(f"DONE — {len(results)} qualifying stocks")
print(f"A+ : {sum(1 for r in results if r['grade']=='A+')}")
print(f"A  : {sum(1 for r in results if r['grade']=='A')}")
print(f"B+ : {sum(1 for r in results if r['grade']=='B+')}")
print(f"B  : {sum(1 for r in results if r['grade']=='B')}")
print(f"VERY HIGH : {sum(1 for r in results if r['confidenceLabel']=='VERY HIGH')}")
print(f"HIGH      : {sum(1 for r in results if r['confidenceLabel']=='HIGH')}")
print("=" * 60)
