#!/usr/bin/env python3
"""
patch_matrix_jsx.py
====================
Patches Matrix182.jsx in the private frontend repo with full MRS-enabled
Part 7 (Market Regime Score) and Part 8 (CPM Capital Protection Mode).

Usage:
  From matrix181-backend root (where PAT_TOKEN env var is set):
    python scripts/patch_matrix_jsx.py            # patch and push
    python scripts/patch_matrix_jsx.py --dry-run  # preview only, no push

Environment:
  PAT_TOKEN   GitHub personal access token (read+write on frontend repo)

Repos:
  Frontend:  gauravmsm/matrix181--frontend-   (private)
  File:      src/Matrix182.jsx
"""

import os, sys, re, base64, json, textwrap
import requests
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
PAT      = os.environ.get("PAT_TOKEN", "")
GUSER    = "gauravmsm"
FE_REPO  = "matrix181--frontend-"
JSX_PATH = "src/Matrix182.jsx"
BRANCH   = "main"
DRY_RUN  = "--dry-run" in sys.argv

HEADERS  = {
    "Authorization": f"token {PAT}",
    "Accept":        "application/vnd.github.v3+json",
}
BASE_URL = f"https://api.github.com/repos/{GUSER}/{FE_REPO}/contents/{JSX_PATH}"

# ══════════════════════════════════════════════════════════════════════════════
# REPLACEMENT STRINGS
# The patch replaces Part 7 and Part 8 sections in Matrix182.jsx.
# Detection: regex anchors on the comment markers already in the file.
# ══════════════════════════════════════════════════════════════════════════════

