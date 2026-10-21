# splunk-query

An agent skill for Claude Code and Codex that runs Splunk searches from the terminal and
gets better at your Splunk instance over time.

- **Uses your browser login.** It runs searches through a dedicated Chrome profile, so SSO
  and MFA happen once in a normal window; later runs are headless. The session renews
  silently while your SSO session lasts.
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

- Google Chrome.
- Python 3.9+ with Playwright: `python3 -m pip install --user playwright`. No browser
  download is needed; the runner uses the installed Chrome.
- Node.js 22.20+ for the `skills` installer.
- Network access to your Splunk instance (and VPN if your company requires it).

## Install

```bash
DISABLE_TELEMETRY=1 npx skills add ndinhtoan47/splunk-query -g -a claude-code -a codex -y
```

`DISABLE_TELEMETRY=1` turns off the installer's anonymous telemetry.

## First use

Ask your agent a Splunk question. On the first run it asks for your Splunk URL: paste any
URL from your browser's Splunk tab. Then a Chrome window opens once for your login.

To set it up yourself instead:

```bash
S=~/.claude/skills/splunk-query/scripts/splunk_query.py
python3 $S configure --url '<a URL from your Splunk tab>'
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
| Session cookies and Chrome profile | `~/.cache/splunk-query/` (or `SPLUNK_QUERY_HOME`) |

Keep the notes and session files private: the notes name your indexes, fields and hosts,
and the session folder holds live cookies.

## Update and remove

```bash
DISABLE_TELEMETRY=1 npx skills check
DISABLE_TELEMETRY=1 npx skills update splunk-query
DISABLE_TELEMETRY=1 npx skills remove splunk-query -g
```

Removing the skill leaves your settings, notes and session in place; delete
`~/.config/splunk-query/` and `~/.cache/splunk-query/` for a full cleanup.

## Development

```bash
python3 -m unittest discover -s tests
```

The tests need no network, browser or Playwright.
