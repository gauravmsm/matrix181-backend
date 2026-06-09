"""
MATRIX 18.5 — Fundamentals Fetcher v2
=======================================
Screener.in blocks GitHub Actions IPs — switched to:
  1. NSE financial results API  → EPS, revenue, PAT (quarterly)
  2. NSE shareholding API       → promoter %, FII, DII
  3. NSE quote API              → PE, sector, industry
  4. Tickertape public API      → ROCE, ROE, D/E (fallback)

Runs weekly via GitHub Actions (Sunday 8:00 AM IST).
Writes: data/fundamentals.json → pushed to backend repo via PAT.
"""

import requests, json, os, time, base64, re
from datetime import datetime

PAT   = os.environ.get("PAT_TOKEN", "")
GUSER = "gauravmsm"
GREPO = "matrix181-backend"

# Session with NSE-friendly headers
S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.nseindia.com/",
})

GOVERNANCE_TYPE = {
    "ABB":"MNC","ABBOTINDIA":"MNC","3MINDIA":"MNC","BOSCHLTD":"MNC",
    "CUMMINSIND":"MNC","GILLETTE":"MNC","HONAUT":"MNC","MARUTI":"MNC",
    "NESTLEIND":"MNC","PFIZER":"MNC","SIEMENS":"MNC","TIMKEN":"MNC",
    "WHIRLPOOL":"MNC","COLPAL":"MNC","SANOFI":"MNC","GLAND":"MNC",
    "ASTRAZEN":"MNC","TORNTPHARM":"GROUP","COALINDIA":"PSU","NTPC":"PSU",
    "ONGC":"PSU","POWERGRID":"PSU","BPCL":"PSU","GAIL":"PSU",
    "INDIANB":"PSU","BANKBARODA":"PSU","CANARABANK":"PSU","SBIN":"PSU",
    "IOC":"PSU","BHEL":"PSU","DIVISLABS":"GROUP","AIAENG":"GROUP",
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
    body = {"message": f"fundamentals update {datetime.now():%Y-%m-%d}",
            "content": base64.b64encode(content_str.encode()).decode(),
            "branch": "main"}
    if sha: body["sha"] = sha
    try:
        r = requests.put(url, headers=hdrs, json=body, timeout=30)
        ok = r.status_code in (200,201)
        print(f"  GitHub push {'OK' if ok else 'FAIL'} ({r.status_code})")
        return ok
    except Exception as e:
        print(f"  Push error: {e}"); return False

def get_scan_symbols():
    try:
        url  = f"https://api.github.com/repos/{GUSER}/{GREPO}/contents/results/matrix181_results.json"
        hdrs = {"Authorization":f"token {PAT}","Accept":"application/vnd.github.v3+json"}
        r = requests.get(url, headers=hdrs, timeout=15)
        if r.status_code != 200: return []
        content = base64.b64decode(r.json()["content"]).decode("utf-8")
        data    = json.loads(content)
        return [s["symbol"] for s in data.get("stocks", [])]
    except Exception as e:
        print(f"  Symbol fetch error: {e}"); return []

def warm_nse_session():
    """NSE requires a session cookie — warm it first."""
    try:
        S.get("https://www.nseindia.com/", timeout=10)
        time.sleep(0.5)
    except: pass

def fetch_nse_quote(symbol):
    """
    NSE quote API — returns PE, sector, industry, 52W data.
    """
    try:
        r = S.get(
            f"https://www.nseindia.com/api/quote-equity?symbol={symbol}",
            timeout=15
        )
        if r.status_code != 200: return {}
        d    = r.json()
        info = d.get("info", {})
        meta = d.get("metadata", {})
        pinfo= d.get("priceInfo", {})
        return {
            "companyName": info.get("companyName"),
            "sector":      info.get("sector"),
            "industry":    info.get("industry"),
            "pe":          meta.get("pdSymbolPe"),
            "pb":          meta.get("pdPmktCapFf"),
            "high52w":     pinfo.get("weekHighLow",{}).get("max"),
            "low52w":      pinfo.get("weekHighLow",{}).get("min"),
        }
    except: return {}

def fetch_nse_financials(symbol):
    """
    NSE financial results API — quarterly P&L data.
    Derives: EPS growth, revenue growth, PAT trend, CFO proxy.
    """
    try:
        r = S.get(
            f"https://www.nseindia.com/api/financial-results-comparision"
            f"?index=equities&symbol={symbol}&consolidated=true",
            timeout=15
        )
        if r.status_code != 200:
            # Try standalone
            r = S.get(
                f"https://www.nseindia.com/api/financial-results-comparision"
                f"?index=equities&symbol={symbol}&consolidated=false",
                timeout=15
            )
        if r.status_code != 200: return {}

        d = r.json()
        # NSE returns list of quarterly results
        results = d if isinstance(d, list) else d.get("data", [])
        if len(results) < 4: return {}

        # Extract PAT and EPS for last 8 quarters
        pat_vals = []
        eps_vals = []
        rev_vals = []
        for q in results[:8]:
            try:
                pat = float(str(q.get("netProfitLoss","") or "").replace(",",""))
                if pat: pat_vals.append(pat)
            except: pass
            try:
                eps = float(str(q.get("earningsPerShare","") or "").replace(",",""))
                if eps: eps_vals.append(eps)
            except: pass
            try:
                rev = float(str(q.get("totalIncome","") or q.get("revenue","") or "").replace(",",""))
                if rev: rev_vals.append(rev)
            except: pass

        # YoY growth: compare avg of last 4Q vs prev 4Q
        def yoy_growth(vals):
            if len(vals) < 8: return None
            recent = sum(vals[:4])
            prev   = sum(vals[4:8])
            if prev and prev > 0:
                return round((recent - prev) / abs(prev) * 100, 1)
            return None

        return {
            "epsGrowthYoY":   yoy_growth(eps_vals),
            "revenueGrowthYoY": yoy_growth(rev_vals),
            "patTrend":       "Positive" if pat_vals and pat_vals[0] > 0 else "Negative",
        }
    except Exception as e:
        return {}

def fetch_nse_shareholding(symbol):
    """NSE shareholding pattern — promoter, FII, DII, public."""
    try:
        r = S.get(
            f"https://www.nseindia.com/api/corporate-shareholding-pattern"
            f"?symbol={symbol}&tabName=shareHolder",
            timeout=15
        )
        if r.status_code != 200: return {}
        data    = r.json()
        records = data.get("data", []) or data.get("shareHoldingList", [])
        if not records: return {}
        latest = records[-1]
        cats   = latest.get("shareHolderList", latest.get("shareholderList", []))
        result = {"quarter": latest.get("quarter") or latest.get("date")}
        for cat in cats:
            name = (cat.get("category","") or cat.get("name","")).lower()
            pct  = None
            for key in ("holdingPer","percentageOfTotal","per"):
                if key in cat:
                    try: pct = float(cat[key]); break
                    except: pass
            if pct is None: continue
            if "promoter" in name and "public" not in name:
                result["promoterPct"] = pct
            elif "fii" in name or "foreign inst" in name:
                result["fiiPct"] = pct
            elif "dii" in name or "domestic inst" in name:
                result["diiPct"] = pct
            elif "public" in name:
                result["publicPct"] = pct
        return result
    except: return {}

def fetch_tickertape(symbol):
    """
    Tickertape public API — ROCE, ROE, D/E, current ratio, OPM.
    Works from server IPs unlike Screener.in.
    """
    try:
        # Tickertape uses slug format (lowercase)
        slug = symbol.lower().replace("&","").replace("-","")
        r = requests.get(
            f"https://api.tickertape.in/stocks/{slug}/ratios",
            headers={"User-Agent":"Mozilla/5.0","Accept":"application/json"},
            timeout=15
        )
        if r.status_code != 200:
            return {}
        d = r.json()
        ratios = d.get("data", {})

        def get_ratio(key):
            val = ratios.get(key)
            if isinstance(val, dict): val = val.get("value") or val.get("current")
            try: return float(val) if val is not None else None
            except: return None

        return {
            "roce":           get_ratio("roce") or get_ratio("returnOnCapitalEmployed"),
            "roe":            get_ratio("roe")  or get_ratio("returnOnEquity"),
            "debtEquity":     get_ratio("debtToEquity") or get_ratio("de"),
            "currentRatio":   get_ratio("currentRatio"),
            "operatingMargin":get_ratio("operatingProfitMargin") or get_ratio("opm"),
            "netMargin":      get_ratio("netProfitMargin") or get_ratio("npm"),
            "epsGrowth3yr":   get_ratio("epsGrowth3Year") or get_ratio("eps3yrCAGR"),
            "salesGrowth3yr": get_ratio("revenueGrowth3Year") or get_ratio("rev3yrCAGR"),
            "interestCoverage": get_ratio("interestCoverageRatio"),
        }
    except: return {}

def fetch_fundamentals_one(symbol):
    """Fetch all fundamentals for one symbol from NSE + Tickertape."""
    result = {"symbol": symbol, "updatedAt": datetime.now().strftime("%Y-%m-%d"),
              "governanceType": GOVERNANCE_TYPE.get(symbol, "PRIVATE")}

    # NSE quote (sector, PE)
    quote = fetch_nse_quote(symbol)
    result.update({k:v for k,v in quote.items() if v is not None})
    time.sleep(0.3)

    # NSE financials (EPS/revenue growth)
    fin = fetch_nse_financials(symbol)
    result.update({k:v for k,v in fin.items() if v is not None})
    time.sleep(0.3)

    # NSE shareholding (promoter %, FII, DII)
    sh = fetch_nse_shareholding(symbol)
    result.update({k:v for k,v in sh.items() if v is not None})
    time.sleep(0.5)

    # Tickertape (ROCE, ROE, D/E) — primary source for ratios
    tt = fetch_tickertape(symbol)
    result.update({k:v for k,v in tt.items() if v is not None})
    time.sleep(0.5)

    return result

# ── Main ──────────────────────────────────────────────────────────────────────
print("="*60)
print("MATRIX 18.5 — FUNDAMENTALS FETCHER v2")
print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
print("Sources: NSE Quote + NSE Financials + NSE Shareholding + Tickertape")
print("="*60)

symbols = get_scan_symbols()
if not symbols:
    print("No symbols from scan results — exiting"); exit(0)

print(f"Fetching fundamentals for {len(symbols)} stocks...")

# Load existing cache
existing = {}
try:
    url  = f"https://api.github.com/repos/{GUSER}/{GREPO}/contents/data/fundamentals.json"
    hdrs = {"Authorization":f"token {PAT}","Accept":"application/vnd.github.v3+json"}
    r    = requests.get(url, headers=hdrs, timeout=15)
    if r.status_code == 200:
        existing = json.loads(base64.b64decode(r.json()["content"]).decode("utf-8"))
        print(f"Existing cache: {len(existing)} stocks")
except: print("No existing cache — fresh fetch")

fund_data = dict(existing)
errors    = []

# Warm NSE session once
warm_nse_session()

for i, sym in enumerate(symbols):
    print(f"  [{i+1}/{len(symbols)}] {sym}...", end=" ", flush=True)
    try:
        data = fetch_fundamentals_one(sym)
        fund_data[sym] = data
        roce = data.get("roce"); roe = data.get("roe")
        de   = data.get("debtEquity"); prom = data.get("promoterPct")
        print(f"ROCE={roce} ROE={roe} D/E={de} Promoter={prom}%")
    except Exception as e:
        errors.append(sym)
        print(f"ERROR: {e}")
        time.sleep(2)

# Push to GitHub via API
out_str = json.dumps(fund_data, indent=2)
os.makedirs("data", exist_ok=True)
with open("data/fundamentals.json","w") as f: f.write(out_str)

print(f"\nFetched : {len(fund_data)} stocks")
print(f"Errors  : {len(errors)} — {errors}")
print("Pushing to GitHub...")
push_github("data/fundamentals.json", out_str)
print("="*60)
print(f"FUNDAMENTALS DONE — {datetime.now():%Y-%m-%d %H:%M:%S}")
print("="*60)
