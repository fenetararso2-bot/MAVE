/* MAVE web app — plain JS, no build step. All text goes through textContent; URLs only via MaveCore.safeUrl. */
(function () {
  "use strict";
  var C = window.MaveCore;
  var API = "/api/v1";
  var TABS = ["web", "news", "images", "videos", "tech", "ai"];
  var MAX_PAGE = 20; // server limit for /search?page=

  // ------------------------------------------------------------------ storage (every access guarded: private mode may throw)
  var store = {
    get: function (k, d) { try { var v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
    set: function (k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* quota / private mode */ } },
    del: function (k) { try { localStorage.removeItem(k); } catch (e) { /* ignore */ } }
  };

  var state = {
    lang: store.get("mave.lang", /^om/i.test(navigator.language || "") ? "om" : "en"),
    theme: store.get("mave.theme", "auto"),
    access: null, // access token lives in memory only
    refresh: store.get("mave.rt", null),
    email: store.get("mave.email", null),
    verified: store.get("mave.verified", false),
    history: store.get("mave.history", []),
    saved: store.get("mave.saved", [])
  };

  function t(key) {
    var d = window.MAVE_I18N;
    return (d[state.lang] && d[state.lang][key]) || d.en[key] || key;
  }

  // ------------------------------------------------------------------ DOM helpers
  function $(id) { return document.getElementById(id); }
  function el(tag, attrs) {
    var n = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined || v === false) return;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k === "on") Object.keys(v).forEach(function (ev) { n.addEventListener(ev, v[ev]); });
      else n.setAttribute(k, v === true ? "" : v);
    });
    (function add(list) {
      list.forEach(function (c) {
        if (c === null || c === undefined || c === false) return;
        if (Array.isArray(c)) return add(c);
        n.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
      });
    })([].slice.call(arguments, 2));
    return n;
  }
  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); return n; }
  var toastTimer;
  function toast(msg) {
    var n = $("toast");
    n.textContent = msg; n.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { n.hidden = true; }, 2600);
  }

  function applyTheme() {
    var r = document.documentElement;
    if (state.theme === "light" || state.theme === "dark") r.setAttribute("data-theme", state.theme);
    else r.removeAttribute("data-theme");
  }
  function applyI18n() {
    document.documentElement.lang = state.lang;
    document.querySelectorAll("[data-i18n]").forEach(function (n) { n.textContent = t(n.getAttribute("data-i18n")); });
    document.querySelectorAll("[data-i18n-placeholder]").forEach(function (n) { n.setAttribute("placeholder", t(n.getAttribute("data-i18n-placeholder"))); });
    document.querySelectorAll("[data-i18n-title]").forEach(function (n) { n.setAttribute("title", t(n.getAttribute("data-i18n-title"))); });
    $("authBtn").textContent = state.email ? t("logout") : t("login");
  }

  // ------------------------------------------------------------------ API with transparent token refresh
  function ApiError(status, detail) { this.status = status; this.detail = detail; }
  var refreshing = null;

  function rawFetch(path, opt) {
    var headers = { Accept: "application/json" };
    if (opt.body !== undefined) headers["Content-Type"] = "application/json";
    if (opt.auth && state.access) headers.Authorization = "Bearer " + state.access;
    return fetch(API + path, { method: opt.method || "GET", headers: headers, body: opt.body === undefined ? undefined : JSON.stringify(opt.body), credentials: "omit" })
      .catch(function () { throw new ApiError(0, "network"); });
  }

  function doRefresh() {
    if (!state.refresh) return Promise.reject(new ApiError(401, "no session"));
    if (!refreshing) {
      refreshing = rawFetch("/auth/refresh", { method: "POST", body: { refresh_token: state.refresh } })
        .then(function (r) { return r.ok ? r.json() : Promise.reject(new ApiError(r.status, "refresh")); })
        .then(function (d) { setSession(d); return d; })
        .catch(function (e) { if (e.status === 401 || e.status === 403) clearSession(); throw e; })
        .then(function (d) { refreshing = null; return d; }, function (e) { refreshing = null; throw e; });
    }
    return refreshing;
  }

  function api(path, opt) {
    opt = opt || {};
    function attempt(retry) {
      return rawFetch(path, opt).then(function (r) {
        if (r.status === 401 && opt.auth && retry && state.refresh) {
          return doRefresh().then(function () { return attempt(false); });
        }
        return r.text().then(function (txt) {
          var data = null;
          try { data = txt ? JSON.parse(txt) : null; } catch (e) { /* non-JSON error page */ }
          if (!r.ok) throw new ApiError(r.status, data && data.detail);
          return data;
        });
      });
    }
    return opt.auth && !state.access && state.refresh ? doRefresh().then(function () { return attempt(true); }) : attempt(true);
  }

  function setSession(d) {
    state.access = d.token || state.access;
    if (d.refresh_token) { state.refresh = d.refresh_token; store.set("mave.rt", state.refresh); }
    if (d.email) { state.email = d.email; store.set("mave.email", d.email); }
    if (typeof d.email_verified === "boolean") { state.verified = d.email_verified; store.set("mave.verified", d.email_verified); }
    applyI18n();
  }
  function clearSession() {
    state.access = null; state.refresh = null; state.email = null; state.verified = false;
    store.del("mave.rt"); store.del("mave.email"); store.del("mave.verified");
    applyI18n();
  }

  // ------------------------------------------------------------------ history + saved (local first, mirrored to the server when logged in)
  function loggedIn() { return !!state.email; }
  function quietApi(path, opt) { if (loggedIn()) api(path, opt).catch(function () { /* local copy is the source of truth offline */ }); }

  function addHistory(q) {
    state.history = C.pushHistory(state.history, q, 200);
    store.set("mave.history", state.history);
    quietApi("/me/history", { method: "POST", auth: true, body: { query: q.slice(0, 200) } });
  }
  function isSaved(url) { return state.saved.some(function (s) { return s.url === url; }); }
  function toggleSaved(item) {
    if (isSaved(item.url)) {
      state.saved = state.saved.filter(function (s) { return s.url !== item.url; });
      quietApi("/me/saved?url=" + encodeURIComponent(item.url), { method: "DELETE", auth: true });
      toast(t("removed_ok"));
    } else {
      var rec = { title: item.title, url: item.url, snippet: item.snippet || "", thumbnail: item.thumbnail || null };
      state.saved.unshift(rec);
      quietApi("/me/saved", { method: "POST", auth: true, body: { title: rec.title.slice(0, 300), url: rec.url.slice(0, 2000), snippet: rec.snippet.slice(0, 1000), thumbnail: rec.thumbnail } });
      toast(t("saved_ok"));
    }
    store.set("mave.saved", state.saved);
  }

  /** After login: merge server lists into the local ones and upload a few local-only items (capped: API rate limit). */
  function syncAfterLogin() {
    return Promise.all([api("/me/history", { auth: true }), api("/me/saved", { auth: true })]).then(function (res) {
      var srvHist = res[0].items || [], srvSaved = res[1].items || [];
      var localOnlyHist = state.history.filter(function (q) { return srvHist.indexOf(q) < 0; }).slice(0, 15);
      var srvUrls = {};
      srvSaved.forEach(function (s) { srvUrls[s.url] = true; });
      var localOnlySaved = state.saved.filter(function (s) { return !srvUrls[s.url]; }).slice(0, 15);
      state.history = srvHist.concat(state.history.filter(function (q) { return srvHist.indexOf(q) < 0; })).slice(0, 200);
      var seen = {}, merged = [];
      C.addUnique(merged, srvSaved.concat(state.saved), seen);
      state.saved = merged;
      store.set("mave.history", state.history); store.set("mave.saved", state.saved);
      localOnlyHist.reverse().forEach(function (q) { api("/me/history", { method: "POST", auth: true, body: { query: q } }).catch(function () {}); });
      localOnlySaved.forEach(function (s) { api("/me/saved", { method: "POST", auth: true, body: { title: s.title.slice(0, 300), url: s.url, snippet: (s.snippet || "").slice(0, 1000), thumbnail: s.thumbnail } }).catch(function () {}); });
    }).catch(function () { /* stay on local data */ });
  }

  // ------------------------------------------------------------------ views
  var view = $("view");
  var searchToken = 0; // invalidates in-flight searches when the user navigates

  function go(route, params) { location.hash = C.buildHash(route, params); }
  function errorState(e, retry) {
    var key = C.errorKey(e && typeof e.status === "number" ? e.status : 500);
    return el("div", { class: "state err" }, el("p", { text: t(key) }), retry ? el("button", { on: { click: retry }, text: t("retry") }) : null);
  }

  function renderHome() {
    clear(view);
    $("q").value = "";
    var chips = el("div", { class: "chips" });
    view.appendChild(el("section", { class: "hero" }, el("h1", { text: "MAVE" }), el("p", { text: t("tagline") })));
    if (state.history.length) {
      view.appendChild(el("h2", { text: t("recent"), class: "state" }));
      var rc = el("div", { class: "chips" });
      state.history.slice(0, 6).forEach(function (q) { rc.appendChild(chip(q)); });
      view.appendChild(rc);
    }
    view.appendChild(el("h2", { text: t("trending"), class: "state" }));
    view.appendChild(chips);
    api("/trending").then(function (d) { (d.trending || []).forEach(function (q) { chips.appendChild(chip(q)); }); }).catch(function () {});
  }
  function chip(q) { return el("button", { class: "chip", on: { click: function () { go("search", { q: q, type: "web" }); } }, text: q }); }

  // ---- search
  function renderSearch(params) {
    var q = (params.q || "").trim();
    var type = TABS.indexOf(params.type) >= 0 ? params.type : "web";
    if (!q) return renderHome();
    $("q").value = q;
    var token = ++searchToken;
    clear(view);
    var tabs = el("div", { class: "tabs", role: "tablist" });
    TABS.forEach(function (k) {
      tabs.appendChild(el("button", { role: "tab", "aria-selected": String(k === type), on: { click: function () { go("search", { q: q, type: k }); } }, text: t(k) }));
    });
    view.appendChild(tabs);
    addHistory(q);
    if (type === "ai") return renderAi(q, token);

    var body = el("div"); var did = el("div", { class: "did", hidden: true });
    var list = el("div", { class: type === "images" ? "grid" : "" });
    var foot = el("div", { class: "more" });
    view.appendChild(did); view.appendChild(body); body.appendChild(list); view.appendChild(foot);

    var page = 0, loading = false, done = false, seen = {}, total = 0, observer = null;
    if (type === "web" || type === "tech") {
      body.insertBefore(el("p", { class: "did" }, el("button", { class: "ghost", on: { click: function () { go("search", { q: q, type: "ai" }); } }, text: "✨ " + t("ai_ask") })), list);
    }

    function skeletons(n) { clear(foot); for (var i = 0; i < n; i++) foot.appendChild(el("div", { class: "skeleton" })); }
    function loadMore() {
      if (loading || done || token !== searchToken) return;
      loading = true; page++;
      if (page === 1) skeletons(4); else clear(foot);
      api("/search?q=" + encodeURIComponent(q) + "&type=" + type + "&page=" + page + "&lang=" + state.lang).then(function (d) {
        if (token !== searchToken) return;
        loading = false; clear(foot);
        if (page === 1 && d.corrected && d.corrected.toLowerCase() !== q.toLowerCase()) {
          did.hidden = false; clear(did);
          did.appendChild(document.createTextNode(t("did_you_mean") + " "));
          did.appendChild(el("a", { href: C.buildHash("search", { q: d.corrected, type: type }), text: d.corrected }));
        }
        // Advisory category hint from the backend ("intent"); only a known tab other than the current one is offered.
        if (page === 1 && d.intent && d.intent !== type && TABS.indexOf(d.intent) >= 0 && d.intent !== "ai") {
          did.hidden = false;
          did.appendChild(el("div", {}, t("try_tab") + " ", el("a", { href: C.buildHash("search", { q: q, type: d.intent }), text: t(d.intent) })));
        }
        var added = C.addUnique([], d.results || [], seen);
        total += added.length;
        added.forEach(function (r) { var n = renderResult(r, type); if (n) list.appendChild(n); });
        if (!added.length || page >= MAX_PAGE) {
          done = true;
          foot.appendChild(el("p", { class: "state", text: total ? t("end_results") : t("no_results") }));
        } else {
          foot.appendChild(el("button", { on: { click: loadMore }, text: t("load_more") }));
          watch();
        }
      }).catch(function (e) {
        if (token !== searchToken) return;
        loading = false; page--; clear(foot);
        foot.appendChild(errorState(e, loadMore));
      });
    }
    function watch() {
      if (observer) observer.disconnect();
      if (!("IntersectionObserver" in window)) return;
      observer = new IntersectionObserver(function (es) { if (es[0].isIntersecting) loadMore(); }, { rootMargin: "300px" });
      observer.observe(foot);
    }
    loadMore();
  }

  function renderResult(r, type) {
    var href = C.safeUrl(r.url);
    if (!href) return null; // never render a link we cannot prove is http(s)
    var thumb = C.safeUrl(r.thumbnail);
    var title = r.title || C.domainOf(href) || href;
    if (type === "images") {
      return el("a", { href: href, target: "_blank", rel: "noopener noreferrer", title: title },
        thumb ? el("img", { src: thumb, alt: title, loading: "lazy", referrerpolicy: "no-referrer" }) : null,
        el("span", { text: C.domainOf(href) }));
    }
    var saveBtn = el("button", { class: "ghost" });
    function paintSave() { saveBtn.textContent = isSaved(href) ? "★ " + t("unsave") : "☆ " + t("save"); }
    paintSave();
    saveBtn.addEventListener("click", function () { toggleSaved({ title: title, url: href, snippet: r.snippet, thumbnail: thumb }); paintSave(); });
    var box = el("article", { class: "res" + (thumb && (type === "videos" || type === "news") ? " with-thumb" : "") });
    var text = el("div", { class: "txt" },
      el("div", { class: "site", text: C.domainOf(href) }),
      el("h3", {}, el("a", { href: href, target: "_blank", rel: "noopener noreferrer", text: title })),
      r.snippet ? el("p", { text: r.snippet }) : null,
      el("div", { class: "acts" }, saveBtn));
    if (thumb && (type === "videos" || type === "news")) box.appendChild(el("img", { src: thumb, alt: "", loading: "lazy", referrerpolicy: "no-referrer" }));
    box.appendChild(text);
    return box;
  }

  // ---- AI answer with numbered citations
  function renderAi(q, token) {
    var card = el("section", { class: "ai", "aria-live": "polite" }, el("h2", { text: "✨ " + t("ai") }), el("p", { class: "state", text: t("ai_thinking") }));
    view.appendChild(card);
    api("/ai/answer", { method: "POST", body: { query: q, lang: state.lang } }).then(function (d) {
      if (token !== searchToken) return;
      clear(card); card.appendChild(el("h2", { text: "✨ " + t("ai") }));
      var cites = d.citations || [];
      if (d.insufficient) {
        card.appendChild(el("p", { class: "warn", text: d.answer || t("ai_insufficient") }));
      } else {
        var ans = el("div", { class: "answer" });
        C.splitCitations(d.answer, cites).forEach(function (p) {
          if (p.text !== undefined) { ans.appendChild(document.createTextNode(p.text)); return; }
          var c = cites.filter(function (x) { return x.n === p.cite; })[0];
          var href = c && C.safeUrl(c.url);
          ans.appendChild(href ? el("a", { class: "cite", href: href, target: "_blank", rel: "noopener noreferrer", title: c.title || "", text: String(p.cite) }) : document.createTextNode("[" + p.cite + "]"));
        });
        card.appendChild(ans);
        if (d.grounded === false) card.appendChild(el("p", { class: "warn", text: t("ai_ungrounded") }));
      }
      if (cites.length) {
        var ol = el("ol");
        cites.forEach(function (c) {
          var href = C.safeUrl(c.url);
          if (!href) return;
          var li = el("li", { value: String(c.n) }, el("a", { href: href, target: "_blank", rel: "noopener noreferrer", text: c.title || C.domainOf(href) }), " ", el("small", { text: C.domainOf(href) }));
          ol.appendChild(li);
        });
        card.appendChild(el("h2", { text: t("sources") })); card.appendChild(ol);
      }
    }).catch(function (e) {
      if (token !== searchToken) return;
      clear(card); card.appendChild(errorState(e, function () { clear(view); renderSearch({ q: q, type: "ai" }); }));
    });
  }

  // ---- saved + history
  function renderSaved() {
    clear(view);
    view.appendChild(el("h1", { text: t("saved") }));
    function paint() {
      var box = view.querySelector(".list") || view.appendChild(el("div", { class: "list" }));
      clear(box);
      if (!state.saved.length) { box.appendChild(el("p", { class: "state", text: t("empty") })); return; }
      state.saved.forEach(function (s) { var n = renderResult({ title: s.title, url: s.url, snippet: s.snippet, thumbnail: s.thumbnail }, "web"); if (n) box.appendChild(n); });
    }
    paint();
    if (loggedIn()) api("/me/saved", { auth: true }).then(function (d) {
      var seen = {}, merged = []; C.addUnique(merged, (d.items || []).concat(state.saved), seen);
      state.saved = merged; store.set("mave.saved", merged); if (location.hash.indexOf("#/saved") === 0) paint();
    }).catch(function () {});
  }
  function renderHistory() {
    clear(view);
    view.appendChild(el("div", { class: "row" }, el("h1", { text: t("history") }), el("button", { class: "ghost", text: t("clear"), on: { click: function () {
      state.history = []; store.set("mave.history", []); quietApi("/me/history", { method: "DELETE", auth: true }); renderHistory();
    } } })));
    var box = el("div", { class: "list" }); view.appendChild(box);
    function paint() {
      clear(box);
      if (!state.history.length) { box.appendChild(el("p", { class: "state", text: t("empty") })); return; }
      state.history.forEach(function (q) {
        box.appendChild(el("div", { class: "res row" },
          el("a", { href: C.buildHash("search", { q: q, type: "web" }), text: q }),
          el("button", { class: "ghost", "aria-label": t("unsave") + " " + q, text: "✕", on: { click: function () {
            state.history = state.history.filter(function (x) { return x !== q; }); store.set("mave.history", state.history);
            quietApi("/me/history?q=" + encodeURIComponent(q), { method: "DELETE", auth: true }); paint();
          } } })));
      });
    }
    paint();
    if (loggedIn()) api("/me/history", { auth: true }).then(function (d) {
      var srv = d.items || []; state.history = srv.concat(state.history.filter(function (q) { return srv.indexOf(q) < 0; })).slice(0, 200);
      store.set("mave.history", state.history); if (location.hash.indexOf("#/history") === 0) paint();
    }).catch(function () {});
  }

  // ---- settings (language, theme, account)
  function renderSettings() {
    clear(view);
    view.appendChild(el("h1", { text: t("settings") }));
    var lang = el("select", { id: "setLang" }, el("option", { value: "om", text: "Afaan Oromoo" }), el("option", { value: "en", text: "English" }));
    lang.value = state.lang;
    lang.addEventListener("change", function () { state.lang = lang.value; store.set("mave.lang", state.lang); pushPrefs(); applyI18n(); renderSettings(); });
    var theme = el("select", { id: "setTheme" }, el("option", { value: "auto", text: t("theme_auto") }), el("option", { value: "light", text: t("theme_light") }), el("option", { value: "dark", text: t("theme_dark") }));
    theme.value = state.theme;
    theme.addEventListener("change", function () { state.theme = theme.value; store.set("mave.theme", state.theme); applyTheme(); pushPrefs(); });
    view.appendChild(el("div", { class: "panel" }, el("label", {}, el("span", { text: t("language") }), lang), el("label", {}, el("span", { text: t("theme") }), theme)));

    var acct = el("div", { class: "panel" }); view.appendChild(acct);
    if (!loggedIn()) {
      acct.appendChild(el("p", { text: t("not_signed_in") }));
      acct.appendChild(el("button", { class: "primary", text: t("login"), on: { click: function () { openAuth("login"); } } }));
    } else {
      acct.appendChild(el("h2", { text: t("profile") }));
      acct.appendChild(el("p", { text: t("signed_in_as") + " " + state.email }));
      if (!state.verified) acct.appendChild(verifyBox());
      else acct.appendChild(el("p", { class: "msg ok", text: "✓ " + t("email_verified") }));
      acct.appendChild(el("div", { class: "row" },
        el("button", { text: t("logout"), on: { click: function () { logout(false); } } }),
        el("button", { text: t("logout_all"), on: { click: function () { logout(true); } } })));
      acct.appendChild(deleteBox());
    }
    view.appendChild(el("div", { class: "panel" }, el("p", { text: t("offline_note") }), el("button", { text: t("clear_local"), on: { click: function () {
      state.history = []; state.saved = []; store.del("mave.history"); store.del("mave.saved"); toast(t("cleared"));
    } } })));
  }
  function pushPrefs() { quietApi("/me/preferences", { method: "PUT", auth: true, body: { lang: state.lang, theme: state.theme === "auto" ? null : state.theme } }); }

  function verifyBox() {
    var msg = el("p", { class: "msg", role: "alert" });
    var code = el("input", { maxlength: "200", autocomplete: "one-time-code", "aria-label": t("code") });
    return el("div", {}, el("h2", { text: t("verify_title") }), el("p", { class: "warn", text: t("email_unverified") }),
      el("label", {}, el("span", { text: t("code") }), code),
      el("div", { class: "row" },
        el("button", { class: "primary", text: t("verify_btn"), on: { click: function () {
          if (code.value.trim().length < 10) { msg.textContent = t("err_code"); return; }
          api("/auth/verify-email", { method: "POST", body: { token: code.value.trim() } })
            .then(function () { state.verified = true; store.set("mave.verified", true); toast(t("verify_done")); renderSettings(); })
            .catch(function (e) { msg.textContent = e.status === 400 || e.status === 422 ? t("err_code_invalid") : t(C.errorKey(e.status)); });
        } } }),
        el("button", { class: "ghost", text: t("resend"), on: { click: function () { api("/auth/resend-verification", { method: "POST", auth: true }).then(function () { toast(t("reset_sent")); }).catch(function (e) { msg.textContent = t(C.errorKey(e.status)); }); } } })),
      msg);
  }
  function deleteBox() {
    var msg = el("p", { class: "msg", role: "alert" });
    var pw = el("input", { type: "password", autocomplete: "current-password", "aria-label": t("password") });
    return el("div", {}, el("h2", { text: t("delete_account") }), el("p", { text: t("delete_confirm") }), pw,
      el("div", { class: "row" }, el("button", { text: t("delete_account"), on: { click: function () {
        if (!pw.value) return;
        api("/me/delete", { method: "POST", auth: true, body: { password: pw.value } })
          .then(function () { clearSession(); state.history = []; state.saved = []; store.del("mave.history"); store.del("mave.saved"); toast(t("deleted")); go("home"); })
          .catch(function (e) { msg.textContent = t(C.errorKey(e.status)); });
      } } })), msg);
  }
  function logout(all) {
    var body = { refresh_token: state.refresh || undefined, all: !!all };
    var p = state.access || state.refresh ? api("/auth/logout", { method: "POST", auth: !!state.access, body: body }).catch(function () {}) : Promise.resolve();
    p.then(function () { clearSession(); go("home"); renderRoute(); });
  }

  // ------------------------------------------------------------------ auth dialog: login | register | forgot | reset
  var authMode = "login";
  function openAuth(mode, prefill) {
    authMode = mode; paintAuth();
    if (prefill) $("aCode").value = prefill;
    var d = $("authDlg"); if (!d.open) d.showModal();
    $("aEmail").focus();
  }
  function paintAuth() {
    var m = authMode, titles = { login: t("login"), register: t("signup"), forgot: t("reset_title"), reset: t("reset_title") };
    $("authTitle").textContent = titles[m];
    $("aPwRow").hidden = m === "forgot";
    $("aCodeRow").hidden = m !== "reset";
    $("aPw").required = m !== "forgot";
    $("aPw").setAttribute("autocomplete", m === "login" ? "current-password" : "new-password");
    $("aPwRow").firstChild.textContent = m === "reset" ? t("new_password") : t("password");
    $("authSubmit").textContent = { login: t("login"), register: t("signup"), forgot: t("send_code"), reset: t("reset_btn") }[m];
    $("authSwitch").textContent = m === "login" ? t("no_account") : t("have_account");
    $("authSwitch").hidden = m === "forgot" || m === "reset";
    $("authForgot").hidden = m !== "login";
    setAuthMsg("", false);
  }
  function setAuthMsg(text, ok) { var n = $("authMsg"); n.textContent = text; n.className = "msg" + (ok ? " ok" : ""); }

  function submitAuth(ev) {
    ev.preventDefault();
    var email = $("aEmail").value.trim(), pw = $("aPw").value, m = authMode;
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) return setAuthMsg(t("err_email"), false);
    if (m !== "forgot" && pw.length < 8) return setAuthMsg(t("err_pw_short"), false);
    var btn = $("authSubmit"); btn.disabled = true;
    var req;
    if (m === "login" || m === "register") {
      req = api("/auth/" + (m === "login" ? "login" : "register"), { method: "POST", body: { email: email, password: pw } }).then(function (d) {
        setSession(d); $("aPw").value = ""; $("authDlg").close(); toast(t("signed_in_as") + " " + (d.email || email));
        return syncAfterLogin().then(renderRoute);
      });
    } else if (m === "forgot") {
      req = api("/auth/forgot-password", { method: "POST", body: { email: email } }).then(function () { authMode = "reset"; paintAuth(); setAuthMsg(t("reset_sent"), true); });
    } else {
      var code = $("aCode").value.trim();
      if (code.length < 10) { btn.disabled = false; return setAuthMsg(t("err_code"), false); }
      req = api("/auth/reset-password", { method: "POST", body: { token: code, password: pw } }).then(function () { authMode = "login"; paintAuth(); $("aPw").value = ""; setAuthMsg(t("reset_done"), true); });
    }
    req.catch(function (e) {
      var key = m === "reset" && (e.status === 400 || e.status === 422) ? "err_code_invalid" : C.errorKey(e.status);
      setAuthMsg(t(key), false);
    }).then(function () { btn.disabled = false; });
  }

  // ------------------------------------------------------------------ autocomplete + voice
  var suggTimer, suggSeq = 0, suggIdx = -1;
  function hideSugg() { $("sugg").hidden = true; suggIdx = -1; $("q").setAttribute("aria-expanded", "false"); }
  function onType() {
    clearTimeout(suggTimer);
    var v = $("q").value.trim();
    if (!v) return hideSugg();
    suggTimer = setTimeout(function () {
      var seq = ++suggSeq;
      api("/suggest?q=" + encodeURIComponent(v)).then(function (d) {
        if (seq !== suggSeq) return;
        var ul = clear($("sugg")); var items = (d.suggestions || []).slice(0, 8);
        if (!items.length) return hideSugg();
        items.forEach(function (s, i) {
          ul.appendChild(el("li", { role: "option", id: "sg" + i, "aria-selected": "false", text: s, on: { mousedown: function (e) { e.preventDefault(); $("q").value = s; hideSugg(); submitSearch(); } } }));
        });
        ul.hidden = false; suggIdx = -1;
      }).catch(hideSugg);
    }, 200);
  }
  function onKey(e) {
    var ul = $("sugg"), items = ul.querySelectorAll("li");
    if (e.key === "Escape") return hideSugg();
    if (ul.hidden || !items.length || (e.key !== "ArrowDown" && e.key !== "ArrowUp")) return;
    e.preventDefault();
    suggIdx = (suggIdx + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
    items.forEach(function (li, i) { li.setAttribute("aria-selected", String(i === suggIdx)); });
    $("q").value = items[suggIdx].textContent;
  }
  function submitSearch() {
    var q = $("q").value.trim(); hideSugg();
    if (!q) return;
    var cur = C.parseHash(location.hash);
    go("search", { q: q, type: cur.route === "search" && TABS.indexOf(cur.params.type) >= 0 ? cur.params.type : "web" });
  }
  function setupVoice() {
    var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) return;
    var btn = $("voiceBtn"); btn.hidden = false;
    btn.addEventListener("click", function () {
      var r = new SR(); r.lang = state.lang === "om" ? "om-ET" : "en-US"; r.interimResults = false; r.maxAlternatives = 1;
      r.onresult = function (e) { $("q").value = e.results[0][0].transcript; submitSearch(); };
      r.onerror = function () { toast(t("err_invalid")); };
      try { r.start(); } catch (e) { /* already listening */ }
    });
  }

  // ------------------------------------------------------------------ router
  function renderRoute() {
    var h = C.parseHash(location.hash);
    searchToken++;
    window.scrollTo(0, 0);
    switch (h.route) {
      case "search": return renderSearch(h.params);
      case "saved": return renderSaved();
      case "history": return renderHistory();
      case "settings": return renderSettings();
      case "reset-password": go("home"); return openAuth("reset", h.params.token || "");
      case "verify-email":
        if (h.params.token) api("/auth/verify-email", { method: "POST", body: { token: h.params.token } }).then(function () { state.verified = true; store.set("mave.verified", true); toast(t("verify_done")); }).catch(function (e) { toast(t(C.errorKey(e.status))); });
        return go("settings");
      default: return renderHome();
    }
  }

  function init() {
    applyTheme(); applyI18n();
    $("searchForm").addEventListener("submit", function (e) { e.preventDefault(); submitSearch(); });
    $("q").addEventListener("input", onType); $("q").addEventListener("keydown", onKey); $("q").addEventListener("blur", function () { setTimeout(hideSugg, 120); });
    $("authBtn").addEventListener("click", function () { if (loggedIn()) logout(false); else openAuth("login"); });
    $("authForm").addEventListener("submit", submitAuth);
    $("authSwitch").addEventListener("click", function () { authMode = authMode === "login" ? "register" : "login"; paintAuth(); });
    $("authForgot").addEventListener("click", function () { authMode = "forgot"; paintAuth(); });
    $("authCancel").addEventListener("click", function () { $("authDlg").close(); });
    window.addEventListener("hashchange", renderRoute);
    setupVoice();
    if (state.refresh) doRefresh().then(syncAfterLogin).catch(function () {}).then(renderRoute); else renderRoute();
    if ("serviceWorker" in navigator && (location.protocol === "https:" || location.hostname === "localhost" || location.hostname === "127.0.0.1")) {
      navigator.serviceWorker.register("sw.js").catch(function () { /* offline shell is optional */ });
    }
  }
  init();
})();
