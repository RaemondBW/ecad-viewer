#!/usr/bin/env python3
"""
brd_convert.py — a (partial) reader for .brd board files.

Status: reverse-engineering in progress. What is decoded and reliable:

  * File header — magic/version, the object-type count table, the version
    string. See parse_header().
  * The reference-designator string table (refdes -> object id). See
    refdes_ids().
  * The component-instance records: fixed 48-byte (12x u32) rows whose
    field[0] is the refdes id. Field map (validated against a BOM on
    HSD_FPGA_final.brd):
        [-1] object id
        [0]  refdes id             (-> refdes string via the string table)
        [1]  component-instance id
        [2]  PLACEMENT X            <- board coordinate (confirmed)
        [3]  == 0
        [4]  PLACEMENT Y            <- board coordinate (confirmed)
        [5]  == 7 (record/type tag)
        [6],[7]  object-graph pointers
        [8]  device / part-instance pointer
        [11] per-instance pointer
    Coordinates are integers (~6k..97k here); the design extent maps to roughly a
    9" x 8.6" board (unit ~ 0.1 mil). component_placements() extracts
    refdes -> (x, y): on HSD_FPGA_final.brd it yields 264 components, ALL of
    which cross-validate against the BOM.

TODO: raise placement coverage past 264/~317 (some records use a variant
layout), recover rotation/mirror, resolve field[8] -> device name/part number,
and extract the board outline + footprint/pad geometry for a fuller PCB view.
Note: designator->value already comes cheaply from the BOM export.
"""

import re
import struct
import sys
from pathlib import Path

_REF_RE = re.compile(rb"([RCLDQUJXY]\d{1,4})\x00")
_STR_RE = re.compile(rb"[ -~][ -~/.\\]{1,60}\x00")


def parse_header(d):
    """Decode the fixed header + object-type count table + version string."""
    u = struct.unpack_from("<32I", d, 0)
    hdr = {
        "magic": u[0],                 # 0x00160100 family = 16.x
        "file_size": u[8],             # matches len(d)
        "size_ok": u[8] == len(d),
    }
    # type table: (index, count) pairs starting at u32[24], until a non-pair run
    types = {}
    o = 24 * 4
    while o + 8 <= len(d):
        idx, cnt = struct.unpack_from("<II", d, o)
        # stop when the "index" stops being a small increasing tag, or the
        # "count" is implausibly large (the version string read as a u32)
        if not (0 < idx < 4096) or (types and idx <= max(types)) or cnt > len(d):
            break
        types[idx] = cnt
        o += 8
        if len(types) > 200:
            break
    hdr["type_counts"] = types
    # ASCII version/date string follows the table
    m = _STR_RE.search(d, o)
    hdr["version_string"] = m.group().rstrip(b"\x00").decode("latin1") if m else ""
    return hdr


def refdes_ids(d, lo=0, hi=None):
    """Map reference designator -> object id (u32 immediately preceding the
    \\0-terminated refdes string). Restrict to [lo,hi) to target the string
    table and avoid collisions elsewhere."""
    hi = hi if hi is not None else len(d)
    out = {}
    for m in _REF_RE.finditer(d):
        o = m.start()
        if lo <= o < hi and o >= 4:
            out.setdefault(m.group(1).decode(), struct.unpack_from("<I", d, o - 4)[0])
    return out


def id_strings(d):
    """Map object id -> string, for every id that directly precedes a string."""
    out = {}
    for m in _STR_RE.finditer(d):
        o = m.start()
        if o >= 4:
            out.setdefault(struct.unpack_from("<I", d, o - 4)[0],
                           m.group().rstrip(b"\x00").decode("latin1"))
    return out


def component_records(d, refid, lo, hi):
    """Yield {refdes, fields[12]} for each 48-byte component row in [lo,hi)
    whose field[0] is a known refdes id."""
    id2ref = {v: k for k, v in refid.items()}
    seen = set()
    for k in range(lo, hi - 4, 4):
        v = struct.unpack_from("<I", d, k)[0]
        if v in id2ref and id2ref[v] not in seen:
            seen.add(id2ref[v])
            yield {"refdes": id2ref[v],
                   "fields": list(struct.unpack_from("<12I", d, k))}


