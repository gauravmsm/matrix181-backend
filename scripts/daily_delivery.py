"""
scripts/daily_delivery.py  — MATRIX 18.5 v2
=============================================
UPGRADES over v1:
  1. Saves FULL NSE data (12 columns) not just SYMBOL,DELIV_PER
  2. Retry logic — 3 attempts with 5-min wait for NSE delays
  3. Proxy support via PROXY_URL secret

Columns saved per day:
  SYMBOL, DATE1, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, CLOSE_PRICE,
  PREV_CLOSE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES,
  DELIV_QTY, DELIV_PER

Source: NSE sec_bhavdata_full_{DDMMYYYY}.csv (published ~5:30 PM IST)
This SINGLE file from NSE contains BOTH OHLCV AND delivery data.

Output: data/delivery/YYYY-MM-DD.csv in GitHub repo
  → scan.py reads DELIV_PER from this file (as before)
  → Full OHLCV data is preserved for future use / master builder

Required GitHub Secrets:
  PAT_TOKEN  — GitHub Personal Access Token (repo scope)
  PROXY_URL  — Residential proxy (optional but recommended)
               Free: webshare.io → 10 residential proxies, 1GB/month
               Format: http://username:password@host:port
"""
import requests
import pandas as pd
import zipfile
import io
import os
import base64
import time
from datetime import datetime, timedelta
from io import StringIO

# ── Config ─────────────────────────────────────────────────────────────────────
PAT       = os.environ.get("PAT_TOKEN", "")
PROXY_URL = os.environ.get("PROXY_URL", "")
GUSER     = "gauravmsm"
GREPO     = "matrix181-backend"
BRANCH    = "main"

# Full columns to save from sec_bhavdata_full
# These are the NSE column names — script handles variants automatically
REQUIRED_COLS = [
    "SYMBOL",
    "DATE1",          # trading date from NSE
    "OPEN_PRICE",
    "HIGH_PRICE",
    "LOW_PRICE",
    "CLOSE_PRICE",
    "PREV_CLOSE",
    "TTL_TRD_QNTY",   # total traded quantity (volume)
    "TURNOVER_LACS",  # turnover in lakhs
    "NO_OF_TRADES",
    "DELIV_QTY",      # deliverable quantity
    "DELIV_PER",      # delivery % — used by MATRIX scan
]

# Column name variants across NSE format versions
COL_ALIASES = {
    "SYMBOL":       ["SYMBOL", "SCRIPID"],
    "DATE1":        ["DATE1", "DATE", "TIMESTAMP", "TRADE_DATE"],
    "OPEN_PRICE":   ["OPEN_PRICE", "OPEN", "OPENPRICE"],
    "HIGH_PRICE":   ["HIGH_PRICE", "HIGH", "HIGHPRICE"],
    "LOW_PRICE":    ["LOW_PRICE",  "LOW",  "LOWPRICE"],
    "CLOSE_PRICE":  ["CLOSE_PRICE","CLOSE","CLOSEPRICE","LAST_PRICE"],
    "PREV_CLOSE":   ["PREV_CLOSE", "PREVCLOSE", "PREV_CLOSE_PRICE"],
    "TTL_TRD_QNTY": ["TTL_TRD_QNTY","TOTTRDQTY","TRADED_QTY","VOLUME"],
    "TURNOVER_LACS":["TURNOVER_LACS","TOTTRDVAL","TURNOVER","TOTALTRDVAL"],
    "NO_OF_TRADES": ["NO_OF_TRADES","TOTALTRADES","NUM_TRADES"],
    "DELIV_QTY":    ["DELIV_QTY","DELIVERABLE_QTY","DELIV_QTY_EQ"],
    "DELIV_PER":    ["DELIV_PER","DEL_PER","PERCENTDELI","DELIVERY_PCT"],
}

NSE_HDR = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.nseindia.com/",
    "Accept": "text/html,application/xhtml+xml,*/*",
    "DNT": "1",
}
GH_HDR = {
    "Authorization": f"token {PAT}",
    "Accept": "application/vnd.github.v3+json",
}

S = requests.Session()
S.headers.update(NSE_HDR)
if PROXY_URL:
    S.proxies = {"http": PROXY_URL, "https": PROXY_URL}
    masked = PROXY_URL.split('@')[-1] if '@' in PROXY_URL else PROXY_URL[:30]
    print(f"Proxy: {masked}")
else:
    print("No proxy configured — direct connection (may be blocked by NSE)")


# ── Helpers ────────────────────────────────────────────────────────────────────

def trdates(n):
    out, d = [], datetime.now()
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return out


