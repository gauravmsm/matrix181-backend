from flask import Flask, jsonify
from flask_cors import CORS
import pandas as pd
import numpy as np
import requests
import zipfile
import io
import os
import time
import json
import logging
import threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import pytz

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

IST = pytz.timezone("Asia/Kolkata")

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

CACHE_FILE = "cache.json"
CACHE_TTL  = 14 * 3600


def get_session():
    try:
        SESSION.get("https://www.nseindia.com", timeout=10)
    except Exception as e:
        log.warning(f"Session warmup failed: {e}")


def fetch_bhavcopy(date):
    d = date.strftime("%d%b%Y").upper()
    urls = [
        f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{date.strftime('%Y%m%d')}_F_0000.csv.zip",
        f"https://www.nseindia.com/content/historical/EQUITIES/{date.year}/{date.strftime('%b').upper()}/cm{d}bhav.csv.zip",
    ]
    for u in urls:
        try:
            r = SESSION.get(u, timeout=10)
            if r.status_code == 200:
                z = zipfile.ZipFile(io.BytesIO(r.content))
                df = pd.read_csv(z.open(z.namelist()[0]))
                df.columns = df.columns.str.strip().str.upper()
                return df
        except Exception as e:
            log.debug(f"Bhavcopy failed {u}: {e}")
    return None


def fetch_delivery(date):
    d = date.strftime("%d%m%Y")
    url = f"https://nsearchives.nseindia.com/archives/equities/deliveries/MTO_{d}.DAT"
    try:
        r = SESSION.get(url, timeout=10)
        if r.status_code == 200:
            lines = r.text.strip().split("\n")
            rows = []
            for line in lines:
                if line.startswith("#") or not line.strip():
                    continue
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 6:
                    rows.append({
                        "SYMBOL":          parts[2],
                        "SERIES":          parts[3],
                        "TRADED_QTY":      safe_float(parts[4]),
                        "DELIVERABLE_QTY": safe_float(parts[5]),
                    })
            if rows:
                df = pd.DataFrame(rows)
                df = df[df["SERIES"] == "EQ"]
                df["DELIVERY_PCT"] = (
                    df["DELIVERABLE_QTY"] / df["TRADED_QTY"] * 100
                ).round(2)
                return df[["SYMBOL", "DELIVERY_PCT"]]
    except Exception as e:
        log.debug(f"Delivery failed: {e}")
    return None


def fetch_one_date(date):
    ds    = date.strftime("%Y-%m-%d")
    bhav  = fetch_bhavcopy(date)
    deliv = fetch_delivery(date)
    return ds, bhav, deliv


def safe_float(x):
    try:
        return float(str(x).replace(",", ""))
    except:
        return 0.0


def get_trading_dates(n=20):
    dates = []
    d = datetime.now()
    while len(dates) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            dates.append(d)
    return dates


def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    delta = closes.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    rsi   = 100 - (100 / (1 + rs))
    val   = rsi.iloc[-1]
    return round(float(val), 1) if not np.isnan(val) else None


def calc_vol_trend(volumes, period=20):
    if len(volumes) < period:
        return None
    v = volumes.tail(period).reset_index(drop=True)
    x = np.arange(len(v))
    slope, _ = np.polyfit(x, v, 1)
    avg = v.mean()
    if avg == 0:
        return 0.0
    return round(slope / avg * 100, 2)


def calc_avg_delivery(deliveries):
    vals = [d for d in deliveries if d is not None]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 2)


def dist_from_52w_low(close, low_52w):
    if not low_52w or low_52w == 0:
        return None
    return round((close - low_52w) / low_52w * 100, 2)


def dist_from_52w_high(close, high_52w):
    if not high_52w or high_52w == 0:
        return None
    return round((high_52w - close) / high_52w * 100, 2)


