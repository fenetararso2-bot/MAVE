"""Ops files: Prometheus alert rules (cross-checked against the metrics the API really exports) and the
restore/verify script (run for real against a fake `docker`, so no database is touched)."""
import ast
import importlib.util
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
OPS = ROOT / "ops"
HAVE_YAML = importlib.util.find_spec("yaml") is not None


def exported_metric_names() -> set[str]:
    """Metric names /admin/metrics can emit: the request metrics plus one gauge per numeric key of index.stats()."""
    from app.core import metrics

    metrics.reset()
    metrics.observe("GET", "/x", 200, 0.1)
    names = set(re.findall(r"^(mave_[a-z_]+)", metrics.render(), re.M))
    src = (ROOT / "backend" / "app" / "search" / "engine.py").read_text()
    stats_fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == "stats")
    ret = next(n for n in ast.walk(stats_fn) if isinstance(n, ast.Return))
    names |= {"mave_" + k.value for k in ret.value.keys if isinstance(k, ast.Constant)}
    return names


@unittest.skipUnless(HAVE_YAML, "PyYAML not installed")
class TestAlertRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import yaml

        cls.doc = yaml.safe_load((OPS / "alerts.yml").read_text())
        cls.rules = [r for g in cls.doc["groups"] for r in g["rules"]]
        cls.prom = yaml.safe_load((OPS / "prometheus.yml").read_text())

    def test_every_rule_is_complete(self):
        names = [r["alert"] for r in self.rules]
        self.assertEqual(len(names), len(set(names)), "duplicate alert names")
        self.assertGreaterEqual(len(names), 5)
        for r in self.rules:
            self.assertIn(r["labels"]["severity"], ("critical", "warning", "info"), r["alert"])
            self.assertTrue(r["annotations"]["summary"] and r["annotations"]["description"], r["alert"])
            self.assertRegex(r["for"], r"^\d+[smh]$", r["alert"])

    def test_only_exported_metrics_are_used(self):
        known = exported_metric_names() | {"up"}
        for r in self.rules:
            used = set(re.findall(r"\b(mave_[a-z_]+|up)\b(?=\s*[{\[<>=!/)]|\s|$)", r["expr"]))
            self.assertTrue(used, r["alert"])
            self.assertLessEqual(used, known, f"{r['alert']} uses unknown metrics {used - known}")

    def test_label_values_exist(self):
        for r in self.rules:
            for v in re.findall(r'status="([^"]+)"', r["expr"]):
                self.assertIn(v, ("2xx", "3xx", "4xx", "5xx"), r["alert"])

    def test_expressions_are_balanced(self):
        for r in self.rules:
            e = r["expr"]
            self.assertEqual(e.count("("), e.count(")"), r["alert"])
            self.assertEqual(e.count("["), e.count("]"), r["alert"])
            self.assertEqual(e.count("{"), e.count("}"), r["alert"])
            self.assertEqual(e.count('"') % 2, 0, r["alert"])

    def test_search_route_regex_matches_both_mount_points(self):
        r = next(x for x in self.rules if x["alert"] == "MaveSlowSearch")
        pattern = re.search(r'route=~"([^"]+)"', r["expr"]).group(1)
        for route in ("/api/v1/search", "/search"):
            self.assertRegex(route, f"^(?:{pattern})$")
        self.assertNotRegex("/api/v1/suggest", f"^(?:{pattern})$")

    def test_prometheus_config_loads_the_rules(self):
        self.assertTrue(any(str(f).endswith("alerts.yml") for f in self.prom["rule_files"]))


