"""
scripts/backfill_delivery.py — MATRIX 18.5 v2
===============================================
Same upgrades as daily_delivery.py v2:
  - Full 12-column NSE data saved
  - Retry logic per date
  - Proxy support

Run from: GitHub Actions → Backfill Delivery Data → Run workflow
Input: BACKFILL_DAYS (default 60)
"""
import requests, pandas as pd, zipfile, io, os, base64, time
from datetime import datetime, timedelta
from io import StringIO

PAT           = os.environ.get("PAT_TOKEN", "")
PROXY_URL     = os.environ.get("PROXY_URL", "")
BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS", "60"))
GUSER         = "gauravmsm"
GREPO         = "matrix181-backend"
BRANCH        = "main"

REQUIRED_COLS = [
    "SYMBOL","DATE1","OPEN_PRICE","HIGH_PRICE","LOW_PRICE",
    "CLOSE_PRICE","PREV_CLOSE","TTL_TRD_QNTY","TURNOVER_LACS",
    "NO_OF_TRADES","DELIV_QTY","DELIV_PER",
]
COL_ALIASES = {
    "SYMBOL":       ["SYMBOL","SCRIPID"],
    "DATE1":        ["DATE1","DATE","TIMESTAMP","TRADE_DATE"],
    "OPEN_PRICE":   ["OPEN_PRICE","OPEN","OPENPRICE"],
    "HIGH_PRICE":   ["HIGH_PRICE","HIGH","HIGHPRICE"],
    "LOW_PRICE":    ["LOW_PRICE","LOW","LOWPRICE"],
    "CLOSE_PRICE":  ["CLOSE_PRICE","CLOSE","CLOSEPRICE","LAST_PRICE"],
    "PREV_CLOSE":   ["PREV_CLOSE","PREVCLOSE","PREV_CLOSE_PRICE"],
    "TTL_TRD_QNTY": ["TTL_TRD_QNTY","TOTTRDQTY","TRADED_QTY","VOLUME"],
    "TURNOVER_LACS":["TURNOVER_LACS","TOTTRDVAL","TURNOVER","TOTALTRDVAL"],
    "NO_OF_TRADES": ["NO_OF_TRADES","TOTALTRADES","NUM_TRADES"],
    "DELIV_QTY":    ["DELIV_QTY","DELIVERABLE_QTY"],
    "DELIV_PER":    ["DELIV_PER","DEL_PER","PERCENTDELI","DELIVERY_PCT"],
}

NSE_HDR = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
    "Accept": "text/html,application/xhtml+xml,*/*",
    "DNT": "1",
}
GH_HDR = {"Authorization": f"token {PAT}", "Accept": "application/vnd.github.v3+json"}

S = requests.Session()
S.headers.update(NSE_HDR)
if PROXY_URL:
    S.proxies = {"http": PROXY_URL, "https": PROXY_URL}
    masked = PROXY_URL.split('@')[-1] if '@' in PROXY_URL else PROXY_URL[:30]
    print(f"Proxy: {masked}")


def trdates(n):
    out, d = [], datetime.now()
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5: out.append(d)
    return out


def prime_nse():
    try:
        print("Priming NSE session...")
        S.get("https://www.nseindia.com", timeout=20); time.sleep(2)
        S.get("https://www.nseindia.com/api/marketStatus", timeout=15); time.sleep(1)
        print("NSE session primed ✓")
    except Exception as e:
        print(f"  Prime warning: {e}")


def normalise_columns(df):
    df.columns = df.columns.str.strip().str.upper()
    rn = {}
    for std, variants in COL_ALIASES.items():
        if std not in df.columns:
            for v in variants:
                if v in df.columns: rn[v] = std; break
    if rn: df.rename(columns=rn, inplace=True)
    return df


def extract_eq_data(df):
    if "SERIES" in df.columns:
        df = df[df["SERIES"].astype(str).str.strip() == "EQ"].copy()
    df = normalise_columns(df)
    if "SYMBOL" not in df.columns or "DELIV_PER" not in df.columns:
        return None
    existing = [c for c in REQUIRED_COLS if c in df.columns]
    df = df[existing].copy()
    df["SYMBOL"]    = df["SYMBOL"].astype(str).str.strip().str.upper()
    df["DELIV_PER"] = pd.to_numeric(df["DELIV_PER"], errors="coerce")
    for col in [c for c in existing if c not in ["SYMBOL","DATE1"]]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df[df["DELIV_PER"].between(0.01, 100.0)]
    if "DELIV_PER" in df.columns: df["DELIV_PER"] = df["DELIV_PER"].round(2)
    for col in ["OPEN_PRICE","HIGH_PRICE","LOW_PRICE","CLOSE_PRICE","PREV_CLOSE"]:
        if col in df.columns: df[col] = df[col].round(2)
    df = df.drop_duplicates(subset=["SYMBOL"]).reset_index(drop=True)
    return df if len(df) >= 100 else None