# ── Part 7 replacement block ──────────────────────────────────────────────────
PART7_JSX = r"""
          {/* ── PART 7: MARKET REGIME SCORE (MRS 4-Factor) ────────────────── */}
          <Section>
            <SectionHeader>
              <SectionTitle>Part 7 — Market Regime (MRS 4-Factor)</SectionTitle>
              <div style={{display:"flex",alignItems:"center",gap:6}}>
                {s.mrs != null && (
                  <span style={{fontSize:13,fontWeight:700,
                    color:MRS_COLORS[s.mrsRegime]||"#d29922"}}>
                    MRS {s.mrs}
                  </span>
                )}
                <RegimeBadge regime={s.mrsRegime||s.regime}
                  label={s.mrsRegimeLabel||(s.mrsRegime||s.regime)} />
              </div>
            </SectionHeader>

            {s.mrs != null ? (
              <>
                {/* Composite score bar */}
                <div style={{marginBottom:10}}>
                  <Row2>
                    <Label>MRS COMPOSITE SCORE</Label>
                    <Value style={{fontSize:20,fontWeight:800,
                      color:MRS_COLORS[s.mrsRegime]||"#d29922"}}>
                      {s.mrs} <span style={{fontSize:12,color:"#6e7681"}}>/100</span>
                    </Value>
                  </Row2>
                  <ScoreBar val={s.mrs} max={100}
                    color={MRS_COLORS[s.mrsRegime]||"#d29922"} />
                  <div style={{display:"flex",justifyContent:"space-between",
                    marginTop:3,fontSize:9,color:"#6e7681"}}>
                    {["0 RISK_OFF","35 CAUTIOUS","50 SELECT.","65 RISK_ON","80 BULL","100"]
                      .map(v=><span key={v}>{v}</span>)}
                  </div>
                </div>

                <Divider />

                {/* Component header row */}
                <div style={{display:"grid",
                  gridTemplateColumns:"32px 1fr 42px 42px 42px",
                  gap:6,fontSize:9,fontWeight:600,letterSpacing:"0.07em",
                  color:"#6e7681",paddingBottom:4}}>
                  <span>Wt.</span><span>Component</span>
                  <span style={{textAlign:"right"}}>Score</span>
                  <span style={{textAlign:"right"}}>/100</span>
                  <span style={{textAlign:"right"}}>Pts</span>
                </div>

                {/* Component 1: Market Breadth */}
                <MRSRow
                  weight="40%"
                  label="Market Breadth"
                  detail={s.mrsBreadth!=null
                    ? `${s.mrsBreadth}% above 200DMA · ${breadthLabel(s.mrsBreadthScore)}`
                    : "Pending"}
                  score={s.mrsBreadthScore}
                  mul="×0.40"
                  pts={s.mrsComponents?.breadth}
                  accentColor={MRS_COLORS[s.mrsRegime]}
                />

                {/* Component 2: Index Trend */}
                <MRSRow
                  weight="30%"
                  label="Index Trend (Nifty 50)"
                  detail={`${s.mrsIndexTrendLabel||"—"}${s.mrsNiftyRsi!=null
                    ? " · RSI "+s.mrsNiftyRsi.toFixed(1):""} — SMA stack`}
                  score={s.mrsIndexTrend}
                  mul="×0.30"
                  pts={s.mrsComponents?.indexTrend}
                  accentColor={MRS_COLORS[s.mrsRegime]}
                />

                {/* Component 3: India VIX */}
                <MRSRow
                  weight="20%"
                  label="India VIX"
                  detail={s.vix!=null ? `VIX ${s.vix} · 8-band scoring` : "Unavailable"}
                  score={s.mrsVixScore}
                  mul="×0.20"
                  pts={s.mrsComponents?.vix}
                  accentColor={MRS_COLORS[s.mrsRegime]}
                />

                {/* Component 4: Distribution Days */}
                <MRSRow
                  weight="10%"
                  label="Distribution Days (25d)"
                  detail={s.mrsDistDays!=null
                    ? `${s.mrsDistDays} day${s.mrsDistDays!==1?"s":""} · ${ddLabel(s.mrsDistDays)}${s.mrsDistDays>=6?" ⚠ IBD":""}`
                    : "Pending"}
                  score={s.mrsDistDayScore}
                  mul="×0.10"
                  pts={s.mrsComponents?.distDays}
                  accentColor={MRS_COLORS[s.mrsRegime]}
                  noBorder
                />

                {/* Formula total */}
                <div style={{display:"flex",justifyContent:"flex-end",gap:8,
                  paddingTop:8,borderTop:"1px solid rgba(255,255,255,0.06)"}}>
                  <span style={{fontSize:10,color:"#6e7681",fontWeight:600,
                    marginRight:"auto"}}>
                    MRS = 0.40×B + 0.30×T + 0.20×V + 0.10×D
                  </span>
                  <span style={{fontSize:13,fontWeight:800,
                    color:MRS_COLORS[s.mrsRegime]||"#d29922"}}>= {s.mrs}</span>
                </div>
              </>
            ) : (
              /* Fallback: legacy display when MRS fields absent */
              <InfoGrid cols={3}>
                <InfoCell label="INDIA VIX"
                  value={s.vix??'—'} color="#3fb950" />
                <InfoCell label="REGIME"
                  value={s.regime||'—'}
                  color={REGIME_COLOR[s.regime]||"#d29922"} />
                <InfoCell label="MRS STATUS"
                  value="Run updated scan"
                  color="#f0883e" />
              </InfoGrid>
            )}

            {/* Action callout */}
            <Callout color={MRS_COLORS[s.mrsRegime]||"#d29922"}>
              <span style={{fontSize:15}}>
                {{"BULL":"🚀","RISK_ON":"✅","SELECTIVE":"👀",
                  "CAUTIOUS":"⚠️","RISK_OFF":"🔴"}[s.mrsRegime]||"⚪"}
              </span>
              <span>
                <strong style={{color:MRS_COLORS[s.mrsRegime]||"#d29922"}}>
                  {s.mrsRegimeLabel||s.regime}
                </strong>
                {" — "}
                {MRS_ACTION[s.mrsRegime]||"Assessing market conditions…"}
              </span>
            </Callout>
          </Section>
"""

