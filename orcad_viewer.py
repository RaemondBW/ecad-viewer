#!/usr/bin/env python3
"""
orcad_viewer.py — Generate a self-contained schematic web view from an OrCAD
Capture .DSN, rendered from the design's *native* geometry (real part positions
and wire routing parsed directly from the binary), not an auto-layout.

    python schematic-viewer/orcad_viewer.py design.DSN [-o out.html] [--diff old.DSN]

With --diff, two .DSN versions are compared and the changes are overlaid on the
new design's geometry (added / removed / changed parts and nets).
"""

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))
import orcad_convert as oc  # noqa: E402


def _component_index(design):
    """Flat {designator: {pkg, page, nets:set}} for diffing."""
    idx = {}
    for pageid, page in design["pages"].items():
        names = page["net_names"]
        pt = page["point_nets"]
        for inst in page["instances"]:
            nets = set()
            for _, x, y in inst["pins"]:
                nid = pt.get((x, y))
                if nid is not None:
                    nets.add(names.get(nid) or f"N${nid:08x}")
            idx[inst["designator"]] = {
                "pkg": inst["package"], "page": pageid, "nets": nets,
            }
    return idx


def compute_diff(old_design, new_design):
    """Component-level diff keyed by designator; also flags net changes."""
    old = _component_index(old_design)
    new = _component_index(new_design)
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = {}
    for des in sorted(set(old) & set(new)):
        o, n = old[des], new[des]
        reasons = []
        if o["pkg"] != n["pkg"]:
            reasons.append(f"package {o['pkg']} → {n['pkg']}")
        if o["nets"] != n["nets"]:
            gone = o["nets"] - n["nets"]
            got = n["nets"] - o["nets"]
            if gone:
                reasons.append("nets removed: " + ", ".join(sorted(gone)))
            if got:
                reasons.append("nets added: " + ", ".join(sorted(got)))
        if reasons:
            changed[des] = reasons
    return {"added": added, "removed": removed, "changed": changed}


def build_model(dsn_path, diff_path=None):
    design = oc.load_dsn(dsn_path)
    model = oc.build_sheets(design)
    model["name"] = Path(dsn_path).stem
    if diff_path:
        old_design = oc.load_dsn(diff_path)
        _attach_diff(model, old_design, design, Path(diff_path).stem)
    return model


def _attach_diff(model, old_design, new_design, old_name):
    """Compute the diff and, for removed parts, their old-sheet geometry so the
    viewer can draw them as ghosts where they used to sit."""
    diff = compute_diff(old_design, new_design)
    diff["old"] = old_name
    model["diff"] = diff
    removed = set(diff["removed"])
    old_sheets = oc.build_sheets(old_design)["sheets"]
    ghosts = {}
    for s in old_sheets:
        gs = [{"des": p["des"], "box": p["box"]}
              for p in s["parts"] if p["des"] in removed and p["box"]]
        if gs:
            ghosts[s["id"]] = gs
    model["removedGeom"] = ghosts


def generate(dsn_path, out_path, diff_path=None):
    model = build_model(dsn_path, diff_path)
    html = HTML_TEMPLATE.replace("__MODEL__", json.dumps(model))
    Path(out_path).write_text(html)
    n = sum(len(s["parts"]) for s in model["sheets"])
    print(f"Wrote {out_path}  ({len(model['sheets'])} sheets, {n} parts, "
          f"{Path(out_path).stat().st_size // 1024} KB)")
    if diff_path:
        d = model["diff"]
        print(f"  diff vs {d['old']}: +{len(d['added'])} "
              f"-{len(d['removed'])} ~{len(d['changed'])}")


