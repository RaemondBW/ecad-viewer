#!/usr/bin/env python3
"""
orcad_convert.py — Convert an OrCAD Capture .DSN design to the schematic-viewer
pinout format (the same pinout/*.json that generate.py consumes).

The .DSN is a Microsoft Compound File (OLE2) whose schematic pages are stored in
Cadence's proprietary binary structure format. This module reconstructs
connectivity directly from those binary streams — no OrCAD/Cadence tools needed.

Approach (validated empirically against Werni2A/OpenOrCadParser's reversing):
  * Each schematic page (Views/<view>/Pages/<page>) is a sequence of "structure"
    records. We locate the records we need by their distinctive binary
    signatures rather than by strict sequential parsing (which is
    version-sensitive and fragile).
  * Wires (Structure::WireScalar) carry a uint32 net `id` plus two int32
    endpoints. Every segment of a net shares the same id.
  * The page's net list maps net id -> net name.
  * Part instances (Structure::PlacedInstance) carry a package name, a
    reference designator, and per-pin placed coordinates (T0x10 records).
  * A pin binds to a net geometrically: the pin's placed coordinate coincides
    with a wire endpoint (validated 100% on the sample design).
  * Nets are merged across pages by name (global/power/off-page/hierarchical
    connectivity); unnamed nets remain page-local and get synthetic names.

Usage:
    python schematic-viewer/orcad_convert.py design.DSN [--out OUTPUT_DIR]
        [--view] [--no-download]

If OUTPUT_DIR is omitted, a directory named <design>_viewer/ is created next to
the .DSN file, with pinout/*.json inside it.
"""

import argparse
import json
import re
import struct
import subprocess
import sys
from pathlib import Path

try:
    import olefile
except ImportError:
    sys.exit("This tool needs the 'olefile' package: pip install olefile")

SCRIPT_DIR = Path(__file__).parent
PREAMBLE_MAGIC = bytes.fromhex("ffe45c39")  # struct-boundary sentinel

# Handles / ids live in a per-file object-id space; on the sample design that is
# the 0x0097_0000..0x0098_0000 window. We detect the window per file instead of
# hard-coding it.


def _u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


# ── ID window detection ───────────────────────────────────────────────────────

def _detect_net_window(data):
    """Net ids live in a 0xHHHH_0000 window that differs per view/page. Derive
    it from the wire signature (<u32 handle><u32 net_id><u32 0x30>
    <4×i32 bounded, nonzero-length coords>): the wire's own handle sits in a
    separate low object window, but every net_id shares the page's net window.
    Return the dominant nonzero high-16 of net_id, or None if no wires."""
    from collections import Counter
    hi = Counter()
    n = len(data)
    for o in range(0, n - 28):
        if _u32(data, o + 8) != 0x30:
            continue
        nid = _u32(data, o + 4)
        if (nid >> 16) == 0:
            continue
        x1, y1, x2, y2 = struct.unpack_from("<iiii", data, o + 12)
        if (x1, y1) != (x2, y2) and max(abs(x1), abs(y1), abs(x2), abs(y2)) < 100000:
            hi[nid >> 16] += 1
    if not hi:
        return None
    return hi.most_common(1)[0][0]


# ── Net name table (per page) ─────────────────────────────────────────────────

def _parse_net_names(data, net_win):
    """The page stores a run of [uint32 net_id][uint16 len][name\\0] entries
    where net_id is in the page's net window. Returns {net_id: name}."""
    names = {}
    o = 0
    n = len(data)
    while o < n - 7:
        vid = _u32(data, o)
        if (vid >> 16) == net_win:
            ln = _u16(data, o + 4)
            if 0 < ln < 64 and o + 6 + ln < n:
                s = data[o + 6:o + 6 + ln]
                if s and all(32 <= c < 127 for c in s) and data[o + 6 + ln] == 0:
                    names.setdefault(vid, s.decode("latin1"))
                    o += 6 + ln + 1
                    continue
        o += 1
    return names


