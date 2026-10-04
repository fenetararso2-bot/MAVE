// Executes the real web/app.js and web/admin.js against a minimal DOM shim with a fake API. Run: node --test tests/web/smoke.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { install, fakeFetch, tick } = require("./domshim.js");

const WEB = path.join(__dirname, "../../web");
const APP_IDS = [
  "view", ["q", "input"], "sugg", "toast", ["authDlg", "dialog"], ["authForm", "form"], ["aEmail", "input"], ["aPw", "input"],
  ["aPwRow", "label", (n) => n.appendChild(Object.assign(new (require("./domshim.js").El)("span"), {}))], ["aCode", "input"], ["aCodeRow", "label"],
  "authTitle", "authMsg", ["authSubmit", "button"], ["authSwitch", "button"], ["authForgot", "button"], ["authCancel", "button"], ["authBtn", "button"],
  ["searchForm", "form"], ["voiceBtn", "button"],
];

function loadApp(hash, routes, store = {}) {
  for (const k of Object.keys(require.cache)) if (k.startsWith(WEB)) delete require.cache[k];
  const dom = install(APP_IDS, hash, store);
  const calls = fakeFetch(routes);
  require(path.join(WEB, "i18n.js"));
  globalThis.MaveCore = require(path.join(WEB, "core.js")); // in a browser core.js attaches itself to window
  require(path.join(WEB, "app.js"));
  return { dom, calls, store };
}
const links = (root) => root.byTag("a");

test("home: shows trending chips from the API", async () => {
  const { dom } = loadApp("", { "GET /trending": { trending: ["Kotlin", "Afaan Oromoo"] } });
  await tick();
  const chips = dom.byId.view.all().filter((n) => n.className === "chip").map((n) => n.textContent);
  assert.deepEqual(chips, ["Kotlin", "Afaan Oromoo"]);
});

test("search: renders results, skips unsafe URLs, de-duplicates, offers 'did you mean'", async () => {
  const { dom, calls } = loadApp("#/search?q=kotlin&type=web", {
    "GET /search": { results: [
      { title: "Good", url: "https://a.com/1", snippet: "snippet one" },
      { title: "Evil", url: "javascript:alert(1)", snippet: "x" },
      { title: "Dup", url: "https://a.com/1", snippet: "dup" },
      { title: "Second", url: "https://b.com/2", snippet: "two" },
    ], corrected: "kotlin lang", page: 1 },
    "POST /me/history": { ok: true },
  });
  await tick();
  const articles = dom.byId.view.byTag("article");
  assert.equal(articles.length, 2);
  const hrefs = links(dom.byId.view).map((a) => a.attrs.href).filter(Boolean);
  assert.ok(hrefs.includes("https://a.com/1") && hrefs.includes("https://b.com/2"));
  assert.ok(!hrefs.some((h) => h.startsWith("javascript:")));
  assert.ok(links(dom.byId.view).every((a) => !a.attrs.target || /noopener/.test(a.attrs.rel)));
  assert.match(dom.byId.view.textContent, /kotlin lang/);
  assert.ok(calls.some((c) => c.url.includes("/api/v1/search?q=kotlin&type=web&page=1")));
  // the UI language is sent as a hint so the backend can apply Afaan Oromoo stemming to short queries
  assert.ok(calls.some((c) => /\/api\/v1\/search\?.*&lang=(om|en)$/.test(c.url)));
  assert.equal(dom.byId.q.value, "kotlin");
});

test("search: category hint from the backend links to that tab; unknown / current / ai hints are ignored", async () => {
  const run = async (intent, type) => {
    const { dom } = loadApp("#/search?q=oduu&type=" + type, { "GET /search": { results: [{ title: "R", url: "https://a.com/1", snippet: "s" }], corrected: null, page: 1, intent: intent }, "POST /me/history": { ok: true } });
    await tick();
    return dom;
  };
  const dom = await run("news", "web");
  assert.match(dom.byId.view.textContent, /Try the tab:/);
  const hint = links(dom.byId.view).find((a) => /type=news/.test(a.attrs.href || ""));
  assert.ok(hint, "link to the news tab");
  assert.equal(hint.textContent, "News");
  for (const [intent, type] of [["web", "web"], ["ai", "web"], ["javascript:x", "web"], [null, "web"]]) {
    assert.doesNotMatch((await run(intent, type)).byId.view.textContent, /Try the tab:/, String(intent));
  }
});

test("search: API failure shows a retry state, not a crash", async () => {
  const { dom } = loadApp("#/search?q=x&type=news", { "GET /search": { __status: 502, body: { detail: "AI provider error 500" } } });
  await tick();
  assert.match(dom.byId.view.textContent, /Search provider is unavailable/);
  assert.ok(dom.byId.view.byTag("button").some((b) => b.textContent === "Retry"));
});

