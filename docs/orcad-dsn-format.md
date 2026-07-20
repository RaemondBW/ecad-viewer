# OrCAD Capture `.DSN` Schematic Format

A complete reference for writing an interpreter that reconstructs schematic
**geometry and connectivity** from an OrCAD Capture `.DSN` design, with **no
Cadence tools required**.

> **Status / provenance.** The `.DSN` binary structure format is proprietary and
> undocumented by Cadence. Everything here was reverse-engineered and validated
> empirically against real designs (cross-checked with
> [Werni2A/OpenOrCadParser](https://github.com/Werni2A/OpenOrCadParser)). Field
> offsets are stable across the OrCAD 16.x / 17.x era files tested, but treat the
> format as version-sensitive: **locate records by signature, not by strict
> sequential parsing.** This is the single most important design decision — the
> record stream contains variable-length property blocks that a strict walker
> trips over, so a robust reader scans for the distinctive byte signature of each
> record type it cares about.

The reference implementation is `orcad_convert.py` in this repo; this document is
the format spec behind it.

---

## 1. Container: OLE2 Compound File

A `.DSN` is a **Microsoft Compound File Binary** (OLE2) — the same container as
old `.doc`/`.xls`. Use any OLE2 library (`olefile` in Python, `libgsf`, etc.) to
enumerate and read its internal streams. You do **not** parse the raw file bytes
directly; you parse the *decompressed streams* the OLE layer hands you.

### Streams that matter

| Path | Contents |
|---|---|
| `Views/<view>/Pages/<page>` | One schematic **page** (sheet). The bulk of the work. Binary structure records: wires, nets, part instances, pins, page graphics. |
| `Cache` | Library **symbol definitions** (pin names + positions) and the **title-block** symbol. Optional but needed for IC pin names. |
| *(others: `HierarchyHeirarchy`, `Netlist`, `Library`, root properties…)* | Not required for geometry/connectivity; ignore unless extending. |

Enumerate every entry; keep those whose path starts with `Views/` and contains
`/Pages/`. The **view** name is `entry[1]`, the **page** name is `entry[-1]`. A
page id of `"<view>/<page>"` is a convenient stable key. Views are typically
prefixed `01_`, `02_`, … so sorting page ids gives sheet order.

---

## 2. Coordinate system and units

* Coordinates are signed integers in the schematic's own grid, **1 unit = 1 mil**
  (0.001"). A typical A/B-size sheet spans roughly `0..1600 × 0..1100` units.
* Origin is top-left-ish; **Y increases downward** (screen convention). No flip
  needed for a screen renderer.
* Wire endpoints are stored as **`int32`**. Pin placement coordinates are stored
  as **`uint16`** (a separate, smaller record) — they still land on the same grid
  and coincide exactly with wire endpoints.

---

## 3. Object-id "windows" (the key trick)

Every object in a page carries a 32-bit **handle/id**. These ids are *not*
random — they cluster into `0xHHHH_0000` **windows** (the high 16 bits identify a
pool). Two windows matter:

* **Net window** — the high-16 shared by every wire's `net_id` and by the
  net-name table entries on that page. Merges all segments of a net.
* **Object window(s)** — the high-16 shared by object handles (wires, pins).
  Used to recognise not-connected pins.

The window values **differ per page**, so **detect them, never hard-code**.

### 3.1 Net-window detection

Scan for the wire signature and take the dominant high-16 of `net_id`:

```
for o in 0 .. len-28:
    if u32(o+8) != 0x30:            continue    # wire's constant field
    nid = u32(o+4)                              # candidate net_id
    if (nid >> 16) == 0:            continue
    x1,y1,x2,y2 = i32×4 @ o+12
    if (x1,y1) != (x2,y2) and max(|coords|) < 100000:
        tally[nid >> 16] += 1
net_window = argmax(tally)          # or None if no wires on this page
```

A page with no wires (e.g. a pure hierarchical-block page) has no net window;
emit it as empty.

### 3.2 Object-window detection

Falls out of wire parsing (§4): collect the high-16 of each wire's *handle*
(`u32 @ o+0`); keep every window that recurs (`count ≥ 2`). Pin detection also
falls back to wire-endpoint coincidence, so this set only needs to catch NC pins.

---

## 4. Wires — `Structure::WireScalar`

**Signature / layout (28 bytes):**

| Offset | Type | Field |
|---|---|---|
| +0  | `u32` | object **handle** (high-16 = object window) |
| +4  | `u32` | **net_id** (high-16 = net window) — all segments of a net share it |
| +8  | `u32` | constant **`0x30`** |
| +12 | `i32` | x1 |
| +16 | `i32` | y1 |
| +20 | `i32` | x2 |
| +24 | `i32` | y2 |

**Scan** every 1-byte offset; accept when `(net_id>>16) == net_window`,
`u32(o+8) == 0x30`, endpoints are not equal, and `max(|coord|) < 100000`. Each
hit is one wire segment `(net_id, x1, y1, x2, y2)`.

### 4.1 Point → net map

Build `point_nets[(x,y)] = net_id` from **both endpoints** of every wire. This is
the table pins snap to. (Optionally also add integer grid points along
axis-aligned segments if you need mid-segment pin taps; endpoints suffice for the
sample designs.)

---

## 5. Net-name table

Named nets store a run of entries somewhere in the page stream:

| Offset | Type | Field |
|---|---|---|
| +0 | `u32` | net_id (high-16 == net window) |
| +4 | `u16` | name length `L` (`0 < L < 64`) |
| +6 | `L` bytes | ASCII name (all `0x20..0x7e`) |
| +6+L | `u8` | `0x00` terminator |

Scan for `u32` values in the net window; when the following bytes form a valid
length-prefixed, NUL-terminated ASCII string, record `net_names[net_id] = name`
(first occurrence wins). Unnamed nets keep no entry.

---

## 6. Part instances — `Structure::PlacedInstance`

A part instance is anchored by its **package reference**, a string of the form:

```
<package-name>.Normal\0        regex: ([\x20-\x7e]{1,48})\.Normal\x00
```

Every `.Normal` match starts one instance; the instance's bytes run until the
next `.Normal` (or end of stream).

### 6.1 Designator & strings

Immediately **after** the `.Normal\0` marker is a run of **length-prefixed,
NUL-terminated** strings (`u16 len`, `len` bytes, `\0`):

1. the **reference designator** (first real string, e.g. `U12`, `R517`),
2. the **source package**,
3. then per-pin net names / property values.

Take the **first** real string as the designator. (Taking the *last* strings
grabs a net name on parts with many connected nets, e.g. connectors.) Filter out
noise tokens like `'"`.

### 6.2 Pins — `T0x10` records

Within an instance's byte range, pin placements are 14-byte records:

| Offset | Type | Field |
|---|---|---|
| +0 | `u16` | pin **index** (1..200) |
| +2 | `u16` | placed **x** (`0 < x < 60000`) |
| +4 | `u16` | placed **y** (`0 < y < 60000`) |
| +6 | `u32` | **handle** (high-16 = object window) |
| +10 | `u32` | constant **`0`** |

**Accept a candidate pin** when `1 ≤ idx ≤ 200`, `handle != 0`, the trailing
`u32 == 0`, coords in range, **and** either:

* `(x,y)` is a **wire endpoint** (the connectivity-validated test — this is a
  *connected* pin), **or**
* `handle`'s high-16 is in an **object window** *and* `(x,y)` lies inside the
  sheet bound (catches *not-connected* pins).

The sheet bound rejects stray records whose handle happens to fall in an object
window but whose coordinate is far off-sheet (which would otherwise blow up the
part's box). Compute the bound from the drawn page **frame** (border graphics,
§8) with a tight ±25-mil margin; fall back to the wire extent (±1000) or a
generous default.

Dedupe pins by handle. Result per instance: `{designator, package,
pins:[(idx,x,y)]}`.

### 6.3 Pin → net binding

Purely **geometric**: `net_id = point_nets[(pin.x, pin.y)]`. A pin's placed
coordinate coincides with a wire endpoint (validated 100% on the sample design).
No pin↔net field exists to parse — the binding *is* the geometry.

---

## 7. Cross-page net merging & net keys

Named nets are electrically **global** (power/ground/off-page/hierarchical
connectivity merges by **name** across pages). Unnamed nets are **page-local**.
Produce a stable key:

```
net_key(pageid, net_id):
    name = net_names[net_id]
    return name if name else f"N${pageid}:{net_id:08x}"
```

Two pins on different pages with the same *named* net are connected; two unnamed
nets never merge even if their ids collide across pages.

---

## 8. Page graphics primitives

Drawing primitives (sheet frame, comment notes, boxes, dividers) share the
wire/pin coordinate space. Framing:

```
<type><type><u32 len><00 00 00 00><body…>       body is `len` bytes
```

i.e. `byte[o] == byte[o+1] == type`, `u32 len @ o+2` (`0 < len < 20000`), four
zero bytes at `o+6`, body at `o+10`. `type` is the Primitive enum:

| type | primitive | body |
|---|---|---|
| `0x28` | **Rect** | `i32 x1,y1,x2,y2` |
| `0x29` | **Line** | `i32 x1,y1,x2,y2` |
| `0x2a` | Arc | *(bbox etc.; not decoded here)* |
| `0x2b` | Ellipse | *(not decoded)* |
| `0x2d` | **Polyline** | `u16 count`, then `count × (i32 x, i32 y)` |
| `0x2e` | **CommentText** | `i32 bbox[4]`, `i32 posX,posY @+16`, `u32 @+24`, `u16 fontSize @+28`, `text\0 @+30` |

Validate coordinates with a sheet-sane range (e.g. `-5000..80000`) to reject
false hits. The **page frame** = bounding box of all Line+Rect graphics; it gives
the sheet border and the bound used in §6.2. The largest CommentText is usually
the page's descriptive heading (title-block subtitle).

---

## 9. `Cache` stream — symbol definitions & title block

Optional, but required for **IC pin names** and the **title block**.

### 9.1 Symbol definitions

The Cache is a run of symbol blocks, each headed by `<name>.Normal\0` (regex
`([\x20-\x7e]{2,48})\.Normal\x00`). A block runs to the next header. Inside, each
pin is a **SymbolPin** record found by the preamble magic:

```
FF E4 5C 39  00 00 00 00        PREAMBLE_MAGIC + 4 zero bytes
<u16 len><name (len bytes)>\0
<u32 flags><i32 startX><i32 startY> …
```

`(startX, startY)` is the pin's grid position in the symbol's **own local frame**
(`|start| < 5000`). Names are short (`0 < len < 18`), printable, excluding `/` and
`\`. Result: `symbols[name] = [(pin_name, sx, sy), …]`. Keep, per name, the block
that actually carries pins.

### 9.2 Assigning pin names to placed instances

For a "box" (IC) instance with placed pins `[(idx,x,y)]` and a matching symbol
`[(name,sx,sy)]`, find the rigid transform — rotation `k·90°` + optional mirror +
translation — that best aligns the two point sets, then map matched placed pins
to symbol pin names. Accept only if ≥60% (and ≥2) of pins align within a small
tolerance. This is a point-set registration, not a byte parse: the two records
share no ids, only geometry.

### 9.3 Title block

The page title-block is itself a symbol in the Cache. Anchor on the field labels
`Title\0`, `Sheet\0`, `Size\0` (CommentText), then parse the graphics around them
(§8) in the symbol's local frame (coords `0..400`). Cells are Rect/Line; field
labels are CommentText. Store `{w, h, lines, rects, labels}`; the viewer places it
in each sheet's bottom-right corner (`ox = frameRight - w`, `oy = frameBottom -
h`).

---

## 10. Derived / cosmetic reconstruction (optional)

These make the render look like OrCAD but aren't required for connectivity:

* **Junction dots** — a point where **≥3** wire ends meet is a real electrical
  tie (OrCAD draws a solid dot).
* **Dangling flags** — a **degree-1** wire endpoint that is *not* a component pin
  gets a power/ground/off-page connector glyph. Classify by net name:
  `GND*`/`*GND`/`VSS`/`0V` → ground; `+xxV`/`VCC*`/`VDD*`/`VEE`/`VREF`/… → power
  rail; else off-page **port**. Orient the glyph outward along the wire.
* **Port direction** — hierarchical/off-page symbols encode direction in their
  name: `PORTRIGHT`=out, `PORTLEFT`=in, `PORTBOTH`/`OFFPAGE*`=bidirectional
  (regex `(PORT[A-Z]+|OFFPAGE[A-Z]+)-[LR]\0`; the placement stores an `i16 x,y`
  insertion point at name-end+6). Self-calibrate the fixed per-symbol offset from
  the electrical pin by finding the single `(dx,dy)` that lands the most instances
  on the known flag grid.
* **Bus-entry stubs** — OrCAD hides bus taps behind a symbol we don't parse,
  leaving a ~10-mil gap. For each off-page port, cast a ray outward; if it reaches
  a **bus** wire (net name matching `\[\d+\.\.\d+\]`) within ~50 mil and aligned,
  emit a connecting segment carrying the signal net.

---

## 11. Bill of Materials (values sidecar)

The schematic streams do **not** carry per-reference **values** — they come from
a separate OrCAD **BOM text export** (`.BOM`). It's tab-separated with a header
row starting `Item … Part`; columns are `Item, Quantity, Reference, Part`. The
Reference list wraps onto indented continuation lines. Parse to
`{designator: value}` and merge by refdes.

---

## 12. Minimal parse algorithm

```
open OLE2(.DSN)
for each stream "Views/<view>/Pages/<page>":
    data = stream bytes
    net_window = detect_net_window(data)            # §3.1
    graphics   = parse_graphics(data)               # §8
    if net_window is None: emit empty page; continue
    wires, obj_windows = parse_wires(data, net_window)   # §4
    net_names          = parse_net_names(data, net_window) # §5
    point_nets         = endpoints_of(wires)        # §4.1
    bound              = frame_or_wire_extent(graphics, wires)  # §6.2
    instances          = parse_instances(data, point_nets, obj_windows, bound) # §6
    bind pins→nets via point_nets                   # §6.3
parse Cache: symbols + title block                  # §9
merge nets across pages by net_key                  # §7
(optional) junctions, flags, ports, bus stubs        # §10
(optional) merge BOM values by refdes                # §11
```

Output per sheet: wires (`x1,y1,x2,y2,net_key`), parts (`designator, package,
box, pins[(x,y,net_key,idx,pin_name)], symbol_kind`), labels, junctions,
connectors, page frame, graphics, title block.

---

## 13. Gotchas & reliability notes

* **Signature-scan, don't sequentially parse.** Property blocks are
  variable-length and version-dependent; a strict walker desyncs. Every record
  here is found by its byte signature + structural validation.
* **Windows are per-page.** Detect the net window and object windows for *each*
  page; never reuse or hard-code them.
* **Pin coords are `u16`, wire coords are `i32`** — different widths, same grid.
* **Designator = first string after `.Normal`**, not the last (connectors have
  many trailing net-name strings).
* **False pin records** exist (property/graphic records look pin-shaped). Two
  independent filters kill them: wire-endpoint coincidence, or object-window
  handle + on-sheet bound.
* **NC (unconnected) pins** don't touch a wire — they're recovered only via the
  object-window handle test, so keep that path.
* **Key/handle collisions**: when two candidates claim the same id, prefer the
  one with a resolved/consistent link (e.g. a real net name, an in-window handle).
* Everything geometric (pin→net, symbol pin-name mapping, junctions, flags) is
  reconstructed from **coordinate coincidence**, because the format stores the
  wiring as geometry, not as an explicit netlist inside the page stream.
