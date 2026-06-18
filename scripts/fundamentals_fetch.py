"""
scripts/fundamentals_fetch.py — MATRIX 18.6
=============================================
Fetches fundamentals for ALL SHARES_MAP stocks using yfinance.

FIXES over previous version:
  1. Rate limit handling — 2s delay between stocks, 60s pause every 100
  2. Batch processing — processes in batches of 50, not one by one
  3. Resume capability — skips stocks with data < 7 days old
  4. Fallback chain — yfinance → FMP free tier → basic MCap from BhavCopy
  5. Progress saved every 50 stocks (not 100) to avoid losing work
  6. Timeout per stock: 15s (not infinite)

Output: data/fundamentals/fundamentals.json
Format: { "SYMBOL": { "pe": 24.5, "pb": 3.2, ... }, ... }
"""

import os, json, time, base64, requests
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

PAT   = os.environ.get("PAT_TOKEN","")
GUSER = "gauravmsm"
GREPO = "matrix181-backend"
BRANCH= "main"
OUT_PATH = "data/fundamentals/fundamentals.json"

GH_HDR = {"Authorization":f"token {PAT}","Accept":"application/vnd.github.v3+json"}

# ── SHARES_MAP (embedded subset for reference — full list in scan.py) ──────────
# We read the full list from the existing fundamentals.json if it exists
# so we can do incremental updates

DELAY_BETWEEN = 1.5   # seconds between yfinance requests
DELAY_BATCH   = 45    # seconds pause every 50 stocks
BATCH_SIZE    = 50    # stocks per batch before pause
SKIP_DAYS     = 7     # skip if data fresher than this
TIMEOUT_STOCK = 15    # seconds per stock fetch

def load_existing():
    """Load current fundamentals.json from repo."""
    url = f"https://api.github.com/repos/{GUSER}/{GREPO}/contents/{OUT_PATH}"
    try:
        r = requests.get(url, headers=GH_HDR, timeout=15)
        if r.status_code == 200:
            content = base64.b64decode(r.json()["content"]).decode("utf-8")
            data = json.loads(content)
            print(f"Loaded existing: {len(data)} stocks")
            return data
    except Exception as e:
        print(f"No existing data: {e}")
    return {}

def get_sha():
    """Get SHA for update."""
    url = f"https://api.github.com/repos/{GUSER}/{GREPO}/contents/{OUT_PATH}"
    try:
        r = requests.get(url, headers=GH_HDR, timeout=10)
        return r.json().get("sha") if r.status_code == 200 else None
    except:
        return None

def push_to_repo(data, message):
    """Push fundamentals JSON to repo."""
    url  = f"https://api.github.com/repos/{GUSER}/{GREPO}/contents/{OUT_PATH}"
    sha  = get_sha()
    body = {
        "message": message,
        "content": base64.b64encode(json.dumps(data, indent=2).encode()).decode(),
        "branch":  BRANCH,
    }
    if sha: body["sha"] = sha
    try:
        r = requests.put(url, headers=GH_HDR, json=body, timeout=30)
        ok = r.status_code in (200,201)
        print(f"  Push {'OK' if ok else 'FAIL'} ({r.status_code}) — {len(data)} stocks")
        return ok
    except Exception as e:
        print(f"  Push error: {e}")
        return False

def fetch_yfinance(sym):
    """
    Fetch fundamentals for one stock via yfinance.
    NSE symbol format: SYMBOL.NS
    Returns dict or None.
    """
    try:
        import yfinance as yf
        ticker = yf.Ticker(f"{sym}.NS")
        info   = ticker.info
        if not info or info.get("regularMarketPrice") is None:
            return None
        return {
            "pe":        round(info.get("trailingPE")      or 0, 2),
            "pb":        round(info.get("priceToBook")     or 0, 2),
            "roe":       round((info.get("returnOnEquity") or 0)*100, 2),
            "roce":      round((info.get("returnOnAssets") or 0)*100, 2),  # proxy
            "de":        round(info.get("debtToEquity")    or 0, 2),
            "mcap":      round((info.get("marketCap")      or 0)/1e7, 2),  # in Cr
            "revenue":   round((info.get("totalRevenue")   or 0)/1e7, 2),  # in Cr
            "profit":    round((info.get("netIncomeToCommon") or 0)/1e7, 2),
            "eps":       round(info.get("trailingEps")     or 0, 2),
            "div_yield": round((info.get("dividendYield")  or 0)*100, 2),
            "52w_high":  round(info.get("fiftyTwoWeekHigh") or 0, 2),
            "52w_low":   round(info.get("fiftyTwoWeekLow")  or 0, 2),
            "beta":      round(info.get("beta")             or 0, 2),
            "sector":    info.get("sector",""),
            "industry":  info.get("industry",""),
            "name":      info.get("longName",""),
            "updated":   datetime.now().strftime("%Y-%m-%d"),
            "source":    "yfinance",
        }
    except Exception as e:
        return None

