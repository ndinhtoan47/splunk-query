#!/usr/bin/env python3
"""Run Splunk searches through a saved browser session (SSO or Splunk login) and a headless Chrome.

Commands:
  configure        save the Splunk URL, default app and locale
  doctor           check Python, Playwright, Chrome, config and network (never opens a window)
  query "<SPL>"    run one search and print the results
  batch FILE       run a JSON list of named searches in one browser session
  knowledge WORDS  find saved searches, macros, event types and dashboards matching words
  macro NAME       print a search macro's definition
  dashboard NAME   print the searches behind a dashboard's panels
  notes ACTION     show, add or remove searches saved in the local notes file
  check            report whether the saved session is valid (never opens a window)
  login            open a browser window and log in now
  logout           delete the saved session (--all also deletes the browser profile)

Exit codes: 0 ok, 1 other error, 2 search rejected/failed, 3 Splunk or sign-in page unreachable
(VPN/network), 4 login needed/failed, 5 not configured.
"""
import argparse
import csv
import importlib.util
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote, urlencode, urlparse

EXIT_ERROR, EXIT_SEARCH, EXIT_VPN, EXIT_LOGIN, EXIT_CONFIG = 1, 2, 3, 4, 5
DEFAULT_APP = "search"
DEFAULT_LOCALE = "en-US"
DEFAULT_COUNT = 100
MAX_PARALLEL = 4

LOCALE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
APP_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
SEARCH_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
PLACEHOLDER = re.compile(r"(?<!<)<([A-Z][A-Z0-9_]*)>(?!>)")
QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"')
INLINE_EARLIEST = re.compile(r"(?<![\w.])earliest\s*=")
DASH_TOKEN = re.compile(r"\$([A-Za-z0-9_.:-]+)\$")
QUOTA = re.compile(r"concurrent|quota|maximum number of", re.IGNORECASE)
WRITE_COMMANDS = re.compile(
    r"\|\s*(delete|collect|outputlookup|outputcsv|outputtext|sendemail|sendalert|script|run|"
    r"tscollect|mcollect|meventcollect)\b",
    re.IGNORECASE,
)
NETWORK_MARKERS = (
    "ERR_NAME_NOT_RESOLVED", "ERR_CONNECTION", "ERR_ADDRESS_UNREACHABLE", "ERR_INTERNET_DISCONNECTED",
    "ERR_NETWORK", "ERR_TIMED_OUT", "ERR_TUNNEL", "ERR_PROXY", "ENOTFOUND", "ECONNREFUSED",
    "ECONNRESET", "ETIMEDOUT", "EHOSTUNREACH", "ENETUNREACH", "Timeout",
)
SIGN_IN_FORM = ('input[type="password"], input[name="identifier"], input[name="username"], '
                'input[type="email"], input[name="loginfmt"]')
# kind: (label, plural, endpoint, field holding the SPL, single field to fetch or None for all)
KNOWLEDGE_TYPES = {
    "saved": ("saved search", "saved searches", "/servicesNS/-/-/saved/searches", "search", None),
    "macro": ("macro", "macros", "/servicesNS/-/-/configs/conf-macros", "definition", None),
    "eventtype": ("event type", "event types", "/servicesNS/-/-/saved/eventtypes", "search", None),
    "dashboard": ("dashboard", "dashboards", "/servicesNS/-/-/data/ui/views", None, "label"),
}
NOTES_HEADER = (
    "# splunk-query notes\n\n"
    "Searches that proved right on this Splunk instance, saved by the agent. Private to this\n"
    "machine: never commit, publish or paste them elsewhere.\n"
)
NOT_CONFIGURED = (
    "Splunk is not configured. Run: splunk_query.py configure --url https://<instance> --app <app> "
    "(or set SPLUNK_QUERY_URL)."
)

# Set by load_playwright(); the browser code paths only run after it.
PwError = PwTimeout = None


