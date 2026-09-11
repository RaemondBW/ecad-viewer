"""Host extension points shared by the two viewers.

`generate(..., ext=...)` splices host-supplied fragments into the page at these
slots, so a host can add its own chrome, styles and scripts (and, with
`shell=True`, its own model loader) without editing the templates:

    css          inside the page <style>
    body         first thing inside <body> (overlays, gates)
    toolbar      in the toolbar, left of the view controls
    toolbar_end  at the right end of the toolbar
    js           inside the main <script>, right after `window.Viewer` is defined
                 (so it can register `Viewer.on('ready', ...)` immediately)
    tail         after the main <script> (e.g. a <script type="module">)

The JS side is `window.Viewer` — see the "host integration" block in either
template for the events and hooks it offers.
"""
import re

SLOTS = {
    "css": "/*__EXT_CSS__*/",
    "body": "<!--__EXT_BODY__-->",
    "toolbar": "<!--__EXT_TOOLBAR__-->",
    "toolbar_end": "<!--__EXT_TOOLBAR_END__-->",
    "js": "/*__EXT_JS__*/",
    "tail": "<!--__EXT_TAIL__-->",
}


def apply(html, ext=None):
    """Fill every slot from `ext` (unknown keys are an error; missing keys → empty)."""
    ext = dict(ext or {})
    bad = set(ext) - set(SLOTS)
    if bad:
        raise ValueError(f"unknown ext slot(s): {sorted(bad)}; valid: {sorted(SLOTS)}")
    for key, marker in SLOTS.items():
        html = html.replace(marker, ext.get(key, "") or "")
    return html


def strip_webfonts(html):
    """Remove the Google-Fonts <link> tags so the file makes no network request at
    all (the viewer falls back to system mono/sans fonts)."""
    return re.sub(r"[ \t]*<link[^>]*fonts\.(?:googleapis|gstatic)\.com[^>]*>\n?", "", html)