def id_to_refdes(d):
    """id -> refdes for every id that directly precedes a refdes string."""
    out = {}
    for m in _REF_RE.finditer(d):
        o = m.start()
        if o >= 4:
            out.setdefault(struct.unpack_from("<I", d, o - 4)[0], m.group(1).decode())
    return out


_REFDES_OK = "URCLDQJXYTFKPWVAS"


def parse_strings(d):
    """Parse the string table at 0x1200: repeated [u32 id][NUL string][word pad].

    The walk RESYNCS on junk: a non-printable "string" means we lost phase with
    the table (15.x files interleave non-string records), and without recovery
    every later entry — including all refdes — is garbage, which cascades into
    zero components and an empty viewer. On junk, step one word and retry."""
    strings = {}
    p = 0x1200
    n = len(d)
    while p < n - 4 and p < 3_000_000:
        sid = _u32(d, p)
        e = p + 4
        while e < n and d[e] != 0:
            e += 1
        s = d[p + 4:e]
        if len(s) < 256 and all(32 <= b < 127 for b in s):
            if s:
                strings.setdefault(sid, s.decode("latin1"))
            p = (e + 1 + 3) & ~3      # empty strings are valid records — keep phase
        else:
            p += 4          # junk / lost phase — resync at the next word
    return strings


def _index(d, types):
    """key -> (type, offset) for the given block types (structural, by first byte)."""
    k2o = {}
    for k in range(0x1200, len(d) - 90, 4):
        t = d[k]
        if t in types and 1 <= _u32(d, k + 4) < 300000:
            k2o.setdefault(_u32(d, k + 4), (t, k))
    return k2o


# padstack pad-primitive type byte -> shape
_SHAPES = {2: "round", 6: "rect", 12: "oblong", 22: "oblong", 23: "poly"}


def _resolve_padstack(d, k2o, pkey):
    """0x1C padstack → (shape, w, h, layerCount) for the real copper pad. SMD
    (layerCount<=1) uses components[0]; through-hole uses the first non-clearance
    shape component. Components start at padstack+224 (V180 layout), stride 36:
    type@+0, w@+8, h@+12. Dimensions are in board coordinate units."""
    ent = k2o.get(pkey)
    if not ent or ent[0] != 0x1C:
        return None
    pk = ent[1]
    layers = struct.unpack_from("<H", d, pk + 44)[0]
    if not (0 <= layers <= 60):                # garbage / misaligned padstack
        return None
    ncomps = 21 + layers * 4

    def comp(i):
        c = pk + 224 + i * 36
        return d[c], struct.unpack_from("<i", d, c + 8)[0], struct.unpack_from("<i", d, c + 12)[0]

    if layers <= 1:
        t, w, h = comp(0)
    else:
        t = w = h = 0
        for i in range(1, min(ncomps, 80)):
            tt, ww, hh = comp(i)
            if tt not in (0, 23) and 0 < ww < 200000 and 0 < hh < 200000:
                t, w, h = tt, ww, hh
                break
    if t == 0 or not (0 < w < 200000 and 0 < h < 200000):
        return None
    return (_SHAPES.get(t, "rect"), w, h, layers)


