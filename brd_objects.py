"""Validated .brd object graph (A_172-family layouts).

brd_convert.py's heuristic scans identify blocks by a lone type byte, which is
reliable only when a strong structural filter follows (as for ETCH copper). This
module builds a cross-validated object graph using the block layouts
reverse-engineered by github.com/bernayigit/brd_parser (types.h, A_172):

    x1B net           t(u32) k@4 next@8 net_name(strid)@12
    x04 net-member    t(u32) k@4 next@8 ptr1(->x1B)@12 ptr2(->member)@16
    x05 track         t(u16) class@2 layer@3 k@4 ptr1(->x04)@12 firstSeg@56
    x15/16/17 line    t(u32) k@4 next@8 parent@12 width@24 coords[4]@28
    x01 arc           t(u16) k@4 next@8 parent@12 width@24 coords[4]@28
                      cx(fp)@44 cy(fp)@52 r(fp)@60 bbox@68   (fp = word-swapped double)
    x28 shape         t(u16) class@2 layer@3 k@4 ptr1(->x04)@12 firstSeg@40 bbox@60
    x32 pad           t(u16) sub@2  layer@3 k@4 ptr1(->x04)@12 fp@28 coords@68
    x33 via           t(u16) sub@2  layer@3 k@4 ptr1(->x04)@12 x,y@32

Cross-links prune false positives: an x04 must point at a real x1B, members'
ptr1 must land in x04s, segment parents must be known tracks/shapes, and arcs
must be geometrically self-consistent (|dist(center, endpoint) - r| ≈ 0).

Layer classes seen here: 6 = ETCH (copper); 0x15 (21) = route keepin / board
shape; the rest are small mechanical/drawing classes.
"""

import math
import struct

_u16 = lambda d, k: struct.unpack_from("<H", d, k)[0]
_u32 = lambda d, k: struct.unpack_from("<I", d, k)[0]
_i32 = lambda d, k: struct.unpack_from("<i", d, k)[0]

ETCH = 0x06
KEEPIN = 0x15
_SANE = 3_000_000


def _cfp(d, k):
    """Swapped-word 8-byte float: the two 32-bit words are swapped vs IEEE double."""
    lo, hi = struct.unpack_from("<II", d, k)
    return struct.unpack("<d", struct.pack("<II", hi, lo))[0]


def _plausible_key(key):
    return 1 <= key < 0x2000000


def _printable_name(s):
    return (isinstance(s, str) and 0 < len(s) < 100
            and all(32 <= ord(c) < 127 for c in s))


def _sane_pt(*vals):
    return all(-_SANE < v < _SANE for v in vals)


def parse_graph(d, strings):
    """Build the validated object graph. Returns a dict of:
    nets    x1Bkey -> name
    x04s    x04key -> netname
    tracks  x05key -> (cls, sub, netname|None)
    shapes  [{key, cls, sub, net, first, bbox, off}]
    segsL   segkey -> (offset, parentkey)          x15/16/17 lines
    segsA   segkey -> (offset, parentkey, geom)    validated x01 arcs;
            geom = (w, sx, sy, ex, ey, cx, cy, r)
    pads    [(key, x1, y1, x2, y2, net|None, fpkey, sub, layer)]
    vias    [(x, y, net)]
    """
    n = len(d)

    # Keys collide across false-positive blocks; a later real x1B must beat an
    # earlier junk hit at the same key, so keep the best candidate per key
    # (a printable resolved name wins).
    nets = {}
    for k in range(0x1200, n - 60, 4):
        if d[k] == 0x1B and d[k + 1] == 0 and d[k + 2] == 0 and d[k + 3] == 0:
            key = _u32(d, k + 4)
            name = strings.get(_u32(d, k + 12))
            if _plausible_key(key) and _printable_name(name) and key not in nets:
                nets[key] = name

    # Anonymous nets (no printable name) still group copper exactly; give them a
    # stable synthetic name. Only linked-list-consistent candidates qualify.
    anon_cand = {}
    for k in range(0x1200, n - 60, 4):
        if d[k] == 0x1B and d[k + 1] == 0 and d[k + 2] == 0 and d[k + 3] == 0:
            key = _u32(d, k + 4)
            if _plausible_key(key) and key not in nets:
                anon_cand.setdefault(key, _u32(d, k + 8))
    for key, nxt in anon_cand.items():
        if nxt == 0 or nxt in anon_cand or nxt in nets:
            nets.setdefault(key, "$N%d" % key)

    x04s = {}
    for k in range(0x1200, n - 20, 4):
        if d[k] == 0x04 and d[k + 1] == 0 and d[k + 2] == 0 and d[k + 3] == 0:
            key, netk = _u32(d, k + 4), _u32(d, k + 12)
            if _plausible_key(key) and netk in nets:
                x04s.setdefault(key, nets[netk])

    tracks = {}
    for k in range(0x1200, n - 60, 4):
        if d[k] == 0x05 and d[k + 1] == 0:
            key = _u32(d, k + 4)
            cls, sub = d[k + 2], d[k + 3]
            if not (_plausible_key(key) and cls <= 0x18 and sub <= 0x30):
                continue
            net = x04s.get(_u32(d, k + 12))
            # net-linked candidate beats a junk hit that stole the key earlier
            if key not in tracks or (net and not tracks[key][2]):
                tracks[key] = (cls, sub, net)

    shape_by_key = {}
    for k in range(0x1200, n - 76, 4):
        if d[k] == 0x28 and d[k + 1] == 0:
            key = _u32(d, k + 4)
            cls, sub = d[k + 2], d[k + 3]
            if not _plausible_key(key) or cls > 0x18 or sub > 0x30:
                continue
            bbox = struct.unpack_from("<iiii", d, k + 60)
            if not _sane_pt(*bbox):
                continue
            cand = {"key": key, "cls": cls, "sub": sub,
                    "net": x04s.get(_u32(d, k + 12)),
                    "first": _u32(d, k + 40), "bbox": list(bbox), "off": k}
            prev = shape_by_key.get(key)
            if prev is None or (cand["net"] and not prev["net"]):
                shape_by_key[key] = cand
    shapes = list(shape_by_key.values())
    shape_keys = set(shape_by_key)

    parent_ok = lambda pk: pk in tracks or pk in shape_keys
    segsL, segsA = {}, {}
    for k in range(0x1200, n - 84, 4):
        t = d[k]
        if t in (0x15, 0x16, 0x17) and d[k + 1] == 0 and d[k + 2] == 0 and d[k + 3] == 0:
            key, pk = _u32(d, k + 4), _u32(d, k + 12)
            if _plausible_key(key) and parent_ok(pk):
                segsL.setdefault(key, (k, pk))
        elif t == 0x01 and d[k + 1] == 0:
            key, pk = _u32(d, k + 4), _u32(d, k + 12)
            if not (_plausible_key(key) and parent_ok(pk)):
                continue
            geom = _arc_geom(d, k)
            if geom:
                segsA.setdefault(key, (k, pk, geom))

    pads, vias = [], []
    for k in range(0x1200, n - 84, 4):
        t = d[k]
        if t == 0x32 and d[k + 1] == 0:
            key = _u32(d, k + 4)
            if not _plausible_key(key):
                continue
            box = struct.unpack_from("<iiii", d, k + 68)
            if _sane_pt(*box):
                pads.append((key, *box, x04s.get(_u32(d, k + 12)),
                             _u32(d, k + 28), d[k + 2], d[k + 3]))
        elif t == 0x33 and d[k + 1] == 0:
            x, y = _i32(d, k + 32), _i32(d, k + 36)
            net = x04s.get(_u32(d, k + 12))
            if net is not None and _sane_pt(x, y):
                vias.append((x, y, net))

    return {"nets": nets, "x04s": x04s, "tracks": tracks, "shapes": shapes,
            "segsL": segsL, "segsA": segsA, "pads": pads, "vias": vias}


