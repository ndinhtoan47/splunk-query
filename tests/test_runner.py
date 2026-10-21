import importlib.util
import io
import json
import os
import pathlib
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "splunk-query" / "scripts" / "splunk_query.py"
PATTERNS = ROOT / "skills" / "splunk-query" / "reference" / "spl-patterns.md"

spec = importlib.util.spec_from_file_location("splunk_query", SCRIPT)
sq = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sq)


class TempEnv(unittest.TestCase):
    """Point config, notes and state at a temp folder and clear SPLUNK_QUERY_* variables."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        env = {k: v for k, v in os.environ.items() if not k.startswith("SPLUNK_QUERY_")}
        env["SPLUNK_QUERY_CONFIG"] = os.path.join(self.tmp.name, "config", "config.json")
        env["SPLUNK_QUERY_HOME"] = os.path.join(self.tmp.name, "state")
        self.env = mock.patch.dict(os.environ, env, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def write_config(self, data):
        path = os.environ["SPLUNK_QUERY_CONFIG"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(data, fh)

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = sq.main(argv)
        return code, out.getvalue(), err.getvalue()


class ConfigTests(TempEnv):
    def test_parse_plain_and_browser_urls(self):
        self.assertEqual(sq.parse_splunk_url("https://x.example.com/"), ("https://x.example.com", None, None))
        self.assertEqual(sq.parse_splunk_url("https://x.example.com/en-GB/app/ops/search?q=1"),
                         ("https://x.example.com", "en-GB", "ops"))
        self.assertEqual(sq.parse_splunk_url("https://proxy.example.com/splunk/en-US/app/search/search"),
                         ("https://proxy.example.com/splunk", "en-US", "search"))

    def test_rejects_non_https(self):
        with self.assertRaises(sq.Fail) as ctx:
            sq.parse_splunk_url("http://x.example.com")
        self.assertEqual(ctx.exception.code, sq.EXIT_CONFIG)

    def test_not_configured_is_exit_5(self):
        with self.assertRaises(sq.Fail) as ctx:
            sq.load_config()
        self.assertEqual(ctx.exception.code, sq.EXIT_CONFIG)
        self.assertIsNone(sq.load_config(required=False))

    def test_precedence_flag_env_file(self):
        self.write_config({"url": "https://file.example.com", "app": "fileapp", "locale": "en-GB"})
        cfg = sq.load_config()
        self.assertEqual((cfg.url, cfg.app, cfg.locale), ("https://file.example.com", "fileapp", "en-GB"))
        os.environ["SPLUNK_QUERY_URL"] = "https://env.example.com"
        os.environ["SPLUNK_QUERY_APP"] = "envapp"
        cfg = sq.load_config()
        self.assertEqual((cfg.url, cfg.app), ("https://env.example.com", "envapp"))
        self.assertEqual(sq.load_config(app="flagapp").app, "flagapp")

    def test_defaults(self):
        self.write_config({"url": "https://x.example.com"})
        cfg = sq.load_config()
        self.assertEqual((cfg.app, cfg.locale), ("search", "en-US"))
        self.assertEqual(cfg.web("/splunkd/__raw/services"), "https://x.example.com/en-US/splunkd/__raw/services")

    def test_configure_writes_private_file(self):
        code, out, _err = self.run_main(["configure", "--url", "https://x.example.com/en-US/app/ops/search"])
        self.assertEqual(code, 0)
        path = os.environ["SPLUNK_QUERY_CONFIG"]
        with open(path) as fh:
            self.assertEqual(json.load(fh), {"url": "https://x.example.com", "locale": "en-US", "app": "ops"})
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertIn("app: ops", out)

    def test_instance_binding_and_sso_host(self):
        cfg = sq.Config("https://a.example.com", "search", "en-US")
        sq.check_instance(cfg)
        sq.record_instance(cfg)
        sq.record_instance(cfg, "login.idp.example.com")
        self.assertEqual(sq.read_instance(), {"url": "https://a.example.com", "sso_host": "login.idp.example.com"})
        with self.assertRaises(sq.Fail) as ctx:
            sq.check_instance(sq.Config("https://b.example.com", "search", "en-US"))
        self.assertEqual(ctx.exception.code, sq.EXIT_LOGIN)

    def test_link(self):
        cfg = sq.Config("https://x.example.com", "ops", "en-US")
        link = cfg.search_link("ops", "search index=main action=crash", "-4h", None)
        self.assertEqual(link, "https://x.example.com/en-US/app/ops/search?q=search%20index%3Dmain%20action%3Dcrash&earliest=-4h")


class SearchRuleTests(TempEnv):
    def test_inline_earliest_detection(self):
        self.assertTrue(sq.has_inline_earliest("index=main earliest=-1h action=x"))
        self.assertTrue(sq.has_inline_earliest("earliest = -1d index=main"))
        self.assertFalse(sq.has_inline_earliest('index=main "earliest=-1h"'))
        self.assertFalse(sq.has_inline_earliest("index=main | stats earliest(_time) as first"))
        self.assertFalse(sq.has_inline_earliest("index=main my_earliest=3"))

    def test_time_is_required(self):
        with self.assertRaises(sq.Fail) as ctx:
            sq.check_search("index=main action=crash", None, False)
        self.assertEqual(ctx.exception.code, sq.EXIT_SEARCH)
        self.assertFalse(sq.check_search("index=main action=crash", "-1h", False))
        self.assertTrue(sq.check_search("earliest=-1h index=main", None, False))

    def test_write_gate(self):
        with self.assertRaises(sq.Fail):
            sq.check_search("earliest=-1h index=main | collect index=x", None, False)
        self.assertTrue(sq.check_search("earliest=-1h index=main | outputlookup x.csv", None, True))

    def test_search_prefix(self):
        self.assertEqual(sq.Search("q", "index=main").sent, "search index=main")
        self.assertEqual(sq.Search("q", "| makeresults").sent, "| makeresults")

    def test_query_without_time_exits_before_browser(self):
        os.environ["SPLUNK_QUERY_URL"] = "https://x.example.com"
        with mock.patch.object(sq, "load_playwright", side_effect=AssertionError):
            code, _out, err = self.run_main(["query", "index=main action=crash"])
        self.assertEqual(code, sq.EXIT_SEARCH)
        self.assertIn("no time range", err)


class OutputTests(unittest.TestCase):
    rows = [{"action": "crash", "c": "3", "_time": "t1", "_cd": "x"}, {"action": "login", "c": ["1", "2"], "_time": "t2"}]

    def render(self, fmt, wanted=None, tag=None, rows=None):
        out = io.StringIO()
        sq.print_results(["action", "c", "_time", "_cd"], self.rows if rows is None else rows, fmt, wanted, out, tag=tag)
        return out.getvalue()

    def test_json_and_tag(self):
        first = json.loads(self.render("json", tag="s1").splitlines()[0])
        self.assertEqual(list(first)[0], "search")

    def test_table(self):
        text = self.render("table")
        self.assertNotIn("_cd", text)
        self.assertIn("1 | 2", text)
        self.assertEqual(self.render("table", rows=[]), "(no rows)\n")

    def test_csv_wanted(self):
        self.assertEqual(self.render("csv", wanted=["action"]).splitlines(), ["action", "crash", "login"])

    def test_truncation_report(self):
        cfg = sq.Config("https://x.example.com", "search", "en-US")
        search = sq.Search("s", "index=main", "-1h", count=2)
        search.results, search.job, search.sid = [{}, {}], {"resultCount": "10", "runDuration": "1.5"}, "sid1"
        err = io.StringIO()
        with redirect_stderr(err):
            sq.report(cfg, search, named=True)
        self.assertIn("s: TRUNCATED: showing 2 of 10 rows", err.getvalue())


class BatchTests(unittest.TestCase):
    spec = {
        "params": {"INDEX": "index", "SERVICE": "host part"},
        "searches": [
            {"name": "a", "spl": "index=<INDEX> host=*<SERVICE>* | stats count", "earliest": "-4h"},
            {"name": "b", "spl": "index=<INDEX> | stats count by foo | foreach x_* [eval y='<<FIELD>>']", "earliest": "-1h"},
        ],
    }

    def test_fill_params_and_overrides(self):
        searches = sq.build_searches(self.spec, {"INDEX": "main", "SERVICE": "api"}, "ops", earliest="-2d")
        self.assertEqual(searches[0].spl, "index=main host=*api* | stats count")
        self.assertIn("'<<FIELD>>'", searches[1].spl)
        self.assertEqual([s.earliest for s in searches], ["-2d", "-2d"])

    def test_missing_and_unknown_params(self):
        with self.assertRaises(sq.Fail) as ctx:
            sq.build_searches(self.spec, {"INDEX": "main"}, "search")
        self.assertIn("SERVICE", str(ctx.exception))
        with self.assertRaises(sq.Fail) as ctx:
            sq.build_searches(self.spec, {"INDEX": "main", "SERVICE": "api", "X": "1"}, "search")
        self.assertIn("Unknown --param X", str(ctx.exception))

    def test_example_in_spl_patterns_loads(self):
        text = PATTERNS.read_text(encoding="utf-8")
        example = text.split("```json\n", 1)[1].split("\n```", 1)[0]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write(example)
        try:
            batch = sq.load_batch(fh.name)
            self.assertEqual(len(sq.build_searches(batch, {"INDEX": "main", "SERVICE": "api"}, "search")), 2)
        finally:
            os.unlink(fh.name)


class FakeResponse:
    def __init__(self, status, body=None):
        self.status = status
        self.body = body or {}
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self.body


class FakeSession:
    """Fake Splunk REST: each job is done after `polls` status checks; dispatches beyond `quota` get a 503."""

    def __init__(self, polls=2, quota=None, fail=(), knowledge=None):
        self.polls, self.quota, self.fail, self.knowledge = polls, quota, set(fail), knowledge or {}
        self.jobs, self.active, self.max_active, self.cancelled, self.calls = {}, set(), 0, [], []

    def api(self, method, path, params=None, form=None, timeout=None):
        self.calls.append((method, path, params))
        if path in self.knowledge:
            return FakeResponse(*self.knowledge[path])
        if method == "POST" and path.endswith("/search/jobs"):
            if self.quota is not None and len(self.active) >= self.quota:
                return FakeResponse(503, {"messages": [{"type": "FATAL", "text": "maximum number of concurrent searches"}]})
            if form["search"] in self.fail:
                return FakeResponse(400, {"messages": [{"type": "FATAL", "text": "Unknown search command"}]})
            sid = "sid%d" % len(self.jobs)
            self.jobs[sid] = {"left": self.polls, "spl": form["search"]}
            self.active.add(sid)
            self.max_active = max(self.max_active, len(self.active))
            return FakeResponse(201, {"sid": sid})
        if path.endswith("/control"):
            self.cancelled.append(path)
            return FakeResponse(200)
        sid = path.split("/jobs/")[1].split("/")[0]
        job = self.jobs[sid]
        if path.endswith("/results"):
            return FakeResponse(200, {"fields": [{"name": "x"}], "results": [{"x": job["spl"]}], "messages": []})
        job["left"] -= 1
        done = job["left"] <= 0
        if done:
            self.active.discard(sid)
        return FakeResponse(200, {"entry": [{"content": {"isDone": done, "resultCount": "1"}}]})


class RunSearchesTests(unittest.TestCase):
    def run_all(self, session, searches, timeout=300):
        clock = iter(range(0, 100000)).__next__
        with redirect_stderr(io.StringIO()):
            return sq.run_searches(session, "me", searches, timeout, sleep=lambda s: None, clock=clock)

    def test_runs_at_most_four_at_once(self):
        session = FakeSession()
        searches = [sq.Search("s%d" % i, "| makeresults count=%d" % i, "0") for i in range(6)]
        self.run_all(session, searches)
        self.assertEqual(session.max_active, 4)
        self.assertTrue(all(s.done and not s.error and s.results for s in searches))

    def test_quota_full_waits(self):
        session = FakeSession(quota=2)
        searches = [sq.Search("s%d" % i, "| makeresults count=%d" % i, "0") for i in range(5)]
        self.run_all(session, searches)
        self.assertEqual(session.max_active, 2)
        self.assertTrue(all(s.done and not s.error for s in searches))

    def test_one_failure_does_not_stop_the_rest(self):
        session = FakeSession(fail={"| bad"})
        searches = [sq.Search("bad", "| bad", "0"), sq.Search("good", "| makeresults", "0")]
        self.run_all(session, searches)
        self.assertIn("Unknown search command", searches[0].error)
        self.assertIsNone(searches[1].error)

    def test_timeout_cancels(self):
        session = FakeSession(polls=10 ** 6)
        searches = [sq.Search("slow", "| makeresults", "0")]
        self.run_all(session, searches, timeout=5)
        self.assertIn("cancelled", searches[0].error)
        self.assertEqual(len(session.cancelled), 1)


def entry(name, app, **content):
    return {"name": name, "acl": {"app": app}, "content": content}


class KnowledgeTests(unittest.TestCase):
    def fake(self):
        saved = {"entry": [
            entry("Zeta error report", "other", search="index=main error | stats count"),
            entry("Login failures", "ops", search="index=main action=login_failed"),
            entry("Errors by service", "ops", search="index=main error | stats count by host"),
            entry("Old errors", "ops", search="index=main error", disabled="1"),
        ]}
        macros = {"entry": [entry("app_errors", "ops", definition="(error OR exception) NOT noise", args="")]}
        return FakeSession(knowledge={
            "/servicesNS/-/-/saved/searches": (200, saved),
            "/servicesNS/-/-/configs/conf-macros": (200, macros),
            "/servicesNS/-/-/saved/eventtypes": (403, {}),
            "/servicesNS/-/-/data/ui/views": (200, {"entry": [entry("svc_health", "ops", label="Service errors")]}),
        })

    def test_matches_ranks_and_skips_disabled(self):
        session = self.fake()
        found = sq.collect_knowledge(session, list(sq.KNOWLEDGE_TYPES), ["errors"], 10, prefer_app="ops")
        saved, total = found["saved"]
        self.assertEqual([i["name"] for i in saved], ["Errors by service", "Zeta error report"])
        self.assertEqual(total, 2)
        self.assertEqual(found["macro"][0][0]["spl"], "(error OR exception) NOT noise")
        self.assertIsNone(found["eventtype"])
        self.assertEqual(found["dashboard"][0][0]["name"], "svc_health")
        dashboard_call = [c for c in session.calls if c[1].endswith("/data/ui/views")][0]
        self.assertEqual(dashboard_call[2], {"count": "0", "f": "label"})

    def test_all_words_must_match_and_limit(self):
        found = sq.collect_knowledge(self.fake(), ["saved"], ["error", "service"], 10)
        self.assertEqual([i["name"] for i in found["saved"][0]], ["Errors by service"])
        found = sq.collect_knowledge(self.fake(), ["saved"], ["error"], 1)
        self.assertEqual((len(found["saved"][0]), found["saved"][1]), (1, 2))

    def test_format(self):
        text = sq.format_knowledge(sq.collect_knowledge(self.fake(), list(sq.KNOWLEDGE_TYPES), ["error"], 1, "ops"))
        self.assertIn("== saved searches: 2 (showing 1; raise --count or add words) ==", text)
        self.assertIn("== event types: not readable with this account ==", text)
        self.assertIn("panels: dashboard svc_health --app ops", text)


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class NotesTests(TempEnv):
    def add(self, title, spl="earliest=-1h index=main error | stats count", *extra):
        return self.run_main(["notes", "add", "--title", title, "--spl", spl] + list(extra))

    def test_add_show_remove(self):
        code, out, _err = self.add("Top errors", "earliest=-4h index=main error | stats count by punct",
                                   "--use-when", "top errors", "--read-it", "count per shape")
        self.assertEqual(code, 0)
        self.assertIn("saved 'Top errors'", out)
        self.add("User errors", "earliest=-7d index=main user_id=<USER_ID> error", "--params", "<USER_ID> numeric id")
        path = sq.notes_path()
        self.assertEqual(path, os.path.join(self.tmp.name, "config", "notes.md"))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        text = read(path)
        self.assertTrue(text.startswith("# splunk-query notes"))
        self.assertIn("Status: verified ", text)
        _code, out, _err = self.run_main(["notes", "show", "USER"])
        self.assertIn("### User errors", out)
        self.assertNotIn("### Top errors", out)
        _code, out, _err = self.run_main(["notes", "show", "nothing-like-this"])
        self.assertIn("No notes match", out)
        code, out, _err = self.run_main(["notes", "remove", "top", "errors"])
        self.assertEqual(code, 0)
        self.assertNotIn("Top errors", read(path))
        self.assertIn("User errors", read(path))

    def test_add_rules(self):
        code, _out, err = self.add("No time", "index=main error")
        self.assertEqual(code, sq.EXIT_SEARCH)
        self.assertIn("time range inline", err)
        self.add("Same")
        code, _out, err = self.add("same")
        self.assertEqual(code, sq.EXIT_SEARCH)
        self.assertIn("--replace", err)
        code, out, _err = self.add("same", "earliest=-2h index=main error", "--replace")
        self.assertEqual(code, 0)
        self.assertEqual(read(sq.notes_path()).count("### "), 1)

    def test_show_without_notes(self):
        _code, out, _err = self.run_main(["notes", "show"])
        self.assertIn("No notes yet", out)

    def test_notes_override(self):
        os.environ["SPLUNK_QUERY_NOTES"] = os.path.join(self.tmp.name, "elsewhere.md")
        self.assertEqual(sq.notes_path(), os.path.join(self.tmp.name, "elsewhere.md"))


SIMPLE_XML = """<form>
  <label>Service health</label>
  <search id="base"><query>index=main action=request | stats count by host</query>
    <earliest>$time1.earliest$</earliest><latest>$time1.latest$</latest></search>
  <fieldset>
    <input type="time" token="time1"><default><earliest>-24h@h</earliest><latest>now</latest></default></input>
    <input type="dropdown" token="idx"><label>Index</label><default>index=main</default></input>
  </fieldset>
  <row><panel><title>Requests per host</title>
    <table><search base="base"><query>| sort - count</query></search></table>
    <chart><title>Crashes</title><search><query>$idx$ action=crash | timechart count</query>
      <earliest>-4h</earliest></search></chart>
  </panel></row>
