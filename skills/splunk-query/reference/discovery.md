# Exploring an unknown Splunk instance

Use this when no note and no saved search, macro or dashboard answers the question. Each
step is one cheap, bounded search. Write down what you learn (index, field names) and save
the final search to the notes when it works.

Placeholders: `<INDEX>`, `<SOURCETYPE>`, `<FIELD>`, `<ID>`, `<WORD>`.

## 1. Which indexes have data

```spl
| tstats count where index=* by index
| sort - count
```

Run with `--earliest=-24h`. `tstats` reads index metadata, so it is fast. If it is not
allowed for the account, use `| eventcount summarize=false index=* | dedup index | table index`.

## 2. What is in an index

```spl
| tstats count where index=<INDEX> by sourcetype
| sort - count
```

Then `by host` or `by source` the same way. Host and source names often say which service
wrote the logs.

## 3. Which fields an event type has

```spl
earliest=-1h index=<INDEX> sourcetype=<SOURCETYPE>
| head 1000
| fieldsummary maxvals=5
| table field count distinct_count values
```

Look for:
- an action or event-name field (values like `login`, `purchase`, `crash`);
- user and session id fields;
- message and error fields (`*err*`, `*msg*`, `*exception*`);
- version and platform fields.

## 4. Look at one event

```spl
earliest=-1h index=<INDEX> sourcetype=<SOURCETYPE> <WORD>
| head 1
```

One raw event shows the real field names and values. Use it to confirm a field before you
filter or group on it.

## Worked example: "find errors for user id X"

1. Find where the id appears. A literal value needs no field name:

   ```spl
   earliest=-7d index=* "<ID>"
   | stats count by index sourcetype
   ```

2. Read one of those events (`| head 1`) to learn which field holds the id and which
   fields carry errors.
3. Build the real search, for example:

   ```spl
   earliest=-7d index=<INDEX> <ID_FIELD>=<ID> (<ERROR_FIELD>=* OR error OR exception OR fail*)
   | stats count min(_time) as first max(_time) as last by <ACTION_FIELD> <ERROR_FIELD>
   | sort - count
   ```

4. If the user wants the story, run `| table _time <ACTION_FIELD> <ERROR_FIELD> | sort _time`
   over a short window.

## Worked example: "top 7 errors"

1. Run `knowledge error` first. A team macro or saved search for errors (with the known
   noise removed) beats any generic definition. If one exists, build on it.
2. Otherwise find where errors live:

   ```spl
   earliest=-1h index=* (error OR exception OR fatal OR severe OR fail*)
   | stats count by index sourcetype
   | sort - count
   ```

3. Group the errors in the busiest index by message shape, so lines that differ only by ids
   count together:

   ```spl
   earliest=-4h index=<INDEX> (error OR exception OR fatal OR severe OR fail*)
   | stats count dc(<USER_FIELD>) as users last(_raw) as example by punct
   | sort - count
   | head 7
   ```

   If the events have an error-name or message field, group by it instead of `punct`.
4. Report each with its count, distinct users and an example line, and say which definition
   of "error" you used.

## Before saying "nothing found"

- Run the same search without the narrowest filter: does the index have events in the
  window at all?
- Check the field name's case: field names are case-sensitive.
- Widen the window once; data can arrive late.

## Cost rules

- Always bound time. Prefer `tstats`, `metadata` and `head` for discovery.
- `index=*` only with a literal term or `tstats`, and only over short windows.
- For long windows, put literal terms before field filters:
  `index=<INDEX> "<WORD>" | search <FIELD>=<WORD>`.