test("AI tab: numbered citations link to sources, unknown [n] stays text, ungrounded warning", async () => {
  const { dom, calls } = loadApp("#/search?q=oromia&type=ai", {
    "POST /ai/answer": { answer: "Oromia is a region [1]. Odd [9].", sources: [], grounded: true, insufficient: false,
      citations: [{ n: 1, title: "Oromia", url: "https://a.com/oromia" }, { n: 2, title: "Bad", url: "javascript:x" }] },
  });
  await tick();
  const cite = dom.byId.view.all().filter((n) => n.className === "cite");
  assert.equal(cite.length, 1);
  assert.equal(cite[0].attrs.href, "https://a.com/oromia");
  assert.equal(cite[0].textContent, "1");
  assert.match(dom.byId.view.textContent, /\[9\]/);
  assert.ok(!links(dom.byId.view).some((a) => (a.attrs.href || "").startsWith("javascript:")));
  const req = calls.find((c) => c.url.endsWith("/ai/answer"));
  assert.deepEqual(req.body, { query: "oromia", lang: "en" });
});

test("AI tab: insufficient evidence is reported, not hidden", async () => {
  const { dom } = loadApp("#/search?q=zzz&type=ai", { "POST /ai/answer": { answer: "", sources: [], citations: [], grounded: false, insufficient: true } });
  await tick();
  assert.match(dom.byId.view.textContent, /not enough evidence/);
});

test("login flow stores the refresh token, keeps the access token in memory, then syncs", async () => {
  const store = { local: {}, session: {} };
  const { dom, calls } = loadApp("#/settings", {
    "POST /auth/login": { token: "ACCESS", refresh_token: "R".repeat(40), email: "u@example.com", email_verified: true, expires_in: 900 },
    "GET /me/history": { items: ["old query"] }, "GET /me/saved": { items: [] },
  }, store);
  await tick();
  dom.byId.aEmail.value = "u@example.com"; dom.byId.aPw.value = "password123";
  dom.byId.authForm.fire("submit");
  await tick(12);
  assert.equal(JSON.parse(store.local["mave.rt"]), "R".repeat(40));
  assert.ok(!Object.values(store.local).some((v) => v.includes("ACCESS")), "access token must never be persisted");
  assert.equal(dom.byId.authDlg.open, false);
  assert.ok(calls.some((c) => c.url.endsWith("/me/history") && c.method === "GET" && c.headers.Authorization === "Bearer ACCESS"));
  assert.match(dom.byId.view.textContent, /u@example.com/);
});

test("expired access token is refreshed once and the request retried", async () => {
  let first = true;
  const store = { local: { "mave.rt": JSON.stringify("R".repeat(40)), "mave.email": JSON.stringify("u@example.com"), "mave.verified": "true" } };
  const { calls } = loadApp("#/history", {
    "POST /auth/refresh": { token: "NEW", refresh_token: "S".repeat(40), email: "u@example.com", email_verified: true },
    "GET /me/history": (url, opts, call) => (call.headers.Authorization === "Bearer NEW" ? { items: ["a"] } : { __status: 401, body: { detail: "expired" } }),
    "GET /me/saved": { items: [] },
  }, store);
  await tick(14);
  assert.equal(calls.filter((c) => c.url.endsWith("/auth/refresh")).length, 1, "single-flight refresh");
  assert.ok(calls.some((c) => c.url.endsWith("/me/history") && c.headers.Authorization === "Bearer NEW"));
  assert.equal(JSON.parse(store.local["mave.rt"]), "S".repeat(40), "rotated refresh token is saved");
});

test("saved items work offline from local storage", async () => {
  const saved = [{ title: "Kept", url: "https://k.com/", snippet: "s", thumbnail: null }];
  const { dom } = loadApp("#/saved", {}, { local: { "mave.saved": JSON.stringify(saved) } });
  await tick();
  assert.match(dom.byId.view.textContent, /Kept/);
  assert.ok(links(dom.byId.view).some((a) => a.attrs.href === "https://k.com/"));
});

test("reset-password link opens the dialog with the code prefilled", async () => {
  const { dom } = loadApp("#/reset-password?token=TOKEN_1234567890", {});
  await tick();
  assert.equal(dom.byId.authDlg.open, true);
  assert.equal(dom.byId.aCode.value, "TOKEN_1234567890");
  assert.equal(dom.byId.aCodeRow.hidden, false);
});

