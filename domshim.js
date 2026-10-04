// Minimal DOM + fetch shim: just enough to execute web/app.js and web/admin.js end to end under node.
// appendChild() throws on non-nodes, so wrong child types (e.g. arrays) fail loudly like in a browser.
class El {
  constructor(tag) { this.tagName = tag; this.children = []; this.attrs = {}; this.listeners = {}; this.className = ""; this.hidden = false; this.value = ""; this.open = false; this.data = ""; this.parent = null; }
  get firstChild() { return this.children[0] || null; }
  appendChild(c) {
    if (!(c instanceof El)) throw new TypeError("appendChild: not a node: " + typeof c + " " + JSON.stringify(c));
    this.children.push(c); c.parent = this; return c;
  }
  removeChild(c) { this.children = this.children.filter((x) => x !== c); return c; }
  insertBefore(n, ref) { const i = this.children.indexOf(ref); this.children.splice(i < 0 ? this.children.length : i, 0, n); n.parent = this; return n; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === "hidden") this.hidden = true; if (k === "class") this.className = String(v); if (k === "value") this.value = String(v); }
  removeAttribute(k) { delete this.attrs[k]; }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  addEventListener(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); }
  fire(ev, extra) { (this.listeners[ev] || []).forEach((fn) => fn(Object.assign({ preventDefault() {}, key: "" }, extra))); }
  get textContent() { return this.tagName === "#text" ? this.data : this.children.map((c) => c.textContent).join(""); }
  set textContent(v) { if (this.tagName === "#text") this.data = String(v); else { this.children = []; if (v !== "") this.appendChild(Object.assign(new El("#text"), { data: String(v) })); } }
  matches(sel) {
    if (sel.startsWith(".")) return this.className.split(/\s+/).includes(sel.slice(1));
    const m = sel.match(/^\[([\w-]+)\]$/); if (m) return m[1] in this.attrs;
    return this.tagName === sel;
  }
  querySelectorAll(sel) { const out = []; const walk = (n) => n.children.forEach((c) => { if (c.matches(sel)) out.push(c); walk(c); }); walk(this); return out; }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  showModal() { this.open = true; } close() { this.open = false; } focus() {}
  find(pred) { return this.querySelectorAll("*x").concat(this.all().filter(pred)); }
  all() { const out = []; const walk = (n) => n.children.forEach((c) => { out.push(c); walk(c); }); walk(this); return out; }
  byTag(tag) { return this.all().filter((n) => n.tagName === tag); }
}

function install(ids, hash, store) {
  const body = new El("body");
  const byId = {};
  ids.forEach((spec) => {
    const [id, tag = "div", extra] = Array.isArray(spec) ? spec : [spec];
    const n = new El(tag); n.attrs.id = id; byId[id] = n; body.appendChild(n);
    if (extra) extra(n);
  });
  const winListeners = {};
  const doc = {
    body, documentElement: new El("html"), getElementById: (id) => byId[id] || null,
    createElement: (t) => new El(t), createElementNS: (_ns, t) => Object.assign(new El(t), { namespaceURI: "svg" }),
    createTextNode: (s) => Object.assign(new El("#text"), { data: String(s) }),
    querySelectorAll: (s) => body.querySelectorAll(s), querySelector: (s) => body.querySelector(s),
  };
  const mem = (src) => ({ getItem: (k) => (k in src ? src[k] : null), setItem: (k, v) => { src[k] = String(v); }, removeItem: (k) => { delete src[k]; } });
  const globals = {
    Node: El, document: doc, localStorage: mem(store.local || {}), sessionStorage: mem(store.session || {}),
    location: { hash, protocol: "http:", hostname: "example.test" }, navigator: { language: "en-US" },
    confirm: () => true, scrollTo() {},
  };
  // defineProperty: newer Node versions ship getter-only globals such as `navigator`
  Object.keys(globals).forEach((k) => Object.defineProperty(globalThis, k, { value: globals[k], configurable: true, writable: true }));
  globalThis.window = globalThis;
  globalThis.addEventListener = (ev, fn) => { (winListeners[ev] = winListeners[ev] || []).push(fn); };
  return { byId, body, doc, fireWindow: (ev) => (winListeners[ev] || []).forEach((fn) => fn({})) };
}

/** routes: {"GET /path-prefix": body | (url, opts) => body | {status, body}} -> installs global fetch, records calls */
function fakeFetch(routes) {
  const calls = [];
  globalThis.fetch = async (url, opts = {}) => {
    const method = (opts.method || "GET").toUpperCase();
    calls.push({ method, url, headers: opts.headers || {}, body: opts.body ? JSON.parse(opts.body) : undefined });
    const key = Object.keys(routes).find((k) => k.split(" ")[0] === method && url.includes(k.split(" ")[1]));
    if (!key) return { ok: false, status: 404, text: async () => JSON.stringify({ detail: "no route " + method + " " + url }) };
    let r = routes[key]; if (typeof r === "function") r = r(url, opts, calls[calls.length - 1]);
    const status = r && r.__status ? r.__status : 200;
    const body = r && r.__status ? r.body : r;
    const text = body === undefined ? "" : JSON.stringify(body);
    return { ok: status < 400, status, text: async () => text, json: async () => JSON.parse(text) };
  };
  return calls;
}

const tick = (n = 6) => new Promise((res) => { let i = 0; const f = () => (++i >= n ? res() : setImmediate(f)); f(); });

module.exports = { El, install, fakeFetch, tick };
