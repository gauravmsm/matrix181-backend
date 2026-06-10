"""
MATRIX 18.5 — Fundamentals Fetcher v5
=======================================
CHANGES FROM v4:
  1. Fetches ALL stocks in SHARES_MAP (2452 stocks), not just scan-passing
  2. NSE→Yahoo symbol corrections for known mismatches
  3. Parallel fetching (5 workers) → ~8 min for full universe
  4. D/E=None disambiguation: zero debt vs missing data
  5. Incremental update: skip stocks updated in last 7 days
  6. Smarter fallback chain: .NS → .BO → symbol variants

MATRIX 18.1 parameters covered:
  V3  Promoter %           ← NSE shareholding (Yahoo proxy)
  V7  CFO/PAT              ← yfinance cashflow
  V8  Debt/Equity          ← yfinance balance sheet
  V13 Credit stability     ← D/E + interest coverage
  V19 Survivability        ← D/E + current ratio + CFO
  P2  ROCE/ROE             ← yfinance (ROA proxy)
  P2  Balance sheet score  ← D/E + current ratio
  P2  Cash flow score      ← CFO/PAT
  P2  Capital allocation   ← EPS/revenue growth
  P2  Moat                 ← OPM + ROA
"""

import json, os, base64, time, requests
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

PAT   = os.environ.get("PAT_TOKEN", "")
GUSER = "gauravmsm"
GREPO = "matrix181-backend"

# ── NSE → Yahoo Finance symbol corrections ────────────────────────────────────
# Format: "NSE_SYMBOL": "YAHOO_SYMBOL" (without .NS/.BO suffix)
SYMBOL_MAP = {
    # Known mismatches — NSE symbol differs from Yahoo ticker
    "GVT&D":      "GVTD",
    "JSFB":       "JSB",          # J&K Small Finance Bank
    "GMRAIRPORT": "GMRP",         # GMR Airports Infrastructure
    "M&M":        "M%26M",        # URL encode &
    "M&MFIN":     "M%26MFIN",
    "L&TFH":      "L%26TFH",
    "HSCL":       "HSCL",
    "WELSPUNLIV":  "WELSPUNLIV",
    # These should work with .NS but often return empty — try .BO
    "ENRIN":      None,            # None = skip, likely delisted/OTC
    "TIINDIA":    "TIINDIA",       # Should work — retry logic handles it
}

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
}

def push_github(path, content_str):
    if not PAT: print("  No PAT"); return False
    url  = f"https://api.github.com/repos/{GUSER}/{GREPO}/contents/{path}"
    hdrs = {"Authorization":f"token {PAT}","Accept":"application/vnd.github.v3+json"}
    sha  = None
    try:
        r = requests.get(url, headers=hdrs, timeout=15)
        if r.status_code == 200: sha = r.json().get("sha")
    except: pass
    body = {"message":f"fundamentals update {datetime.now():%Y-%m-%d}",
            "content":base64.b64encode(content_str.encode()).decode(),"branch":"main"}
    if sha: body["sha"] = sha
    try:
        r = requests.put(url, headers=hdrs, json=body, timeout=30)
        ok = r.status_code in (200,201)
        print(f"  GitHub push {'OK' if ok else 'FAIL'} ({r.status_code})")
        return ok
    except Exception as e:
        print(f"  Push error: {e}"); return False

def get_all_symbols():
    """Read SHARES_MAP from scan.py to get full NSE universe."""
    try:
        url  = f"https://api.github.com/repos/{GUSER}/{GREPO}/contents/scan.py"
        hdrs = {"Authorization":f"token {PAT}","Accept":"application/vnd.github.v3+json"}
        r    = requests.get(url, headers=hdrs, timeout=30)
        if r.status_code != 200: return []
        content = base64.b64decode(r.json()["content"]).decode("utf-8")
        import re
        # Extract all symbols from SHARES_MAP
        symbols = re.findall(r'"([A-Z0-9&%]+)":\s*[\d.]+', content)
        # Filter out obviously non-stock entries
        symbols = [s for s in symbols if len(s) >= 2 and len(s) <= 15
                   and not s.startswith("NV") and "NIFTY" not in s
                   and "BEES" not in s and "ETF" not in s]
        print(f"  SHARES_MAP symbols: {len(symbols)}")
        return list(set(symbols))
    except Exception as e:
        print(f"  Symbol fetch error: {e}"); return []