def prime_nse():
    """Get NSE session cookies — required before archive downloads."""
    try:
        print("Priming NSE session...")
        S.get("https://www.nseindia.com", timeout=20)
        time.sleep(2)
        S.get("https://www.nseindia.com/api/marketStatus", timeout=15)
        time.sleep(1)
        print("NSE session primed ✓")
    except Exception as e:
        print(f"  Prime warning: {e}")


def normalise_columns(df):
    """
    Map whatever column names NSE uses in this version to our standard names.
    NSE changes column names occasionally — this handles all known variants.
    """
    df.columns = df.columns.str.strip().str.upper()
    rn = {}
    for standard, variants in COL_ALIASES.items():
        if standard not in df.columns:
            for v in variants:
                if v in df.columns:
                    rn[v] = standard
                    break
    if rn:
        df.rename(columns=rn, inplace=True)
    return df


def extract_eq_data(df):
    """
    Filter EQ series, normalise columns, keep only required columns,
    clean numeric fields. Returns clean DataFrame or None.
    """
    # Keep EQ series only
    if "SERIES" in df.columns:
        df = df[df["SERIES"].astype(str).str.strip() == "EQ"].copy()
    elif "SERIES_TYPE" in df.columns:
        df = df[df["SERIES_TYPE"].astype(str).str.strip() == "EQ"].copy()

    df = normalise_columns(df)

    # Check minimum required columns
    if "SYMBOL" not in df.columns:
        print("  ERROR: SYMBOL column missing after normalisation")
        return None
    if "DELIV_PER" not in df.columns:
        print("  ERROR: DELIV_PER column missing after normalisation")
        return None

    # Keep only columns that exist (some may be absent in certain file versions)
    existing = [c for c in REQUIRED_COLS if c in df.columns]
    missing  = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        print(f"  Note: these columns not in this file version: {missing}")
    df = df[existing].copy()

    # Clean SYMBOL
    df["SYMBOL"] = df["SYMBOL"].astype(str).str.strip().str.upper()

    # Clean DELIV_PER (critical for MATRIX)
    df["DELIV_PER"] = pd.to_numeric(df["DELIV_PER"], errors="coerce")

    # Clean numeric columns
    num_cols = [c for c in existing if c not in ["SYMBOL", "DATE1"]]
    for col in num_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # Remove rows with invalid delivery
    df = df[df["DELIV_PER"].between(0.01, 100.0)]

    # Round decimals
    if "DELIV_PER" in df.columns:
        df["DELIV_PER"] = df["DELIV_PER"].round(2)
    for col in ["OPEN_PRICE","HIGH_PRICE","LOW_PRICE","CLOSE_PRICE","PREV_CLOSE"]:
        if col in df.columns:
            df[col] = df[col].round(2)

    df = df.drop_duplicates(subset=["SYMBOL"])
    df = df.reset_index(drop=True)

    if len(df) < 100:
        print(f"  Only {len(df)} rows after cleaning — suspicious")
        return None

    return df


def fetch_with_retry(url, max_attempts=3, wait_secs=300):
    """
    Fetch URL with retry logic.
    NSE often publishes files late — retry after 5 minutes.
    3 attempts × 5 min wait = handles up to 10-minute delays.
    """
    for attempt in range(max_attempts):
        try:
            print(f"  Attempt {attempt+1}/{max_attempts}: {url[-50:]}")
            r = S.get(url, timeout=30)
            if r.status_code == 200 and len(r.content) > 5000:
                print(f"  Got {len(r.content):,} bytes ✓")
                return r
            else:
                print(f"  HTTP {r.status_code}, {len(r.content)} bytes")
        except Exception as e:
            print(f"  Request error: {e}")

        if attempt < max_attempts - 1:
            print(f"  Waiting {wait_secs//60} minutes before retry...")
            time.sleep(wait_secs)

    print(f"  All {max_attempts} attempts failed")
    return None


def fetch_sec_bhavdata(date):
    """
    Primary source: NSE sec_bhavdata_full_{DDMMYYYY}.csv
    This single file contains BOTH OHLCV AND delivery data.
    Published by NSE at ~5:30 PM IST daily.
    """
    ds  = date.strftime("%d%m%Y")
    url = f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{ds}.csv"

    r = fetch_with_retry(url)
    if r is None:
        return None

    for enc in ["utf-8", "latin-1"]:
        try:
            df = pd.read_csv(StringIO(r.content.decode(enc)))
            break
        except UnicodeDecodeError:
            continue
    else:
        print("  Could not decode file")
        return None

    df = extract_eq_data(df)
    if df is not None:
        print(f"  sec_bhavdata_full: {len(df)} EQ stocks, {len(df.columns)} columns")
    return df


