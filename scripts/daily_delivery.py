import requests, zipfile, io, os, base64, time
import pandas as pd
from datetime import datetime, timedelta
from io import StringIO

PAT    = os.environ["PAT_TOKEN"]
GUSER  = "gauravmsm"
GREPO  = "matrix181-backend"
BRANCH = "main"

NSE_HDR = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}
S  = requests.Session()
S.headers.update(NSE_HDR)
GH = {"Authorization": f"token {PAT}", "Accept": "application/vnd.github.v3+json"}

def trdates(n):
    out, d = [], datetime.now()
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return out

def prime_nse():
    try:
        S.get("https://www.nseindia.com", timeout=15)
        time.sleep(2)
        S.get("https://www.nseindia.com/api/marketStatus", timeout=10)
        time.sleep(1)
        print("NSE session primed")
    except Exception as e:
        print(f"NSE prime warning: {e}")

def fetch_delivery(date):
    ds     = date.strftime("%d%m%Y")
    dt_str = date.strftime("%Y-%m-%d")

    # Primary: sec_bhavdata_full (same source as your uploaded Excel files)
    url = f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{ds}.csv"
    try:
        r = S.get(url, timeout=30)
        if r.status_code == 200 and len(r.content) > 5000:
            for enc in ["utf-8", "latin-1"]:
                try: df = pd.read_csv(StringIO(r.content.decode(enc))); break
                except UnicodeDecodeError: continue
            df.columns = df.columns.str.strip().str.upper()
            if "SERIES" in df.columns:
                df = df[df["SERIES"].astype(str).str.strip() == "EQ"].copy()
            if "SYMBOL" in df.columns and "DELIV_PER" in df.columns:
                df["SYMBOL"]    = df["SYMBOL"].astype(str).str.strip().str.upper()
                df["DELIV_PER"] = pd.to_numeric(df["DELIV_PER"], errors="coerce")
                df = df[["SYMBOL","DELIV_PER"]].dropna()
                df = df[df["DELIV_PER"].between(0.01, 100)]
                if len(df) > 100:
                    print(f"  {dt_str}: {len(df)} stocks (sec_bhavdata_full)")
                    return df
    except Exception as e:
        print(f"  sec_bhavdata err {dt_str}: {e}")

    # Fallback: PR delivery ZIP
    url = f"https://nsearchives.nseindia.com/archives/equities/deliveries/PR{ds}.zip"
    try:
        r = S.get(url, timeout=30)
        if r.status_code == 200 and len(r.content) > 5000:
            z  = zipfile.ZipFile(io.BytesIO(r.content))
            df = pd.read_csv(z.open(z.namelist()[0]), header=None)
            df.columns = range(len(df.columns))
            df = df[df[0].astype(str).str.strip() == "DR"].copy()
            df = df[df[3].astype(str).str.strip() == "EQ"].copy()
            df["SYMBOL"]    = df[2].astype(str).str.strip().str.upper()
            df["DELIV_PER"] = pd.to_numeric(df[6], errors="coerce")
            df = df[["SYMBOL","DELIV_PER"]].dropna()
            df = df[df["DELIV_PER"].between(0.01, 100)]
            if len(df) > 100:
                print(f"  {dt_str}: {len(df)} stocks (PR ZIP)")
                return df
    except Exception as e:
        print(f"  PR ZIP err {dt_str}: {e}")

    print(f"  {dt_str}: no data found")
    return None

def push_csv(dt_str, df):
    url     = f"https://api.github.com/repos/{GUSER}/{GREPO}/contents/data/delivery/{dt_str}.csv"
    content = df[["SYMBOL","DELIV_PER"]].round(2).to_csv(index=False)
    body    = {
        "message": f"delivery {dt_str}",
        "content": base64.b64encode(content.encode()).decode(),
        "branch":  BRANCH,
    }
    try:
        r = requests.get(url, headers=GH, timeout=10)
        if r.status_code == 200:
            body["sha"] = r.json().get("sha")
    except: pass
    try:
        r = requests.put(url, headers=GH, json=body, timeout=20)
        ok = r.status_code in (200, 201)
        print(f"  GitHub {'OK' if ok else 'FAIL'} ({r.status_code}) → data/delivery/{dt_str}.csv")
        return ok
    except Exception as e:
        print(f"  GitHub error: {e}"); return False

# Upload last 2 trading days (catches today + yesterday if missed)
prime_nse()
dates = trdates(2)
for date in dates:
    dt_str = date.strftime("%Y-%m-%d")
    print(f"\n{dt_str}")
    df = fetch_delivery(date)
    if df is not None:
        push_csv(dt_str, df)
    time.sleep(0.5)
print("\nDone")
