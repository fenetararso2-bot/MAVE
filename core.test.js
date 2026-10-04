// Run: node --test tests/web/   (Node >= 18). Pure helpers of the web app: no DOM needed.
const test = require("node:test");
const assert = require("node:assert/strict");
const C = require("../../web/core.js");

test("safeUrl only lets http(s) through", () => {
  assert.equal(C.safeUrl("https://a.com/x?y=1"), "https://a.com/x?y=1");
  assert.equal(C.safeUrl("http://a.com"), "http://a.com/");
  for (const bad of ["javascript:alert(1)", "data:text/html,<script>", "file:///etc/passwd", "//evil.com", "", null, undefined, "not a url"]) {
    assert.equal(C.safeUrl(bad), null, String(bad));
  }
});

test("domainOf strips www and survives garbage", () => {
  assert.equal(C.domainOf("https://www.example.org/a"), "example.org");
  assert.equal(C.domainOf("nope"), "");
});

test("parseHash / buildHash round trip", () => {
  assert.deepEqual(C.parseHash(""), { route: "home", params: {} });
  assert.deepEqual(C.parseHash("#/saved"), { route: "saved", params: {} });
  const h = C.buildHash("search", { q: "afaan oromoo & news", type: "news", empty: "" });
  assert.equal(h, "#/search?q=afaan+oromoo+%26+news&type=news");
  assert.deepEqual(C.parseHash(h), { route: "search", params: { q: "afaan oromoo & news", type: "news" } });
  assert.deepEqual(C.parseHash("#/reset-password?token=abc_DEF-123").params, { token: "abc_DEF-123" });
});

test("splitCitations links only citations that exist", () => {
  const cites = [{ n: 1 }, { n: 2 }];
  const parts = C.splitCitations("Oromia is large [1]. It has cities [2][9].", cites);
  assert.deepEqual(parts, [
    { text: "Oromia is large " }, { cite: 1 }, { text: ". It has cities " }, { cite: 2 }, { text: "[9]." },
  ]);
  assert.deepEqual(C.splitCitations("no markers", cites), [{ text: "no markers" }]);
  assert.deepEqual(C.splitCitations("", cites), []);
  assert.deepEqual(C.splitCitations("a [1]", []), [{ text: "a [1]" }]);
});

test("addUnique de-duplicates across pages and ignores url-less items", () => {
  const seen = {}, list = [];
  assert.equal(C.addUnique(list, [{ url: "a" }, { url: "b" }, { url: "a" }, {}], seen).length, 2);
  assert.equal(C.addUnique(list, [{ url: "b" }, { url: "c" }], seen).length, 1);
  assert.deepEqual(list.map((x) => x.url), ["a", "b", "c"]);
});

test("errorKey maps HTTP status to a message key", () => {
  assert.equal(C.errorKey(0), "err_network");
  assert.equal(C.errorKey(429), "err_rate");
  assert.equal(C.errorKey(502), "err_provider");
  assert.equal(C.errorKey(401), "err_login");
  assert.equal(C.errorKey(409), "err_exists");
  assert.equal(C.errorKey(422), "err_invalid");
  assert.equal(C.errorKey(500), "err_server");
});

test("pushHistory moves repeats to the front and caps the list", () => {
  assert.deepEqual(C.pushHistory(["b", "a"], "a", 200), ["a", "b"]);
  assert.deepEqual(C.pushHistory(["b"], "  ", 200), ["b"]);
  assert.equal(C.pushHistory(Array.from({ length: 300 }, (_, i) => "q" + i), "new", 200).length, 200);
});

test("every UI string exists in both languages", () => {
  global.window = {};
  require("../../web/i18n.js");
  const { en, om } = global.window.MAVE_I18N;
  assert.deepEqual(Object.keys(om).sort(), Object.keys(en).sort());
  for (const k of Object.keys(en)) assert.ok(om[k].trim() && en[k].trim(), k);
});
