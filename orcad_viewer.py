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
import comment_ui           # noqa: E402


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
    # Per-designator values come from a sibling OrCAD BOM export (the schematic
    # streams don't carry them). Attach val to each part when present.
    bom = {}
    for ext in (".BOM", ".bom", ".Bom"):
        p = Path(dsn_path).with_suffix(ext)
        if p.exists():
            bom = oc.parse_bom(p)
            break
    if bom:
        n = 0
        for s in model["sheets"]:
            for part in s["parts"]:
                v = bom.get(part["des"])
                if v:
                    part["val"] = v
                    n += 1
        model["hasValues"] = True
        print(f"  BOM: {p.name} → values on {n} parts")
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
        gs = [{"des": p["des"], "box": p["box"], "sym": p["sym"],
               "pins": p["pins"], "val": p.get("val"), "pkg": p["pkg"]}
              for p in s["parts"] if p["des"] in removed and p["box"]]
        if gs:
            ghosts[s["id"]] = gs
    model["removedGeom"] = ghosts

    def wkey(w):   # orientation-independent; MUST match the JS wkey string form
        a, b = (round(w[0]), round(w[1])), (round(w[2]), round(w[3]))
        if a > b:
            a, b = b, a
        return f"{a[0]},{a[1]},{b[0]},{b[1]}"
    old_by_id = {s["id"]: s for s in old_sheets}
    new_by_id = {s["id"]: s for s in model["sheets"]}
    added_wires, removed_wires = {}, {}
    for s in model["sheets"]:
        oset = {wkey(w) for w in old_by_id.get(s["id"], {}).get("wires", [])}
        a = [wkey(w) for w in s.get("wires", []) if wkey(w) not in oset]
        if a:
            added_wires[s["id"]] = a
    for s in old_sheets:
        ns = {wkey(w) for w in new_by_id.get(s["id"], {}).get("wires", [])}
        r = [w for w in s.get("wires", []) if wkey(w) not in ns]
        if r:
            removed_wires[s["id"]] = r
    model["addedWires"] = added_wires
    model["removedWires"] = removed_wires