// ---------------------------------------------------------------- admin dashboard
function loadAdmin(routes, keyInSession = "adminkey") {
  for (const k of Object.keys(require.cache)) if (k.startsWith(WEB)) delete require.cache[k];
  const { El } = require("./domshim.js");
  const ids = ["keyForm", ["adminKey", "input"], "keyMsg", "panel", "adminNav", ["adminLogout", "button"], "toast"];
  const dom = install(ids, "", { session: keyInSession ? { "mave.adminKey": keyInSession } : {} });
  ["overview", "crawler", "analytics", "users", "audit"].forEach((t) => { const b = new El("button"); b.className = "tab"; b.attrs["data-tab"] = t; dom.body.appendChild(b); });
  dom.byId.panel.hidden = true; dom.byId.adminNav.hidden = true;
  const calls = fakeFetch(routes);
  require(path.join(WEB, "admin.js"));
  return { dom, calls, tabs: dom.body.querySelectorAll(".tab") };
}
const ADMIN = {
  "GET /admin/health": { ok: true, version: "0.9.0", environment: "development", schema_version: 5, database: "postgresql", providers: { web_search: false, ai: null, smtp: false }, config: { cors_origins: 0, trust_proxy: false, public_url_set: false } },
  "GET /admin/stats": { documents: 12, domains: 3, users: 2, queries_logged: 40, frontier_pending: 5, frontier_failed: 1 },
  "GET /admin/seeds": { items: [{ url: "https://om.wikipedia.org/", enabled: true, added_at: 1700000000 }] },
  "GET /admin/crawl-errors": { items: [{ url: "https://bad.org/", status: "failed", attempts: 3, last_error: "HTTP 403", added_at: 1700000000 }] },
  "GET /admin/analytics": { days: 14, total: 6, per_day: [{ day: "2026-10-03", n: 2 }, { day: "2026-10-04", n: 4 }], top_queries: [{ query: "kotlin", n: 4 }], top_domains: [{ domain: "a.org", documents: 9 }], languages: [{ lang: "om", documents: 4 }] },
  "GET /admin/users": { total: 1, items: [{ id: 7, email: "<img src=x onerror=alert(1)>@e.com", email_verified: false, active_sessions: 2, created_at: 1700000000, role: "user" }] },
  "GET /admin/audit": { items: [{ ts: 1700000000, actor: "admin-key", action: "seed.add", detail: "https://om.wikipedia.org/" }] },
};

test("admin: signs in from session key and renders health + stats", async () => {
  const { dom, calls } = loadAdmin(ADMIN);
  await tick(10);
  assert.equal(dom.byId.panel.hidden, false);
  assert.match(dom.byId.panel.textContent, /System health/);
  assert.match(dom.byId.panel.textContent, /Documents indexed/);
  assert.ok(calls.every((c) => c.headers["X-Admin-Key"] === "adminkey"));
});

test("admin: crawler, analytics, users and audit tabs all render without errors", async () => {
  const { dom, tabs } = loadAdmin(ADMIN);
  await tick(10);
  const click = async (name) => { tabs.find((t) => t.attrs["data-tab"] === name).fire("click"); await tick(10); return dom.byId.panel.textContent; };
  assert.match(await click("crawler"), /om\.wikipedia\.org[\s\S]*HTTP 403/);
  assert.match(await click("analytics"), /kotlin[\s\S]*a\.org[\s\S]*om/);
  assert.ok(dom.byId.panel.byTag("rect").length === 2, "one bar per day");
  assert.match(await click("users"), /Users \(1\)/);
  assert.match(await click("audit"), /seed\.add/);
});

test("admin: user data is rendered as text, never as HTML", async () => {
  const { dom, tabs } = loadAdmin(ADMIN);
  await tick(10);
  tabs.find((t) => t.attrs["data-tab"] === "users").fire("click");
  await tick(10);
  assert.equal(dom.byId.panel.byTag("img").length, 0);
  assert.match(dom.byId.panel.textContent, /<img src=x onerror=alert\(1\)>@e\.com/);
});

test("admin: a rejected key returns to the sign-in form", async () => {
  const { dom } = loadAdmin({ "GET /admin/health": { __status: 403, body: { detail: "Forbidden" } } }, "wrongkey");
  await tick(10);
  assert.equal(dom.byId.panel.hidden, true);
  assert.equal(dom.byId.keyForm.hidden, false);
});

test("admin: users tab shows the role and promotes a user (confirmed, audited server-side)", async () => {
  const routes = { ...ADMIN, "POST /admin/users/7/role": { ok: true, role: "admin" } };
  const { dom, calls, tabs } = loadAdmin(routes);
  await tick(10);
  tabs.find((tab) => tab.attrs["data-tab"] === "users").fire("click");
  await tick(10);
  assert.match(dom.byId.panel.textContent, /Role/);
  const btn = dom.byId.panel.byTag("button").find((b) => b.textContent === "Make admin");
  assert.ok(btn, "a Make admin button");
  global.confirm = () => true;
  btn.fire("click");
  await tick(10);
  const post = calls.find((c) => c.method === "POST" && c.url.endsWith("/admin/users/7/role"));
  assert.ok(post, "POST /users/7/role was sent");
  assert.deepEqual(post.body, { role: "admin" });  // fakeFetch records the parsed JSON body
  assert.equal(post.headers["X-Admin-Key"], "adminkey");
});