class Fail(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class SessionExpired(Exception):
    pass


class SearchError(Exception):
    """A failure that ends one search but not the others in a batch."""


class QuotaFull(Exception):
    pass


def log(message):
    print("[splunk] " + message, file=sys.stderr, flush=True)


def state_dir():
    return os.path.expanduser(os.environ.get("SPLUNK_QUERY_HOME") or "~/.cache/splunk-query")


def state_file():
    return os.path.join(state_dir(), "storage_state.json")


def profile_dir():
    return os.path.join(state_dir(), "chrome-profile")


def instance_file():
    return os.path.join(state_dir(), "instance.json")


def config_path():
    return os.path.expanduser(os.environ.get("SPLUNK_QUERY_CONFIG") or "~/.config/splunk-query/config.json")


def load_playwright():
    global PwError, PwTimeout
    try:
        from playwright.sync_api import Error, TimeoutError, sync_playwright
    except ImportError:
        raise Fail(EXIT_ERROR, "Python package 'playwright' is missing. Install it: python3 -m pip install --user playwright")
    PwError, PwTimeout = Error, TimeoutError
    return sync_playwright


def parse_splunk_url(raw):
    """Return (base_url, locale, app) from a base URL or any Splunk Web URL pasted from the browser."""
    raw = (raw or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme != "https" or not parsed.netloc:
        raise Fail(EXIT_CONFIG, "Splunk URL must start with https://, got %r" % raw)
    parts = [p for p in parsed.path.split("/") if p]
    locale = app = None
    for i, part in enumerate(parts):
        if LOCALE.match(part):
            locale = part
            if i + 2 < len(parts) and parts[i + 1] == "app":
                app = parts[i + 2]
            parts = parts[:i]
            break
    base = "https://%s%s" % (parsed.netloc, "".join("/" + p for p in parts))
    return base, locale, app


class Config:
    def __init__(self, url, app, locale):
        self.url = url
        self.app = app
        self.locale = locale
        self.host = urlparse(url).netloc

    def web(self, path):
        return "%s/%s%s" % (self.url, self.locale, path)

    def search_url(self, app):
        return self.web("/app/%s/search" % quote(app, safe=""))

    def search_link(self, app, spl, earliest, latest):
        params = {"q": spl}
        if earliest is not None:
            params["earliest"] = earliest
        if latest:
            params["latest"] = latest
        return "%s?%s" % (self.search_url(app), urlencode(params, quote_via=quote))


def read_config_file():
    path = config_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as fh:
            data = json.load(fh)
    except ValueError:
        raise Fail(EXIT_CONFIG, "Config file %s is not valid JSON; run configure again." % path)
    return data if isinstance(data, dict) else {}


def check_app(app):
    if not APP_NAME.match(app or ""):
        raise Fail(EXIT_CONFIG, "Invalid app name %r." % app)
    return app


def check_locale(locale):
    if not LOCALE.match(locale or ""):
        raise Fail(EXIT_CONFIG, "Invalid locale %r (expected like en-US)." % locale)
    return locale


def load_config(app=None, required=True):
    """Settings precedence: --app flag, then SPLUNK_QUERY_* environment, then the config file."""
    data = read_config_file()
    url = os.environ.get("SPLUNK_QUERY_URL") or data.get("url")
    if not url:
        if required:
            raise Fail(EXIT_CONFIG, NOT_CONFIGURED)
        return None
    base, url_locale, url_app = parse_splunk_url(url)
    app = app or os.environ.get("SPLUNK_QUERY_APP") or data.get("app") or url_app or DEFAULT_APP
    locale = os.environ.get("SPLUNK_QUERY_LOCALE") or data.get("locale") or url_locale or DEFAULT_LOCALE
    return Config(base, check_app(app), check_locale(locale))


def read_instance():
    try:
        with open(instance_file()) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def check_instance(cfg):
    saved = read_instance().get("url")
    if saved and saved != cfg.url:
        raise Fail(EXIT_LOGIN, "The saved session belongs to %s, not %s. Run: splunk_query.py logout --all "
                               "(or set SPLUNK_QUERY_HOME to another folder)." % (saved, cfg.url))


def record_instance(cfg, sso_host=None):
    """Bind the saved session to this instance and remember the sign-in host for doctor."""
    data = read_instance()
    changed = False
    if not data.get("url"):
        data["url"], changed = cfg.url, True
    if sso_host and data.get("sso_host") != sso_host:
        data["sso_host"], changed = sso_host, True
    if changed:
        os.makedirs(state_dir(), mode=0o700, exist_ok=True)
        with open(instance_file(), "w") as fh:
            json.dump(data, fh)


def unreachable(exc):
    text = str(exc).splitlines()[0]
    return Fail(EXIT_VPN, "Cannot reach Splunk or its sign-in page (%s). Check the VPN or network and retry." % text)


def is_network_error(exc):
    return (PwTimeout is not None and isinstance(exc, PwTimeout)) or any(m in str(exc) for m in NETWORK_MARKERS)


class Session:
    def __init__(self, pw, cfg, headless):
        self.cfg = cfg
        self.ctx = launch(pw, headless)
        self.restore_session_cookies()
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.nav_failure = None
        self.sso_host = None
        self.page.on("requestfailed", self._on_request_failed)

    def _on_request_failed(self, request):
        # SAML hops to the sign-in page run after goto() returns, so their DNS/connect errors only surface here.
        if request.is_navigation_request() and request.failure:
            self.nav_failure = "%s at %s" % (request.failure, request.url.split("?")[0])

    def raise_if_unreachable(self):
        if self.nav_failure and is_network_error(self.nav_failure):
            raise unreachable(self.nav_failure)

    def restore_session_cookies(self):
        # Chrome drops cookies without an expiry (splunkd_*, SSO session ids) when the profile closes.
        if not os.path.exists(state_file()):
            return
        with open(state_file()) as fh:
            cookies = json.load(fh).get("cookies", [])
        if cookies:
            self.ctx.add_cookies(cookies)

    def close(self):
        self.ctx.close()

    def save(self):
        self.ctx.storage_state(path=state_file())
        os.chmod(state_file(), 0o600)

    def api(self, method, path, params=None, form=None, timeout=60000):
        headers = {"X-Requested-With": "XMLHttpRequest"}
        for cookie in self.ctx.cookies(self.cfg.url):
            if cookie["name"].startswith("splunkweb_csrf_token_"):
                headers["X-Splunk-Form-Key"] = cookie["value"]
        query = {"output_mode": "json"}
        query.update(params or {})
        try:
            return self.ctx.request.fetch(
                self.cfg.web("/splunkd/__raw" + path), method=method, params=query, form=form,
                headers=headers, max_redirects=0, timeout=timeout,
            )
        except PwError as exc:
            if is_network_error(exc):
                raise unreachable(exc)
            raise

    def whoami(self):
        resp = self.api("GET", "/services/authentication/current-context")
        if resp.status != 200:
            return None
        try:
            return resp.json()["entry"][0]["content"]["username"]
        except (ValueError, KeyError, IndexError):
            return None

    def on_splunk_app(self):
        url = urlparse(self.page.url)
        return url.netloc == self.cfg.host and "/app/" in url.path

    def note_sso_host(self):
        host = urlparse(self.page.url).netloc
        if host and host != self.cfg.host:
            self.sso_host = host

    def sign_in_wants_input(self):
        """True when the page is a login form (SSO provider or Splunk's own) waiting for the user."""
        url = urlparse(self.page.url)
        if url.netloc == self.cfg.host and "/account/login" not in url.path:
            return False
        try:
            return self.page.locator(SIGN_IN_FORM).first.is_visible()
        except PwError:
            return False

    def goto(self, url):
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except PwError as exc:
            if is_network_error(exc):
                raise unreachable(exc)
            raise

    def silent_sso(self, app):
        """Follow the SSO redirect headlessly; succeeds while the provider's session is still alive."""
        self.goto(self.cfg.search_url(app))
        deadline = time.time() + 30
        while time.time() < deadline:
            self.raise_if_unreachable()
            self.note_sso_host()
            if self.on_splunk_app():
                return True
            if self.sign_in_wants_input():
                return False
            self.page.wait_for_timeout(1000)
        return False

    def interactive_login(self, app, timeout):
        self.goto(self.cfg.search_url(app))
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.page.is_closed():
                raise Fail(EXIT_LOGIN, "Login window was closed before Splunk opened.")
            self.raise_if_unreachable()
            self.note_sso_host()
            if self.on_splunk_app() and self.whoami():
                return
            try:
                self.page.wait_for_timeout(1500)
            except PwError:
                raise Fail(EXIT_LOGIN, "Login window was closed before Splunk opened.")
        raise Fail(EXIT_LOGIN, "Login not finished within %ds." % timeout)


def launch(pw, headless):
    """Open the one persistent profile shared by headless runs and the login window."""
    os.makedirs(state_dir(), mode=0o700, exist_ok=True)
    try:
        return pw.chromium.launch_persistent_context(profile_dir(), channel="chrome", headless=headless)
    except PwError as exc:
        if "ProcessSingleton" in str(exc) or "SingletonLock" in str(exc):
            raise Fail(EXIT_ERROR, "Chrome profile %s is in use by another splunk_query run; wait and retry." % profile_dir())
        # Bundled Chromium is older than Chrome and cannot open a profile Chrome already upgraded.
        return pw.chromium.launch_persistent_context(profile_dir() + "-chromium", headless=headless)


def open_session(pw, cfg, app, interactive, login_timeout, force_login=False):
    """Return (session, username), logging in again when the saved cookies expired."""
    check_instance(cfg)
    session = Session(pw, cfg, headless=True)
    if not force_login:
        user = session.whoami()
        if user:
            record_instance(cfg)
            return session, user
        log("Splunk session expired; trying silent SSO.")
        if session.silent_sso(app):
            user = session.whoami()
            if user:
                session.save()
                record_instance(cfg, session.sso_host)
                log("Re-logged in as %s." % user)
                return session, user
    session.close()

    if not interactive:
        raise Fail(EXIT_LOGIN, "An interactive login is needed. Run: splunk_query.py login")
    log("Opening a Chrome window: log in and approve MFA if asked (waiting up to %ds)." % login_timeout)
    window = Session(pw, cfg, headless=False)
    try:
        window.interactive_login(app, login_timeout)
        window.save()
        sso_host = window.sso_host
    finally:
        window.close()

    session = Session(pw, cfg, headless=True)
    user = session.whoami()
    if not user:
        session.close()
        raise Fail(EXIT_LOGIN, "Login finished but Splunk still rejects the session.")
    record_instance(cfg, sso_host)
    log("Logged in as %s." % user)
    return session, user


def with_session(cfg, args, work):
    """Run work(session, user) once, logging in again if Splunk rejects the session midway."""
    sync_playwright = load_playwright()
    with sync_playwright() as pw:
        for attempt in range(2):
            session, user = open_session(pw, cfg, cfg.app, not args.no_interactive, args.login_timeout)
            try:
                result = work(session, user)
                session.save()
                return result
            except SessionExpired:
                if attempt:
                    raise Fail(EXIT_LOGIN, "Splunk keeps rejecting the session after re-login.")
                log("Session rejected midway; logging in again.")
            finally:
                session.close()


def json_body(resp):
    if "json" not in (resp.headers.get("content-type") or ""):
        return {}
    try:
        return resp.json()
    except ValueError:
        return {}


def messages_text(messages):
    if isinstance(messages, dict):
        messages = [{"type": k, "text": t} for k, texts in messages.items() for t in texts]
    return "; ".join("%s: %s" % (m.get("type"), m.get("text")) for m in messages or []) or "no message"


class Search:
    def __init__(self, name, spl, earliest=None, latest=None, count=DEFAULT_COUNT, app=DEFAULT_APP):
        self.name = name
        self.spl = spl.strip()
        self.earliest = earliest
        self.latest = latest
        self.count = DEFAULT_COUNT if count is None else count
        self.app = app
        self.sent = self.spl if self.spl.startswith("|") or self.spl.lower().startswith("search ") else "search " + self.spl
        self.sid = None
        self.started = None
        self.job = {}
        self.fields = []
        self.results = []
        self.messages = []
        self.error = None
        self.done = False

    @property
    def total(self):
        try:
            return int(float(self.job.get("resultCount") or 0))
        except (TypeError, ValueError):
            return 0


def has_inline_earliest(spl):
    return bool(INLINE_EARLIEST.search(QUOTED.sub('""', spl)))


def check_search(spl, earliest, allow_write, label="Search"):
    """Refuse empty searches, unapproved writes and searches with no time range. Returns True for inline earliest=."""
    if not spl.strip():
        raise Fail(EXIT_SEARCH, "%s is empty." % label)
    if WRITE_COMMANDS.search(spl) and not allow_write:
        raise Fail(EXIT_SEARCH, "%s contains a command that writes or sends data; pass --allow-write to run it." % label)
    inline = has_inline_earliest(spl)
    if not inline and earliest is None:
        raise Fail(EXIT_SEARCH, "%s has no time range. Put earliest= in the SPL or pass --earliest "
                                "(e.g. --earliest=-4h; --earliest=0 is all time). If the question does not say, "
                                "ask the user." % label)
    return inline


def jobs_path(user, app):
    return "/servicesNS/%s/%s/search/jobs" % (quote(user, safe=""), quote(app, safe=""))


def dispatch(session, user, search):
    form = {"search": search.sent}
    if search.earliest is not None:
        form["earliest_time"] = search.earliest
    if search.latest:
        form["latest_time"] = search.latest
    resp = session.api("POST", jobs_path(user, search.app), form=form)
    if resp.status in (401, 303):
        raise SessionExpired()
    body = json_body(resp)
    if resp.status not in (200, 201) or "sid" not in body:
        text = messages_text(body.get("messages"))
        if resp.status == 503 or QUOTA.search(text):
            raise QuotaFull(text)
        raise SearchError("Search rejected (HTTP %d): %s" % (resp.status, text))
    search.sid = body["sid"]


def job_path(user, search):
    return "%s/%s" % (jobs_path(user, search.app), quote(search.sid, safe=""))


def poll(session, user, search, timeout, clock):
    """Return True once the job is done; cancel it and raise SearchError after the timeout."""
    resp = session.api("GET", job_path(user, search))
    if resp.status == 401:
        raise SessionExpired()
    if resp.status != 200:
        raise SearchError("Job status failed (HTTP %d)." % resp.status)
    search.job = resp.json()["entry"][0]["content"]
    if search.job.get("isFailed") or search.job.get("dispatchState") == "FAILED":
        raise SearchError("Search failed: %s" % messages_text(search.job.get("messages")))
    if search.job.get("isDone"):
        return True
    if clock() - search.started > timeout:
        session.api("POST", job_path(user, search) + "/control", form={"action": "cancel"})
        raise SearchError("Still running after %ds; cancelled. Narrow the time range or put literal terms "
                          "before field filters." % timeout)
    return False


def fetch(session, user, search):
    resp = session.api("GET", job_path(user, search) + "/results", params={"count": str(search.count)}, timeout=120000)
    if resp.status == 401:
        raise SessionExpired()
    if resp.status == 204:
        data = {}
    elif resp.status != 200:
        raise SearchError("Fetching results failed (HTTP %d)." % resp.status)
    else:
        data = json_body(resp)
    search.results = data.get("results", [])
    search.fields = [f["name"] if isinstance(f, dict) else f for f in data.get("fields", [])]
    search.messages = [m for m in data.get("messages", []) if m.get("type") in ("WARN", "ERROR")]


def run_searches(session, user, searches, timeout, max_parallel=MAX_PARALLEL, sleep=time.sleep, clock=time.time):
    """Dispatch up to max_parallel jobs at once and collect each one's results. Finished searches are skipped."""
    pending = [s for s in searches if not s.done]
    for s in pending:
        s.sid = None
    running = []
    quota_waits = 0
    last_report = clock()
    while pending or running:
        while pending and len(running) < max_parallel:
            search = pending[0]
            try:
                dispatch(session, user, search)
            except QuotaFull as exc:
                if not running:
                    quota_waits += 1
                    if quota_waits > 60:
                        search.error, search.done = "Splunk search quota stayed full: %s" % exc, True
                        pending.pop(0)
                        continue
                    log("Splunk concurrent-search quota is full; waiting.")
                    sleep(5)
                break
            except SearchError as exc:
                search.error, search.done = str(exc), True
                pending.pop(0)
                continue
            search.started = clock()
            pending.pop(0)
            running.append(search)
            log("%s: job %s started." % (search.name, search.sid))

        still_running = []
        for search in running:
            try:
                if poll(session, user, search, timeout, clock):
                    fetch(session, user, search)
                    search.done = True
                else:
                    still_running.append(search)
            except SearchError as exc:
                search.error, search.done = str(exc), True
        running = still_running
        if running and clock() - last_report > 10:
            log("Running: " + ", ".join("%s %d%%" % (s.name, float(s.job.get("doneProgress", 0)) * 100) for s in running))
            last_report = clock()
        if pending or running:
            sleep(1)
    return searches


def report(cfg, search, named):
    prefix = search.name + ": " if named else ""
    if search.error:
        log("%sFAILED: %s" % (prefix, search.error))
    else:
        log("%s%d rows (job total %d, %.1fs). sid=%s" % (
            prefix, len(search.results), search.total, float(search.job.get("runDuration") or 0), search.sid))
        if search.total > len(search.results):
            hint = "raise --count; 0 = all" if search.count else "the server row limit was hit; aggregate more"
            log("%sTRUNCATED: showing %d of %d rows (%s)." % (prefix, len(search.results), search.total, hint))
        for message in search.messages:
            log("%s%s: %s" % (prefix, message.get("type"), message.get("text")))
    log("%slink: %s" % (prefix, cfg.search_link(search.app, search.sent, search.earliest, search.latest)))


def columns(fields, results, wanted):
    if wanted:
        return wanted
    cols = [f for f in fields if not f.startswith("_") or f in ("_time", "_raw")]
    if not cols and results:
        cols = list(results[0].keys())
    return cols


def cell(value):
    if isinstance(value, list):
        return " | ".join(str(v) for v in value)
    return "" if value is None else str(value)


def print_results(fields, results, fmt, wanted, out, tag=None):
    cols = columns(fields, results, wanted)
    if fmt == "json":
        for row in results:
            data = {c: row.get(c) for c in cols} if wanted else row
            if tag is not None:
                data = dict([("search", tag)] + list(data.items()))
            out.write(json.dumps(data, ensure_ascii=True) + "\n")
    elif fmt == "csv":
        writer = csv.writer(out)
        writer.writerow(cols)
        for row in results:
            writer.writerow([cell(row.get(c)) for c in cols])
    elif not results:
        out.write("(no rows)\n")
    else:
        rows = [[cell(row.get(c)).replace("\n", " ")[:80] for c in cols] for row in results]
        widths = [max([len(c)] + [len(r[i]) for r in rows]) for i, c in enumerate(cols)]
        out.write("  ".join(c.ljust(w) for c, w in zip(cols, widths)).rstrip() + "\n")
        out.write("  ".join("-" * w for w in widths) + "\n")
        for r in rows:
            out.write("  ".join(v.ljust(w) for v, w in zip(r, widths)).rstrip() + "\n")


def emit(args, writer):
    if args.out:
        with open(args.out, "w", newline="") as fh:
            writer(fh)
        log("Wrote %s" % args.out)
    else:
        writer(sys.stdout)


def wanted_fields(args):
    return [f.strip() for f in args.fields.split(",") if f.strip()] if args.fields else None


def load_batch(path):
    try:
        with open(path) as fh:
            spec = json.load(fh)
    except (OSError, ValueError) as exc:
        raise Fail(EXIT_SEARCH, "Cannot read batch file %s: %s" % (path, exc))
    problems = []
    if not isinstance(spec, dict) or not isinstance(spec.get("searches"), list) or not spec["searches"]:
        raise Fail(EXIT_SEARCH, "Batch file %s needs a non-empty \"searches\" list." % path)
    params = spec.get("params", {})
    if not isinstance(params, dict):
        problems.append("\"params\" must be an object of NAME: description")
    seen = set()
    for i, item in enumerate(spec["searches"]):
        if not isinstance(item, dict) or not isinstance(item.get("spl"), str):
            problems.append("search %d needs \"name\" and \"spl\"" % (i + 1))
            continue
        name = item.get("name", "")
        if not SEARCH_NAME.match(name) or name in seen:
            problems.append("search %d has a missing, invalid or duplicate name %r" % (i + 1, name))
        seen.add(name)
        undeclared = sorted(set(PLACEHOLDER.findall(item["spl"])) - set(params))
        if undeclared:
            problems.append("search %s uses undeclared params: %s" % (name, ", ".join(undeclared)))
    if problems:
        raise Fail(EXIT_SEARCH, "Batch file %s: %s." % (path, "; ".join(problems)))
    return spec


def parse_params(pairs):
    params = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise Fail(EXIT_SEARCH, "--param needs KEY=VALUE, got %r." % pair)
        params[key.strip()] = value
    return params


def build_searches(spec, params, app, earliest=None, latest=None, count=None, allow_write=False):
    declared = spec.get("params", {})
    unknown = sorted(set(params) - set(declared))
    if unknown:
        raise Fail(EXIT_SEARCH, "Unknown --param %s. This file takes: %s." % (
            ", ".join(unknown), ", ".join(sorted(declared)) or "no params"))
    used = set()
    for item in spec["searches"]:
        used.update(PLACEHOLDER.findall(item["spl"]))
    missing = sorted(k for k in used if not params.get(k))
    if missing:
        raise Fail(EXIT_SEARCH, "Missing params:\n" + "\n".join("  --param %s=...   %s" % (k, declared[k]) for k in missing))
    searches = []
    for item in spec["searches"]:
        spl = PLACEHOLDER.sub(lambda m: params[m.group(1)], item["spl"])
        e = earliest if earliest is not None else item.get("earliest")
        lt = latest if latest is not None else item.get("latest")
        c = count if count is not None else item.get("count", DEFAULT_COUNT)
        check_search(spl, e, allow_write, label="Search %s" % item["name"])
        searches.append(Search(item["name"], spl, e, lt, c, item.get("app") or app))
    return searches


def describe_batch(spec, out):
    out.write("%s: %s\n" % (spec.get("name", "batch"), spec.get("description", "")))
    params = spec.get("params", {})
    out.write("Params:%s\n" % ("" if params else " none"))
    for key in sorted(params):
        out.write("  --param %s=...   %s\n" % (key, params[key]))
    out.write("Searches:\n")
    for item in spec["searches"]:
        window = "%s..%s" % (item.get("earliest", "?"), item.get("latest") or "now")
        out.write("  %s (%s)\n    %s\n" % (item["name"], window, item["spl"]))


def format_macro(entry):
    content = entry.get("content", {})
    acl = entry.get("acl", {})
    lines = [
        "macro: %s" % entry.get("name"),
        "app: %s (sharing: %s)" % (acl.get("app", "?"), acl.get("sharing", "?")),
        "args: %s" % (content.get("args") or "-"),
        "iseval: %s" % content.get("iseval", "0"),
    ]
    if content.get("description"):
        lines.append("description: %s" % content["description"])
    lines.append("definition:")
    lines.extend("  " + line for line in (content.get("definition") or "").splitlines() or [""])
    return "\n".join(lines) + "\n"


def _text(node, tag):
    found = node.find(tag)
    return found.text.strip() if found is not None and found.text else None


def _simple_search(search, bases):
    query = _text(search, "query") or ""
    earliest, latest = _text(search, "earliest"), _text(search, "latest")
    base_id, depth = search.get("base"), 0
    while base_id and base_id in bases and depth < 5:
        base = bases[base_id]
        query = ((_text(base, "query") or "") + " " + query).strip()
        earliest, latest = earliest or _text(base, "earliest"), latest or _text(base, "latest")
        base_id, depth = base.get("base"), depth + 1
    if search.get("ref"):
        query = query or "| savedsearch %s" % search.get("ref")
    return query, earliest, latest


def parse_simple_xml(root, label):
    bases = {s.get("id"): s for s in root.iter("search") if s.get("id")}
    inputs = []
    for node in root.iter("input"):
        default = node.find("default")
        if node.get("type") == "time" and default is not None:
            value = "%s..%s" % (_text(default, "earliest") or "", _text(default, "latest") or "")
        else:
            value = (default.text.strip() if default is not None and default.text else None) or _text(node, "initialValue")
        inputs.append({"token": node.get("token"), "type": node.get("type"), "default": value, "label": _text(node, "label")})
    panels = []
    for panel in root.iter("panel"):
        panel_title = _text(panel, "title")
        for viz in panel:
            search = viz.find("search") if viz.tag in ("chart", "table", "single", "event", "map", "viz") else None
            if search is None:
                continue
            query, earliest, latest = _simple_search(search, bases)
            panels.append({"title": _text(viz, "title") or panel_title or "", "type": viz.tag, "spl": query,
                           "earliest": earliest, "latest": latest})
    return {"label": label, "kind": "Simple XML", "inputs": inputs, "panels": panels}


def parse_studio(definition, label):
    sources = definition.get("dataSources", {})
    defaults = (definition.get("defaults", {}).get("dataSources", {}).get("ds.search", {})
                .get("options", {}).get("queryParameters", {}))

    def resolve(ds_id, depth=0):
        source = sources.get(ds_id, {})
        options = source.get("options", {})
        query = options.get("query", "")
        params = options.get("queryParameters") or {}
        if source.get("type") == "ds.savedSearch":
            query = "| savedsearch %s" % options.get("ref", "?")
        if source.get("type") == "ds.chain" and options.get("extend") and depth < 5:
            base_query, base_params = resolve(options["extend"], depth + 1)
            query, params = (base_query + " " + query).strip(), params or base_params
        return query, params

    inputs = []
    for item in definition.get("inputs", {}).values():
        options = item.get("options", {})
        inputs.append({"token": options.get("token"), "type": item.get("type"), "default": options.get("defaultValue"),
                       "label": item.get("title")})
    panels = []
    for viz_id, viz in definition.get("visualizations", {}).items():
        primary = viz.get("dataSources", {}).get("primary")
        if not primary:
            continue
        query, params = resolve(primary)
        params = params or defaults
        panels.append({"title": viz.get("title") or viz.get("options", {}).get("title") or viz_id,
                       "type": viz.get("type"), "spl": query, "earliest": params.get("earliest"),
                       "latest": params.get("latest")})
    return {"label": label or definition.get("title", ""), "kind": "Dashboard Studio", "inputs": inputs, "panels": panels}


def parse_dashboard(text):
    text = (text or "").strip()
    if text.startswith("{"):
        return parse_studio(json.loads(text), "")
    root = ET.fromstring(text)
    label = _text(root, "label") or ""
    definition = root.find("definition")
    if root.get("version") == "2" and definition is not None and definition.text:
        return parse_studio(json.loads(definition.text), label)
    return parse_simple_xml(root, label)


def format_dashboard(name, app, data):
    lines = ["dashboard: %s (%s, app %s)" % (data["label"] or name, data["kind"], app)]
    if data["inputs"]:
        lines.append("inputs:")
        for item in data["inputs"]:
            lines.append("  $%s$  %s  default: %s%s" % (
                item["token"], item["type"], item["default"], ("  (%s)" % item["label"]) if item["label"] else ""))
    lines.append("panels: %d" % len(data["panels"]))
    for i, panel in enumerate(data["panels"], 1):
        lines.append("[%d] %s (%s)" % (i, panel["title"], panel["type"]))
        lines.append("    time: %s .. %s" % (panel["earliest"] or "-", panel["latest"] or "-"))
        tokens = sorted(set(DASH_TOKEN.findall(panel["spl"])))
        if tokens:
            lines.append("    tokens: " + ", ".join("$%s$" % t for t in tokens))
        lines.append("    spl: " + "\n         ".join(panel["spl"].splitlines() or [""]))
    return "\n".join(lines) + "\n"


def execute(cfg, args, searches):
    with_session(cfg, args, lambda session, user: run_searches(session, user, searches, args.timeout))


def cmd_query(args):
    cfg = load_config(args.app)
    inline = check_search(args.spl, args.earliest, args.allow_write)
    if inline and args.earliest is not None:
        log("The SPL has its own earliest=; it wins over --earliest.")
    search = Search("query", args.spl, args.earliest, args.latest, args.count, cfg.app)
    execute(cfg, args, [search])
    if search.error:
        report(cfg, search, named=False)
        raise Fail(EXIT_SEARCH, search.error)
    emit(args, lambda out: print_results(search.fields, search.results, args.format, wanted_fields(args), out))
    report(cfg, search, named=False)


def cmd_batch(args):
    spec = load_batch(args.file)
    if args.describe:
        describe_batch(spec, sys.stdout)
        return
    cfg = load_config(args.app or spec.get("app"))
    searches = build_searches(spec, parse_params(args.param), cfg.app, args.earliest, args.latest, args.count,
                              args.allow_write)
    execute(cfg, args, searches)
    wanted = wanted_fields(args)

    def write(out):
        for search in searches:
            if args.format != "json":
                out.write("== %s ==%s\n" % (search.name, " FAILED: " + search.error if search.error else ""))
            if not search.error:
                print_results(search.fields, search.results, args.format, wanted, out,
                              tag=search.name if args.format == "json" else None)

    emit(args, write)
    for search in searches:
        report(cfg, search, named=True)
    failed = [s.name for s in searches if s.error]
    if failed:
        raise Fail(EXIT_SEARCH, "%d of %d searches failed: %s" % (len(failed), len(searches), ", ".join(failed)))


def cmd_macro(args):
    cfg = load_config(args.app)
    name = quote(args.name.strip("`"), safe="")

    def work(session, _user):
        for template in ("/servicesNS/-/%s/configs/conf-macros/%s", "/servicesNS/-/%s/admin/macros/%s"):
            resp = session.api("GET", template % (quote(cfg.app, safe=""), name))
            if resp.status == 401:
                raise SessionExpired()
            if resp.status == 200:
                return format_macro(resp.json()["entry"][0])
        return None

    text = with_session(cfg, args, work)
    if text is None:
        raise Fail(EXIT_SEARCH, "Macro %s not found in app %s. Macros with arguments are named like name(2); "
                                "also try --app search." % (args.name, cfg.app))
    sys.stdout.write(text)


def cmd_dashboard(args):
    cfg = load_config(args.app)

    def work(session, _user):
        path = "/servicesNS/-/%s/data/ui/views/%s" % (quote(cfg.app, safe=""), quote(args.name, safe=""))
        resp = session.api("GET", path)
        if resp.status == 401:
            raise SessionExpired()
        if resp.status != 200:
            return None
        return resp.json()["entry"][0]["content"].get("eai:data", "")

    text = with_session(cfg, args, work)
    if text is None:
        raise Fail(EXIT_SEARCH, "Dashboard %s not found in app %s (pass --app with the dashboard's app; knowledge lists it)."
                   % (args.name, cfg.app))
    try:
        data = parse_dashboard(text)
    except (ET.ParseError, ValueError) as exc:
        raise Fail(EXIT_ERROR, "Cannot parse dashboard %s: %s" % (args.name, exc))
    if args.format == "json":
        sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=True) + "\n")
    else:
        sys.stdout.write(format_dashboard(args.name, cfg.app, data))


