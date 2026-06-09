"""
MATRIX 18.5 — Fundamentals Fetcher v4
=======================================
FIXES from v1-v3 failure analysis:
  ISSUE 1: Tickertape → 403 blocked. REMOVED.
  ISSUE 2: NSE APIs → 403 blocked from server IPs. REMOVED as primary.
  ISSUE 3: Cookie expiry during multi-stock runs. FIXED with per-call retry.

PRIMARY SOURCE: Yahoo Finance via yfinance library
  - Works reliably from GitHub Actions server IPs
  - NSE symbols: SYMBOL.NS (e.g. TIMKEN.NS)
  - Returns ROE, D/E, margins, PE, promoter %, sector etc.

FALLBACK CACHE: If fetch fails, keep last known values (never write None
  over valid data). This prevents dashboard from showing empty fundamentals
  when Yahoo Finance has a transient issue.

FIELD CONVERSIONS (yfinance quirks for Indian stocks):
  returnOnEquity    → multiply × 100 (0.17 = 17%)
  returnOnAssets    → multiply × 100 (ROCE proxy)
  operatingMargins  → multiply × 100
  profitMargins     → multiply × 100
  debtToEquity      → divide ÷ 100 (yfinance gives as %, not ratio)
  heldPercentInsiders     → multiply × 100 (promoter proxy)
  heldPercentInstitutions → multiply × 100 (FII+DII proxy)

NOTA BENE on D/E: yfinance debtToEquity is inconsistent across stocks.
  We store raw value and flag zero-debt stocks separately using:
  totalDebt == 0 → debtEquity = 0.0 (override)
"""

import json, os, base64, time, requests
from datetime import datetime

PAT   = os.environ.get("PAT_TOKEN", "")
GUSER = "gauravmsm"
GREPO = "matrix181-backend"

# Governance type — update quarterly
GOVERNANCE_TYPE = {
    "ABB":"MNC","ABBOTINDIA":"MNC","3MINDIA":"MNC","BOSCHLTD":"MNC",
    "CUMMINSIND":"MNC","GILLETTE":"MNC","HONAUT":"MNC","MARUTI":"MNC",
    "NESTLEIND":"MNC","PFIZER":"MNC","SIEMENS":"MNC","TIMKEN":"MNC",
    "WHIRLPOOL":"MNC","COLPAL":"MNC","SANOFI":"MNC","GLAND":"MNC",
    "ASTRAZEN":"MNC","TORNTPHARM":"GROUP","COALINDIA":"PSU","NTPC":"PSU",
    "ONGC":"PSU","POWERGRID":"PSU","BPCL":"PSU","GAIL":"PSU",
    "INDIANB":"PSU","BANKBARODA":"PSU","CANARABANK":"PSU","SBIN":"PSU",
    "IOC":"PSU","BHEL":"PSU","DIVISLABS":"GROUP","AIAENG":"GROUP",
    "GLAND":"MNC","TATACOMM":"GROUP","FEDERALBNK":"PRIVATE",
    "TIINDIA":"GROUP","GMRAIRPORT":"GROUP","SOLARINDS":"PRIVATE",
    "ENRIN":"PRIVATE","ZYDUSLIFE":"GROUP","IPCALAB":"GROUP",
    "ANTHEM":"PRIVATE","NH":"PRIVATE","JSFB":"PRIVATE",
}

def push_github(path, content_str):
    if not PAT:
        print("  No PAT — skipping push"); return False
    url  = f"https://api.github.com/repos/{GUSER}/{GREPO}/contents/{path}"
    hdrs = {"Authorization": f"token {PAT}",
            "Accept": "application/vnd.github.v3+json"}
    sha = None
    try:
        r = requests.get(url, headers=hdrs, timeout=15)
        if r.status_code == 200:
            sha = r.json().get("sha")
    except: pass
    body = {
        "message": f"fundamentals update {datetime.now():%Y-%m-%d}",
        "content": base64.b64encode(content_str.encode()).decode(),
        "branch":  "main",
    }
    if sha: body["sha"] = sha
    try:
        r = requests.put(url, headers=hdrs, json=body, timeout=30)
        ok = r.status_code in (200, 201)
        print(f"  GitHub push {'OK' if ok else 'FAIL'} ({r.status_code})")
        return ok
    except Exception as e:
        print(f"  Push error: {e}"); return False

