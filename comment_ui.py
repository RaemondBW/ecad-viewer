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


# ── Firebase platform backend (optional) ─────────────────────────────────────
# When BACKEND is set, generate() splices a <script type="module"> that loads the
# Firebase SDK from the gstatic CDN, builds { store, identity, permissions } from
# the same stores/firebase.js the bundled apps use, and attaches it to the live
# window.Comments via setBackend(). Falls back to localStorage if it can't load.
import json

_FB_CDN = "https://www.gstatic.com/firebasejs/12.15.0/"
_FB_MODULES = {
    "firebase/app": "firebase-app.js",
    "firebase/app-check": "firebase-app-check.js",
    "firebase/auth": "firebase-auth.js",
    "firebase/firestore": "firebase-firestore.js",
    "firebase/functions": "firebase-functions.js",
}

# The public web config + platform ids. appId is per-caller (a tenant on the platform).
BACKEND = {
    "firebaseConfig": {
        "apiKey": "AIzaSyDXtRd1rIDoOy8fecz1WesCbW8pHizmwJA",
        "authDomain": "canvas-comments-c463a.firebaseapp.com",
        "projectId": "canvas-comments-c463a",
        "storageBucket": "canvas-comments-c463a.firebasestorage.app",
        "messagingSenderId": "717599728108",
        "appId": "1:717599728108:web:4df16195665ecd874e20c9",
    },
    "appCheckSiteKey": None,   # App Check not wired yet (rules bootstrap-relaxed)
    "googleClientId": "717599728108-269dgfuvjrpt21ogvpo2dvcr1blon0hs.apps.googleusercontent.com",
}


def firebase_bootstrap(app_id):
    """A <script type="module"> that CDN-loads Firebase, builds the backend for
    `app_id` scoped by window.__cmtContext, and calls window.Comments.setBackend().
    Skips itself in embedded (?modal=1) previews."""
    src = (_LIB_DIR / "stores" / "firebase.js").read_text()
    for bare, fname in _FB_MODULES.items():
        src = src.replace(f"'{bare}'", f"'{_FB_CDN}{fname}'")
    src = src.replace("export default createFirebaseComments;", "")
    src = re.sub(r"^export\s+function", "function", src, flags=re.M)
    if "export " in src or "'firebase/" in src:
        raise RuntimeError("comment_ui: firebase.js not fully CDN-transformed")
    cfg = json.dumps({**BACKEND, "appId": app_id})
    boot = (
        "\ntry {\n"
        "  if (new URLSearchParams(location.search).get('modal') !== '1') {\n"
        f"    const b = createFirebaseComments({{ ...{cfg}, context: window.__cmtContext }});\n"
        "    window.__cmtBackend = b;\n"
        "    if (window.Comments && window.Comments.setBackend) window.Comments.setBackend(b);\n"
        "  }\n"
        "} catch (e) { console.warn('[canvas-comments] backend unavailable, using localStorage:', e); }\n"
    )
    return '<script type="module">\n' + src + boot + "</script>"
