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
            print(f"  Existing file found SHA: {sha[:8]}")
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
            print(f"  GitHub API save OK — status {r.status_code}")
            return True
        else:
            print(f"  GitHub API save FAILED — {r.status_code}: {r.text[:300]}")
            return False
    except Exception as e:
        print(f"  GitHub API save error: {e}")
        return False

def get_session():
    try:
        SESSION.get("https://www.nseindia.com", timeout=10)
        print("NSE session ready")
    except Exception as e:
        print(f"Session warmup: {e}")

def normalise_bhav(df):
    df.columns = df.columns.str.strip().str.upper()
    rename = {
        "TCKRSYMB":     "SYMBOL",
        "SCTYSRS":      "SERIES",
        "CLSPRIC":      "CLOSE",
        "HGHPRIC":      "HIGH",
        "LWPRIC":       "LOW",
        "TTLTRADGVOL":  "VOLUME",
        "TRADDT":       "DATE",
        "OPNPRIC":      "OPEN",
        "LASTPRIC":     "LAST",
        "TOTTRDQTY":    "VOLUME",
        "TTL_TRD_QNTY": "VOLUME",
    }
    df.rename(columns={k: v for k, v in rename.items() if k in df.columns}, inplace=True)
    return df

def fetch_bhavcopy(date):
    d = date.strftime("%d%b%Y").upper()
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
            print(f"  Bhavcopy failed {date.strftime('%Y-%m-%d')}: {e}")
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
        print(f"  Delivery failed: {e}")
    return None

def fetch_one(date):
    ds = date.strftime("%Y-%m-%d")
    return ds, fetch_bhavcopy(date), fetch_delivery(date)

def safe_float(x):
    try:
        return float(str(x).replace(",", ""))
    except:
        return 0.0

def get_trading_dates(n=25):
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

def calc_vol_trend(volumes, period=15):
    clean = volumes.dropna()
    if len(clean) < period:
        return None
    v = clean.tail(period).reset_index(drop=True)
    x = np.arange(len(v))
    try:
        slope, _ = np.polyfit(x, v, 1)
        avg = float(v.mean())
        return round(slope / avg * 100, 2) if avg != 0 else 0.0
    except:
        return None

def calc_avg_delivery(vals):
    vals = [d for d in vals if d is not None and not np.isnan(d)]
    return round(sum(vals) / len(vals), 2) if vals else None

def calc_score(avg_del, vol_trend, rsi, from_low):
    if any(v is None for v in [vol_trend, rsi, from_low]):
        return 0
    del_val   = avg_del if avg_del is not None else 50
    del_score = min(del_val / 80 * 30, 30)
    vol_score = min(max(vol_trend / 40 * 25, 0), 25)
    rsi_score = max(0, 25 - abs(rsi - 50) * 0.9)
    low_score = max(0, 20 - from_low * 0.5)
    return min(int(round(del_score + vol_score + rsi_score + low_score)), 100)

def grade_setup(score, rsi, avg_del, vol_trend, from_low):
    if any(v is None for v in [rsi, vol_trend, from_low]):
        return "C"
    def del_ok(t):
        return True if avg_del is None else avg_del >= t
    if score>=90 and 45<=rsi<=55 and del_ok(65) and vol_trend>=15 and from_low<=10: return "A+"
    if score>=80 and 40<=rsi<=60 and del_ok(55) and vol_trend>=8  and from_low<=20: return "A"
    if score>=65 and 38<=rsi<=62 and del_ok(45) and vol_trend>=4  and from_low<=30: return "B+"
    if score>=50 and 35<=rsi<=65 and del_ok(35) and vol_trend>0   and from_low<=40: return "B"
    return "C"