# ─────────────────────────────────────────────────────────────────────────────
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OrCAD Schematic Viewer</title>
<style>
:root{
  --paper:#fbfaf7; --paper-2:#f4f2ec; --panel:#f2f0ea; --panel-2:#e8e5dc;
  --ink:#161512; --ink-2:#56524a; --ink-3:#8d887d;
  --line:#ddd9cf; --line-2:#c7c1b4; --accent:#c2410c;
  --wire:#7a1fa8; --wire-hi:#c2410c; --pin:#b00000; --label:#8a6d3b;
  --netlabel:#1a45c4; --junction:#b00000; --ink-tb:#111;
  --comp-fill:#ffffff; --comp-stroke:#9a5300; --comp:#9a5300; --comp-hi:#c2410c;
  --grid:rgba(0,0,0,0.04);
  --d-add:#1a7f37; --d-add-soft:rgba(26,127,55,.14);
  --d-del:#cf222e; --d-del-soft:rgba(207,34,46,.13);
  --d-chg:#9a6700; --d-chg-soft:rgba(154,103,0,.16);
  --font-ui:'IBM Plex Sans',system-ui,-apple-system,sans-serif;
  --font-mono:'IBM Plex Mono','Menlo',monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{font-family:var(--font-ui);color:var(--ink);background:var(--paper);
  display:flex;flex-direction:column;overflow:hidden}
header{display:flex;align-items:center;gap:14px;padding:8px 14px;
  border-bottom:1px solid var(--line);background:var(--panel);flex-shrink:0}
header .title{font-weight:600;font-size:14px}
header .sub{color:var(--ink-3);font-size:12px;font-family:var(--font-mono)}
header .spacer{flex:1}
.search{display:flex;align-items:center;gap:6px}
.search input{font-family:var(--font-mono);font-size:12px;padding:4px 8px;
  border:1px solid var(--line-2);border-radius:6px;background:var(--paper);
  color:var(--ink);width:180px}
button.tool{background:var(--paper);border:1px solid var(--line-2);border-radius:6px;
  color:var(--ink);padding:4px 10px;font-size:12px;cursor:pointer;font-family:var(--font-ui)}
button.tool:hover{border-color:var(--ink-3)}
#main{display:flex;flex:1;min-height:0}
#nav{width:210px;flex-shrink:0;border-right:1px solid var(--line);
  background:var(--panel);overflow-y:auto;padding:6px 0}
.nav-h{font-size:10px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;
  color:var(--ink-3);padding:8px 12px 4px}
.sheet{padding:6px 12px;font-size:12px;cursor:pointer;border-left:2px solid transparent;
  display:flex;justify-content:space-between;gap:6px;align-items:baseline}
.sheet:hover{background:var(--paper-2)}
.sheet.active{background:var(--paper-2);border-left-color:var(--accent);font-weight:600}
.sheet .cnt{color:var(--ink-3);font-size:10px;font-family:var(--font-mono)}
#stage{flex:1;position:relative;overflow:hidden;background:var(--paper)}
#svg{width:100%;height:100%;display:block;cursor:grab;touch-action:none}
#svg.panning{cursor:grabbing}
.frame{fill:none;stroke:var(--line-2);stroke-width:1.5;vector-effect:non-scaling-stroke}
.glyph{stroke:var(--ink-3);stroke-width:1;fill:none;vector-effect:non-scaling-stroke}
.gbox{fill:none;stroke:var(--line-2);stroke-width:1;vector-effect:non-scaling-stroke}
.note{fill:var(--ink-2);font-family:var(--font-ui)}
.wire{stroke:var(--wire);stroke-width:1;fill:none;vector-effect:non-scaling-stroke}
.pin{fill:var(--pin)}
.comp rect{fill:var(--comp-fill);stroke:var(--comp);stroke-width:1;
  vector-effect:non-scaling-stroke}
.comp .sym{fill:none;stroke:var(--comp);stroke-width:1.4;vector-effect:non-scaling-stroke;
  stroke-linejoin:round;stroke-linecap:round}
.comp .sym.fill{fill:var(--comp)}
.comp .lead{stroke:var(--comp);stroke-width:1;vector-effect:non-scaling-stroke}
.comp .hit{fill:transparent;stroke:none}
.comp text{fill:var(--comp);font-family:var(--font-mono);text-anchor:middle;
  dominant-baseline:middle}
.comp .lbl{font-family:var(--font-mono);fill:var(--comp)}
.comp .val{font-family:var(--font-mono);fill:var(--comp)}
.comp .pinname{font-family:var(--font-mono);fill:var(--comp)}
.comp .pinnum{font-family:var(--font-mono);fill:var(--ink-3)}
.nlabel{fill:var(--netlabel);font-family:var(--font-mono);dominant-baseline:middle}
.junction{fill:var(--junction);stroke:none}
.flag{fill:none;stroke:var(--comp);stroke-width:1.2;vector-effect:non-scaling-stroke;stroke-linejoin:round;stroke-linecap:round}
.flag.fill{fill:var(--comp)}
.flabel{fill:var(--netlabel);font-family:var(--font-mono)}
.flagg.hot .flag{stroke:var(--wire-hi)}
.flagg.hot .flag.fill{fill:var(--wire-hi)}
.flagg.hot .flabel{fill:var(--wire-hi);font-weight:700}
/* page frame + zone-reference grid border */
.pborder{fill:none;stroke:var(--ink-3);stroke-width:1;vector-effect:non-scaling-stroke}
.pborder.outer{stroke-dasharray:5 3}
.ztick{stroke:var(--ink-3);stroke-width:1;vector-effect:non-scaling-stroke}
.zlbl{fill:var(--ink-2);font-family:var(--font-ui);text-anchor:middle;dominant-baseline:central}
/* title block */
.tb-cell{fill:#fff;stroke:var(--ink-tb);stroke-width:1;vector-effect:non-scaling-stroke}
.tb-lbl{fill:var(--ink-3);font-family:var(--font-ui);dominant-baseline:central}
.tb-val{fill:var(--ink-tb);font-family:var(--font-ui);dominant-baseline:central}
.tb-title{fill:var(--ink-tb);font-family:var(--font-ui);font-weight:600}
.dim{opacity:.18}
.wire.hot{stroke:var(--wire-hi);stroke-width:2.2}
.pin.hot{fill:var(--wire-hi)}
.nlabel.hot{fill:var(--wire-hi);font-weight:700}
.comp.hot rect{stroke:var(--comp-hi);stroke-width:2}
.comp.add rect{stroke:var(--d-add);fill:var(--d-add-soft);stroke-width:1.6}
.comp.chg rect{stroke:var(--d-chg);fill:var(--d-chg-soft);stroke-width:1.6}
.comp.ghost rect{stroke:var(--d-del);fill:var(--d-del-soft);stroke-dasharray:4 3}
#tip{position:absolute;pointer-events:none;background:var(--ink);color:#fff;
  font-family:var(--font-mono);font-size:11px;padding:4px 7px;border-radius:5px;
  opacity:0;transition:opacity .1s;max-width:320px;z-index:5;white-space:pre-line}
