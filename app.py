from flask import Flask, jsonify
from flask_cors import CORS
import pandas as pd
import numpy as np
import requests
import zipfile
import io
import os
import time
import logging
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

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

CACHE = {"data": None, "ts": 0}
CACHE_TTL = 1800


def get_session():
    try:
        SESSION.get("https://www.nseindia.com", timeout=10)
    except Exception as e:
        log.warning(f"Session warmup failed: {e}")


def fetch_bhavcopy(date):
    d = date.strftime("%d%b%Y").upper()
    url = f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{date.strftime('%Y%m%d')}_F_0000.csv.zip"
    url2 = f"https://www.nseindia.com/content/historical/EQUITIES/{date.year}/{date.strftime('%b').upper()}/cm{d}bhav.csv.zip"
    for u in [url, url2]:
        try:
            r = SESSION.get(u, timeout=15)
            if r.status_code == 200:
                z = zipfile.ZipFile(io.BytesIO(r.content))
                name = z.namelist()[0]
                df = pd.read_csv(z.open(name))
                df.columns = df.columns.str.strip().str.upper()
                return df
        except Exception as e:
            log.debug(f"Bhavcopy fetch failed for {u}: {e}")
    return None


def fetch_delivery(date):
    d = date.strftime("%d%m%Y")
    url = f"https://nsearchives.nseindia.com/archives/equities/deliveries/MTO_{d}.DAT"
    try:
        r = SESSION.get(url, timeout=15)
        if r.status_code == 200:
            lines = r.text.strip().split("\n")
            data_lines = [l for l in lines if not l.startswith("#") and l.strip()]
            rows = []
            for line in data_lines:
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 6:
                    rows.append({
                        "SYMBOL": parts[2],
                        "SERIES": parts[3],
                        "TRADED_QTY": safe_float(parts[4]),
                        "DELIVERABLE_QTY": safe_float(parts[5]),
                    })
            if rows:
                df = pd.DataFrame(rows)
                df = df[df["SERIES"] == "EQ"]
                df["DELIVERY_PCT"] = (df["DELIVERABLE_QTY"] / df["TRADED_QTY"] * 100).round(2)
                return df[["SYMBOL", "DELIVERY_PCT", "TRADED_QTY"]]
    except Exception as e:
        log.debug(f"Delivery fetch failed: {e}")
    return None


def safe_float(x):
    try:
        return float(str(x).replace(",", ""))
    except:
        return 0.0


def get_trading_dates(n=25):
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
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    val = rsi.iloc[-1]
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
    if score >= 80 and 40 <= rsi <= 60 and avg_del >= 55 and vol_trend >= 8 and from_low <= 20:
        return "A"
    if score >= 65 and 38 <= rsi <= 62 and avg_del >= 45 and vol_trend >= 4 and from_low <= 30:
        return "B+"
    if score >= 50 and 35 <= rsi <= 65 and avg_del >= 35 and vol_trend > 0 and from_low <= 40:
        return "B"
    return "C"


