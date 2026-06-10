"""
BSE API Test Script
Run this locally or in GitHub Actions to verify which endpoints work.
Tests multiple BSE endpoints for financial data coverage.
"""
import requests, json, time

S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com",
})

# Test stocks: RELIANCE=500325, TIMKEN=500480, GLAND=543245, HDFCBANK=500180
TEST_STOCKS = [
    ("RELIANCE",  "500325", "INE002A01018"),
    ("HDFCBANK",  "500180", "INE040A01034"),
    ("TIMKEN",    "500480", "INE325A01013"),
    ("GLAND",     "543245", "INE380O01010"),
]

ENDPOINTS = [
    # Ratios / Key metrics
    ("Ratios",        "https://api.bseindia.com/BseIndiaAPI/api/RatioNew/w?scripcode={scrip}&flag=C"),
    ("Peer Compare",  "https://api.bseindia.com/BseIndiaAPI/api/Peercomp/w?scripcode={scrip}"),
    ("Company Header","https://api.bseindia.com/BseIndiaAPI/api/ComHeadernew/w?quotetype=EQ&scripcode={scrip}&seriesid="),
    ("Financials",    "https://api.bseindia.com/BseIndiaAPI/api/FinancialResultsNew/w?scripcode={scrip}&period=Annual&type=C"),
    ("Balance Sheet", "https://api.bseindia.com/BseIndiaAPI/api/BalanceSheet/w?scripcode={scrip}&type=C"),
    ("P&L",           "https://api.bseindia.com/BseIndiaAPI/api/ProfitLoss/w?scripcode={scrip}&type=C"),
    ("Shareholding",  "https://api.bseindia.com/BseIndiaAPI/api/ShareHoldingPtrn/w?scripcode={scrip}&type=C"),
    ("Key Stats",     "https://api.bseindia.com/BseIndiaAPI/api/GetParameterList/w?scripcode={scrip}"),
    # Alternative BSE domain
    ("Quote",         "https://www.bseindia.com/stock-share-price/reliance-industries/reliance/{scrip}/"),
]

print("=" * 60)
print("BSE API ENDPOINT TEST")
print("=" * 60)

# Test with RELIANCE first
scrip = "500325"
name  = "RELIANCE"

print(f"\nTesting with {name} (scrip={scrip})\n")

results = {}
for ep_name, url_template in ENDPOINTS:
    url = url_template.replace("{scrip}", scrip)
    try:
        r = S.get(url, timeout=10)
        status = r.status_code
        if status == 200:
            try:
                d = r.json()
                # Show first 200 chars of response
                preview = json.dumps(d)[:200]
                results[ep_name] = {"status": status, "preview": preview, "type": type(d).__name__}
                print(f"✅ {ep_name:20} {status} | {type(d).__name__} | {preview[:100]}")
            except:
                results[ep_name] = {"status": status, "preview": r.text[:100]}
                print(f"⚠️  {ep_name:20} {status} | Non-JSON | {r.text[:80]}")
        else:
            results[ep_name] = {"status": status}
            print(f"❌ {ep_name:20} {status} | {r.text[:60]}")
        time.sleep(0.5)
    except Exception as e:
        results[ep_name] = {"error": str(e)}
        print(f"💥 {ep_name:20} ERROR | {e}")

print("\n" + "=" * 60)
print("WORKING ENDPOINTS:")
for name, res in results.items():
    if res.get("status") == 200:
        print(f"  ✅ {name}")
print("\nFAILED ENDPOINTS:")
for name, res in results.items():
    if res.get("status") != 200:
        print(f"  ❌ {name}: {res.get('status','ERROR')}")