def calc_score(avg_del, vol_trend, rsi, from_low):
    if any(v is None for v in [avg_del, vol_trend, rsi, from_low]):
        return 0
    del_score = min(avg_del / 80 * 30, 30)
    vol_score = min(max(vol_trend / 40 * 25, 0), 25)
    rsi_score = max(0, 25 - abs(rsi - 50) * 0.9)
    low_score = max(0, 20 - from_low * 0.5)
    return min(int(round(del_score + vol_score + rsi_score + low_score)), 100)


def grade_setup(score, rsi, avg_del, vol_trend, from_low):
    if any(v is None for v in [rsi, avg_del, vol_trend, from_low]):
        return "UNGRADED"
    if score >= 90 and 45 <= rsi <= 55 and avg_del >= 65 and vol_trend >= 15 and from_low <= 10:
        return "A+"
    if score >= 80 and 40 <= rsi <= 60 and avg_del >= 55 and vol_trend >= 8  and from_low <= 20:
        return "A"
    if score >= 65 and 38 <= rsi <= 62 and avg_del >= 45 and vol_trend >= 4  and from_low <= 30:
        return "B+"
    if score >= 50 and 35 <= rsi <= 65 and avg_del >= 35 and vol_trend > 0   and from_low <= 40:
        return "B"
    return "C"


def calc_confidence(avg_del, del_today, vol_trend, rsi,
                    from_low, from_high, score, grade, del_vals, volumes):
    if any(v is None for v in [avg_del, vol_trend, rsi, from_low]):
        return None, {}, "INSUFFICIENT DATA"

    signals = {}

    del_consistency = 0
    if del_vals and len(del_vals) >= 5:
        high_del_days   = sum(1 for d in del_vals if d >= 50)
        consistency_pct = high_del_days / len(del_vals) * 100
        del_consistency = min(consistency_pct / 100 * 20, 20)
        if del_today and del_today > avg_del:
            del_consistency = min(del_consistency + 3, 20)
    signals["delivery_quality"] = round(del_consistency, 1)

    vol_conviction = 0
    if vol_trend is not None:
        if vol_trend >= 30:   vol_conviction = 20
        elif vol_trend >= 20: vol_conviction = 16
        elif vol_trend >= 10: vol_conviction = 12
        elif vol_trend >= 5:  vol_conviction = 8
        elif vol_trend > 0:   vol_conviction = 4
        if volumes is not None and len(volumes) >= 10:
            recent_avg  = float(volumes.tail(5).mean())
            overall_avg = float(volumes.mean())
            if overall_avg > 0 and recent_avg > overall_avg * 1.2:
                vol_conviction = min(vol_conviction + 4, 20)
    signals["volume_conviction"] = round(vol_conviction, 1)

    rsi_quality = 0
    if rsi is not None:
        if 47 <= rsi <= 53:   rsi_quality = 20
        elif 44 <= rsi <= 56: rsi_quality = 16
        elif 40 <= rsi <= 60: rsi_quality = 11
        elif 35 <= rsi <= 65: rsi_quality = 6
        else:                 rsi_quality = 0
    signals["rsi_zone"] = round(rsi_quality, 1)

    rr_score = 0
    if from_low is not None:
        if from_low <= 5:    rr_score = 20
        elif from_low <= 10: rr_score = 17
        elif from_low <= 15: rr_score = 13
        elif from_low <= 20: rr_score = 9
        elif from_low <= 30: rr_score = 5
        elif from_low <= 40: rr_score = 2
    signals["risk_reward"] = round(rr_score, 1)

    price_pos = 0
    if from_high is not None:
        if from_high >= 30:   price_pos = 10
        elif from_high >= 20: price_pos = 8
        elif from_high >= 10: price_pos = 5
        elif from_high >= 5:  price_pos = 2
        else:                 price_pos = 0
    signals["price_position"] = round(price_pos, 1)

    grade_bonus = {"A+": 10, "A": 8, "B+": 5, "B": 2}.get(grade, 0)
    signals["grade_bonus"] = grade_bonus

    total = (del_consistency + vol_conviction + rsi_quality +
             rr_score + price_pos + grade_bonus)
    total = min(int(round(total)), 100)

    if total >= 85:   label = "VERY HIGH"
    elif total >= 70: label = "HIGH"
    elif total >= 55: label = "MODERATE"
    elif total >= 40: label = "LOW"
    else:             label = "VERY LOW"

    return total, signals, label