def component_placements(d, strings=None, scale=1):
    """Extract every placed component as {refdes: (x, y, side, rot_mdeg, pads)}.

    scale: unit multiplier applied to every coordinate as it is read (before the
    plausibility filters). Boards saved in mils (coords ~100s-1000s) pass scale=100
    to normalize to the 0.1-mil units all the absolute thresholds assume.

    Components are 0x2D footprint instances: coordX@+32, coordY@+36, rotation
    (millidegrees)@+28, side(0=top,1=bottom)@+2, instRef@+40 → 0x07 whose
    refDesStrPtr@+28 keys the string table. Its footprint is its pad chain:
    firstPadPtr@+48 → 0x32 placed pads (bbox coords@+68 = x1,y1,x2,y2 in board
    units), linked by next@+8. Validating via the resolvable refdes drops
    false-positive blocks. Coords are raw board units (y down)."""
    if strings is None:
        strings = parse_strings(d)
    k2o = _index(d, (0x0D, 0x1C))              # only for padstack resolution
    # Reliable 0x07 instance index — scan ONLY 0x07 blocks. The shared index keeps
    # one block per key, and a real 0x07's key can be stolen by a false block of
    # another type, which drops the whole component (e.g. U602 was lost this way).
    idx07 = {}
    for k in range(0x1200, len(d) - 32, 4):
        if d[k] == 0x07 and 1 <= _u32(d, k + 4) < 300000:
            idx07.setdefault(_u32(d, k + 4), k)
    # validated components — scan 0x2D blocks directly, resolve refdes via idx07
    comps = {}                     # 0x2D key -> (refdes, cx, cy, side, rot)
    for k in range(0x1200, len(d) - 90, 4):
        if d[k] != 0x2D:
            continue
        key = _u32(d, k + 4)
        if not (1 <= key < 300000):
            continue
        cx, cy = struct.unpack_from("<ii", d, k + 32)
        cx *= scale; cy *= scale
        if cx < 15000 or cy < 15000:              # off-board / origin false positives
            continue
        o7 = idx07.get(_u32(d, k + 40))
        if o7 is None:
            continue
        ref = strings.get(_u32(d, o7 + 28), "")
        if ref and ref[0] in _REFDES_OK and any(c.isdigit() for c in ref):
            comps[key] = (ref, cx, cy, d[k + 2], _u32(d, k + 28))
    # assign every 0x32 pad to its footprint via parentFp@+28. Scan 0x32 blocks
    # DIRECTLY (not via the shared key index) — that index keeps one block per
    # key and a real pad's key can collide with another block type, silently
    # dropping pads (e.g. C614 lost a pad this way).
    padmap = {}
    for k in range(0x1200, len(d) - 90, 4):
        if d[k] != 0x32:
            continue
        par = _u32(d, k + 28)
        if par not in comps:
            continue
        x1, y1, x2, y2 = struct.unpack_from("<iiii", d, k + 68)
        x1 *= scale; y1 *= scale; x2 *= scale; y2 *= scale
        px, py = (x1 + x2) / 2, (y1 + y2) / 2
        if not (all(-3_000_000 < v < 3_000_000 for v in (x1, y1, x2, y2)) and px > 12000 and py > 12000):
            continue
        # true-to-scale pad: SMD placed-bbox IS the real rotated pad AABB; for THT
        # the bbox is the clearance keep-out, so read the real copper pad w/h from
        # the 0x1C padstack (0x32→0x0D→0x1C).
        shape = "rect"
        ps = None
        pg = k2o.get(_u32(d, k + 36))
        if pg and pg[0] == 0x0D:
            ps = _resolve_padstack(d, k2o, _u32(d, pg[1] + 28))
        if ps and ps[3] > 1:                       # through-hole
            shape, w, h, _ = ps
            w *= scale; h *= scale
            x1, y1, x2, y2 = int(px - w / 2), int(py - h / 2), int(px + w / 2), int(py + h / 2)
        elif ps:
            shape = ps[0]
        rnd = 1 if shape in ("round", "oblong") else 0
        bx = _clamp_box(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2), 50000)
        padmap.setdefault(par, set()).add((bx[0], bx[1], bx[2], bx[3], rnd))
    padmap = {ck: list(s) for ck, s in padmap.items()}
    out = {}
    for ck, (ref, cx, cy, side, rot) in comps.items():
        out.setdefault(ref, (cx, cy, side, rot, padmap.get(ck, [])))
    return out