# ── Wires ─────────────────────────────────────────────────────────────────────

def _parse_wires(data, net_win):
    """Structure::WireScalar body: <u32 handle><u32 net_id><u32 0x30>
    <i32 x1><i32 y1><i32 x2><i32 y2>. net_id is in the page's net window; the
    handle sits in a separate low object window. Returns (list of
    (net_id,x1,y1,x2,y2), object_window) — the object window is the dominant
    high-16 of the wire handles, shared by pin (T0x10) handles."""
    from collections import Counter
    wires = []
    objhi = Counter()
    n = len(data)
    for o in range(0, n - 28):
        nid = _u32(data, o + 4)
        if (nid >> 16) != net_win:
            continue
        if _u32(data, o + 8) != 0x30:
            continue
        x1, y1, x2, y2 = struct.unpack_from("<iiii", data, o + 12)
        if (x1, y1) != (x2, y2) and max(abs(x1), abs(y1), abs(x2), abs(y2)) < 100000:
            wires.append((nid, x1, y1, x2, y2))
            h = _u32(data, o) >> 16
            if h:
                objhi[h] += 1
    # Object handles for pins live in one or more high-16 windows (a page can use
    # several, and some pins even carry 16-bit handles with high-16 == 0). Keep
    # every window that recurs among wire handles; pin detection also falls back
    # to wire-endpoint coincidence, so this set only needs to catch NC pins.
    obj_wins = {k for k, c in objhi.items() if c >= 2}
    return wires, obj_wins


def _wire_point_nets(wires):
    """Map every point that lies on a wire (endpoints + integer grid points
    along axis-aligned segments) to its net id, for pin snapping."""
    pt = {}
    for nid, x1, y1, x2, y2 in wires:
        pt[(x1, y1)] = nid
        pt[(x2, y2)] = nid
    return pt


# ── Part instances + pins ─────────────────────────────────────────────────────

_PKG_RE = re.compile(rb"([\x20-\x7e]{1,48})\.Normal\x00")
def _parse_pins_in(data, a, b, endpoints, obj_wins, bound):
    """Pin (T0x10) signature within [a,b): <u16 idx 1..><u16 x><u16 y>
    <u32 handle><u32 0>. A candidate is a real pin if its coordinate lands on a
    wire endpoint (the connectivity-validated test — connected pins) OR its
    handle sits in an object window (catches not-connected pins). Both reject
    the property/graphic records that also look pin-shaped (off-wire coords in
    unrelated handle windows). `bound` = (lo_x, lo_y, hi_x, hi_y) rejects the
    occasional false record whose handle happens to fall in an object window but
    whose coordinate is far off-sheet (which otherwise blows up a part's box).
    Returns [(idx,x,y)] deduped by handle."""
    lo_x, lo_y, hi_x, hi_y = bound
    pins = {}
    o = a
    while o < b - 14:
        idx, x, y = struct.unpack_from("<HHH", data, o)
        h = _u32(data, o + 6)
        z = _u32(data, o + 10)
        if 1 <= idx <= 200 and h != 0 and z == 0 and 0 < x < 60000 and 0 < y < 60000 \
                and ((x, y) in endpoints
                     or ((h >> 16) in obj_wins
                         and lo_x <= x <= hi_x and lo_y <= y <= hi_y)):
            pins[h] = (idx, x, y)
            o += 14
            continue
        o += 1
    return sorted(pins.values())


def _strings_in(data, a, b):
    """All length-prefixed zero-terminated strings in [a,b): (offset, text)."""
    out = []
    o = a
    while o < b - 2:
        ln = _u16(data, o)
        if 0 < ln < 48 and o + 2 + ln < b:
            s = data[o + 2:o + 2 + ln]
            if all(32 <= c < 127 for c in s) and data[o + 2 + ln] == 0:
                out.append((o, s.decode("latin1")))
                o += 2 + ln + 1
                continue
        o += 1
    return out


