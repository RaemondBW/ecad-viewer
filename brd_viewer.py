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
import brd_objects as bo          # noqa: E402
import orcad_convert as oc        # noqa: E402
import comment_ui                 # noqa: E402

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


def _drop_outlier_pads(pads):
    """Drop pads that sit far from the component's pad cluster — a few parts get a
    spurious pad on the far side of the board (a mis-parented 0x32), which would
    otherwise blow up the footprint bbox (and the zoom-to-part preview)."""
    if len(pads) < 3:
        return pads
    cx = sorted((p[0] + p[2]) / 2 for p in pads)[len(pads) // 2]     # robust centre (median)
    cy = sorted((p[1] + p[3]) / 2 for p in pads)[len(pads) // 2]
    dist = lambda p: (((p[0] + p[2]) / 2 - cx) ** 2 + ((p[1] + p[3]) / 2 - cy) ** 2) ** 0.5
    med = sorted(dist(p) for p in pads)[len(pads) // 2]
    thr = max(med * 6, 400000)                                        # keep genuine large parts (BGAs)
    kept = [p for p in pads if dist(p) <= thr]
    return kept if kept else pads


def build(brd_path, bom_path=None):
    d = Path(brd_path).read_bytes()
    hdr = bc.parse_header(d)              # magic + version string, for format diagnostics
    strings = bc.parse_strings(d)
    placements = bc.component_placements(d, strings)
    bom = oc.parse_bom(bom_path) if bom_path and Path(bom_path).exists() else {}
    graph = bo.parse_graph(d, strings)   # validated nets / pours / outline (brd_objects)
    parts, axs, ays = [], [], []
    for ref, (x, y, side, rot, pads) in placements.items():
        pre = re.match(r"^[A-Za-z]+", ref)
        t = pre.group()[0].upper() if pre else "?"
        pads = _drop_outlier_pads(pads)     # drop mis-parented pads across the board
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
    mx = max((ext[2] - ext[0]), (ext[3] - ext[1])) * 0.02 + 8000
    bb = (ext[0] - mx, ext[1] - mx, ext[2] + mx, ext[3] + mx)
    inb = lambda x, y: bb[0] <= x <= bb[2] and bb[1] <= y <= bb[3]

    # net-name table: index 0 = "no net"; display strips the $N synthetic prefix
    net_idx, net_names = {}, [""]
    def nidx(name):
        if not name:
            return 0
        if name not in net_idx:
            net_idx[name] = len(net_names)
            net_names.append(name)
        return net_idx[name]

    # copper from the validated graph: ETCH-track segments, tagged with their net.
    # Same geometry filters as the old bc.copper_segments (corruption artifacts).
    copper = []
    for key, (off, pk) in graph["segsL"].items():
        tr = graph["tracks"].get(pk)
        if not tr or tr[0] != bo.ETCH:
            continue
        w, sx, sy, ex, ey = bo.seg_line(d, off)
        if not (0 < w <= 15000) or (sx, sy) == (ex, ey):
            continue
        if max(abs(ex - sx), abs(ey - sy)) > 300000:
            continue
        if (abs(sx) < 4000 and abs(sy) < 4000) or (abs(ex) < 4000 and abs(ey) < 4000):
            continue
        if inb(sx, sy) and inb(ex, ey):
            copper.append((sx, sy, ex, ey, tr[1], w, nidx(tr[2])))

    # copper pours / planes: ETCH shapes with walked boundaries (translucent fills)
    pours = []
    for s in graph["shapes"]:
        if s["cls"] != bo.ETCH:
            continue
        pts = bo.shape_boundary(d, graph, s)
        if len(pts) < 3:
            continue
        flat = [c for p in pts for c in p]
        if not all(inb(pts[i][0], pts[i][1]) for i in (0, len(pts) // 2, -1)):
            continue
        pours.append({"n": nidx(s["net"]), "l": s["sub"], "p": flat})

    # board outline: the largest route-keepin shape (this design has no explicit
    # BOARD GEOMETRY outline; the keepin hugs the board edge)
    outline = []
    keepins = [s for s in graph["shapes"] if s["cls"] == bo.KEEPIN]
    if keepins:
        big = max(keepins, key=lambda s: (s["bbox"][2] - s["bbox"][0]) * (s["bbox"][3] - s["bbox"][1]))
        pts = bo.shape_boundary(d, graph, big)
        if len(pts) >= 3:
            outline = [c for p in pts for c in p]

    # vias with nets (validated); fall back to the heuristic scan if none found
    vias_flat = []
    seen_v = set()
    for x, y, net in graph["vias"]:
        if inb(x, y) and (x, y) not in seen_v:
            seen_v.add((x, y))
            vias_flat += [x, y, 2000, nidx(net)]
    if not vias_flat:
        vias_flat = [c for v in bc.vias(d, bb) for c in (v[0], v[1], v[2], 0)]

    # pad → net lookup by exact bbox (both read the same 0x32 coords)
    pad_net = {}
    for (_k, x1, y1, x2, y2, net, _fp, _sub, _lay) in graph["pads"]:
        if net:
            pad_net[(x1, y1, x2, y2)] = nidx(net)
    for pt in parts:
        for p in pt["pads"]:
            p.append(pad_net.get((p[0], p[1], p[2], p[3]), 0))

    layers = sorted({s[4] for s in copper})
    return {"name": Path(brd_path).stem, "parts": parts, "extent": ext,
            "fmt": {"magic": hdr.get("magic", 0), "version": hdr.get("version_string", "")},
            "copper": [c for s in copper for c in s],      # x1,y1,x2,y2,layer,width,net × n
            "vias": vias_flat,                             # x,y,half,net × n
            "pours": pours,                                # [{n,l,p:[x,y…]}]
            "outline": outline,                            # [x,y…]
            "netNames": net_names,
            "layers": layers,
            "layerColors": {str(l): LAYER_COLORS[i % len(LAYER_COLORS)] for i, l in enumerate(layers)},
            "types": {k: {"label": v[0], "color": v[1], "hs": v[2]} for k, v in TYPES.items()}}


def generate(brd_path, out_path, bom_path=None, xprobe=None, model=None, shell=False, offline=False):
    # shell=True: data-free viewer that fetches the board after sign-in (hosted, private).
    # offline=True (embedded only): drop the Firebase/comments bootstrap + web-font links
    # so the file makes no network request — pure self-contained viewer + cross-probe.
    if shell:
        boot, fb = "", comment_ui.shell_bootstrap("schematic-viewer", "layout")
    else:
        if model is None:
            model = build(brd_path, bom_path)
        boot = "window.__renderModel(" + json.dumps(model) + ", " + json.dumps(xprobe) + ");"
        fb = "" if offline else comment_ui.firebase_bootstrap("schematic-viewer")
    html = (HTML.replace("/*__BOOT__*/", boot)
                .replace("/*__CMT_CSS__*/", comment_ui.CSS)
                .replace("/*__CMT_JS__*/", comment_ui.JS)
                .replace("<!--__CMT_FIREBASE__-->", fb))
    if offline:
        html = comment_ui.strip_webfonts(html)
    Path(out_path).write_text(html)
    if shell:
        print(f"Wrote {out_path}  (layout shell, {Path(out_path).stat().st_size // 1024} KB)")
        return
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
#layers{position:absolute;left:12px;top:12px;background:rgba(251,250,247,.96);border:1px solid #E0DCD1;border-radius:10px;padding:9px 11px;font-size:11.5px;display:flex;flex-direction:column;gap:3px;box-shadow:0 6px 18px rgba(20,16,8,.12)}
#layers .lh{font-size:10px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#8B8578;margin-bottom:3px}
#layers .lyr{display:flex;align-items:center;gap:7px;cursor:pointer;user-select:none;padding:2px 3px;border-radius:5px;font-family:'IBM Plex Mono',monospace;color:#43403A}
#layers .lyr:hover{background:#F0EDE3}
#layers .lyr.off{opacity:.4;text-decoration:line-through}
#layers .lyr i{width:14px;height:5px;border-radius:2px;display:inline-block}
#netlegend{position:absolute;right:12px;bottom:12px;background:rgba(251,250,247,.96);border:1px solid #E0DCD1;border-radius:10px;padding:9px 11px;font-size:11.5px;max-width:46%;box-shadow:0 6px 18px rgba(20,16,8,.12)}
#netlegend .ll{font-size:10px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#8B8578;margin-bottom:4px}
#netlegend .nl{display:flex;align-items:center;gap:7px;padding:2px 0;font-family:'IBM Plex Mono',monospace;color:#43403A}
#netlegend .nl i{width:11px;height:11px;border-radius:3px;flex:0 0 auto}
#netlegend .nl span{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
#netlegend .nl b{cursor:pointer;color:#A19B8E;font-weight:400;padding:0 2px;flex:0 0 auto}
#netlegend .nl b:hover{color:#CF222E}
#tip{position:fixed;pointer-events:none;background:#221F1A;color:#FBFAF7;font-family:'IBM Plex Mono',monospace;font-size:11px;padding:5px 9px;border-radius:6px;opacity:0;transition:opacity .1s;z-index:9;white-space:pre-line}
.board{fill:#1c2a25;stroke:#0e1512}
.cu{stroke-opacity:.85;fill:none;stroke-linecap:round;stroke-linejoin:round}
.pour{fill-opacity:.20;fill-rule:evenodd;stroke-width:0}
.via{fill:#C9CCD1;stroke:#3a3f45;stroke-width:200}
#scene.dim .cu,#scene.dim .via,#scene.dim .pad,#scene.dim .clbl{opacity:.4}
.hl{stroke:#FFF3C4;fill:none;stroke-linecap:round;stroke-linejoin:round;stroke-opacity:.95}
.hlv,.hlp{fill:none;stroke:#FFF3C4;stroke-width:2.5;vector-effect:non-scaling-stroke}
.hlr{stroke:#8FD0FF}
/* iframe-embed mode: hide chrome, let the net fill the frame */
body.modal #bar,body.modal #layers{display:none!important}
body.modal #stage{inset:0!important}
body.modal #svg{pointer-events:none}   /* embedded preview: static, no pan/zoom/hover/click */
/* cross-probe modal */
#xmodal{display:none;position:fixed;inset:0;background:rgba(20,18,14,.55);z-index:20;align-items:center;justify-content:center}
#xbox{background:#12100C;border:1px solid #3a352b;border-radius:12px;width:min(760px,86vw);height:min(620px,82vh);display:flex;flex-direction:column;overflow:hidden;box-shadow:0 20px 60px rgba(0,0,0,.5)}
/* hover "peek": a non-blocking corner preview (pointer passes through to the board) */
#xmodal.peek{background:none;pointer-events:none;align-items:flex-start;justify-content:flex-end;padding:60px 14px 14px}
#xmodal.peek #xbox{pointer-events:none;width:min(40%,460px);height:min(52%,400px);box-shadow:0 18px 48px rgba(0,0,0,.5)}
#xmodal.peek #xopen,#xmodal.peek #xclose{display:none}
#xhead{display:flex;align-items:center;gap:12px;padding:10px 14px;border-bottom:1px solid #2c281f;color:#EDEAE2;font-family:'IBM Plex Mono',monospace;font-size:12px}
#xtitle{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#xopen,#xclose{color:#8FD0FF;text-decoration:none;cursor:pointer;font-size:12px;border:1px solid #3a4a58;border-radius:6px;padding:3px 9px;background:none}
#xclose{color:#C7C1B4;border-color:#3a352b}
#xframe{flex:1;border:0;background:#0e0c08}
.comp{stroke-width:0;cursor:pointer}
.pad{cursor:pointer}
.comp.hot,.pad.hot{stroke:#FFFFFF;stroke-width:1400;paint-order:stroke}
.clbl{fill:#EDEAE2;font-family:'IBM Plex Mono',monospace;text-anchor:middle;dominant-baseline:central;pointer-events:none;paint-order:stroke;stroke:rgba(12,18,15,.55);stroke-width:60}
/*__CMT_CSS__*/
</style></head>
<body>
<div id="shell-gate" style="position:fixed;inset:0;z-index:9999;background:#FBFAF7;display:none;align-items:center;justify-content:center;flex-direction:column;font-family:'IBM Plex Sans',system-ui,sans-serif">
  <div style="font-size:19px;font-weight:700;color:#221F1A;margin-bottom:6px">PCB Project</div>
  <div id="shell-gate-msg" style="font-size:14px;color:#8B8578;margin-bottom:20px">Loading&hellip;</div>
  <button id="shell-gate-btn" style="display:none;align-items:center;gap:8px;height:40px;padding:0 18px;border-radius:10px;border:1px solid #D9D4C6;background:#fff;color:#3A362E;font:600 14px 'IBM Plex Sans',system-ui,sans-serif;cursor:pointer">Sign in with Google</button>
</div>
<div id="bar">
  <button class="tbtn" onclick="location.href='../../'" title="Back to projects">&#8592; Projects</button>
  <button class="tbtn" id="to-sch" title="View the schematic">Schematic</button>
  <div style="width:1px;height:22px;background:var(--line,#D9D4C6);flex-shrink:0"></div>
  <div><div class="name" id="nm">PCB</div><div class="sub" id="sub"></div></div>
  <div style="flex:1"></div>
  <button class="tbtn" id="cmt-btn" title="Add / view comments">💬 Comment</button>
  <button class="tbtn" id="flip" title="Mirror the board horizontally (view from the back)">Flip</button>
  <button class="tbtn" id="fit">Fit</button>
  <div style="display:flex;align-items:center;border:1px solid #D9D4C6;border-radius:8px;background:#FFF;overflow:hidden">
    <button class="tbtn" id="zo" style="border:none;border-radius:0">&#8722;</button>
    <span id="zl" style="font-family:'IBM Plex Mono',monospace;font-size:11px;min-width:44px;text-align:center">100%</span>
    <button class="tbtn" id="zi" style="border:none;border-radius:0">+</button>
  </div>
  <button id="cmt-share" class="tbtn" style="display:none;flex-shrink:0" title="Share this project">Share</button>
  <div id="tb-rev" style="display:none;position:relative;flex-shrink:0"></div>
  <div id="cmt-account" style="margin-left:6px;flex-shrink:0"></div>
</div>
<div id="stage"><svg id="svg"><g id="scene"></g><g id="hlg"></g></svg><div id="layers"></div><div id="netlegend" style="display:none"></div></div>
<div id="tip"></div>
<div id="xmodal"><div id="xbox">
  <div id="xhead"><span id="xtitle"></span><a id="xopen">Open full →</a><button id="xclose">✕</button></div>
  <iframe id="xframe" src="about:blank"></iframe>
</div></div>
<script>
/*__CMT_JS__*/
const DOCID = new URLSearchParams(location.search).get('doc') || '';
// ---- debug logging -----------------------------------------------------------
// Tagged, timestamped console output so a user hitting a failure (a board that
// won't open, a freeze, a render error) can copy the console and send it back.
// Everything is prefixed [CanvasPCB/layout]; global handlers catch anything the
// try/catches miss. Silent by default beyond a startup banner + stage timings.
const DBG = (function () {
  const TAG = '[CanvasPCB/layout]';
  const now = () => (typeof performance !== 'undefined' ? performance.now() : Date.now());
  const t0 = now();
  const ms = () => Math.round(now() - t0) + 'ms';
  // Every line is ALSO stored in window.__cpcbLogs so it can be copied in one shot,
  // even if DevTools was opened after load: run  copy(window.__cpcbLogs.join('\n'))
  const store = (window.__cpcbLogs = window.__cpcbLogs || []);
  const fmt = a => a.map(x => { try { return typeof x === 'object' ? JSON.stringify(x) : String(x); } catch (e) { return '' + x; } }).join(' ');
  const mk = sink => (...a) => {
    try { store.push(TAG + ' ' + ms() + ' ' + fmt(a)); if (store.length > 800) store.shift(); } catch (e) {}
    try { sink.call(console, TAG, ms(), ...a); } catch (e) {}
  };
  return { TAG, ms, log: mk(console.log), warn: mk(console.warn), error: mk(console.error) };
})();
window.addEventListener('error', e => DBG.error('uncaught error:', e.message,
  '@', (e.filename || '') + ':' + (e.lineno || '') + ':' + (e.colno || ''),
  (e.error && e.error.stack) || ''));
window.addEventListener('unhandledrejection', e => DBG.error('unhandled rejection:',
  (e.reason && (e.reason.stack || e.reason.message)) || e.reason));
DBG.log('viewer script loaded; doc=' + (DOCID || '(embedded)') + ' url=' + location.href);
// The whole viewer runs from __renderModel: embedded builds call it immediately
// (see /*__BOOT__*/); the hosted shell calls it after sign-in + fetch. The comment
// library above stays synchronous so the backend can attach to window.Comments.
window.__renderModel = function (model, xprobe, oldModel) {
try {
const M = model, XP = xprobe || null;   // XP: {xnets, companion, ...} or null
DBG.log('renderModel: model stats =', {
  name: M && M.name, parts: M && M.parts && M.parts.length,
  copperSegs: M && M.copper ? (M.copper.length / 7 | 0) : 0,
  vias: M && M.vias ? (M.vias.length / 4 | 0) : 0,
  pours: M && M.pours ? M.pours.length : 0,
  outlinePts: M && M.outline ? (M.outline.length / 2 | 0) : 0,
  netNames: M && M.netNames ? M.netNames.length : 0,
  layers: M && M.layers, xprobe: !!XP, standalone: !!(XP && XP.standalone),
  hasOldModel: !!oldModel });
// revision diff (shell mode): compare part placement + counts against an older rev
const DIFF = (function(){
  if(!oldModel) return null;
  const stride = m => m.netNames ? 7 : 6, vstride = m => m.netNames ? 4 : 3;
  const byRef = m => { const o={}; for(const pt of m.parts) o[pt.ref]=pt; return o; };
  const o=byRef(oldModel), n=byRef(M);
  const added=[], removed=[], moved=[];
  for(const r in n) if(!(r in o)) added.push(n[r]);
  for(const r in o) if(!(r in n)) removed.push(o[r]);
  for(const r in n) if(r in o){ const dx=n[r].x-o[r].x, dy=n[r].y-o[r].y;
    if(Math.abs(dx)+Math.abs(dy)>5000) moved.push({ref:r, ox:o[r].x, oy:o[r].y, nx:n[r].x, ny:n[r].y}); }
  // copper diff: key each segment by its (orientation-independent) endpoints+layer
  const so=stride(oldModel), sn=stride(M);
  const segKey=(C,i)=>{ let ax=C[i],ay=C[i+1],bx=C[i+2],by=C[i+3];
    if(ax>bx||(ax===bx&&ay>by)){const t=ax;ax=bx;bx=t;const u=ay;ay=by;by=u;}
    return ax+','+ay+','+bx+','+by+','+C[i+4]; };
  const oset=new Set(); for(let i=0;i<oldModel.copper.length;i+=so) oset.add(segKey(oldModel.copper,i));
  const nset=new Set(); for(let i=0;i<M.copper.length;i+=sn) nset.add(segKey(M.copper,i));
  const addedCu=[], removedCu=[];
  for(let i=0;i<M.copper.length;i+=sn) if(!oset.has(segKey(M.copper,i))) addedCu.push(M.copper[i],M.copper[i+1],M.copper[i+2],M.copper[i+3]);
  for(let i=0;i<oldModel.copper.length;i+=so) if(!nset.has(segKey(oldModel.copper,i))) removedCu.push(oldModel.copper[i],oldModel.copper[i+1],oldModel.copper[i+2],oldModel.copper[i+3]);
  const chg=new Set([...added,...removed].map(p=>p.ref).concat(moved.map(m=>m.ref)));
  return { added, removed, moved, addedCu, removedCu, chg,
    oldTraces: oldModel.copper.length/so|0, newTraces: M.copper.length/sn|0,
    oldVias: (oldModel.vias||[]).length/vstride(oldModel)|0, newVias: (M.vias||[]).length/vstride(M)|0,
    label: 'rev ' + ((window.__docMeta||{}).diffRev || '?') };
})();
const svg=document.getElementById('svg'), scene=document.getElementById('scene'), hlg=document.getElementById('hlg'), tip=document.getElementById('tip');
const [x0,y0,x1,y1]=M.extent, W=x1-x0, H=y1-y0, PAD=Math.max(W,H)*0.04;
const flipY = y => (y1 - y);   // board y is up
let flipped=false;                              // horizontal mirror: view the board from the back
const FX = x => flipped ? (x0 + x1 - x) : x;    // involution around the board centre
let showOutline=true;
let view={x:0,y:0,k:1};
function applyView(){ const t=`translate(${view.x},${view.y}) scale(${view.k})`; scene.setAttribute('transform',t); hlg.setAttribute('transform',t); document.getElementById('zl').textContent=Math.round(view.k*100/baseK)+'%'; Comments.reproject(); }
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
  for(let i=0;i<c.length;i+=7){
    const lay=c[i+4]; if(hiddenLayers.has(lay)) continue;
    const g=lay+'|'+c[i+5]; (grp[g]=grp[g]||{lay,w:c[i+5],d:[]}).d.push(`M${FX(c[i])} ${flipY(c[i+1])}L${FX(c[i+2])} ${flipY(c[i+3])}`);
  }
  return Object.values(grp).sort((a,b)=>a.lay-b.lay).map(g=>
    `<path class="cu" d="${g.d.join('')}" stroke="${M.layerColors[g.lay]}" stroke-width="${Math.max(g.w,minW)}"/>`).join('');
}
function viasSVG(){
  // One coalesced <path> (each via a two-arc circle subpath) instead of one <circle>
  // per via — on a dense board that's the difference between ~1 node and tens of
  // thousands, which is what lets a large board open at all.
  const v=M.vias||[]; if(!v.length) return '';
  let d='';
  for(let i=0;i<v.length;i+=4){ const cx=FX(v[i]),cy=flipY(v[i+1]),r=v[i+2];
    d+=`M${cx-r} ${cy}a${r} ${r} 0 1 0 ${2*r} 0a${r} ${r} 0 1 0 ${-2*r} 0`; }
  return `<path class="via" d="${d}"/>`;
}
function poursSVG(){
  let h='';
  for(const s of (M.pours||[])){       // copper pours / power planes (0x28 shapes)
    if(hiddenLayers.has(s.l)) continue;
    const p=s.p; let dd='M'+FX(p[0])+' '+flipY(p[1]);
    for(let i=2;i<p.length;i+=2) dd+='L'+FX(p[i])+' '+flipY(p[i+1]);
    h+=`<path class="pour" d="${dd}Z" fill="${(M.layerColors&&M.layerColors[s.l])||'#666'}" fill-opacity="0.13" stroke="${(M.layerColors&&M.layerColors[s.l])||'#666'}" stroke-opacity="0.35" stroke-width="${minW*2}"/>`;
  }
  return h;
}
function outlineSVG(){
  const p=M.outline||[]; if(!showOutline||p.length<6) return '';
  let dd='M'+FX(p[0])+' '+flipY(p[1]);
  for(let i=2;i<p.length;i+=2) dd+='L'+FX(p[i])+' '+flipY(p[i+1]);
  return `<path d="${dd}Z" fill="none" stroke="#8A8577" stroke-width="${minW*4}" stroke-dasharray="${minW*16} ${minW*10}"/>`;
}
function diffCopperSVG(){
  if(!DIFF) return '';
  let h='';
  const draw=(arr,col,w,dash)=>{ let d=''; for(let i=0;i<arr.length;i+=4) d+=`M${FX(arr[i])} ${flipY(arr[i+1])}L${FX(arr[i+2])} ${flipY(arr[i+3])}`;
    return d?`<path d="${d}" stroke="${col}" stroke-width="${w}" fill="none" stroke-linecap="round"${dash?` stroke-dasharray="${dash}"`:''}/>`:''; };
  h+=draw(DIFF.removedCu,'#CF222E',minW*2.6,`${minW*10} ${minW*7}`);   // gone (was in old rev)
  h+=draw(DIFF.addedCu,'#1A7F37',minW*2.6,'');                          // new routing this rev
  return h;
}
function diffSVG(){
  if(!DIFF) return '';
  const R=9000; let h='';
  for(const pt of DIFF.added)   h+=`<circle cx="${FX(pt.x)}" cy="${flipY(pt.y)}" r="${R}" fill="none" stroke="#1A7F37" stroke-width="${minW*4}"/>`;
  for(const pt of DIFF.removed) h+=`<circle cx="${FX(pt.x)}" cy="${flipY(pt.y)}" r="${R}" fill="none" stroke="#CF222E" stroke-width="${minW*3}" stroke-dasharray="${minW*8} ${minW*6}"/>`;
  for(const mv of DIFF.moved){
    h+=`<line x1="${FX(mv.ox)}" y1="${flipY(mv.oy)}" x2="${FX(mv.nx)}" y2="${flipY(mv.ny)}" stroke="#9A6700" stroke-width="${minW*3}" stroke-dasharray="${minW*6} ${minW*5}"/>`;
    h+=`<circle cx="${FX(mv.nx)}" cy="${flipY(mv.ny)}" r="${R*0.8}" fill="none" stroke="#9A6700" stroke-width="${minW*4}"/>`;
  }
  return h;
}
function render(){
  let h=`<rect class="board" x="${x0-PAD}" y="${flipY(y1)-PAD}" width="${W+2*PAD}" height="${H+2*PAD}" rx="${PAD*0.3}"/>`;
  h+=outlineSVG();
  // In diff mode the unchanged board fades back so the copper add/remove overlay reads clearly.
  let b='';                            // base ink (dimmed under a diff)
  if(showCopper && M.copper.length) b+=poursSVG()+copperPaths();
  if(showVias) b+=viasSVG();
  // Pads: coalesced into at most four <path> nodes (rect/round × top/bottom side) plus
  // one <text> per part, instead of a DOM node per pad. Part hit-testing no longer
  // relies on these being individual elements — it uses the _padgrid spatial index
  // (see padAt), the same way trace picking uses _pgrid.
  if(showParts){
    const rectT=[], rectB=[], elT=[], elB=[]; let labels='';
    for(const p of M.parts){
      const np=p.pads.length;
      // a part's pads are copper on its side layer — hide the part when that layer is hidden
      const partLayer=p.side?M.layers[M.layers.length-1]:M.layers[0];
      if(hiddenLayers.has(partLayer)) continue;
      const pads=np?p.pads:[[p.x-6000,p.y-6000,p.x+6000,p.y+6000,0]];
      const rects=p.side?rectB:rectT, els=p.side?elB:elT;
      for(const pd of pads){
        const w=pd[2]-pd[0], hh=pd[3]-pd[1];     // already capped to pitch in build()
        if(pd[4]){ const rx=w/2, ry=hh/2, cx=FX(pd[0]+rx), cy=flipY(pd[1]+ry);
          els.push(`M${cx-rx} ${cy}a${rx} ${ry} 0 1 0 ${2*rx} 0a${rx} ${ry} 0 1 0 ${-2*rx} 0`); }
        else { const x=FX(pd[0]+w/2)-w/2, y=flipY(pd[3]);
          rects.push(`M${x} ${y}h${w}v${hh}h${-w}z`); }
      }
      labels+=`<text class="clbl" x="${FX(p.x)}" y="${flipY(p.y)}" font-size="${LBL}">${esc(p.ref)}</text>`;
    }
    const padPath=(a,fill)=> a.length?`<path class="pad" d="${a.join('')}" fill="${fill}"/>`:'';
    b+=padPath(rectT,topC)+padPath(elT,topC)+padPath(rectB,botC)+padPath(elB,botC)+labels;
  }
  h += DIFF ? `<g opacity="0.16">${b}</g>` + diffCopperSVG() : b;
  h+=diffSVG();
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
DBG.log('buildNets: start (union-find over copper/vias/pads)…');
(function buildNets(){
  const C=M.copper, eh={};
  for(let i=0;i<C.length;i+=7){ const L=C[i+4];
    _uni(nqk(C[i],C[i+1])+'@'+L, nqk(C[i+2],C[i+3])+'@'+L);
    if(C[i+6]) _uni(nqk(C[i],C[i+1])+'@'+L, 'N#'+C[i+6]);   // exact net id (x1B graph)
    const b0=Math.floor(C[i]/CG)+','+Math.floor(C[i+1]/CG), b1=Math.floor(C[i+2]/CG)+','+Math.floor(C[i+3]/CG);
    (eh[b0]=eh[b0]||[]).push([C[i],C[i+1],L]); (eh[b1]=eh[b1]||[]).push([C[i+2],C[i+3],L]);
  }
  const V=M.vias||[];
  for(let i=0;i<V.length;i+=4){ const p=nqk(V[i],V[i+1]); for(const L of _LN) _uni(p+'@'+_L0, p+'@'+L);
    if(V[i+3]) _uni(p+'@'+_L0, 'N#'+V[i+3]); }
  let gi=0;
  for(const pt of M.parts) for(const pd of pt.pads){
    const cnt=new Map();
    for(let bx=Math.floor(pd[0]/CG);bx<=Math.floor(pd[2]/CG);bx++) for(let by=Math.floor(pd[1]/CG);by<=Math.floor(pd[3]/CG);by++){
      const arr=eh[bx+','+by]; if(!arr) continue;
      for(const e of arr) if(e[0]>=pd[0]&&e[0]<=pd[2]&&e[1]>=pd[1]&&e[1]<=pd[3]){ const r=_find(nqk(e[0],e[1])+'@'+e[2]); cnt.set(r,(cnt.get(r)||0)+1); }
    }
    if(pd[5]) { const r=_find('N#'+pd[5]); cnt.set(r,(cnt.get(r)||0)+2); }   // exact pad net
    _padNets[gi]=new Set(cnt.keys());
    let best=null,bc=0; for(const [r,c] of cnt) if(c>bc){bc=c;best=r;}
    _padMain[gi]=best; gi++;
  }
})();
// ---- perf caches (roots are static after buildNets) ----
// per-segment / per-via union-find root, so hover highlight is O(1) lookups; and a
// spatial grid of segments so hover-picking scans only nearby copper, not all of it.
const _segRoot=[], _viaRoot=[];
const PG=20000, _pgrid=new Map();
const _rootName={};   // union root -> real net name (synthetic $N names stay hidden)
(function buildCaches(){
  const C=M.copper, V=M.vias||[];
  for(let i=0;i<C.length;i+=7){
    _segRoot[i/7]=_find(nqk(C[i],C[i+1])+'@'+C[i+4]);
    if(C[i+6]&&_rootName[_segRoot[i/7]]===undefined){ const nm=(M.netNames||[])[C[i+6]]; if(nm&&nm[0]!=='$') _rootName[_segRoot[i/7]]=nm; }
    const x0=Math.min(C[i],C[i+2]),x1=Math.max(C[i],C[i+2]),y0=Math.min(C[i+1],C[i+3]),y1=Math.max(C[i+1],C[i+3]);
    for(let gx=Math.floor(x0/PG);gx<=Math.floor(x1/PG);gx++) for(let gy=Math.floor(y0/PG);gy<=Math.floor(y1/PG);gy++){
      const k=gx+','+gy; let a=_pgrid.get(k); if(!a){a=[];_pgrid.set(k,a);} a.push(i); }
  }
  for(let i=0;i<V.length;i+=4) _viaRoot[i/4]=_find(nqk(V[i],V[i+1])+'@'+_L0);
})();
DBG.log('buildNets + caches done:', {unionNodes: Object.keys(_par).length, segGridCells: _pgrid.size, namedRoots: Object.keys(_rootName).length});
const _seen=new Int32Array((M.copper.length/7|0)+1); let _gen=0;
// Pad spatial index — pads are drawn as coalesced paths (no per-pad DOM), so part
// hover/click hit-tests against this grid instead of e.target. Each item is a pad's
// board-space rect plus its part's ref/side/centre.
const _padItems=[], PADPG=20000, _padgrid=new Map();
(function buildPadIndex(){
  for(const pt of M.parts){
    const src=pt.pads.length?pt.pads:[[pt.x-6000,pt.y-6000,pt.x+6000,pt.y+6000,0]];
    for(const pd of src){
      const idx=_padItems.length;
      _padItems.push({x1:pd[0],y1:pd[1],x2:pd[2],y2:pd[3],ref:pt.ref,side:pt.side,px:pt.x,py:pt.y});
      for(let gx=Math.floor(pd[0]/PADPG);gx<=Math.floor(pd[2]/PADPG);gx++) for(let gy=Math.floor(pd[1]/PADPG);gy<=Math.floor(pd[3]/PADPG);gy++){
        const k=gx+','+gy; let a=_padgrid.get(k); if(!a){a=[];_padgrid.set(k,a);} a.push(idx); }
    }
  }
})();
const _byRef={}; for(const pt of M.parts) _byRef[pt.ref]=pt;   // ref → part, for hover tooltips
// Pad containing (or within tol of) a board point, honouring layer visibility; null if none.
function padAt(bx,by){
  const tol=8/view.k, t2=tol*tol; let best=null,bd=1e30;
  const gx=Math.floor(bx/PADPG),gy=Math.floor(by/PADPG);
  for(let ax=gx-1;ax<=gx+1;ax++) for(let ay=gy-1;ay<=gy+1;ay++){ const a=_padgrid.get(ax+','+ay); if(!a) continue;
    for(const idx of a){ const it=_padItems[idx];
      if(hiddenLayers.has(it.side?_LN[_LN.length-1]:_LN[0])) continue;
      const dx=Math.max(it.x1-bx,0,bx-it.x2), dy=Math.max(it.y1-by,0,by-it.y2), d=dx*dx+dy*dy;
      if(d<bd){bd=d;best=it;} } }
  return (best && bd<=t2) ? {ref:best.ref,x:best.px,y:best.py} : null;
}
function traceRoot(i){ return _segRoot[i/7]; }
function viaRoot(x,y){ return _find(nqk(x,y)+'@'+_L0); }
let pinnedNet=null, hoverNet=null;   // net highlight: each a Set of net roots, or null
let pinnedRef=null, hoverRef=null;   // part highlight: a component refdes, or null
let pinnedXnets=[];                  // xnet indices behind pinnedNet (for cross-probe round-trip)
// Multiple highlighted nets, each in its own colour (matched to the schematic when
// cross-probed). pinnedNet is the derived union used for hit-testing / dimming.
const PALETTE=['#EA580C','#2563EB','#16A34A','#9333EA','#DB2777','#0891B2','#CA8A04','#DC2626'];
let pinnedList=[];   // [{ roots:Set, color, name, xnet }]
function nextColor(){ const used=new Set(pinnedList.map(p=>p.color)); return PALETTE.find(c=>!used.has(c))||PALETTE[pinnedList.length%PALETTE.length]; }
function syncPinnedNet(){
  if(!pinnedList.length){ pinnedNet=null; pinnedXnets=[]; return; }
  const u=new Set(); pinnedList.forEach(p=>p.roots.forEach(r=>u.add(r))); pinnedNet=u;
  pinnedXnets=pinnedList.filter(p=>p.xnet!=null).map(p=>p.xnet);
}
function netNameOf(xi,root){ if(xi!=null&&XP&&XP.xnets[xi]) return XP.xnets[xi].name||('net '+xi); return (typeof _rootName!=='undefined'&&_rootName[root])||'net'; }
// Cross-probe: carry the highlighted nets to the schematic as ?xnet=i,j,k.
function schematicHref(){ const u=new URLSearchParams(location.search); u.delete('ref');
  if(pinnedXnets.length) u.set('xnet',pinnedXnets.join(',')); else u.delete('xnet');
  // standalone pair → jump straight to the sibling file; hosted → the dashboard.
  const base=(XP&&XP.standalone&&XP.companion)?XP.companion:'../';
  return base+'?'+u.toString(); }
function activeNet(){ return pinnedNet||hoverNet; }
function inNet(net,r){ return net && r!=null && net.has(r); }
function padInNet(net,pn){ if(!net||!pn) return false; for(const r of pn) if(net.has(r)) return true; return false; }
// Draw one net's copper/vias/pads into the highlight layer, in `color`.
function drawNet(net,color){
  const C=M.copper; let h=''; const byW={};
  for(let i=0;i<C.length;i+=7){ if(hiddenLayers.has(C[i+4])||!inNet(net,traceRoot(i))) continue;
    const w=Math.max(C[i+5],minW); (byW[w]=byW[w]||[]).push(`M${FX(C[i])} ${flipY(C[i+1])}L${FX(C[i+2])} ${flipY(C[i+3])}`); }
  for(const w in byW) h+=`<path class="hl" style="stroke:${color}" d="${byW[w].join('')}" stroke-width="${+w*1.7}"/>`;
  const V=M.vias||[];
  for(let i=0;i<V.length;i+=4) if(inNet(net,_viaRoot[i/4])) h+=`<circle class="hlv" style="stroke:${color}" cx="${FX(V[i])}" cy="${flipY(V[i+1])}" r="${V[i+2]}"/>`;
  let gi=0;
  for(const pt of M.parts){ const hid=hiddenLayers.has(pt.side?_LN[_LN.length-1]:_LN[0]);
    for(const pd of pt.pads){ const pn=_padNets[gi++]; if(hid||!padInNet(net,pn)) continue;
      const w=pd[2]-pd[0], hh=pd[3]-pd[1];
      if(pd[4]) h+=`<ellipse class="hlp" style="stroke:${color}" cx="${FX(pd[0]+w/2)}" cy="${flipY(pd[1]+hh/2)}" rx="${w/2}" ry="${hh/2}"/>`;
      else h+=`<rect class="hlp" style="stroke:${color}" x="${FX(pd[0]+w/2)-w/2}" y="${flipY(pd[3])}" width="${w}" height="${hh}" rx="${Math.min(w,hh)*0.15}"/>`; } }
  return h;
}
// Draw a single component's pads outlined (part cross-probe highlight).
function drawRef(ref){ let h='';
  for(const pt of M.parts){ if(pt.ref!==ref) continue; if(hiddenLayers.has(pt.side?_LN[_LN.length-1]:_LN[0])) continue;
    for(const pd of pt.pads){ const w=pd[2]-pd[0], hh=pd[3]-pd[1];
      if(pd[4]) h+=`<ellipse class="hlp hlr" cx="${FX(pd[0]+w/2)}" cy="${flipY(pd[1]+hh/2)}" rx="${w/2}" ry="${hh/2}"/>`;
      else h+=`<rect class="hlp hlr" x="${FX(pd[0]+w/2)-w/2}" y="${flipY(pd[3])}" width="${w}" height="${hh}" rx="${Math.min(w,hh)*0.15}"/>`; } }
  return h;
}
function updateHighlight(){
  const aref=pinnedRef||hoverRef;    // a hovered/clicked part's pads take priority over the net
  const active = aref!=null || pinnedList.length>0 || hoverNet!=null;
  scene.classList.toggle('dim', active);
  if(!active){ hlg.innerHTML=''; return; }
  let h='';
  if(aref) h+=drawRef(aref);
  else if(pinnedList.length) for(const pl of pinnedList) h+=drawNet(pl.roots,pl.color);   // each in its colour
  else if(hoverNet) h+=drawNet(hoverNet,'#FFF3C4');                                        // transient hover
  hlg.innerHTML=h;
}
function renderNetLegend(){
  const el=document.getElementById('netlegend'); if(!el) return;
  if(!pinnedList.length){ el.style.display='none'; el.innerHTML=''; return; }
  el.style.display='';
  el.innerHTML='<div class="ll">Highlighted nets</div>'+pinnedList.map((p,i)=>
    `<div class="nl"><i style="background:${p.color}"></i><span title="${esc(p.name||'net')}">${esc(p.name||'net')}</span><b data-rm="${i}" title="Remove">&times;</b></div>`).join('');
  el.querySelectorAll('b[data-rm]').forEach(b=>b.onclick=e=>{ e.stopPropagation();
    pinnedList.splice(+b.dataset.rm,1); syncPinnedNet(); updateHighlight(); renderNetLegend(); });
}
function _distSeg(px,py,ax,ay,bx,by){ const dx=bx-ax,dy=by-ay,l2=dx*dx+dy*dy; let t=l2?((px-ax)*dx+(py-ay)*dy)/l2:0; t=Math.max(0,Math.min(1,t)); return Math.hypot(px-(ax+t*dx),py-(ay+t*dy)); }
function pickTraceNet(bx,by){
  const C=M.copper, tol=14/view.k; let best=-1,bd=1e18; _gen++;   // ~14px tolerance
  const gx0=Math.floor((bx-tol)/PG),gx1=Math.floor((bx+tol)/PG),gy0=Math.floor((by-tol)/PG),gy1=Math.floor((by+tol)/PG);
  for(let gx=gx0;gx<=gx1;gx++) for(let gy=gy0;gy<=gy1;gy++){ const a=_pgrid.get(gx+','+gy); if(!a) continue;
    for(const i of a){ const si=i/7; if(_seen[si]===_gen) continue; _seen[si]=_gen; if(hiddenLayers.has(C[i+4])) continue;
      const dd=_distSeg(bx,by,C[i],C[i+1],C[i+2],C[i+3]); if(dd<bd){bd=dd;best=i;} } }
  return (best>=0 && bd<tol) ? traceRoot(best) : null;
}
function boardXY(e){ const r=svg.getBoundingClientRect(); const sx=(e.clientX-r.left-view.x)/view.k, sy=(e.clientY-r.top-view.y)/view.k; return [FX(sx), y1-sy]; }
// nearest pad / trace to a board point — used to snap a comment to the closest item
function _closestOnSeg(px,py,ax,ay,bx,by){ const dx=bx-ax,dy=by-ay,l2=dx*dx+dy*dy; let t=l2?((px-ax)*dx+(py-ay)*dy)/l2:0; t=Math.max(0,Math.min(1,t)); return [ax+t*dx,ay+t*dy]; }
function nearestPad(bx,by){ let bd=1e30,best=null;   // nearest by pad rect; point at the PART centre
  for(const pt of M.parts){ if(hiddenLayers.has(pt.side?_LN[_LN.length-1]:_LN[0])) continue;
    for(const pd of pt.pads){ const dx=Math.max(pd[0]-bx,0,bx-pd[2]),dy=Math.max(pd[1]-by,0,by-pd[3]),d=dx*dx+dy*dy;
      if(d<bd){bd=d;best={ref:pt.ref,x:pt.x,y:pt.y};} } }   // pt.x/pt.y = pad centroid (component centre)
  return best?{...best,d:Math.sqrt(bd)}:null; }
function nearestTrace(bx,by){ const C=M.copper, R=120/view.k; let bd=1e30,bi=-1,cx=0,cy=0; _gen++;   // only within snap range
  const gx0=Math.floor((bx-R)/PG),gx1=Math.floor((bx+R)/PG),gy0=Math.floor((by-R)/PG),gy1=Math.floor((by+R)/PG);
  for(let gx=gx0;gx<=gx1;gx++) for(let gy=gy0;gy<=gy1;gy++){ const a=_pgrid.get(gx+','+gy); if(!a) continue;
    for(const i of a){ const si=i/7; if(_seen[si]===_gen) continue; _seen[si]=_gen; if(hiddenLayers.has(C[i+4])) continue;
      const q=_closestOnSeg(bx,by,C[i],C[i+1],C[i+2],C[i+3]),dx=bx-q[0],dy=by-q[1],d=dx*dx+dy*dy; if(d<bd){bd=d;bi=i;cx=q[0];cy=q[1];} } }
  return bi<0?null:{d:Math.sqrt(bd),root:traceRoot(bi),x:cx,y:cy}; }
// ---- cross-probe: canonical xnets <-> layout net roots, modal preview of the schematic ----
const QP=new URLSearchParams(location.search), MODALMODE=QP.get('modal')==='1';
const rootToXnet=new Map(), xnetRoots=[];
(function initXP(){
  if(!XP||!XP.xnets) return;
  XP.xnets.forEach((xn,i)=>{ const roots=new Set();
    for(const r of (xn.reps||[])){ const rt=_find(nqk(r[0],r[1])+'@'+r[2]); roots.add(rt); if(!rootToXnet.has(rt)) rootToXnet.set(rt,i); }
    xnetRoots[i]=roots; });
})();
function zoomToBox(b,m,minCtx){ if(!b) return; if(flipped) b=[FX(b[2]),b[1],FX(b[0]),b[3]]; const rr=svg.getBoundingClientRect(); m=(m==null?0.3:m);
  // minCtx: minimum context window so a tiny target doesn't zoom past all context
  const mc=(minCtx==null?90000:minCtx);
  const bw=Math.max((b[2]-b[0]),mc), bh=Math.max((b[3]-b[1]),mc);
  view.k=Math.min(rr.width/(bw*(1+m)), rr.height/(bh*(1+m)), baseK*30);
  view.x=rr.width/2-((b[0]+b[2])/2)*view.k; view.y=rr.height/2-flipY((b[1]+b[3])/2)*view.k; applyView(); }
function xnetOfNet(net){ if(!net) return null; for(const r of net) if(rootToXnet.has(r)) return rootToXnet.get(r); return null; }
function refBox(ref){ const p=M.parts.find(q=>q.ref===ref); if(!p||!p.pads.length) return null;
  let b=[1e18,1e18,-1e18,-1e18]; for(const pd of p.pads){ b=[Math.min(b[0],pd[0]),Math.min(b[1],pd[1]),Math.max(b[2],pd[2]),Math.max(b[3],pd[3])]; } return b; }
const modal=document.getElementById('xmodal'), mframe=document.getElementById('xframe'),
      mtitle=document.getElementById('xtitle'), mopen=document.getElementById('xopen');
// Companion (schematic) URL preserving doc/rev/diff, so "Open full" lands on the
// same revision in this same tab.
function companionUrl(extra){ const u=new URLSearchParams(location.search); ['ref','xnet','xcolor','modal'].forEach(k=>u.delete(k));
  for(const k in extra) u.set(k,extra[k]); return XP.companion+'?'+u.toString(); }
// The preview iframe loads once, then retargets via postMessage — so hovering nets
// updates the peek instantly instead of reloading the whole schematic each time.
let _mReady=false, _mPending=null, _mLoaded=false;
if(mframe) mframe.addEventListener('load',()=>{ _mReady=true; if(_mPending){ mframe.contentWindow.postMessage(_mPending,'*'); _mPending=null; } });
function showProbe(target,title,peek){ if(!XP||!XP.companion||MODALMODE) return;
  mtitle.textContent=title; mopen.href=companionUrl(target);
  modal.classList.toggle('peek',!!peek); modal._peek=!!peek; modal.style.display='flex';
  const payload={type:'xprobe', ...target};
  if(!_mLoaded){ _mLoaded=true; _mReady=false; mframe.src=companionUrl(target)+'&modal=1'; _mPending=payload; }
  else if(_mReady) mframe.contentWindow.postMessage(payload,'*'); else _mPending=payload; }
function hideModal(){ if(modal){ modal.style.display='none'; modal.classList.remove('peek'); modal._peek=false; } }
function crossProbeNet(net,peek){ const xi=xnetOfNet(net); if(xi==null){ if(peek&&modal._peek) hideModal(); return; }
  showProbe({xnet:''+xi}, 'Schematic · '+(XP.xnets[xi].name||('net '+xi)), peek); }
function crossProbeRef(ref){ showProbe({ref:ref}, 'Schematic · '+ref, false); }
function bind(){
  // Pads are coalesced paths now (no per-pad DOM), so part hover/click is handled by
  // coordinate hit-testing (padAt) in the svg pointer handlers, and hover feedback is
  // drawn in the highlight layer (drawRef on hoverRef). Nothing to delegate here.
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
function pick(e){   // what's under the cursor — a part (its pad) takes priority over a trace's net
  const b=boardXY(e);
  const pad=padAt(b[0],b[1]);
  if(pad) return {ref:pad.ref};
  const n=pickTraceNet(b[0],b[1]);
  return n!=null ? {net:n} : null;
}
let drag=null, down=null, hoverKey=null, _peekT=null;
const pinned=()=>pinnedNet!==null||pinnedRef!==null;
svg.addEventListener('pointerdown',e=>{ if(MODALMODE)return; down={x:e.clientX,y:e.clientY,moved:false}; const bd=boardXY(e); if(padAt(bd[0],bd[1]))return; drag={x:e.clientX,y:e.clientY,vx:view.x,vy:view.y}; svg.setPointerCapture(e.pointerId); });
svg.addEventListener('pointermove',e=>{
  if(MODALMODE)return;
  if(down && Math.abs(e.clientX-down.x)+Math.abs(e.clientY-down.y)>4) down.moved=true;
  if(drag){ view.x=drag.vx+e.clientX-drag.x; view.y=drag.vy+e.clientY-drag.y; applyView(); return; }
  if(!pinned() && !Comments.isPlacing()){           // hover-highlight when nothing is pinned
    const p=pick(e), key=p?(p.ref?'r:'+p.ref:'n:'+p.net):null;
    if(key!==hoverKey){ hoverKey=key; hoverRef=p&&p.ref||null; hoverNet=(p&&p.net!=null)?new Set([p.net]):null; updateHighlight();
      if(p&&p.ref){ const pt=_byRef[p.ref], ty=pt&&M.types[pt.t];   // part → refdes + value + type
        tip.textContent=p.ref+(pt&&pt.val?'  '+pt.val:'')+'\n'+((ty&&ty.label)||(pt&&pt.t)||''); tip.style.opacity=1; }
      else if(p&&p.net!=null&&_rootName[p.net]){ tip.textContent=_rootName[p.net]; tip.style.opacity=1; }
      else tip.style.opacity=0;
      // peek the schematic for the hovered net (debounced; iframe retargets via postMessage)
      clearTimeout(_peekT);
      if(p&&p.net!=null){ const nn=p.net; _peekT=setTimeout(()=>{ if(hoverKey==='n:'+nn) crossProbeNet(new Set([nn]),true); }, 200); }
      else if(modal._peek) hideModal();
    }
    if(tip.style.opacity==='1'){ tip.style.left=(e.clientX+14)+'px'; tip.style.top=(e.clientY+8)+'px'; }   // follow cursor
  }
});
svg.addEventListener('pointerleave',()=>{ clearTimeout(_peekT); if(modal._peek) hideModal();
  if(!pinned() && hoverKey!==null){ hoverKey=null; hoverRef=null; hoverNet=null; updateHighlight(); } });
svg.addEventListener('pointerup',e=>{
  if(MODALMODE)return;
  if(down && !down.moved){
    if(Comments.isPlacing()){ Comments.place(e.clientX,e.clientY); drag=null; down=null; return; }
    const p=pick(e); hoverKey=null; hoverRef=null; hoverNet=null;
    if(!p){ pinnedList=[]; pinnedRef=null; syncPinnedNet(); updateHighlight(); renderNetLegend(); hideModal(); }
    else if(p.ref){                                 // a part → highlight it + preview it in the schematic
      if(pinnedRef===p.ref){ pinnedRef=null; updateHighlight(); hideModal(); }
      else { pinnedRef=p.ref; pinnedList=[]; syncPinnedNet(); updateHighlight(); renderNetLegend(); crossProbeRef(p.ref); }
    } else {                                         // a trace → toggle the net in the coloured highlight set
      const n=p.net, hit=pinnedList.findIndex(pl=>pl.roots.has(n));
      if(hit>=0){ pinnedList.splice(hit,1); syncPinnedNet(); updateHighlight(); renderNetLegend(); hideModal(); }
      else { const xi=xnetOfNet(new Set([n])), roots=(xi!=null?xnetRoots[xi]:new Set([n]));
        pinnedRef=null; pinnedList.push({ roots, color:nextColor(), name:netNameOf(xi,n), xnet:(xi!=null?xi:null) });
        syncPinnedNet(); updateHighlight(); renderNetLegend(); crossProbeNet(roots); }
    }
  }
  drag=null; down=null;
});
svg.addEventListener('wheel',e=>{ if(MODALMODE)return; e.preventDefault(); const r=svg.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top;
  const f=Math.exp(-e.deltaY*0.0015),nk=view.k*f; view.x=mx-(mx-view.x)*(nk/view.k); view.y=my-(my-view.y)*(nk/view.k); view.k=nk; applyView(); },{passive:false});
document.getElementById('fit').onclick=fit;
if(XP&&XP.companion) document.getElementById('to-sch').onclick=()=>location.href=schematicHref();
else document.getElementById('to-sch').style.display='none';
document.getElementById('flip').onclick=e=>{ flipped=!flipped; e.currentTarget.style.background=flipped?'#EFE9DB':''; render(); if(window.Comments&&Comments.reproject) Comments.reproject(); };
document.getElementById('zi').onclick=()=>zoomBy(1.3);
document.getElementById('zo').onclick=()=>zoomBy(0.77);
window.addEventListener('resize',fit);
// left panel: clickable copper layers
// Copper layers are ordered top→bottom (ETCH subclass index ascending). Name them
// by their position in the stackup: outer layers Top/Bottom, the rest Inner N.
function layerName(i,n){ return i===0?'Top':(i===n-1?'Bottom':'Inner '+i); }
function renderLayers(){
  const n=(M.layers||[]).length;
  const rows=(M.layers||[]).map((l,i)=>`<div class="lyr${hiddenLayers.has(l)?' off':''}" data-l="${l}"><i style="background:${M.layerColors[l]}"></i>${layerName(i,n)}</div>`).join('');
  const oRow=(M.outline&&M.outline.length)?`<div class="lyr${showOutline?'':' off'}" data-l="outline"><i style="background:#8A8577"></i>Outline</div>`:'';
  document.getElementById('layers').innerHTML='<div class="lh">Copper layers</div>'+rows+oRow;
  document.getElementById('layers').querySelectorAll('.lyr').forEach(el=>el.onclick=()=>{
    if(el.dataset.l==='outline'){ showOutline=!showOutline; renderLayers(); render(); return; }
    const l=+el.dataset.l; if(hiddenLayers.has(l))hiddenLayers.delete(l);else hiddenLayers.add(l); renderLayers(); render();});
}
renderLayers();
document.getElementById('nm').textContent=M.name;
document.getElementById('sub').textContent=M.parts.length+' components · '+((M.copper||[]).length/7|0)+' traces · '+((M.vias||[]).length/4|0)+' vias · '+(M.layers||[]).length+' layers';
if(MODALMODE) document.body.classList.add('modal');
if(modal){ document.getElementById('xclose').onclick=hideModal;
  modal.addEventListener('click',ev=>{ if(ev.target===modal) hideModal(); }); }
DBG.log('initial render: start (building scene SVG)…');
render(); fit();
DBG.log('initial render: done', {sceneNodes: scene.childElementCount, baseK: baseK});
if(DIFF){ const el=document.getElementById('sub');
  el.innerHTML+=` · <span style="color:#8B8578">vs ${DIFF.label}</span>`+
    ` <span style="padding:1px 6px;border-radius:9px;background:rgba(26,127,55,.14);color:#1A7F37">+${DIFF.added.length}</span>`+
    ` <span style="padding:1px 6px;border-radius:9px;background:rgba(207,34,46,.13);color:#CF222E">&#8722;${DIFF.removed.length}</span>`+
    ` <span style="padding:1px 6px;border-radius:9px;background:rgba(154,103,0,.16);color:#9A6700">&#8703;${DIFF.moved.length} moved</span>`+
    ((DIFF.addedCu.length||DIFF.removedCu.length)?` &nbsp;<span style="color:#8B8578">copper</span>`+
       ` <span style="padding:1px 6px;border-radius:9px;background:rgba(26,127,55,.14);color:#1A7F37">+${DIFF.addedCu.length/4|0}</span>`+
       ` <span style="padding:1px 6px;border-radius:9px;background:rgba(207,34,46,.13);color:#CF222E">&#8722;${DIFF.removedCu.length/4|0}</span> segs`:'')+
    (DIFF.newVias!==DIFF.oldVias?` <span style="color:#8B8578">vias ${DIFF.oldVias}&#8594;${DIFF.newVias}</span>`:''); }
(function renderRevUI(){
  const meta=window.__docMeta, el=document.getElementById('tb-rev');
  if(!el||!meta||!(meta.rev>1)) return;
  const revs=(meta.revs&&meta.revs.length?meta.revs.map(r=>+r.n):Array.from({length:meta.rev},(_,i)=>i+1)).sort((a,b)=>b-a);
  const url=(r,dv)=>{const u=new URLSearchParams(location.search);u.set('rev',r);if(dv)u.set('diff',dv);else u.delete('diff');return '?'+u.toString();};
  el.style.display='flex';
  el.innerHTML=`<button class="tbtn" id="tb-rev-btn" style="min-width:64px">Rev ${meta.curRev}${meta.diffRev?' vs '+meta.diffRev:''} &#9662;</button>`+
    `<div id="tb-rev-menu" style="display:none;position:absolute;top:40px;right:0;min-width:190px;background:#fff;border:1px solid #E0DCD1;border-radius:10px;box-shadow:0 14px 32px rgba(24,20,10,.16);padding:5px;z-index:90"></div>`;
  const menu=document.getElementById('tb-rev-menu');
  menu.innerHTML=revs.map(r=>
    `<a href="${url(r)}" style="display:block;padding:7px 9px;border-radius:6px;text-decoration:none;color:#221F1A;font-size:12.5px;${r===meta.curRev&&!meta.diffRev?'background:#F1EDE3;font-weight:600':''}">Rev ${r}${r===meta.rev?' (latest)':''}</a>`+
    (r<meta.curRev?`<a href="${url(meta.curRev,r)}" style="display:block;padding:5px 9px 7px 22px;border-radius:6px;text-decoration:none;color:#9A6700;font-size:11.5px;${meta.diffRev===r?'background:#F9F3E4;font-weight:600':''}">&Delta; diff vs rev ${r}</a>`:'')
  ).join('')+(meta.diffRev?`<a href="${url(meta.curRev)}" style="display:block;padding:7px 9px;border-radius:6px;text-decoration:none;color:#CF222E;font-size:11.5px;border-top:1px solid #EEE9DE;margin-top:4px">clear diff</a>`:'');
  document.getElementById('tb-rev-btn').onclick=e=>{e.stopPropagation();menu.style.display=menu.style.display==='none'?'block':'none';};
  document.addEventListener('click',()=>{menu.style.display='none';});
})();
// ---- comments: anchor to a pad (part), a trace's net, or an open point ----
if(!MODALMODE) window.__cmtContext='brd:'+(M.name||'');   // shared with the Firebase backend bootstrap
if(!MODALMODE) Comments.init({
  context:window.__cmtContext, svg:svg, stage:document.getElementById('stage'),
  rev:()=>(window.__docMeta||{}).curRev||1,          // comments locked to their rev
  latestRev:()=>(window.__docMeta||{}).rev||1,       // legacy (unversioned) comments belong to latest
  diffRev:()=>(window.__docMeta||{}).diffRev||null,  // diff view shows both (new / gone)
  button:document.getElementById('cmt-btn'),
  project:(x,y)=>({sx:FX(x)*view.k+view.x, sy:flipY(y)*view.k+view.y}),
  resolveAnchor:(cx,cy)=>{
    const r=svg.getBoundingClientRect();
    const bx=FX((cx-r.left-view.x)/view.k), by=y1-((cy-r.top-view.y)/view.k);
    const tol=90/view.k;                       // how near an item must be to point at it
    const pad=nearestPad(bx,by), tr=nearestTrace(bx,by);
    const pD=pad?pad.d:1e30, tD=tr?tr.d:1e30;   // anchor stays at the cursor (bx,by); tx/ty is the item to point at
    if(pD<=tD && pD<tol) return {kind:'pad',x:bx,y:by,ref:pad.ref,label:'Part '+pad.ref,tx:pad.x,ty:pad.y};
    if(tD<tol){ const xi=xnetOfNet(new Set([tr.root])); const nm=_rootName[tr.root]||(xi!=null&&XP?(XP.xnets[xi].name||''):''); return {kind:'net',x:bx,y:by,ref:(xi!=null?'xnet'+xi:null),label:'Net'+(nm?' '+nm:''),tx:tr.x,ty:tr.y}; }
    return {kind:'point',x:bx,y:by,label:'Open space'};
  }
});
// apply a cross-probe target (from ?xnet/?ref on load, or a postMessage from an
// embedding parent so a live preview can update without reloading the page).
let _curKeep=undefined;                            // currently isolated layer (null=all shown)
function applyTarget(t){
  t=t||{};
  pinnedList=[]; pinnedRef=null; hoverNet=null; hoverRef=null;
  let keep=null, part=null;
  if(t.ref){ part=M.parts.find(q=>q.ref===t.ref); if(part){ pinnedRef=t.ref; keep=part.side?_LN[_LN.length-1]:_LN[0]; } }
  else {                                            // one or more xnets → highlight each in its colour
    const xs=(t.xnets!=null?t.xnets:(t.xnet!=null?[+t.xnet]:[])).filter(i=>xnetRoots[i]);
    const cols=t.xcolors||[];
    pinnedList=xs.map((i,k)=>({ roots:xnetRoots[i], color:(cols[k]||PALETTE[k%PALETTE.length]), name:netNameOf(i), xnet:i }));
  }
  syncPinnedNet();
  if(keep!==_curKeep){                             // layer visibility changed → full re-render
    _curKeep=keep; hiddenLayers.clear(); if(keep!=null) _LN.forEach(l=>{ if(l!==keep) hiddenLayers.add(l); });
    renderLayers(); render();
  } else updateHighlight();                         // same layers → cheap overlay update
  renderNetLegend();
  if(pinnedXnets.length && XP) zoomToBox(XP.xnets[pinnedXnets[0]].bbox,0.5);
  else if(part) zoomToBox(refBox(t.ref),0.25,12000);
}
function applyParams(){ const xnet=QP.get('xnet'), xcolor=QP.get('xcolor'), ref=QP.get('ref');
  if(xnet!==null) applyTarget({xnets: xnet.split(',').map(s=>+s), xcolors:(xcolor||'').split(',').map(c=>c?('#'+c):'')});
  else if(ref) applyTarget({ref}); }
window.addEventListener('message',e=>{ const d=e.data; if(d&&d.type==='xprobe') applyTarget(d); });
if(QP.get('xnet')!==null || QP.get('ref')){
  if(document.readyState==='complete') setTimeout(applyParams,40);
  else window.addEventListener('load',()=>setTimeout(applyParams,40));
}
DBG.log('renderModel: completed OK');
} catch (err) {
  DBG.error('renderModel FAILED:', (err && (err.stack || err.message)) || err);
  try { const s=document.getElementById('svg'); if(s) s.insertAdjacentHTML('afterend',
    '<div style="position:fixed;left:12px;bottom:12px;max-width:64ch;padding:11px 13px;'
    +'background:#7f1d1d;color:#fff;font:12px/1.45 ui-monospace,Menlo,monospace;border-radius:9px;z-index:99999">'
    +'⚠ Layout viewer hit an error. Open the browser console (⌥⌘J / Ctrl+Shift+J), copy the '
    +'<b>[CanvasPCB/layout]</b> lines, and send them.</div>'); } catch(_){}
  throw err;
}
};   // end window.__renderModel
/*__BOOT__*/
</script>
<!--__CMT_FIREBASE__-->
</body></html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("brd", type=Path)
    ap.add_argument("-o", "--output", type=Path, default=None)
    ap.add_argument("--bom", type=Path, default=None,
                    help="OrCAD BOM export for values (default: sibling <stem>.BOM)")
    ap.add_argument("--offline", action="store_true",
                    help="fully self-contained file: no web fonts, no comments backend")
    args = ap.parse_args()
    out = args.output or args.brd.parent / (args.brd.stem + "_pcb.html")
    bom = args.bom
    if bom is None:
        for c in (args.brd.with_suffix(".BOM"), args.brd.parent.parent / (args.brd.stem + ".BOM")):
            if c.exists():
                bom = c
                break
    generate(args.brd, out, bom, offline=args.offline)


if __name__ == "__main__":
    main()