# ── Part 8 replacement block ──────────────────────────────────────────────────
PART8_JSX = r"""
          {/* ── PART 8: CPM — CAPITAL PROTECTION MODE (MRS-driven) ─────────── */}
          <Section>
            <SectionHeader>
              <SectionTitle>Part 8 — CPM (Capital Protection Mode)</SectionTitle>
              <CPMBadge pct={s.deploymentPct} label={s.cpmLabel} />
            </SectionHeader>

            {/* Main metrics */}
            <InfoGrid cols={3}>
              <InfoCell label="DEPLOY %"
                value={s.deploymentPct!=null?`${s.deploymentPct}%`:'—'}
                color={CPM_COLORS[s.cpmLabel]||"#d29922"}
                large />
              <InfoCell label="CPM MODE"
                value={s.cpmLabel||'—'}
                color={CPM_COLORS[s.cpmLabel]||"#d29922"} />
              <InfoCell label="MRS REGIME"
                value={s.mrsRegimeLabel||s.regime||'—'}
                color={MRS_COLORS[s.mrsRegime||s.regime]||"#d29922"}
                sub={s.mrs!=null?`MRS ${s.mrs} / 100`:null} />
            </InfoGrid>

            {/* Deploy bar */}
            <div style={{marginTop:8,marginBottom:2}}>
              <ScoreBar val={s.deploymentPct||0} max={100}
                color={CPM_COLORS[s.cpmLabel]||"#d29922"} />
            </div>
            <div style={{display:"flex",justifyContent:"space-between",
              fontSize:9,color:"#6e7681",marginBottom:8}}>
              <span>0 HOLD</span><span>30</span>
              <span>50</span><span>75</span><span>100 FULL</span>
            </div>

            {/* Step-by-step derivation (only when MRS available) */}
            {s.mrsRegime && (
              <>
                <Divider />
                <div style={{fontSize:10,color:"#6e7681",fontWeight:600,
                  letterSpacing:"0.07em",marginBottom:8}}>
                  CPM DERIVATION — compute_cpm_v2()
                </div>
                {[
                  {
                    n:"1", label:"MRS Regime Base",
                    detail:`${s.mrsRegime} (MRS ${s.mrs??'—'}) → base ${
                      {BULL:100,RISK_ON:80,SELECTIVE:55,CAUTIOUS:30,RISK_OFF:10}
                      [s.mrsRegime]??55}%`,
                    result:`${
                      {BULL:100,RISK_ON:80,SELECTIVE:55,CAUTIOUS:30,RISK_OFF:10}
                      [s.mrsRegime]??55}%`,
                    color:MRS_COLORS[s.mrsRegime],
                  },{
                    n:"2", label:"Structural Quality Modifier",
                    detail:s.structuralScore!=null
                      ?`${s.structuralScore}/60 (${s.structuralGrade||'—'}) → ×${
                        s.structuralScore>=52?1.00:s.structuralScore>=42?0.90:
                        s.structuralScore>=30?0.75:0.50}`
                      :"Structural score pending",
                    result:s.structuralScore!=null
                      ?`${Math.round(({BULL:100,RISK_ON:80,SELECTIVE:55,
                        CAUTIOUS:30,RISK_OFF:10}[s.mrsRegime]??55) *
                        (s.structuralScore>=52?1.00:s.structuralScore>=42?0.90:
                        s.structuralScore>=30?0.75:0.50))}%`
                      :"—",
                    color:"#c9d1d9",
                  },{
                    n:"3", label:"Tactical Grade Multiplier",
                    detail:s.grade?`Grade ${s.grade} → ×${
                      {"A+":1.00,"A":0.90,"B+":0.75,"B":0.50}[s.grade]??0.6}`
                      :"Grade pending",
                    result:"~computed",
                    color:"#c9d1d9",
                  },{
                    n:"4", label:"Snapped to Standard Level",
                    detail:"0 / 30 / 50 / 75 / 100% tiers",
                    result:`${s.deploymentPct??'—'}% → ${s.cpmLabel||'—'}`,
                    color:CPM_COLORS[s.cpmLabel]||"#d29922",
                  },
                ].map(row=>(
                  <div key={row.n} style={{display:"grid",
                    gridTemplateColumns:"20px 1fr auto",
                    gap:8,padding:"5px 0",alignItems:"center",
                    borderBottom:row.n!=="4"
                      ?"1px solid rgba(255,255,255,0.04)":"none"}}>
                    <span style={{width:20,height:20,borderRadius:"50%",
                      background:"rgba(255,255,255,0.06)",display:"flex",
                      alignItems:"center",justifyContent:"center",
                      fontSize:10,fontWeight:700,color:"#8b949e"}}>
                      {row.n}
                    </span>
                    <div>
                      <div style={{fontSize:12,color:"#c9d1d9",fontWeight:500}}>
                        {row.label}
                      </div>
                      <div style={{fontSize:10,color:"#6e7681"}}>{row.detail}</div>
                    </div>
                    <span style={{fontSize:12,fontWeight:700,color:row.color,
                      whiteSpace:"nowrap"}}>
                      {row.result}
                    </span>
                  </div>
                ))}
              </>
            )}

            {/* Guidance callout */}
            <Callout color={CPM_COLORS[s.cpmLabel]||"#d29922"}>
              <span style={{color:CPM_COLORS[s.cpmLabel]||"#d29922",fontSize:14}}>
                ●
              </span>
              <span style={{fontSize:12,color:"#c9d1d9"}}>
                {CPM_GUIDANCE[s.cpmLabel]?.(s.mrsRegimeLabel||s.regime)
                  ||"Awaiting MRS data from scan output."}
              </span>
            </Callout>
          </Section>
"""