def word_matches(word, text):
    return word in text or (len(word) > 3 and word.endswith("s") and word[:-1] in text)


def collect_knowledge(session, kinds, words, limit, prefer_app=None):
    """Return {kind: (matches, total_matching) or None} for enabled objects containing every word.

    Filters on the client: the REST "search" filter matched "error" but not "errors" in a live test.
    """
    words = [w.lower() for w in words]
    found = {}
    for kind in kinds:
        _label, _plural, path, field, only = KNOWLEDGE_TYPES[kind]
        params = {"count": "0"}
        if only:
            params["f"] = only
        resp = session.api("GET", path, params=params, timeout=120000)
        if resp.status == 401:
            raise SessionExpired()
        if resp.status != 200:
            found[kind] = None
            continue
        items = []
        for entry in json_body(resp).get("entry", []):
            content = entry.get("content", {})
            if str(content.get("disabled", "0")).lower() in ("1", "true"):
                continue
            item = {
                "name": entry.get("name") or "",
                "app": entry.get("acl", {}).get("app"),
                "title": content.get("label") or content.get("description") or "",
                "spl": (content.get(field) or "") if field else "",
                "earliest": content.get("dispatch.earliest_time") if kind == "saved" else None,
                "args": content.get("args") if kind == "macro" else None,
            }
            label_text = " ".join((item["name"], item["title"])).lower()
            text = label_text + " " + item["spl"].lower()
            if all(word_matches(w, text) for w in words):
                item["rank"] = (not all(word_matches(w, label_text) for w in words), item["app"] != prefer_app)
                items.append(item)
        items.sort(key=lambda i: (i["rank"], i["name"].lower(), i["app"] or ""))
        found[kind] = (items[:limit], len(items))
    return found