def _clamp_box(x1, y1, x2, y2, maxdim):
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    hw = min(abs(x2 - x1), maxdim) / 2
    hh = min(abs(y2 - y1), maxdim) / 2
    return (int(cx - hw), int(cy - hh), int(cx + hw), int(cy + hh))


def copper_shapes(d):
    """Extract copper pour / power-ground plane outlines from ETCH 0x28 shapes.
    0x28: layer class@+2, subclass@+3, firstSegmentPtr@+40 → a chain of
    0x15/0x16/0x17 boundary segments (via next@+8) whose start points trace the
    polygon; coords@+60 is the bbox (rectangle fallback). Returns
    [(layer, [(x, y), ...])] — the outer boundary, in board units (y down)."""
    # boundary edges are 0x15/16/17 lines AND 0x01 arcs — all share the same
    # key@+4, next@+8, startX@+28..endY@+40 layout (arcs linearised start→end)
    edge = _SEGT + (0x01,)
    k2o = _index(d, (0x28,) + edge)
    out = []
    for key, (t, k) in k2o.items():
        if t != 0x28 or d[k + 2] != _ETCH:
            continue
        layer = d[k + 3]
        seg, seen, pts, bad = _u32(d, k + 40), set(), [], False
        while seg and seg in k2o and seg not in seen and len(pts) < 60000:
            seen.add(seg)
            so = k2o[seg][1]
            if d[so] not in edge:
                break
            sx, sy, ex, ey = struct.unpack_from("<iiii", d, so + 28)
            if not all(-3_000_000 < v < 3_000_000 for v in (sx, sy, ex, ey)):
                bad = True                     # chain ran into garbage → distrust it
                break
            if not pts:
                pts.append((sx, sy))
            pts.append((ex, ey))
            seg = _u32(d, so + 8)
        # only keep a cleanly-traced, closed-ish boundary; a chain that ran into
        # garbage isn't trustworthy, and a bbox rectangle would fill the board
        # with a wrong shape — better to omit it than draw a wrong plane
        if not bad and len(pts) >= 4:
            out.append((layer, pts))
    return out


def vias(d, bounds=None):
    """Extract plated vias as [(x, y, radius)] from 0x33 blocks (coordsX@+32,
    coordsY@+36, padstack@+44). The via's true-to-scale pad size is read from the
    0x1C padstack; vias whose padstack won't resolve are dropped as false
    positives. `bounds` clips to the board."""
    k2o = _index(d, (0x33, 0x1C))
    out = []
    for key, (t, k) in k2o.items():
        if t != 0x33:
            continue
        # require a real (sane) padstack to filter false-positive 0x33 hits
        if not _resolve_padstack(d, k2o, _u32(d, k + 44)):
            continue
        # bbox@+64 is the clearance keep-out frame; the finished via pad is a
        # fraction of it (via padstacks don't resolve cleanly on this file)
        bx1, by1, bx2, by2 = struct.unpack_from("<iiii", d, k + 64)
        r = int(max(abs(bx2 - bx1), abs(by2 - by1)) * 0.28)
        if not (200 < r < 8000):
            continue
        x, y = struct.unpack_from("<ii", d, k + 32)
        if bounds and not (bounds[0] <= x <= bounds[2] and bounds[1] <= y <= bounds[3]):
            continue
        if -3_000_000 < x < 3_000_000 and -3_000_000 < y < 3_000_000:
            out.append((x, y, r))
    return out


# Block layouts (16.x/>=V172, cross-checked against the KiCad importer).
# Every block: main loop consumes the
# 1-byte type tag, then the record follows. All blocks carry `key` at +4.
#   0x05 TRACK  : layer@+2(class),+3(subclass); firstSegPtr@+56; key@+4
#   0x15/16/17  : (line seg) next@+8; width@+24; startX@+28,startY@+32,endX@+36,endY@+40
#   ETCH layer class = 0x06 (subclass = copper layer index, 0=top)
_ETCH = 0x06
_SEGT = (0x15, 0x16, 0x17)


