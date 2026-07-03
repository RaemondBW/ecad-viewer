#!/usr/bin/env python3
"""
brd_viewer.py — a placement webview for a Cadence Allegro .brd board.

Renders every component the .brd parser can place (brd_convert.component_placements)
as a marker at its board XY, coloured by type, with the value from a sibling
OrCAD BOM export shown on hover. This is a first PCB view: placements only
(no footprint outlines / copper / board outline yet — those need more of the
Allegro geometry decoded).

    python schematic-viewer/brd_viewer.py board.brd [-o out.html] [--bom design.BOM]
"""

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))
import brd_convert as bc          # noqa: E402
import orcad_convert as oc        # noqa: E402

# marker colour + half-size (board units) per reference-designator prefix
TYPES = {
    "R": ("Resistor", "#C2410C", 300),
    "C": ("Capacitor", "#2563EB", 300),
    "L": ("Inductor", "#16A34A", 350),
    "D": ("Diode", "#9333EA", 350),
    "U": ("IC", "#0891B2", 1400),
    "Q": ("Transistor", "#DB2777", 700),
    "J": ("Connector", "#CA8A04", 1800),
    "X": ("Connector", "#CA8A04", 1800),
    "Y": ("Crystal", "#0D9488", 700),
    "T": ("Transformer", "#B45309", 1600),
    "F": ("Fuse", "#DC2626", 500),
    "B": ("Jumper", "#8B8578", 300),
}


# copper layer colours by ETCH subclass index (0 = top … high = bottom)
LAYER_COLORS = ["#E24A3B", "#2E7DD1", "#31A354", "#E6A020", "#9C56C4",
                "#17A2A2", "#C94FA0", "#8A9440", "#B5651D", "#6C7BD1"]


def _cap_to_pitch(pads):
    """Shrink only pads that would overlap a neighbour, per axis, using the gap to
    the nearest neighbour that shares this pad's *perpendicular* span. A global
    min-pitch (the old approach) breaks mixed footprints: on a QFN the tiny gap
    between a corner pad and an edge pad collapsed every pad to a sliver. Here a
    QFN's side pads are only bounded by other side pads (same column), not by the
    top row, so real sizes are preserved and only true overlaps are trimmed."""
    if len(pads) < 2:
        return [tuple(p) for p in pads]
    out = []
    for i, (x1, y1, x2, y2, rnd) in enumerate(pads):
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        hw, hh = (x2 - x1) / 2, (y2 - y1) / 2
        dxs, dys = [], []
        for j, p in enumerate(pads):
            if j == i:
                continue
            pcx, pcy = (p[0] + p[2]) / 2, (p[1] + p[3]) / 2
            # ignore sub-pitch gaps (<2500): a real pad pitch here is >=5000, so a
            # tiny centre offset is a placement artifact, not a neighbour to cap to
            if min(y2, p[3]) > max(y1, p[1]) and abs(pcx - cx) > 2500:   # shares y-span → same row
                dxs.append(abs(pcx - cx))
            if min(x2, p[2]) > max(x1, p[0]) and abs(pcy - cy) > 2500:   # shares x-span → same column
                dys.append(abs(pcy - cy))
        if dxs:
            hw = min(hw, min(dxs) * 0.46)
        if dys:
            hh = min(hh, min(dys) * 0.46)
        out.append((int(cx - hw), int(cy - hh), int(cx + hw), int(cy + hh), rnd))
    return out