def format_knowledge(found):
    lines = []
    for kind, result in found.items():
        plural = KNOWLEDGE_TYPES[kind][1]
        if result is None:
            lines.append("== %s: not readable with this account ==" % plural)
            continue
        items, total = result
        shown = "" if len(items) == total else " (showing %d; raise --count or add words)" % len(items)
        lines.append("== %s: %d%s ==" % (plural, total, shown))
        for item in items:
            lines.append("- %s (app %s)%s" % (item["name"], item["app"], ("  " + item["title"]) if item["title"] else ""))
            if item["args"]:
                lines.append("    args: %s" % item["args"])
            if item["earliest"]:
                lines.append("    earliest: %s" % item["earliest"])
            if item["spl"]:
                lines.append("    spl: " + "\n         ".join(item["spl"].strip().splitlines()))
            if kind == "dashboard":
                lines.append("    panels: dashboard %s --app %s" % (item["name"], item["app"]))
    return "\n".join(lines) + "\n"


def cmd_knowledge(args):
    cfg = load_config(args.app)
    kinds = args.type or list(KNOWLEDGE_TYPES)
    found = with_session(cfg, args, lambda session, _user: collect_knowledge(session, kinds, args.words, args.count, cfg.app))
    sys.stdout.write(format_knowledge(found))