def generate(dsn_path, out_path, diff_path=None, xprobe=None, shell=False, model=None, offline=False):
    # shell=True emits a data-free viewer that fetches the model after sign-in (hosted,
    # private). shell=False bakes the model in via /*__BOOT__*/ (standalone file).
    # `model` lets callers pass a pre-built model (e.g. from a non-OrCAD parser).
    # offline=True (embedded only): drop the Firebase/comments backend bootstrap and
    # the web-font links so the file references NOTHING on the network — pure, fully
    # self-contained viewer + cross-probe. (Comments/sign-in are omitted.)
    if shell:
        boot, fb = "", comment_ui.shell_bootstrap("schematic-viewer", "schematic")
        model = None
    else:
        if model is None:
            model = build_model(dsn_path, diff_path)
        boot = "window.__renderModel(" + json.dumps(model) + ", " + json.dumps(xprobe) + ");"
        fb = "" if offline else comment_ui.firebase_bootstrap("schematic-viewer")
    html = (HTML_TEMPLATE.replace("/*__BOOT__*/", boot)
                         .replace("/*__CMT_CSS__*/", comment_ui.CSS)
                         .replace("/*__CMT_JS__*/", comment_ui.JS)
                         .replace("<!--__CMT_FIREBASE__-->", fb))
    if offline:
        html = comment_ui.strip_webfonts(html)
    Path(out_path).write_text(html)
    if shell:
        print(f"Wrote {out_path}  (schematic shell, {Path(out_path).stat().st_size // 1024} KB)")
        return
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
<title>Schematic Viewer</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin="anonymous">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
html,body{margin:0;padding:0;height:100%;overflow:hidden;background:#E9E7E1;
  font-family:'IBM Plex Sans',system-ui,sans-serif;color:#221F1A}
*{box-sizing:border-box}
::-webkit-scrollbar{width:11px;height:11px}
::-webkit-scrollbar-thumb{background:#D3CEC2;border-radius:8px;border:3px solid transparent;background-clip:content-box}
::-webkit-scrollbar-track{background:transparent}
@keyframes schFlash{0%,100%{opacity:1}50%{opacity:.25}}
/* chrome */
.tbtn{padding:5px 11px;font-size:12px;font-weight:500;border:1px solid #D9D4C6;border-radius:8px;
  background:#FFFFFF;cursor:pointer;color:#3A362E;font-family:inherit;flex-shrink:0}
.tbtn:hover{border-color:#B9B3A2;background:#F7F5EF}
.tbtn.icon{min-width:34px;padding:5px 8px;font-size:14px;line-height:1}
.tbtn-mi{display:block;width:100%;text-align:left;padding:8px 9px;border:none;background:none;border-radius:6px;cursor:pointer;color:#221F1A;font:13px 'IBM Plex Sans',system-ui,sans-serif}
.tbtn-mi:hover{background:#F6F3EC}
.zbtn{border:none;background:transparent;padding:5px 9px;cursor:pointer;font-size:13px;color:#3A362E;font-family:inherit}
.zbtn:hover{background:#F7F5EF}
.srch{height:32px;width:270px;padding:0 10px 0 30px;font-family:'IBM Plex Mono',monospace;font-size:12px;
  border:1px solid #D9D4C6;border-radius:8px;background:#FFFFFF;color:#221F1A;outline:none}
.srch:focus{border-color:#C2410C;box-shadow:0 0 0 3px rgba(194,65,12,0.10)}
.sres:hover{background:#F6F4EE}
.iconx{border:none;background:transparent;cursor:pointer;color:#A19B8E;font-size:15px;line-height:1;padding:2px 6px;border-radius:5px}
.iconx:hover{background:#EFEBE0;color:#57524A}
.pinrow:hover{background:#F4F1E9}
.chip{font-family:'IBM Plex Mono',monospace;font-size:10px;padding:2px 8px;border-radius:999px;
  border:1px solid #E8CDB6;background:#FBF1E8;color:#B4530F;cursor:pointer}
.chip:hover{background:#F6E3D3}
.bomref{font-family:'IBM Plex Mono',monospace;font-size:10px;padding:2px 6px;border-radius:5px;
  border:1px solid #E4E0D3;background:#FFFFFF;color:#57524A;cursor:pointer}
.bomref:hover{border-color:#C2410C;color:#C2410C}
.pgrow{display:flex;gap:10px;align-items:center;padding:5px 12px 5px 20px;cursor:pointer;border-left:3px solid transparent}
.pgrow:hover{background:#F4F1E9}
.pgrow.active{border-left-color:#C2410C;background:#F1EDE1}
/* scene theme (scoped, dark override) */
.sch-stage{background:#E9E7E1}
.sch-stage.dark{background:#141519}
.sch-scene{
  --p-page:#FFFFFF; --p-pageline:#D9D4C7; --p-zone:#8F8A7D; --p-line:#C6C1B2;
  --p-glyph:#A9A395; --p-note:#6C675C; --p-wire:#0E7490; --p-pin:#BE123C;
  --p-comp:#92400E; --p-compfill:#FFFFFF; --p-pinnum:#A19B8E; --p-net:#4338CA;
  --p-jct:#BE123C; --p-ink:#221F1A; --p-hot:#EA580C;
}
.sch-scene.dark{
  --p-page:#1C1E24; --p-pageline:#31343D; --p-zone:#767B87; --p-line:#3A3E48;
  --p-glyph:#565B66; --p-note:#9BA1AB; --p-wire:#53C7BE; --p-pin:#F0716C;
  --p-comp:#E0A55C; --p-compfill:#22252C; --p-pinnum:#767B87; --p-net:#93A5FD;
  --p-jct:#F0716C; --p-ink:#E8E6E1; --p-hot:#FF8A3D;
}
.sch-scene .page-bg{fill:var(--p-page);stroke:var(--p-pageline)}
.sch-scene .pborder{fill:none;stroke:var(--p-zone);stroke-width:1;vector-effect:non-scaling-stroke}
.sch-scene .pborder.outer{stroke-dasharray:5 3}
.sch-scene .ztick{stroke:var(--p-zone);stroke-width:1;vector-effect:non-scaling-stroke}
.sch-scene .zlbl{fill:var(--p-note);font-family:'IBM Plex Sans',sans-serif;text-anchor:middle;dominant-baseline:central}
.sch-scene .glyph{stroke:var(--p-glyph);stroke-width:1;fill:none;vector-effect:non-scaling-stroke}
.sch-scene .gbox{fill:none;stroke:var(--p-line);stroke-width:1;vector-effect:non-scaling-stroke}
.sch-scene .note{fill:var(--p-note);font-family:'IBM Plex Sans',sans-serif}
.sch-scene .wire{stroke:var(--p-wire);stroke-width:1;fill:none;vector-effect:non-scaling-stroke}
.sch-scene .wire.bus{stroke-width:2.8}
.sch-scene .pin{fill:var(--p-pin)}
.sch-scene .comp rect{fill:var(--p-compfill);stroke:var(--p-comp);stroke-width:1;vector-effect:non-scaling-stroke}
.sch-scene .comp .sym{fill:none;stroke:var(--p-comp);stroke-width:1.4;vector-effect:non-scaling-stroke;stroke-linejoin:round;stroke-linecap:round}
.sch-scene .comp .sym.fill{fill:var(--p-comp)}
.sch-scene .comp .lead{stroke:var(--p-comp);stroke-width:1;vector-effect:non-scaling-stroke}
.sch-scene .comp .hit{fill:transparent;stroke:none}
.sch-scene .comp text{fill:var(--p-comp);font-family:'IBM Plex Mono',monospace;text-anchor:middle;dominant-baseline:middle}
.sch-scene .comp .lbl,.sch-scene .comp .val,.sch-scene .comp .pinname{font-family:'IBM Plex Mono',monospace;fill:var(--p-comp)}
.sch-scene .comp .pinnum{font-family:'IBM Plex Mono',monospace;fill:var(--p-pinnum)}
.sch-scene .nlabel{fill:var(--p-net);font-family:'IBM Plex Mono',monospace;dominant-baseline:middle}
.sch-scene .junction{fill:var(--p-jct);stroke:none}
.sch-scene .flag{fill:none;stroke:var(--p-comp);stroke-width:1.2;vector-effect:non-scaling-stroke;stroke-linejoin:round;stroke-linecap:round}
.sch-scene .flag.fill{fill:var(--p-comp)}
.sch-scene .flag.arrow{fill:var(--p-note);stroke:var(--p-note)}
.sch-scene .flabel{fill:var(--p-net);font-family:'IBM Plex Mono',monospace}
.sch-scene .flagg.hot .flag{stroke:var(--hot,var(--p-hot))}
.sch-scene .flagg.hot .flag.fill{fill:var(--hot,var(--p-hot))}
.sch-scene .flagg.hot .flabel{fill:var(--hot,var(--p-hot));font-weight:700}
.sch-scene .tb-cell{fill:var(--p-page);stroke:var(--p-ink);stroke-width:1;vector-effect:non-scaling-stroke}
.sch-scene .tb-lbl{fill:var(--p-pinnum);font-family:'IBM Plex Sans',sans-serif;dominant-baseline:central}
.sch-scene .tb-val{fill:var(--p-ink);font-family:'IBM Plex Sans',sans-serif;dominant-baseline:central}
.sch-scene .tb-title{fill:var(--p-ink);font-family:'IBM Plex Sans',sans-serif;font-weight:600}
.sch-scene .dim{opacity:.15}
.sch-scene .wire.hot{stroke:var(--hot,var(--p-hot));stroke-width:2.2}
.sch-scene .wire.bus.hot{stroke-width:3.6}
.sch-scene .pin.hot{fill:var(--hot,var(--p-hot))}
.sch-scene .nlabel.hot{fill:var(--hot,var(--p-hot));font-weight:700}
.sch-scene .comp.hot rect{stroke:var(--hot,var(--p-hot));stroke-width:2}
/* transient net highlight when hovering a net in the part card */
.sch-scene .wire.preview{stroke:var(--p-hot);stroke-width:2.6;opacity:1}
.sch-scene .wire.bus.preview{stroke-width:3.8}
.sch-scene .pin.preview{fill:var(--p-hot);opacity:1}
.sch-scene .nlabel.preview{fill:var(--p-hot);font-weight:700;opacity:1}
.sch-scene .flagg.preview{opacity:1}
.sch-scene .flagg.preview .flag{stroke:var(--p-hot)}
.sch-scene .flagg.preview .flabel{fill:var(--p-hot);font-weight:700}
.sch-scene .comp.sel rect{stroke:var(--p-hot);stroke-width:2}
.sch-scene .comp.sel .sym{stroke:var(--p-hot)}
.sch-scene .comp.sel .sym.fill{fill:var(--p-hot)}
.sch-scene .comp.sel text{fill:var(--p-hot)}
.sch-scene .comp.flash{animation:schFlash .5s ease 3}
/* diff overlay: recolor the actual component + trace, no bounding box */
.sch-scene .comp.add .sym,.sch-scene .comp.add rect:not(.hit){stroke:var(--p-add,#1A7F37)}
.sch-scene .comp.add .sym.fill{fill:var(--p-add,#1A7F37)}
.sch-scene .comp.add rect:not(.hit){fill:none}
.sch-scene .comp.add text{fill:var(--p-add,#1A7F37)}
.sch-scene .comp.chg .sym,.sch-scene .comp.chg rect:not(.hit){stroke:var(--p-chg,#9A6700)}
.sch-scene .comp.chg .sym.fill{fill:var(--p-chg,#9A6700)}
.sch-scene .comp.chg rect:not(.hit){fill:none}
.sch-scene .comp.chg text{fill:var(--p-chg,#9A6700)}
.sch-scene .comp.ghost .sym,.sch-scene .comp.ghost rect:not(.hit){stroke:var(--p-del,#CF222E)}
.sch-scene .comp.ghost .sym.fill{fill:var(--p-del,#CF222E)}
.sch-scene .comp.ghost rect:not(.hit){fill:none}
.sch-scene .comp.ghost text{fill:var(--p-del,#CF222E)}
.sch-scene .comp.ghost{opacity:.9}
.sch-scene .comp.add .pin,.sch-scene .comp.add circle{stroke:var(--p-add,#1A7F37)}
.sch-scene .wire.add,.sch-scene .pin.add{stroke:var(--p-add,#1A7F37)}
.sch-scene .wire.ghost{stroke:var(--p-del,#CF222E);opacity:.85}
.sch-mini .mini-page{fill:var(--p-page);stroke:var(--p-pageline);stroke-width:1;vector-effect:non-scaling-stroke}
.sch-mini .mini-wire{stroke:var(--p-wire);stroke-width:1;vector-effect:non-scaling-stroke;opacity:.5}
.sch-mini .mini-part{fill:var(--p-comp);opacity:.35}
.sch-mini .mini-vp{fill:rgba(234,88,12,0.10);stroke:#EA580C;stroke-width:1.4;vector-effect:non-scaling-stroke;cursor:grab}
body.xmodal .xtop,body.xmodal #sidebar,body.xmodal #minimap,body.xmodal #inspector{display:none!important}
body.xmodal #cmt-btn,body.xmodal #cmt-layer{display:none!important}
body.xmodal #svg{pointer-events:none}   /* embedded preview: static, no pan/zoom/hover/click */
/*__CMT_CSS__*/
</style></head>
<body>
<div id="shell-gate" style="position:fixed;inset:0;z-index:9999;background:#FBFAF7;display:none;align-items:center;justify-content:center;flex-direction:column;font-family:'IBM Plex Sans',system-ui,sans-serif">
  <div style="font-size:19px;font-weight:700;color:#221F1A;margin-bottom:6px">PCB Project</div>
  <div id="shell-gate-msg" style="font-size:14px;color:#8B8578;margin-bottom:20px">Loading&hellip;</div>
  <button id="shell-gate-btn" style="display:none;align-items:center;gap:8px;height:40px;padding:0 18px;border-radius:10px;border:1px solid #D9D4C6;background:#fff;color:#3A362E;font:600 14px 'IBM Plex Sans',system-ui,sans-serif;cursor:pointer">Sign in with Google</button>
</div>
<div style="position:relative;height:100vh;display:flex;flex-direction:column;overflow:hidden">
  <!-- toolbar -->
  <div class="xtop" style="display:flex;align-items:center;gap:12px;height:54px;padding:0 14px;background:#FBFAF7;border-bottom:1px solid #E0DCD1;flex-shrink:0;z-index:40;position:relative">
    <button id="tb-sheets-btn" class="tbtn" style="display:none">Sheets</button>
    <button class="tbtn" onclick="location.href='../'" title="Back to projects" style="flex-shrink:0">&#8592; Projects</button>
    <button class="tbtn" id="to-lay" title="View the board layout" style="flex-shrink:0">Layout</button>
    <div style="width:1px;height:26px;background:#E0DCD1;flex-shrink:0"></div>
    <div style="display:flex;flex-direction:column;gap:1px;min-width:0">
      <div id="tb-name" style="font-size:14px;font-weight:700;letter-spacing:.01em;line-height:1.15;white-space:nowrap">Schematic</div>
      <div id="tb-sub" style="font-size:10.5px;color:#8B8578;font-family:'IBM Plex Mono',monospace;line-height:1.2;white-space:nowrap">loading…</div>
    </div>
    <div style="width:1px;height:26px;background:#E0DCD1;flex-shrink:0"></div>
    <div style="position:relative;flex-shrink:0">
      <svg viewBox="0 0 16 16" style="position:absolute;left:9px;top:9px;width:14px;height:14px;pointer-events:none">
        <circle cx="7" cy="7" r="4.5" fill="none" stroke="#A19B8E" stroke-width="1.5"></circle>
        <line x1="10.5" y1="10.5" x2="14" y2="14" stroke="#A19B8E" stroke-width="1.5" stroke-linecap="round"></line>
      </svg>
      <input id="tb-search" class="srch" placeholder="Find part or net…" autocomplete="off" spellcheck="false">
      <div id="tb-drop" style="display:none;position:absolute;top:37px;left:0;width:330px;background:#FFFFFF;border:1px solid #E0DCD1;border-radius:10px;box-shadow:0 14px 32px rgba(24,20,10,0.16);overflow:hidden;z-index:80"></div>
    </div>
    <div id="tb-crumb" style="font-size:10.5px;font-family:'IBM Plex Mono',monospace;color:#8B8578;white-space:nowrap;overflow:hidden;text-overflow:ellipsis"></div>
    <div id="tb-diff" style="display:none;align-items:center;gap:6px;font-family:'IBM Plex Mono',monospace;font-size:11px"></div>
    <div style="flex:1"></div>
    <button id="cmt-btn" class="tbtn icon" title="Add / view comments">&#128172;</button>
    <button id="tb-fit" class="tbtn">Fit</button>
    <div style="display:flex;align-items:center;border:1px solid #D9D4C6;border-radius:8px;background:#FFFFFF;overflow:hidden;flex-shrink:0">
      <button id="tb-zout" class="zbtn">&#8722;</button>
      <span id="tb-zoom" style="font-family:'IBM Plex Mono',monospace;font-size:11px;min-width:44px;text-align:center;color:#3A362E">100%</span>
      <button id="tb-zin" class="zbtn">+</button>
    </div>
    <div id="tb-rev" style="display:none;position:relative;flex-shrink:0"></div>
    <button id="cmt-share" class="tbtn" style="display:none;flex-shrink:0" title="Share this project">Share</button>
    <div style="position:relative;flex-shrink:0">
      <button id="tb-more" class="tbtn icon" title="More">&#8943;</button>
      <div id="tb-more-menu" style="display:none;position:absolute;top:40px;right:0;min-width:184px;background:#fff;border:1px solid #E0DCD1;border-radius:10px;box-shadow:0 14px 32px rgba(24,20,10,.16);padding:5px;z-index:90">
        <button id="tb-theme" class="tbtn-mi" title="Toggle canvas theme">Dark canvas</button>
        <button id="tb-bom" class="tbtn-mi">Bill of materials</button>
      </div>
    </div>
    <div id="cmt-account" style="margin-left:6px;flex-shrink:0"></div>
  </div>
  <!-- main -->
  <div style="display:flex;flex:1;min-height:0;position:relative">
    <div id="sidebar" style="width:252px;flex-shrink:0;border-right:1px solid #E0DCD1;background:#FBFAF7;overflow-y:auto;padding:2px 0 12px"></div>
    <div id="stage" class="sch-stage" style="flex:1;position:relative;overflow:hidden;min-width:0">
      <svg id="svg" class="sch-scene" style="width:100%;height:100%;display:block;cursor:default;touch-action:none;user-select:none;-webkit-user-select:none"><g id="scene"></g></svg>
      <div id="status" style="position:absolute;left:12px;bottom:12px;z-index:10;max-width:60%"></div>
      <div id="minimap" style="position:absolute;right:12px;bottom:12px;background:rgba(251,250,247,0.92);border:1px solid #E0DCD1;border-radius:10px;padding:5px;box-shadow:0 8px 20px rgba(24,20,10,0.10)">
        <svg id="mini" class="sch-scene sch-mini" style="display:block;width:188px;height:126px;touch-action:none"></svg>
      </div>
    </div>
    <div id="inspector" style="display:none;position:absolute;top:14px;right:14px;width:300px;max-height:calc(100% - 28px);z-index:35;background:rgba(251,250,247,0.97);border:1px solid #E0DCD1;border-radius:12px;box-shadow:0 16px 40px rgba(20,16,8,0.22);overflow-y:auto;-webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px)"></div>
  </div>
  <!-- bom modal -->
  <div id="bom" style="display:none"></div>
  <!-- tooltip -->
  <div id="tip" style="position:fixed;left:0;top:0;pointer-events:none;background:#221F1A;color:#FBFAF7;font-family:'IBM Plex Mono',monospace;font-size:11px;padding:4px 8px;border-radius:6px;opacity:0;transition:opacity .1s;max-width:320px;z-index:200;white-space:pre-line"></div>
</div>
<script>
/* ── SchRender: pure SVG builders (ported from render-core.js) ── */
(function () {
  const esc = s => (s + '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const isBus = n => /\[\d+\.\.\d+\]/.test(String(n));
  function borderSVG(fr) {
    const [fx, fy, fX, fY] = fr, W = fX - fx, H = fY - fy, D = 22;
    const ix = fx + D, iy = fy + D, iX = fX - D, iY = fY - D;
    const ncols = Math.max(4, Math.min(12, Math.round(W / 210)));
    const nrows = Math.max(3, Math.min(10, Math.round(H / 210)));
    let s = `<rect class="pborder outer" x="${fx}" y="${fy}" width="${W}" height="${H}"/>` +
      `<rect class="pborder" x="${ix}" y="${iy}" width="${iX - ix}" height="${iY - iy}"/>`;
    const fs = Math.min(D * 0.7, 14);
    for (let i = 0; i < ncols; i++) {
      const x0 = ix + i * (iX - ix) / ncols, xc = x0 + (iX - ix) / ncols / 2;
      if (i > 0) s += `<line class="ztick" x1="${x0}" y1="${fy}" x2="${x0}" y2="${iy}"/>` +
        `<line class="ztick" x1="${x0}" y1="${iY}" x2="${x0}" y2="${fY}"/>`;
      const n = (ncols - i);
      s += `<text class="zlbl" x="${xc}" y="${fy + D / 2}" font-size="${fs}">${n}</text>` +
        `<text class="zlbl" x="${xc}" y="${fY - D / 2}" font-size="${fs}">${n}</text>`;
    }
    for (let j = 0; j < nrows; j++) {
      const y0 = iy + j * (iY - iy) / nrows, yc = y0 + (iY - iy) / nrows / 2;
      if (j > 0) s += `<line class="ztick" x1="${fx}" y1="${y0}" x2="${ix}" y2="${y0}"/>` +
        `<line class="ztick" x1="${iX}" y1="${y0}" x2="${fX}" y2="${y0}"/>`;
      const L = String.fromCharCode(65 + (nrows - 1 - j));
      s += `<text class="zlbl" x="${fx + D / 2}" y="${yc}" font-size="${fs}">${L}</text>` +
        `<text class="zlbl" x="${fX - D / 2}" y="${yc}" font-size="${fs}">${L}</text>`;
    }
    return s;
  }
  function titleblockSVG(tb, geom) {
    if (!tb || !geom) return '';
    const ox = tb.ox, oy = tb.oy;
    let s = '';
    for (const r of geom.rects)
      s += `<rect class="tb-cell" x="${ox + r[0]}" y="${oy + r[1]}" width="${r[2] - r[0]}" height="${r[3] - r[1]}"/>`;
    for (const L of geom.lines)
      s += `<line class="tb-cell" x1="${ox + L[0]}" y1="${oy + L[1]}" x2="${ox + L[2]}" y2="${oy + L[3]}"/>`;
    for (const t of geom.labels)
      s += `<text class="tb-lbl" x="${ox + t.x}" y="${oy + t.y}" font-size="9">${esc(t.s)}</text>`;
    const title = ('' + tb.title);
    const tfs = Math.max(6, Math.min(14, 336 / Math.max(title.length * 0.6, 1)));
    s += `<text class="tb-title" x="${ox + 180}" y="${oy + 34}" font-size="${tfs.toFixed(1)}" text-anchor="middle">${esc(title)}</text>`;
    s += `<text class="tb-val" x="${ox + 180}" y="${oy + 64}" font-size="9" text-anchor="middle">${esc(tb.company)}</text>`;
    s += `<text class="tb-val" x="${ox + 40}" y="${oy + 108}" font-size="10">${esc(tb.size)}</text>`;
    if (tb.rev) s += `<text class="tb-val" x="${ox + 190}" y="${oy + 108}" font-size="10">${esc(tb.rev)}</text>`;
    if (tb.date) s += `<text class="tb-val" x="${ox + 40}" y="${oy + 130}" font-size="9">${esc(tb.date)}</text>`;
    s += `<text class="tb-val" x="${ox + 248}" y="${oy + 131}" font-size="9" text-anchor="middle">${tb.n}</text>`;
    s += `<text class="tb-val" x="${ox + 285}" y="${oy + 131}" font-size="9" text-anchor="middle">${tb.total}</text>`;
    return s;
  }
  function flagSVG(f, ctr) {
    const x = f.x, y = f.y;
    const dir = { u: [0, -1], d: [0, 1], l: [-1, 0], r: [1, 0] }[f.orient || 'd'];
    const ux = dir[0], uy = dir[1], vx = -uy, vy = ux;
    const P = (t, s) => [x + ux * t + vx * s, y + uy * t + vy * s];
    const M = p => p[0].toFixed(1) + ' ' + p[1].toFixed(1);
    const L = (a, b) => `<line class="flag" x1="${a[0].toFixed(1)}" y1="${a[1].toFixed(1)}" x2="${b[0].toFixed(1)}" y2="${b[1].toFixed(1)}"/>`;
    const nk = f.key ? ` data-net="${esc(f.key)}"` : '';
    const vert = (f.orient === 'u' || f.orient === 'd');
    const sideLabel = () => {
      if (!f.net) return '';
      const lp = P(-3, -3.5);
      const lx = lp[0].toFixed(1), ly = lp[1].toFixed(1);
      const anc = vert ? (f.orient === 'd' ? 'start' : 'end') : (f.orient === 'l' ? 'start' : 'end');
      const rot = vert ? ` transform="rotate(-90 ${lx} ${ly})"` : '';
      return `<text class="flabel" x="${lx}" y="${ly}" font-size="8" text-anchor="${anc}" dominant-baseline="central"${rot}>${esc(f.net)}</text>`;
    };
    let g = `<g class="flagg"${nk}>`;
    if (f.kind === 'gnd') {
      g += L(P(0, 0), P(4, 0)) + L(P(4, -5), P(4, 5)) + L(P(7, -3), P(7, 3)) + L(P(10, -1.5), P(10, 1.5));
      g += sideLabel();
    } else if (f.kind === 'pwr') {
      g += L(P(0, -5), P(0, 5));
      g += sideLabel();
    } else {
      const name = f.net || '', fs = 8;
      const toCenter = ctr ? (ux * (ctr[0] - x) + uy * (ctr[1] - y)) > 0 : (f.orient === 'd' || f.orient === 'r');
      const labelOnly = !toCenter;
      if (labelOnly) {
        const off = fs * 0.7;
        const lp = P(-2, off), lx = lp[0].toFixed(1), ly = lp[1].toFixed(1);
        const anc = (f.orient === 'u' || f.orient === 'r') ? 'end' : 'start';
        const rot = vert ? ` transform="rotate(-90 ${lx} ${ly})"` : '';
        g += `<text class="flabel" x="${lx}" y="${ly}" font-size="${fs}" text-anchor="${anc}" dominant-baseline="central"${rot}>${esc(name)}</text>`;
      } else {
        const w = fs * 0.7, chev = w;
        const bodyLen = Math.max(14, name.length * fs * 0.6 + 5);
        let pts, tc;
        if (f.dir === 'out') {
          pts = [P(0, -w), P(bodyLen, -w), P(bodyLen + chev, 0), P(bodyLen, w), P(0, w)];
          tc = bodyLen / 2;
        } else if (f.dir === 'in') {
          pts = [P(0, 0), P(chev, -w), P(bodyLen + chev, -w), P(bodyLen + chev, w), P(chev, w)];
          tc = chev + bodyLen / 2;
        } else {
          pts = [P(0, 0), P(chev, -w), P(bodyLen + chev, -w), P(bodyLen + 2 * chev, 0), P(bodyLen + chev, w), P(chev, w)];
          tc = chev + bodyLen / 2;
        }
        g += `<polygon class="flag" points="${pts.map(M).join(' ')}"/>`;
        const tp = P(tc, 0), tx = tp[0].toFixed(1), ty = tp[1].toFixed(1);
        const rot = vert ? ` transform="rotate(-90 ${tx} ${ty})"` : '';
        g += `<text class="flabel" x="${tx}" y="${ty}" font-size="${fs}" text-anchor="middle" dominant-baseline="central"${rot}>${esc(name)}</text>`;
      }
    }
    return g + `</g>`;
  }
  function symSVG(type, a, b) {
    const dx = b[0] - a[0], dy = b[1] - a[1];
    // Degenerate (coincident/duplicated) leads would collapse the glyph to a point —
    // assume a standard span so a passive is never invisible.
    const L = Math.hypot(dx, dy) || 40;
    const ux = dx / L, uy = dy / L, vx = -uy, vy = ux;
    const cx = (a[0] + b[0]) / 2, cy = (a[1] + b[1]) / 2;
    // Body scales with the pin span (as an IC scales with its box) so a passive is
    // never a speck between long leads; floored so a tight part still reads. `sc`
    // scales the originally 11-mil-tuned glyph widths with it; `fs` sizes the label.
    const bl = Math.max(9, Math.min(L * 0.34, 40)), sc = Math.max(1, Math.min(bl / 11, 2.4));
    const w = 5.5 * sc, fs = Math.max(12, Math.min(bl * 0.6, 22));
    const P = (t, s) => [cx + ux * t + vx * s, cy + uy * t + vy * s];
    const M = (p) => p[0].toFixed(1) + ' ' + p[1].toFixed(1);
    const line = (p, q, c = 'sym') => `<line class="${c}" x1="${p[0].toFixed(1)}" y1="${p[1].toFixed(1)}" x2="${q[0].toFixed(1)}" y2="${q[1].toFixed(1)}"/>`;
    let svg = '', gap = bl;
    if (type === 'res') {
      const e1 = P(-bl, 0), e2 = P(bl, 0), rw = 4.5 * sc;
      const c = [P(-bl, rw), P(bl, rw), P(bl, -rw), P(-bl, -rw)];
      svg += `<polygon class="sym" points="${c.map(M).join(' ')}"/>`;
      svg += line(a, e1, 'lead') + line(b, e2, 'lead');
    } else if (type === 'cap' || type === 'cape') {
      gap = 3.2 * sc; const pw = 7 * sc;
      const e1 = P(-gap, 0), e2 = P(gap, 0);
      svg += line(P(-gap, -pw), P(-gap, pw));
      if (type === 'cape') {
        const c1 = P(gap, -pw), c2 = P(gap, pw), cc = P(gap + 3 * sc, 0);
        svg += `<path class="sym" d="M ${M(c1)} Q ${M(cc)} ${M(c2)}"/>`;
        const pp = P(-gap - 4 * sc, -pw - 2 * sc);
        svg += `<text class="sym fill" x="${pp[0].toFixed(1)}" y="${pp[1].toFixed(1)}" font-size="${(6 * sc).toFixed(1)}" stroke="none" text-anchor="middle" dominant-baseline="central">+</text>`;
      } else {
        svg += line(P(gap, -pw), P(gap, pw));
      }
      svg += line(a, e1, 'lead') + line(b, e2, 'lead');
    } else if (type === 'ind') {
      const n = 4, e1 = P(-bl, 0), e2 = P(bl, 0), step = (2 * bl) / n, r = (step / 2);
      let d = `M ${M(e1)}`;
      for (let i = 0; i < n; i++) { const s0 = -bl + i * step, s1 = s0 + step; d += ` A ${r.toFixed(1)} ${r.toFixed(1)} 0 0 1 ${M(P(s1, 0))}`; }
      svg += `<path class="sym" d="${d}"/>`;
      svg += line(a, e1, 'lead') + line(b, e2, 'lead');
    } else if (type === 'diode') {
      const e1 = P(-bl, 0), e2 = P(bl, 0);
      const t1 = P(-bl, -w), t2 = P(-bl, w), apex = P(bl * 0.55, 0);
      svg += `<polygon class="sym fill" points="${M(t1)} ${M(t2)} ${M(apex)}"/>`;
      svg += line(P(bl * 0.55, -w), P(bl * 0.55, w));
      svg += line(a, e1, 'lead') + line(b, e2, 'lead');
    }
    svg += `<rect class="hit" x="${(cx - Math.abs(ux) * bl - Math.abs(vx) * w - 2).toFixed(1)}" y="${(cy - Math.abs(uy) * bl - Math.abs(vy) * w - 2).toFixed(1)}" width="${(2 * (Math.abs(ux) * bl + Math.abs(vx) * w + 2)).toFixed(1)}" height="${(2 * (Math.abs(uy) * bl + Math.abs(vy) * w + 2)).toFixed(1)}"/>`;
    const off = w + 7 * sc, lx = cx + vx * off, ly = cy + vy * off;
    return { svg, lx, ly, fs };
  }
  function sceneSVG(s, model, dctx) {
    dctx = dctx || {};
    const diff = dctx.diff, addedSet = dctx.addedSet || new Set(), chgSet = dctx.chgSet || new Set();
    let h = '';
    if (s.frame) {
      const [fx, fy, fX, fY] = s.frame;
      h += `<rect class="page-bg" x="${fx - 16}" y="${fy - 16}" width="${fX - fx + 32}" height="${fY - fy + 32}" rx="3"/>`;
    }
    const g = s.graphics || { lines: [], rects: [], texts: [], polys: [] };
    if (s.frame) h += borderSVG(s.frame);
    for (const L of g.lines) {
      if (s.frame && (Math.abs(L[0] - L[2]) < 2 && (Math.abs(L[0] - s.frame[0]) < 3 || Math.abs(L[0] - s.frame[2]) < 3))) continue;
      if (s.frame && (Math.abs(L[1] - L[3]) < 2 && (Math.abs(L[1] - s.frame[1]) < 3 || Math.abs(L[1] - s.frame[3]) < 3))) continue;
      h += `<line class="glyph" x1="${L[0]}" y1="${L[1]}" x2="${L[2]}" y2="${L[3]}"/>`;
    }
    for (const r of g.rects) h += `<rect class="gbox" x="${Math.min(r[0], r[2])}" y="${Math.min(r[1], r[3])}" width="${Math.abs(r[2] - r[0])}" height="${Math.abs(r[3] - r[1])}"/>`;
    for (const pl of g.polys) {
      if (pl.length < 2) continue;
      h += `<polyline class="glyph" points="${pl.map(p => p[0] + ' ' + p[1]).join(' ')}"/>`;
    }
    for (const t of g.texts) {
      const fs = Math.max(8, Math.min(t.h * 0.85, 40));
      const lines = ('' + t.s).split(/\r\n|\n|\r/);
      let ts = `<text class="note" x="${t.x}" y="${t.y + fs * 0.8}" font-size="${fs.toFixed(1)}">`;
      lines.forEach((ln, i) => { ts += `<tspan x="${t.x}" dy="${i === 0 ? 0 : fs * 1.15}">${esc(ln)}</tspan>`; });
      h += ts + `</text>`;
    }
    // orientation-independent wire key, to tag added/removed traces in a diff
    const wkey = w => { let a = [Math.round(w[0]), Math.round(w[1])], b = [Math.round(w[2]), Math.round(w[3])];
      if (a[0] > b[0] || (a[0] === b[0] && a[1] > b[1])) { const t = a; a = b; b = t; }
      return a[0] + ',' + a[1] + ',' + b[0] + ',' + b[1]; };
    const addW = (diff && dctx.addedWires && dctx.addedWires[s.id]) || null;
    for (const w of s.wires) {
      let cls = isBus(w[4]) ? 'wire bus' : 'wire';
      if (addW && addW.has(wkey(w))) cls += ' add';       // new trace this rev
      h += `<line class="${cls}" data-net="${esc(w[4])}" x1="${w[0]}" y1="${w[1]}" x2="${w[2]}" y2="${w[3]}"/>`;
    }
    if (diff) for (const w of (dctx.removedWires && dctx.removedWires[s.id]) || [])   // trace gone this rev
      h += `<line class="wire ghost" x1="${w[0]}" y1="${w[1]}" x2="${w[2]}" y2="${w[3]}"/>`;
    // one part → its full symbol geometry, tinted by diff role (add/chg/ghost)
    const compSVG = (p, _pi, cls) => {
      if (!p.box) return '';
      let h = `<g class="${cls}" data-des="${esc(p.des)}" data-pi="${_pi}" data-pkg="${esc(p.pkg)}">`;
      if (p.sym !== 'box' && p.pins.length === 2) {
        const a = p.pins[0], b = p.pins[1];
        const gg = symSVG(p.sym, [a[0], a[1]], [b[0], b[1]]);
        h += gg.svg;
        const lfs = gg.fs, ls = lfs * 0.62;   // label font + half the line spacing
        if (p.val) {   // designator + value stacked beside the symbol
          h += `<text class="lbl" x="${gg.lx}" y="${(gg.ly - ls).toFixed(1)}" font-size="${lfs.toFixed(1)}" text-anchor="middle" dominant-baseline="central">${esc(p.des)}</text>` +
               `<text class="val" x="${gg.lx}" y="${(gg.ly + ls).toFixed(1)}" font-size="${lfs.toFixed(1)}" text-anchor="middle" dominant-baseline="central">${esc(p.val)}</text>`;
        } else {
          h += `<text class="lbl" x="${gg.lx}" y="${gg.ly}" font-size="${lfs.toFixed(1)}" text-anchor="middle" dominant-baseline="central">${esc(p.des)}</text>`;
        }
      } else {
        const [bx, by, bw, bh] = p.box, cx = bx + bw / 2, cy = by + bh / 2;
        const named = p.pins.some(pin => pin[4]);
        const fs = Math.max(8, Math.min(13, Math.min(bw, bh) * 0.35));
        h += `<rect x="${bx}" y="${by}" width="${bw}" height="${bh}" rx="2"/>`;
        h += `<text x="${cx}" y="${named ? by + 9 : cy}" font-size="${fs}">${esc(p.des)}</text>`;
        for (const pin of p.pins) {
          if (!pin[4]) continue;
          const px = pin[0], py = pin[1];
          const dL = Math.abs(px - bx), dR = Math.abs(px - (bx + bw)), dT = Math.abs(py - by), dB = Math.abs(py - (by + bh));
          const mn = Math.min(dL, dR, dT, dB);
          let tx = px, ty = py, anchor = 'middle';
          if (mn === dL) { tx = px + 3; anchor = 'start'; }
          else if (mn === dR) { tx = px - 3; anchor = 'end'; }
          else if (mn === dT) { ty = py + 7; }
          else { ty = py - 3; }
          h += `<text class="pinname" x="${tx}" y="${ty}" font-size="7" text-anchor="${anchor}" dominant-baseline="central">${esc(pin[4])}</text>`;
        }
      }
      h += `</g>`;
      if (p.sym !== 'box') for (const pin of p.pins)
        h += `<circle class="pin${cls.indexOf('add') > 0 ? ' add' : ''}" data-net="${esc(pin[2])}" cx="${pin[0]}" cy="${pin[1]}" r="1"/>`;
      return h;
    };
    for (let _pi = 0; _pi < s.parts.length; _pi++) { const p = s.parts[_pi];
      let cls = 'comp';
      if (diff) { if (addedSet.has(p.des)) cls += ' add'; else if (chgSet.has(p.des)) cls += ' chg'; }
      h += compSVG(p, _pi, cls);
    }
    if (diff) for (const gh of (dctx.removedGeom && dctx.removedGeom[s.id]) || [])  // removed part, drawn in place, red
      h += compSVG(gh, -1, 'comp ghost');
    const fc = s.frame ? [(s.frame[0] + s.frame[2]) / 2, (s.frame[1] + s.frame[3]) / 2] : null;
    for (const f of s.connectors || []) h += flagSVG(f, fc);
    for (const j of s.junctions || []) h += `<circle class="junction" cx="${j[0]}" cy="${j[1]}" r="1.6"/>`;
    for (const l of s.labels) {
      h += `<text class="nlabel" data-net="${esc(l.key)}" x="${l.x}" y="${l.y - 3}" font-size="9">${esc(l.text)}</text>`;
    }
    h += titleblockSVG(s.tb, model.titleblock);
    return h;
  }
  function fitBox(s) {
    if (s.frame) return s.frame;
    const xs = [], ys = [];
    for (const w of s.wires) { xs.push(w[0], w[2]); ys.push(w[1], w[3]); }
    for (const p of s.parts) { if (p.box) { xs.push(p.box[0], p.box[0] + p.box[2]); ys.push(p.box[1], p.box[1] + p.box[3]); } }
    if (!xs.length) return s.bbox;
    const pct = (a, q) => { a = a.slice().sort((u, v) => u - v); return a[Math.floor((a.length - 1) * q)]; };
    return [pct(xs, 0.01), pct(ys, 0.01), pct(xs, 0.99), pct(ys, 0.99)];
  }
  function miniInner(s) {
    const b = fitBox(s);
    let h = `<rect class="mini-page" x="${b[0]}" y="${b[1]}" width="${b[2] - b[0]}" height="${b[3] - b[1]}"/>`;
    for (const w of s.wires) h += `<line class="mini-wire" x1="${w[0]}" y1="${w[1]}" x2="${w[2]}" y2="${w[3]}"/>`;
    for (const p of s.parts) { if (p.box) h += `<rect class="mini-part" x="${p.box[0]}" y="${p.box[1]}" width="${p.box[2]}" height="${p.box[3]}"/>`; }
    return { box: b, html: h };
  }
  function thumbDataURI(s) {
    const b = fitBox(s), pad = 30;
    const vb = `${b[0] - pad} ${b[1] - pad} ${b[2] - b[0] + 2 * pad} ${b[3] - b[1] + 2 * pad}`;
    let h = `<rect x="${b[0]}" y="${b[1]}" width="${b[2] - b[0]}" height="${b[3] - b[1]}" fill="#FFFFFF" stroke="#DFD9CC" stroke-width="6"/>`;
    for (const w of s.wires) h += `<line x1="${w[0]}" y1="${w[1]}" x2="${w[2]}" y2="${w[3]}" stroke="#9AA6B8" stroke-width="7" opacity="0.9"/>`;
    for (const p of s.parts) { if (p.box) h += `<rect x="${p.box[0]}" y="${p.box[1]}" width="${p.box[2]}" height="${p.box[3]}" fill="#E4D4B8"/>`; }
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${vb}">${h}</svg>`;
    return 'data:image/svg+xml;utf8,' + encodeURIComponent(svg);
  }
  window.SchRender = { esc, isBus, borderSVG, titleblockSVG, flagSVG, symSVG, sceneSVG, fitBox, miniInner, thumbDataURI };
})();

/* ── App ── */
let M = null;              // in shell mode the model is fetched from the backend after
let XP = null;             // sign-in; in embedded mode /*__BOOT__*/ calls __renderModel now
const DOCID = new URLSearchParams(location.search).get('doc') || '';
// ---- debug logging -----------------------------------------------------------
// Tagged, timestamped console output so a user hitting a failure can copy the
// console and send it back. Everything is prefixed [CanvasPCB/schematic]; global
// handlers catch anything the try/catch misses.
const DBG = (function () {
  const TAG = '[CanvasPCB/schematic]';
  const now = () => (typeof performance !== 'undefined' ? performance.now() : Date.now());
  const t0 = now();
  const ms = () => Math.round(now() - t0) + 'ms';
  const wrap = fn => (...a) => { try { fn(TAG, ms(), ...a); } catch (e) {} };
  return { TAG, ms, log: wrap(console.log.bind(console)), warn: wrap(console.warn.bind(console)),
           error: wrap(console.error.bind(console)) };
})();
window.addEventListener('error', e => DBG.error('uncaught error:', e.message,
  '@', (e.filename || '') + ':' + (e.lineno || '') + ':' + (e.colno || ''),
  (e.error && e.error.stack) || ''));
window.addEventListener('unhandledrejection', e => DBG.error('unhandled rejection:',
  (e.reason && (e.reason.stack || e.reason.message)) || e.reason));
DBG.log('viewer script loaded; doc=' + (DOCID || '(embedded)') + ' url=' + location.href);
// Layout URL preserving doc/rev/diff, so cross-probe "Open full" stays on the same
// revision in the same tab.
function companionHref(extra){ const u = new URLSearchParams(location.search); ['ref','xnet','xcolor','modal'].forEach(k=>u.delete(k)); for(const k in (extra||{})) u.set(k, extra[k]); return XP.companion + '?' + u.toString(); }
/*__CMT_JS__*/
const R = window.SchRender, esc = R.esc;
const $ = id => document.getElementById(id);

/* ── cross-probe: embed the layout preview inside the details (inspector) card ── */
const XQP = new URLSearchParams(location.search), XMODAL = XQP.get('modal') === '1';
let schToXnet = new Map();
let layoutRefs = new Set();
function refreshXprobeMaps() {   // (re)build from XP — called by init() once the model is in
  schToXnet = new Map();
  if (XP && XP.xnets) XP.xnets.forEach((xn, i) => { if (xn.sch != null && !schToXnet.has(xn.sch)) schToXnet.set(xn.sch, i); });
  layoutRefs = new Set((XP && XP.layoutRefs) || []);
}
// The details card holds ONE persistent layout iframe at its top, driven by
// postMessage — hovering across parts updates the preview live instead of
// reloading pcb.html each time. ensureShell() builds the fixed frame + header
// once; renderInspector fills #ins-card and calls driveFrame() to retarget it.
let _frameReady = false, _framePending = null, _frameTarget = null;
function ensureShell() {
  const ins = $('inspector');
  if (ins.querySelector('#ins-frame')) return;
  ins.innerHTML =
    `<div id="ins-head" style="display:flex;align-items:center;justify-content:space-between;padding:10px 10px 6px 16px;cursor:move;user-select:none">` +
      `<span id="ins-label" style="font-size:10px;font-weight:700;letter-spacing:.09em;text-transform:uppercase;color:#8B8578">Part</span>` +
      `<span style="display:flex;align-items:center;gap:10px">` +
        `<a id="ins-open" style="font-size:10.5px;color:#2563a8;text-decoration:none;display:none">Open full →</a>` +
        `<button id="ins-x" class="iconx" style="cursor:pointer">&times;</button></span></div>` +
    `<div id="ins-preview" style="display:none;border-bottom:1px solid #EAE6DA"><iframe id="ins-frame"` +
      (XP && XP.companion ? ` src="${companionHref({ modal: '1' })}"` : '') +
      ` style="width:100%;height:190px;border:0;display:block;background:#0e0c08"></iframe></div>` +
    `<div id="ins-card"></div>`;
  $('ins-x').addEventListener('click', closeSel);
  const fr = $('ins-frame');
  fr.addEventListener('load', () => { _frameReady = true; if (_framePending) { fr.contentWindow.postMessage(_framePending, '*'); _framePending = null; } });
  const head = $('ins-head');
  head.addEventListener('pointerdown', e => {
    if (e.target.closest('#ins-x') || e.target.closest('a')) return;
    const st = { x: e.clientX, y: e.clientY, l: ins.offsetLeft, t: ins.offsetTop };
    head.setPointerCapture(e.pointerId);
    const mv = ev => { ins.style.left = (st.l + ev.clientX - st.x) + 'px'; ins.style.top = (st.t + ev.clientY - st.y) + 'px'; ins.style.right = 'auto'; ins.style.bottom = 'auto'; };
    const up = () => { head.removeEventListener('pointermove', mv); head.removeEventListener('pointerup', up); };
    head.addEventListener('pointermove', mv); head.addEventListener('pointerup', up);
  });
}
function driveFrame(target) {   // target: {ref}|{xnet} with .href, or null to hide the preview
  const prev = $('ins-preview'), open = $('ins-open'), fr = $('ins-frame');
  if (!target || !XP || !XP.companion) { prev.style.display = 'none'; open.style.display = 'none'; return; }
  prev.style.display = 'block'; open.style.display = ''; open.href = target.href;
  const key = target.ref != null ? 'r' + target.ref : 'n' + target.xnet;
  if (key !== _frameTarget) { _frameTarget = key;
    const payload = { type: 'xprobe', ref: target.ref, xnet: target.xnet };
    if (_frameReady) fr.contentWindow.postMessage(payload, '*'); else _framePending = payload;
  }
}

let cur = 0, pinNets = [], selDes = null, selPi = null, selNet = null, cardPinned = false, q = '', searchFocus = false, _cmtSheet = -1;
let bomOpen = false, bomQ = '', dark = false, collapsed = {}, sheetsOpen = true, ready = false;
let view = { x: 0, y: 0, k: 1 };
let netSheets, netNames, bomAll, partCount, thumbs, dctx;
// distinct highlight colors for multi-net pinning (readable on light + dark)
const PALETTE = ['#EA580C', '#2563EB', '#16A34A', '#9333EA', '#DB2777', '#0891B2', '#CA8A04', '#DC2626'];
function nextColor() {
  const used = new Set(pinNets.map(p => p.color));
  return PALETTE.find(c => !used.has(c)) || PALETTE[pinNets.length % PALETTE.length];
}
function isPinned(k) { return pinNets.some(p => p.key === k); }
let svgEl, sceneEl, miniEl, vpEl, tipEl, infoEl, drag = null, miniDrag = false, suppressClick = false;
let hoverDes = null, hoverTimer = 0, overDes = null, closeTimer = 0;
// close the hover card shortly after the cursor leaves both the part and the
// card; entering the card cancels the pending close so it stays reachable
function scheduleCardClose() { if (cardPinned) return; clearTimeout(closeTimer); closeTimer = setTimeout(() => { if (selDes && !cardPinned) closeSel(); }, 260); }
function cancelCardClose() { clearTimeout(closeTimer); }

function netName(k) { return netNames.get(k) || k; }
function sheetLabel(i) { const s = M.sheets[i]; return s.page || s.view; }

function init() {
  refreshXprobeMaps();
  const diff = M.diff || null;
  dctx = diff ? { diff, addedSet: new Set(diff.added), chgSet: new Set(Object.keys(diff.changed)), removedGeom: M.removedGeom,
    addedWires: Object.fromEntries(Object.entries(M.addedWires || {}).map(([k, v]) => [k, new Set(v)])),
    removedWires: M.removedWires || {} } : {};
  netSheets = new Map(); netNames = new Map();
  M.sheets.forEach((s, i) => {
    const add = k => { if (!k) return; if (!netSheets.has(k)) netSheets.set(k, new Set()); netSheets.get(k).add(i); };
    (s.wires || []).forEach(w => add(w[4]));
    (s.parts || []).forEach(p => (p.pins || []).forEach(pin => add(pin[2])));
    (s.connectors || []).forEach(f => add(f.key));
    (s.labels || []).forEach(l => add(l.key));
    Object.entries(s.nets || {}).forEach(([k, v]) => { if (v && !netNames.has(k)) netNames.set(k, v); });
  });
  const bomMap = new Map();
  M.sheets.forEach((s, i) => s.parts.forEach(p => {
    const val = p.val || '', pkg = p.pkg || '(no package)';
    const key = val + '' + pkg;
    if (!bomMap.has(key)) bomMap.set(key, { val, pkg, refs: [] });
    bomMap.get(key).refs.push({ des: p.des, si: i });
  }));
  bomAll = [...bomMap.values()].sort((a, b) =>
    b.refs.length - a.refs.length || a.val.localeCompare(b.val) || a.pkg.localeCompare(b.pkg));
  // count physical DEVICES (unique refdes), not drawn sections — multi-section
  // parts (gate arrays, resistor packs) share one refdes across several symbols
  partCount = new Set(M.sheets.flatMap(s => s.parts.map(p => p.des))).size;
  thumbs = M.sheets.map(s => R.thumbDataURI(s));

  svgEl = $('svg'); sceneEl = $('scene'); miniEl = $('mini'); tipEl = $('tip');
  bindCanvas(); bindMini(); bindToolbar();
  // keep the hover card open while the cursor is over it
  const insEl = $('inspector');
  insEl.addEventListener('pointerenter', cancelCardClose);
  insEl.addEventListener('pointerleave', () => { if (selDes) scheduleCardClose(); });
  ready = true;
  updChrome(); renderSidebar(); renderScene(); fit();
  // cross-probe: hide chrome when embedded, wire the modal, apply URL params
  if (XMODAL) document.body.classList.add('xmodal');
  const _xn = XQP.get('xnet'), _xr = XQP.get('ref');
  // Apply a cross-probe target (from ?xnet/?ref on load, or a postMessage from the
  // layout's preview modal so hovering nets there retargets this view without a reload).
  const applyXprobe = t => {
    if (!t) return;
    if (t.xnet != null && XP && XP.xnets) {
      const idxs = ('' + t.xnet).split(',').map(s => +s).filter(i => XP.xnets[i] && XP.xnets[i].sch != null);
      if (!idxs.length) return;
      clearPins();
      const k0 = XP.xnets[idxs[0]].sch;
      goNet(k0, [...(netSheets.get(k0) || [])]);          // navigate to the first net's sheet + pin it
      idxs.slice(1).forEach(i => pinNet(XP.xnets[i].sch)); // pin the rest
      applyPins(); renderStatus(); renderSidebar(); centerNet(k0);
    } else if (t.ref) {
      let bi = -1, bp = -1;   // sheet with the most pins of this refdes = the meaningful view
      for (let i = 0; i < M.sheets.length; i++) {
        const tot = M.sheets[i].parts.filter(p => p.des === t.ref).reduce((n, p) => n + (p.pins || []).length, 0);
        if (tot > bp) { bp = tot; bi = i; }
      }
      if (bp <= 0) return;
      if (XMODAL) {                    // clean preview: highlight + fit the part, no card / mini-map
        if (bi !== cur) { cur = bi; renderScene(); fit(); updChrome(); renderSidebar(); }
        selDes = t.ref; updateSelMark(); fitPart(t.ref);
      } else goToPart(bi, t.ref);
    }
  };
  if (_xn !== null) applyXprobe({ xnet: _xn });
  else if (_xr) applyXprobe({ ref: _xr });
  window.addEventListener('message', e => { const d = e.data; if (d && d.type === 'xprobe') applyXprobe(d); });
  // ---- comments: anchor to a part, a net, or an open point (per sheet) ----
  if (!XMODAL) {
    window.__cmtContext = () => 'sch:' + (M.name || '') + ':' + cur;   // shared with the Firebase backend bootstrap
    Comments.init({
      context: window.__cmtContext,
      rev: () => (window.__docMeta || {}).curRev || 1,          // comments are locked to the rev they were made on
      latestRev: () => (window.__docMeta || {}).rev || 1,       // unversioned (legacy) comments belong to the latest rev
      diffRev: () => (window.__docMeta || {}).diffRev || null,  // in a diff, show both revs (new / gone)
      svg: svgEl, stage: $('stage'), button: $('cmt-btn'),
      onChange: () => renderSidebar(),   // refresh per-sheet comment counts
      project: (x, y) => ({ sx: x * view.k + view.x, sy: y * view.k + view.y }),
      resolveAnchor: (cx, cy) => {
        const r = svgEl.getBoundingClientRect();
        const sx = (cx - r.left - view.x) / view.k, sy = (cy - r.top - view.y) / view.k;
        const s = M.sheets[cur], tol = 90 / view.k;
        let best = null, bd = tol * tol;               // nearest pin / wire to point the indicator at
        const seg = (ax, ay, bx, by) => { const dx = bx - ax, dy = by - ay, l2 = dx * dx + dy * dy; let t = l2 ? ((sx - ax) * dx + (sy - ay) * dy) / l2 : 0; t = Math.max(0, Math.min(1, t)); const qx = ax + t * dx, qy = ay + t * dy; return [qx, qy, (sx - qx) * (sx - qx) + (sy - qy) * (sy - qy)]; };
        for (const p of s.parts) {   // nearest by pin, but aim at the component centre
          const cx = p.box ? p.box[0] + p.box[2] / 2 : (p.pins && p.pins.length ? p.pins.reduce((a, q) => a + q[0], 0) / p.pins.length : 0);
          const cy = p.box ? p.box[1] + p.box[3] / 2 : (p.pins && p.pins.length ? p.pins.reduce((a, q) => a + q[1], 0) / p.pins.length : 0);
          for (const pin of (p.pins || [])) { const dx = pin[0] - sx, dy = pin[1] - sy, d = dx * dx + dy * dy; if (d < bd) { bd = d; best = { kind: 'pin', tx: cx, ty: cy, ref: p.des + '.' + pin[3], label: 'Pin ' + p.des + '.' + pin[3] }; } }
        }
        for (const w of (s.wires || [])) { const q = seg(w[0], w[1], w[2], w[3]); if (q[2] < bd) { bd = q[2]; best = { kind: 'net', tx: q[0], ty: q[1], ref: w[4], label: 'Net ' + (netName(w[4]) || w[4]) }; } }
        if (best) return { kind: best.kind, x: sx, y: sy, ref: best.ref, label: best.label, tx: best.tx, ty: best.ty };   // anchor at cursor, point at the item
        for (const p of s.parts) if (p.box && sx >= p.box[0] && sx <= p.box[0] + p.box[2] && sy >= p.box[1] && sy <= p.box[1] + p.box[3]) return { kind: 'part', x: sx, y: sy, ref: p.des, label: 'Part ' + p.des, tx: p.box[0] + p.box[2] / 2, ty: p.box[1] + p.box[3] / 2 };
        return { kind: 'point', x: sx, y: sy, label: 'Open space' };
      }
    });
    window._cmtReady = true; _cmtSheet = cur;
    // Doc-wide per-sheet comment counts (rev-aware badges in the sheet list).
    window.__sheetCounts = window.__sheetCounts || {};
    if (Comments.watchCounts) Comments.watchCounts('sch:' + (M.name || '') + ':', m => { window.__sheetCounts = m || {}; renderSidebar(); });
  }
}

/* ---------- scene ---------- */
function renderScene() {
  const s = M.sheets[cur];
  sceneEl.innerHTML = R.sceneSVG(s, M, dctx);
  applyView();
  renderStatus();
  applyPins();
  updateSelMark();
  renderMini();
  if (window._cmtReady && _cmtSheet !== cur) { _cmtSheet = cur; Comments.setContext(); }   // per-sheet comments
}
function applyView() {
  sceneEl.setAttribute('transform', `translate(${view.x},${view.y}) scale(${view.k})`);
  $('tb-zoom').textContent = Math.round(view.k * 100) + '%';
  updateVp();
  if (window.Comments) Comments.reproject();
}
function fit() {
  if (!svgEl || !ready) return;
  const s = M.sheets[cur], b = R.fitBox(s), r = svgEl.getBoundingClientRect();
  if (!r.width || !r.height) return;
  const w = Math.max(b[2] - b[0], 10), h = Math.max(b[3] - b[1], 10);
  const pad = 46, k = Math.min((r.width - 2 * pad) / w, (r.height - 2 * pad) / h);
  view.k = Math.min(k, 8);
  view.x = (r.width - (b[0] + b[2]) * view.k) / 2;
  view.y = (r.height - (b[1] + b[3]) * view.k) / 2;
  applyView();
}
function zoomBy(f) {
  const r = svgEl.getBoundingClientRect(), mx = r.width / 2, my = r.height / 2;
  const nk = Math.min(20, Math.max(0.05, view.k * f));
  view.x = mx - (mx - view.x) * (nk / view.k);
  view.y = my - (my - view.y) * (nk / view.k);
  view.k = nk; applyView();
}

/* ---------- canvas events ---------- */
function bindCanvas() {
  if (XMODAL) return;   // embedded preview: static, no pan/zoom/select/hover
  const el = svgEl;
  el.addEventListener('pointerdown', e => {
    if (e.target.closest('[data-net],[data-des]')) return;
    drag = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y, moved: false };
    el.setPointerCapture(e.pointerId);
  });
  el.addEventListener('pointermove', e => {
    if (drag) {
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
      view.x = drag.vx + dx; view.y = drag.vy + dy; applyView(); return;
    }
    // hover a part → pop up its detail card; leaving it → schedule close
    const c = e.target.closest('.comp[data-des]');
    const des = c ? c.dataset.des : null;
    if (des !== overDes) {
      if (des) {                       // entered a part
        cancelCardClose();
        if (des !== selDes && !cardPinned && !XMODAL && !(window.Comments && Comments.isPlacing())) { hoverDes = des; clearTimeout(hoverTimer);
          const hpi = c ? +c.dataset.pi : null;
          hoverTimer = setTimeout(() => { if (hoverDes === des && !cardPinned) selectPart(des, hpi); }, 90); }
      } else {                         // left a part onto empty canvas
        hoverDes = null; clearTimeout(hoverTimer);
        if (selDes && !cardPinned) scheduleCardClose();
      }
      overDes = des;
    }
    hideTip();
  });
  el.addEventListener('pointerup', () => {
    if (drag && drag.moved) suppressClick = true;
    drag = null;
  });
  el.addEventListener('pointerleave', () => { hideTip(); overDes = null; if (selDes) scheduleCardClose(); });
  el.addEventListener('pointerover', e => {
    const n = e.target.closest('[data-net]'); if (n && !pinNets.length) hoverNet(n.dataset.net);
  });
  el.addEventListener('pointerout', e => {
    const n = e.target.closest('[data-net]'); if (n && !pinNets.length) applyPins();
  });
  el.addEventListener('click', e => {
    if (suppressClick) { suppressClick = false; return; }
    if (window.Comments && Comments.isPlacing()) { Comments.place(e.clientX, e.clientY); return; }
    const n = e.target.closest('[data-net]');
    if (n) { const k = n.dataset.net; togglePin(k); if (isPinned(k)) { cardPinned = true; selectNet(k); } else if (selNet === k) closeSel(); return; }
    const c = e.target.closest('.comp[data-des]');
    if (c) { cardPinned = true; selectPart(c.dataset.des, +c.dataset.pi); return; }   // click pins the card open
    if (selDes || selNet) closeSel();
  });
  el.addEventListener('wheel', e => {
    e.preventDefault();
    const r = el.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
    const f = Math.exp(-e.deltaY * 0.0015), nk = Math.min(20, Math.max(0.05, view.k * f));
    view.x = mx - (mx - view.x) * (nk / view.k);
    view.y = my - (my - view.y) * (nk / view.k);
    view.k = nk; applyView();
  }, { passive: false });
}

/* ---------- minimap ---------- */
function renderMini() {
  const s = M.sheets[cur], m = R.miniInner(s), pad = 20;
  miniEl.setAttribute('viewBox', `${m.box[0] - pad} ${m.box[1] - pad} ${m.box[2] - m.box[0] + 2 * pad} ${m.box[3] - m.box[1] + 2 * pad}`);
  miniEl.innerHTML = m.html + '<rect class="mini-vp" x="0" y="0" width="0" height="0"/>';
  vpEl = miniEl.querySelector('.mini-vp');
  updateVp();
}
function updateVp() {
  if (!vpEl || !svgEl) return;
  const r = svgEl.getBoundingClientRect();
  vpEl.setAttribute('x', (0 - view.x) / view.k);
  vpEl.setAttribute('y', (0 - view.y) / view.k);
  vpEl.setAttribute('width', r.width / view.k);
  vpEl.setAttribute('height', r.height / view.k);
}
function bindMini() {
  const el = miniEl;
  const center = (e) => {
    const pt = el.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY;
    const ctm = el.getScreenCTM(); if (!ctm) return;
    const p = pt.matrixTransform(ctm.inverse());
    const r = svgEl.getBoundingClientRect();
    view.x = r.width / 2 - p.x * view.k; view.y = r.height / 2 - p.y * view.k; applyView();
  };
  el.addEventListener('pointerdown', e => { el.setPointerCapture(e.pointerId); miniDrag = true; center(e); });
  el.addEventListener('pointermove', e => { if (miniDrag) center(e); });
  el.addEventListener('pointerup', () => { miniDrag = false; });
}

/* ---------- highlight / info ---------- */
// paint every pinned net in its own colour (via the per-element --hot var);
// other nets keep full opacity (no dimming)
function applyPins() {
  const map = new Map(pinNets.map(p => [p.key, p.color]));
  sceneEl.querySelectorAll('[data-net]').forEach(el => {
    const c = map.get(el.dataset.net);
    if (c) { el.classList.add('hot'); el.style.setProperty('--hot', c); }
    else { el.classList.remove('hot'); el.style.removeProperty('--hot'); }
    el.classList.remove('dim');
  });
  if (!pinNets.length) setDefaultInfo();
}
// transient single-net highlight on hover (only when nothing is pinned)
function hoverNet(k) {
  const els = [...sceneEl.querySelectorAll('[data-net]')];
  const here = els.some(el => el.dataset.net === k && k !== '');
  els.forEach(el => {
    const on = el.dataset.net === k && k !== '';
    el.classList.toggle('hot', on); el.style.removeProperty('--hot');
    el.classList.remove('dim');   // highlight only; don't dim the rest
  });
  if (infoEl) {
    const name = netName(k);
    const pages = [...(netSheets.get(k) || [])].filter(i => i !== cur).map(i => sheetLabel(i));
    const also = pages.length ? ' · also on ' + pages.join(', ') : '';
    infoEl.textContent = here
      ? `${name} — ${sceneEl.querySelectorAll('.wire.hot').length} segments${also}`
      : `${name} — not on this page${also}`;
  }
}
function setDefaultInfo() {
  if (!infoEl || !ready) return;
  const s = M.sheets[cur];
  infoEl.textContent = `${s.parts.length} parts · ${s.wires.length} wires · ${Object.keys(s.nets).length} nets`;
}
function togglePin(k) {
  if (!k) return;
  const i = pinNets.findIndex(p => p.key === k);
  if (i >= 0) pinNets.splice(i, 1); else pinNets.push({ key: k, color: nextColor() });
  applyPins(); renderStatus(); renderSidebar();
}
function pinNet(k) {   // ensure a net is pinned (used by search / pin-row clicks)
  if (k && !isPinned(k)) pinNets.push({ key: k, color: nextColor() });
  applyPins(); renderStatus(); renderSidebar();
}
function removePinAt(i) { pinNets.splice(i, 1); applyPins(); renderStatus(); renderSidebar(); }
function clearPins() { pinNets = []; applyPins(); renderStatus(); renderSidebar(); }

/* ---------- selection / navigation ---------- */
function selectPart(des, pi) { selDes = des; selPi = (pi == null ? null : pi); selNet = null; updateSelMark(); renderInspector(); }
function selectNet(k) { selNet = k; selDes = null; selPi = null; updateSelMark(); renderInspector(); }
function closeSel() { selDes = null; selPi = null; selNet = null; cardPinned = false; updateSelMark(); renderInspector(); }
function updateSelMark() {
  sceneEl.querySelectorAll('.comp.sel').forEach(el => el.classList.remove('sel'));
  if (selDes) [...sceneEl.querySelectorAll('.comp[data-des]')]
    .filter(e => e.dataset.des === selDes).forEach(el => el.classList.add('sel'));
}
function selectSheet(i) {
  if (i === cur) return;
  cur = i; selDes = null;
  renderScene(); fit(); updChrome(); renderSidebar(); renderInspector();
}
function goToPart(si, des) {
  if (si !== cur) { cur = si; selDes = des; renderScene(); fit(); updChrome(); renderSidebar(); centerPart(des); }
  else { selDes = des; centerPart(des); }
  updateSelMark(); renderInspector();
}
function fitPart(des) {   // modal preview: fit ALL sections of the device on this sheet
  const s = M.sheets[cur], sec = s.parts.filter(x => x.des === des);
  if (!sec.length) return;
  let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
  for (const p of sec) {
    if (p.box) { x0 = Math.min(x0, p.box[0]); y0 = Math.min(y0, p.box[1]); x1 = Math.max(x1, p.box[0] + p.box[2]); y1 = Math.max(y1, p.box[1] + p.box[3]); }
    (p.pins || []).forEach(q => { x0 = Math.min(x0, q[0]); y0 = Math.min(y0, q[1]); x1 = Math.max(x1, q[0]); y1 = Math.max(y1, q[1]); });
  }
  if (x1 < x0) return;
  const r = svgEl.getBoundingClientRect(), m = 0.35, w = Math.max(x1 - x0, 20), h = Math.max(y1 - y0, 20);
  view.k = Math.min(8, Math.min(r.width / (w * (1 + m)), r.height / (h * (1 + m))));
  view.x = r.width / 2 - (x0 + x1) / 2 * view.k; view.y = r.height / 2 - (y0 + y1) / 2 * view.k; applyView();
}
function centerPart(des) {
  const s = M.sheets[cur], p = s.parts.find(x => x.des === des);
  if (!p) return;
  let cx, cy;
  if (p.box) { cx = p.box[0] + p.box[2] / 2; cy = p.box[1] + p.box[3] / 2; }
  else if (p.pins && p.pins.length) {
    cx = p.pins.reduce((a, q) => a + q[0], 0) / p.pins.length;
    cy = p.pins.reduce((a, q) => a + q[1], 0) / p.pins.length;
  } else return;
  const r = svgEl.getBoundingClientRect();
  view.k = Math.min(6, Math.max(view.k, 2.2));
  view.x = r.width / 2 - cx * view.k; view.y = r.height / 2 - cy * view.k; applyView();
  const el = [...sceneEl.querySelectorAll('.comp[data-des]')].find(e => e.dataset.des === des);
  if (el) { el.classList.add('flash'); setTimeout(() => el.classList.remove('flash'), 1600); }
}
function goNet(k, pages) {
  q = ''; searchFocus = false; $('tb-search').value = ''; renderDrop();
  if (!pages.includes(cur) && pages.length) { cur = pages[0]; selDes = null; renderScene(); fit(); updChrome(); renderInspector(); }
  pinNet(k);
}
// Cross-probe: carry the highlighted nets to the layout view as ?xnet=i,j,k
// (the layout consumes the same param on load). Only nets that have a layout
// counterpart in the correspondence table travel.
function layoutHref() {
  const u = new URLSearchParams(location.search); u.delete('ref');
  const xs = [], xc = [];
  const add = (key, color) => { if (key != null && schToXnet.has(key)) { const i = schToXnet.get(key); if (!xs.includes(i)) { xs.push(i); xc.push((color || PALETTE[0]).replace('#', '')); } } };
  pinNets.forEach(p => add(p.key, p.color));                 // carry each pinned net + its colour
  if (selNet != null && !isPinned(selNet)) add(selNet, PALETTE[0]);
  if (xs.length) { u.set('xnet', xs.join(',')); u.set('xcolor', xc.join(',')); } else { u.delete('xnet'); u.delete('xcolor'); }
  // standalone pair → jump straight to the sibling file; hosted → the layout/ route.
  const base = (XP && XP.standalone && XP.companion) ? XP.companion : 'layout/';
  return base + '?' + u.toString();
}
function centerNet(k) {   // fit the view around a net's wires/pins on the current sheet
  const els = [...sceneEl.querySelectorAll('[data-net]')].filter(el => el.dataset.net === k);
  let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
  els.forEach(el => { const b = el.getBBox && el.getBBox();
    if (b) { x0 = Math.min(x0, b.x); y0 = Math.min(y0, b.y); x1 = Math.max(x1, b.x + b.width); y1 = Math.max(y1, b.y + b.height); } });
  if (x1 < x0) return;
  const r = svgEl.getBoundingClientRect(), cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
  view.k = Math.min(6, Math.max(1.2, Math.min(r.width / ((x1 - x0) * 1.5 + 1), r.height / ((y1 - y0) * 1.5 + 1))));
  view.x = r.width / 2 - cx * view.k; view.y = r.height / 2 - cy * view.k; applyView();
}

/* ---------- tooltip ---------- */
function showTip(e, txt) {
  tipEl.textContent = txt; tipEl.style.opacity = 1;
  tipEl.style.left = (e.clientX + 14) + 'px'; tipEl.style.top = (e.clientY + 14) + 'px';
}
function hideTip() { tipEl.style.opacity = 0; }
function diffReason(des) {
  if (!dctx.diff) return '';
  if (dctx.addedSet.has(des)) return '\n+ added';
  if (dctx.chgSet.has(des)) return '\n~ ' + dctx.diff.changed[des].join('\n~ ');
  return '';
}

/* ---------- chrome rendering ---------- */
function updChrome() {
  $('tb-name').textContent = M.name || 'Schematic';
  $('tb-sub').textContent = `${M.sheets.length} sheets · ${partCount} parts`;
  const s = M.sheets[cur];
  $('tb-crumb').textContent = s ? `${s.view} / ${s.page}` : '';
  $('tb-theme').textContent = dark ? 'Light canvas' : 'Dark canvas';
  $('tb-sheets-btn').style.display = sheetsOpen ? 'none' : 'block';
  $('sidebar').style.display = sheetsOpen ? 'block' : 'none';
  const d = dctx.diff;
  const dd = $('tb-diff');
  if (d) {
    dd.style.display = 'flex';
    dd.innerHTML = `<span style="color:#8B8578">vs ${esc(d.old)}</span>` +
      `<span style="padding:2px 7px;border-radius:10px;background:rgba(26,127,55,.14);color:#1A7F37">+${d.added.length}</span>` +
      `<span style="padding:2px 7px;border-radius:10px;background:rgba(207,34,46,.13);color:#CF222E">&#8722;${d.removed.length}</span>` +
      `<span style="padding:2px 7px;border-radius:10px;background:rgba(154,103,0,.16);color:#9A6700">~${Object.keys(d.changed).length}</span>`;
  } else dd.style.display = 'none';
}
function renderDrop() {
  const drop = $('tb-drop');
  const t = q.trim().toLowerCase();
  if (!ready || !searchFocus || !t) { drop.style.display = 'none'; drop.innerHTML = ''; return; }
  const badgeNet = "background:#EEF0FC;color:#4338CA;font-family:'IBM Plex Mono',monospace;font-size:9px;font-weight:600;padding:2px 5px;border-radius:4px;flex-shrink:0";
  const badgePart = "background:#F7EFE4;color:#92400E;font-family:'IBM Plex Mono',monospace;font-size:9px;font-weight:600;padding:2px 5px;border-radius:4px;flex-shrink:0";
  const results = [];
  const seen = new Set();
  for (const [k, set] of netSheets) {
    const name = netName(k);
    if (!name || name === 'SKIP') continue;
    if (name.toLowerCase().includes(t) && !seen.has(name)) {
      seen.add(name);
      const pages = [...set];
      results.push({ kind: 'NET', label: name, sub: pages.length + (pages.length > 1 ? ' pages' : ' page'), badge: badgeNet, act: () => goNet(k, pages) });
      if (results.filter(r => r.kind === 'NET').length >= 6) break;
    }
  }
  let pc = 0;
  outer: for (let i = 0; i < M.sheets.length; i++) {
    for (const p of M.sheets[i].parts) {
      if (p.des.toLowerCase().includes(t)) {
        const si = i, des = p.des;
        results.push({ kind: 'PART', label: des, sub: sheetLabel(i), badge: badgePart, act: () => { q = ''; searchFocus = false; $('tb-search').value = ''; renderDrop(); goToPart(si, des); } });
        if (++pc >= 6) break outer;
      }
    }
  }
  drop.style.display = 'block';
  if (!results.length) { drop.innerHTML = `<div style="padding:10px 12px;font-size:11.5px;color:#8B8578">No matching part or net</div>`; return; }
  drop.innerHTML = results.map((r, i) =>
    `<div class="sres" data-i="${i}" style="display:flex;align-items:center;gap:8px;padding:7px 10px;cursor:pointer">` +
    `<span style="${r.badge}">${r.kind}</span>` +
    `<span style="font-family:'IBM Plex Mono',monospace;font-size:12px;font-weight:600;color:#221F1A;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(r.label)}</span>` +
    `<span style="font-size:10.5px;color:#8B8578;margin-left:auto;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:150px">${esc(r.sub)}</span></div>`
  ).join('');
  [...drop.querySelectorAll('.sres')].forEach(el => el.addEventListener('mousedown', ev => { ev.preventDefault(); results[+el.dataset.i].act(); }));
}
function renderSidebar() {
  const bar = $('sidebar');
  const byView = new Map();
  M.sheets.forEach((s, i) => { if (!byView.has(s.view)) byView.set(s.view, []); byView.get(s.view).push(i); });
  let h = `<div style="display:flex;align-items:center;justify-content:space-between;padding:10px 10px 6px 16px">` +
    `<span style="font-size:10px;font-weight:700;letter-spacing:.09em;text-transform:uppercase;color:#8B8578">Sheets</span>` +
    `<button id="sb-collapse" class="iconx" title="Collapse panel" style="font-size:14px">&#171;</button></div>`;
  for (const [vw, idxs] of byView) {
    const open = !collapsed[vw];
    h += `<div><div class="sb-grp" data-vw="${esc(vw)}" style="display:flex;align-items:baseline;gap:6px;padding:8px 14px 3px 16px;cursor:pointer">` +
      `<span style="font-size:9px;color:#A19B8E;width:9px;flex-shrink:0">${open ? '&#9662;' : '&#9656;'}</span>` +
      `<span style="font-size:10.5px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:#57524A;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(vw.replace(/^\d+_/, ''))}</span>` +
      `<span style="font-size:10px;color:#A19B8E;font-family:'IBM Plex Mono',monospace;margin-left:auto">${idxs.length}</span></div>`;
    if (open) {
      for (const i of idxs) {
        const s = M.sheets[i], active = i === cur;
        // one dot per pinned net present on this page, in that net's colour
        const dots = pinNets.filter(p => (netSheets.get(p.key) || new Set()).has(i))
          .map(p => `<span title="${esc(netName(p.key))}" style="width:6px;height:6px;border-radius:50%;background:${p.color};display:inline-block;flex-shrink:0"></span>`).join('');
        const th = thumbs[i] ? `background-image:url(&quot;${thumbs[i]}&quot;);` : '';
        // rev-aware unresolved comment count for this sheet (locked to the rev being
        // viewed; in a diff, also count the compared rev). Falls back to Comments.countFor
        // (localStorage) when the doc-wide watcher hasn't populated yet.
        const _md = window.__docMeta || {}, _rn = _md.curRev, _dv = _md.diffRev, _lt = _md.rev || 1;
        const _arr = (window.__sheetCounts || {})['sch:' + (M.name || '') + ':' + i];
        const cc = _arr ? _arr.filter(r => { const e = (r == null ? _lt : r); return e === _rn || (_dv && e === _dv); }).length
          : ((window.Comments && Comments.countFor) ? Comments.countFor('sch:' + (M.name || '') + ':' + i) : 0);
        const cbadge = cc ? `<span title="${cc} comment${cc > 1 ? 's' : ''}" style="display:inline-flex;align-items:center;gap:2px;font-size:9.5px;font-weight:700;color:#fff;background:#F5A623;border-radius:8px;padding:0 6px;line-height:15px;font-family:'IBM Plex Mono',monospace">&#128172; ${cc}</span>` : '';
        h += `<div class="pgrow${active ? ' active' : ''}" data-i="${i}">` +
          `<div style="width:62px;height:42px;background:#FFFFFF;${th}background-size:contain;background-repeat:no-repeat;background-position:center;border:1px solid ${active ? '#C9A97F' : '#E4E0D3'};border-radius:4px;flex-shrink:0"></div>` +
          `<div style="min-width:0;flex:1">` +
          `<div style="font-size:12px;font-weight:500;color:#221F1A;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(s.page || s.view)}</div>` +
          `<div style="display:flex;align-items:center;gap:5px;margin-top:1px">` +
          `<span style="font-size:10px;color:#A19B8E;font-family:'IBM Plex Mono',monospace">${s.parts.length} parts</span>` +
          cbadge + dots +
          `</div></div></div>`;
      }
    }
  }
  bar.innerHTML = h;
  $('sb-collapse').addEventListener('click', toggleSheets);
  [...bar.querySelectorAll('.sb-grp')].forEach(el => el.addEventListener('click', () => { const v = el.dataset.vw; collapsed = { ...collapsed, [v]: !collapsed[v] }; renderSidebar(); }));
  [...bar.querySelectorAll('.pgrow')].forEach(el => el.addEventListener('click', () => selectSheet(+el.dataset.i)));
}
function renderStatus() {
  const st = $('status'); infoEl = null;
  if (!pinNets.length) {
    st.innerHTML = `<div style="background:rgba(251,250,247,0.92);border:1px solid #E0DCD1;border-radius:8px;padding:5px 9px;font-size:11px;font-family:'IBM Plex Mono',monospace;color:#6E6A60;pointer-events:none;width:fit-content;white-space:nowrap"><span id="info"></span></div>`;
    infoEl = $('info'); setDefaultInfo(); return;
  }
  const s = M.sheets[cur];
  let blocks = '';
  pinNets.forEach((p, pi) => {
    const segs = s.wires.filter(w => w[4] === p.key).length;
    const onPage = segs > 0 || (s.labels || []).some(l => l.key === p.key) || (s.connectors || []).some(f => f.key === p.key);
    const meta = onPage ? segs + ' segments · this page' : 'not on this page';
    // every page carrying this net, in fixed sheet order (incl. current, marked)
    const allPages = [...(netSheets.get(p.key) || [])].sort((a, b) => a - b);
    const chips = allPages.map(i => {
      const isCur = i === cur;
      const style = isCur
        ? `background:${p.color};border:1px solid ${p.color};color:#FFFFFF;font-weight:600`
        : `background:#FBF1E8;border:1px solid #E8CDB6;color:#B4530F`;
      return `<button class="chip" data-i="${i}" style="font-family:'IBM Plex Mono',monospace;font-size:10px;padding:2px 8px;border-radius:999px;cursor:pointer;${style}">${esc(sheetLabel(i))}</button>`;
    }).join('');
    blocks += `<div style="display:flex;flex-direction:column;gap:4px">` +
      `<div style="display:flex;align-items:center;gap:8px">` +
      `<span style="width:9px;height:9px;border-radius:50%;background:${p.color};flex-shrink:0"></span>` +
      `<span style="font-family:'IBM Plex Mono',monospace;font-size:12px;font-weight:600;color:#221F1A;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(netName(p.key))}</span>` +
      `<span style="font-size:10.5px;color:#8B8578;font-family:'IBM Plex Mono',monospace;white-space:nowrap">${meta}</span>` +
      `<button class="pin-x" data-p="${pi}" title="Remove net" style="margin-left:auto;border:none;background:transparent;cursor:pointer;color:#8B8578;font-size:14px;line-height:1;padding:0 2px">&times;</button></div>` +
      (chips ? `<div style="display:flex;flex-wrap:wrap;gap:4px;align-items:center"><span style="font-size:10px;color:#8B8578;margin-right:2px">pages</span>${chips}</div>` : '') +
      `</div>`;
  });
  st.innerHTML = `<div style="background:rgba(251,250,247,0.96);border:1px solid #E0DCD1;border-radius:10px;padding:8px 10px;box-shadow:0 8px 20px rgba(24,20,10,0.10);display:flex;flex-direction:column;gap:9px;max-width:600px;max-height:46vh;overflow-y:auto">${blocks}</div>`;
  [...st.querySelectorAll('.pin-x')].forEach(el => el.addEventListener('click', () => removePinAt(+el.dataset.p)));
  [...st.querySelectorAll('.chip')].forEach(el => el.addEventListener('click', () => selectSheet(+el.dataset.i)));
}
// transient net highlight driven by hovering a net row in the part card
function previewNet(k) {
  clearPreview();
  if (!k) return;
  sceneEl.querySelectorAll('[data-net]').forEach(el => { if (el.dataset.net === k) el.classList.add('preview'); });
}
function clearPreview() { sceneEl.querySelectorAll('.preview').forEach(el => el.classList.remove('preview')); }
// place the floating card next to the selected part (flip / clamp to stay in view)
function positionInspectorNear(des) {
  const s = M.sheets[cur], ins = $('inspector');
  const p = (selPi != null && s.parts[selPi] && s.parts[selPi].des === des) ? s.parts[selPi] : s.parts.find(x => x.des === des);
  if (!p || !svgEl || !ins.offsetParent) return;
  let cx, cy;
  if (p.box) { cx = p.box[0] + p.box[2] / 2; cy = p.box[1] + p.box[3] / 2; }
  else if (p.pins && p.pins.length) { cx = p.pins.reduce((a, q) => a + q[0], 0) / p.pins.length; cy = p.pins.reduce((a, q) => a + q[1], 0) / p.pins.length; }
  else return;
  const sv = svgEl.getBoundingClientRect(), row = ins.offsetParent.getBoundingClientRect();
  const px = sv.left + cx * view.k + view.x, py = sv.top + cy * view.k + view.y;
  const w = ins.offsetWidth || 300, h = Math.min(ins.offsetHeight || 320, sv.height - 24);
  const GAP = 110;                             // keep clear of the cursor / part
  let left = px + GAP;                         // prefer to the right of the part
  if (left + w > sv.right - 10) left = px - GAP - w;   // flip to the left
  left = Math.max(sv.left + 10, Math.min(left, sv.right - w - 10));
  let top = Math.max(sv.top + 10, Math.min(py - 80, sv.bottom - h - 10));
  ins.style.left = (left - row.left) + 'px'; ins.style.top = (top - row.top) + 'px';
  ins.style.right = 'auto'; ins.style.bottom = 'auto';
}
function renderNetCard() {
  const ins = $('inspector'); ensureShell(); ins.style.display = 'block';
  ins.style.left = 'auto'; ins.style.right = '14px'; ins.style.top = '14px'; ins.style.bottom = 'auto';
  $('ins-label').textContent = 'Net';
  const k = selNet, xi = schToXnet.has(k) ? schToXnet.get(k) : null;
  const also = [...(netSheets.get(k) || [])].filter(i => i !== cur).map(i => sheetLabel(i));
  driveFrame(xi != null && XP && XP.companion ? { xnet: xi, href: companionHref({ xnet: xi }) } : null);
  $('ins-card').innerHTML =
    `<div style="padding:8px 16px 12px;border-bottom:1px solid #EAE6DA">` +
    `<div style="font-family:'IBM Plex Mono',monospace;font-size:15px;font-weight:600;color:#221F1A;word-break:break-all">${esc(netName(k) || k)}</div>` +
    (also.length ? `<div style="font-size:10.5px;color:#A19B8E;font-family:'IBM Plex Mono',monospace;margin-top:5px">also on ${esc(also.join(', '))}</div>` : '') +
    `</div>` +
    (xi == null ? `<div style="padding:14px 16px;color:#A19B8E;font-size:12px">No matched net in the layout.</div>` : '');
}
function renderInspector() {
  const ins = $('inspector');
  if (selNet) return renderNetCard();
  if (!selDes) { ins.style.display = 'none'; return; }
  // A physical DEVICE is often drawn as several schematic sections (OrCAD
  // multi-section parts: gates, resistor arrays, power sections) sharing one
  // refdes. Aggregate every section so the card shows the whole device.
  const secs = [];
  M.sheets.forEach((sh, i) => sh.parts.forEach((p, pi) => { if (p.des === selDes) secs.push({ p, si: i, pi }); }));
  if (!secs.length) { ins.style.display = 'none'; return; }
  const rank = sc => (sc.si === cur && sc.pi === selPi) ? -1 : sc.si === cur ? 0 : 1 + sc.si;
  secs.sort((a, b) => rank(a) - rank(b));
  const sel = secs[0].p, si = secs[0].si;
  ensureShell(); ins.style.display = 'block';
  $('ins-label').textContent = 'Part';
  const inLayout = !(XP && XP.layoutRefs) || layoutRefs.has(sel.des);
  driveFrame(inLayout && XP && XP.companion ? { ref: sel.des, href: companionHref({ ref: sel.des }) } : null);
  const diffNote = diffReason(selDes).trim();
  const pins = [];
  secs.forEach((sc, k) => (sc.p.pins || []).forEach(pin => pins.push({
    num: pin[3] || '·', name: pin[4] || '—',
    net: pin[2] ? (netName(pin[2]) || pin[2]) : '(unconnected)', key: pin[2] || '',
    sec: k, secSheet: sc.si,
  })));
  // device-kind note: N two-pin sections of an R/C/L = an array in one package
  const multi = secs.length > 1;
  const allTwoPin = multi && secs.every(sc => (sc.p.pins || []).length === 2);
  const kindNote = multi
    ? (allTwoPin && /^[RCL]/i.test(sel.des)
        ? `${{R:'Resistor',C:'Capacitor',L:'Inductor'}[sel.des[0].toUpperCase()]} array · ${secs.length} × ${sel.val || '?'} in one package`
        : `${secs.length} sections on the schematic — one physical device (${pins.length} pins shown)`)
    : '';
  $('ins-card').innerHTML =
    `<div style="padding:8px 16px 12px;border-bottom:1px solid #EAE6DA">` +
    `<div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap">` +
    `<span style="font-family:'IBM Plex Mono',monospace;font-size:20px;font-weight:600;color:#221F1A">${esc(sel.des)}</span>` +
    (sel.val ? `<span style="font-family:'IBM Plex Mono',monospace;font-size:15px;font-weight:600;color:#C2410C">${esc(sel.val)}</span>` : '') +
    `</div>` +
    `<div style="font-size:11.5px;color:#6E6A60;margin-top:3px;word-break:break-word;line-height:1.35">${esc(sel.pkg || '—')}</div>` +
    `<div style="font-size:10.5px;color:#A19B8E;font-family:'IBM Plex Mono',monospace;margin-top:5px">${esc(sheetLabel(si))}</div>` +
    (kindNote ? `<div style="margin-top:6px;font-size:11px;color:#2563a8;line-height:1.4">${esc(kindNote)}</div>` : '') +
    (inLayout ? '' : `<div style="margin-top:6px;font-size:11px;color:#A19B8E">Not placed in this layout.</div>`) +
    (diffNote ? `<div style="margin-top:6px;font-size:11px;color:#9A6700;font-family:'IBM Plex Mono',monospace;white-space:pre-line">${esc(diffNote)}</div>` : '') +
    `</div>` +
    `<div style="padding:8px 8px 14px">` +
    `<div style="font-size:10px;font-weight:700;letter-spacing:.09em;text-transform:uppercase;color:#8B8578;padding:2px 8px 6px">Pins · ${pins.length}${multi ? ` · ${secs.length} sections` : ''}</div>` +
    pins.map((pn, i) =>
      (multi && (i === 0 || pins[i - 1].sec !== pn.sec)
        ? `<div style="font-size:9.5px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:#B7AF9C;padding:7px 8px 2px">Section ${pn.sec + 1} · ${esc(sheetLabel(pn.secSheet))}</div>` : '') +
      `<div class="pinrow" data-i="${i}" style="display:grid;grid-template-columns:30px 1fr;gap:2px 8px;padding:5px 8px;border-radius:6px;cursor:pointer">` +
      `<span style="font-family:'IBM Plex Mono',monospace;font-size:10px;color:#A19B8E;text-align:right;padding-top:2px">${esc(pn.num)}</span>` +
      `<span style="min-width:0"><span style="display:block;font-size:11.5px;color:#221F1A;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(pn.name)}</span>` +
      `<span style="display:block;font-family:'IBM Plex Mono',monospace;font-size:10px;color:#4338CA;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(pn.net)}</span></span></div>`
    ).join('') + `</div>`;
  [...ins.querySelectorAll('.pinrow')].forEach(el => {
    const k = pins[+el.dataset.i].key;
    el.addEventListener('click', () => { if (k) togglePin(k); });
    el.addEventListener('mouseenter', () => previewNet(k));   // highlight net in main view
    el.addEventListener('mouseleave', clearPreview);
  });
  if (si === cur) positionInspectorNear(sel.des);   // float next to the part
}
function renderBom() {
  const bom = $('bom');
  if (!bomOpen) { bom.style.display = 'none'; bom.innerHTML = ''; return; }
  const t = bomQ.trim().toLowerCase();
  const rows = bomAll.filter(r => !t || r.val.toLowerCase().includes(t) || r.pkg.toLowerCase().includes(t) || r.refs.some(x => x.des.toLowerCase().includes(t)));
  const lineItems = bomAll.length, valued = bomAll.filter(r => r.val).length;
  bom.style.display = 'block';
  bom.innerHTML =
    `<div id="bom-back" style="position:fixed;inset:0;z-index:100;background:rgba(24,20,12,0.38);display:flex;align-items:center;justify-content:center">` +
    `<div id="bom-card" style="width:min(880px,94vw);max-height:80vh;display:flex;flex-direction:column;background:#FBFAF7;border-radius:16px;box-shadow:0 24px 64px rgba(20,16,8,0.30);overflow:hidden">` +
    `<div style="display:flex;align-items:center;gap:12px;padding:14px 18px;border-bottom:1px solid #E7E3D7">` +
    `<div style="min-width:0"><div style="font-size:15px;font-weight:700">Bill of materials</div>` +
    `<div style="font-size:11px;color:#8B8578;font-family:'IBM Plex Mono',monospace;margin-top:1px">${partCount} parts · ${lineItems} line items · ${valued} valued · ${M.sheets.length} sheets</div></div>` +
    `<div style="flex:1"></div>` +
    `<input id="bom-q" class="srch" value="${esc(bomQ)}" placeholder="Filter value, package, or ref…" autocomplete="off" spellcheck="false" style="width:230px;height:30px;padding:0 10px;font-size:11.5px">` +
    `<button id="bom-x" class="iconx" style="font-size:17px">&times;</button></div>` +
    `<div style="display:grid;grid-template-columns:96px 150px 44px 1fr;gap:14px;padding:8px 18px;border-bottom:1px solid #E7E3D7;font-size:10px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#8B8578">` +
    `<span>Value</span><span>Package</span><span style="text-align:right">Qty</span><span>References</span></div>` +
    `<div id="bom-rows" style="overflow-y:auto">` + bomRowsHTML(rows) + `</div></div></div>`;
  $('bom-back').addEventListener('click', () => { bomOpen = false; renderBom(); });
  $('bom-card').addEventListener('click', e => e.stopPropagation());
  $('bom-x').addEventListener('click', () => { bomOpen = false; renderBom(); });
  const bq = $('bom-q');
  bq.addEventListener('input', e => { bomQ = e.target.value; const tt = bomQ.trim().toLowerCase(); const rr = bomAll.filter(r => !tt || r.val.toLowerCase().includes(tt) || r.pkg.toLowerCase().includes(tt) || r.refs.some(x => x.des.toLowerCase().includes(tt))); $('bom-rows').innerHTML = bomRowsHTML(rr); bindBomRefs(); });
  bindBomRefs();
}
function bomRowsHTML(rows) {
  return rows.map(r =>
    `<div style="display:grid;grid-template-columns:96px 150px 44px 1fr;gap:14px;padding:8px 18px;border-bottom:1px solid #F0EDE3;align-items:start">` +
    `<span style="font-family:'IBM Plex Mono',monospace;font-size:12px;font-weight:600;color:${r.val ? '#C2410C' : '#A19B8E'};word-break:break-word;line-height:1.4">${esc(r.val || '—')}</span>` +
    `<span style="font-family:'IBM Plex Mono',monospace;font-size:11px;color:#6E6A60;word-break:break-word;line-height:1.4">${esc(r.pkg)}</span>` +
    `<span style="font-family:'IBM Plex Mono',monospace;font-size:12px;color:#6E6A60;text-align:right">${r.refs.length}</span>` +
    `<span style="display:flex;flex-wrap:wrap;gap:4px">` +
    r.refs.map(x => `<button class="bomref" data-si="${x.si}" data-des="${esc(x.des)}">${esc(x.des)}</button>`).join('') +
    `</span></div>`
  ).join('');
}
function bindBomRefs() {
  [...$('bom-rows').querySelectorAll('.bomref')].forEach(el => el.addEventListener('click', () => {
    bomOpen = false; renderBom(); goToPart(+el.dataset.si, el.dataset.des);
  }));
}

/* ---------- ui handlers ---------- */
function toggleSheets() { sheetsOpen = !sheetsOpen; updChrome(); }
function toggleTheme() {
  dark = !dark;
  $('stage').classList.toggle('dark', dark);
  svgEl.classList.toggle('dark', dark);
  miniEl.classList.toggle('dark', dark);
  updChrome();
}
function bindToolbar() {
  $('tb-sheets-btn').addEventListener('click', toggleSheets);
  $('to-lay').addEventListener('click', () => location.href = layoutHref());  // visibility toggled in __renderModel
  const moreMenu = $('tb-more-menu');
  $('tb-more').addEventListener('click', e => { e.stopPropagation(); moreMenu.style.display = moreMenu.style.display === 'none' ? 'block' : 'none'; });
  document.addEventListener('click', () => { if (moreMenu) moreMenu.style.display = 'none'; });
  $('tb-theme').addEventListener('click', toggleTheme);
  $('tb-bom').addEventListener('click', () => { bomOpen = true; bomQ = ''; renderBom(); });
  $('tb-fit').addEventListener('click', fit);
  $('tb-zin').addEventListener('click', () => zoomBy(1.3));
  $('tb-zout').addEventListener('click', () => zoomBy(0.77));
  const si = $('tb-search');
  si.addEventListener('input', e => { q = e.target.value; searchFocus = true; renderDrop(); });
  si.addEventListener('focus', () => { searchFocus = true; renderDrop(); });
  si.addEventListener('blur', () => setTimeout(() => { searchFocus = false; renderDrop(); }, 150));
  window.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      if (bomOpen) { bomOpen = false; renderBom(); }
      else if (selDes) closeSel();
      else if (pinNets.length) clearPins();
    }
  });
  window.addEventListener('resize', () => { updateVp(); });
}

// Model-level diff between two stored revisions — the client-side counterpart of
// orcad_convert.compute_diff. Components are keyed by refdes. Named nets compare by
// name. Anonymous N$… keys embed per-save object ids (renumbered on every save), so
// they are matched STRUCTURALLY instead: a net's identity is its endpoint set
// (REF.PIN list); old and new anonymous nets pair by best endpoint overlap. A net
// that merely got renumbered matches 100% and stays quiet; a pin moved to a
// different net breaks the match only for that part.
function matchAnonNets(oldM, newM) {
  const endpoints = m => {          // netKey -> Set("REF.PIN"), anonymous nets only
    const eps = {};
    m.sheets.forEach(s => s.parts.forEach(p => (p.pins || []).forEach(pin => {
      const k = pin[2]; if (!k || !/^N\$/.test(k)) return;
      (eps[k] = eps[k] || new Set()).add(p.des + '.' + (pin[3] || '?'));
    })));
    return eps;
  };
  const oe = endpoints(oldM), ne = endpoints(newM);
  const byEp = {};                   // endpoint -> [new net keys] for candidate lookup
  for (const k in ne) for (const ep of ne[k]) (byEp[ep] = byEp[ep] || []).push(k);
  const oldId = {}, newId = {}, usedNew = new Set();
  let seq = 0;
  const label = eps => { const a = [...eps].sort(); return '(' + a.slice(0, 2).join('–') + (a.length > 2 ? ' +' + (a.length - 2) : '') + ')'; };
  // largest nets first so big buses claim their best match before 2-pin stubs
  for (const ok of Object.keys(oe).sort((a, b) => oe[b].size - oe[a].size)) {
    const cand = {};
    for (const ep of oe[ok]) for (const nk of (byEp[ep] || [])) if (!usedNew.has(nk)) cand[nk] = (cand[nk] || 0) + 1;
    let best = null, bc = 0;
    for (const nk in cand) if (cand[nk] > bc) { bc = cand[nk]; best = nk; }
    if (best && bc * 2 >= Math.max(oe[ok].size, ne[best].size)) {   // >=50% overlap
      const id = 'anon net ' + label(ne[best]);
      oldId[ok] = id; newId[best] = id; usedNew.add(best); seq++;
    } else oldId[ok] = 'anon net ' + label(oe[ok]) + ' [gone]';
  }
  for (const nk in ne) if (!(nk in newId)) newId[nk] = 'anon net ' + label(ne[nk]) + ' [new]';
  return { oldId, newId };
}

function computeModelDiff(oldM, newM, oldLabel) {
  const anon = matchAnonNets(oldM, newM);
  const index = (m, ids) => {
    const out = {};
    m.sheets.forEach(s => s.parts.forEach(p => {
      const e = out[p.des] = out[p.des] || { pkg: p.pkg, nets: new Set(), pins: 0 };
      e.pins += (p.pins || []).length;
      (p.pins || []).forEach(pin => {
        const n = pin[2]; if (!n) return;
        e.nets.add(/^N\$/.test(n) ? (ids[n] || n) : n);   // anonymous → structural identity
      });
    }));
    return out;
  };
  const o = index(oldM, anon.oldId), n = index(newM, anon.newId);
  const added = Object.keys(n).filter(d => !(d in o)).sort();
  const removed = Object.keys(o).filter(d => !(d in n)).sort();
  const changed = {};
  for (const d of Object.keys(n).filter(d => d in o).sort()) {
    const a = o[d], b = n[d], reasons = [];
    if (a.pkg !== b.pkg) reasons.push(`package ${a.pkg} → ${b.pkg}`);
    if (a.pins !== b.pins) reasons.push(`pins ${a.pins} → ${b.pins}`);
    const gone = [...a.nets].filter(x => !b.nets.has(x)), got = [...b.nets].filter(x => !a.nets.has(x));
    if (gone.length) reasons.push('nets removed: ' + gone.sort().join(', '));
    if (got.length) reasons.push('nets added: ' + got.sort().join(', '));
    if (reasons.length) changed[d] = reasons;
  }
  // ghost geometry for removed parts — full symbol so it draws in red where it sat
  const rm = new Set(removed), ghosts = {};
  oldM.sheets.forEach(s => {
    const gs = s.parts.filter(p => rm.has(p.des) && p.box)
      .map(p => ({ des: p.des, box: p.box, sym: p.sym, pins: p.pins, val: p.val, pkg: p.pkg }));
    if (gs.length) ghosts[s.id] = gs;
  });
  // trace diff, per sheet id: added wires (green) and removed wires (red)
  const wkey = w => { let a = [Math.round(w[0]), Math.round(w[1])], b = [Math.round(w[2]), Math.round(w[3])];
    if (a[0] > b[0] || (a[0] === b[0] && a[1] > b[1])) { const t = a; a = b; b = t; }
    return a[0] + ',' + a[1] + ',' + b[0] + ',' + b[1]; };
  const oldBy = {}, newBy = {};
  oldM.sheets.forEach(s => oldBy[s.id] = s);
  newM.sheets.forEach(s => newBy[s.id] = s);
  const addedWires = {}, removedWires = {};
  newM.sheets.forEach(s => { const os = new Set(((oldBy[s.id] || {}).wires || []).map(wkey));
    const a = (s.wires || []).filter(w => !os.has(wkey(w))).map(wkey);
    if (a.length) addedWires[s.id] = a; });
  oldM.sheets.forEach(s => { const ns = new Set(((newBy[s.id] || {}).wires || []).map(wkey));
    const r = (s.wires || []).filter(w => !ns.has(wkey(w)));
    if (r.length) removedWires[s.id] = r; });
  return { diff: { old: oldLabel, added, removed, changed }, removedGeom: ghosts, addedWires, removedWires };
}

// Revision selector (shell mode): switch revs, open a diff against an older rev.
function renderRevUI() {
  const meta = window.__docMeta, el = $('tb-rev');
  if (!el || !meta || !(meta.rev > 1)) return;
  const revs = (meta.revs && meta.revs.length ? meta.revs.map(r => +r.n) : Array.from({ length: meta.rev }, (_, i) => i + 1)).sort((a, b) => b - a);
  const url = (r, dv) => { const u = new URLSearchParams(location.search); u.set('rev', r); if (dv) u.set('diff', dv); else u.delete('diff'); return '?' + u.toString(); };
  el.style.display = 'flex';
  el.innerHTML = `<button class="tbtn" id="tb-rev-btn" style="min-width:64px">Rev ${meta.curRev}${meta.diffRev ? ' vs ' + meta.diffRev : ''} ▾</button>` +
    `<div id="tb-rev-menu" style="display:none;position:absolute;top:40px;right:0;min-width:190px;background:#fff;border:1px solid #E0DCD1;border-radius:10px;box-shadow:0 14px 32px rgba(24,20,10,.16);padding:5px;z-index:90"></div>`;
  const menu = $('tb-rev-menu');
  menu.innerHTML = revs.map(r =>
    `<a href="${url(r)}" style="display:block;padding:7px 9px;border-radius:6px;text-decoration:none;color:#221F1A;font-size:12.5px;${r === meta.curRev && !meta.diffRev ? 'background:#F1EDE3;font-weight:600' : ''}">Rev ${r}${r === meta.rev ? ' (latest)' : ''}</a>` +
    (r < meta.curRev ? `<a href="${url(meta.curRev, r)}" style="display:block;padding:5px 9px 7px 22px;border-radius:6px;text-decoration:none;color:#9A6700;font-size:11.5px;${meta.diffRev === r ? 'background:#F9F3E4;font-weight:600' : ''}">&Delta; diff vs rev ${r}</a>` : '')
  ).join('') + (meta.diffRev ? `<a href="${url(meta.curRev)}" style="display:block;padding:7px 9px;border-radius:6px;text-decoration:none;color:#CF222E;font-size:11.5px;border-top:1px solid #EEE9DE;margin-top:4px">clear diff</a>` : '');
  $('tb-rev-btn').onclick = e => { e.stopPropagation(); menu.style.display = menu.style.display === 'none' ? 'block' : 'none'; };
  document.addEventListener('click', () => { menu.style.display = 'none'; });
}

// Entry point. Embedded builds call this immediately (see /*__BOOT__*/); the hosted
// shell calls it from the auth+fetch controller once the model is loaded. In shell
// mode a third argument carries an OLDER revision's model to diff against.
window.__renderModel = function (model, xprobe, oldModel) {
 try {
  M = model; XP = xprobe || null;
  DBG.log('renderModel: model stats =', {
    name: M && M.name, sheets: M && M.sheets && M.sheets.length,
    parts: M && M.sheets ? M.sheets.reduce((n, s) => n + (s.parts ? s.parts.length : 0), 0) : 0,
    wires: M && M.sheets ? M.sheets.reduce((n, s) => n + (s.wires ? s.wires.length : 0), 0) : 0,
    xprobe: !!XP, companion: !!(XP && XP.companion), standalone: !!(XP && XP.standalone),
    hasOldModel: !!oldModel });
  { const tl = document.getElementById('to-lay'); if (tl) tl.style.display = (XP && XP.companion) ? '' : 'none'; }
  if (oldModel) {
    const meta = window.__docMeta || {};
    const r = computeModelDiff(oldModel, M, 'rev ' + (meta.diffRev || '?'));
    M.diff = r.diff; M.removedGeom = r.removedGeom; M.addedWires = r.addedWires; M.removedWires = r.removedWires;
  }
  DBG.log('renderModel: init() start (build sheets + render scene)…');
  init();
  renderRevUI();
  DBG.log('renderModel: completed OK', {sheet: (typeof cur !== 'undefined' ? cur : null)});
 } catch (err) {
  DBG.error('renderModel FAILED:', (err && (err.stack || err.message)) || err);
  try { document.body.insertAdjacentHTML('beforeend',
    '<div style="position:fixed;left:12px;bottom:12px;max-width:64ch;padding:11px 13px;'
    +'background:#7f1d1d;color:#fff;font:12px/1.45 ui-monospace,Menlo,monospace;border-radius:9px;z-index:99999">'
    +'⚠ Schematic viewer hit an error. Open the browser console (⌥⌘J / Ctrl+Shift+J), copy the '
    +'<b>[CanvasPCB/schematic]</b> lines, and send them.</div>'); } catch(_){}
  throw err;
 }
};
/*__BOOT__*/
</script>
<!--__CMT_FIREBASE__-->
</body></html>"""


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dsn", type=Path, help="OrCAD Capture .DSN")
    ap.add_argument("-o", "--output", type=Path, default=None)
    ap.add_argument("--diff", type=Path, default=None,
                    help="an older .DSN to diff against")
    ap.add_argument("--offline", action="store_true",
                    help="fully self-contained file: no web fonts, no comments backend")
    args = ap.parse_args()
    out = args.output or args.dsn.parent / (args.dsn.stem + "_schematic.html")
    generate(args.dsn, out, args.diff, offline=args.offline)


if __name__ == "__main__":
    main()
