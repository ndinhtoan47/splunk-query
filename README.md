# splunk-query

An agent skill for Claude Code and Codex that runs Splunk searches from the terminal and
gets better at your Splunk instance over time.

- **Uses your browser login.** It runs searches through a dedicated profile of Chrome,
  Edge or Playwright's Chromium, so SSO and MFA happen once in a normal window; later runs
  are headless. The session renews silently while your SSO session lasts.
- **Reuses what your team already built.** Before writing new SPL it looks for matching
  saved searches, macros, event types and dashboards on the instance, and reads the
  searches behind dashboard panels.
- **Explores carefully.** When nothing fits, it discovers indexes, sourcetypes and fields
  with cheap, time-bounded searches before running the real one.
- **Remembers what worked, privately.** Searches that answered a question go into a notes
  file on your machine (`~/.config/splunk-query/notes.md`), so next time it starts from
  them. Nothing about your instance is stored in the skill itself.
- **Read-only by default.** Commands that write or send data (`collect`, `outputlookup`,
  `sendemail`, `delete`, ...) are refused unless explicitly allowed.

## Requirements

- Python 3.9+ (on macOS, `python3` comes with the Xcode Command Line Tools).
- Node.js 22.20+ for the `skills` installer.
- Network access to your Splunk instance (and VPN if your company requires it).

Nothing else to install by hand. On the first run the agent runs `setup`, which:
- installs Playwright into a private Python environment in `~/.cache/splunk-query/venv`,
  so your system Python stays untouched;
- uses Google Chrome or Microsoft Edge if one is installed, and otherwise downloads
  Playwright's Chromium (about 150 MB).

If your company's SSO only accepts a managed browser, install Chrome or Edge and run
`setup --browser chrome` (or `msedge`). On Linux, Chromium may also need
`python3 -m playwright install-deps chromium` (needs sudo).

## Install

```bash
DISABLE_TELEMETRY=1 npx skills add ndinhtoan47/splunk-query -g -a claude-code -a codex -y
```

`DISABLE_TELEMETRY=1` turns off the installer's anonymous telemetry.

Then open Claude Code or Codex and ask:

```text
find me top 3 errors last 3 days
```

That's all. The first time, the agent asks for your Splunk URL (paste any URL from your
browser's Splunk tab), installs what it needs, and opens a browser window once for your
login. After that, searches run in the background.

## First run in detail

You don't need this section: the agent does all of it for you. It's here if you want to
know what happens, or prefer to set things up before your first question.

On the first question the agent:
1. asks for your Splunk URL and saves it;
2. runs `setup` if Playwright or a usable browser is missing;
3. opens a browser window for your login (SSO and MFA). Later runs reuse that session.

To do these steps yourself:

```bash
S=~/.claude/skills/splunk-query/scripts/splunk_query.py
python3 $S setup --url '<a URL from your Splunk tab>'
python3 $S doctor
python3 $S login
```

Or export `SPLUNK_QUERY_URL` (and optionally `SPLUNK_QUERY_APP`) in your shell profile.

## Examples

- "Find me the top 7 errors in the last 4 hours."
- "What errors did user 12345 hit yesterday?"
- "Is the login service healthy since the deploy at 10:00?"
- "What does the `my_errors` macro do?"
- "Show the searches behind the `service_health` dashboard."

## Where things live

| What | Where |
|---|---|
| Instance settings | `~/.config/splunk-query/config.json` (or `SPLUNK_QUERY_URL`, `SPLUNK_QUERY_APP`, `SPLUNK_QUERY_LOCALE`) |
| Notes of searches that worked | `~/.config/splunk-query/notes.md` (or `SPLUNK_QUERY_NOTES`) |
| Session cookies, browser profiles, private Python environment | `~/.cache/splunk-query/` (or `SPLUNK_QUERY_HOME`) |
| Browser choice | `auto` by default; `setup --browser` with chrome, msedge or chromium; or `SPLUNK_QUERY_BROWSER` |

Keep the notes and session files private: the notes name your indexes, fields and hosts,
and the session folder holds live cookies.

## Update

```bash
DISABLE_TELEMETRY=1 npx skills update splunk-query -g
```

## Uninstall

```bash
DISABLE_TELEMETRY=1 npx skills remove splunk-query -g
```

Keep the `-g`: the skill is installed globally, and without it the installer looks only in
the current folder and reports `Found 0 unique installed skill(s)`.

This removes the skill but leaves your settings, notes and saved login on disk. To delete
those too:

```bash
rm -rf ~/.config/splunk-query ~/.cache/splunk-query
```

`~/.config/splunk-query/` holds your notes of searches that worked; keep it if you might
reinstall.

## Development

```bash
python3 -m unittest discover -s tests
```

The tests need no network, browser or Playwright.
