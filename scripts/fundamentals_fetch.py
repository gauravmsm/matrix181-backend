"""
MATRIX 18.5 — Fundamentals Fetcher
====================================
Runs weekly via GitHub Actions (Sunday 8:00 AM IST).
Scrapes Screener.in + NSE shareholding for all stocks
that appear in the latest scan results.

Writes: data/fundamentals.json → pushed to backend repo.
scan.py reads this cache for structural grade computation.

Workflow: .github/workflows/fundamentals.yml
Schedule: 0 2 * * 0  (Sunday 2:00 AM UTC = 7:30 AM IST)
"""

import requests, json, os, time, base64, re
from datetime import datetime

PAT   = os.environ.get("PAT_TOKEN", "")
GUSER = "gauravmsm"
GREPO = "matrix181-backend"

HDR = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}
S = requests.Session()
S.headers.update(HDR)

# ── Governance type lookup (static — update quarterly) ────────────────────────
# MNC = foreign promoter, PSU = govt promoter, GROUP = Indian business group
GOVERNANCE_TYPE = {
    # MNCs
    "ABB": "MNC", "ABBOTINDIA": "MNC", "3MINDIA": "MNC", "ASIANPAINT": "MNC",
    "BOSCHLTD": "MNC", "CUMMINSIND": "MNC", "GILLETTE": "MNC", "HONAUT": "MNC",
    "MARUTI": "MNC", "NESTLEIND": "MNC", "PFIZER": "MNC", "SIEMENS": "MNC",
    "TIMKEN": "MNC", "WHIRLPOOL": "MNC", "COLPAL": "MNC", "GSKCONSUMER": "MNC",
    "SANOFI": "MNC", "GLAND": "MNC", "ASTRAZEN": "MNC", "TORNTPHARM": "GROUP",
    # PSUs
    "COALINDIA": "PSU", "NTPC": "PSU", "ONGC": "PSU", "POWERGRID": "PSU",
    "BPCL": "PSU", "GAIL": "PSU", "INDIANB": "PSU", "BANKBARODA": "PSU",
    "CANARABANK": "PSU", "SBIN": "PSU", "IOC": "PSU", "BHEL": "PSU",
    # Default: private Indian
}

def push_github(path, content_str):
    """Push a file to the GitHub repo."""
    if not PAT:
        print("  No PAT — skipping push")
        return False
    url  = f"https://api.github.com/repos/{GUSER}/{GREPO}/contents/{path}"
    hdrs = {"Authorization": f"token {PAT}",
            "Accept": "application/vnd.github.v3+json"}
    sha  = None
    try:
        r = requests.get(url, headers=hdrs, timeout=15)
        if r.status_code == 200:
            sha = r.json().get("sha")
    except:
        pass
    body = {
        "message": f"fundamentals update {datetime.now():%Y-%m-%d}",
        "content": base64.b64encode(content_str.encode()).decode(),
        "branch": "main",
    }
    if sha:
        body["sha"] = sha
    try:
        r = requests.put(url, headers=hdrs, json=body, timeout=30)
        ok = r.status_code in (200, 201)
        print(f"  GitHub push {'OK' if ok else 'FAIL'} ({r.status_code})")
        return ok
    except Exception as e:
        print(f"  Push error: {e}")
        return False


def get_scan_symbols():
    """Read latest scan results to get the symbol list to fetch fundamentals for."""
    try:
        url  = (f"https://api.github.com/repos/{GUSER}/{GREPO}"
                f"/contents/results/matrix181_results.json")
        hdrs = {"Authorization": f"token {PAT}",
                "Accept": "application/vnd.github.v3+json"}
        r = requests.get(url, headers=hdrs, timeout=15)
        if r.status_code != 200:
            return []
        content = base64.b64decode(r.json()["content"]).decode("utf-8")
        data    = json.loads(content)
        return [s["symbol"] for s in data.get("stocks", [])]
    except Exception as e:
        print(f"  Symbol fetch error: {e}")
        return []