#info{position:absolute;left:10px;bottom:10px;font-family:var(--font-mono);
  font-size:11px;color:var(--ink-3);background:var(--panel);border:1px solid var(--line);
  border-radius:6px;padding:5px 8px;pointer-events:none}
#diffbar{display:none;align-items:center;gap:10px;font-size:12px;font-family:var(--font-mono)}
#diffbar .chip{padding:2px 7px;border-radius:10px}
.chip.add{background:var(--d-add-soft);color:var(--d-add)}
.chip.del{background:var(--d-del-soft);color:var(--d-del)}
.chip.chg{background:var(--d-chg-soft);color:var(--d-chg)}
.legend{position:absolute;right:10px;top:10px;font-family:var(--font-mono);font-size:11px;
  background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:6px 8px;display:none}
.legend div{display:flex;align-items:center;gap:6px}.legend i{width:10px;height:10px;border-radius:2px;display:inline-block}
</style></head>
<body>
<header>
  <span class="title" id="dsn-name">Schematic</span>
  <span class="sub" id="dsn-sub"></span>
  <div id="diffbar"></div>
  <span class="spacer"></span>
  <div class="search"><input id="q" placeholder="find part or net…" autocomplete="off"></div>
  <button class="tool" id="fit">Fit</button>
</header>
<div id="main">
  <div id="nav"><div class="nav-h">Sheets</div><div id="sheets"></div></div>
  <div id="stage">
    <svg id="svg"><g id="scene"></g></svg>
    <div id="tip"></div>
    <div id="info"></div>
    <div class="legend" id="legend">
      <div><i style="background:var(--d-add)"></i>added</div>
      <div><i style="background:var(--d-del)"></i>removed</div>
      <div><i style="background:var(--d-chg)"></i>changed</div>
    </div>
  </div>
</div>
<script>
const MODEL = __MODEL__;
const svg = document.getElementById('svg'), scene = document.getElementById('scene');
const tip = document.getElementById('tip'), info = document.getElementById('info');
let cur = 0, view = {x:0,y:0,k:1}, pinnedNet = null;

document.getElementById('dsn-name').textContent = MODEL.name || 'Schematic';
document.getElementById('dsn-sub').textContent = MODEL.sheets.length + ' sheets';

// ── diff summary ──
const diff = MODEL.diff;
if (diff){
  const bar = document.getElementById('diffbar'); bar.style.display='flex';
  bar.innerHTML = `<span>vs ${diff.old}</span>`+
    `<span class="chip add">+${diff.added.length}</span>`+
    `<span class="chip del">−${diff.removed.length}</span>`+
    `<span class="chip chg">~${Object.keys(diff.changed).length}</span>`;
  document.getElementById('legend').style.display='block';
}
const addedSet = new Set(diff? diff.added : []);
const chgSet = new Set(diff? Object.keys(diff.changed) : []);

// ── sheet nav ──
const sheetsEl = document.getElementById('sheets');
MODEL.sheets.forEach((s,i)=>{
  const el=document.createElement('div'); el.className='sheet'; el.dataset.i=i;
  el.innerHTML=`<span>${s.view}${s.page&&s.page!==s.view?' · '+s.page:''}</span>`+
    `<span class="cnt">${s.parts.length}p</span>`;
  el.onclick=()=>selectSheet(i);
  sheetsEl.appendChild(el);
});