def fetch_pr_zip(date):
    """
    Fallback: PR{DDMMYYYY}.zip (delivery only, no OHLCV).
    Used when sec_bhavdata_full is unavailable.
    Note: PR ZIP only has delivery columns — OHLCV will be missing.
    """
    ds  = date.strftime("%d%m%Y")
    url = f"https://nsearchives.nseindia.com/archives/equities/deliveries/PR{ds}.zip"

    r = fetch_with_retry(url, max_attempts=2, wait_secs=120)
    if r is None:
        return None

    try:
        z   = zipfile.ZipFile(io.BytesIO(r.content))
        df  = pd.read_csv(z.open(z.namelist()[0]), header=None)
        df.columns = range(len(df.columns))
        df  = df[df[0].astype(str).str.strip() == "DR"].copy()
        df  = df[df[3].astype(str).str.strip() == "EQ"].copy()
        df["SYMBOL"]    = df[2].astype(str).str.strip().str.upper()
        df["DELIV_PER"] = pd.to_numeric(df[6], errors="coerce")
        df = df[["SYMBOL","DELIV_PER"]].dropna()
        df = df[df["DELIV_PER"].between(0.01, 100.0)]
        df["DELIV_PER"] = df["DELIV_PER"].round(2)
        if len(df) < 100:
            return None
        print(f"  PR ZIP fallback: {len(df)} stocks (OHLCV not available)")
        return df
    except Exception as e:
        print(f"  PR ZIP error: {e}")
        return None


def repo_sha(path):
    """Get SHA of existing file in repo (needed for update)."""
    url = f"https://api.github.com/repos/{GUSER}/{GREPO}/contents/{path}"
    try:
        r = requests.get(url, headers=GH_HDR, timeout=10)
        return r.json().get("sha") if r.status_code == 200 else None
    except:
        return None


def push_to_repo(dt_str, df):
    """
    Push full DataFrame to data/delivery/{dt_str}.csv in GitHub repo.
    Overwrites if exists (daily refresh).
    """
    path = f"data/delivery/{dt_str}.csv"
    url  = f"https://api.github.com/repos/{GUSER}/{GREPO}/contents/{path}"
    sha  = repo_sha(path)

    csv_content = df.to_csv(index=False)   # full DataFrame, all columns
    body = {
        "message": f"delivery {dt_str} ({len(df)} stocks, {len(df.columns)} cols)",
        "content": base64.b64encode(csv_content.encode()).decode(),
        "branch":  BRANCH,
    }
    if sha:
        body["sha"] = sha

    try:
        r = requests.put(url, headers=GH_HDR, json=body, timeout=30)
        ok = r.status_code in (200, 201)
        action = "updated" if sha else "created"
        cols_info = f"{len(df.columns)} columns" if len(df.columns) > 2 else "DELIV_PER only"
        print(f"  GitHub {'OK' if ok else 'FAIL'} ({r.status_code}) "
              f"— {path} {action} ({len(df)} stocks, {cols_info})")
        if not ok:
            print(f"  Response: {r.text[:200]}")
        return ok
    except Exception as e:
        print(f"  GitHub error: {e}")
        return False


# ── Main ────────────────────────────────────────────────────────────────────────

if not PAT:
    print("ERROR: PAT_TOKEN not set in GitHub Secrets")
    raise SystemExit(1)

print("=" * 60)
print("MATRIX 18.5 — Daily Delivery Upload v2")
print(f"Time (UTC): {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print(f"Saving: full NSE data ({len(REQUIRED_COLS)} columns)")
print(f"Retry:  3 attempts × 5-min wait")
print("=" * 60)

prime_nse()
print()

# Process last 2 trading days — today + yesterday safety net
dates = trdates(2)
ok = failed = skipped = 0

for date in dates:
    dt_str = date.strftime("%Y-%m-%d")
    print(f"[{dt_str}  {date.strftime('%A')}]")

    # Skip if already uploaded (avoid redundant overwrites)
    if repo_sha(f"data/delivery/{dt_str}.csv"):
        print("  Already in repo — skip")
        skipped += 1
        print()
        continue

    # Try primary source first (full data)
    df = fetch_sec_bhavdata(date)

    # Fallback to PR ZIP (delivery only)
    if df is None:
        print("  Trying PR ZIP fallback (delivery only)...")
        df = fetch_pr_zip(date)

    if df is None:
        print(f"  FAILED: No data available for {dt_str}")
        print(f"  NSE may not have published yet — will retry tomorrow")
        failed += 1
        print()
        continue

    if push_to_repo(dt_str, df):
        ok += 1
    else:
        failed += 1

    time.sleep(0.5)
    print()

print("=" * 60)
print(f"Done: {ok} uploaded | {skipped} skipped | {failed} failed")
if ok > 0:
    print(f"Data: https://github.com/{GUSER}/{GREPO}/tree/main/data/delivery")
if failed > 0:
    print("Failed dates will be caught by backfill or tomorrow's run")
    raise SystemExit(1)   # marks GitHub Action as ❌ so you get notified