def build_screen():
    get_session()
    trading_dates = get_trading_dates(25)
    log.info(f"Fetching data for {len(trading_dates)} trading dates")

    bhavcopy_list = []
    delivery_map = {}

    for date in trading_dates:
        ds = date.strftime("%Y-%m-%d")
        bhav = fetch_bhavcopy(date)
        if bhav is not None:
            bhav["DATE"] = ds
            bhavcopy_list.append(bhav)
            log.info(f"Bhavcopy {ds}: {len(bhav)} rows")
        deliv = fetch_delivery(date)
        if deliv is not None:
            delivery_map[ds] = dict(zip(deliv["SYMBOL"], deliv["DELIVERY_PCT"]))
            log.info(f"Delivery {ds}: {len(deliv)} symbols")

    if not bhavcopy_list:
        raise ValueError("Could not fetch any Bhavcopy data from NSE")

    all_bhav = pd.concat(bhavcopy_list, ignore_index=True)
    all_bhav.columns = all_bhav.columns.str.strip().str.upper()

    rename_map = {
        "TOTTRDQTY": "VOLUME",
        "TTL_TRD_QNTY": "VOLUME",
        "LAST": "CLOSE",
    }
    for old, new in rename_map.items():
        if old in all_bhav.columns and new not in all_bhav.columns:
            all_bhav.rename(columns={old: new}, inplace=True)

    if "SERIES" in all_bhav.columns:
        all_bhav = all_bhav[all_bhav["SERIES"] == "EQ"]

    all_bhav["CLOSE"] = pd.to_numeric(all_bhav.get("CLOSE", 0), errors="coerce")
    all_bhav["VOLUME"] = pd.to_numeric(all_bhav.get("VOLUME", 0), errors="coerce")
    all_bhav["HIGH"] = pd.to_numeric(all_bhav.get("HIGH", 0), errors="coerce")
    all_bhav["LOW"] = pd.to_numeric(all_bhav.get("LOW", 0), errors="coerce")
    all_bhav["DATE"] = pd.to_datetime(all_bhav["DATE"])
    all_bhav.sort_values(["SYMBOL", "DATE"], inplace=True)

    results = []

    for symbol in all_bhav["SYMBOL"].unique():
        sdf = all_bhav[all_bhav["SYMBOL"] == symbol].copy()
        if len(sdf) < 10:
            continue
        closes = sdf["CLOSE"].dropna()
        volumes = sdf["VOLUME"].dropna()
        if len(closes) < 2 or closes.iloc[-1] <= 0:
            continue

        ltp = round(float(closes.iloc[-1]), 2)
        prev = float(closes.iloc[-2])
        change = round((ltp - prev) / prev * 100, 2) if prev else 0
        high_52w = round(float(sdf["HIGH"].max()), 2)
        low_52w = round(float(sdf["LOW"].min()), 2)
        from_low = dist_from_52w_low(ltp, low_52w)
        vol_today = int(volumes.iloc[-1]) if len(volumes) else 0

        rsi = calc_rsi(closes)
        vol_trend = calc_vol_trend(volumes)

        del_vals = []
        for date_row in sdf["DATE"].tail(20):
            ds = date_row.strftime("%Y-%m-%d")
            dp = delivery_map.get(ds, {}).get(symbol)
            if dp is not None:
                del_vals.append(dp)
        avg_del = calc_avg_delivery(del_vals)
        latest_del = del_vals[-1] if del_vals else None

        score = calc_score(avg_del, vol_trend, rsi, from_low)
        grade = grade_setup(score, rsi, avg_del, vol_trend, from_low)

        if grade in ("C", "UNGRADED"):
            continue

        results.append({
            "symbol": symbol,
            "ltp": ltp,
            "change": change,
            "high52w": high_52w,
            "low52w": low_52w,
            "fromLow": from_low,
            "volume": vol_today,
            "rsi": rsi,
            "volTrend": vol_trend,
            "avgDelivery": avg_del,
            "deliveryToday": latest_del,
            "score": score,
            "grade": grade,
            "daysOfData": len(sdf),
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    log.info(f"Screen complete: {len(results)} qualifying stocks")
    return results


@app.route("/api/screen", methods=["GET"])
def screen():
    now = time.time()
    if CACHE["data"] and (now - CACHE["ts"]) < CACHE_TTL:
        return jsonify({
            "status": "ok",
            "cached": True,
            "count": len(CACHE["data"]),
            "stocks": CACHE["data"],
            "fetchedAt": datetime.fromtimestamp(CACHE["ts"]).isoformat()
        })
    try:
        stocks = build_screen()
        CACHE["data"] = stocks
        CACHE["ts"] = now
        return jsonify({
            "status": "ok",
            "cached": False,
            "count": len(stocks),
            "stocks": stocks,
            "fetchedAt": datetime.now().isoformat()
        })
    except Exception as e:
        log.error(f"Screen failed: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})


@app.route("/api/cache/clear", methods=["POST"])
def clear_cache():
    CACHE["data"] = None
    CACHE["ts"] = 0
    return jsonify({"status": "ok", "message": "Cache cleared"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