# ── Helper constants to inject before the component function ──────────────────
MRS_HELPERS_JS = r"""
// ── MRS helper constants (injected by patch_matrix_jsx.py) ───────────────────
const MRS_COLORS = {
  BULL:      "#3fb950",
  RISK_ON:   "#58a6ff",
  SELECTIVE: "#d29922",
  CAUTIOUS:  "#f0883e",
  RISK_OFF:  "#f85149",
};
const CPM_COLORS = {
  FULL:       "#3fb950",
  AGGRESSIVE: "#58a6ff",
  PARTIAL:    "#d29922",
  CAUTIOUS:   "#f0883e",
  HOLD:       "#f85149",
};
const REGIME_COLOR = {
  RISK_ON:   "#58a6ff",
  SELECTIVE: "#d29922",
  RISK_OFF:  "#f85149",
};
const MRS_ACTION = {
  BULL:      "Full deployment eligible — all Grade A/A+ with T1/T2 quality valid.",
  RISK_ON:   "Standard deployment — Grade A/A+ with Tier 1/2 quality valid.",
  SELECTIVE: "Raise quality bar — Grade A or A+ only. No B+ entries.",
  CAUTIOUS:  "Minimal new entries — Q1 A+ setups only. Protect existing gains.",
  RISK_OFF:  "Capital protection — no new entries. Review holdings for exit triggers.",
};
const CPM_GUIDANCE = {
  FULL:       (r) => `Full allocation. ${r} — highest conviction setups active.`,
  AGGRESSIVE: (r) => `Deploy 75%. ${r} — add remaining on breakout confirmation.`,
  PARTIAL:    (r) => `Deploy 50%. ${r} — Tranche 1 only. Wait for LTIA before adding.`,
  CAUTIOUS:   (r) => `Deploy 30%. ${r} — Q1 A+ setups only. Protect existing gains.`,
  HOLD:       (r) => `Hold cash. ${r} — no new entries. Review for exit triggers.`,
};
const breadthLabel = (score) =>
  score==null?"—":score>=80?"Broad Bull":score>=60?"Healthy":
  score>=40?"Mixed":score>=20?"Weak":"Deep Bear";
const ddLabel = (count) =>
  count==null?"—":count<=1?"Clean":count<=3?"Minimal":
  count<=5?"Some":count<=7?"IBD Warning":count<=9?"Heavy":"Critical";
const scoreBandColor = (s) =>
  s>=80?"#3fb950":s>=60?"#58a6ff":s>=40?"#d29922":s>=20?"#f0883e":"#f85149";

// MRS component row
const MRSRow = ({weight,label,detail,score,mul,pts,accentColor,noBorder}) => (
  <div style={{display:"grid",
    gridTemplateColumns:"32px 1fr 42px 42px 42px",
    alignItems:"center",gap:6,padding:"6px 0",
    borderBottom:noBorder?"none":"1px solid rgba(255,255,255,0.04)"}}>
    <span style={{fontSize:10,color:"#6e7681",fontWeight:700}}>{weight}</span>
    <div>
      <div style={{fontSize:12,color:"#c9d1d9",fontWeight:500}}>{label}</div>
      <div style={{height:4,background:"rgba(255,255,255,0.06)",borderRadius:3,
        overflow:"hidden",margin:"3px 0 2px"}}>
        <div style={{width:`${Math.min(100,Math.max(0,score??0))}%`,height:"100%",
          borderRadius:3,background:scoreBandColor(score??0),
          transition:"width 0.4s ease"}} />
      </div>
      <div style={{fontSize:9,color:"#6e7681"}}>{detail}</div>
    </div>
    <span style={{fontSize:11,fontWeight:700,textAlign:"right",
      color:scoreBandColor(score??0)}}>{score??'—'}</span>
    <span style={{fontSize:10,textAlign:"right",color:"#6e7681"}}>{mul}</span>
    <span style={{fontSize:11,fontWeight:700,textAlign:"right",
      color:accentColor||"#d29922"}}>
      {pts!=null?`+${pts}`:"—"}
    </span>
  </div>
);

// Regime badge
const RegimeBadge = ({regime,label}) => (
  <span style={{
    fontSize:10,fontWeight:700,letterSpacing:"0.06em",textTransform:"uppercase",
    color:MRS_COLORS[regime]||"#d29922",
    background:`${MRS_COLORS[regime]||"#d29922"}18`,
    border:`1px solid ${MRS_COLORS[regime]||"#d29922"}`,
    borderRadius:6,padding:"2px 8px",
  }}>{label||regime||"—"}</span>
);

// CPM badge
const CPMBadge = ({pct,label}) => (
  <span style={{
    fontSize:10,fontWeight:700,letterSpacing:"0.06em",textTransform:"uppercase",
    color:CPM_COLORS[label]||"#d29922",
    background:`${CPM_COLORS[label]||"#d29922"}18`,
    border:`1px solid ${CPM_COLORS[label]||"#d29922"}`,
    borderRadius:6,padding:"2px 8px",
  }}>{pct!=null?`${pct}% `:""}{label||"—"}</span>
);
// ── end MRS helpers ───────────────────────────────────────────────────────────
"""