class FakeDocker(unittest.TestCase):
    """Runs ops/restore_postgres.sh with a stand-in `docker` that logs every call."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        self.log = t / "docker.log"
        fake = t / "bin" / "docker"
        fake.parent.mkdir()
        fake.write_text(
            "#!/bin/sh\n"
            'echo "docker $*" >> "$FAKE_LOG"\n'
            'case "$*" in\n'
            '  *pg_restore*) cat > /dev/null; [ "${FAKE_FAIL_RESTORE:-}" = 1 ] && exit 1; exit 0 ;;\n'
            '  *"SELECT count(*)"*) echo 42 ;;\n'
            '  *"MAX(version)"*) echo "${FAKE_VERSION:-5}" ;;\n'
            "esac\n"
            "exit 0\n"
        )
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.dump = t / "mave-2026-10-04.dump"
        self.dump.write_bytes(b"PGDMP-fake")
        self.env = {**os.environ, "PATH": f"{fake.parent}:{os.environ['PATH']}", "FAKE_LOG": str(self.log)}
        self.env.pop("MAVE_RESTORE_CONFIRM", None)

    def run_script(self, *args, **env):
        p = subprocess.run(["sh", str(OPS / "restore_postgres.sh"), *map(str, args)], capture_output=True, text=True,
                           env={**self.env, **env}, stdin=subprocess.DEVNULL, timeout=30)
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        return p, calls


class TestRestoreScript(FakeDocker):
    def test_bad_usage_never_touches_docker(self):
        for args in ((), ("/nonexistent.dump",), (self.dump, "--bogus")):
            p, calls = self.run_script(*args)
            self.assertEqual(p.returncode, 2, args)
            self.assertEqual(calls, [], args)
        empty = Path(self.tmp.name) / "empty.dump"
        empty.write_bytes(b"")
        p, calls = self.run_script(empty, "--verify")
        self.assertEqual((p.returncode, calls), (2, []))

    def test_verify_restores_into_a_scratch_database_only(self):
        p, calls = self.run_script(self.dump, "--verify")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("users: 42 rows", p.stdout)
        self.assertIn("schema version: 5", p.stdout)
        restores = [c for c in calls if "pg_restore" in c]
        self.assertEqual(len(restores), 1)
        self.assertIn("-d mave_verify", restores[0])
        self.assertFalse(any("stop api" in c or "--clean" in c for c in calls))  # the live database/API are untouched
        self.assertIn("DROP DATABASE IF EXISTS mave_verify", calls[-1])  # scratch database is cleaned up

    def test_verify_fails_for_a_dump_that_is_not_a_mave_backup_and_still_cleans_up(self):
        p, calls = self.run_script(self.dump, "--verify", FAKE_VERSION="0")
        self.assertEqual(p.returncode, 1)
        self.assertIn("not a MAVE backup", p.stderr)
        self.assertIn("DROP DATABASE", calls[-1])

    def test_verify_fails_when_pg_restore_fails_and_still_cleans_up(self):
        p, calls = self.run_script(self.dump, "--verify", FAKE_FAIL_RESTORE="1")
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn("OK:", p.stdout)
        self.assertIn("DROP DATABASE", calls[-1])

    def test_live_restore_needs_explicit_confirmation(self):
        p, calls = self.run_script(self.dump)
        self.assertEqual(p.returncode, 3)
        self.assertIn("MAVE_RESTORE_CONFIRM=mave", p.stderr)
        self.assertEqual(calls, [])
        p, calls = self.run_script(self.dump, MAVE_RESTORE_CONFIRM="yes")  # anything but the exact word is refused
        self.assertEqual((p.returncode, calls), (3, []))

    def test_confirmed_live_restore_stops_restores_then_starts_the_api(self):
        p, calls = self.run_script(self.dump, MAVE_RESTORE_CONFIRM="mave")
        self.assertEqual(p.returncode, 0, p.stderr)
        order = [next(i for i, c in enumerate(calls) if key in c) for key in ("stop api", "pg_restore", "start api")]
        self.assertEqual(order, sorted(order))
        self.assertIn("--clean", calls[order[1]])
        self.assertIn("-d mave ", calls[order[1]] + " ")

    def test_failed_live_restore_leaves_the_api_stopped(self):
        p, calls = self.run_script(self.dump, MAVE_RESTORE_CONFIRM="mave", FAKE_FAIL_RESTORE="1")
        self.assertNotEqual(p.returncode, 0)
        self.assertTrue(any("stop api" in c for c in calls))
        self.assertFalse(any("start api" in c for c in calls))
        self.assertIn("RESTORE FAILED", p.stderr)

    def test_shell_scripts_have_valid_syntax(self):
        for name in ("restore_postgres.sh", "backup_postgres.sh"):
            r = subprocess.run(["sh", "-n", str(OPS / name)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
