"""Shared commenting UI for the schematic and layout viewers.

Provides a self-contained `Comments` JS module (CSS + JS strings) that both
generators embed. Comments are threads (an initial message + replies) anchored
to a point, part, net, pin or pad. Storage sits behind a small async interface
(`list / addThread / addReply / update / remove`) so the placeholder
localStorage store can be swapped for a real backend later:

    Comments.setStore(myBackedStore)   // same async method shape

Each viewer calls Comments.init(adapter) supplying coordinate + anchor hooks:
    context      : string | () => string   (storage scope; per-sheet in schematic)
    svg, stage   : the SVG element and the positioned stage container
    button       : the toolbar toggle button
    project(x,y) : world coords -> {sx,sy} screen coords relative to `stage`
    resolveAnchor(clientX, clientY) -> {kind, x, y, ref?, label}
    onViewChange(cb): register cb to run whenever the view pans/zooms
"""

CSS = r"""
#cmt-layer{position:absolute;inset:0;pointer-events:none;z-index:16;overflow:hidden}
.cmt-marker{position:absolute;transform:translate(-50%,-100%);pointer-events:auto;cursor:pointer}
.cmt-marker .pin{width:22px;height:22px;border-radius:50% 50% 50% 3px;background:#F5A623;border:2px solid #fff;box-shadow:0 2px 6px rgba(0,0,0,.32);display:flex;align-items:center;justify-content:center;transform:rotate(45deg)}
.cmt-marker .pin b{transform:rotate(-45deg);color:#fff;font:700 11px 'IBM Plex Mono',monospace}
.cmt-marker.resolved .pin{background:#9AA0A8}
.cmt-marker.active .pin{outline:2px solid #2563a8;outline-offset:2px}
body.cmt-placing #svg,body.cmt-placing #svg *{cursor:default!important}
#cmt-ghost{position:absolute;transform:translate(-50%,-50%);pointer-events:none;z-index:17}
#cmt-ghost .pin{width:22px;height:22px;border-radius:50% 50% 50% 3px;background:#F5A623;border:2px solid #fff;box-shadow:0 2px 6px rgba(0,0,0,.32)}
#cmt-ghost.circle .pin{border-radius:50%!important;transform:none!important}
.tbtn.on,#cmt-btn.on{background:#F5A623;border-color:#D98E12;color:#fff}
#cmt-panel{position:absolute;width:306px;max-height:78%;display:none;flex-direction:column;background:#fff;border:1px solid #E0DCD1;border-radius:12px;box-shadow:0 16px 40px rgba(20,16,8,.24);z-index:41;overflow:hidden;font-family:'IBM Plex Sans',system-ui,sans-serif}
#cmt-panel .cmt-head{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:9px 10px 9px 13px;border-bottom:1px solid #EEE9DE;font-size:12px}
#cmt-panel .cmt-anchor{font-weight:700;color:#3A362D;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#cmt-panel .cmt-head span{display:flex;align-items:center;gap:6px;flex-shrink:0}
#cmt-panel .cmt-head button{cursor:pointer;border:1px solid #D9D4C6;background:#fff;border-radius:6px;font-size:11px;padding:2px 8px;color:#6E6A60}
#cmt-panel .cmt-msgs{overflow-y:auto;padding:2px 13px}
#cmt-panel .cmt-msg{padding:8px 0;border-bottom:1px solid #F2EEE3}
#cmt-panel .cmt-msg:last-child{border-bottom:none}
#cmt-panel .cmt-meta{font-size:10.5px;color:#A19B8E;margin-bottom:2px}
#cmt-panel .cmt-meta b{color:#5A5648;font-weight:600}
#cmt-panel .cmt-text{font-size:12.5px;color:#221F1A;white-space:pre-wrap;word-break:break-word;line-height:1.4}
#cmt-panel .cmt-empty{font-size:11px;color:#A19B8E;padding:8px 0}
#cmt-panel .cmt-reply{display:flex;flex-direction:column;gap:6px;padding:8px 12px;border-top:1px solid #EEE9DE;background:#FBFAF7}
#cmt-panel .cmt-row{display:flex;gap:6px;align-items:center}
#cmt-panel .cmt-input{resize:none;border:1px solid #D9D4C6;border-radius:6px;padding:6px 8px;font:12px 'IBM Plex Sans',system-ui;outline:none}
#cmt-panel .cmt-input:focus,#cmt-panel .cmt-author:focus{border-color:#F5A623}
#cmt-panel .cmt-author{flex:1;min-width:0;border:1px solid #D9D4C6;border-radius:6px;padding:4px 7px;font:11px 'IBM Plex Sans',system-ui;outline:none;color:#5A5648}
#cmt-panel .cmt-send{cursor:pointer;border:none;background:#2563a8;color:#fff;border-radius:6px;font-size:12px;padding:0 14px;height:26px}
#cmt-panel .cmt-send:disabled{background:#B9BEC6;cursor:default}
"""