function esc(s){return (s+'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

// Draw the OrCAD-style page border with a zone-reference grid (numbers along
// top/bottom, letters along left/right, ticks between an outer and inner frame).
function borderSVG(fr){
  const[fx,fy,fX,fY]=fr, W=fX-fx, H=fY-fy, D=22;
  const ix=fx+D, iy=fy+D, iX=fX-D, iY=fY-D;
  const ncols=Math.max(4,Math.min(12,Math.round(W/210)));
  const nrows=Math.max(3,Math.min(10,Math.round(H/210)));
  let s=`<rect class="pborder outer" x="${fx}" y="${fy}" width="${W}" height="${H}"/>`+
        `<rect class="pborder" x="${ix}" y="${iy}" width="${iX-ix}" height="${iY-iy}"/>`;
  const fs=Math.min(D*0.7,14);
  for(let i=0;i<ncols;i++){
    const x0=ix+i*(iX-ix)/ncols, xc=x0+(iX-ix)/ncols/2;
    if(i>0) s+=`<line class="ztick" x1="${x0}" y1="${fy}" x2="${x0}" y2="${iy}"/>`+
               `<line class="ztick" x1="${x0}" y1="${iY}" x2="${x0}" y2="${fY}"/>`;
    const n=(ncols-i); // OrCAD numbers increase right→left
    s+=`<text class="zlbl" x="${xc}" y="${fy+D/2}" font-size="${fs}">${n}</text>`+
       `<text class="zlbl" x="${xc}" y="${fY-D/2}" font-size="${fs}">${n}</text>`;
  }
  for(let j=0;j<nrows;j++){
    const y0=iy+j*(iY-iy)/nrows, yc=y0+(iY-iy)/nrows/2;
    if(j>0) s+=`<line class="ztick" x1="${fx}" y1="${y0}" x2="${ix}" y2="${y0}"/>`+
               `<line class="ztick" x1="${iX}" y1="${y0}" x2="${fX}" y2="${y0}"/>`;
    const L=String.fromCharCode(65+(nrows-1-j)); // letters increase bottom→top
    s+=`<text class="zlbl" x="${fx+D/2}" y="${yc}" font-size="${fs}">${L}</text>`+
       `<text class="zlbl" x="${fX-D/2}" y="${yc}" font-size="${fs}">${L}</text>`;
  }
  return s;
}

// Draw the extracted title-block table at its bottom-right origin, with fields.
function titleblockSVG(tb, geom){
  if(!tb||!geom) return '';
  const ox=tb.ox, oy=tb.oy;
  let s='';
  for(const r of geom.rects)
    s+=`<rect class="tb-cell" x="${ox+r[0]}" y="${oy+r[1]}" width="${r[2]-r[0]}" height="${r[3]-r[1]}"/>`;
  for(const L of geom.lines)
    s+=`<line class="tb-cell" x1="${ox+L[0]}" y1="${oy+L[1]}" x2="${ox+L[2]}" y2="${oy+L[3]}"/>`;
  for(const t of geom.labels)
    s+=`<text class="tb-lbl" x="${ox+t.x}" y="${oy+t.y}" font-size="9">${esc(t.s)}</text>`;
  // field values — short sheet name centered in the title area (0..200, 0..80)
  const title=(''+tb.title);
  const tfs=Math.max(8,Math.min(15,185/Math.max(title.length*0.6,1)));
  s+=`<text class="tb-title" x="${ox+100}" y="${oy+40}" font-size="${tfs.toFixed(1)}" text-anchor="middle">${esc(title)}</text>`;
  s+=`<text class="tb-val" x="${ox+280}" y="${oy+40}" font-size="11" text-anchor="middle">${esc(tb.company)}</text>`;
  s+=`<text class="tb-val" x="${ox+40}" y="${oy+108}" font-size="10">${esc(tb.size)}</text>`;
  if(tb.rev) s+=`<text class="tb-val" x="${ox+190}" y="${oy+108}" font-size="10">${esc(tb.rev)}</text>`;
  if(tb.date) s+=`<text class="tb-val" x="${ox+40}" y="${oy+130}" font-size="9">${esc(tb.date)}</text>`;
  s+=`<text class="tb-val" x="${ox+248}" y="${oy+131}" font-size="9" text-anchor="middle">${tb.n}</text>`;
  s+=`<text class="tb-val" x="${ox+285}" y="${oy+131}" font-size="9" text-anchor="middle">${tb.total}</text>`;
  return s;
}

// Draw a power/ground/off-page connector glyph at its wire-touch point (x,y),
// pointing outward along the wire (orient u/d/l/r), with the net label.
function flagSVG(f){
  const x=f.x, y=f.y;
  const dir={u:[0,-1],d:[0,1],l:[-1,0],r:[1,0]}[f.orient||'d'];
  const ux=dir[0],uy=dir[1],vx=-uy,vy=ux;
  const P=(t,s)=>[x+ux*t+vx*s, y+uy*t+vy*s];
  const M=p=>p[0].toFixed(1)+' '+p[1].toFixed(1);
  const L=(a,b)=>`<line class="flag" x1="${a[0].toFixed(1)}" y1="${a[1].toFixed(1)}" x2="${b[0].toFixed(1)}" y2="${b[1].toFixed(1)}"/>`;
  const nk=f.key?` data-net="${esc(f.key)}"`:'';
  let g=`<g class="flagg"${nk}>`;
  if(f.kind==='gnd'){
    g+=L(P(0,0),P(5,0))+L(P(5,-6),P(5,6))+L(P(8,-4),P(8,4))+L(P(11,-2),P(11,2));
  } else if(f.kind==='pwr'){
    g+=L(P(0,0),P(5,0))+`<polygon class="flag fill" points="${M(P(5,-4))} ${M(P(5,4))} ${M(P(10,0))}"/>`;
  } else {
    const pts=[P(0,-4),P(9,-4),P(14,0),P(9,4),P(0,4)];
    g+=`<polygon class="flag" points="${pts.map(M).join(' ')}"/>`;
  }
  // net label just beyond the glyph, anchored by direction
  if(f.net){
    const lp=P(f.kind==='port'?17:14,0);
    const anc=f.orient==='l'?'end':(f.orient==='r'?'start':'middle');
    g+=`<text class="flabel" x="${lp[0].toFixed(1)}" y="${lp[1].toFixed(1)}" font-size="8" text-anchor="${anc}" dominant-baseline="central">${esc(f.net)}</text>`;
  }
  return g+`</g>`;
}

// Draw a 2-terminal schematic symbol between pin points a and b.
function symSVG(type,a,b){
  const dx=b[0]-a[0], dy=b[1]-a[1], L=Math.hypot(dx,dy)||1;
  const ux=dx/L, uy=dy/L, vx=-uy, vy=ux;
  const cx=(a[0]+b[0])/2, cy=(a[1]+b[1])/2;
  const bl=Math.min(L*0.32,11), w=5.5;
  const P=(t,s)=>[cx+ux*t+vx*s, cy+uy*t+vy*s];
  const M=(p)=>p[0].toFixed(1)+' '+p[1].toFixed(1);
  const line=(p,q,c='sym')=>`<line class="${c}" x1="${p[0].toFixed(1)}" y1="${p[1].toFixed(1)}" x2="${q[0].toFixed(1)}" y2="${q[1].toFixed(1)}"/>`;
  let svg='', gap=bl;
  if(type==='res'){
    // IEC rectangle body (matches OrCAD)
    const e1=P(-bl,0), e2=P(bl,0), rw=4.5;
    const c=[P(-bl,rw),P(bl,rw),P(bl,-rw),P(-bl,-rw)];
    svg+=`<polygon class="sym" points="${c.map(M).join(' ')}"/>`;
    svg+=line(a,e1,'lead')+line(b,e2,'lead');
  } else if(type==='cap' || type==='cape'){
    gap=3.2; const pw=7;
    const e1=P(-gap,0), e2=P(gap,0);
    svg+=line(P(-gap,-pw),P(-gap,pw));           // plate 1 (straight)
    if(type==='cape'){                            // plate 2 curved (polarized)
      const c1=P(gap,-pw), c2=P(gap,pw), cc=P(gap+3,0);
      svg+=`<path class="sym" d="M ${M(c1)} Q ${M(cc)} ${M(c2)}"/>`;
      const pp=P(-gap-4,-pw-2);                   // '+' marker
      svg+=`<text class="sym fill" x="${pp[0].toFixed(1)}" y="${pp[1].toFixed(1)}" font-size="6" stroke="none" text-anchor="middle" dominant-baseline="central">+</text>`;
    } else {
      svg+=line(P(gap,-pw),P(gap,pw));            // plate 2 (straight)
    }
    svg+=line(a,e1,'lead')+line(b,e2,'lead');
  } else if(type==='ind'){
    const n=4, e1=P(-bl,0), e2=P(bl,0), step=(2*bl)/n, r=(step/2);
    let d=`M ${M(e1)}`;
    for(let i=0;i<n;i++){ const s0=-bl+i*step, s1=s0+step;
      d+=` A ${r.toFixed(1)} ${r.toFixed(1)} 0 0 1 ${M(P(s1,0))}`; }
    svg+=`<path class="sym" d="${d}"/>`;
    svg+=line(a,e1,'lead')+line(b,e2,'lead');
  } else if(type==='diode'){
    const e1=P(-bl,0), e2=P(bl,0);
    const t1=P(-bl,-w), t2=P(-bl,w), apex=P(bl*0.55,0);
    svg+=`<polygon class="sym fill" points="${M(t1)} ${M(t2)} ${M(apex)}"/>`;
    svg+=line(P(bl*0.55,-w),P(bl*0.55,w));        // cathode bar
    svg+=line(a,e1,'lead')+line(b,e2,'lead');
  }
  // invisible hit target along the body
  svg+=`<rect class="hit" x="${(cx-Math.abs(ux)*bl-Math.abs(vx)*w-2).toFixed(1)}" y="${(cy-Math.abs(uy)*bl-Math.abs(vy)*w-2).toFixed(1)}" width="${(2*(Math.abs(ux)*bl+Math.abs(vx)*w+2)).toFixed(1)}" height="${(2*(Math.abs(uy)*bl+Math.abs(vy)*w+2)).toFixed(1)}"/>`;
  const off=w+7, lx=cx+vx*off, ly=cy+vy*off;
  return {svg, lx, ly};
}
function netId(k){return 'n'+btoa(unescape(encodeURIComponent(k))).replace(/[^a-zA-Z0-9]/g,'');}

function selectSheet(i){
  cur=i; pinnedNet=null;
  document.querySelectorAll('.sheet').forEach(e=>e.classList.toggle('active',+e.dataset.i===i));
  render();
  fit();
}

function render(){
  const s = MODEL.sheets[cur];
  let h='';
  // page layout layer (behind everything): zone border, boxes, notes
  const g = s.graphics||{lines:[],rects:[],texts:[],polys:[]};
  if(s.frame) h+=borderSVG(s.frame);
  for(const L of g.lines){ // skip the raw border edge lines (replaced by zone border)
    if(s.frame && (Math.abs(L[0]-L[2])<2 && (Math.abs(L[0]-s.frame[0])<3||Math.abs(L[0]-s.frame[2])<3)) ) continue;
    if(s.frame && (Math.abs(L[1]-L[3])<2 && (Math.abs(L[1]-s.frame[1])<3||Math.abs(L[1]-s.frame[3])<3)) ) continue;
    h+=`<line class="glyph" x1="${L[0]}" y1="${L[1]}" x2="${L[2]}" y2="${L[3]}"/>`;
  }
  for(const r of g.rects) h+=`<rect class="gbox" x="${Math.min(r[0],r[2])}" y="${Math.min(r[1],r[3])}" width="${Math.abs(r[2]-r[0])}" height="${Math.abs(r[3]-r[1])}"/>`;
  for(const pl of g.polys){ if(pl.length<2)continue;
    h+=`<polyline class="glyph" points="${pl.map(p=>p[0]+' '+p[1]).join(' ')}"/>`; }
  for(const t of g.texts){
    const fs=Math.max(8,Math.min(t.h*0.85,40));
    const lines=(''+t.s).split(/\r\n|\n|\r/);
    let ts=`<text class="note" x="${t.x}" y="${t.y+fs*0.8}" font-size="${fs.toFixed(1)}">`;
    lines.forEach((ln,i)=>{ ts+=`<tspan x="${t.x}" dy="${i===0?0:fs*1.15}">${esc(ln)}</tspan>`; });
    h+=ts+`</text>`;
  }
  // wires
  for(const w of s.wires){
    h+=`<line class="wire" data-net="${esc(w[4])}" x1="${w[0]}" y1="${w[1]}" x2="${w[2]}" y2="${w[3]}"/>`;
  }
  // parts
  for(const p of s.parts){
    if(!p.box) continue;
    let cls='comp';
    if(diff){ if(addedSet.has(p.des)) cls+=' add'; else if(chgSet.has(p.des)) cls+=' chg'; }
    h+=`<g class="${cls}" data-des="${esc(p.des)}" data-pkg="${esc(p.pkg)}">`;
    if(p.sym!=='box' && p.pins.length===2){
      const a=p.pins[0], b=p.pins[1];
      const g=symSVG(p.sym,[a[0],a[1]],[b[0],b[1]]);
      h+=g.svg + `<text class="lbl" x="${g.lx}" y="${g.ly}" font-size="10" text-anchor="middle" dominant-baseline="central">${esc(p.des)}</text>`;
    } else {
      const[bx,by,bw,bh]=p.box, cx=bx+bw/2, cy=by+bh/2;
      const named=p.pins.some(pin=>pin[4]);
      const fs=Math.max(8,Math.min(13,Math.min(bw,bh)*0.35));
      h+=`<rect x="${bx}" y="${by}" width="${bw}" height="${bh}" rx="2"/>`;
      // designator: centered if no pin names, else near top so names have room
      h+=`<text x="${cx}" y="${named?by+9:cy}" font-size="${fs}">${esc(p.des)}</text>`;
      // pin names inside the box, adjacent to each pin
      for(const pin of p.pins){
        if(!pin[4]) continue;
        const px=pin[0], py=pin[1];
        const dL=Math.abs(px-bx),dR=Math.abs(px-(bx+bw)),dT=Math.abs(py-by),dB=Math.abs(py-(by+bh));
        const mn=Math.min(dL,dR,dT,dB);
        let tx=px,ty=py,anchor='middle';
        if(mn===dL){tx=px+3;anchor='start';}
        else if(mn===dR){tx=px-3;anchor='end';}
        else if(mn===dT){ty=py+7;}
        else {ty=py-3;}
        h+=`<text class="pinname" x="${tx}" y="${ty}" font-size="7" text-anchor="${anchor}" dominant-baseline="central">${esc(pin[4])}</text>`;
      }
    }
    h+=`</g>`;
    for(const pin of p.pins){
      h+=`<circle class="pin" data-net="${esc(pin[2])}" cx="${pin[0]}" cy="${pin[1]}" r="1.8"/>`;
    }
  }
  // removed parts (ghost) — placed at their old page position if same sheet name
  if(diff){
    const ghosts = (MODEL.removedGeom&&MODEL.removedGeom[s.id])||[];
    for(const g of ghosts){
      const[bx,by,bw,bh]=g.box, cx=bx+bw/2, cy=by+bh/2;
      h+=`<g class="comp ghost" data-des="${esc(g.des)}"><rect x="${bx}" y="${by}" width="${bw}" height="${bh}" rx="2"/>`+
         `<text x="${cx}" y="${cy}" font-size="11">${esc(g.des)}</text></g>`;
    }
  }
  // power/ground/off-page connector glyphs
  for(const f of s.connectors||[]) h+=flagSVG(f);
  // junction dots (electrical ties)
  for(const j of s.junctions||[]) h+=`<circle class="junction" cx="${j[0]}" cy="${j[1]}" r="3"/>`;
  // net labels
  for(const l of s.labels){
    h+=`<text class="nlabel" data-net="${esc(l.key)}" x="${l.x}" y="${l.y-3}" font-size="9">${esc(l.text)}</text>`;
  }
  // title block (bottom-right)
  h+=titleblockSVG(s.tb, MODEL.titleblock);
  scene.innerHTML=h;
  info.textContent=`${s.parts.length} parts · ${s.wires.length} wires · ${Object.keys(s.nets).length} nets`;
  attachHover();
  applyView();
}

// ── hover / highlight ──
function attachHover(){
  scene.querySelectorAll('[data-net]').forEach(el=>{
    el.addEventListener('mouseenter',()=>{ if(!pinnedNet) hiNet(el.dataset.net); });
    el.addEventListener('mouseleave',()=>{ if(!pinnedNet) clearHi(); });
    el.addEventListener('click',e=>{ e.stopPropagation();
      pinnedNet = pinnedNet===el.dataset.net?null:el.dataset.net;
      pinnedNet?hiNet(pinnedNet):clearHi(); });
  });
  scene.querySelectorAll('.comp[data-des]').forEach(el=>{
    el.addEventListener('mousemove',e=>showTip(e, el.dataset.des+(el.dataset.pkg?'\n'+el.dataset.pkg:'')+diffReason(el.dataset.des)));
    el.addEventListener('mouseleave',hideTip);
  });
}
function diffReason(des){
  if(!diff) return '';
  if(addedSet.has(des)) return '\n＋ added';
  if(chgSet.has(des)) return '\n~ '+diff.changed[des].join('\n~ ');
  return '';
}
function hiNet(k){
  scene.classList.add('dimmed');
  scene.querySelectorAll('[data-net]').forEach(el=>{
    const on = el.dataset.net===k && k!=='';
    el.classList.toggle('hot',on);
    el.classList.toggle('dim',!on);
  });
  const s=MODEL.sheets[cur]; const nm=s.nets[k];
  info.textContent = (nm||k) + ' — ' +
    scene.querySelectorAll('.pin.hot').length + ' pins, ' +
    scene.querySelectorAll('.wire.hot').length + ' segments';
}
function clearHi(){
  scene.classList.remove('dimmed');
  scene.querySelectorAll('.hot,.dim').forEach(el=>el.classList.remove('hot','dim'));
  const s=MODEL.sheets[cur];
  info.textContent=`${s.parts.length} parts · ${s.wires.length} wires · ${Object.keys(s.nets).length} nets`;
}
function showTip(e,txt){ tip.textContent=txt; tip.style.opacity=1;
  const r=svg.getBoundingClientRect(); tip.style.left=(e.clientX-r.left+12)+'px'; tip.style.top=(e.clientY-r.top+12)+'px'; }
function hideTip(){ tip.style.opacity=0; }

// ── pan / zoom ──
function applyView(){ scene.setAttribute('transform',`translate(${view.x},${view.y}) scale(${view.k})`); }
let drag=null;
svg.addEventListener('pointerdown',e=>{ if(e.target.closest('[data-net],[data-des]'))return;
  drag={x:e.clientX,y:e.clientY,vx:view.x,vy:view.y}; svg.classList.add('panning'); svg.setPointerCapture(e.pointerId); });
svg.addEventListener('pointermove',e=>{ if(!drag)return; view.x=drag.vx+(e.clientX-drag.x); view.y=drag.vy+(e.clientY-drag.y); applyView(); });
svg.addEventListener('pointerup',e=>{ drag=null; svg.classList.remove('panning'); });
svg.addEventListener('click',e=>{ if(!e.target.closest('[data-net]')&&pinnedNet){pinnedNet=null;clearHi();} });
svg.addEventListener('wheel',e=>{ e.preventDefault();
  const r=svg.getBoundingClientRect(), mx=e.clientX-r.left, my=e.clientY-r.top;
  const f=Math.exp(-e.deltaY*0.0015), nk=Math.min(20,Math.max(0.05,view.k*f));
  view.x=mx-(mx-view.x)*(nk/view.k); view.y=my-(my-view.y)*(nk/view.k); view.k=nk; applyView();
},{passive:false});

// robust fit: ignore coordinate outliers so a stray far object doesn't shrink everything
function fitBox(s){
  if(s.frame) return s.frame;   // fit to the drawn page edge when present
  const xs=[],ys=[];
  for(const w of s.wires){xs.push(w[0],w[2]);ys.push(w[1],w[3]);}
  for(const p of s.parts){if(p.box){xs.push(p.box[0],p.box[0]+p.box[2]);ys.push(p.box[1],p.box[1]+p.box[3]);}}
  if(!xs.length)return s.bbox;
  const pct=(a,q)=>{a=a.slice().sort((u,v)=>u-v);return a[Math.floor((a.length-1)*q)];};
  return [pct(xs,0.01),pct(ys,0.01),pct(xs,0.99),pct(ys,0.99)];
}
function fit(){
  const s=MODEL.sheets[cur]; const b=fitBox(s);
  const r=svg.getBoundingClientRect();
  const w=Math.max(b[2]-b[0],10), h=Math.max(b[3]-b[1],10);
  const pad=40, k=Math.min((r.width-2*pad)/w,(r.height-2*pad)/h);
  view.k=Math.min(k,8);
  view.x=(r.width-(b[0]+b[2])*view.k)/2;
  view.y=(r.height-(b[1]+b[3])*view.k)/2;
  applyView();
}
document.getElementById('fit').onclick=fit;
window.addEventListener('resize',()=>applyView());

// ── search ──
const q=document.getElementById('q');
q.addEventListener('input',()=>{
  const t=q.value.trim().toLowerCase(); if(!t){clearHi();pinnedNet=null;return;}
  // net match first
  const s=MODEL.sheets[cur];
  const netKey=Object.keys(s.nets).find(k=>(s.nets[k]||k).toLowerCase()===t)
            || Object.keys(s.nets).find(k=>(s.nets[k]||k).toLowerCase().includes(t));
  if(netKey){ pinnedNet=netKey; hiNet(netKey); return; }
  // part match: locate & center
  const comp=[...scene.querySelectorAll('.comp[data-des]')].find(e=>e.dataset.des.toLowerCase().includes(t));
  if(comp){ const bb=comp.getBBox(); const r=svg.getBoundingClientRect();
    view.k=Math.min(6,view.k<1?2:view.k);
    view.x=r.width/2-(bb.x+bb.width/2)*view.k; view.y=r.height/2-(bb.y+bb.height/2)*view.k; applyView();
    comp.classList.add('hot'); setTimeout(()=>comp.classList.remove('hot'),1500);
  }
});
q.addEventListener('keydown',e=>{ if(e.key==='Enter'){ /* jump to first matching sheet */
  const t=q.value.trim().toLowerCase(); if(!t)return;
  for(let i=0;i<MODEL.sheets.length;i++){ if(MODEL.sheets[i].parts.some(p=>p.des.toLowerCase().includes(t))){ if(i!==cur)selectSheet(i); setTimeout(()=>q.dispatchEvent(new Event('input')),50); break; } }
}});

selectSheet(0);
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dsn", type=Path, help="OrCAD Capture .DSN")
    ap.add_argument("-o", "--output", type=Path, default=None)
    ap.add_argument("--diff", type=Path, default=None,
                    help="an older .DSN to diff against")
    args = ap.parse_args()
    out = args.output or args.dsn.parent / (args.dsn.stem + "_schematic.html")
    generate(args.dsn, out, args.diff)


if __name__ == "__main__":
    main()
