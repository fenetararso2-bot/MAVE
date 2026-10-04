"""Structure of the HTTP layer (app/api/*). Source-level (ast) checks: they need neither FastAPI nor PostgreSQL,
so they also run on machines where the API tests are skipped. They freeze the public route table and keep the
security-relevant wiring (admin key, bearer user) from being dropped by a refactor."""
import ast
import pathlib
import unittest

APP = pathlib.Path(__file__).resolve().parent.parent / "app"

# (module, METHOD, path) — mounted under /api/v1 (and un-prefixed for MAVE <= 0.2 clients) by app.main
EXPECTED = {
    ("admin", "POST", "/admin/crawl"), ("admin", "GET", "/admin/stats"), ("admin", "GET", "/admin/health"),
    ("admin", "GET", "/admin/analytics"), ("admin", "GET", "/admin/crawl-errors"), ("admin", "GET", "/admin/seeds"),
    ("admin", "POST", "/admin/seeds"), ("admin", "PUT", "/admin/seeds"), ("admin", "DELETE", "/admin/seeds"),
    ("admin", "GET", "/admin/users"), ("admin", "POST", "/admin/users/{user_id}/revoke-sessions"),
    ("admin", "POST", "/admin/users/{user_id}/disable"), ("admin", "POST", "/admin/users/{user_id}/enable"),
    ("admin", "POST", "/admin/users/{user_id}/role"),
    ("admin", "GET", "/admin/audit"), ("admin", "GET", "/admin/metrics"),
    ("ai", "POST", "/ai/answer"),
    ("auth", "POST", "/auth/register"), ("auth", "POST", "/auth/login"), ("auth", "POST", "/auth/refresh"),
    ("auth", "POST", "/auth/logout"), ("auth", "POST", "/auth/forgot-password"), ("auth", "POST", "/auth/reset-password"),
    ("auth", "POST", "/auth/verify-email"), ("auth", "POST", "/auth/resend-verification"),
    ("health", "GET", "/health"),
    ("history", "GET", "/me/history"), ("history", "POST", "/me/history"), ("history", "DELETE", "/me/history"),
    ("saved", "GET", "/me/saved"), ("saved", "POST", "/me/saved"), ("saved", "DELETE", "/me/saved"),
    ("search", "GET", "/search"), ("search", "GET", "/suggest"),
    ("trending", "GET", "/trending"),
    ("users", "GET", "/me"), ("users", "GET", "/me/preferences"), ("users", "PUT", "/me/preferences"),
    ("users", "POST", "/me/delete"),
}


def routes():
    """Yield (module, METHOD, path, decorator_call, function_node) for every ``@router.<method>(...)`` in app/api."""
    for f in sorted((APP / "api").glob("*.py")):
        for node in ast.parse(f.read_text(encoding="utf-8")).body:
            for d in getattr(node, "decorator_list", []):
                if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                        and isinstance(d.func.value, ast.Name) and d.func.value.id == "router"):
                    yield f.stem, d.func.attr.upper(), d.args[0].value, d, node


class TestApiLayout(unittest.TestCase):
    def test_route_table_is_frozen(self):
        found = [(m, meth, path) for m, meth, path, _, _ in routes()]
        self.assertEqual(len(found), len(set(found)), "a route is declared twice")
        self.assertEqual(set(found), EXPECTED)

    def test_every_admin_route_declares_the_admin_dependency(self):
        for module, method, path, call, _ in routes():
            if not path.startswith("/admin/"):
                continue
            deps = next((k.value for k in call.keywords if k.arg == "dependencies"), None)
            self.assertIsNotNone(deps, f"{method} {path}: no dependencies=[...]")
            text = ast.unparse(deps)
            expected = "require_admin_or_bearer" if path == "/admin/metrics" else "require_admin"
            self.assertIn(expected, text, f"{method} {path}")

    def test_every_state_changing_admin_route_audits_with_the_acting_admin(self):
        """Since v0.11.4 admins are individual users: an audit row must name who acted, never a hard-coded actor."""
        for module, method, path, _, fn in routes():
            if not path.startswith("/admin/") or method == "GET":
                continue
            self.assertIn("AdminActor", ast.unparse(fn.args), f"{method} {path}: no `actor: AdminActor` parameter")
            calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and ast.unparse(n.func) == "admin.audit"]
            self.assertTrue(calls, f"{method} {path}: state change without an audit entry")
            for c in calls:
                kw = {k.arg: ast.unparse(k.value) for k in c.keywords}
                self.assertEqual(kw.get("actor"), "actor.label", f"{method} {path}: audit() without actor=actor.label")

    def test_nobody_changes_their_own_role_or_disables_themselves(self):
        """The routes must hand the acting user's id to the service layer, which refuses self-service changes."""
        wanted = {"/admin/users/{user_id}/role": "admin.set_user_role", "/admin/users/{user_id}/disable": "admin.set_user_disabled"}
        for module, method, path, _, fn in routes():
            if path in wanted:
                calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and ast.unparse(n.func) == wanted[path]]
                self.assertEqual(len(calls), 1, path)
                self.assertEqual({k.arg: ast.unparse(k.value) for k in calls[0].keywords}.get("actor_user_id"), "actor.user_id", path)

    def test_admin_module_holds_only_admin_routes(self):
        for module, method, path, _, _ in routes():
            self.assertEqual(module == "admin", path.startswith("/admin/"), f"{method} {path} is in {module}.py")

    def test_every_me_route_needs_a_signed_in_user(self):
        for module, method, path, _, fn in routes():
            if path != "/me" and not path.startswith("/me/"):
                continue
            self.assertIn("current_user", ast.unparse(fn.args), f"{method} {path}")

    def test_modules_share_one_router_each_and_the_package_mounts_them_all(self):
        modules = {f.stem for f in (APP / "api").glob("*.py")} - {"__init__", "deps", "schemas"}
        init = (APP / "api" / "__init__.py").read_text(encoding="utf-8")
        for name in modules:
            self.assertIn(name, init, f"api/__init__.py does not mount {name}")
            src = (APP / "api" / f"{name}.py").read_text(encoding="utf-8")
            self.assertEqual(src.count("router = APIRouter()"), 1, name)

    def test_main_declares_no_routes_and_api_never_imports_main(self):
        main = ast.parse((APP / "main.py").read_text(encoding="utf-8"))
        for node in main.body:
            for d in getattr(node, "decorator_list", []):
                if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in {"get", "post", "put", "delete"}:
                    self.assertEqual(d.args[0].value, "/", "only the web-app redirect may be declared in main.py")
        for f in (APP / "api").glob("*.py"):
            text = f.read_text(encoding="utf-8")
            self.assertNotIn("from ..main", text, f.name)
            self.assertNotIn("import app.main", text, f.name)


