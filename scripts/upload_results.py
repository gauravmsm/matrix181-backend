"""
scripts/upload_results.py
==========================
Pushes scan.py output (results/matrix181_results.json) to GitHub repo.
Runs after scan.py completes in the daily scan workflow.

FIX: Retry logic for 409 SHA mismatch errors.
     On 409 — re-fetch SHA and retry immediately (up to 3 times).
     This handles race conditions and stale SHA values.
"""
import os, requests, base64, json, time

PAT   = os.environ.get("PAT_TOKEN", "")
GUSER = "gauravmsm"
GREPO = "matrix181-backend"
PATH  = "results/matrix181_results.json"

if not PAT:
    print("ERROR: PAT_TOKEN not set"); raise SystemExit(1)

try:
    with open(PATH, "rb") as f:
        content = f.read()
    size = os.path.getsize(PATH)
    print(f"Results file: {size:,} bytes")
    data = json.loads(content)
    print(f"Stocks: {len(data.get('stocks', []))}")
except FileNotFoundError:
    print(f"ERROR: {PATH} not found — scan.py may have failed")
    raise SystemExit(1)
except json.JSONDecodeError as e:
    print(f"ERROR: Invalid JSON in results — {e}")
    raise SystemExit(1)

hdrs = {
    "Authorization": f"token {PAT}",
    "Accept": "application/vnd.github.v3+json",
}
url = f"https://api.github.com/repos/{GUSER}/{GREPO}/contents/{PATH}"
encoded = base64.b64encode(content).decode()

def get_sha():
    """Always fetch fresh SHA immediately before PUT."""
    try:
        r = requests.get(url, headers=hdrs, timeout=15)
        if r.status_code == 200:
            sha = r.json().get("sha")
            print(f"  SHA: {sha[:12]}...")
            return sha
        elif r.status_code == 404:
            print("  File does not exist yet — will create")
            return None
    except Exception as e:
        print(f"  SHA fetch warning: {e}")
    return None

def push(sha):
    body = {
        "message": f"scan results — {data.get('fetchedAt','')[:10]} — {len(data.get('stocks',[]))} stocks",
        "content": encoded,
        "branch":  "main",
    }
    if sha:
        body["sha"] = sha
    return requests.put(url, headers=hdrs, json=body, timeout=30)

# ── Upload with retry on 409 ──────────────────────────────────────────────────
MAX_ATTEMPTS = 3
for attempt in range(1, MAX_ATTEMPTS + 1):
    print(f"\nUpload attempt {attempt}/{MAX_ATTEMPTS}...")
    sha = get_sha()           # always fresh SHA right before PUT
    try:
        r = push(sha)
        if r.status_code in (200, 201):
            print(f"✅ GitHub OK ({r.status_code}) — results uploaded successfully")
            break
        elif r.status_code == 409:
            print(f"  409 SHA conflict — re-fetching SHA and retrying...")
            time.sleep(2)     # brief pause then retry with fresh SHA
            continue
        else:
            print(f"  FAILED ({r.status_code}): {r.text[:300]}")
            if attempt == MAX_ATTEMPTS:
                raise SystemExit(1)
    except requests.exceptions.RequestException as e:
        print(f"  Request error: {e}")
        if attempt == MAX_ATTEMPTS:
            raise SystemExit(1)
        time.sleep(3)
else:
    print(f"❌ All {MAX_ATTEMPTS} attempts failed")
    raise SystemExit(1)