def _arc_geom(d, k):
    """Validated arc geometry at block offset k, or None. The center/radius must
    actually describe a circle through both endpoints — this one check removes
    essentially every false positive the `01 00` byte pattern produces."""
    w = _u32(d, k + 24)
    sx, sy, ex, ey = struct.unpack_from("<iiii", d, k + 28)
    if not _sane_pt(sx, sy, ex, ey) or w > 500000:
        return None
    try:
        cx, cy, r = _cfp(d, k + 44), _cfp(d, k + 52), _cfp(d, k + 60)
    except struct.error:
        return None
    if not (math.isfinite(cx) and math.isfinite(cy) and math.isfinite(r)):
        return None
    if not (200 <= r < _SANE) or not _sane_pt(cx, cy):
        return None
    tol = r * 0.02 + 60
    if abs(math.hypot(sx - cx, sy - cy) - r) > tol:
        return None
    if abs(math.hypot(ex - cx, ey - cy) - r) > tol:
        return None
    return (w, sx, sy, ex, ey, cx, cy, r)


def seg_line(d, off):
    """(width, sx, sy, ex, ey) for a line-segment block."""
    return (_u32(d, off + 24), *struct.unpack_from("<iiii", d, off + 28))


def shape_boundary(d, graph, shape, max_pts=6000):
    """Walk a shape's boundary chain (firstSeg → next@+8), collecting polygon
    points. Arcs are flattened with a few samples so curves keep their bulge."""
    segsL, segsA = graph["segsL"], graph["segsA"]
    pts, key, seen = [], shape["first"], set()
    while _plausible_key(key) and key not in seen and len(pts) < max_pts:
        seen.add(key)
        if key in segsL:
            off, _ = segsL[key]
            _, sx, sy, ex, ey = seg_line(d, off)
            if not pts or pts[-1] != (sx, sy):
                pts.append((sx, sy))
            pts.append((ex, ey))
            key = _u32(d, off + 8)
        elif key in segsA:
            off, _, (w, sx, sy, ex, ey, cx, cy, r) = segsA[key]
            if not pts or pts[-1] != (sx, sy):
                pts.append((sx, sy))
            a0 = math.atan2(sy - cy, sx - cx)
            a1 = math.atan2(ey - cy, ex - cx)
            # sample the short way around; boundary arcs are corner rounds
            if a1 < a0:
                a1 += 2 * math.pi
            if a1 - a0 > math.pi:
                a0, a1 = a1, a0 + 2 * math.pi
            steps = max(2, min(8, int(r / 20000) + 2))
            for i in range(1, steps):
                a = a0 + (a1 - a0) * i / steps
                pts.append((int(cx + r * math.cos(a)), int(cy + r * math.sin(a))))
            pts.append((ex, ey))
            key = _u32(d, off + 8)
        else:
            break
    return pts