class TestDbPackageLayout(unittest.TestCase):
    def test_public_names_are_still_importable_from_app_db(self):
        init = (APP / "db" / "__init__.py").read_text(encoding="utf-8")
        tree = ast.parse(init)
        defined = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        for n in tree.body:
            if isinstance(n, (ast.Assign, ast.AnnAssign)):
                for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                    if isinstance(t, ast.Name):
                        defined.add(t.id)
            elif isinstance(n, ast.ImportFrom):
                defined |= {a.asname or a.name for a in n.names}
        for name in ("Conn", "Row", "to_pyformat", "db", "init_db", "migrate", "schema_version", "SCHEMA_VERSION",
                     "MIGRATIONS", "close_pool", "close_pools", "redact_dsn"):
            self.assertIn(name, defined, name)

    def test_migrations_are_contiguous_and_start_at_one(self):
        tree = ast.parse((APP / "db" / "migrations.py").read_text(encoding="utf-8"))
        table = next(n.value for n in tree.body if isinstance(n, ast.AnnAssign) and n.target.id == "MIGRATIONS")
        versions = [k.value for k in table.keys]
        self.assertEqual(versions, list(range(1, len(versions) + 1)))


class TestProviderSplit(unittest.TestCase):
    """Web search (Brave) and AI completions are separate layers; the error type they share lives in core."""

    @staticmethod
    def imports(path):
        names = set()
        for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(n, ast.ImportFrom):
                names.add(("." * n.level) + (n.module or ""))
                names |= {("." * n.level) + (n.module or "") + "." + a.name for a in n.names}
            elif isinstance(n, ast.Import):
                names |= {a.name for a in n.names}
        return names

    def test_ai_providers_knows_nothing_about_web_search_and_vice_versa(self):
        ai = self.imports(APP / "ai" / "providers.py")
        web = self.imports(APP / "search" / "web.py")
        self.assertFalse({i for i in ai if "web" in i or "search" in i}, ai)
        self.assertFalse({i for i in web if "xai" in i or "providers" in i or "ai" == i.lstrip(".").split(".")[0]}, web)
        ai_src = (APP / "ai" / "providers.py").read_text(encoding="utf-8")
        for needle in ("BRAVE", "brave", "web_search"):
            self.assertNotIn(needle, ai_src)

    def test_provider_error_is_defined_once_in_core(self):
        owners = []
        for f in APP.rglob("*.py"):
            for n in ast.parse(f.read_text(encoding="utf-8")).body:
                if isinstance(n, ast.ClassDef) and n.name == "ProviderError":
                    owners.append(f.relative_to(APP).as_posix())
        self.assertEqual(owners, ["core/errors.py"])

    def test_http_layer_does_not_talk_to_providers_directly(self):
        for f in [*(APP / "api").glob("*.py"), APP / "main.py"]:
            self.assertNotIn("httpx", self.imports(f), f.name)
            self.assertNotIn("..ai.providers", self.imports(f), f.name)
            self.assertNotIn(".ai.providers", self.imports(f), f.name)


if __name__ == "__main__":
    unittest.main()