def fetch_with_retry(url, max_attempts=2, wait_secs=60):
    """For backfill, shorter wait (historical data — just needs to load)."""
    for attempt in range(max_attempts):
        try:
            r = S.get(url, timeout=30)
            if r.status_code == 200 and len(r.content) > 5000:
                return r
        except Exception as e:
            pass
        if attempt < max_attempts - 1:
            time.sleep(wait_secs)
    return None


def fetch_sec_bhavdata(date):
    ds  = date.strftime("%d%m%Y")
    url = f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{ds}.csv"
    r   = fetch_with_retry(url)
    if r is None: return None
    for enc in ["utf-8","latin-1"]:
        try: df = pd.read_csv(StringIO(r.content.decode(enc))); break
        except UnicodeDecodeError: continue
    else: return None
    return extract_eq_data(df)


def fetch_pr_zip(date):
    ds  = date.strftime("%d%m%Y")
    url = f"https://nsearchives.nseindia.com/archives/equities/deliveries/PR{ds}.zip"
    r   = fetch_with_retry(url, max_attempts=1)
    if r is None: return None
    try:
        z  = zipfile.ZipFile(io.BytesIO(r.content))
        df = pd.read_csv(z.open(z.namelist()[0]), header=None)
        df.columns = range(len(df.columns))
        df = df[df[0].astype(str).str.strip() == "DR"].copy()
        df = df[df[3].astype(str).str.strip() == "EQ"].copy()
        df["SYMBOL"]    = df[2].astype(str).str.strip().str.upper()
        df["DELIV_PER"] = pd.to_numeric(df[6], errors="coerce")
        df = df[["SYMBOL","DELIV_PER"]].dropna()
        df = df[df["DELIV_PER"].between(0.01,100.0)]
        df["DELIV_PER"] = df["DELIV_PER"].round(2)
        return df if len(df) >= 100 else None
    except: return None


def already_exists(dt_str):
    url = f"https://api.github.com/repos/{GUSER}/{GREPO}/contents/data/delivery/{dt_str}.csv"
    try: return requests.get(url, headers=GH_HDR, timeout=10).status_code == 200
    except: return False


def repo_sha(dt_str):
    url = f"https://api.github.com/repos/{GUSER}/{GREPO}/contents/data/delivery/{dt_str}.csv"
    try:
        r = requests.get(url, headers=GH_HDR, timeout=10)
        return r.json().get("sha") if r.status_code == 200 else None
    except: return None


def push_to_repo(dt_str, df):
    url  = f"https://api.github.com/repos/{GUSER}/{GREPO}/contents/data/delivery/{dt_str}.csv"
    sha  = repo_sha(dt_str)
    body = {
        "message": f"delivery {dt_str} ({len(df)} stocks, {len(df.columns)} cols)",
        "content": base64.b64encode(df.to_csv(index=False).encode()).decode(),
        "branch":  BRANCH,
    }
    if sha: body["sha"] = sha
    try:
        r   = requests.put(url, headers=GH_HDR, json=body, timeout=30)
        ok  = r.status_code in (200, 201)
        print(f"  GitHub {'OK' if ok else 'FAIL'} ({r.status_code}) "
              f"— {dt_str}.csv ({len(df)} stocks, {len(df.columns)} cols)")
        return ok
    except Exception as e:
        print(f"  GitHub error: {e}"); return False


# ── Main ────────────────────────────────────────────────────────────────────────
if not PAT:
    print("ERROR: PAT_TOKEN not set"); raise SystemExit(1)

print("=" * 60)
print(f"MATRIX 18.5 — Backfill Delivery v2 ({BACKFILL_DAYS} days)")
print(f"Time (UTC): {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 60)

prime_nse()

dates = trdates(BACKFILL_DAYS)
ok = skipped = failed = 0

for date in dates:
    dt_str = date.strftime("%Y-%m-%d")
    if already_exists(dt_str):
        print(f"  {dt_str}: exists — skip")
        skipped += 1
        continue

    print(f"\n[{dt_str}]")
    df = fetch_sec_bhavdata(date)
    if df is None:
        df = fetch_pr_zip(date)
    if df is None:
        print(f"  No data (archive may not exist for this date)")
        failed += 1
        continue
    if push_to_repo(dt_str, df): ok += 1
    else: failed += 1
    time.sleep(0.8)

print(f"\n{'='*60}")
print(f"Done: {ok} uploaded | {skipped} skipped | {failed} failed")
print(f"Repo: https://github.com/{GUSER}/{GREPO}/tree/main/data/delivery")