def load_cache():
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r") as f:
                data = json.load(f)
            age = time.time() - data["ts"]
            if age < CACHE_TTL:
                log.info(f"Cache hit — {int(age/60)} min old")
                return data
    except Exception as e:
        log.warning(f"Cache load failed: {e}")
    return None


def save_cache(stocks, auto_run=False):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump({
                "ts":        time.time(),
                "stocks":    stocks,
                "fetchedAt": datetime.now(IST).isoformat(),
                "autoRun":   auto_run,
            }, f)
        log.info(f"Cache saved — {len(stocks)} stocks")
    except Exception as e:
        log.warning(f"Cache save failed: {e}")


def build_screen():
    get_session()
    trading_dates = get_trading_dates(20)
    log.info(f"Fetching {len(trading_dates)} dates in parallel")

    bhavcopy_list = []
    delivery_map  = {}

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch_one_date, d): d for d in trading_dates}
        for future in as_completed(futures):
            ds, bhav, deliv = future.result()
            if bhav is not None:
                bhav["DATE"] = ds
                bhavcopy_list.append(bhav)
            if deliv is not None:
                delivery_map[ds] = dict(zip(deliv["SYMBOL"], deliv["DELIVERY_PCT"]))

    if not bhavcopy_list:
        raise ValueError("Could not fetch any Bhavcopy data from NSE")

    log.info(f"Got {len(bhavcopy_list)} days of data")

    all_bhav = pd.concat(bhavcopy_list, ignore_index=True)
    all_bhav.columns = all_bhav.columns.str.strip().str.upper()

    rename_map = {
        "TOTTRDQTY":    "VOLUME",
        "TTL_TRD_QNTY": "VOLUME",
        "LAST":         "CLOSE",
    }
    for old, new in rename_map.items():
        if old in all_bhav.columns and new not in all_bhav.columns:
            all_bhav.rename(columns={old: new}, inplace=True)

    if "SERIES" in all_bhav.columns:
        all_bhav = all_bhav[all_bhav["SERIES"] == "EQ"]

    for col in ["CLOSE", "VOLUME", "HIGH", "LOW"]:
        if col in all_bhav.columns:
            all_bhav[col] = pd.to_numeric(all_bhav[col], errors="coerce")

    all_bhav["DATE"] = pd.to_datetime(all_bhav["DATE"])
    all_bhav.sort_values(["SYMBOL", "DATE"], inplace=True)

    results = []

    for symbol in all_bhav["SYMBOL"].unique():
        sdf     = all_bhav[all_bhav["SYMBOL"] == symbol].copy()
        if len(sdf) < 8:
            continue
        closes  = sdf["CLOSE"].dropna()
        volumes = sdf["VOLUME"].dropna()
        if len(closes) < 2 or closes.iloc[-1] <= 0:
            continue

        ltp       = round(float(closes.iloc[-1]), 2)
        prev      = float(closes.iloc[-2])
        change    = round((ltp - prev) / prev * 100, 2) if prev else 0
        high_52w  = round(float(sdf["HIGH"].max()), 2) if "HIGH" in sdf else ltp
        low_52w   = round(float(sdf["LOW"].min()),  2) if "LOW"  in sdf else ltp
        from_low  = dist_from_52w_low(ltp, low_52w)
        from_high = dist_from_52w_high(ltp, high_52w)
        vol_today = int(volumes.iloc[-1]) if len(volumes) else 0

        rsi       = calc_rsi(closes)
        vol_trend = calc_vol_trend(volumes)

        del_vals = []
        for date_row in sdf["DATE"].tail(20):
            ds = date_row.strftime("%Y-%m-%d")
            dp = delivery_map.get(ds, {}).get(symbol)
            if dp is not None:
                del_vals.append(dp)
        avg_del    = calc_avg_delivery(del_vals)
        latest_del = del_vals[-1] if del_vals else None

        score = calc_score(avg_del, vol_trend, rsi, from_low)
        grade = grade_setup(score, rsi, avg_del, vol_trend, from_low)

        if grade in ("C", "UNGRADED"):
            continue

        conf_score, conf_signals, conf_label = calc_confidence(
            avg_del, latest_del, vol_trend, rsi,
            from_low, from_high, score, grade,
            del_vals, volumes if len(volumes) else None
        )

        results.append({
            "symbol":              symbol,
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
            "confidence":          conf_score,
            "confidenceLabel":     conf_label,
            "confidenceBreakdown": conf_signals,
        })

    results.sort(key=lambda x: (x["confidence"] or 0), reverse=True)
    log.info(f"Screen complete — {len(results)} qualifying stocks")
    return results