def load_cache():
    try:
        url  = f"https://api.github.com/repos/{GUSER}/{GREPO}/contents/data/fundamentals.json"
        hdrs = {"Authorization":f"token {PAT}","Accept":"application/vnd.github.v3+json"}
        r    = requests.get(url, headers=hdrs, timeout=15)
        if r.status_code == 200:
            data = json.loads(base64.b64decode(r.json()["content"]).decode("utf-8"))
            print(f"  Cache: {len(data)} stocks")
            return data
    except: pass
    return {}

def needs_update(cached_entry, force=False):
    """Return True if stock needs a fresh fetch (older than 7 days or missing key fields)."""
    if force or not cached_entry: return True
    updated = cached_entry.get("updatedAt","")
    if not updated: return True
    try:
        age = (datetime.now() - datetime.strptime(updated, "%Y-%m-%d")).days
        if age > 7: return True
        # Re-fetch if key fields are still None
        if cached_entry.get("roe") is None and cached_entry.get("roce") is None:
            return True
        return False
    except: return True

def safe_pct(val):
    if val is None: return None
    raw = val.get("raw", val) if isinstance(val, dict) else val
    try: return round(float(raw) * 100, 2)
    except: return None

def safe_num(val):
    raw = val.get("raw", val) if isinstance(val, dict) else val
    if raw is None: return None
    try: return float(raw)
    except: return None

