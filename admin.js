/* MAVE admin dashboard — plain JS. Talks to /api/v1/admin/* with the X-Admin-Key header (session storage only). */
(function () {
  "use strict";
  var API = "/api/v1/admin";
  var key = null;
  try { key = sessionStorage.getItem("mave.adminKey"); } catch (e) { /* ignore */ }

  function $(id) { return document.getElementById(id); }
  function el(tag, attrs) {
    var n = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined || v === false) return;
      if (k === "class") n.className = v; else if (k === "text") n.textContent = v;
      else if (k === "on") Object.keys(v).forEach(function (ev) { n.addEventListener(ev, v[ev]); });
      else n.setAttribute(k, v === true ? "" : v);
    });
    (function add(list) {
      list.forEach(function (c) {
        if (c === null || c === undefined || c === false) return;
        if (Array.isArray(c)) return add(c); // lists of rows/cells
        n.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
      });
    })([].slice.call(arguments, 2));
    return n;
  }
  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); return n; }
  var toastTimer;
  function toast(m) { var n = $("toast"); n.textContent = m; n.hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(function () { n.hidden = true; }, 2800); }
  function when(ts) { return ts ? new Date(ts * 1000).toLocaleString() : "—"; }

  function call(path, opt) {
    opt = opt || {};
    var headers = { "X-Admin-Key": key || "", Accept: "application/json" };
    if (opt.body !== undefined) headers["Content-Type"] = "application/json";
    return fetch(API + path, { method: opt.method || "GET", headers: headers, body: opt.body === undefined ? undefined : JSON.stringify(opt.body), credentials: "omit" })
      .catch(function () { throw { status: 0, detail: "No connection to the server" }; })
      .then(function (r) {
        return r.text().then(function (txt) {
          var d = null; try { d = txt ? JSON.parse(txt) : null; } catch (e) { /* not JSON */ }
          if (!r.ok) { if (r.status === 403) signOut(true); throw { status: r.status, detail: (d && d.detail) || ("HTTP " + r.status) }; }
          return d;
        });
      });
  }
  function fail(e) { toast(typeof e.detail === "string" ? e.detail : "Error"); }

  // ---- sign in / out
  function signOut(expired) {
    key = null; try { sessionStorage.removeItem("mave.adminKey"); } catch (e) { /* ignore */ }
    $("panel").hidden = true; $("adminNav").hidden = true; $("keyForm").hidden = false;
    $("keyMsg").textContent = expired ? "Admin key rejected or admin API disabled (set MAVE_ADMIN_KEY)." : "";
  }
  function signIn() {
    $("keyForm").hidden = true; $("adminNav").hidden = false; $("panel").hidden = false; show("overview");
  }
  $("keyForm").addEventListener("submit", function (e) {
    e.preventDefault(); key = $("adminKey").value; $("adminKey").value = "";
    call("/health").then(function () { try { sessionStorage.setItem("mave.adminKey", key); } catch (x) { /* ignore */ } signIn(); })
      .catch(function (err) { key = null; $("keyMsg").textContent = err.status === 403 ? "Wrong key, or admin API disabled." : String(err.detail); });
  });
  $("adminLogout").addEventListener("click", function () { signOut(false); });
  document.querySelectorAll(".tab").forEach(function (b) { b.addEventListener("click", function () { show(b.getAttribute("data-tab")); }); });

  var tabs = { overview: overview, crawler: crawler, analytics: analytics, users: users, audit: audit };
  function show(name) {
    document.querySelectorAll(".tab").forEach(function (b) { b.setAttribute("aria-selected", String(b.getAttribute("data-tab") === name)); });
    var p = clear($("panel")); p.appendChild(el("p", { class: "state", text: "Loading…" }));
    tabs[name](p);
  }

  function table(headers, rows, empty) {
    if (!rows.length) return el("p", { class: "note", text: empty || "Nothing to show." });
    var thead = el("thead", {}, el("tr", {}, headers.map(function (h) { return el("th", { text: h }); })));
    var tbody = el("tbody", {}, rows.map(function (r) { return el("tr", {}, r.map(function (c) { return el("td", {}, c instanceof Node ? c : document.createTextNode(c === null || c === undefined ? "—" : String(c))); })); }));
    return el("div", { class: "scroll" }, el("table", { class: "tbl" }, thead, tbody));
  }
  function card(label, value) { return el("div", { class: "card" }, el("b", { text: String(value) }), el("span", { text: label })); }
  function pill(ok, yes, no) { return el("span", { class: "pill " + (ok ? "ok" : "bad"), text: ok ? yes : no }); }

  // ---- overview: health + index statistics
  function overview(p) {
    Promise.all([call("/health"), call("/stats")]).then(function (r) {
      var h = r[0], s = r[1]; clear(p);
      p.appendChild(el("div", { class: "panel" }, el("h2", { text: "System health" }),
        el("div", { class: "cards" },
          card("version", h.version), card("environment", h.environment), card("schema", "v" + h.schema_version), card("database", h.database)),
        el("p", {}, "Web search (Brave): ", pill(h.providers.web_search, "configured", "not configured"), "  AI provider: ",
          pill(!!h.providers.ai, h.providers.ai || "", "not configured"), "  SMTP: ", pill(h.providers.smtp, "configured", "not configured")),
        el("p", { class: "note", text: "CORS origins: " + h.config.cors_origins + " · trust proxy: " + h.config.trust_proxy + " · public URL set: " + h.config.public_url_set })));
      var labels = { documents: "Documents indexed", domains: "Domains", users: "Users", queries_logged: "Queries logged", frontier_pending: "Frontier pending", frontier_done: "Frontier done", frontier_failed: "Failed", frontier_blocked: "Blocked (robots/SSRF)", frontier_duplicate: "Duplicates", frontier_gone: "Gone (404/410)", frontier_retrying: "Retrying", documents_due_recrawl: "Due for recrawl" };
      var cards = el("div", { class: "cards" });
      Object.keys(labels).forEach(function (k) { if (k in s) cards.appendChild(card(labels[k], s[k])); });
      p.appendChild(el("div", { class: "panel" }, el("h2", { text: "Index statistics" }), cards,
        el("p", { class: "note", text: "Prometheus metrics: GET /api/v1/admin/metrics with the X-Admin-Key header." })));
    }).catch(function (e) { clear(p); p.appendChild(el("p", { class: "state err", text: String(e.detail) })); });
  }

  // ---- crawler: start a crawl, manage seeds, inspect errors
  function crawler(p) {
    Promise.all([call("/seeds"), call("/crawl-errors?limit=100")]).then(function (r) {
      clear(p);
      var seeds = r[0].items, errs = r[1].items;
      var url = el("input", { type: "url", placeholder: "https://example.org/", "aria-label": "Seed URL", maxlength: "2000" });
      p.appendChild(el("div", { class: "panel" }, el("h2", { text: "Seeds" }),
        el("form", { class: "inline", on: { submit: function (e) { e.preventDefault(); call("/seeds", { method: "POST", body: { url: url.value.trim() } }).then(function () { toast("Seed added"); show("crawler"); }).catch(fail); } } }, url, el("button", { class: "primary", type: "submit", text: "Add" })),
        table(["URL", "Enabled", "Added", ""], seeds.map(function (s) {
          return [s.url, pill(s.enabled, "on", "off"), when(s.added_at), el("span", { class: "row" },
            el("button", { class: "ghost", text: s.enabled ? "Disable" : "Enable", on: { click: function () { call("/seeds", { method: "PUT", body: { url: s.url, enabled: !s.enabled } }).then(function () { show("crawler"); }).catch(fail); } } }),
            el("button", { class: "ghost", text: "Remove", on: { click: function () { if (confirm("Remove " + s.url + "?")) call("/seeds?url=" + encodeURIComponent(s.url), { method: "DELETE" }).then(function () { show("crawler"); }).catch(fail); } } }))];
        }), "No seeds yet.")));

      var pages = el("input", { type: "number", value: "100", min: "1", max: "5000" }), depth = el("input", { type: "number", value: "2", min: "0", max: "5" }), delay = el("input", { type: "number", value: "1.0", min: "0.2", max: "10", step: "0.1" });
      p.appendChild(el("form", { class: "panel", on: { submit: function (e) {
        e.preventDefault();
        call("/crawl", { method: "POST", body: { max_pages: Number(pages.value), max_depth: Number(depth.value), delay: Number(delay.value) } })
          .then(function (d) { toast("Crawl started with " + d.seeds + " seed(s)"); }).catch(fail);
      } } }, el("h2", { text: "Start a crawl (uses the enabled seeds)" }),
        el("div", { class: "row" }, el("label", {}, el("span", { text: "Max pages" }), pages), el("label", {}, el("span", { text: "Max depth" }), depth), el("label", {}, el("span", { text: "Delay (s)" }), delay)),
        el("button", { class: "primary", type: "submit", text: "Start crawl" }),
        el("p", { class: "note", text: "Respects robots.txt, per-domain delay and SSRF protection. Runs in the background; watch Overview and the errors below." })));

      p.appendChild(el("div", { class: "panel" }, el("h2", { text: "Crawl errors and retries" }),
        table(["URL", "Status", "Attempts", "Last error", "Added"], errs.map(function (x) { return [x.url, x.status, x.attempts, x.last_error, when(x.added_at)]; }), "No crawl errors.")));
    }).catch(function (e) { clear(p); p.appendChild(el("p", { class: "state err", text: String(e.detail) })); });
  }

  // ---- analytics
  function chart(perDay) {
    var W = 600, H = 150, pad = 18, n = perDay.length;
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 " + W + " " + (H + pad)); svg.setAttribute("class", "chart"); svg.setAttribute("role", "img"); svg.setAttribute("aria-label", "Searches per day");
    var max = Math.max.apply(null, perDay.map(function (d) { return d.n; }).concat([1])), bw = W / Math.max(n, 1);
    perDay.forEach(function (d, i) {
      var h = Math.round((d.n / max) * (H - 14)), r = document.createElementNS(svg.namespaceURI, "rect");
      r.setAttribute("x", String(i * bw + 3)); r.setAttribute("y", String(H - h)); r.setAttribute("width", String(Math.max(bw - 6, 2))); r.setAttribute("height", String(h));
      var tt = document.createElementNS(svg.namespaceURI, "title"); tt.textContent = d.day + ": " + d.n; r.appendChild(tt); svg.appendChild(r);
      if (n <= 14 || i % Math.ceil(n / 10) === 0) { var tx = document.createElementNS(svg.namespaceURI, "text"); tx.setAttribute("x", String(i * bw + bw / 2)); tx.setAttribute("y", String(H + 12)); tx.setAttribute("text-anchor", "middle"); tx.textContent = d.day.slice(5); svg.appendChild(tx); }
    });
    return svg;
  }
  function analytics(p) {
    call("/analytics?days=14").then(function (a) {
      clear(p);
      p.appendChild(el("div", { class: "panel" }, el("h2", { text: "Searches, last " + a.days + " days (" + a.total + ")" }), a.per_day.length ? chart(a.per_day) : el("p", { class: "note", text: "No queries logged yet." })));
      var top = a.top_queries, mx = Math.max.apply(null, top.map(function (x) { return x.n; }).concat([1]));
      p.appendChild(el("div", { class: "panel" }, el("h2", { text: "Top queries" }),
        table(["Query", "Count", ""], top.map(function (x) { return [x.query, x.n, el("meter", { min: "0", max: String(mx), value: String(x.n) })]; }), "No queries yet.")));
      p.appendChild(el("div", { class: "panel" }, el("h2", { text: "Index: top domains" }), table(["Domain", "Documents"], a.top_domains.map(function (d) { return [d.domain, d.documents]; }), "Index is empty."),
        el("h2", { text: "Index: languages" }), table(["Language", "Documents"], a.languages.map(function (d) { return [d.lang, d.documents]; }), "Index is empty.")));
    }).catch(function (e) { clear(p); p.appendChild(el("p", { class: "state err", text: String(e.detail) })); });
  }

  // ---- users and roles (every action is audited with the acting admin)
  function users(p) {
    var q = "";
    function load() {
      call("/users?limit=50&q=" + encodeURIComponent(q)).then(function (d) {
        clear(p);
        var input = el("input", { type: "search", value: q, placeholder: "Search by email", "aria-label": "Search users", maxlength: "100" });
        p.appendChild(el("div", { class: "panel" }, el("h2", { text: "Users (" + d.total + ")" }),
          el("form", { class: "inline", on: { submit: function (e) { e.preventDefault(); q = input.value.trim(); load(); } } }, input, el("button", { type: "submit", text: "Search" })),
          table(["ID", "Email", "Verified", "Status", "Role", "Active sessions", "Created", "", "", ""], d.items.map(function (u) {
            var isAdmin = u.role === "admin";
            return [u.id, u.email, pill(u.email_verified, "yes", "no"), pill(!u.disabled, "active", "disabled"),
              el("span", { class: "pill" + (isAdmin ? " ok" : ""), text: isAdmin ? "admin" : "user" }), u.active_sessions, when(u.created_at),
              el("button", { class: "ghost", text: isAdmin ? "Remove admin" : "Make admin", on: { click: function () { if (confirm((isAdmin ? "Remove admin role from " : "Make admin: ") + u.email + "?")) call("/users/" + u.id + "/role", { method: "POST", body: { role: isAdmin ? "user" : "admin" } }).then(function () { toast(isAdmin ? "Admin role removed" : "Admin role granted"); load(); }).catch(fail); } } }),
              el("button", { class: "ghost", text: u.disabled ? "Enable" : "Disable", on: { click: function () { if (confirm((u.disabled ? "Enable " : "Disable ") + u.email + "?")) call("/users/" + u.id + (u.disabled ? "/enable" : "/disable"), { method: "POST" }).then(function () { toast(u.disabled ? "Account enabled" : "Account disabled"); load(); }).catch(fail); } } }),
              el("button", { class: "ghost", text: "End sessions", on: { click: function () { if (confirm("End all sessions of " + u.email + "?")) call("/users/" + u.id + "/revoke-sessions", { method: "POST" }).then(function (r) { toast(r.revoked + " session(s) ended"); load(); }).catch(fail); } } })];
          }), "No users.")));
      }).catch(function (e) { clear(p); p.appendChild(el("p", { class: "state err", text: String(e.detail) })); });
    }
    load();
  }

  function audit(p) {
    call("/audit?limit=200").then(function (d) {
      clear(p);
      p.appendChild(el("div", { class: "panel" }, el("h2", { text: "Audit log" }), table(["When", "Actor", "Action", "Detail"], d.items.map(function (x) { return [when(x.ts), x.actor, x.action, x.detail]; }), "No administrative actions yet.")));
    }).catch(function (e) { clear(p); p.appendChild(el("p", { class: "state err", text: String(e.detail) })); });
  }

  if (key) call("/health").then(signIn).catch(function () { signOut(false); });
})();