# ══════════════════════════════════════════════════════════════════════════════
# PATCHING LOGIC
# ══════════════════════════════════════════════════════════════════════════════

def get_file():
    """Fetch Matrix182.jsx content and SHA from GitHub."""
    r = requests.get(BASE_URL, headers=HEADERS, params={"ref": BRANCH}, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"GET failed: {r.status_code} — {r.text[:300]}")
    data = r.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    sha     = data["sha"]
    print(f"  Fetched Matrix182.jsx — {len(content):,} chars, SHA {sha[:8]}…")
    return content, sha


def push_file(content, sha, message):
    """Push updated content back to GitHub."""
    encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
    payload = {
        "message": message,
        "content": encoded,
        "sha":     sha,
        "branch":  BRANCH,
    }
    r = requests.put(BASE_URL, headers=HEADERS, json=payload, timeout=30)
    ok = r.status_code in (200, 201)
    print(f"  Push {'OK ✅' if ok else 'FAILED ❌'} ({r.status_code})")
    if not ok:
        print(f"  {r.text[:500]}")
    return ok


def inject_helpers(content):
    """
    Inject MRS_HELPERS_JS constants before the main export default function.
    Safe to re-run: replaces existing injected block if present.
    """
    # Remove any previous injection
    marker_start = "// ── MRS helper constants (injected by patch_matrix_jsx.py) ───────────────────"
    marker_end   = "// ── end MRS helpers ───────────────────────────────────────────────────────────"
    if marker_start in content:
        start_i = content.index(marker_start)
        end_i   = content.index(marker_end) + len(marker_end) + 1
        content = content[:start_i] + content[end_i:]
        print("  Removed previous MRS helper injection")

    # Find insertion point: just before `export default function` or `function Matrix`
    patterns = [
        r"(export default function \w+)",
        r"(export default \(\) =>)",
        r"(function Matrix\w+\s*\()",
        r"(const Matrix\w+\s*=)",
    ]
    for pat in patterns:
        m = re.search(pat, content)
        if m:
            idx = m.start()
            content = content[:idx] + MRS_HELPERS_JS + "\n" + content[idx:]
            print(f"  ✅ Injected MRS helpers before '{m.group(0)[:40]}…'")
            return content

    print("  ⚠️  Could not find insertion point for helpers — prepending to file")
    content = MRS_HELPERS_JS + "\n" + content
    return content


def replace_section(content, part_num, new_jsx):
    """
    Replace a Part N section in Matrix182.jsx.
    Strategy 1: match existing {/* Part N: … */} comment blocks
    Strategy 2: match the rendered section title string
    Strategy 3: warn and skip
    """
    # Strategy 1: JSX comment markers
    patterns_s1 = [
        rf'(\{{\/\*[^*]*Part\s*{part_num}[^*]*\*\/\}}[\s\S]*?)(?=\{{\/\*[^*]*Part\s*{part_num+1})',
        rf'(\{{\/\*[^*]*PART\s*{part_num}[^*]*\*\/\}}[\s\S]*?)(?=\{{\/\*[^*]*PART\s*{part_num+1})',
    ]
    for pat in patterns_s1:
        m = re.search(pat, content)
        if m:
            content = content[:m.start()] + new_jsx + content[m.end():]
            print(f"  ✅ Part {part_num} replaced via comment marker")
            return content, True

    # Strategy 2: section title text
    title_map = {
        7: ["Part 7", "PART 7", "Market Regime", "MARKET REGIME",
            "India VIX", "INDIA VIX"],
        8: ["Part 8", "PART 8", "CPM", "Capital Protection", "CAPITAL PROTECTION",
            "Deploy %", "DEPLOY %", "deploymentPct"],
    }
    for anchor in title_map.get(part_num, []):
        if anchor in content:
            # Find the enclosing <Section> … </Section> block
            idx = content.index(anchor)
            # Walk back to find the opening <Section>
            search_back = content[:idx]
            open_tag_idx = search_back.rfind("<Section>")
            if open_tag_idx == -1:
                open_tag_idx = search_back.rfind("<Section ")
            if open_tag_idx != -1:
                # Walk forward to find the matching </Section>
                depth = 0
                i = open_tag_idx
                while i < len(content):
                    if content[i:i+8] == "<Section":
                        depth += 1
                    elif content[i:i+10] == "</Section>":
                        depth -= 1
                        if depth == 0:
                            close_end = i + 10
                            content = content[:open_tag_idx] + new_jsx.strip() + "\n          " + content[close_end:]
                            print(f"  ✅ Part {part_num} replaced via anchor '{anchor}'")
                            return content, True
                    i += 1

    print(f"  ⚠️  Part {part_num}: no replacement pattern matched — skipped")
    print(f"       Add a comment {{/* ── PART {part_num}: … */}} in Matrix182.jsx")
    print(f"       then re-run this script.")
    return content, False


