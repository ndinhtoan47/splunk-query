# SPL patterns

Patterns for building searches. They are standard SPL; field names here (`action`,
`user_id`, `session_id`, `message`) are examples, so use the instance's real names from
notes, knowledge objects or `discovery.md`.

## Shape of a search

```spl
earliest=-4h index=<INDEX> action=error
| stats count as errors dc(user_id) as users by message
| sort - errors
```

1. Filter first: time, index, then the most selective terms.
2. Then transform (`stats`, `timechart`, `table`) so the result is small.
3. Then shape (`eval`, `sort`, `where`, `head`).

## Keep it fast

- Always bound the time and name an index.
- For multi-day scans, put literal terms before field filters. A field-only filter can scan
  every event in the window; a literal term uses the index:

  ```spl
  earliest=-14d index=<INDEX> ("login_failed" OR "timeout")
  | search action IN (login_failed, timeout)
  | stats count by action
  ```

- Aggregate before returning. Raw-event searches return every field; add `| table ...`.
- Avoid `transaction` and `join` over wide ranges; `stats ... by <id>` does the same job
  faster.

## Count and rate

- `count` (`c`) counts events; `dc(user_id)` counts distinct users. Say which you report.
- Conditional counts give a rate in one pass:

  ```spl
  | eval failed=if(action="request_failed", user_id, null)
  | eval total=if(action="request", user_id, null)
  | stats count(failed) as failed count(total) as total
  | eval rate=failed/total
  ```

- A count alone means little. Give its denominator and a comparison window.

## Trends and comparisons

- `| timechart span=1h dc(user_id) by action limit=10`: one series per value.
- `| bin _time span=30m` before `stats ... by _time` for custom buckets.
- `| stats sparkline count by message`: a small trend per row.
- Before vs after: run the same search over two windows (`earliest=-2h` and
  `earliest=-26h latest=-24h`), or `| timechart span=1h count | timewrap 1d`.

## Group errors by shape

```spl
earliest=-20m index=<INDEX> (error OR exception)
| stats sparkline count last(_raw) as example last(host) as host by punct
| sort 0 - count
```

`punct` is the punctuation pattern of a line, so messages that differ only in ids group
together.

## Discover fields and values

- `| head 1` on a narrow search shows one full event.
- `| fieldsummary maxvals=5` lists fields with counts and top values.
- `| table _time *err* *msg*` keeps every field whose name matches.
- `| stats count by action` lists the event names of a source.

## Multi-value and JSON fields

- `| spath input=<json_field>` extracts fields from a JSON string.
- `| foreach prefix_* [eval ALL=mvappend(ALL, '<<FIELD>>')]` collects similar fields into one.
- `| rex field=message "(?<file>[\w/]+\.\w+):(?<line>\d+)"` pulls a file and line number out of a message.

## Batch files

`batch` runs a JSON file of named searches in one session, up to 4 at a time:

```json
{
  "name": "release-check",
  "description": "Error picture of one service after a release.",
  "params": {"INDEX": "index to search", "SERVICE": "part of the host name"},
  "searches": [
    {"name": "errors", "earliest": "-2h",
     "spl": "index=<INDEX> host=*<SERVICE>* (error OR exception) | stats count by punct | sort - count"},
    {"name": "hosts", "earliest": "-10m",
     "spl": "index=<INDEX> host=*<SERVICE>* | stats count by host"}
  ]
}
```

Run it with `batch file.json --param INDEX=main --param SERVICE=api`. Put each search's time in `earliest`
rather than inline, so `--earliest` can override all of them. Keep such files next to the
notes (`~/.config/splunk-query/`), not in this skill's folder.

## Common mistakes

- In the search command, `OR` binds tighter than the implicit `AND`. Use parentheses:
  `(action=a message=*x*) OR (action=b)`.
- `sort` without a number keeps 10000 rows; use `sort 0 - count`.
- `top` and `rare` return 10 rows by default.
- Field names are case-sensitive; values are not.
- Quote values with spaces or special characters: `name="Some Value*"`.