def _parse_instances(data, endpoints, obj_wins, bound):
    """Return list of dicts: {designator, package, pins:[(idx,x,y)]}."""
    refs = [(m.start(), m.group(1).decode("latin1"))
            for m in _PKG_RE.finditer(data)]
    bounds = [r[0] for r in refs] + [len(data)]
    insts = []
    for i, (off, pkg) in enumerate(refs):
        start = off
        end = bounds[i + 1]
        after = off + len(pkg) + len(".Normal") + 1
        strs = [s for _, s in _strings_in(data, after, end)]
        # designator = last string that isn't the trailing source_package and
        # isn't a property-value token like  '"  ; source_package == last string.
        real = [s for s in strs if s not in ("'\"",)]
        designator = None
        if len(real) >= 2:
            designator = real[-2]
        elif real:
            designator = real[0]
        pins = _parse_pins_in(data, after, end, endpoints, obj_wins, bound)
        insts.append({
            "designator": designator or f"?{i}",
            "package": pkg,
            "pins": pins,
        })
    return insts


# ── Page + design assembly ────────────────────────────────────────────────────

def load_dsn(path):
    """Parse a .DSN into a design dict with pages, wires, instances, net names."""
    ole = olefile.OleFileIO(str(path))
    pages = {}
    for entry in ole.listdir():
        p = "/".join(entry)
        if p.startswith("Views/") and "/Pages/" in p:
            view = entry[1]
            page = entry[-1]
            pages[f"{view}/{page}"] = ole.openstream(entry).read()
    titleblock, symbols = None, {}
    if ole.exists("Cache"):
        try:
            cache = ole.openstream("Cache").read()
            titleblock = _extract_titleblock(cache)
            symbols = parse_cache_symbols(cache)
        except Exception:
            titleblock, symbols = None, {}
    design = {"pages": {}, "titleblock": titleblock, "symbols": symbols}
    for pageid, data in pages.items():
        net_win = _detect_net_window(data)
        graphics = _parse_graphics(data)
        if net_win is None:
            # No wires on this page (e.g. a pure hierarchical-block page).
            design["pages"][pageid] = {
                "wires": [], "net_names": {}, "instances": [],
                "point_nets": {}, "net_win": None, "obj_wins": set(),
                "graphics": graphics,
            }
            continue
        wires, obj_wins = _parse_wires(data, net_win)
        names = _parse_net_names(data, net_win)
        point_nets = _wire_point_nets(wires)
        # Sheet bound for rejecting off-sheet false pins: prefer the drawn page
        # frame (border graphics); else the wire extent, generously padded.
        gl = graphics["lines"] + graphics["rects"]
        if gl:
            fxs = [c for r in gl for c in (r[0], r[2])]
            fys = [c for r in gl for c in (r[1], r[3])]
            bound = (min(fxs) - 200, min(fys) - 200, max(fxs) + 200, max(fys) + 200)
        elif wires:
            wxs = [c for w in wires for c in (w[1], w[3])]
            wys = [c for w in wires for c in (w[2], w[4])]
            bound = (min(wxs) - 1000, min(wys) - 1000, max(wxs) + 1000, max(wys) + 1000)
        else:
            bound = (-5000, -5000, 80000, 80000)
        insts = _parse_instances(data, set(point_nets), obj_wins, bound)
        design["pages"][pageid] = {
            "wires": wires,
            "net_names": names,
            "instances": insts,
            "point_nets": point_nets,
            "net_win": net_win,
            "obj_wins": obj_wins,
            "graphics": graphics,
        }
    return design


def _net_key(pageid, nid, names):
    """Stable net key. Named nets merge across pages by name; unnamed nets are
    page-local synthetic ids."""
    nm = names.get(nid)
    if nm:
        return nm
    return f"N${pageid}:{nid:08x}"