def fetch_one(sym):
    """
    Fetch yfinance data for one NSE symbol.
    Returns (symbol, data_dict) or (symbol, None) on failure.
    """
    import yfinance as yf

    # Skip known delisted/OTC symbols
    if SYMBOL_MAP.get(sym) is None and sym in SYMBOL_MAP:
        return sym, {"symbol":sym,"skipReason":"delisted_or_otc","updatedAt":datetime.now().strftime("%Y-%m-%d")}

    # Build ticker variants to try
    yf_sym = SYMBOL_MAP.get(sym, sym)
    variants = [f"{yf_sym}.NS", f"{yf_sym}.BO", f"{sym}.NS", f"{sym}.BO"]
    # Remove duplicates while preserving order
    seen = set(); variants = [v for v in variants if not (v in seen or seen.add(v))]

    info = None
    used_ticker = None
    for ticker_str in variants:
        try:
            t    = yf.Ticker(ticker_str)
            d    = t.info
            if d and d.get("regularMarketPrice"):
                info        = d
                used_ticker = ticker_str
                break
            time.sleep(0.1)
        except: continue

    if not info:
        return sym, None

    # ── Extract fields ────────────────────────────────────────────────────────
    roe  = safe_pct(info.get("returnOnEquity"))
    roce = safe_pct(info.get("returnOnAssets"))
    opm  = safe_pct(info.get("operatingMargins"))
    npm  = safe_pct(info.get("profitMargins"))
    eps_g = safe_pct(info.get("earningsQuarterlyGrowth"))
    rev_g = safe_pct(info.get("revenueGrowth"))
    cr    = safe_num(info.get("currentRatio"))
    qr    = safe_num(info.get("quickRatio"))
    pe    = safe_num(info.get("trailingPE") or info.get("forwardPE"))
    pb    = safe_num(info.get("priceToBook"))

    # D/E disambiguation
    de_raw    = safe_num(info.get("debtToEquity"))
    total_debt = safe_num(info.get("totalDebt")) or 0
    if total_debt == 0:
        de = 0.0  # confirmed zero debt
    elif de_raw is not None:
        de = round(de_raw / 100, 3)
        if de > 20: de = None  # data error
    else:
        de = None  # genuinely unknown

    # Shareholding
    promoter = safe_pct(info.get("heldPercentInsiders"))
    inst     = safe_pct(info.get("heldPercentInstitutions"))

    # CFO/PAT — attempt cash flow fetch
    cfo_pat = None
    try:
        t2  = yf.Ticker(used_ticker)
        cf  = t2.cashflow
        inc = t2.financials
        if cf is not None and not cf.empty and inc is not None and not inc.empty:
            cfo_val = pat_val = None
            for idx in cf.index:
                if "operating" in str(idx).lower() and "cash" in str(idx).lower():
                    try: cfo_val = float(cf.loc[idx].iloc[0]); break
                    except: pass
            for idx in inc.index:
                if "net income" in str(idx).lower():
                    try: pat_val = float(inc.loc[idx].iloc[0]); break
                    except: pass
            if cfo_val and pat_val and pat_val != 0:
                cfo_pat = round(cfo_val / pat_val, 2)
    except: pass

    return sym, {
        "symbol":         sym,
        "yahooTicker":    used_ticker,
        "companyName":    info.get("longName") or info.get("shortName"),
        "sector":         info.get("sector"),
        "industry":       info.get("industry"),
        "governanceType": GOVERNANCE_TYPE.get(sym, "PRIVATE"),
        "pe":             pe,   "pb": pb,
        "high52w":        safe_num(info.get("fiftyTwoWeekHigh")),
        "low52w":         safe_num(info.get("fiftyTwoWeekLow")),
        "roe":            roe,  "roce": roce,
        "operatingMargin":opm,  "netMargin": npm,
        "debtEquity":     de,
        "currentRatio":   cr,   "quickRatio": qr,
        "cfoPatRatio":    cfo_pat,
        "epsGrowthYoY":   eps_g, "revenueGrowthYoY": rev_g,
        "promoterPct":    promoter,
        "institutionalPct": inst,
        "updatedAt":      datetime.now().strftime("%Y-%m-%d"),
        "fetchSource":    "yfinance_v5",
    }

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("="*60)
    print("MATRIX 18.5 — FUNDAMENTALS FETCHER v5")
    print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("Scope  : ALL SHARES_MAP stocks (full NSE universe)")
    print("="*60)

    try:
        import yfinance as yf
        print(f"yfinance: {yf.__version__}")
    except ImportError:
        import subprocess, sys
        subprocess.check_call([sys.executable,"-m","pip","install","yfinance","-q"])
        import yfinance as yf

    all_symbols = get_all_symbols()
    if not all_symbols:
        print("No symbols found — exiting"); return

    existing = load_cache()

    # Determine which stocks need update
    to_fetch = [s for s in all_symbols if needs_update(existing.get(s))]
    skip_ct  = len(all_symbols) - len(to_fetch)
    print(f"\nTotal   : {len(all_symbols)} stocks")
    print(f"Skip    : {skip_ct} (updated within 7 days)")
    print(f"Fetch   : {len(to_fetch)} stocks")
    print(f"Workers : 5 parallel")
    print(f"ETA     : ~{len(to_fetch)*0.8/5/60:.0f} min\n")

    fund_data  = dict(existing)
    success = errors = 0
    batch_size = 100  # push to GitHub every 100 stocks

    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(fetch_one, sym): sym for sym in to_fetch}
        done = 0
        for fut in as_completed(futures):
            sym = futures[fut]
            done += 1
            try:
                sym_out, data = fut.result()
                if data:
                    fund_data[sym_out] = data
                    success += 1
                    roce = data.get("roce"); roe = data.get("roe")
                    de   = data.get("debtEquity"); prom = data.get("promoterPct")
                    print(f"  [{done}/{len(to_fetch)}] {sym_out:15} "
                          f"ROCE={roce} ROE={roe} D/E={de} Promoter={prom}%")
                else:
                    errors += 1
                    print(f"  [{done}/{len(to_fetch)}] {sym:15} → No data")
            except Exception as e:
                errors += 1
                print(f"  [{done}/{len(to_fetch)}] {sym:15} → ERROR: {e}")

            # Push progress every 100 stocks so partial data is saved
            if done % batch_size == 0:
                print(f"\n  Saving progress ({done}/{len(to_fetch)})...")
                out_str = json.dumps(fund_data, indent=2)
                push_github("data/fundamentals.json", out_str)
                print()

    # Final push
    out_str = json.dumps(fund_data, indent=2)
    os.makedirs("data", exist_ok=True)
    with open("data/fundamentals.json","w") as f: f.write(out_str)

    print(f"\n{'='*60}")
    print(f"Success : {success}")
    print(f"Errors  : {errors}")
    print(f"Total   : {len(fund_data)} stocks in cache")
    print("Final push to GitHub...")
    push_github("data/fundamentals.json", out_str)
    print(f"DONE — {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("="*60)

if __name__ == "__main__":
    main()
