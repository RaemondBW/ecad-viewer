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


def _demodulify_iife(filename, default_export):
    """A comments/ library file as a classic-script IIFE (ES module syntax removed),
    leaking only its window.* global."""
    src = (_LIB_DIR / filename).read_text()
    src = src.replace(f"export default {default_export};", "")
    src = re.sub(r"^export\s+function", "function", src, flags=re.M)
    if "export " in src:
        raise RuntimeError(f"comment_ui: unhandled `export` in {filename} after de-module-ify")
    return "(function(){\n" + src + "\n})();"


# window.Comments (overlay) + window.Account (account indicator + Profile/API keys).
JS = _demodulify_iife("comments.js", "createComments") + "\n" + _demodulify_iife("account.js", "createAccount")
CSS = ""   # styles are auto-injected by the libraries at init(); see comments/*.css


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


def _firebase_src():
    """firebase.js with its bare ES imports rewritten to the gstatic CDN."""
    src = (_LIB_DIR / "stores" / "firebase.js").read_text()
    for bare, fname in _FB_MODULES.items():
        src = src.replace(f"'{bare}'", f"'{_FB_CDN}{fname}'")
    src = src.replace("export default createFirebaseComments;", "")
    src = re.sub(r"^export\s+function", "function", src, flags=re.M)
    if "export " in src or "'firebase/" in src:
        raise RuntimeError("comment_ui: firebase.js not fully CDN-transformed")
    return src


def firebase_bootstrap(app_id):
    """Embedded-mode bootstrap: the model is already in the page. CDN-loads Firebase,
    attaches the backend to window.Comments, mounts the account chip. Skips ?modal=1."""
    cfg = json.dumps({**BACKEND, "appId": app_id, "authMode": "popup"})
    boot = (
        "\ntry {\n"
        "  if (new URLSearchParams(location.search).get('modal') !== '1') {\n"
        f"    const b = createFirebaseComments({{ ...{cfg}, context: window.__cmtContext }});\n"
        "    window.__cmtBackend = b;\n"
        "    if (window.Comments && window.Comments.setBackend) window.Comments.setBackend(b);\n"
        "    const mnt = document.getElementById('cmt-account');\n"
        "    if (window.Account && mnt) window.Account.init({ mount: mnt, identity: b.identity, apiKeys: b.apiKeys });\n"
        "  }\n"
        "} catch (e) { console.warn('[canvas-comments] backend unavailable, using localStorage:', e); }\n"
    )
    return '<script type="module">\n' + _firebase_src() + boot + "</script>"


def shell_bootstrap(app_id, view):
    """Hosted-shell controller: the page ships with NO model. Requires sign-in, then
    fetches documents/{?doc}/content/{view} = {data, xprobe} (rules-gated) and calls
    window.__renderModel(). Nothing is visible without a valid, authorized login — so
    the design data is never served in the HTML. `view` is 'schematic' or 'layout'.
    In ?modal=1 (cross-probe preview) it renders silently on the shared session,
    skipping the gate and comment/account chrome."""
    cfg = json.dumps({**BACKEND, "appId": app_id, "authMode": "popup"})
    # `doc` is already imported by firebase.js (same module scope); only add getDoc.
    imp = f"import {{ getDoc }} from '{_FB_CDN}firebase-firestore.js';\n"
    boot = (
        "\n(function () {\n"
        "  const QP = new URLSearchParams(location.search);\n"
        "  const docId = QP.get('doc') || '', MODAL = QP.get('modal') === '1';\n"
        f"  const VIEW = {json.dumps(view)};\n"
        "  const gate = document.getElementById('shell-gate'), gmsg = document.getElementById('shell-gate-msg'), gbtn = document.getElementById('shell-gate-btn');\n"
        "  const showGate = (msg, btn) => { if (MODAL || !gate) return; gate.style.display = 'flex'; if (gmsg) gmsg.textContent = msg; if (gbtn) gbtn.style.display = btn ? 'inline-flex' : 'none'; };\n"
        "  const hideGate = () => { if (gate) gate.style.display = 'none'; };\n"
        "  showGate('Loading\\u2026', false);\n"
        "  let rendered = false;\n"
        "  try {\n"
        "    const ctx = () => { const c = window.__cmtContext; return typeof c === 'function' ? c() : (c || (VIEW + ':' + docId)); };\n"
        f"    const b = createFirebaseComments({{ ...{cfg}, context: ctx }});\n"
        "    window.__cmtBackend = b;\n"
        "    if (gbtn) gbtn.onclick = () => b.identity.signIn();\n"
        "    b.identity.subscribe(async () => {\n"
        "      const u = b.identity.current();\n"
        "      if (!u) { showGate('Sign in to view this project.', true); return; }\n"
        "      if (rendered) { hideGate(); return; }\n"
        "      try {\n"
        "        const snap = await getDoc(doc(b.db, 'documents', docId, 'content', VIEW));\n"
        "        if (!snap.exists()) throw new Error('missing');\n"
        "        const dd = snap.data();\n"
        "        window.__renderModel(JSON.parse(dd.data), dd.xprobe ? JSON.parse(dd.xprobe) : null);\n"
        "        rendered = true; hideGate();\n"
        "        if (!MODAL) {\n"
        "          if (window.Comments && window.Comments.setBackend) window.Comments.setBackend(b);\n"
        "          const mnt = document.getElementById('cmt-account');\n"
        "          if (window.Account && mnt) window.Account.init({ mount: mnt, identity: b.identity, apiKeys: b.apiKeys });\n"
        "        }\n"
        "      } catch (e) { showGate('You don\\u2019t have access to this project.', false); }\n"
        "    });\n"
        "  } catch (e) { console.warn('[shell] backend error', e); showGate('Unable to load.', false); }\n"
        "})();\n"
    )
    return '<script type="module">\n' + imp + _firebase_src() + boot + "</script>"