def scrape_screener(symbol, retries=2):
    """
    Scrape Screener.in for key financial ratios.
    Tries consolidated first, then standalone.
    Returns dict of fundamental metrics.
    """
    urls = [
        f"https://www.screener.in/company/{symbol}/consolidated/",
        f"https://www.screener.in/company/{symbol}/",
    ]
    html = None
    for url in urls:
        for attempt in range(retries):
            try:
                r = S.get(url, timeout=20, headers={
                    **HDR,
                    "Referer": "https://www.screener.in/",
                    "Accept": "text/html,application/xhtml+xml",
                })
                if r.status_code == 200:
                    html = r.text
                    break
                time.sleep(2)
            except:
                time.sleep(3)
        if html:
            break

    if not html:
        return {}

    def extract_ratio(label_pattern):
        """Extract numeric value after a label in Screener.in HTML."""
        patterns = [
            rf'{label_pattern}[^<]*</span>[^<]*<span[^>]*class="[^"]*number[^"]*"[^>]*>\s*([\d,.]+)',
            rf'<span[^>]*>\s*{label_pattern}\s*</span>[^<]*<span[^>]*>\s*([\d,.]+)',
            rf'{label_pattern}[^<]*</li>[^<]*<li[^>]*>\s*<span[^>]*>\s*([\d,.]+)',
        ]
        for pat in patterns:
            m = re.search(pat, html, re.IGNORECASE)
            if m:
                try:
                    return float(m.group(1).replace(",", ""))
                except:
                    pass
        return None

    # Extract top ratios section
    ratios_section = re.search(
        r'id=["\']top-ratios["\'][^>]*>(.*?)</section',
        html, re.DOTALL | re.IGNORECASE
    )
    rs = ratios_section.group(1) if ratios_section else html

    def from_section(label):
        m = re.search(
            rf'<span[^>]*>\s*{label}[^<]*</span>\s*<span[^>]*>\s*([\d,.]+)',
            rs, re.IGNORECASE
        )
        return float(m.group(1).replace(",","")) if m else None

    roce    = from_section("ROCE") or extract_ratio("ROCE")
    roe     = from_section("ROE")  or extract_ratio("ROE")
    de      = from_section("Debt / Equity") or extract_ratio("Debt.*Equity") or extract_ratio("D/E")
    cr      = from_section("Current Ratio") or extract_ratio("Current Ratio")
    opm     = from_section("OPM") or extract_ratio("OPM")
    pe      = from_section("Stock P/E") or extract_ratio("Stock P/E") or extract_ratio("P/E")

    # EPS / Sales growth — look in growth table
    def extract_cagr(label):
        m = re.search(
            rf'{label}.*?(\d+\.?\d*)\s*%.*?3\s*[Yy]r|3\s*[Yy]r.*?{label}.*?(\d+\.?\d*)\s*%',
            html, re.DOTALL
        )
        if m:
            return float(m.group(1) or m.group(2))
        # Alternative: look for table row
        m2 = re.search(rf'<td[^>]*>\s*{label}.*?</td>.*?<td[^>]*>\s*([\d.]+)',
                       html, re.DOTALL | re.IGNORECASE)
        return float(m2.group(1)) if m2 else None

    eps_g  = extract_cagr("EPS")
    sale_g = extract_cagr("Sales")

    # CFO/PAT from cash flow table
    cfo_pat = None
    try:
        cfo_m = re.search(r'Cash from Operations[^<]*</td>.*?<td[^>]*>([\d,.-]+)', html, re.DOTALL|re.IGNORECASE)
        pat_m = re.search(r'Net Profit[^<]*</td>.*?<td[^>]*>([\d,.-]+)',          html, re.DOTALL|re.IGNORECASE)
        if cfo_m and pat_m:
            cfo = float(cfo_m.group(1).replace(",",""))
            pat = float(pat_m.group(1).replace(",",""))
            cfo_pat = round(cfo/pat, 2) if pat and pat != 0 else None
    except:
        pass

    # Interest coverage
    int_cov = extract_ratio("Interest Coverage") or extract_ratio("Int.*Cover")

    return {
        "roce":           roce,
        "roe":            roe,
        "debtEquity":     de,
        "currentRatio":   cr,
        "operatingMargin": opm,
        "pe":             pe,
        "epsGrowth3yr":   eps_g,
        "salesGrowth3yr": sale_g,
        "cfoPatRatio":    cfo_pat,
        "interestCoverage": int_cov,
    }


