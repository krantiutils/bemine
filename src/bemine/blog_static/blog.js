// ampixa.com/blog: listing filter + grid/map toggle, contents highlight, read-aloud player. No dependencies.
(function () {
  "use strict";
  const $ = id => document.getElementById(id);
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const reduced = () => matchMedia("(prefers-reduced-motion:reduce)").matches;

  // ---- listing ----------------------------------------------------------------------------------------------
  const dataEl = $("posts-data");
  if (dataEl) {
    const D = JSON.parse(dataEl.textContent), POSTS = D.posts, PROJ = D.projects;
    const q = new URLSearchParams(location.search);
    let filter = PROJ[q.get("project")] ? q.get("project") : "all";
    let view = q.get("view") === "map" ? "map" : "grid";
    const save = () => {
      const u = new URLSearchParams();
      if (view === "map") u.set("view", "map");
      if (filter !== "all") u.set("project", filter);
      history.replaceState(null, "", location.pathname + (u.toString() ? "?" + u : ""));
    };
    function chips() {
      const counts = {};
      POSTS.forEach(x => counts[x.p] = (counts[x.p] || 0) + 1);
      const all = [["all", "all", POSTS.length], ...Object.keys(PROJ).map(k => [k, PROJ[k], counts[k] || 0])];
      $("chips").innerHTML = all.map(([k, l, n]) =>
        `<button class="chip" data-k="${esc(k)}" aria-pressed="${k === filter}">${esc(l)}<span class="n">${n}</span></button>`).join("");
      $("chips").querySelectorAll(".chip").forEach(b => b.onclick = () => { filter = b.dataset.k; save(); render(); });
    }
    function grid() {
      document.querySelectorAll("#grid .card").forEach(li => li.hidden = filter !== "all" && li.dataset.p !== filter);
    }
    function map() {
      const keys = Object.keys(PROJ), W = 1120, rowH = 46, gap = 22;
      let y = 24;
      const groups = keys.map(k => {
        const ps = POSTS.filter(x => x.p === k), h = ps.length * rowH, g = { k, ps, top: y, mid: y + h / 2 - rowH / 2 + 16 };
        y += h + gap; return g;
      }).filter(g => g.ps.length);
      const H = Math.max(y, 80), rootY = H / 2, rx = 24, px = 210, qx = 470;
      const curve = (x0, y0, x1, y1) => { const m = (x0 + x1) / 2; return `M${x0} ${y0} C${m} ${y0},${m} ${y1},${x1} ${y1}`; };
      let s = "";
      groups.forEach(g => {
        const on = filter === g.k;
        s += `<path class="edge${on ? " on" : ""}" d="${curve(rx + 150, rootY, px, g.mid)}"/>`;
        g.ps.forEach((x, i) => { s += `<path class="edge${on ? " on" : ""}" d="${curve(px + 180, g.mid, qx, g.top + i * rowH + 16)}"/>`; });
      });
      s += `<g class="pill${filter === "all" ? " on" : ""}" data-k="all" tabindex="0" role="button"><rect x="${rx}" y="${rootY - 16}" width="150" height="32"/><text x="${rx + 14}" y="${rootY + 4}">blog ${POSTS.length}</text></g>`;
      groups.forEach(g => {
        const on = filter === g.k;
        s += `<g class="pill${on ? " on" : ""}" data-k="${esc(g.k)}" tabindex="0" role="button"><rect x="${px}" y="${g.mid - 16}" width="180" height="32"/><text x="${px + 12}" y="${g.mid + 4}">${esc(PROJ[g.k])} ${g.ps.length}</text></g>`;
        g.ps.forEach((x, i) => {
          const yy = g.top + i * rowH, dim = filter !== "all" && !on, t = x.t.length > 72 ? x.t.slice(0, 70) + "…" : x.t;
          s += `<a href="${esc(x.url)}"><g class="post${on ? " on" : ""}${dim ? " dim" : ""}"><rect x="${qx}" y="${yy}" width="${W - qx - 24}" height="34"/>` +
            `<text class="g" x="${qx + 10}" y="${yy + 25}">${esc(x.g)}</text><text x="${qx + 56}" y="${yy + 22}">${esc(t)}</text>` +
            `<text x="${W - 120}" y="${yy + 22}" style="font:11px var(--mono)">${esc(x.d)}</text></g></a>`;
        });
      });
      const svg = $("svg");
      svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.setAttribute("width", W); svg.setAttribute("height", H); svg.innerHTML = s;
      svg.querySelectorAll(".pill").forEach(p => {
        const go = () => { filter = p.dataset.k; save(); render(); };
        p.onclick = go; p.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } };
      });
      $("outline").innerHTML = groups.filter(g => filter === "all" || g.k === filter).map(g =>
        `<h3>${esc(PROJ[g.k])}</h3><ul>${g.ps.map(x => `<li><a href="${esc(x.url)}">${esc(x.t)}</a></li>`).join("")}</ul>`).join("");
    }
    function render() {
      chips();
      $("v-grid").setAttribute("aria-pressed", view === "grid"); $("v-map").setAttribute("aria-pressed", view === "map");
      $("grid").hidden = view !== "grid"; $("map").hidden = view !== "map"; $("legend").hidden = view !== "map";
      view === "grid" ? grid() : map();
    }
    $("v-grid").onclick = () => { view = "grid"; save(); render(); };
    $("v-map").onclick = () => { view = "map"; save(); render(); };
    render();
  }

  // ---- post: contents highlight -----------------------------------------------------------------------------
  const prose = $("prose");
  if (!prose) return;
  const heads = [...prose.querySelectorAll("h2[id]")], tocLinks = [...document.querySelectorAll(".toc a")];
  if (heads.length && tocLinks.length) {
    const mark = () => {
      let cur = heads[0];
      heads.forEach(h => { if (h.getBoundingClientRect().top < 120) cur = h; });
      tocLinks.forEach(a => a.classList.toggle("on", a.getAttribute("href") === "#" + cur.id));
    };
    addEventListener("scroll", mark, { passive: true }); mark();
  }

  // ---- post: read-aloud (one clip per [data-a] block, audio/<n>.mp3) -------------------------------------
  const bar = $("listen");
  if (!bar) return;
  const L = { q: [...prose.querySelectorAll("[data-a]")], i: 0, rate: 1, au: new Audio() };
  L.au.preload = "auto";
  const play = $("l-play"), info = $("l-info"), prog = $("l-prog");
  const setPlaying = on => { play.setAttribute("aria-pressed", on); play.textContent = on ? "❚❚ pause" : (L.au.currentTime > 0 ? "▶ resume" : "▶ listen"); };
  function headOf(el) { let h = el; while (h && h.tagName !== "H2") h = h.previousElementSibling || h.parentElement; return h && h.id ? h.textContent : "intro"; }
  function clear() { prose.querySelectorAll(".reading").forEach(e => e.classList.remove("reading")); }
  function go() {
    const el = L.q[L.i];
    if (!el) { stop(); return; }
    clear(); el.classList.add("reading");
    el.scrollIntoView({ block: "center", behavior: reduced() ? "auto" : "smooth" });
    prog.style.width = (100 * L.i / L.q.length) + "%"; info.textContent = headOf(el);
    L.au.src = "audio/" + el.dataset.a + ".mp3"; L.au.playbackRate = L.rate;
    L.au.play().then(() => setPlaying(true)).catch(() => { info.textContent = "press listen again to start audio"; setPlaying(false); });
  }
  function stop() { L.au.pause(); L.au.removeAttribute("src"); L.i = 0; prog.style.width = "0"; clear(); setPlaying(false); info.textContent = bar.dataset.info; }
  L.au.onended = () => { L.i++; go(); };
  L.au.onerror = () => { if (!L.au.getAttribute("src")) return; info.textContent = "clip " + L.i + " did not load; skipping"; L.i++; go(); };
  play.onclick = () => {
    if (!L.au.paused) { L.au.pause(); setPlaying(false); return; }
    if (L.au.getAttribute("src") && L.au.currentTime > 0 && !L.au.ended) { L.au.play(); setPlaying(true); return; }
    go();
  };
  $("l-speed").onclick = () => { L.rate = L.rate >= 1.5 ? .75 : +(L.rate + .25).toFixed(2); L.au.playbackRate = L.rate; $("l-speed").textContent = L.rate + "×"; };
  // a small play button per block; clicking or selecting text never starts audio
  L.q.forEach(el => {
    const b = document.createElement("button");
    b.className = "pp"; b.type = "button"; b.textContent = "▶"; b.setAttribute("aria-label", "Listen from here");
    b.onclick = e => { e.preventDefault(); L.i = L.q.indexOf(el); go(); };
    el.prepend(b);
  });
})();