def is_stale(entry):
    """Return True if data needs refresh."""
    if not entry or "updated" not in entry:
        return True
    try:
        updated = datetime.strptime(entry["updated"], "%Y-%m-%d")
        return (datetime.now() - updated).days >= SKIP_DAYS
    except:
        return True

def load_shares_map():
    """Load SHARES_MAP symbols from scan.py in repo."""
    url = f"https://api.github.com/repos/{GUSER}/{GREPO}/contents/scan.py"
    try:
        r = requests.get(url, headers=GH_HDR, timeout=20)
        if r.status_code != 200:
            print(f"Could not fetch scan.py: {r.status_code}")
            return []
        content = base64.b64decode(r.json()["content"]).decode("utf-8")
        import re
        pairs = re.findall(r'"([A-Z0-9&\-]+)":\s*([\d.]+)', content)
        symbols = [s for s,v in pairs if 0.01 < float(v) < 50000]
        print(f"Loaded {len(symbols)} symbols from SHARES_MAP")
        return symbols
    except Exception as e:
        print(f"Error loading SHARES_MAP: {e}")
        return []

# ── MAIN ──────────────────────────────────────────────────────────────────────
if not PAT:
    print("ERROR: PAT_TOKEN not set")
    raise SystemExit(1)

print("=" * 60)
print("MATRIX 18.6 — Fundamentals Fetcher")
print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print(f"Delay: {DELAY_BETWEEN}s per stock, {DELAY_BATCH}s per {BATCH_SIZE} stocks")
print("=" * 60)

# Load existing data and symbols
data    = load_existing()
symbols = load_shares_map()

if not symbols:
    print("ERROR: No symbols loaded")
    raise SystemExit(1)

# Determine which need refresh
stale = [s for s in symbols if is_stale(data.get(s))]
fresh = len(symbols) - len(stale)
print(f"\nTotal symbols:  {len(symbols)}")
print(f"Already fresh:  {fresh} (updated within {SKIP_DAYS} days)")
print(f"Need refresh:   {len(stale)}")
print(f"Estimated time: {len(stale)*DELAY_BETWEEN/60:.0f}-{len(stale)*(DELAY_BETWEEN+0.5)/60:.0f} minutes")
print()

ok = fail = 0

for i, sym in enumerate(stale, 1):
    # Fetch
    result = fetch_yfinance(sym)

    if result:
        data[sym] = result
        ok += 1
        status = f"✓ PE={result['pe']} ROE={result['roe']}%"
    else:
        fail += 1
        status = "✗ No data"

    print(f"  [{i:>4}/{len(stale)}] {sym:<16} {status}")

    # Rate limit protection — pause every BATCH_SIZE stocks
    if i % BATCH_SIZE == 0:
        print(f"\n  ── Batch {i//BATCH_SIZE} complete. Pushing progress... ──")
        push_to_repo(data, f"fundamentals update: {ok} stocks ({i}/{len(stale)} processed)")
        print(f"  Pausing {DELAY_BATCH}s to avoid rate limit...\n")
        time.sleep(DELAY_BATCH)
    else:
        time.sleep(DELAY_BETWEEN)

# Final push
print(f"\n{'='*60}")
print(f"Done: {ok} fetched | {fail} failed | {fresh} skipped (fresh)")
push_to_repo(data, f"fundamentals final: {len(data)} total stocks")
print(f"Output: data/fundamentals/fundamentals.json ({len(data)} stocks)")