def patch(content):
    """Apply all patches to content string. Returns patched content."""
    replaced = {}

    # 1. Inject shared helpers
    content = inject_helpers(content)

    # 2. Replace Part 7
    content, replaced[7] = replace_section(content, 7, PART7_JSX)

    # 3. Replace Part 8
    content, replaced[8] = replace_section(content, 8, PART8_JSX)

    return content, replaced


def show_diff_summary(original, patched):
    orig_lines   = original.count("\n")
    patched_lines= patched.count("\n")
    print(f"\n  Lines: {orig_lines} → {patched_lines} ({patched_lines-orig_lines:+d})")
    print(f"  Chars: {len(original):,} → {len(patched):,} ({len(patched)-len(original):+,})")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("patch_matrix_jsx.py — MRS Part 7 + Part 8 patcher")
    print(f"Mode     : {'DRY RUN (no push)' if DRY_RUN else 'LIVE (will push)'}")
    print(f"Target   : {GUSER}/{FE_REPO}/src/Matrix182.jsx")
    print(f"PAT      : {'SET' if PAT else '⚠️  NOT SET — will fail on push'}")
    print("=" * 60)

    if not PAT and not DRY_RUN:
        print("ERROR: PAT_TOKEN environment variable is not set.")
        print("       Set it via: export PAT_TOKEN=ghp_xxxx")
        print("       Or run with: PAT_TOKEN=ghp_xxxx python scripts/patch_matrix_jsx.py")
        sys.exit(1)

    # Fetch
    print("\n1. Fetching Matrix182.jsx…")
    try:
        original, sha = get_file()
    except Exception as e:
        print(f"   FAILED: {e}")
        sys.exit(1)

    # Patch
    print("\n2. Applying patches…")
    patched, replaced = patch(original)
    show_diff_summary(original, patched)

    n_replaced = sum(replaced.values())
    if n_replaced == 0:
        print("\n⚠️  No sections were replaced. Matrix182.jsx may need manual markers.")
        print("   Add these comment lines to their respective sections:")
        print("     {/* ── PART 7: MARKET REGIME SCORE (MRS 4-Factor) ────────────────── */}")
        print("     {/* ── PART 8: CPM — CAPITAL PROTECTION MODE (MRS-driven) ─────────── */}")
        sys.exit(0)

    if DRY_RUN:
        print("\n3. DRY RUN — patched content preview (first 500 chars of MRS section):")
        if "MRS_COLORS" in patched:
            idx = patched.index("MRS_COLORS")
            print(patched[idx:idx+500])
        print("\n   (No changes pushed. Remove --dry-run to apply.)")
        sys.exit(0)

    # Push
    print("\n3. Pushing updated Matrix182.jsx…")
    msg = (f"feat: MRS Part 7 + CPM Part 8 — 4-factor regime score "
           f"({datetime.now():%Y-%m-%d %H:%M})")
    ok = push_file(patched, sha, msg)

    print("\n" + "=" * 60)
    if ok:
        print("✅ DONE — Matrix182.jsx updated. Vercel will auto-deploy.")
        print(f"   Replaced: {[k for k,v in replaced.items() if v]}")
        print("   Frontend: https://matrix181-frontend.vercel.app")
    else:
        print("❌ Push failed. Check PAT_TOKEN permissions (need contents:write on frontend repo).")
    print("=" * 60)


if __name__ == "__main__":
    main()
