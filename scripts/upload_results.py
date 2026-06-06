import os, requests, base64, json

PAT = os.environ.get("PAT_TOKEN", "")
if not PAT:
    print("No PAT_TOKEN — skipping upload"); exit(0)

results_file = "results/matrix181_results.json"
try:
    data = json.load(open(results_file))
    print(f"File size: {os.path.getsize(results_file)} bytes")
except Exception as e:
    print(f"Results file not found: {e}"); exit(0)

url  = "https://api.github.com/repos/gauravmsm/matrix181-backend/contents/results/matrix181_results.json"
hdrs = {"Authorization": f"token {PAT}", "Accept": "application/vnd.github.v3+json"}
sha  = None
try:
    r = requests.get(url, headers=hdrs, timeout=15)
    if r.status_code == 200:
        sha = r.json().get("sha")
        print(f"Existing SHA: {sha}")
except: pass

body = {
    "message": "scan results",
    "content": base64.b64encode(open(results_file, "rb").read()).decode(),
    "branch":  "main",
}
if sha: body["sha"] = sha

r = requests.put(url, headers=hdrs, json=body, timeout=30)
print(f"PUT status: {r.status_code}")
if r.status_code in (200, 201):
    print("SUCCESS — file saved")
    print(f"URL: {r.json().get('content',{}).get('download_url','')}")
else:
    print(f"FAIL: {r.text[:200]}")
    exit(1)
    