def seconds_until_745pm_ist():
    now_ist = datetime.now(IST)
    target  = now_ist.replace(hour=19, minute=45, second=0, microsecond=0)
    if now_ist >= target:
        target += timedelta(days=1)
    diff = (target - now_ist).total_seconds()
    log.info(f"Next auto-run in {int(diff//3600)}h {int((diff%3600)//60)}m")
    return diff


def auto_run_scheduler():
    while True:
        wait = seconds_until_745pm_ist()
        log.info(f"Scheduler sleeping {int(wait)} seconds until 7:45 PM IST")
        time.sleep(wait)
        now_ist = datetime.now(IST)
        if now_ist.weekday() >= 5:
            log.info("Weekend — skipping auto-run")
            continue
        log.info("=== AUTO-RUN TRIGGERED 7:45 PM IST ===")
        try:
            stocks = build_screen()
            save_cache(stocks, auto_run=True)
            log.info(f"Auto-run complete — {len(stocks)} stocks cached")
        except Exception as e:
            log.error(f"Auto-run failed: {e}", exc_info=True)
        time.sleep(60)


scheduler_thread = threading.Thread(target=auto_run_scheduler, daemon=True)
scheduler_thread.start()
log.info("Scheduler started — auto-run at 7:45 PM IST daily")


@app.route("/api/screen", methods=["GET"])
def screen():
    cached = load_cache()
    if cached:
        return jsonify({
            "status":    "ok",
            "cached":    True,
            "autoRun":   cached.get("autoRun", False),
            "count":     len(cached["stocks"]),
            "stocks":    cached["stocks"],
            "fetchedAt": cached["fetchedAt"],
        })
    try:
        stocks = build_screen()
        save_cache(stocks)
        return jsonify({
            "status":    "ok",
            "cached":    False,
            "autoRun":   False,
            "count":     len(stocks),
            "stocks":    stocks,
            "fetchedAt": datetime.now(IST).isoformat(),
        })
    except Exception as e:
        log.error(f"Screen failed: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/health", methods=["GET"])
def health():
    now_ist  = datetime.now(IST)
    target   = now_ist.replace(hour=19, minute=45, second=0, microsecond=0)
    if now_ist >= target:
        target += timedelta(days=1)
    mins_until = int((target - now_ist).total_seconds() / 60)
    return jsonify({
        "status":        "ok",
        "time_ist":      now_ist.isoformat(),
        "next_auto_run": f"{mins_until} minutes",
        "cache_exists":  os.path.exists(CACHE_FILE),
    })


@app.route("/api/cache/clear", methods=["POST"])
def clear_cache():
    try:
        if os.path.exists(CACHE_FILE):
            os.remove(CACHE_FILE)
    except:
        pass
    return jsonify({"status": "ok", "message": "Cache cleared"})


@app.route("/api/run", methods=["POST"])
def manual_run():
    try:
        if os.path.exists(CACHE_FILE):
            os.remove(CACHE_FILE)
        stocks = build_screen()
        save_cache(stocks)
        return jsonify({
            "status":    "ok",
            "count":     len(stocks),
            "stocks":    stocks,
            "fetchedAt": datetime.now(IST).isoformat(),
        })
    except Exception as e:
        log.error(f"Manual run failed: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
