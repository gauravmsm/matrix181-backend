"""
patch_matrix_jsx.py
Patches Matrix182.jsx in the frontend repo checkout.
Run by patch_jsx.yml after checking out frontend repo.
"""
import os, sys

jsx_path = os.path.join('frontend', 'src', 'Matrix182.jsx')

if not os.path.exists(jsx_path):
    print(f"ERROR: {jsx_path} not found")
    print(f"Current dir: {os.getcwd()}")
    print(f"Files: {os.listdir('.')}")
    sys.exit(1)

with open(jsx_path, 'r', encoding='utf-8') as f:
    content = f.read()

print(f"File loaded: {len(content)} chars, {len(content.splitlines())} lines")
changed = False

# ── Patch 1: safeParseJSON ────────────────────────────────────────────────────
if 'const safeParseJSON' not in content:
    safe_fn = (
        '\nconst safeParseJSON = async (response) => {\n'
        '  const text = await response.text();\n'
        '  const clean = text\n'
        "    .replace(/:\\s*NaN\\b/g, ': null')\n"
        "    .replace(/:\\s*Infinity\\b/g, ': null');\n"
        '  return JSON.parse(clean);\n'
        '};\n'
    )
    marker = 'const TABS = ["ALL", "A+", "A", "B+", "B"];'
    if marker in content:
        content = content.replace(marker, marker + safe_fn)
        print("✅ Added safeParseJSON")
        changed = True
    else:
        print("⚠️  TABS marker not found — skipping safeParseJSON patch")
else:
    print("✓  safeParseJSON already present")

# ── Patch 2: Run Full Veto button ─────────────────────────────────────────────
if 'Run Full Veto' not in content:
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
        # Try to find approximate location for debugging
        idx = content.find('require on-demand analysis')
        if idx >= 0:
            print(f"⚠️  Found footnote at char {idx} but exact match failed")
            print(f"    Context: {repr(content[idx-100:idx+100])}")
        else:
            print("⚠️  Footnote text not found in file")
else:
    print("✓  Run Full Veto button already present")

# ── Write result ──────────────────────────────────────────────────────────────
if changed:
    with open(jsx_path, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f"✅ File written: {len(content)} chars")
else:
    print("No changes — file unchanged")