def calc_confidence(avg_del, del_today, vol_trend, rsi,
                    from_low, from_high, score, grade, del_vals, volumes):
    sig = {}
    dq = 0
    if del_vals and len(del_vals) >= 3:
        dq = min(sum(1 for d in del_vals if d >= 50) / len(del_vals) * 20, 20)
        if del_today and del_today > (avg_del or 0):
            dq = min(dq + 3, 20)
    elif avg_del and avg_del >= 50:
        dq = 10
    sig["delivery_quality"] = round(dq, 1)
    vc = 0
    if vol_trend is not None:
        if vol_trend >= 30:   vc = 20
        elif vol_trend >= 20: vc = 16
        elif vol_trend >= 10: vc = 12
        elif vol_trend >= 5:  vc = 8
        elif vol_trend > 0:   vc = 4
        if volumes is not None and len(volumes) >= 10:
            if float(volumes.mean()) > 0 and float(volumes.tail(5).mean()) > float(volumes.mean()) * 1.2:
                vc = min(vc + 4, 20)
    sig["volume_conviction"] = round(vc, 1)
    rq = 0
    if rsi is not None:
        rq = 20 if 47<=rsi<=53 else 16 if 44<=rsi<=56 else 11 if 40<=rsi<=60 else 6 if 35<=rsi<=65 else 0
    sig["rsi_zone"] = round(rq, 1)
    rr = 0
    if from_low is not None:
        rr = 20 if from_low<=5 else 17 if from_low<=10 else 13 if from_low<=15 else 9 if from_low<=20 else 5 if from_low<=30 else 2 if from_low<=40 else 0
    sig["risk_reward"] = round(rr, 1)
    pp = 0
    if from_high is not None:
        pp = 10 if from_high>=30 else 8 if from_high>=20 else 5 if from_high>=10 else 2 if from_high>=5 else 0
    sig["price_position"] = round(pp, 1)
    gb = {"A+":10,"A":8,"B+":5,"B":2}.get(grade, 0)
    sig["grade_bonus"] = gb
    total = min(int(round(dq + vc + rq + rr + pp + gb)), 100)
    label = "VERY HIGH" if total>=85 else "HIGH" if total>=70 else "MODERATE" if total>=55 else "LOW" if total>=40 else "VERY LOW"
    return total, sig, label


print("=" * 60)
print("MATRIX 18.1 — GITHUB ACTIONS DAILY SCAN")
print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print(f"PAT_TOKEN set: {'YES' if GITHUB_TOKEN else 'NO — API save will fail'}")
print("=" * 60)

get_session()
trading_dates = get_trading_dates(25)
print(f"Fetching {len(trading_dates)} weekdays...")

bhavcopy_list = []
delivery_map  = {}
completed     = 0

with ThreadPoolExecutor(max_workers=6) as ex:
    futures = {ex.submit(fetch_one, d): d for d in trading_dates}
    for future in as_completed(futures):
        try:
            ds, bhav, deliv = future.result()
            if bhav is not None:
                bhav["DATE"] = ds
                bhavcopy_list.append(bhav)
            if deliv is not None:
                try:
                    delivery_map[ds] = dict(zip(deliv["SYMBOL"], deliv["DELIVERY_PCT"]))
                except:
                    pass
            completed += 1
            print(f"  {completed}/{len(trading_dates)} — bhav:{len(bhavcopy_list)} deliv:{len(delivery_map)}")
        except Exception as e:
            completed += 1
            print(f"  Skipped: {e}")

actual_days = len(bhavcopy_list)
print(f"\nBhavcopy days : {actual_days}")
print(f"Delivery days : {len(delivery_map)}")

if actual_days == 0:
    output  = {"stocks":[],"count":0,"fetchedAt":datetime.now().isoformat(),"autoRun":True}
    content = json.dumps(output)
    os.makedirs("results", exist_ok=True)
    with open("results/matrix181_results.json","w") as f: f.write(content)
    save_via_github_api(content)
    exit(0)

all_bhav = pd.concat(bhavcopy_list, ignore_index=True)
if "SYMBOL" not in all_bhav.columns:
    print("FATAL: SYMBOL missing")
    exit(1)

for col in ["CLOSE","VOLUME","HIGH","LOW"]:
    if col in all_bhav.columns:
        all_bhav[col] = pd.to_numeric(all_bhav[col], errors="coerce")
if "DATE" in all_bhav.columns:
    all_bhav["DATE"] = pd.to_datetime(all_bhav["DATE"], errors="coerce")
if "SERIES" in all_bhav.columns:
    all_bhav = all_bhav[all_bhav["SERIES"] == "EQ"]

all_bhav.sort_values(["SYMBOL","DATE"], inplace=True)
symbols = all_bhav["SYMBOL"].dropna().unique()
print(f"Total EQ symbols: {len(symbols)}")