def notes_path():
    return os.path.expanduser(os.environ.get("SPLUNK_QUERY_NOTES")
                              or os.path.join(os.path.dirname(config_path()), "notes.md"))


def read_notes():
    """Return (header, [(title, text), ...]) from the notes file."""
    path = notes_path()
    if not os.path.exists(path):
        return NOTES_HEADER, []
    with open(path, encoding="utf-8") as fh:
        parts = re.split(r"^### ", fh.read(), flags=re.M)
    entries = [(part.splitlines()[0].strip(), "### " + part.rstrip() + "\n") for part in parts[1:]]
    return parts[0], entries


def write_notes(header, entries):
    path = notes_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(header.rstrip() + "\n")
        for _title, text in entries:
            fh.write("\n" + text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def format_note(title, spl, use_when, params, read_it, status, today):
    return "### %s\n\nUse when: %s\nParams: %s\n\n```spl\n%s\n```\n\nRead it: %s\nStatus: %s\nAdded: %s\n" % (
        title, use_when or "-", params or "none", spl.strip(), read_it or "-", status or "verified %s" % today, today)


def cmd_notes(args):
    if args.action == "path":
        print(notes_path())
        return
    header, entries = read_notes()
    if args.action == "show":
        words = [w.lower() for w in args.words]
        hits = [text for _title, text in entries if all(w in text.lower() for w in words)]
        if not entries:
            print("No notes yet (%s)." % notes_path())
        elif not hits:
            print("No notes match %r (%d notes in %s)." % (" ".join(args.words), len(entries), notes_path()))
        else:
            sys.stdout.write("\n".join(hits))
        return
    if args.action == "remove":
        title = " ".join(args.words).strip()
        keep = [e for e in entries if e[0].lower() != title.lower()]
        if len(keep) == len(entries):
            raise Fail(EXIT_SEARCH, "No note titled %r." % title)
        write_notes(header, keep)
        print("OK: removed %r" % title)
        return
    title = (args.title or "").strip()
    if not title or "\n" in title:
        raise Fail(EXIT_SEARCH, "notes add needs --title on one line.")
    if not args.spl or not has_inline_earliest(args.spl):
        raise Fail(EXIT_SEARCH, "notes add needs --spl with the time range inline (earliest=...), so it reruns as saved.")
    exists = any(t.lower() == title.lower() for t, _text in entries)
    if exists and not args.replace:
        raise Fail(EXIT_SEARCH, "A note titled %r exists; pass --replace to overwrite it." % title)
    entries = [e for e in entries if e[0].lower() != title.lower()]
    today = time.strftime("%Y-%m-%d")
    entries.append((title, format_note(title, args.spl, args.use_when, args.params, args.read_it, args.status, today)))
    write_notes(header, entries)
    print("OK: %s %r in %s" % ("replaced" if exists else "saved", title, notes_path()))


def cmd_check(args):
    cfg = load_config(args.app)
    check_instance(cfg)
    sync_playwright = load_playwright()
    with sync_playwright() as pw:
        session = Session(pw, cfg, headless=True)
        try:
            user = session.whoami()
            if user:
                record_instance(cfg)
                print("OK: logged in to %s as %s" % (cfg.url, user))
                return
            user = session.silent_sso(cfg.app) and session.whoami()
            if user:
                session.save()
                record_instance(cfg, session.sso_host)
                print("OK: session renewed through SSO as %s" % user)
                return
        finally:
            session.close()
    raise Fail(EXIT_LOGIN, "Session expired and an interactive login is needed. Run: splunk_query.py login")


def cmd_login(args):
    cfg = load_config(args.app)
    sync_playwright = load_playwright()
    with sync_playwright() as pw:
        session, user = open_session(pw, cfg, cfg.app, True, args.login_timeout, force_login=True)
        session.close()
    print("OK: logged in to %s as %s" % (cfg.url, user))


def cmd_logout(args):
    removed = []
    for path in (state_file(), instance_file()) if args.all else (state_file(),):
        if os.path.exists(path):
            os.remove(path)
            removed.append(path)
    if args.all:
        for path in (profile_dir(), profile_dir() + "-chromium"):
            if os.path.isdir(path):
                shutil.rmtree(path)
                removed.append(path)
    print("OK: removed %s" % (", ".join(removed) if removed else "nothing (no saved session)"))


def cmd_configure(args):
    data = read_config_file()
    changed = False
    if args.url:
        url, url_locale, url_app = parse_splunk_url(args.url)
        data["url"] = url
        if url_locale and not args.locale:
            data["locale"] = url_locale
        if url_app and not args.app:
            data["app"] = url_app
        changed = True
    if args.app:
        data["app"] = check_app(args.app)
        changed = True
    if args.locale:
        data["locale"] = check_locale(args.locale)
        changed = True
    if changed:
        if not data.get("url"):
            raise Fail(EXIT_CONFIG, "Pass --url as well: there is no saved URL yet.")
        path = config_path()
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        print("OK: saved %s" % path)
    cfg = load_config(required=False)
    if cfg is None:
        raise Fail(EXIT_CONFIG, NOT_CONFIGURED)
    print("url: %s\napp: %s\nlocale: %s" % (cfg.url, cfg.app, cfg.locale))
    for name in ("SPLUNK_QUERY_URL", "SPLUNK_QUERY_APP", "SPLUNK_QUERY_LOCALE"):
        if os.environ.get(name):
            print("note: %s is set and overrides the file" % name)


def tcp_check(host, port=443, timeout=5):
    """Return None when host:port accepts a TCP connection, else a short reason."""
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return None
    except socket.gaierror:
        return "DNS lookup failed"
    except OSError as exc:
        return str(exc) or exc.__class__.__name__


def cmd_doctor(args, network=tcp_check):
    codes = []

    def line(level, message, code=None):
        print("[%s] %s" % (level, message))
        if code:
            codes.append(code)

    if sys.version_info >= (3, 9):
        line("OK", "Python %d.%d.%d" % sys.version_info[:3])
    else:
        line("ERROR", "Python 3.9+ needed, found %d.%d" % sys.version_info[:2], EXIT_ERROR)
    if importlib.util.find_spec("playwright"):
        try:
            from importlib.metadata import version
            line("OK", "playwright %s" % version("playwright"))
        except Exception:
            line("OK", "playwright installed")
    else:
        line("ERROR", "playwright missing: python3 -m pip install --user playwright", EXIT_ERROR)
    chrome = "/Applications/Google Chrome.app"
    if os.path.isdir(chrome) or any(shutil.which(n) for n in ("google-chrome", "google-chrome-stable", "chrome")):
        line("OK", "Google Chrome found")
    else:
        line("WARN", "Google Chrome not found; falls back to Playwright Chromium (python3 -m playwright install chromium), "
                     "which SSO device checks may reject")
    try:
        cfg = load_config(args.app, required=False)
    except Fail as exc:
        cfg = None
        line("ERROR", str(exc), EXIT_CONFIG)
    else:
        if cfg:
            line("OK", "config: %s app=%s locale=%s" % (cfg.url, cfg.app, cfg.locale))
        else:
            line("ERROR", NOT_CONFIGURED, EXIT_CONFIG)
    try:
        os.makedirs(state_dir(), mode=0o700, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=state_dir()):
            pass
        line("OK", "state folder writable: %s" % state_dir())
    except OSError as exc:
        line("ERROR", "state folder %s not writable: %s" % (state_dir(), exc), EXIT_ERROR)
    if os.path.exists(state_file()):
        line("OK", "saved session file present")
    else:
        line("WARN", "no saved session yet; the first query opens a login window")
    if cfg:
        try:
            check_instance(cfg)
        except Fail as exc:
            line("ERROR", str(exc), EXIT_LOGIN)
        hosts = [cfg.host]
        sso_host = read_instance().get("sso_host")
        if sso_host:
            hosts.append(sso_host)
        else:
            line("WARN", "sign-in host unknown until the first login; not checked")
        for host in hosts:
            problem = network(host)
            if problem:
                line("ERROR", "cannot reach %s:443 (%s). Check the VPN or network." % (host, problem), EXIT_VPN)
            else:
                line("OK", "reached %s:443" % host)
    if codes:
        raise Fail(codes[0], "doctor found problems (see above).")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--app", help="Splunk app namespace for macros, saved searches and dashboards")
    common.add_argument("--login-timeout", type=int, default=300, help="seconds to wait for an interactive login")
    common.add_argument("--no-interactive", action="store_true", help="exit 4 instead of opening a login window")
    sub = parser.add_subparsers(dest="command", required=True)

    def output_options(p, count_default):
        p.add_argument("--earliest", help="earliest_time when the SPL has no earliest= (required then)")
        p.add_argument("--latest", help="latest_time (default now)")
        p.add_argument("--count", type=int, default=count_default, help="max rows per search, 0 = all (default 100)")
        p.add_argument("--timeout", type=int, default=300, help="seconds before a job is cancelled")
        p.add_argument("--format", choices=("json", "table", "csv"), default="json", help="json = one object per line")
        p.add_argument("--fields", help="comma-separated columns to keep")
        p.add_argument("--out", help="write results to this file instead of stdout")
        p.add_argument("--allow-write", action="store_true", help="allow collect/outputlookup/sendemail/delete")

    q = sub.add_parser("query", parents=[common], help="run one search")
    q.add_argument("spl", help="SPL; 'search' is prepended unless it starts with '|' or 'search'")
    output_options(q, DEFAULT_COUNT)
    q.set_defaults(handler=cmd_query)

    b = sub.add_parser("batch", parents=[common], help="run a JSON list of named searches")
    b.add_argument("file", help="batch JSON file (see reference/checks/)")
    b.add_argument("--param", action="append", default=[], metavar="KEY=VALUE", help="fill <KEY> in the searches")
    b.add_argument("--describe", action="store_true", help="print params and searches without running")
    output_options(b, None)
    b.set_defaults(handler=cmd_batch)

    m = sub.add_parser("macro", parents=[common], help="print a search macro's definition")
    m.add_argument("name")
    m.set_defaults(handler=cmd_macro)

    d = sub.add_parser("dashboard", parents=[common], help="print the searches behind a dashboard")
    d.add_argument("name")
    d.add_argument("--format", choices=("text", "json"), default="text")
    d.set_defaults(handler=cmd_dashboard)

    k = sub.add_parser("knowledge", parents=[common], help="find saved searches, macros, event types and dashboards")
    k.add_argument("words", nargs="+", help="words to match, e.g. error login")
    k.add_argument("--type", action="append", choices=sorted(KNOWLEDGE_TYPES), help="limit to these types")
    k.add_argument("--count", type=int, default=10, help="max objects per type (default 10)")
    k.set_defaults(handler=cmd_knowledge)

    n = sub.add_parser("notes", help="local notes of searches that proved right")
    n.add_argument("action", choices=("show", "add", "remove", "path"))
    n.add_argument("words", nargs="*", help="show: words every note must contain; remove: the note title")
    n.add_argument("--title", help="add: one-line title")
    n.add_argument("--spl", help="add: the search, with earliest= inline")
    n.add_argument("--use-when", help="add: the question it answers")
    n.add_argument("--params", help="add: <PLACEHOLDER> meanings, or none")
    n.add_argument("--read-it", help="add: how to read the result")
    n.add_argument("--status", help="add: default 'verified <today>'")
    n.add_argument("--replace", action="store_true", help="add: overwrite a note with the same title")
    n.set_defaults(handler=cmd_notes)

    c = sub.add_parser("configure", help="save the Splunk URL, default app and locale")
    c.add_argument("--url", help="https://<instance>[/base-path]; a URL copied from Splunk Web also works")
    c.add_argument("--app", help="default app (default search)")
    c.add_argument("--locale", help="UI locale in URLs (default en-US)")
    c.set_defaults(handler=cmd_configure)

    doc = sub.add_parser("doctor", help="check the setup and network; never opens a window")
    doc.add_argument("--app")
    doc.set_defaults(handler=cmd_doctor)

    for name, handler, text in (("check", cmd_check, "check the saved session"),
                                ("login", cmd_login, "log in now")):
        p = sub.add_parser(name, parents=[common], help=text)
        p.set_defaults(handler=handler)
    lo = sub.add_parser("logout", help="delete the saved session")
    lo.add_argument("--all", action="store_true", help="also delete the browser profile and instance binding")
    lo.set_defaults(handler=cmd_logout)
    return parser


def main(argv=None):
    # Splunk data and dashboard titles can hold any Unicode; a cp1252 console would raise on print.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    args = build_parser().parse_args(argv)
    try:
        args.handler(args)
    except Fail as exc:
        log("ERROR: %s" % exc)
        return exc.code
    except KeyboardInterrupt:
        log("Interrupted.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