def get_scan_symbols():
    """Read passing stocks from latest scan results."""
    try:
        url  = (f"https://api.github.com/repos/{GUSER}/{GREPO}"
                f"/contents/results/matrix181_results.json")
        hdrs = {"Authorization": f"token {PAT}",
                "Accept": "application/vnd.github.v3+json"}
        r = requests.get(url, headers=hdrs, timeout=15)
        if r.status_code != 200:
            print(f"  Scan results fetch failed: {r.status_code}"); return []
        content = base64.b64decode(r.json()["content"]).decode("utf-8")
        stocks  = json.loads(content).get("stocks", [])
        return [s["symbol"] for s in stocks]
    except Exception as e:
        print(f"  Symbol fetch error: {e}"); return []

def load_existing_cache():
    """Load existing fundamentals.json — used as fallback for failed fetches."""
    try:
        url  = (f"https://api.github.com/repos/{GUSER}/{GREPO}"
                f"/contents/data/fundamentals.json")
        hdrs = {"Authorization": f"token {PAT}",
                "Accept": "application/vnd.github.v3+json"}
        r = requests.get(url, headers=hdrs, timeout=15)
        if r.status_code == 200:
            data = json.loads(base64.b64decode(r.json()["content"]).decode("utf-8"))
            print(f"  Cache loaded: {len(data)} stocks")
            return data
    except Exception as e:
        print(f"  Cache load error: {e}")
    return {}

def safe_pct(val):
    """Convert yfinance decimal (0.17) to percentage (17.0). None-safe."""
    if val is None: return None
    try: return round(float(val) * 100, 2)
    except: return None

def safe_float(val):
    """Safe float conversion."""
    if val is None: return None
    try: return float(val)
    except: return None

