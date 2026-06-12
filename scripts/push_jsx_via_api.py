"""
push_jsx_via_api.py
Uses GitHub API to directly update Matrix182.jsx in the frontend repo.
No git commands needed — pure API call.
Run: python3 scripts/push_jsx_via_api.py
Requires: PAT_TOKEN env var with repo write access
"""
import os, requests, base64, json, sys

PAT      = os.environ.get("PAT_TOKEN", "")
GUSER    = "gauravmsm"
REPO     = "matrix181--frontend-"
JSX_PATH = "src/Matrix182.jsx"

if not PAT:
    print("ERROR: PAT_TOKEN not set"); sys.exit(1)

HDR = {
    "Authorization": f"token {PAT}",
    "Accept": "application/vnd.github.v3+json"
}
URL = f"https://api.github.com/repos/{GUSER}/{REPO}/contents/{JSX_PATH}"

# ── Get current file (need SHA for update) ────────────────────────────────────
print(f"Fetching current {JSX_PATH}...")
r = requests.get(URL, headers=HDR, timeout=15)
if r.status_code != 200:
    print(f"ERROR fetching file: {r.status_code} {r.text[:200]}")
    sys.exit(1)

file_data   = r.json()
current_sha = file_data["sha"]
current_content = base64.b64decode(file_data["content"]).decode("utf-8")
print(f"Current file: {len(current_content)} chars, SHA={current_sha[:8]}")

# ── Apply patches ─────────────────────────────────────────────────────────────
content = current_content
changed = False

# Patch 1: safeParseJSON
if "const safeParseJSON" not in content:
    safe_fn = (
        "\nconst safeParseJSON = async (response) => {\n"
        "  const text = await response.text();\n"
        "  const clean = text\n"
        "    .replace(/:\\s*NaN\\b/g, ': null')\n"
        "    .replace(/:\\s*Infinity\\b/g, ': null');\n"
        "  return JSON.parse(clean);\n"
        "};\n"
    )
    marker = 'const TABS = ["ALL", "A+", "A", "B+", "B"];'
    if marker in content:
        content = content.replace(marker, marker + safe_fn)
        print("✅ Added safeParseJSON")
        changed = True
    else:
        print("⚠️  TABS marker not found")
else:
    print("✓  safeParseJSON already present")

# Patch 2: Run Full Veto button
if "Run Full Veto" not in content:
    old_fn = (
        '            <div style={{marginTop:8,fontSize:10,'
        'color:"#2d3748",fontStyle:"italic"}}>\n'
        '              V1/V2/V4/V5/V6/V9/V10/V13/V14/V15/V16/V18/V19 '
        'require on-demand analysis via veto.js\n'
        '            </div>'
    )
    new_btn = (
        '            <div style={{marginTop:12}}>\n'
        '              <button onClick={()=>setVetoOpen(true)}\n'
        '                style={{width:"100%",background:"rgba(16,185,129,0.12)",\n'
        '                  border:"1px solid rgba(16,185,129,0.3)",borderRadius:8,\n'
        '                  padding:"10px 16px",cursor:"pointer",\n'
        '                  color:"#10b981",fontWeight:700,fontSize:12,\n'
        '                  letterSpacing:"0.04em",display:"flex",\n'
        '                  alignItems:"center",justifyContent:"center",gap:8}}>\n'
        '                \U0001f50d Run Full Veto (V1\u201320, All Sources)\n'
        '                <span style={{fontSize:10,fontWeight:400,color:"#4a5568"}}>\n'
        '                  NSE \u00b7 BSE \u00b7 Screener \u00b7 Moneycontrol \u00b7 ~15s\n'
        '                </span>\n'
        '              </button>\n'
        '            </div>'
    )
    if old_fn in content:
        content = content.replace(old_fn, new_btn)
        print("✅ Added Run Full Veto button")
        changed = True
    else:
        # Debug: find what's actually there
        idx = content.find("require on-demand analysis")
        if idx >= 0:
            print(f"⚠️  Footnote found at {idx} but exact string mismatch")
            print(f"    Actual: {repr(content[idx-120:idx+80])}")
        else:
            print("⚠️  Footnote not found — may already be patched differently")
else:
    print("✓  Run Full Veto already present")

# Patch 3: All .json() → safeParseJSON (main results fetch)
if '.then(r => r.json())' in content:
    content = content.replace(
        '.then(r => r.json())',
        '.then(r => safeParseJSON(r))'
    )
    print("✅ Fixed main results fetch")
    changed = True

# Patch 4: atrStop display in IAZ section
if '"atrStop"' not in content and 'atrStop' not in content:
    content = content.replace(
        '<KV label="Hard Stop (−3% 200SMA)" value={stop} color="#ef4444" />',
        '<KV label="Structural Stop (200SMA)" value={stop} color="#ef4444" />\n'
        '            <KV label={`ATR Stop${stock.t1Pct?" (R:R "+stock.riskReward+")":""}`} '
        'value={stock.atrStop!=null?fmt.price(stock.atrStop):"—"} color="#f59e0b" />'
    )
    print("✅ Added ATR stop display")
    changed = True

if not changed:
    print("No changes needed — file already up to date")
    sys.exit(0)

# ── Push via GitHub API ───────────────────────────────────────────────────────
print(f"\nPushing {len(content)} chars to GitHub...")
body = {
    "message": "fix: safeParseJSON + Run Full Veto button + ATR stop",
    "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
    "sha":     current_sha,
    "branch":  "main"
}
r2 = requests.put(URL, headers=HDR, json=body, timeout=30)
if r2.status_code in (200, 201):
    new_sha = r2.json()["content"]["sha"]
    print(f"✅ Pushed successfully! New SHA={new_sha[:8]}")
    print("Vercel will auto-redeploy in ~60 seconds")
else:
    print(f"❌ Push failed: {r2.status_code}")
    print(r2.text[:500])
    sys.exit(1)