JS = r"""
window.Comments = (function () {
  const $ = id => document.getElementById(id);
  const esc = s => (s == null ? '' : s + '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const uid = () => 'c' + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
  const now = () => Date.now();
  const fmt = ts => { try { return new Date(ts).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }); } catch (e) { return ''; } };

  // ---- placeholder backend: localStorage. Swap via Comments.setStore(...) with
  //      the same async method shape once a real backend exists. ----
  function makeLocalStore(ctx) {
    const key = () => 'xcomments:' + (typeof ctx === 'function' ? ctx() : ctx);
    const read = () => { try { return JSON.parse(localStorage.getItem(key()) || '[]'); } catch (e) { return []; } };
    const write = a => { try { localStorage.setItem(key(), JSON.stringify(a)); } catch (e) { } };
    return {
      list: async () => read(),
      addThread: async t => { const a = read(); a.push(t); write(a); return t; },
      addReply: async (id, m) => { const a = read(); const t = a.find(x => x.id === id); if (t) { t.messages.push(m); write(a); } return m; },
      update: async (id, p) => { const a = read(); const t = a.find(x => x.id === id); if (t) { Object.assign(t, p); write(a); } },
      remove: async id => write(read().filter(x => x.id !== id)),
    };
  }

  let cfg, store, threads = [], placing = false, openId = null, draft = null, author = '';

  async function reload() {
    threads = await store.list();
    renderMarkers();
    if (openId) { const t = threads.find(x => x.id === openId); if (t) openThread(openId); else closePanel(); }
  }

  const markerPos = a => cfg.project(a.x, a.y);

  function renderMarkers() {
    const layer = $('cmt-layer'); if (!layer) return;
    let h = '';
    for (const t of threads) {
      const p = markerPos(t.anchor); if (!p) continue;
      h += `<div class="cmt-marker${t.resolved ? ' resolved' : ''}${t.id === openId ? ' active' : ''}" data-id="${t.id}" style="left:${p.sx}px;top:${p.sy}px" title="${esc((t.anchor.label || 'Comment') + ' · ' + ((t.messages[0] || {}).text || ''))}"><div class="pin"><b>${t.messages.length || 1}</b></div></div>`;
    }
    if (draft) { const p = markerPos(draft.anchor); if (p) h += `<div class="cmt-marker active" style="left:${p.sx}px;top:${p.sy}px"><div class="pin"><b>+</b></div></div>`; }
    layer.innerHTML = h;
    [...layer.querySelectorAll('.cmt-marker[data-id]')].forEach(el => el.onclick = ev => { ev.stopPropagation(); openThread(el.dataset.id); });
  }

  function positionPanel(a) {
    const panel = $('cmt-panel'), st = cfg.stage, p = markerPos(a); if (!p) return;
    const w = 306, pad = 10;
    let left = Math.max(pad, Math.min(p.sx + 16, st.clientWidth - w - pad));
    let top = Math.max(pad, Math.min(p.sy - 24, st.clientHeight - 150));
    panel.style.left = left + 'px'; panel.style.top = top + 'px';
  }

  function panelHtml(t, isDraft) {
    return `<div class="cmt-head"><span class="cmt-anchor">${esc(t.anchor.label || 'Comment')}</span><span>` +
      (isDraft ? '' : `<button class="cmt-resolve">${t.resolved ? 'Reopen' : 'Resolve'}</button>`) +
      `<button class="cmt-close" title="Close">&times;</button></span></div>` +
      `<div class="cmt-msgs">` + (t.messages.length ? t.messages.map(m =>
        `<div class="cmt-msg"><div class="cmt-meta"><b>${esc(m.author)}</b> · ${esc(fmt(m.ts))}</div><div class="cmt-text">${esc(m.text)}</div></div>`).join('')
        : (isDraft ? '<div class="cmt-empty">New comment thread</div>' : '')) + `</div>` +
      `<div class="cmt-reply"><textarea class="cmt-input" rows="2" placeholder="${isDraft ? 'Write a comment…' : 'Reply…'}"></textarea>` +
      `<div class="cmt-row"><input class="cmt-author" placeholder="Your name" value="${esc(author)}"/><button class="cmt-send">${isDraft ? 'Comment' : 'Reply'}</button></div></div>`;
  }

  function openThread(id) {
    draft = null; const t = threads.find(x => x.id === id); if (!t) { closePanel(); return; }
    openId = id; const panel = $('cmt-panel'); panel.style.display = 'flex'; panel.innerHTML = panelHtml(t, false);
    positionPanel(t.anchor); wire(t, false); renderMarkers(); panel.querySelector('.cmt-input').focus();
  }
  function openDraft(anchor) {
    openId = null; draft = { anchor, messages: [] }; const panel = $('cmt-panel');
    panel.style.display = 'flex'; panel.innerHTML = panelHtml(draft, true);
    positionPanel(anchor); wire(draft, true); renderMarkers(); panel.querySelector('.cmt-input').focus();
  }
  function closePanel() { openId = null; draft = null; const panel = $('cmt-panel'); if (panel) panel.style.display = 'none'; renderMarkers(); }
  function hideGhost() { const g = $('cmt-ghost'); if (g) g.style.display = 'none'; }

  function wire(t, isDraft) {
    const panel = $('cmt-panel');
    panel.querySelector('.cmt-close').onclick = closePanel;
    const rb = panel.querySelector('.cmt-resolve'); if (rb) rb.onclick = async () => { await store.update(t.id, { resolved: !t.resolved }); await reload(); };
    const send = async () => {
      const ta = panel.querySelector('.cmt-input'), na = panel.querySelector('.cmt-author');
      const txt = (ta.value || '').trim(); if (!txt) return;
      author = (na.value || '').trim() || 'Anonymous'; try { localStorage.setItem('xcomment-author', author); } catch (e) { }
      const msg = { id: uid(), author, text: txt, ts: now() };
      if (isDraft) { const nt = { id: uid(), anchor: t.anchor, resolved: false, ts: now(), messages: [msg] }; draft = null; await store.addThread(nt); await reload(); openThread(nt.id); }
      else { await store.addReply(t.id, msg); await reload(); }
    };
    panel.querySelector('.cmt-send').onclick = send;
    panel.querySelector('.cmt-input').addEventListener('keydown', e => { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); send(); } });
  }

  function setPlacing(on) { placing = !!on; document.body.classList.toggle('cmt-placing', placing); if (cfg && cfg.button) cfg.button.classList.toggle('on', placing); if (!placing) hideGhost(); }

  return {
    init(config) {
      cfg = config;
      author = (() => { try { return localStorage.getItem('xcomment-author') || ''; } catch (e) { return ''; } })();
      store = config.store || makeLocalStore(config.context);
      const st = config.stage;
      if (!$('cmt-layer')) { const l = document.createElement('div'); l.id = 'cmt-layer'; st.appendChild(l); }
      if (!$('cmt-panel')) {
        const p = document.createElement('div'); p.id = 'cmt-panel'; st.appendChild(p);
        p.addEventListener('pointerdown', e => e.stopPropagation());
        p.addEventListener('click', e => e.stopPropagation());
      }
      if (!$('cmt-ghost')) {   // a ring at the cursor; a pointer rotates toward the nearest item
        // (append to the stage, not #cmt-layer, since renderMarkers() rewrites that layer)
        const g = document.createElement('div'); g.id = 'cmt-ghost'; g.style.display = 'none';
        g.innerHTML = '<div class="pin"></div>';   // the marker pin, minus the number
        st.appendChild(g);
      }
      config.svg.addEventListener('pointermove', e => {
        if (!placing) return;
        const a = cfg.resolveAnchor(e.clientX, e.clientY); if (!a) { hideGhost(); return; }
        const c = cfg.project(a.x, a.y), g = $('cmt-ghost'); if (!g) return;
        g.style.left = c.sx + 'px'; g.style.top = c.sy + 'px'; g.style.display = 'block';
        const pin = g.querySelector('.pin');
        if (a.tx != null) { const t = cfg.project(a.tx, a.ty);   // rotate the teardrop's point toward the item
          pin.style.transform = 'rotate(' + (Math.atan2(t.sy - c.sy, t.sx - c.sx) * 180 / Math.PI - 45) + 'deg)'; g.classList.remove('circle'); }
        else { pin.style.transform = 'none'; g.classList.add('circle'); }   // nothing near → a plain circle
      });
      config.svg.addEventListener('pointerleave', () => hideGhost());
      if (config.button) config.button.onclick = () => setPlacing(!placing);
      if (config.onViewChange) config.onViewChange(renderMarkers);
      document.addEventListener('keydown', e => { if (e.key === 'Escape') { if (placing) setPlacing(false); else closePanel(); } });
      reload();
    },
    isPlacing: () => placing,
    place(cx, cy) { if (!placing) return; const a = cfg.resolveAnchor(cx, cy); if (!a) return; setPlacing(false); openDraft(a); },
    reproject() { renderMarkers(); },
    setContext() { closePanel(); reload(); },   // call after the storage context (e.g. sheet) changes
    setStore(s) { store = s; reload(); },
    toggle() { setPlacing(!placing); },
  };
})();
"""