def build_components(design):
    """Resolve each instance's pins to nets, return list of component dicts in
    the pinout schema generate.py expects."""
    components = []
    for pageid, page in design["pages"].items():
        pt = page["point_nets"]
        names = page["net_names"]
        for inst in page["instances"]:
            leads = []
            for idx, x, y in inst["pins"]:
                nid = pt.get((x, y))
                net = _net_key(pageid, nid, names) if nid is not None else ""
                leads.append({
                    "leadDesignator": str(idx),
                    "padNumbers": [str(idx)],
                    "netName": net,
                    "signalType": "signal",
                    "interfaces": [],
                    "isConnected": bool(net),
                })
            des = inst["designator"]
            pkg = inst["package"]
            components.append({
                "name": des,
                "atoAddress": des,
                "designator": des,
                "descriptor": f"{des} — {pkg}",
                "typeName": pkg,
                "footprintUuid": "",
                "leads": leads,
                "warnings": [],
                "_page": pageid,
            })
    return components


def build_sheets(design):
    """Produce a per-sheet geometry model for the native-geometry viewer:
        {name, sheets:[{id, view, page, bbox, wires, parts, labels, nets}]}
    Coordinates are the schematic's own units (mils); the viewer transforms
    them. Net keys merge named nets (by name); unnamed nets are page-local."""
    sheets = []
    for pageid, page in design["pages"].items():
        wires = page["wires"]
        names = page["net_names"]
        insts = page["instances"]
        graphics = page.get("graphics", {"lines": [], "rects": [], "texts": [], "polys": []})
        if not wires and not insts and not graphics["lines"]:
            continue
        view, _, pagename = pageid.partition("/")

        out_wires = []
        label_seen = {}
        net_used = {}
        for nid, x1, y1, x2, y2 in wires:
            key = _net_key(pageid, nid, names)
            out_wires.append([x1, y1, x2, y2, key])
            net_used[key] = names.get(nid, "")
            # remember a representative point per named net for a single label
            if names.get(nid) and key not in label_seen:
                label_seen[key] = ((x1 + x2) // 2, (y1 + y2) // 2)

        pt = page["point_nets"]
        symbols = design.get("symbols", {})
        out_parts = []
        for inst in insts:
            pins = inst["pins"]
            sym = _classify(inst["package"], inst["designator"], len(pins))
            # pin names for IC-style (box) parts, matched geometrically to the
            # cached symbol definition
            pin_names = {}
            if sym == "box":
                sp = symbols.get(inst["package"])
                if sp:
                    pin_names = assign_pin_names(pins, sp)
            ppins = []
            for idx, x, y in pins:
                nid = pt.get((x, y))
                key = _net_key(pageid, nid, names) if nid is not None else ""
                ppins.append([x, y, key, str(idx), pin_names.get(idx, "")])
                if key:
                    net_used.setdefault(key, names.get(nid, "") if nid else "")
            box = _part_box(pins)
            out_parts.append({
                "des": inst["designator"],
                "pkg": inst["package"],
                "box": box,
                "pins": ppins,
                "sym": sym,
            })

        # Power/ground/off-page flags at every dangling wire end.
        pins_set = set((x, y) for inst in insts for _, x, y in inst["pins"])
        flags = _dangling_flags(wires, pins_set, pt, names, pageid)
        flag_keys = set(f["key"] for f in flags if f["key"])

        # Net-name labels on wires — but not where a flag already carries the
        # name (the flag is the port label, as in OrCAD).
        labels = [{"x": p[0], "y": p[1], "text": names_text, "key": key}
                  for key, p in label_seen.items()
                  for names_text in (net_used.get(key, ""),)
                  if names_text and key not in flag_keys]

        # Junction dots: a point where three or more wire ends meet (a real
        # electrical tie, as OrCAD draws with a solid dot).
        from collections import Counter as _C
        endc = _C()
        for _n, x1, y1, x2, y2 in wires:
            endc[(x1, y1)] += 1
            endc[(x2, y2)] += 1
        junctions = [[x, y] for (x, y), c in endc.items() if c >= 3]

        # The page frame is the bounding box of the border line/rect graphics
        # (the drawn sheet edge). Fall back to content extent if absent.
        fxs, fys = [], []
        for x1, y1, x2, y2 in graphics["lines"] + graphics["rects"]:
            fxs += [x1, x2]; fys += [y1, y2]
        frame = [min(fxs), min(fys), max(fxs), max(fys)] if fxs else None

        # bounding box over everything on the sheet
        xs, ys = [], []
        for x1, y1, x2, y2, _ in out_wires:
            xs += [x1, x2]; ys += [y1, y2]
        for p in out_parts:
            if p["box"]:
                bx, by, bw, bh = p["box"]
                xs += [bx, bx + bw]; ys += [by, by + bh]
        if frame:
            xs += [frame[0], frame[2]]; ys += [frame[1], frame[3]]
        bbox = [min(xs), min(ys), max(xs), max(ys)] if xs else [0, 0, 100, 100]

        sheets.append({
            "id": pageid,
            "view": view,
            "page": pagename,
            "bbox": bbox,
            "frame": frame,
            "wires": out_wires,
            "parts": out_parts,
            "labels": labels,
            "graphics": graphics,
            "junctions": junctions,
            "connectors": flags,
            "nets": net_used,
        })
    # order sheets by view name (they are prefixed 01_, 02_, ...)
    sheets.sort(key=lambda s: s["id"])

    # Per-sheet title-block fields (placed bottom-right of the frame).
    tb_geom = design.get("titleblock")
    total = len(sheets)
    for i, s in enumerate(sheets):
        if not (tb_geom and s["frame"]):
            s["tb"] = None
            continue
        # descriptive page heading (the big centered comment) shown as a subtitle
        heading = ""
        if s["graphics"]["texts"]:
            heading = max(s["graphics"]["texts"], key=lambda t: t["h"])["s"]
            heading = heading.replace("\r\n", " ").replace("\n", " ")
        fx, fy, fX, fY = s["frame"]
        size = "B" if (fX - fx) > 1400 else "A"
        s["tb"] = {
            "ox": fX - tb_geom["w"], "oy": fY - tb_geom["h"],
            "title": s["page"] or s["view"],   # short sheet name in the Title cell
            "heading": heading,
            "size": size, "rev": "", "date": "",
            "n": i + 1, "total": total, "company": "Cadence",
        }

    return {"name": "", "sheets": sheets, "titleblock": tb_geom}


def _classify(pkg, des, npins):
    """Map a part to a schematic-symbol kind for the viewer. Standard 2-terminal
    passives get an icon; everything else is drawn as a box."""
    p = (pkg or "").upper()
    d = (des or "").upper()
    if npins == 2:
        if p.startswith("RES") or (p == "R") or d.startswith("R"):
            return "res"
        if "CAPPOL" in p or "POL" in p or p.startswith("CAPP"):
            return "cape"
        if p.startswith("CAP") or (p == "C") or d.startswith("C"):
            return "cap"
        if p.startswith("IND") or (p == "L") or d.startswith("L"):
            return "ind"
        if p.startswith("DIO") or "LED" in p or d.startswith("D") \
                or p.startswith(("MBR", "MUR", "REC", "BAV", "BAT", "1N")):
            return "diode"
    return "box"


def _parse_graphics(data):
    """Extract page-level drawing primitives (the sheet frame, comment notes,
    boxes, dividers) so the viewer can show the full page layout, not just the
    connectivity. Each graphic primitive is framed as
    <type><type><u32 len><0 0 0 0><body> where type is the Primitive enum
    (Rect=0x28, Line=0x29, Arc=0x2a, Ellipse=0x2b, Polyline=0x2d,
    CommentText=0x2e). Coordinates share the wire/pin space."""
    lines, rects, texts, polys = [], [], [], []
    n = len(data)
    o = 0
    while o < n - 12:
        t = data[o]
        if data[o + 1] == t and 0x28 <= t <= 0x2e:
            ln = _u32(data, o + 2)
            if 0 < ln < 20000 and data[o + 6:o + 10] == b"\x00\x00\x00\x00" \
                    and o + 10 + ln <= n:
                body = o + 10
                ok = True
                if t == 0x29 and ln >= 16:      # Line
                    x1, y1, x2, y2 = struct.unpack_from("<iiii", data, body)
                    if _sane(x1, y1, x2, y2):
                        lines.append([x1, y1, x2, y2])
                    else:
                        ok = False
                elif t == 0x28 and ln >= 16:     # Rect
                    x1, y1, x2, y2 = struct.unpack_from("<iiii", data, body)
                    if _sane(x1, y1, x2, y2):
                        rects.append([x1, y1, x2, y2])
                    else:
                        ok = False
                elif t == 0x2e and ln >= 30:     # CommentText
                    # body: bbox(4 i32), pos(2 i32), u32, u16 fontsize, text\0
                    x1, y1, x2, y2 = struct.unpack_from("<iiii", data, body)
                    px, py = struct.unpack_from("<ii", data, body + 16)
                    size = _u16(data, body + 28)
                    s = data[body + 30:body + ln].split(b"\x00", 1)[0]
                    if _sane(x1, y1, x2, y2) and s:
                        texts.append({
                            "x": px, "y": py,
                            "h": max(abs(y2 - y1), 8),
                            "s": s.decode("latin1"),
                        })
                    else:
                        ok = False
                elif t == 0x2d and ln >= 6:       # Polyline
                    cnt = _u16(data, body)
                    pts = []
                    for k in range(cnt):
                        po = body + 2 + k * 8
                        if po + 8 > o + 10 + ln:
                            break
                        px, py = struct.unpack_from("<ii", data, po)
                        pts.append([px, py])
                    if len(pts) >= 2 and all(_sane(*p, *p) for p in pts):
                        polys.append(pts)
                if ok:
                    o += 10 + ln
                    continue
        o += 1
    return {"lines": lines, "rects": rects, "texts": texts, "polys": polys}


def _sane(*coords):
    return all(-5000 <= c <= 80000 for c in coords)


_SYM_HDR = re.compile(rb"([\x20-\x7e]{2,48})\.Normal\x00")


def parse_cache_symbols(cache):
    """Parse the Cache stream into {symbol_name: [(pin_name, sx, sy)]}. Each
    symbol is a block bounded by `<name>.Normal` headers; its pins are
    SymbolPin records (preamble + <u16 len><name>\\0 + <u32 flags><i32 startX>
    <i32 startY>...). The `start` point is the pin's grid position in the
    symbol's own frame; hotpt overruns into per-pin property bytes so we use
    start. Keeps, per name, the block that actually carries pins."""
    hdrs = [(m.start(), m.end(), m.group(1).decode("latin1"))
            for m in _SYM_HDR.finditer(cache)]
    out = {}
    for i, (hs, he, name) in enumerate(hdrs):
        blk_end = hdrs[i + 1][0] if i + 1 < len(hdrs) else len(cache)
        pins = _symbol_pins(cache, he, blk_end)
        if pins and len(pins) >= len(out.get(name, [])):
            out[name] = pins
    return out


def _symbol_pins(data, a, b):
    pins = []
    o = a
    while True:
        k = data.find(PREAMBLE_MAGIC + b"\x00\x00\x00\x00", o, b)
        if k < 0:
            break
        p = k + 8
        if p + 2 > b:
            break
        ln = _u16(data, p)
        nm = data[p + 2:p + 2 + ln]
        if 0 < ln < 18 and p + 2 + ln + 13 <= b and data[p + 2 + ln] == 0 \
                and all(33 <= x < 127 and x not in (0x2f, 0x5c) for x in nm):
            q = p + 2 + ln + 1
            sx, sy = struct.unpack_from("<ii", data, q + 4)
            if abs(sx) < 5000 and abs(sy) < 5000:
                pins.append((nm.decode("latin1"), sx, sy))
        o = k + 8
    return pins


def assign_pin_names(placed, sym_pins):
    """Map an instance's placed pins [(idx,x,y)] to a symbol's pins
    [(name,sx,sy)] via the rigid transform (rotation k·90° + optional mirror +
    translation) that best aligns the two point sets. Returns {idx: pin_name}.
    Empty if counts differ or no orientation aligns."""
    if not sym_pins or len(placed) != len(sym_pins):
        return {}
    S = [(sx, sy) for _, sx, sy in sym_pins]
    P = [(x, y) for _, x, y in placed]

    def xf(pt, r, mir):
        x, y = pt
        if mir:
            x = -x
        for _ in range(r):
            x, y = -y, x
        return x, y

    best = (0, {})
    for r in range(4):
        for mir in (0, 1):
            T = [xf(pt, r, mir) for pt in S]
            tx = sum(x for x, _ in P) / len(P) - sum(x for x, _ in T) / len(T)
            ty = sum(y for _, y in P) / len(P) - sum(y for _, y in T) / len(T)
            used = [False] * len(P)
            m = {}
            for si, (x, y) in enumerate(T):
                sxp, syp = x + tx, y + ty
                for pi, (px, py) in enumerate(P):
                    if not used[pi] and abs(sxp - px) < 10 and abs(syp - py) < 10:
                        used[pi] = True
                        m[pi] = si
                        break
            if len(m) > best[0]:
                best = (len(m), m)
    idx_to_name = {}
    if best[0] >= max(2, len(placed) * 0.6):
        for pi, si in best[1].items():
            idx_to_name[placed[pi][0]] = sym_pins[si][0]
    return idx_to_name


def _extract_titleblock(cache):
    """The page title-block table is a symbol in the Cache stream (its cells are
    Rect/Line primitives and its field labels — Title/Size/Rev/Date/Sheet —
    are CommentText). Extract its geometry (local coords, origin top-left) so
    the viewer can place it in each sheet's bottom-right corner."""
    anchors = [cache.find(b"Title\x00"), cache.find(b"Sheet\x00"),
               cache.find(b"Size\x00")]
    anchors = [a for a in anchors if a > 0]
    if not anchors:
        return None
    lo, hi = max(0, min(anchors) - 500), max(anchors) + 500
    g = _parse_graphics(cache[lo:hi])
    inbox = lambda r: all(0 <= v <= 400 for v in r[:4])
    lines = [L for L in g["lines"] if inbox(L)]
    rects = [r for r in g["rects"] if inbox(r)]
    labels = [{"x": t["x"], "y": t["y"], "h": t["h"], "s": t["s"]}
              for t in g["texts"] if 0 <= t["x"] <= 400 and 0 <= t["y"] <= 200]
    if not rects:
        return None
    return {"w": max(r[2] for r in rects), "h": max(r[3] for r in rects),
            "lines": lines, "rects": rects, "labels": labels}


_VOLT_RE = re.compile(r"^[+-]?\d+(\.\d+)?V", re.I)


def _flag_kind(net):
    """Classify a net at a dangling wire end into a connector glyph:
    ground / power rail / off-page port (signal to another sheet)."""
    u = (net or "").upper()
    if not u:
        return "port"
    if u in ("GND", "DGND", "AGND", "PGND", "GND_IN", "GND_PE", "VSS", "0V") \
            or u.startswith("GND") or u.endswith("GND"):
        return "gnd"
    if _VOLT_RE.match(u) or u in ("VCC", "VDD", "VEE", "VTT", "VREF", "VBAT") \
            or "VREF" in u or "VTT" in u or u.startswith(("VCC", "VDD")):
        return "pwr"
    return "port"


def _dangling_flags(wires, pins, point_nets, names, pageid):
    """OrCAD attaches a power/ground/off-page connector at every dangling wire
    end (a degree-1 endpoint that isn't a component pin). We place a glyph there
    directly — an exact wire-touch point with the correct net — classifying the
    glyph by the net name and orienting it outward along the wire. This is far
    more reliable than the connector instance records (whose coordinates don't
    carry the wire-touch point). Returns [{x,y,kind,orient,net}]."""
    from collections import Counter
    deg = Counter()
    other = {}   # endpoint -> the segment's opposite end (for outward direction)
    for nid, x1, y1, x2, y2 in wires:
        deg[(x1, y1)] += 1
        deg[(x2, y2)] += 1
        other.setdefault((x1, y1), (x2, y2))
        other.setdefault((x2, y2), (x1, y1))
    flags = []
    for (dx, dy), c in deg.items():
        if c != 1 or (dx, dy) in pins:
            continue
        nid = point_nets.get((dx, dy))
        net = names.get(nid, "") if nid is not None else ""
        kind = _flag_kind(net)
        ox, oy = other.get((dx, dy), (dx, dy))
        vx, vy = dx - ox, dy - oy
        if abs(vx) >= abs(vy):
            orient = "r" if vx > 0 else "l"
        else:
            orient = "d" if vy > 0 else "u"
        key = _net_key(pageid, nid, names) if nid is not None else ""
        flags.append({"x": dx, "y": dy, "kind": kind, "orient": orient,
                      "net": net, "key": key})
    return flags


def _part_box(pins):
    """A component body box derived from its pin extents (mils)."""
    if not pins:
        return None
    xs = [p[1] for p in pins]
    ys = [p[2] for p in pins]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    pad = 8
    w = max(x1 - x0, 16)
    h = max(y1 - y0, 16)
    return [x0 - pad, y0 - pad, w + 2 * pad, h + 2 * pad]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dsn", type=Path, help="OrCAD Capture .DSN file")
    ap.add_argument("--out", type=Path, default=None, help="output viewer dir")
    ap.add_argument("--summary", action="store_true", help="print a summary and exit")
    args = ap.parse_args()

    design = load_dsn(args.dsn)
    components = build_components(design)

    if args.summary:
        _print_summary(design, components)
        return

    out = args.out or args.dsn.parent / (args.dsn.stem + "_viewer")
    pinout = out / "pinout"
    pinout.mkdir(parents=True, exist_ok=True)
    for i, c in enumerate(components):
        (pinout / f"{i:03d}_{c['designator'].lower()}.json").write_text(
            json.dumps(c, indent=2))
    print(f"Wrote {len(components)} components to {pinout}")


def _print_summary(design, components):
    print(f"pages: {len(design['pages'])}")
    for pageid, page in design["pages"].items():
        nnamed = len(page["net_names"])
        win = page["net_win"]
        wtxt = f"{win:04x}" if win else "----"
        print(f"  {pageid}: {len(page['instances'])} parts, "
              f"{len(page['wires'])} wires, {nnamed} named nets (win {wtxt})")
    print(f"\ncomponents: {len(components)}")
    connected = sum(1 for c in components
                    for l in c["leads"] if l["isConnected"])
    total = sum(len(c["leads"]) for c in components)
    print(f"leads: {total} total, {connected} connected "
          f"({100*connected//max(total,1)}%)")
    for c in components[:12]:
        nets = ",".join(l["netName"] or "-" for l in c["leads"])
        print(f"  {c['designator']:10s} {c['typeName']:22s} [{nets}]")


if __name__ == "__main__":
    main()
