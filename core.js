/* Pure helpers (no DOM): shared by app.js and unit-tested with node (tests/web/core.test.js). */
(function (root) {
  "use strict";

  /** Only http(s) URLs may ever become an href/src: blocks javascript:, data:, etc. */
  function safeUrl(u) {
    try {
      var p = new URL(String(u || ""));
      return p.protocol === "http:" || p.protocol === "https:" ? p.href : null;
    } catch (e) {
      return null;
    }
  }

  function domainOf(u) {
    try {
      return new URL(u).hostname.replace(/^www\./, "");
    } catch (e) {
      return "";
    }
  }

  /** "#/search?q=a%20b&type=news" -> {route:"search", params:{q:"a b", type:"news"}} */
  function parseHash(hash) {
    var h = String(hash || "").replace(/^#\/?/, "");
    var i = h.indexOf("?");
    var route = (i < 0 ? h : h.slice(0, i)) || "home";
    var params = {};
    if (i >= 0) {
      new URLSearchParams(h.slice(i + 1)).forEach(function (v, k) {
        params[k] = v;
      });
    }
    return { route: route, params: params };
  }

  function buildHash(route, params) {
    var qs = new URLSearchParams();
    Object.keys(params || {}).forEach(function (k) {
      if (params[k] !== undefined && params[k] !== null && params[k] !== "") qs.set(k, params[k]);
    });
    var s = qs.toString();
    return "#/" + route + (s ? "?" + s : "");
  }

  /**
   * Split an AI answer into text and citation parts. "[2]" becomes {cite:2} only when citation 2 exists, so a
   * stray "[9]" stays plain text. Returns [{text}|{cite:n}, ...].
   */
  function splitCitations(text, citations) {
    var valid = {};
    (citations || []).forEach(function (c) {
      valid[c.n] = true;
    });
    var parts = [];
    var re = /\[(\d{1,2})\]/g;
    var last = 0;
    var m;
    text = String(text || "");
    while ((m = re.exec(text))) {
      if (!valid[Number(m[1])]) continue;
      if (m.index > last) parts.push({ text: text.slice(last, m.index) });
      parts.push({ cite: Number(m[1]) });
      last = m.index + m[0].length;
    }
    if (last < text.length) parts.push({ text: text.slice(last) });
    return parts;
  }

  /** Append items whose url is not yet in `seen` (a plain object used as a set). Returns the newly added items. */
  function addUnique(list, items, seen) {
    var added = [];
    (items || []).forEach(function (it) {
      var key = it && it.url;
      if (!key || seen[key]) return;
      seen[key] = true;
      list.push(it);
      added.push(it);
    });
    return added;
  }

  /** HTTP status -> i18n error key. 0 = network failure. */
  function errorKey(status) {
    if (status === 0) return "err_network";
    if (status === 429) return "err_rate";
    if (status === 502) return "err_provider";
    if (status === 401) return "err_login";
    if (status === 409) return "err_exists";
    if (status === 400 || status === 422) return "err_invalid";
    return "err_server";
  }

  function pushHistory(list, q, max) {
    q = String(q || "").trim();
    if (!q) return list;
    var out = [q].concat(list.filter(function (x) { return x !== q; }));
    return out.slice(0, max || 200);
  }

  var api = { safeUrl: safeUrl, domainOf: domainOf, parseHash: parseHash, buildHash: buildHash, splitCitations: splitCitations, addUnique: addUnique, errorKey: errorKey, pushHistory: pushHistory };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.MaveCore = api;
})(typeof window !== "undefined" ? window : globalThis);