results      = []
processed    = 0
skip_rows    = 0
skip_close   = 0
skip_rsi     = 0
skip_vol     = 0
skip_fromlow = 0
skip_grade_c = 0

for symbol in symbols:
    try:
        sdf = all_bhav[all_bhav["SYMBOL"] == symbol].copy()
        processed += 1
        if processed % 500 == 0:
            print(f"  {processed}/{len(symbols)} — qualifying: {len(results)}")
        if len(sdf) < 12: skip_rows += 1; continue
        closes  = sdf["CLOSE"].dropna()  if "CLOSE"  in sdf.columns else pd.Series([], dtype=float)
        volumes = sdf["VOLUME"].dropna() if "VOLUME" in sdf.columns else pd.Series([], dtype=float)
        if len(closes) < 12: skip_close += 1; continue
        ltp = float(closes.iloc[-1])
        if ltp <= 0: skip_close += 1; continue
        ltp       = round(ltp, 2)
        prev      = float(closes.iloc[-2])
        change    = round((ltp - prev) / prev * 100, 2) if prev else 0
        high_52w  = round(float(sdf["HIGH"].max()), 2) if "HIGH" in sdf.columns else ltp
        low_52w   = round(float(sdf["LOW"].min()),  2) if "LOW"  in sdf.columns else ltp
        from_low  = round((ltp - low_52w)  / low_52w  * 100, 2) if low_52w  else None
        from_high = round((high_52w - ltp) / high_52w * 100, 2) if high_52w else None
        vol_today = int(volumes.iloc[-1]) if len(volumes) else 0
        rsi = calc_rsi(closes)
        if rsi is None: skip_rsi += 1; continue
        vol_trend = calc_vol_trend(volumes)
        if vol_trend is None: skip_vol += 1; continue
        if from_low is None: skip_fromlow += 1; continue
        del_vals = []
        if "DATE" in sdf.columns:
            for dr in sdf["DATE"].dropna().tail(25):
                try:
                    ds2 = pd.Timestamp(dr).strftime("%Y-%m-%d")
                    dp  = delivery_map.get(ds2, {}).get(str(symbol))
                    if dp is not None: del_vals.append(float(dp))
                except: pass
        avg_del    = calc_avg_delivery(del_vals)
        latest_del = del_vals[-1] if del_vals else None
        score      = calc_score(avg_del, vol_trend, rsi, from_low)
        grade      = grade_setup(score, rsi, avg_del, vol_trend, from_low)
        if grade == "C": skip_grade_c += 1; continue
        conf, sigs, clabel = calc_confidence(
            avg_del, latest_del, vol_trend, rsi,
            from_low, from_high, score, grade,
            del_vals, volumes if len(volumes) else None
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
            "rsi":                 rsi,
            "volTrend":            vol_trend,
            "avgDelivery":         avg_del,
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
print(f"  Too few rows          : {skip_rows}")
print(f"  No close / zero price : {skip_close}")
print(f"  RSI insufficient      : {skip_rsi}")
print(f"  Vol trend insufficient: {skip_vol}")
print(f"  No 52W distance       : {skip_fromlow}")
print(f"  Grade C               : {skip_grade_c}")
print(f"  QUALIFYING            : {len(results)}")

results.sort(key=lambda x: (x["confidence"] or 0), reverse=True)

output  = {
    "stocks":    results,
    "count":     len(results),
    "fetchedAt": datetime.now().isoformat(),
    "autoRun":   True,
    "daysOfData": actual_days,
}
content = json.dumps(output)

os.makedirs("results", exist_ok=True)
with open("results/matrix181_results.json","w") as f:
    f.write(content)
print("Saved locally")

print("Saving to GitHub via API...")
if save_via_github_api(content):
    print("GitHub API save: SUCCESS")
else:
    print("GitHub API save: FAILED")

print("=" * 60)
print(f"DONE — {len(results)} qualifying stocks")
print(f"A+: {sum(1 for r in results if r['grade']=='A+')}")
print(f"A : {sum(1 for r in results if r['grade']=='A')}")
print(f"B+: {sum(1 for r in results if r['grade']=='B+')}")
print(f"B : {sum(1 for r in results if r['grade']=='B')}")
print(f"VERY HIGH: {sum(1 for r in results if r['confidenceLabel']=='VERY HIGH')}")
print("=" * 60)
