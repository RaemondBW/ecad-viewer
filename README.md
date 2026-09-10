# ecad-viewer — `.brd` + `.DSN` → self-contained HTML

Pure-Python converters that read the vendor's proprietary binary formats directly —
no vendor tools, no exports — and emit single-file interactive HTML viewers:

- **Schematic** (`dsn_viewer.py`): `.DSN` → multi-sheet schematic
  with native symbol geometry, net highlighting, part cards, search, and an
  optional revision **diff** against an older `.DSN`.
- **Layout** (`brd_viewer.py`): `.brd` → board view with per-layer copper,
  vias, pours, outline, component placement, net highlighting, and board flip.
- **Cross-probe** (`xprobe.py`): builds both viewers as a linked pair, so selecting
  a net or part in one highlights it in the other.

Each output is one `.html` file with the model baked in. It works offline and
needs nothing but a browser.

## Files

| File | Role |
|---|---|
| `dsn_convert.py` | `.DSN` parser (OLE2 container → design, sheets, parts, wires, ports) |
| `dsn_viewer.py`  | schematic model builder + HTML template; `build_model()`, `generate()` |
| `brd_convert.py`   | `.brd` parser (header, string table, placements, copper, vias) |
| `brd_objects.py`   | `.brd` record layouts / object decoders used by `brd_convert.py` |
| `brd_viewer.py`    | layout model builder + HTML template; `build()`, `generate()` |
| `xprobe.py`        | schematic↔layout net/part correspondence; `generate_linked()` |
| `comment_ui.py`    | *optional* hook for the separate commenting library (see below) |
| `docs/dsn-format.md`   | reverse-engineered `.DSN` format reference |
| `docs/brd-format.md` | reverse-engineered `.brd` format reference |

The format docs are detailed enough to write an interpreter from scratch.

## Requirements

Python 3.10+. The only third-party package is **`olefile`** (reads the `.DSN`
OLE2 container); the `.brd` path is pure standard library.

```bash
pip install olefile            # or: uv run --with olefile python ...
```

## Usage

```bash
# linked pair — schematic + layout that cross-probe each other:
python3 xprobe.py --build board.DSN board.brd out/ [board.BOM]
#   → out/schematic.html and out/pcb.html

# a single view:
python3 dsn_viewer.py board.DSN -o schematic.html [--diff old.DSN]
python3 brd_viewer.py   board.brd -o layout.html    [--bom board.BOM]

# report cross-probe match quality without building anything:
python3 xprobe.py board.DSN board.brd [board.BOM]
```

The `.BOM` is optional: an Bill-of-Materials text export that supplies
per-part **values** (the `.DSN` streams don't carry them).

Add `--offline` to `xprobe.py --build` (or `offline=True` in the API) to strip
the Google-Fonts links too, so the file makes no network request at all.

### Python API

```python
import dsn_viewer, brd_viewer, xprobe

model = dsn_viewer.build_model("board.DSN", diff_path=None)   # dict; JSON-serialisable
dsn_viewer.generate("board.DSN", "schematic.html")

lay = brd_viewer.build("board.brd", bom_path=None)
brd_viewer.generate("board.brd", "layout.html")

xprobe.generate_linked("board.DSN", "board.brd", "out/", bom_path=None, offline=False)
```

## Commenting (optional)

Threaded comments on the viewers come from a **separate** project,
[`canvas-comments`](https://github.com/RaemondBW/canvas-comments). This repo does
not include it and does not need it. `comment_ui.py` looks for the library at
`$CANVAS_COMMENTS_DIR`, then `./comments/`, then `../comments/`; if none exists
the viewers are built with an inert `window.Comments` and the comment button is
hidden.

To build with comments, check the library out next to this repo (or nest it as
`comments/`, which is git-ignored here) — for example as a sibling submodule, the
way [`bus-mime`](https://github.com/RaemondBW/bus-mime) consumes both. Hosted
"shell" builds (`generate(..., shell=True)`, data fetched after sign-in) require
the library and its Firebase backend.
