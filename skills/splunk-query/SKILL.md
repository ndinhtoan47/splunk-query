---
name: splunk-query
description: >-
  Run and interpret Splunk searches from the terminal through a saved browser session (SSO or
  Splunk login) and a headless Chrome. Use whenever the user wants to query or check Splunk,
  search logs or events, find the top errors, look up what a user id or session id did,
  count users or failures after a release, check a deployment, write or fix SPL, explain a
  Splunk macro, saved search or dashboard, or says "query splunk", "check splunk", "search
  splunk", "chay query splunk", "tim tren splunk". Learns the instance as it goes: reuses the
  team's own saved searches, macros and dashboards, and keeps searches that proved right in
  a private local notes file.
---

# Splunk query

Answer questions with Splunk data. First reuse what is already known (your local notes, then
the team's saved searches, macros and dashboards on the instance), explore only when nothing
fits, and save what worked.

All paths below are relative to the folder that holds this SKILL.md. Set
`S=<that folder>/scripts/splunk_query.py` and run `python3 $S ...`. Run every command with
the Bash timeout set to `600000`: a login waits up to 300 s for the user.

## Commands

```bash
python3 $S notes show error login             # notes containing every word
python3 $S knowledge error login              # saved searches, macros, event types, dashboards
python3 $S macro <name>                       # a macro's definition
python3 $S dashboard <name> --app <app>       # the search behind each panel
python3 $S query '<SPL>' --earliest=-4h       # one search; JSON lines on stdout
python3 $S query '<SPL>' --format table --count 20
python3 $S batch <file.json> --param K=V ...  # several named searches in one session
python3 $S notes add --title '...' --spl '<SPL with earliest=>' --use-when '...' --read-it '...'
python3 $S notes remove <title>
python3 $S doctor                             # setup and network check, no window
python3 $S check | login | logout [--all]
python3 $S configure --url '<URL the user gives>'
```

Options for `query` and `batch`:
- `--earliest`, `--latest`: time range.
- `--count N`: rows per search, default 100; 0 = all.
- `--timeout S`: a job is cancelled after it; default 300.
- `--format json|table|csv`, `--fields a,b`, `--out FILE`.
- `--app`: namespace for macros and dashboards.
- `--no-interactive`, `--allow-write`.

`batch` takes a JSON file of named searches (see "Batch files" in
`reference/spl-patterns.md`). It runs up to 4 jobs at once in one browser session.

First use: when a command exits 5 (not configured), ask the user for their Splunk URL. Any
URL copied from their browser's Splunk tab works; `configure` takes the instance, app and
locale from it. Don't guess a URL or take one from elsewhere.

Codex: the runner launches Chrome, needs network access to Splunk and the sign-in page, and
writes to `~/.cache/splunk-query/` and `~/.config/splunk-query/`. If the sandbox blocks any of
that, rerun the same command with escalated permissions instead of working around it.

## Workflow

1. **Notes.** Run `notes show <key words>`. A matching note is a search that already
   answered this kind of question on this instance; reuse it and fill its params.
2. **Team knowledge.** Run `knowledge <key words>` (try stems: `error`, `login`,
   `purchase`). Read a macro with `macro`, a dashboard's searches with `dashboard`.
   Prefer an existing saved search, macro or dashboard panel over new SPL: it encodes the
   team's own definition of "error", "real user" or "normal". Say which one you used.
3. **Explore** when nothing fits: follow `reference/discovery.md` (indexes, sourcetypes,
   fields, a sample, then the aggregate). Keep every discovery search cheap and bounded.
4. **Params and time.** Ask the user for ids you can't find; never guess one. Take the time
   range from the question. If nothing in the conversation sets one, ask.
5. **Run narrow first** (short range, `stats`, `head`), then widen. See
   `reference/spl-patterns.md`.
6. **Check before "nothing found".** Zero rows can mean a wrong index, field name or time
   range. Confirm the field exists in that window before telling the user there are none.
7. **Report.** Findings first: numbers with their denominator, and a comparison (previous
   window or version). Then the queries (see "Showing the queries"), each with where it came
   from: a note, a saved search or macro, or explored now.
8. **Save what worked.** When a search answered the question and its fields were confirmed
   (it ran, the rows made sense, or the user agreed), save it:
   `notes add --title ... --spl ... --use-when ... --params ... --read-it ...`. Put the time
   range inline, and write ids as `<USER_ID>`-style placeholders, never real values. Tell
   the user you saved it. When a note turns out wrong, fix it with `--replace` or remove it.

## Notes file

- Lives at `~/.config/splunk-query/notes.md` (`notes path` prints it; `SPLUNK_QUERY_NOTES`
  overrides it). Each machine builds its own.
- It holds this instance's index, field and host names. Keep it private: never copy it
  into a repository, issue, chat or this skill's folder.
- Each note has a title, `Use when`, `Params`, an `spl` block, `Read it`, `Status`, `Added`.

## Writing searches

- Every search needs a time range: inline `earliest=` in the SPL, or `--earliest`.
  `--earliest=0` means all time; use it only when the user asks. Inline `earliest=` wins.
- Join relative times to their flag with `=` (`--earliest=-7d`). With a space, argparse
  reads `-7d` as an option.
- `search` is prepended unless the SPL starts with `|` or `search`.
- Prefer transforming searches and keep `--count` small. Raw-event searches return every
  field; add `| table ...` or `--fields`.
- `TRUNCATED: showing N of M rows` on stderr means more rows exist. Say so, or raise
  `--count` or aggregate more.
- The runner refuses `collect`, `outputlookup`, `outputcsv`, `sendemail`, `delete` and
  similar unless `--allow-write` is passed. Pass it only when the user explicitly asks for
  that write.
- One runner at a time: a second run while one is open fails with "profile is in use".

## Showing the queries

Every reply that reports Splunk results lists each search run in that turn, in run order:
- one code block per search, in search-bar form: the time range inline (`earliest=-7d`,
  plus `latest=` if used), without the `search` prefix the runner adds;
- the `link:` the runner printed for it;
- searches that returned 0 rows or failed, marked as such.

Also:
- Say when results were reshaped after the search (sorted, joined, tabled locally).
- Never present a search that was not run as one that was; label suggestions "not run".

## Login and errors

Progress, links and errors go to stderr prefixed `[splunk]`; results go to stdout.

| Exit | Meaning | What to do |
|---|---|---|
| 0 | Results printed | Summarize them and show the queries |
| 2 | Search rejected, failed, timed out, or no time range | Show the Splunk message, then fix the SPL, narrow the range, or ask for the time range |
| 3 | Splunk or the sign-in page unreachable | Tell the user, in their language, to check the VPN or network and say when to retry. Don't retry before they confirm |
| 4 | Interactive login needed or failed, or the session belongs to another instance | Tell the user a Chrome window will open, run `login`, wait for it |
| 5 | Not configured | Ask the user for their Splunk URL and run `configure` |
| 1 | Unexpected error | Show the message; run `doctor` |

Re-login order: saved cookies, then silent SSO in the background, then a visible Chrome
window where the user logs in (and approves MFA if asked). When the runner logs "Opening a
Chrome window", tell the user to finish the login there.

## Safety

- Never print, cat or copy `~/.cache/splunk-query/storage_state.json` or the
  `chrome-profile` folder: they hold live session cookies.
- Splunk data can hold user ids, purchases and device data. Keep results local, don't paste
  them into external services, and summarize rather than dump raw events.
- Don't post results to chat tools or ticket systems yourself. Draft the text if asked.

## Requirements

Google Chrome, Python 3.9+ with `playwright` (`python3 -m pip install --user playwright`;
no browser download needed), and network access to the Splunk instance. `doctor` checks
all of it. Settings: `~/.config/splunk-query/config.json` (or `SPLUNK_QUERY_URL`,
`SPLUNK_QUERY_APP`, `SPLUNK_QUERY_LOCALE`). Session: `~/.cache/splunk-query/` (or
`SPLUNK_QUERY_HOME`).
