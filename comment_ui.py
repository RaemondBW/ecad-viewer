"""Optional commenting UI for the schematic and layout viewers.

The commenting system is a *separate* project (`RaemondBW/canvas-comments`) and
this module is the only place the viewers touch it. It is **optional**: when the
library can't be found, `JS` is an inert stand-in (`window.Comments` whose
methods are no-ops and which hides the comment button), the Firebase bootstrap is
empty, and the generated HTML is a plain self-contained viewer. Nothing else in
this repo changes.

The library is looked up, in order, at:

  1. `$CANVAS_COMMENTS_DIR`                   (explicit override)
  2. `<this dir>/comments/`                   (checkout nested inside this repo)
  3. `<this dir>/../comments/`                (sibling submodule, as in `bus-mime`)

When found, `comments.js` is adapted for inlining into the viewers' single-file
HTML:

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

import os
import re
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _find_lib_dir():
    """Directory holding canvas-comments' `comments.js`, or None if unavailable."""
    env = os.environ.get("CANVAS_COMMENTS_DIR")
    cands = ([Path(env)] if env else []) + [_HERE / "comments", _HERE.parent / "comments"]
    for d in cands:
        if (d / "comments.js").is_file():
            return d
    return None


_LIB_DIR = _find_lib_dir()
HAVE_COMMENTS = _LIB_DIR is not None


def _demodulify_iife(filename, default_export):
    """A comments/ library file as a classic-script IIFE (ES module syntax removed),
    leaking only its window.* global."""
    src = (_LIB_DIR / filename).read_text()
    src = src.replace(f"export default {default_export};", "")
    src = re.sub(r"^export\s+function", "function", src, flags=re.M)
    if "export " in src:
        raise RuntimeError(f"comment_ui: unhandled `export` in {filename} after de-module-ify")
    return "(function(){\n" + src + "\n})();"


# Inert stand-in used when the library isn't available: same surface the viewers
# call (see comments.js `init()`'s return value), every method a no-op. `init`
# hides the toolbar comment button so the UI doesn't advertise a dead feature.
_STUB_JS = """(function(){
  // canvas-comments not bundled: inert window.Comments so the viewer's calls are no-ops.
  const noop = function(){};
  window.Comments = {
    init: function(o){ if (o && o.button) o.button.style.display = 'none'; },
    isPlacing: function(){ return false; }, place: noop, reproject: noop, toggle: noop,
    countFor: function(){ return 0; }, watchCounts: noop, setContext: noop,
    setStore: noop, setBackend: noop
  };
})();"""

if HAVE_COMMENTS:
    # window.Comments (overlay) + window.Account (chip) + window.createDocuments (store)
    # + window.Share (Google-Docs share dialog).
    JS = (_demodulify_iife("comments.js", "createComments") + "\n"
          + _demodulify_iife("account.js", "createAccount") + "\n"
          + _demodulify_iife("documents.js", "createDocuments") + "\n"
          + _demodulify_iife("share.js", "createShare"))
else:
    JS = _STUB_JS
CSS = ""   # styles are auto-injected by the libraries at init(); see comments/*.css


def strip_webfonts(html):
    """Remove the Google-Fonts <link> tags so an offline build makes no network
    request at all (the viewer falls back to system mono/sans fonts)."""
    return re.sub(r"[ \t]*<link[^>]*fonts\.(?:googleapis|gstatic)\.com[^>]*>\n?", "", html)


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
    if not HAVE_COMMENTS:
        return ""
    cfg = json.dumps({**BACKEND, "appId": app_id, "authMode": "popup"})
    boot = (
        "\ntry {\n"
        "  if (new URLSearchParams(location.search).get('modal') !== '1') {\n"
        "    const _doc = new URLSearchParams(location.search).get('doc') || null;\n"
        f"    const b = createFirebaseComments({{ ...{cfg}, docId: _doc, context: window.__cmtContext }});\n"
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
    if not HAVE_COMMENTS:
        raise RuntimeError("shell mode needs the canvas-comments library (set CANVAS_COMMENTS_DIR "
                           "or check it out at ./comments or ../comments)")
    cfg = json.dumps({**BACKEND, "appId": app_id, "authMode": "popup"})
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
        f"    const b = createFirebaseComments({{ ...{cfg}, docId: docId || null, context: ctx }});\n"
        "    window.__cmtBackend = b;\n"
        "    if (gbtn) gbtn.onclick = () => b.identity.signIn();\n"
        "    const attempt = async () => {\n"
        "      if (rendered) return;\n"
        "      const documents = createDocuments(b);\n"
        "      try {\n"
        "        const meta = await documents.get(docId);\n"
        "        if (!meta) throw new Error('missing');\n"
        "        const latest = meta.rev || 1;\n"
        "        const revQ = parseInt(QP.get('rev') || '') || latest;\n"
        "        const diffQ = parseInt(QP.get('diff') || '') || null;   // older rev to compare against\n"
        "        const keyOf = r => (r === latest ? VIEW : VIEW + '-r' + r);\n"
        "        const dd = await documents.getContent(docId, keyOf(revQ));\n"   # public docs succeed while signed-out
        "        if (!dd) throw new Error('missing');\n"
        "        let oldModel = null;\n"
        "        if (diffQ && diffQ !== revQ) {\n"
        "          const od = await documents.getContent(docId, keyOf(diffQ)).catch(() => null);\n"
        "          if (od) oldModel = JSON.parse(od.data);\n"
        "        }\n"
        "        window.__docMeta = { rev: latest, curRev: revQ, diffRev: oldModel ? diffQ : null,\n"
        "          revs: (meta.revs || []).map(r => ({ n: r.n, ts: r.ts, note: r.note })) };\n"
        "        if (!QP.get('rev')) { try { const u = new URL(location.href); u.searchParams.set('rev', revQ); history.replaceState(null, '', u); } catch (e) {} }\n"
        "        window.__renderModel(JSON.parse(dd.data), dd.xprobe ? JSON.parse(dd.xprobe) : null, oldModel);\n"
        "        rendered = true; hideGate();\n"
        "        if (!MODAL) {\n"
        "          if (window.Comments && window.Comments.setBackend) window.Comments.setBackend(b);\n"
        "          const mnt = document.getElementById('cmt-account');\n"
        "          if (window.Account && mnt) window.Account.init({ mount: mnt, identity: b.identity, apiKeys: b.apiKeys });\n"
        "          const shareBtn = document.getElementById('cmt-share');\n"
        "          if (window.Share && shareBtn && b.identity.current()) { window.Share.init({ documents, identity: b.identity, linkFor: id => location.origin + location.pathname + '?doc=' + encodeURIComponent(id) }); shareBtn.style.display = ''; shareBtn.onclick = () => window.Share.open(docId); }\n"
        "        }\n"
        "      } catch (e) {\n"
        "        if (b.identity.current()) showGate('You don\\u2019t have access to this project.', false);\n"
        "        else showGate('Sign in to view this project.', true);\n"   # private → offer sign-in
        "      }\n"
        "    };\n"
        "    b.identity.subscribe(() => attempt());\n"
        "  } catch (e) { console.warn('[shell] backend error', e); showGate('Unable to load.', false); }\n"
        "})();\n"
    )
    return '<script type="module">\n' + _firebase_src() + boot + "</script>"