def _u32(d, k):
    return struct.unpack_from("<I", d, k)[0] if 0 <= k <= len(d) - 4 else 0


def index_tracks_and_segments(d):
    """Build key -> file-offset for 0x05 track blocks and 0x15/0x16/0x17 line
    segments, identifying blocks structurally (self-consistent coords/width/
    layer) so we don't need a perfect contiguous walk of the whole pool."""
    k2o = {}
    n = len(d)
    for k in range(0x1200, n - 88, 4):
        t = d[k]
        key = _u32(d, k + 4)
        if not (1 <= key < 300000):
            continue
        if t == 0x05:
            if 1 <= d[k + 2] <= 0x18:            # plausible layer class
                k2o.setdefault(key, k)
        elif t in _SEGT:
            sx, sy, ex, ey = struct.unpack_from("<iiii", d, k + 28)
            w = _u32(d, k + 24)
            if 0 < w < 500000 and all(-2_000_000 < v < 2_000_000 for v in (sx, sy, ex, ey)):
                k2o.setdefault(key, k)
    return k2o


def copper_segments(d, k2o=None):
    """Extract copper trace segments by their PARENT track, not by walking each
    track's firstSegPtr chain — the chain silently breaks on key collisions and
    drops the tail of a trace. Instead: index 0x05 tracks (key → ETCH layer), then
    scan every 0x15/0x16/0x17 line segment and keep the ones whose parent@+12 is
    an ETCH track, tagged with that track's copper layer. Segment: next@+8,
    parent@+12, width@+24, startX@+28,startY@+32,endX@+36,endY@+40.
    Returns [(x1, y1, x2, y2, layer, width)] in raw board units (y down)."""
    track = {}                                    # 0x05 key -> (classCode, subclass)
    for k in range(0x1200, len(d) - 70, 4):
        if d[k] == 0x05 and 1 <= _u32(d, k + 4) < 300000:
            track.setdefault(_u32(d, k + 4), (d[k + 2], d[k + 3]))
    out = []
    for k in range(0x1200, len(d) - 44, 4):
        if d[k] not in _SEGT:
            continue
        tr = track.get(_u32(d, k + 12))           # parent track
        if not tr or tr[0] != _ETCH:              # copper only (skip silkscreen etc.)
            continue
        sx, sy, ex, ey = struct.unpack_from("<iiii", d, k + 28)
        w = _u32(d, k + 24)
        # drop corruption artifacts: implausible width, absurd length, zero-length,
        # or a garbage endpoint at the coordinate origin (board isn't at 0,0)
        if not (0 < w <= 15000) or (sx, sy) == (ex, ey):
            continue
        if not all(-3_000_000 < v < 3_000_000 for v in (sx, sy, ex, ey)):
            continue
        if max(abs(ex - sx), abs(ey - sy)) > 300000:
            continue
        if (abs(sx) < 4000 and abs(sy) < 4000) or (abs(ex) < 4000 and abs(ey) < 4000):
            continue
        out.append((sx, sy, ex, ey, tr[1], w))
    return out


def summary(path):
    d = Path(path).read_bytes()
    hdr = parse_header(d)
    print(f"{path}  ({len(d)} bytes)")
    print(f"  magic=0x{hdr['magic']:08x}  size_ok={hdr['size_ok']}  version={hdr['version_string']!r}")
    print(f"  object types: {len(hdr['type_counts'])}  "
          f"(largest counts: {sorted(hdr['type_counts'].values(), reverse=True)[:6]})")
    refs = refdes_ids(d, 42000, 47000)      # PS15 string-table window (sample)
    print(f"  refdes in sampled string table: {len(refs)}")
    comps = list(component_records(d, refs, 68000, 84000))
    print(f"  component rows resolved in sample region: {len(comps)}")
    for c in comps[:8]:
        print(f"    {c['refdes']:6} inst={c['fields'][1]} devPtr(f8)={c['fields'][8]}")


if __name__ == "__main__":
    summary(sys.argv[1])
