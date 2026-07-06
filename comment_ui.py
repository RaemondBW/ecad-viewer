"""Shared commenting UI for the schematic and layout viewers.

This module no longer holds its own copy of the commenting code. It loads the
canonical, framework-agnostic library at `../comments/comments.js` (the same
files the Vector Designer imports) and adapts it for inlining into the viewers'
single-file HTML:

  * `JS`  — the library de-module-ified (its `export`s stripped) and wrapped in
            an IIFE, so it drops into the viewers' classic `<script>` and leaks
            only `window.Comments`, exactly as the old embedded copy did.
  * `CSS` — empty. The library auto-injects its stylesheet on `init()` (see
            `comments/comments.css` / `injectCss` in comments.js), so there is a
            single source of truth for the styles too.

The viewers still splice these into their templates via the `/*__CMT_CSS__*/`
and `/*__CMT_JS__*/` placeholders in `generate()`.

The library talks to each host only through a small adapter — see the JSDoc at
the top of `comments/comments.js`. The viewers pass `svg:` (accepted as a legacy
alias for `surface:`), `stage`, `button`, `context`, `project`, `resolveAnchor`,
and either `onViewChange` or a manual `Comments.reproject()` on view changes.
Storage sits behind the same async interface (`list/addThread/addReply/update/
remove`); swap the localStorage placeholder for a real backend with
`Comments.setStore(myStore)`. Storage scope is per-sheet `sch:<name>:<sheetIdx>`
in the schematic and `brd:<name>` in the layout; author persists under
localStorage `xcomment-author`.
"""

import re
from pathlib import Path

# Canonical library lives at repo-root `comments/` (this file is in schematic-viewer/).
_LIB_DIR = Path(__file__).resolve().parent.parent / "comments"


def _load_lib_js():
    """comments.js as a classic-script IIFE (ES module syntax removed)."""
    src = (_LIB_DIR / "comments.js").read_text()
    src = src.replace("export default createComments;", "")     # bare ESM statement
    src = re.sub(r"^export\s+function", "function", src, flags=re.M)  # `export function` -> `function`
    if "export " in src:   # guard: catch any new ESM syntax the transform doesn't cover
        raise RuntimeError("comment_ui: unhandled `export` in comments.js after de-module-ify")
    # Wrap so nothing but window.Comments leaks into the viewer's script scope.
    return "(function(){\n" + src + "\n})();"


JS = _load_lib_js()
CSS = ""   # styles are auto-injected by the library at init(); see comments/comments.css