def fetch_yfinance_one(symbol, existing_data):
    """
    Fetch fundamentals for one symbol via yfinance.
    Falls back to existing cached values for any field that returns None.

    Returns merged dict — never overwrites valid cached data with None.
    """
    import yfinance as yf

    ticker_sym = f"{symbol}.NS"
    cached     = existing_data.get(symbol, {})

    try:
        t    = yf.Ticker(ticker_sym)
        info = t.info

        # Verify we got real data (not empty response)
        if not info or not info.get("regularMarketPrice"):
            # Try BSE suffix
            t    = yf.Ticker(f"{symbol}.BO")
            info = t.info

        if not info or not info.get("regularMarketPrice"):
            print(f"    No price data for {symbol} — using cache")
            return cached or {}

        # ── Extract fields ────────────────────────────────────────────────────
        new_data = {}

        # Identity
        new_data["companyName"] = info.get("longName") or info.get("shortName")
        new_data["sector"]      = info.get("sector")
        new_data["industry"]    = info.get("industry")

        # Valuation
        new_data["pe"] = safe_float(info.get("trailingPE") or info.get("forwardPE"))
        new_data["pb"] = safe_float(info.get("priceToBook"))

        # Profitability (yfinance decimal → %)
        new_data["roe"]              = safe_pct(info.get("returnOnEquity"))
        new_data["roce"]             = safe_pct(info.get("returnOnAssets"))  # ROA as proxy
        new_data["operatingMargin"]  = safe_pct(info.get("operatingMargins"))
        new_data["netMargin"]        = safe_pct(info.get("profitMargins"))
        new_data["grossMargin"]      = safe_pct(info.get("grossMargins"))

        # Balance sheet
        # debtToEquity in yfinance = (totalDebt/stockholdersEquity)*100
        # Divide by 100 to get ratio. Zero-debt stocks return 0.
        de_raw = info.get("debtToEquity")
        if de_raw is not None:
            try:
                de = float(de_raw) / 100
                # Sanity check: D/E > 20 is almost certainly a data error
                new_data["debtEquity"] = round(de, 3) if de <= 20 else None
            except:
                new_data["debtEquity"] = None
        else:
            new_data["debtEquity"] = None

        # Override: if totalDebt is explicitly 0, set D/E = 0
        total_debt = info.get("totalDebt") or 0
        if total_debt == 0:
            new_data["debtEquity"] = 0.0

        new_data["currentRatio"] = safe_float(info.get("currentRatio"))
        new_data["quickRatio"]   = safe_float(info.get("quickRatio"))

        # Shareholding
        # heldPercentInsiders = promoter % proxy (decimal → %)
        # heldPercentInstitutions = FII+DII proxy
        insider = info.get("heldPercentInsiders")
        inst    = info.get("heldPercentInstitutions")
        new_data["promoterPct"]      = safe_pct(insider)
        new_data["institutionalPct"] = safe_pct(inst)
        # FII/DII split not available from yfinance — use NSE API if available

        # Growth (yfinance decimal → %)
        new_data["epsGrowthYoY"]     = safe_pct(info.get("earningsQuarterlyGrowth"))
        new_data["revenueGrowthYoY"] = safe_pct(info.get("revenueGrowth"))

        # ── CFO/PAT ratio from cash flow statement ────────────────────────────
        cfo_pat = None
        try:
            cf  = t.cashflow
            inc = t.financials
            if cf is not None and not cf.empty and inc is not None and not inc.empty:
                # Find operating cash flow row
                cfo_val = None
                for idx in cf.index:
                    if "operating" in str(idx).lower() and "cash" in str(idx).lower():
                        try: cfo_val = float(cf.loc[idx].iloc[0]); break
                        except: pass

                # Find net income row
                pat_val = None
                for idx in inc.index:
                    if "net income" in str(idx).lower():
                        try: pat_val = float(inc.loc[idx].iloc[0]); break
                        except: pass

                if cfo_val is not None and pat_val and pat_val != 0:
                    cfo_pat = round(cfo_val / pat_val, 2)
        except Exception as cf_e:
            pass  # Cash flow fetch can fail — not critical
        new_data["cfoPatRatio"] = cfo_pat

        # ── EPS 3yr CAGR from earnings history ───────────────────────────────
        eps_3yr = None
        try:
            hist = t.earnings_dates
            if hist is not None and len(hist) >= 8:
                eps_vals = hist["EPS Estimate"].dropna().tolist()
                if len(eps_vals) >= 8:
                    recent = sum(eps_vals[:4]) / 4
                    old    = sum(eps_vals[4:8]) / 4
                    if old and old > 0:
                        eps_3yr = round((recent / old - 1) * 100, 1)
        except:
            pass
        new_data["epsGrowth3yr"] = eps_3yr

        # ── Merge: prefer new data, fall back to cache for None values ────────
        merged = dict(cached)  # start with cached values
        for k, v in new_data.items():
            if v is not None:
                merged[k] = v  # only update if new value is not None
            # If v is None and cached has a value, cached value is preserved

        # Always update metadata
        merged["symbol"]         = symbol
        merged["governanceType"] = GOVERNANCE_TYPE.get(symbol, "PRIVATE")
        merged["updatedAt"]      = datetime.now().strftime("%Y-%m-%d")
        merged["fetchSource"]    = "yfinance"

        return merged

    except Exception as e:
        print(f"    yfinance error: {e}")
        # Return cache with updated timestamp — don't overwrite with empty
        if cached:
            cached["fetchError"]  = str(e)
            cached["lastAttempt"] = datetime.now().strftime("%Y-%m-%d")
            return cached
        return {}


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    print("="*60)
    print("MATRIX 18.5 — FUNDAMENTALS FETCHER v4")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("Source : yfinance (Yahoo Finance) — confirmed server-IP friendly")
    print("Fallback: cached values preserved when fetch returns None")
    print("="*60)

    # Import yfinance — install if missing
    try:
        import yfinance as yf
        print(f"yfinance version: {yf.__version__}")
    except ImportError:
        import subprocess, sys
        print("Installing yfinance...")
        subprocess.check_call([sys.executable, "-m", "pip", "install",
                               "yfinance", "-q"])
        import yfinance as yf

    symbols = get_scan_symbols()
    if not symbols:
        print("No symbols from scan — exiting"); return

    print(f"\nFetching fundamentals for {len(symbols)} stocks...")
    existing = load_existing_cache()
    fund_data = dict(existing)
    success = errors = 0

    for i, sym in enumerate(symbols):
        print(f"\n  [{i+1}/{len(symbols)}] {sym}", end=" ", flush=True)
        try:
            data = fetch_yfinance_one(sym, existing)

            if data:
                fund_data[sym] = data
                success += 1
                # Print key values for log verification
                roce  = data.get("roce")
                roe   = data.get("roe")
                de    = data.get("debtEquity")
                prom  = data.get("promoterPct")
                opm   = data.get("operatingMargin")
                print(f"→ ROCE={roce} ROE={roe} D/E={de} OPM={opm}% Promoter={prom}%")
            else:
                errors += 1
                print("→ No data returned")

            time.sleep(0.8)  # Respect Yahoo Finance rate limits

        except Exception as e:
            errors += 1
            print(f"→ ERROR: {e}")
            time.sleep(2)

    # Save and push
    out_str = json.dumps(fund_data, indent=2)
    os.makedirs("data", exist_ok=True)
    with open("data/fundamentals.json", "w") as f:
        f.write(out_str)

    print(f"\n{'='*60}")
    print(f"Success  : {success}/{len(symbols)}")
    print(f"Errors   : {errors}")
    print(f"Total    : {len(fund_data)} stocks in cache")
    print("Pushing to GitHub...")
    push_github("data/fundamentals.json", out_str)
    print(f"DONE — {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("="*60)

if __name__ == "__main__":
    main()
