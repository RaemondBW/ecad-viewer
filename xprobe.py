"""Cross-probe correspondence between an OrCAD .DSN schematic and an Allegro .brd
layout of the same board. Produces a canonical net table linking each schematic
net to a layout geometric net (and a representative layout coordinate), so the
two viewers can highlight the same net in both directions.

Matching: (1) exact net-name match via the .brd 0x04->0x1b net records, then
(2) fuzzy match of the remaining nets by the set of component refdes each net
touches (Jaccard), which covers auto-named N$ signal nets that don't share a
name between Capture and Allegro.
"""
import struct
from pathlib import Path

import brd_convert as bc
import orcad_viewer as ov

_SEGT = (0x15, 0x16, 0x17)


def _u(d, k):
    return struct.unpack_from("<I", d, k)[0]


# ---------------------------------------------------------------- layout side
def _layout_nets(brd_path, model):
    """Replicate the viewer's geometric net grouping (traces + vias, pads by
    containment) and attach a refdes set, net name, bbox and representative
    coordinate to each net. `model` is brd_viewer.build()'s model dict."""
    d = Path(brd_path).read_bytes()
    strings = bc.parse_strings(d)

    # objKey -> net name, from 0x04 net-assign (connItem@+16 -> net@+12 -> 0x1B.netName@+12)
    o1b = {}
    for k in range(0x1200, len(d) - 16, 4):
        if d[k] == 0x1B and 1 <= _u(d, k + 4) < 300000:
            o1b.setdefault(_u(d, k + 4), k)
    name_of = {}
    for k in range(0x1200, len(d) - 20, 4):
        if d[k] == 0x04:
            ob = o1b.get(_u(d, k + 12))
            if ob:
                nm = strings.get(_u(d, ob + 12), "")
                if nm and all(32 <= ord(c) < 127 for c in nm):
                    name_of[_u(d, k + 16)] = nm

    # track key -> net name; then each segment (by parent track) inherits it
    track_name = {}
    for k in range(0x1200, len(d) - 70, 4):
        if d[k] == 0x05 and _u(d, k + 4) in name_of:
            track_name[_u(d, k + 4)] = name_of[_u(d, k + 4)]

    NQ = 250
    def nk(x, y):
        return (round(x / NQ), round(y / NQ))

    par = {}
    def find(a):
        par.setdefault(a, a)
        r = a
        while par[r] != r:
            r = par[r]
        while par[a] != r:
            par[a], a = r, par[a]
        return r
    def uni(a, b):
        par[find(a)] = find(b)

    C = model["copper"]
    layers = model["layers"]
    L0 = layers[0]
    # union trace endpoints (same layer)
    for i in range(0, len(C), 6):
        L = C[i + 4]
        uni((nk(C[i], C[i + 1]), L), (nk(C[i + 2], C[i + 3]), L))
    # vias bridge layers
    V = model["vias"]
    for i in range(0, len(V), 3):
        p = nk(V[i], V[i + 1])
        for L in layers:
            uni((p, L0), (p, L))

    # coarse endpoint hash for pad containment
    CG = 8000
    eh = {}
    for i in range(0, len(C), 6):
        for (x, y, L) in ((C[i], C[i + 1], C[i + 4]), (C[i + 2], C[i + 3], C[i + 4])):
            eh.setdefault((x // CG, y // CG), []).append((x, y, L))

    nets = {}   # root -> {refdes:set, name:{}, bbox:[..], rep:(x,y,layer)}
    def rec(root):
        return nets.setdefault(root, {"refdes": set(), "name": {}, "bbox": None, "rep": None})

    def grow(b, x, y):
        if b is None:
            return [x, y, x, y]
        return [min(b[0], x), min(b[1], y), max(b[2], x), max(b[3], y)]

    # name each net by the track names of its segments (via model copper + .brd tracks)
    # build a coordinate->name map from named tracks' segments
    seg_named = []
    for k in range(0x1200, len(d) - 44, 4):
        if d[k] in _SEGT:
            nm = track_name.get(_u(d, k + 12))
            if nm:
                sx, sy = struct.unpack_from("<ii", d, k + 28)
                seg_named.append((sx, sy, d[k], nm))
    # assign via nearest model segment endpoint (they coincide); use node key
    named_node = {}
    for (sx, sy, _t, nm) in seg_named:
        # layer unknown here; try all layers at this node
        for L in layers:
            named_node.setdefault((nk(sx, sy), L), {})
            named_node[(nk(sx, sy), L)][nm] = named_node[(nk(sx, sy), L)].get(nm, 0) + 1

    for i in range(0, len(C), 6):
        r = find((nk(C[i], C[i + 1]), C[i + 4]))
        e = rec(r)
        e["bbox"] = grow(e["bbox"], C[i], C[i + 1])
        e["bbox"] = grow(e["bbox"], C[i + 2], C[i + 3])
        if e["rep"] is None:
            e["rep"] = (C[i], C[i + 1], C[i + 4])
        nn = named_node.get((nk(C[i], C[i + 1]), C[i + 4]))
        if nn:
            for k2, v2 in nn.items():
                e["name"][k2] = e["name"].get(k2, 0) + v2

    # pads -> refdes onto the nets whose endpoints they contain
    for pt in model["parts"]:
        ref = pt["ref"]
        for pd in pt["pads"]:
            roots = set()
            for bx in range(pd[0] // CG, pd[2] // CG + 1):
                for by in range(pd[1] // CG, pd[3] // CG + 1):
                    for (x, y, L) in eh.get((bx, by), []):
                        if pd[0] <= x <= pd[2] and pd[1] <= y <= pd[3]:
                            roots.add(find((nk(x, y), L)))
            for r in roots:
                rec(r)["refdes"].add(ref)

    out = []
    for r, e in nets.items():
        name = max(e["name"], key=e["name"].get) if e["name"] else None
        out.append({"refdes": e["refdes"], "name": name, "bbox": e["bbox"], "rep": e["rep"]})
    return out


# ------------------------------------------------------------- schematic side
def _schematic_nets(dsn_path):
    m = ov.build_model(str(dsn_path))
    nets = {}   # net_id -> {name, refdes:set, pins:set}
    for sh in m["sheets"]:
        names = sh.get("nets", {})
        for p in sh["parts"]:
            for pin in p["pins"]:
                nid = pin[2]
                if not nid:
                    continue
                e = nets.setdefault(nid, {"name": names.get(nid) or None, "refdes": set()})
                e["refdes"].add(p["des"])
    return nets


def build_correspondence(dsn_path, brd_path, brd_model):
    """Assign each layout geometric net (a fragment — a net splits at pad
    junctions since we don't bridge through pads) to its single best schematic
    net: exact name if it has one, else the schematic net with the highest
    refdes-set Jaccard (>=0.6). Then group fragments by schematic net so one
    schematic net maps to all its layout pieces."""
    lay = _layout_nets(brd_path, brd_model)
    sch = _schematic_nets(dsn_path)
    sch_by_name = {}
    for nid, sn in sch.items():
        if sn["name"]:
            sch_by_name.setdefault(sn["name"], nid)

    assign = {}   # sch net_id -> list of layout fragments, + how matched
    for ln in lay:
        nid, how = None, None
        if ln["name"] and ln["name"] in sch_by_name:
            nid, how = sch_by_name[ln["name"]], "name"
        elif len(ln["refdes"]) >= 2:
            best, bj = None, 0.0
            for cid, sn in sch.items():
                if len(sn["refdes"]) < 2:
                    continue
                inter = len(sn["refdes"] & ln["refdes"])
                if not inter:
                    continue
                j = inter / len(sn["refdes"] | ln["refdes"])
                if j > bj:
                    bj, best = j, cid
            if best and bj >= 0.6:
                nid, how = best, f"fuzzy{bj:.2f}"
        if nid:
            assign.setdefault(nid, []).append((ln, how))

    xnets = []
    for nid, frags in assign.items():
        sn = sch[nid]
        reps = [f[0]["rep"] for f in frags if f[0]["rep"]]
        bb = None
        for f in frags:
            b = f[0]["bbox"]
            if not b:
                continue
            bb = b if bb is None else [min(bb[0], b[0]), min(bb[1], b[1]), max(bb[2], b[2]), max(bb[3], b[3])]
        exact = any(f[1] == "name" for f in frags)
        xnets.append({"name": sn["name"] or nid.split(":")[0], "sch": nid,
                      "reps": reps, "bbox": bb, "exact": exact, "frags": len(frags)})
    return xnets, len(sch), len(lay)


def generate_linked(dsn_path, brd_path, out_dir, bom_path=None,
                    sch_name="orcad_schematic.html", pcb_name="pcb.html"):
    """Generate both viewers wired for cross-probing. The layout model is built
    once and shared with the correspondence so the embedded xnet coordinates
    match the rendered geometry exactly."""
    import brd_viewer, orcad_viewer
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    model = brd_viewer.build(brd_path, bom_path)
    xnets, ns, nl = build_correspondence(dsn_path, brd_path, model)
    payload = [{"name": x["name"], "sch": x["sch"], "reps": x["reps"], "bbox": x["bbox"]}
               for x in xnets]
    brd_viewer.generate(brd_path, out / pcb_name, bom_path,
                        xprobe={"xnets": payload, "companion": sch_name}, model=model)
    orcad_viewer.generate(dsn_path, out / sch_name,
                          xprobe={"xnets": payload, "companion": pcb_name})
    ex = sum(1 for x in xnets if x["exact"])
    print(f"linked {len(payload)} nets ({ex} exact-name, {len(payload) - ex} fuzzy) "
          f"of {ns} schematic / {nl} layout nets")
    print(f"  → {out / sch_name}\n  → {out / pcb_name}")


if __name__ == "__main__":
    import sys
    a = sys.argv[1:]
    if a and a[0] == "--build":       # --build DSN BRD OUTDIR [BOM]
        generate_linked(a[1], a[2], a[3], a[4] if len(a) > 4 else None)
    else:                              # DSN BRD [BOM]  → just report match quality
        import brd_viewer
        model = brd_viewer.build(a[1], a[2] if len(a) > 2 else None)
        xn, ns, nl = build_correspondence(a[0], a[1], model)
        ex = [x for x in xn if x["exact"]]
        print(f"schematic nets: {ns} | layout nets: {nl}")
        print(f"linked: {len(xn)}  (exact-name {len(ex)}, fuzzy {len(xn) - len(ex)})")