def fetch_shareholding_nse(symbol):
    """
    Fetch latest shareholding pattern from NSE.
    Returns promoter%, FII%, DII%, public%.
    """
    # Warm session
    try:
        S.get("https://www.nseindia.com/", timeout=10)
    except:
        pass

    nse_hdrs = {
        **HDR,
        "Accept": "application/json",
        "Referer": f"https://www.nseindia.com/get-quotes/equity?symbol={symbol}",
    }
    try:
        r = S.get(
            f"https://www.nseindia.com/api/corporate-shareholding-pattern"
            f"?symbol={symbol}&tabName=shareHolder",
            headers=nse_hdrs, timeout=15
        )
        if r.status_code != 200:
            return {}
        data = r.json()
        records = data.get("data", []) or data.get("shareHoldingList", [])
        if not records:
            return {}

        # Get latest quarter
        latest   = records[-1]
        cats     = latest.get("shareHolderList", latest.get("shareholderList", []))
        result   = {"quarter": latest.get("quarter") or latest.get("date")}
        for cat in cats:
            name = (cat.get("category","") or cat.get("name","")).lower()
            pct  = None
            for key in ("holdingPer","percentageOfTotal","per","holdingPercentage"):
                if key in cat:
                    try: pct = float(cat[key]); break
                    except: pass
            if pct is None:
                continue
            if "promoter" in name:  result["promoterPct"] = pct
            elif "fii" in name or "foreign" in name: result["fiiPct"] = pct
            elif "dii" in name or "domestic inst" in name: result["diiPct"] = pct
            elif "public" in name:  result["publicPct"] = pct
        return result
    except Exception as e:
        return {}


# ── Main ──────────────────────────────────────────────────────────────────────
print("="*60)
print("MATRIX 18.5 — FUNDAMENTALS FETCHER")
print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
print("="*60)

symbols = get_scan_symbols()
if not symbols:
    print("No symbols from scan results — exiting")
    exit(0)

print(f"Fetching fundamentals for {len(symbols)} stocks...")

# Load existing cache to do incremental update
existing = {}
try:
    url  = (f"https://api.github.com/repos/{GUSER}/{GREPO}"
            f"/contents/data/fundamentals.json")
    hdrs = {"Authorization": f"token {PAT}",
            "Accept": "application/vnd.github.v3+json"}
    r = requests.get(url, headers=hdrs, timeout=15)
    if r.status_code == 200:
        existing = json.loads(
            base64.b64decode(r.json()["content"]).decode("utf-8")
        )
        print(f"Existing cache: {len(existing)} stocks")
except:
    print("No existing cache — fresh fetch")

fund_data = dict(existing)  # preserve existing data
errors    = []

for i, sym in enumerate(symbols):
    print(f"  [{i+1}/{len(symbols)}] {sym}...", end=" ")
    try:
        # Screener.in fundamentals
        scr = scrape_screener(sym)

        # NSE shareholding
        sh  = fetch_shareholding_nse(sym)
        time.sleep(1.5)  # rate limit — ~40 stocks/min

        fund_data[sym] = {
            **scr,
            **{k:v for k,v in sh.items() if v is not None},
            "governanceType": GOVERNANCE_TYPE.get(sym, "PRIVATE"),
            "updatedAt": datetime.now().strftime("%Y-%m-%d"),
            "symbol": sym,
        }
        print(f"ROCE={scr.get('roce')} ROE={scr.get('roe')} D/E={scr.get('debtEquity')} Promoter={sh.get('promoterPct')}%")
    except Exception as e:
        errors.append(sym)
        print(f"ERROR: {e}")
        time.sleep(3)

# Push to repo
os.makedirs("data", exist_ok=True)
out_str = json.dumps(fund_data, indent=2)
with open("data/fundamentals.json","w") as f:
    f.write(out_str)

print(f"\nFetched: {len(fund_data)} stocks")
print(f"Errors:  {len(errors)} — {errors[:10]}")
print(f"Pushing to GitHub...")
push_github("data/fundamentals.json", out_str)
print("="*60)
print(f"FUNDAMENTALS DONE — {datetime.now():%Y-%m-%d %H:%M:%S}")
print("="*60)