def build(brd_path, bom_path=None):
    d = Path(brd_path).read_bytes()
    strings = bc.parse_strings(d)
    placements = bc.component_placements(d, strings)
    bom = oc.parse_bom(bom_path) if bom_path and Path(bom_path).exists() else {}
    parts, axs, ays = [], [], []
    for ref, (x, y, side, rot, pads) in placements.items():
        pre = re.match(r"^[A-Za-z]+", ref)
        t = pre.group()[0].upper() if pre else "?"
        pads = _cap_to_pitch(pads)          # keep pads within their neighbour pitch
        # label at the pad centroid (a footprint origin can be far from its pads)
        if pads:
            x = int(sum((p[0] + p[2]) / 2 for p in pads) / len(pads))
            y = int(sum((p[1] + p[3]) / 2 for p in pads) / len(pads))
        parts.append({"ref": ref, "x": x, "y": y, "t": t, "side": side,
                      "val": bom.get(ref, ""), "pads": [list(p) for p in pads]})
        for p in (pads or [(x - 8000, y - 8000, x + 8000, y + 8000)]):
            axs += [p[0], p[2]]; ays += [p[1], p[3]]
    ext = [min(axs), min(ays), max(axs), max(ays)] if axs else [0, 0, 1000, 1000]
    # copper is authoritative (0x05 ETCH tracks → 0x15/16/17 segments). Clip to the
    # board bounding box + margin to drop the odd out-of-board stray segment.
    mx = max((ext[2] - ext[0]), (ext[3] - ext[1])) * 0.02 + 8000
    bb = (ext[0] - mx, ext[1] - mx, ext[2] + mx, ext[3] + mx)
    inb = lambda x, y: bb[0] <= x <= bb[2] and bb[1] <= y <= bb[3]
    copper = [(x1, y1, x2, y2, lay, w) for (x1, y1, x2, y2, lay, w) in bc.copper_segments(d)
              if inb(x1, y1) and inb(x2, y2)]
    # NOTE: copper pour / plane shapes (0x28) are decoded in
    # brd_convert.copper_shapes() but not rendered — the boundary reconstruction
    # isn't reliable enough yet, so we omit them rather than draw wrong fills.
    layers = sorted({s[4] for s in copper})
    via = [v for v in bc.vias(d, bb)]
    return {"name": Path(brd_path).stem, "parts": parts, "extent": ext,
            "copper": [c for s in copper for c in s],      # x1,y1,x2,y2,layer,width × n
            "vias": [c for v in via for c in v],           # x,y,half × n
            "layers": layers,
            "layerColors": {str(l): LAYER_COLORS[i % len(LAYER_COLORS)] for i, l in enumerate(layers)},
            "types": {k: {"label": v[0], "color": v[1], "hs": v[2]} for k, v in TYPES.items()}}


def generate(brd_path, out_path, bom_path=None, xprobe=None, model=None):
    if model is None:
        model = build(brd_path, bom_path)
    html = (HTML.replace("__MODEL__", json.dumps(model))
                .replace("__XPROBE__", json.dumps(xprobe)))
    Path(out_path).write_text(html)
    print(f"Wrote {out_path}  ({len(model['parts'])} components, "
          f"{len(model['copper']) // 5} copper segments, "
          f"{len(model['layers'])} layers, {Path(out_path).stat().st_size // 1024} KB)")


HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PCB Placement Viewer</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
html,body{margin:0;height:100%;overflow:hidden;background:#E9E7E1;font-family:'IBM Plex Sans',system-ui,sans-serif;color:#221F1A}
*{box-sizing:border-box}
#bar{display:flex;align-items:center;gap:12px;height:54px;padding:0 14px;background:#FBFAF7;border-bottom:1px solid #E0DCD1;position:relative;z-index:5}
#bar .name{font-size:14px;font-weight:700}
#bar .sub{font-size:10.5px;color:#8B8578;font-family:'IBM Plex Mono',monospace}
.tbtn{padding:5px 11px;font-size:12px;font-weight:500;border:1px solid #D9D4C6;border-radius:8px;background:#FFF;cursor:pointer;color:#3A362E;font-family:inherit}
.tbtn:hover{border-color:#B9B3A2;background:#F7F5EF}
#stage{position:absolute;inset:54px 0 0 0;overflow:hidden;background:#E9E7E1}
#svg{width:100%;height:100%;display:block;cursor:default;touch-action:none;user-select:none}
#legend{position:absolute;right:12px;bottom:12px;background:rgba(251,250,247,.94);border:1px solid #E0DCD1;border-radius:10px;padding:8px 10px;font-size:11px;display:flex;flex-wrap:wrap;gap:4px 12px;max-width:44%}
#legend span{display:inline-flex;align-items:center;gap:5px;font-family:'IBM Plex Mono',monospace;color:#57524A}
#legend i{width:9px;height:9px;border-radius:2px;display:inline-block}
#layers{position:absolute;left:12px;top:12px;background:rgba(251,250,247,.96);border:1px solid #E0DCD1;border-radius:10px;padding:9px 11px;font-size:11.5px;display:flex;flex-direction:column;gap:3px;box-shadow:0 6px 18px rgba(20,16,8,.12)}
#layers .lh{font-size:10px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#8B8578;margin-bottom:3px}
#layers .lyr{display:flex;align-items:center;gap:7px;cursor:pointer;user-select:none;padding:2px 3px;border-radius:5px;font-family:'IBM Plex Mono',monospace;color:#43403A}
#layers .lyr:hover{background:#F0EDE3}
#layers .lyr.off{opacity:.4;text-decoration:line-through}
#layers .lyr i{width:14px;height:5px;border-radius:2px;display:inline-block}
#tip{position:fixed;pointer-events:none;background:#221F1A;color:#FBFAF7;font-family:'IBM Plex Mono',monospace;font-size:11px;padding:5px 9px;border-radius:6px;opacity:0;transition:opacity .1s;z-index:9;white-space:pre-line}
.board{fill:#1c2a25;stroke:#0e1512}
.cu{stroke-opacity:.85;fill:none;stroke-linecap:round;stroke-linejoin:round}
.pour{fill-opacity:.20;fill-rule:evenodd;stroke-width:0}
#legend .lyr{cursor:pointer;user-select:none}
#legend .lyr.off{opacity:.32;text-decoration:line-through}
.via{fill:#C9CCD1;stroke:#3a3f45;stroke-width:200}
#scene.dim .cu,#scene.dim .via,#scene.dim .pad,#scene.dim .clbl{opacity:.4}
.hl{stroke:#FFF3C4;fill:none;stroke-linecap:round;stroke-linejoin:round;stroke-opacity:.95}
.hlv{fill:#FFF3C4;stroke:#8a6d00;stroke-width:200}
.hlp{fill:#FFF3C4}
.hlr{fill:#8FD0FF}
/* iframe-embed mode: hide chrome, let the net fill the frame */
body.modal #bar,body.modal #layers,body.modal #legend{display:none!important}
/* cross-probe modal */
#xmodal{display:none;position:fixed;inset:0;background:rgba(20,18,14,.55);z-index:20;align-items:center;justify-content:center}
#xbox{background:#12100C;border:1px solid #3a352b;border-radius:12px;width:min(760px,86vw);height:min(620px,82vh);display:flex;flex-direction:column;overflow:hidden;box-shadow:0 20px 60px rgba(0,0,0,.5)}
#xhead{display:flex;align-items:center;gap:12px;padding:10px 14px;border-bottom:1px solid #2c281f;color:#EDEAE2;font-family:'IBM Plex Mono',monospace;font-size:12px}
#xtitle{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#xopen,#xclose{color:#8FD0FF;text-decoration:none;cursor:pointer;font-size:12px;border:1px solid #3a4a58;border-radius:6px;padding:3px 9px;background:none}
#xclose{color:#C7C1B4;border-color:#3a352b}
#xframe{flex:1;border:0;background:#0e0c08}
.comp{stroke-width:0;cursor:pointer}
.pad{cursor:pointer}
.comp.hot,.pad.hot{stroke:#FFFFFF;stroke-width:1400;paint-order:stroke}
.clbl{fill:#EDEAE2;font-family:'IBM Plex Mono',monospace;text-anchor:middle;dominant-baseline:central;pointer-events:none;paint-order:stroke;stroke:rgba(12,18,15,.55);stroke-width:60}
</style></head>
<body>
<div id="bar">
  <div><div class="name" id="nm">PCB</div><div class="sub" id="sub"></div></div>
  <div style="flex:1"></div>
  <button class="tbtn" id="cu" style="border-color:#C98A3A;color:#9A5A18">● Copper</button>
  <button class="tbtn" id="vi" style="border-color:#9AA0A8;color:#5A6068">● Vias</button>
  <button class="tbtn" id="cp" style="border-color:#8FA88F;color:#3d5a3d">● Parts</button>
  <button class="tbtn" id="fit">Fit</button>
  <div style="display:flex;align-items:center;border:1px solid #D9D4C6;border-radius:8px;background:#FFF;overflow:hidden">
    <button class="tbtn" id="zo" style="border:none;border-radius:0">&#8722;</button>
    <span id="zl" style="font-family:'IBM Plex Mono',monospace;font-size:11px;min-width:44px;text-align:center">100%</span>
    <button class="tbtn" id="zi" style="border:none;border-radius:0">+</button>
  </div>
</div>
<div id="stage"><svg id="svg"><g id="scene"></g><g id="hlg"></g></svg><div id="layers"></div><div id="legend"></div></div>
<div id="tip"></div>
<div id="xmodal"><div id="xbox">
  <div id="xhead"><span id="xtitle"></span><a id="xopen" target="_blank">Open full ↗</a><button id="xclose">✕</button></div>
  <iframe id="xframe" src="about:blank"></iframe>
</div></div>
<script>
const M = __MODEL__;
const XP = __XPROBE__;       // cross-probe: {xnets:[{name,sch,reps,bbox}...], companion} or null
const svg=document.getElementById('svg'), scene=document.getElementById('scene'), hlg=document.getElementById('hlg'), tip=document.getElementById('tip');
const [x0,y0,x1,y1]=M.extent, W=x1-x0, H=y1-y0, PAD=Math.max(W,H)*0.04;
const flipY = y => (y1 - y);   // board y is up
let view={x:0,y:0,k:1};
function applyView(){ const t=`translate(${view.x},${view.y}) scale(${view.k})`; scene.setAttribute('transform',t); hlg.setAttribute('transform',t); document.getElementById('zl').textContent=Math.round(view.k*100/baseK)+'%'; }
let baseK=1;
function esc(s){return (s+'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}

let showCopper=true, showVias=true, showParts=true;   // 0x05→0x15/16/17 + 0x33 + 0x2d
const hiddenLayers=new Set();
const LBL=Math.max(W,H)*0.006;        // refdes label size
const minW=Math.max(W,H)*0.0004;      // min visible trace width
const PADINS=900;                     // soldermask-ring inset (board units) → copper
const topC=(M.layerColors&&M.layers.length)?M.layerColors[M.layers[0]]:'#E24A3B';
const botC=(M.layerColors&&M.layers.length)?M.layerColors[M.layers[M.layers.length-1]]:'#2E7DD1';
function copperPaths(){
  const c=M.copper, grp={};           // group by layer|width for real trace widths
  for(let i=0;i<c.length;i+=6){
    const lay=c[i+4]; if(hiddenLayers.has(lay)) continue;
    const g=lay+'|'+c[i+5]; (grp[g]=grp[g]||{lay,w:c[i+5],d:[]}).d.push(`M${c[i]} ${flipY(c[i+1])}L${c[i+2]} ${flipY(c[i+3])}`);
  }
  return Object.values(grp).sort((a,b)=>a.lay-b.lay).map(g=>
    `<path class="cu" d="${g.d.join('')}" stroke="${M.layerColors[g.lay]}" stroke-width="${Math.max(g.w,minW)}"/>`).join('');
}
function viasSVG(){
  const v=M.vias||[]; let h='';
  for(let i=0;i<v.length;i+=3){ h+=`<circle class="via" cx="${v[i]}" cy="${flipY(v[i+1])}" r="${v[i+2]}"/>`; }
  return h;
}
function poursSVG(){
  let h='';
  for(const s of (M.shapes||[])){
    if(hiddenLayers.has(s.l)) continue;
    const p=s.p; let dd='M'+p[0]+' '+flipY(p[1]);
    for(let i=2;i<p.length;i+=2) dd+='L'+p[i]+' '+flipY(p[i+1]);
    h+=`<path class="pour" d="${dd}Z" fill="${M.layerColors[s.l]}"/>`;
  }
  return h;
}
function render(){
  let h=`<rect class="board" x="${x0-PAD}" y="${flipY(y1)-PAD}" width="${W+2*PAD}" height="${H+2*PAD}" rx="${PAD*0.3}"/>`;
  if(showCopper && M.copper.length) h+=copperPaths();
  if(showVias) h+=viasSVG();
  let gi=0;                            // global pad index (matches buildNets order)
  if(showParts) for(const p of M.parts){
    const np=p.pads.length;
    // a part's pads are copper on its side layer — hide the part when that layer is hidden
    const partLayer=p.side?M.layers[M.layers.length-1]:M.layers[0];
    if(hiddenLayers.has(partLayer)){ gi+=np; continue; }
    const da=`data-ref="${esc(p.ref)}" data-val="${esc(p.val)}" data-t="${esc(p.t)}"`;
    const padC=p.side?botC:topC;      // pad copper is on the component's side layer
    const pads=np?p.pads:[[p.x-6000,p.y-6000,p.x+6000,p.y+6000,0]];
    let li=0;
    for(const pd of pads){
      const pa=`${da} data-pi="${np?gi+li:-1}"`; li++;
      const w=pd[2]-pd[0], hh=pd[3]-pd[1];       // already capped to pitch in build()
      if(pd[4]){ h+=`<ellipse class="pad" ${pa} cx="${pd[0]+w/2}" cy="${flipY(pd[1]+hh/2)}" rx="${w/2}" ry="${hh/2}" fill="${padC}"/>`; }
      else { h+=`<rect class="pad" ${pa} x="${pd[0]}" y="${flipY(pd[3])}" width="${w}" height="${hh}" rx="${Math.min(w,hh)*0.12}" fill="${padC}"/>`; }
    }
    h+=`<text class="clbl" ${da} x="${p.x}" y="${flipY(p.y)}" font-size="${LBL}">${esc(p.ref)}</text>`;
    gi+=np;
  }
  scene.innerHTML=h;
  bind();
  updateHighlight();
}
// ---- geometric net tracing (precise) ----
// Connectivity is TRACES (shared exact endpoints, per layer) + VIAS (bridge layers
// only at the via). Pads are NOT merged into connectivity — a big power/cap pad
// catches endpoints from several nets, and merging via pads cascades every power
// net into one blob. Instead, each pad records which net roots its contained
// endpoints belong to (for highlighting only).
const NQ=250, CG=8000;
const nqk=(x,y)=>Math.round(x/NQ)+','+Math.round(y/NQ);
const _par={};
function _find(a){ if(_par[a]===undefined)_par[a]=a; let r=a; while(_par[r]!==r)r=_par[r]; while(_par[a]!==r){const n=_par[a];_par[a]=r;a=n;} return r; }
function _uni(a,b){ _par[_find(a)]=_find(b); }
const _LN=(M.layers&&M.layers.length)?M.layers:[0], _L0=_LN[0];
const _padNets=[], _padMain=[];   // per pad: Set of net roots, and its dominant root
(function buildNets(){
  const C=M.copper, eh={};
  for(let i=0;i<C.length;i+=6){ const L=C[i+4];
    _uni(nqk(C[i],C[i+1])+'@'+L, nqk(C[i+2],C[i+3])+'@'+L);
    const b0=Math.floor(C[i]/CG)+','+Math.floor(C[i+1]/CG), b1=Math.floor(C[i+2]/CG)+','+Math.floor(C[i+3]/CG);
    (eh[b0]=eh[b0]||[]).push([C[i],C[i+1],L]); (eh[b1]=eh[b1]||[]).push([C[i+2],C[i+3],L]);
  }
  const V=M.vias||[];
  for(let i=0;i<V.length;i+=3){ const p=nqk(V[i],V[i+1]); for(const L of _LN) _uni(p+'@'+_L0, p+'@'+L); }
  let gi=0;
  for(const pt of M.parts) for(const pd of pt.pads){
    const cnt=new Map();
    for(let bx=Math.floor(pd[0]/CG);bx<=Math.floor(pd[2]/CG);bx++) for(let by=Math.floor(pd[1]/CG);by<=Math.floor(pd[3]/CG);by++){
      const arr=eh[bx+','+by]; if(!arr) continue;
      for(const e of arr) if(e[0]>=pd[0]&&e[0]<=pd[2]&&e[1]>=pd[1]&&e[1]<=pd[3]){ const r=_find(nqk(e[0],e[1])+'@'+e[2]); cnt.set(r,(cnt.get(r)||0)+1); }
    }
    _padNets[gi]=new Set(cnt.keys());
    let best=null,bc=0; for(const [r,c] of cnt) if(c>bc){bc=c;best=r;}
    _padMain[gi]=best; gi++;
  }
})();
function traceRoot(i){ const C=M.copper; return _find(nqk(C[i],C[i+1])+'@'+C[i+4]); }
function viaRoot(x,y){ return _find(nqk(x,y)+'@'+_L0); }
let pinnedNet=null, hoverNet=null;   // each: a Set of net roots, or null
let refHL=null;                      // a highlighted component refdes, or null
function activeNet(){ return pinnedNet||hoverNet; }
function inNet(net,r){ return net && r!=null && net.has(r); }
function padInNet(net,pn){ if(!net||!pn) return false; for(const r of pn) if(net.has(r)) return true; return false; }
function updateHighlight(){
  const net=activeNet();
  scene.classList.toggle('dim', net!=null || refHL!=null);
  if(net==null && refHL==null){ hlg.innerHTML=''; return; }
  const C=M.copper; let h=''; const byW={};
  if(net) for(let i=0;i<C.length;i+=6){ if(hiddenLayers.has(C[i+4])||!inNet(net,traceRoot(i))) continue;
    const w=Math.max(C[i+5],minW); (byW[w]=byW[w]||[]).push(`M${C[i]} ${flipY(C[i+1])}L${C[i+2]} ${flipY(C[i+3])}`); }
  for(const w in byW) h+=`<path class="hl" d="${byW[w].join('')}" stroke-width="${+w*1.7}"/>`;
  const V=M.vias||[];
  if(net) for(let i=0;i<V.length;i+=3) if(inNet(net,viaRoot(V[i],V[i+1]))) h+=`<circle class="hlv" cx="${V[i]}" cy="${flipY(V[i+1])}" r="${V[i+2]}"/>`;
  let gi=0;
  for(const pt of M.parts){ const hid=hiddenLayers.has(pt.side?_LN[_LN.length-1]:_LN[0]);
    const isRef=refHL!=null && pt.ref===refHL;
    for(const pd of pt.pads){ const pn=_padNets[gi++]; if(hid) continue;
      if(isRef || padInNet(net,pn)) h+=`<rect class="${isRef?'hlp hlr':'hlp'}" x="${pd[0]}" y="${flipY(pd[3])}" width="${pd[2]-pd[0]}" height="${pd[3]-pd[1]}" rx="${Math.min(pd[2]-pd[0],pd[3]-pd[1])*0.15}"/>`; } }
  hlg.innerHTML=h;
}
function _distSeg(px,py,ax,ay,bx,by){ const dx=bx-ax,dy=by-ay,l2=dx*dx+dy*dy; let t=l2?((px-ax)*dx+(py-ay)*dy)/l2:0; t=Math.max(0,Math.min(1,t)); return Math.hypot(px-(ax+t*dx),py-(ay+t*dy)); }
function pickTraceNet(bx,by){
  const C=M.copper; let best=-1,bd=1e18;
  for(let i=0;i<C.length;i+=6){ if(hiddenLayers.has(C[i+4])) continue; const dd=_distSeg(bx,by,C[i],C[i+1],C[i+2],C[i+3]); if(dd<bd){bd=dd;best=i;} }
  return (best>=0 && bd < 14/view.k) ? traceRoot(best) : null;   // ~14px tolerance
}
function boardXY(e){ const r=svg.getBoundingClientRect(); const sx=(e.clientX-r.left-view.x)/view.k, sy=(e.clientY-r.top-view.y)/view.k; return [sx, y1-sy]; }
// ---- cross-probe: canonical xnets <-> layout net roots, modal preview of the schematic ----
const QP=new URLSearchParams(location.search), MODALMODE=QP.get('modal')==='1';
const rootToXnet=new Map(), xnetRoots=[];
(function initXP(){
  if(!XP||!XP.xnets) return;
  XP.xnets.forEach((xn,i)=>{ const roots=new Set();
    for(const r of (xn.reps||[])){ const rt=_find(nqk(r[0],r[1])+'@'+r[2]); roots.add(rt); if(!rootToXnet.has(rt)) rootToXnet.set(rt,i); }
    xnetRoots[i]=roots; });
})();
function zoomToBox(b,m){ if(!b) return; const rr=svg.getBoundingClientRect(); m=m||0.3;
  // keep a minimum context window so a tiny fragment doesn't zoom to just its pads
  const bw=Math.max((b[2]-b[0]),90000), bh=Math.max((b[3]-b[1]),90000);
  view.k=Math.min(rr.width/(bw*(1+m)), rr.height/(bh*(1+m)), baseK*12);
  view.x=rr.width/2-((b[0]+b[2])/2)*view.k; view.y=rr.height/2-flipY((b[1]+b[3])/2)*view.k; applyView(); }
function xnetOfNet(net){ if(!net) return null; for(const r of net) if(rootToXnet.has(r)) return rootToXnet.get(r); return null; }
function refBox(ref){ const p=M.parts.find(q=>q.ref===ref); if(!p||!p.pads.length) return null;
  let b=[1e18,1e18,-1e18,-1e18]; for(const pd of p.pads){ b=[Math.min(b[0],pd[0]),Math.min(b[1],pd[1]),Math.max(b[2],pd[2]),Math.max(b[3],pd[3])]; } return b; }
const modal=document.getElementById('xmodal'), mframe=document.getElementById('xframe'),
      mtitle=document.getElementById('xtitle'), mopen=document.getElementById('xopen');
function showModal(url,full,title){ if(!XP||!XP.companion||MODALMODE) return;
  mframe.src=url; mtitle.textContent=title; mopen.href=full; modal.style.display='flex'; }
function hideModal(){ if(modal){ modal.style.display='none'; mframe.src='about:blank'; } }
function crossProbeNet(net){ const xi=xnetOfNet(net); if(xi==null) return;
  const xn=XP.xnets[xi]; showModal(XP.companion+'?xnet='+xi+'&modal=1', XP.companion+'?xnet='+xi, 'Schematic · '+(xn.name||('net '+xi))); }
function crossProbeRef(ref){ showModal(XP.companion+'?ref='+encodeURIComponent(ref)+'&modal=1', XP.companion+'?ref='+encodeURIComponent(ref), 'Schematic · '+ref); }
function bind(){
  scene.querySelectorAll('.pad,.comp').forEach(el=>{
    el.addEventListener('pointerover',e=>{
      const ref=el.dataset.ref;
      scene.querySelectorAll('.pad,.comp').forEach(x=>{ if(x.dataset.ref===ref) x.classList.add('hot'); });
      const ty=M.types[el.dataset.t], v=el.dataset.val;
      tip.textContent=ref+(v?'  '+v:'')+'\n'+((ty&&ty.label)||el.dataset.t); tip.style.opacity=1; });
    el.addEventListener('pointermove',e=>{ tip.style.left=(e.clientX+14)+'px'; tip.style.top=(e.clientY+14)+'px'; });
    el.addEventListener('pointerout',()=>{ scene.querySelectorAll('.hot').forEach(x=>x.classList.remove('hot')); tip.style.opacity=0; });
  });
}
function fit(){
  const r=svg.getBoundingClientRect(); if(!r.width) return;
  const bw=W+2*PAD, bh=H+2*PAD, pad=40;
  baseK=Math.min((r.width-2*pad)/bw,(r.height-2*pad)/bh);
  view.k=baseK;
  view.x=(r.width-(x0-PAD+x1+PAD)*view.k)/2;
  view.y=(r.height-((flipY(y1)-PAD)+(flipY(y0)+PAD))*view.k)/2;
  applyView();
}
function zoomBy(f){ const r=svg.getBoundingClientRect(),mx=r.width/2,my=r.height/2,nk=view.k*f;
  view.x=mx-(mx-view.x)*(nk/view.k); view.y=my-(my-view.y)*(nk/view.k); view.k=nk; applyView(); }
function netUnder(e){   // net root under the cursor (pad → its node, else nearest trace)
  const pad=e.target.closest('.pad');
  if(pad){ const pi=+pad.dataset.pi; if(pi>=0 && _padMain[pi]!=null) return _padMain[pi]; }
  const b=boardXY(e); return pickTraceNet(b[0],b[1]);
}
let drag=null, down=null, hoverRoot=null;
svg.addEventListener('pointerdown',e=>{ down={x:e.clientX,y:e.clientY,moved:false}; if(e.target.closest('.pad'))return; drag={x:e.clientX,y:e.clientY,vx:view.x,vy:view.y}; svg.setPointerCapture(e.pointerId); });
svg.addEventListener('pointermove',e=>{
  if(down && Math.abs(e.clientX-down.x)+Math.abs(e.clientY-down.y)>4) down.moved=true;
  if(drag){ view.x=drag.vx+e.clientX-drag.x; view.y=drag.vy+e.clientY-drag.y; applyView(); return; }
  if(pinnedNet===null){ const n=netUnder(e); if(n!==hoverRoot){ hoverRoot=n; hoverNet=n!=null?new Set([n]):null; updateHighlight(); } }  // hover-highlight
});
svg.addEventListener('pointerleave',()=>{ if(pinnedNet===null && hoverNet!==null){ hoverRoot=null; hoverNet=null; updateHighlight(); } });
svg.addEventListener('pointerup',e=>{
  if(down && !down.moved){
    const n=netUnder(e); hoverRoot=null; hoverNet=null;
    if(n==null){ pinnedNet=null; refHL=null; updateHighlight(); hideModal(); }
    else if(pinnedNet && pinnedNet.has(n)){ pinnedNet=null; updateHighlight(); hideModal(); }   // toggle off
    else { const xi=xnetOfNet(new Set([n])); pinnedNet=(xi!=null?xnetRoots[xi]:new Set([n]));
           refHL=null; updateHighlight(); crossProbeNet(pinnedNet); }
  }
  drag=null; down=null;
});
svg.addEventListener('wheel',e=>{ e.preventDefault(); const r=svg.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top;
  const f=Math.exp(-e.deltaY*0.0015),nk=view.k*f; view.x=mx-(mx-view.x)*(nk/view.k); view.y=my-(my-view.y)*(nk/view.k); view.k=nk; applyView(); },{passive:false});
document.getElementById('cu').onclick=e=>{ showCopper=!showCopper; e.currentTarget.style.opacity=showCopper?1:0.45; render(); };
document.getElementById('vi').onclick=e=>{ showVias=!showVias; e.currentTarget.style.opacity=showVias?1:0.45; render(); };
document.getElementById('cp').onclick=e=>{ showParts=!showParts; e.currentTarget.style.opacity=showParts?1:0.45; render(); };
document.getElementById('fit').onclick=fit;
document.getElementById('zi').onclick=()=>zoomBy(1.3);
document.getElementById('zo').onclick=()=>zoomBy(0.77);
window.addEventListener('resize',fit);
// left panel: clickable copper layers  ·  bottom legend: component types
function renderLayers(){
  const rows=(M.layers||[]).map(l=>`<div class="lyr${hiddenLayers.has(l)?' off':''}" data-l="${l}"><i style="background:${M.layerColors[l]}"></i>Layer ${l}</div>`).join('');
  document.getElementById('layers').innerHTML='<div class="lh">Copper layers</div>'+rows;
  document.getElementById('layers').querySelectorAll('.lyr').forEach(el=>el.onclick=()=>{
    const l=+el.dataset.l; if(hiddenLayers.has(l))hiddenLayers.delete(l);else hiddenLayers.add(l); renderLayers(); render();});
}
function renderLegend(){
  const seen={}; M.parts.forEach(p=>seen[p.t]=1);
  document.getElementById('legend').innerHTML=Object.keys(seen).sort().map(t=>{const ty=M.types[t]||{label:t,color:'#6E6A60'};
    return `<span><i style="background:${ty.color}"></i>${esc(ty.label||t)}</span>`;}).join('');
}
renderLayers(); renderLegend();
document.getElementById('nm').textContent=M.name;
document.getElementById('sub').textContent=M.parts.length+' components · '+((M.copper||[]).length/6|0)+' traces · '+((M.vias||[]).length/3|0)+' vias · '+(M.layers||[]).length+' layers';
if(MODALMODE) document.body.classList.add('modal');
if(modal){ document.getElementById('xclose').onclick=hideModal;
  modal.addEventListener('click',ev=>{ if(ev.target===modal) hideModal(); }); }
render(); fit();
// apply cross-probe URL params AFTER full load — a load-time resize re-runs fit()
// and would otherwise clobber the zoom target
function applyParams(){
  const xnet=QP.get('xnet'), ref=QP.get('ref');
  if(xnet!==null && xnetRoots[+xnet]){ pinnedNet=xnetRoots[+xnet]; updateHighlight(); zoomToBox(XP.xnets[+xnet].bbox,0.5); }
  else if(ref){
    refHL=ref;
    const p=M.parts.find(q=>q.ref===ref);        // show only the layer the part sits on
    if(p){ const keep=p.side?_LN[_LN.length-1]:_LN[0];
      hiddenLayers.clear(); _LN.forEach(l=>{ if(l!==keep) hiddenLayers.add(l); }); renderLayers(); }
    render(); zoomToBox(refBox(ref),1.2);
  }
}
if(QP.get('xnet')!==null || QP.get('ref')){
  if(document.readyState==='complete') setTimeout(applyParams,40);
  else window.addEventListener('load',()=>setTimeout(applyParams,40));
}
</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("brd", type=Path)
    ap.add_argument("-o", "--output", type=Path, default=None)
    ap.add_argument("--bom", type=Path, default=None,
                    help="OrCAD BOM export for values (default: sibling <stem>.BOM)")
    args = ap.parse_args()
    out = args.output or args.brd.parent / (args.brd.stem + "_pcb.html")
    bom = args.bom
    if bom is None:
        for c in (args.brd.with_suffix(".BOM"), args.brd.parent.parent / (args.brd.stem + ".BOM")):
            if c.exists():
                bom = c
                break
    generate(args.brd, out, bom)


if __name__ == "__main__":
    main()
