# Cadence Allegro `.brd` Board Format

A complete reference for writing an interpreter that recovers **nets, copper
(tracks + pours), vias, pads, component placements, and the board outline** from
a Cadence Allegro `.brd` layout, with **no Cadence tools required**.

> **Status / provenance.** The `.brd` format is proprietary, binary, and
> undocumented by Cadence. This reference was reverse-engineered and validated
> empirically against real boards (Allegro **16.x / ≥ V172**, "A_172" family),
> cross-checked with the block layouts from
> [bernayigit/brd_parser](https://github.com/bernayigit/brd_parser) (`types.h`)
> and BoardRipper's `ALLEGRO_BRD_FORMAT.md`. Offsets are for the A_172 layout;
> other versions shift some fields (notably padstack internals). **Identify blocks
> by signature + cross-links, not by a strict sequential walk** — the object pool
> is not cleanly walkable and key collisions between block types are common.

The reference implementation is split across `brd_convert.py` (header, strings,
placements, padstacks) and `brd_objects.py` (the validated object graph:
nets/tracks/segments/arcs/shapes/pads/vias). This document is the spec behind
both.

---

## 1. Container & overall shape

Unlike OrCAD's `.DSN`, a `.brd` is **not** an OLE2 file — it's a **flat binary**:
a fixed header, an object-type count table, a version string, then a large
**object pool** of variable records. You read the raw file bytes directly.

Two structural anchors make it tractable:

1. The **string table** begins at a fixed offset **`0x1200`**.
2. The **object pool** is scanned from `0x1200` onward on **4-byte alignment**,
   recognising each block by its 1-byte **type tag** and validating with
   **cross-links** between blocks.

---

## 2. Coordinate system, units, and numeric encodings

* Integer coordinates are **raw board units**, `int32`. **1 unit = 0.1 mil**
  (0.0001") → e.g. a 9"×8.6" board spans ~`0..97000`. **Y increases downward.**
  (A screen renderer that wants Y-up should negate Y.)
* **Cadence 8-byte float** (used for arc center/radius): it's an IEEE-754 double
  with its **two 32-bit words swapped**. Decode:

  ```
  lo, hi = read two u32 at k
  value  = reinterpret_double( pack(u32=hi, u32=lo) )
  ```

* All multi-byte integers are **little-endian**.

Helpers used throughout: `u16(k)`, `u32(k)`, `i32(k)`, `cfp(k)` (the word-swapped
double above).

---

## 3. Header

```
u32[0]   magic            0x00160100 family = Allegro 16.x
u32[8]   file_size        == len(file)         (sanity check)
u32[24…] object-type count table               (starts at byte 96)
         repeated (u32 index, u32 count) pairs, stopping when `index`
         stops being a small increasing tag (0 < idx < 4096, strictly
         increasing) or `count` becomes implausible (> file size).
<ascii>  version / date string, immediately after the table.
```

The count table tells you how many objects of each type the file contains — handy
for sanity checks and progress, not required for extraction.

---

## 4. String table (`0x1200`)

A run of length-implicit, NUL-terminated strings, each keyed by a preceding id:

```
p = 0x1200
loop:
    id  = u32(p);  p += 4
    str = bytes until NUL
    strings[id] = str
    p = align4(p + len(str) + 1)      # pad to the next 4-byte boundary
```

`strings[id]` resolves net names, reference designators, device names, etc.
Referenced by 32-bit **string pointers** in many blocks.

---

## 5. Object-pool block framing

Blocks live in the pool (from `0x1200`). The scanner walks `k = 0x1200, 0x1204,
…` and tests `byte[k]` against the type tags it wants. Conventions that hold for
essentially every block:

* `byte[k]` = **type tag**; for most types the next 1–3 bytes are `0x00`
  (a few types use `byte[k+2]`/`byte[k+3]` as **layer class / subclass** — see
  each record).
* `u32 @ k+4` = the block's **key** (its object id). Plausible keys are
  `1 ≤ key < 0x2000000`.
* `u32 @ k+8` = **next** pointer (for linked lists: segment chains, member
  chains).
* `u32 @ k+12` = a **parent / owner** pointer (segment→track, member→net, …).

Because a real block's key can collide with a stray byte pattern of another type,
**every extracted block is validated by its cross-links** (below), and when two
candidates claim one key, the one whose links resolve wins.

---

## 6. The net / connectivity object graph

Copper is grouped into nets through a small pointer graph:

```
segment/pad/shape/via  --parent@+12-->  x04 (net-member)  --@+12-->  x1B (net) --> name
track (x05)            --ptr1@+12----->  x04
```

Build it in dependency order: nets → members → tracks/shapes → segments →
pads/vias.

### 6.1 `0x1B` — Net

```
byte[k]=0x1B, byte[k+1..3]=0
u32 @ k+4    key
u32 @ k+8    next
u32 @ k+12   string-id  -> strings[…] = net name
```

Accept when the key is plausible and the resolved name is printable
(`0 < len < 100`, all `0x20..0x7e`). First good candidate per key wins.

**Anonymous nets** (no printable name) still group copper exactly — give them a
stable synthetic name (`$N<key>`), but only for linked-list-consistent candidates
(their `next` is 0, another anon candidate, or a named net) so junk `1B` byte
patterns don't create phantom nets.

### 6.2 `0x04` — Net-member

```
byte[k]=0x04, byte[k+1..3]=0
u32 @ k+4    key
u32 @ k+12   net-key   -> x1B      (k+16 = ptr to next member, per types.h)
```

Keep only members whose `net-key` points at a **real `0x1B`**. Result:
`x04[key] = net_name`. This is the join table every copper object uses to find its
net.

### 6.3 `0x05` — Track

A track owns a chain of copper line/arc segments.

```
byte[k]=0x05, byte[k+1]=0
byte[k+2]    layer class     (ETCH = 0x06 for copper)
byte[k+3]    subclass        (copper layer index; 0 = top)
u32 @ k+4    key
u32 @ k+12   ptr1  -> x04    (gives the net)
u32 @ k+56   firstSegPtr     (per types.h; prefer parent-scan, see below)
```

Result: `track[key] = (class, subclass, net_name)`. Validate `class ≤ 0x18`,
`subclass ≤ 0x30`. A net-linked candidate beats a junk hit that stole the key.

### 6.4 `0x15` / `0x16` / `0x17` — Line segment

```
byte[k] in {0x15,0x16,0x17}, byte[k+1..3]=0
u32 @ k+4    key
u32 @ k+8    next            (chain within a track/shape boundary)
u32 @ k+12   parent          -> track (x05) or shape (x28)
u32 @ k+24   width
i32 @ k+28   startX
i32 @ k+32   startY
i32 @ k+36   endX
i32 @ k+40   endY
```

Keep segments whose `parent` is a known track **or** shape. **Copper traces =
segments whose parent track has `class == ETCH (0x06)`**, tagged with that
track's `subclass` (layer). Extract by **parent**, not by walking a track's
`firstSegPtr` chain — the chain silently breaks on key collisions and drops the
tail of a trace.

Reject corruption: `0 < width ≤ 15000`, non-zero length, `|coord| < 3e6`, segment
length `≤ 300000`, and neither endpoint at the coordinate origin (the board isn't
at 0,0).

### 6.5 `0x01` — Arc segment

Same role as a line segment but curved (rounded corners, curved traces):

```
byte[k]=0x01, byte[k+1]=0
u32 @ k+4    key
u32 @ k+8    next
u32 @ k+12   parent
u32 @ k+24   width
i32 @ k+28   startX,  @+32 startY,  @+36 endX,  @+40 endY
cfp @ k+44   centerX  (word-swapped double)
cfp @ k+52   centerY
cfp @ k+60   radius
i32 @ k+68   bbox…
```

**Validation is essential** — the `01 00` byte pattern is common. Require the
geometry to describe a real circle through both endpoints:
`|dist(center, start) − r| ≤ r·0.02 + 60` **and** the same for the end point,
with `200 ≤ r < 3e6` and finite, sane center. This single check removes
essentially every false positive. Linearise start→end (sampling the short way)
for a polyline renderer.

### 6.6 `0x28` — Shape (copper pour / plane)

A filled region (ground plane, power pour). Its boundary is a segment chain.

```
byte[k]=0x28, byte[k+1]=0
byte[k+2]    layer class     (ETCH = 0x06 for a copper pour)
byte[k+3]    subclass        (layer)
u32 @ k+4    key
u32 @ k+12   ptr1  -> x04    (net)
u32 @ k+40   firstSegPtr     -> boundary chain of 0x15/16/17 (+0x01 arcs)
i32 @ k+60   bbox[4]         (x1,y1,x2,y2 — rectangle fallback)
```

**Boundary walk:** start at `firstSegPtr`, follow `next @ +8`, collecting each
segment's start/end points (flatten arcs with a few samples so curves keep their
bulge). Guard against cycles (`seen` set) and runaway length. Only keep a
**cleanly-traced** boundary (≥3–4 points, no run into garbage); a bbox rectangle
would fill the board with a wrong plane, so omit rather than guess. `KEEPIN`
(`class 0x15`) shapes trace the **board outline** the same way (§9).

### 6.7 `0x32` — Pad (placed footprint pad)

```
byte[k]=0x32, byte[k+1]=0
byte[k+2]    subclass
byte[k+3]    layer
u32 @ k+4    key
u32 @ k+12   ptr1  -> x04            (net; may be absent for NC pads)
u32 @ k+28   parent footprint (0x2D) / padstack link
u32 @ k+36   -> 0x0D device -> 0x1C padstack   (real copper pad size)
i32 @ k+68   bbox  x1,y1,x2,y2       (placed pad AABB, board units)
```

`bbox @ +68` is the placed pad's axis-aligned box. For **SMD** pads this *is* the
real rotated pad AABB. For **through-hole** the bbox is the clearance keep-out —
read the true copper pad `w×h` from the `0x1C` padstack (`0x32 → 0x0D → 0x1C`) and
rebuild the box around the pad center. Net comes from `ptr1 → x04`.

### 6.8 `0x33` — Via

```
byte[k]=0x33, byte[k+1]=0
u32 @ k+4    key
u32 @ k+12   ptr1  -> x04            (net)
i32 @ k+32   x
i32 @ k+36   y
u32 @ k+44   padstack (0x1C)         (for the finished size)
i32 @ k+64   bbox  (clearance keep-out; via pad ≈ 0.28× its max extent)
```

Keep vias with a resolvable net and sane coords; require a real `0x1C` padstack to
filter false-positive `0x33` hits. `(x, y, net)` (+ radius from the padstack or
`≈ 0.28 × max(bbox extent)`).

---

## 7. Padstacks — `0x1C`

Defines a pad's copper geometry across layers.

```
u16 @ pk+44          layer count  (sane 0..60)
components start at   pk + 224, stride 36 bytes:
    byte @ +0        primitive type
    i32  @ +8        width
    i32  @ +12       height
```

Pad **shape** by primitive type: `2 = round`, `6 = rect`, `12/22 = oblong`,
`23 = poly` (treat as rect). **SMD** (`layerCount ≤ 1`) uses component 0;
**through-hole** uses the first non-clearance shape component
(type ∉ {0, 23}, `0 < w,h < 200000`). Dimensions are in board units.

*(The `pk+224` / stride-36 layout is V180-specific; other versions differ — this
is the most version-sensitive part of the format.)*

---

## 8. Component placements

Two related record types resolve **refdes → placement + footprint pads**.

### 8.1 `0x2D` — Footprint instance

```
byte[k]=0x2D
byte[k+2]    side            (0 = top, 1 = bottom)
u32 @ k+4    key
u32 @ k+28   rotation        (millidegrees)
i32 @ k+32   coordX          (placement, board units)
i32 @ k+36   coordY
u32 @ k+40   instRef  -> 0x07 instance record
u32 @ k+48   firstPadPtr -> 0x32 chain (per types.h; prefer parent-scan)
```

Reject off-board / origin false positives (`coordX,coordY < 15000`). The pads are
the `0x32` blocks whose `parent@+28` equals this `0x2D`'s key.

### 8.2 `0x07` — Instance (carries the refdes)

```
byte[k]=0x07
u32 @ k+4    key
u32 @ k+28   refDesStrPtr -> strings[…]   (the reference designator)
```

Resolve `0x2D.instRef → 0x07 → strings → refdes`. Accept a refdes whose first
char is a real designator prefix (`URCLDQJXYTFKPWVAS`) and contains a digit.

### 8.3 Assembling a placement

```
for each 0x2D (validated via 0x07 → refdes):
    place = (refdes, coordX, coordY, side, rotation_mdeg)
for each 0x32 whose parent@+28 is a known 0x2D:
    pad_box = bbox@+68  (or padstack-derived for THT)
    attach to that footprint
=> refdes -> (x, y, side, rot_mdeg, [ (x1,y1,x2,y2, round?) … ])
```

**Scan `0x2D` and `0x32` blocks directly** (not through a shared one-block-per-key
index): a real component's or pad's key can be stolen by a false block of another
type, silently dropping the whole part or a pad.

*(An older/alternative path: fixed **48-byte component rows** — `field[0]` =
refdes-id (→ string table), `field[2]` = X, `field[4]` = Y, `field[5]` = 7. Useful
as a cross-check for placement, but the `0x2D/0x07/0x32` path also yields pads and
rotation.)*

---

## 9. Board outline

There is often **no explicit "board geometry" outline**; the **route KEEPIN**
shape hugs the board edge. Take `0x28` shapes with `class == KEEPIN (0x15)`, pick
the largest by bbox area, and trace its boundary chain (§6.6). Fall back to the
overall copper/pad extent if none traces cleanly.

---

## 10. Layer model

* Copper layers are `0x05` tracks / `0x28` shapes with `class == ETCH (0x06)`;
  their `subclass` (`byte @ +3`) is the **copper layer index** (0 = top,
  increasing downward through the stackup). Collect the distinct subclasses that
  appear to enumerate the stackup.
* `class == KEEPIN (0x15)` → route keepin / board shape (§9).
* Other small classes are mechanical / drawing / silkscreen layers (not decoded
  here).

There is **no per-layer name string** in the objects parsed here — name layers by
position (Top / Inner N / Bottom).

---

## 11. Minimal parse algorithm

```
d = read whole file
header  = parse_header(d)                 # §3  (magic, type counts, version)
strings = parse_strings(d, 0x1200)        # §4

# object graph (validate by cross-links)  §6
nets    = scan 0x1B  -> {key: name}        (+ synthetic names for anon)
x04s    = scan 0x04  -> {key: net_name}    (member must point at a real 0x1B)
tracks  = scan 0x05  -> {key: (class, subclass, net)}
shapes  = scan 0x28  -> [{key, class, subclass, net, firstSeg, bbox}]
segsL   = scan 0x15/16/17 -> {key: (offset, parent)}   parent ∈ tracks|shapes
segsA   = scan 0x01  -> validated arcs (center/radius check)

copper  = line segments whose parent track class == ETCH, tagged by subclass  # §6.4
pours   = ETCH shapes, boundary-walked                                        # §6.6
outline = largest KEEPIN shape, boundary-walked                               # §9
vias    = 0x33 with net + sane padstack                                       # §6.8

# placements                               §8
comps   = 0x2D (refdes via 0x07) -> (x, y, side, rot, pads from 0x32@parent)

# join copper/pads/vias to nets via their ptr1 -> x04 -> net name
```

Output: nets (`{key/index: name}`), copper (`x1,y1,x2,y2,layer,width,net`),
vias (`x,y,size,net`), pads per component (`x1,y1,x2,y2,shape,net`), pours
(`net,layer,[points]`), board outline (`[points]`), placements (`refdes, x, y,
side, rotation`).

---

## 12. Gotchas & reliability notes

* **Cross-validate, don't trust a lone type byte.** The single-byte tags collide
  constantly with data. Every block is confirmed by its links (member→net,
  segment→track, arc geometry, via→padstack).
* **Scan target blocks directly.** A shared "key → one block" index silently drops
  real objects when another block type steals the key. For `0x2D`, `0x32`,
  `0x05`, and segments, scan those tags directly and keep all valid hits.
* **Extract copper by parent, not by chain-walking.** `firstSegPtr` chains break
  on collisions and drop trace tails; scanning segments and checking their parent
  track recovers everything.
* **Arcs need geometric validation** (center/radius through endpoints) or you get
  thousands of phantom arcs.
* **Word-swapped doubles** — floats (arc center/radius) are IEEE doubles with the
  two 32-bit halves swapped. Miss this and every arc is garbage.
* **Padstack layout is the most version-specific part** (`pk+224`, stride 36,
  layer count `@+44` are V180). Expect to re-locate these on other Allegro
  versions.
* **THT vs SMD pads differ:** SMD `bbox@+68` is the real pad; THT `bbox@+68` is
  the keep-out — pull the copper size from the padstack.
* **Anonymous nets** are real and must group copper; synthesize stable names but
  gate on linked-list consistency so junk `1B` patterns don't spawn phantom nets.
* **Y is down**, units are **0.1 mil**; negate Y for a Y-up renderer.