</form>"""


class DashboardTests(unittest.TestCase):
    def test_simple_xml(self):
        data = sq.parse_dashboard(SIMPLE_XML)
        first, second = data["panels"]
        self.assertEqual(first["spl"], "index=main action=request | stats count by host | sort - count")
        self.assertEqual((second["title"], second["earliest"]), ("Crashes", "-4h"))
        self.assertIn("tokens: $idx$", sq.format_dashboard("service_health", "ops", data))

    def test_studio(self):
        studio = {
            "dataSources": {
                "base": {"type": "ds.search", "options": {"query": "index=main user_id=$uid$"}},
                "chain": {"type": "ds.chain", "options": {"extend": "base", "query": "| stats count by action"}},
            },
            "visualizations": {"v1": {"type": "splunk.table", "title": "Actions", "dataSources": {"primary": "chain"}}},
            "defaults": {"dataSources": {"ds.search": {"options": {"queryParameters": {"earliest": "-4d@d"}}}}},
        }
        xml = '<dashboard version="2"><label>U</label><definition><![CDATA[%s]]></definition></dashboard>' % json.dumps(studio)
        panel = sq.parse_dashboard(xml)["panels"][0]
        self.assertEqual((panel["spl"], panel["earliest"]), ("index=main user_id=$uid$ | stats count by action", "-4d@d"))


class DoctorTests(TempEnv):
    def test_network_failure_is_exit_3_and_checks_sso_host(self):
        os.environ["SPLUNK_QUERY_URL"] = "https://x.example.com"
        sq.record_instance(sq.load_config(), "login.idp.example.com")
        checked = []
        args = sq.build_parser().parse_args(["doctor"])
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(sq.importlib.util, "find_spec", return_value=True):
            with self.assertRaises(sq.Fail) as ctx:
                sq.cmd_doctor(args, network=lambda host: checked.append(host) or "DNS lookup failed")
        self.assertEqual(ctx.exception.code, sq.EXIT_VPN)
        self.assertEqual(checked, ["x.example.com", "login.idp.example.com"])


if __name__ == "__main__":
    unittest.main()
