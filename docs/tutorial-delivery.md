# Tutorial delivery

The procedure lives in the `tutorial` skill: [`skills/tutorial/SKILL.md`](../skills/tutorial/SKILL.md).

- **Voice mode.** On "tutorial", "walk me through" or "show me what you can do", read
  `notes/first-time-tutorial.md` and deliver it one brief, voice-friendly section at a time,
  waiting for the user to try each step.
- **AG2 Space mode.** Right after the first-contact greeting in the owner's DM, the skill creates
  one "Getting started" HTML page (Room Commons) in that DM, a short AG2 Space tour with one thing
  to try per part (a Claude Code history import part only on a Claude Code core), and summons
  the owner to it. A marker under `<workspace>/state/tutorial/` keeps it to one page per room, so
  a re-link or reinstall refreshes the page instead of adding another.

## What the page is built from

- **The feature list.** The steps come from `skills/tutorial/feature-list.json` (one step per
  feature: title, two body parts, where to try it, a paste prompt, screenshots, the release that
  added it). `--list <file|https URL>` swaps the list; nothing is fetched by default, only https
  is fetched (redirects included), a list is at most 1 MiB and 24 steps, and a list that does not
  validate (markup, a placeholder other than `{name}`, a missing field) is reported on stderr and
  the skill's own file is used.
- **Revisions and `--if-newer`.** The list carries a revision (`tutorial.YYYY-MM-DD.N`), which the
  page records in its ownership tag. `publish --if-newer`, run after an engine update, rebuilds a
  page only when the list's revision is newer than the page's, with the prompts and roles the page
  was last built with; a current page is left alone even when a rebuild would differ.
- **Owner edits are kept.** Before any rewrite the page's text is compared with the hash recorded
  when the skill last wrote it. A page edited since is kept, with or without `--if-newer` and
  whatever the revision (`"action": "kept"`, one stderr line), and its entry is not touched, so a
  later revision does not overwrite the edit either.
- **A rewrite that is cut short is finished, and an edit made meanwhile is kept.** A rewrite clears
  the page and appends the new text in pieces, so before the first write the entry becomes an
  `updating` journal: the target page, the hash of the page before, the hash after each call, and
  the entry before. The next run settles the journal before rendering anything of its own. A page
  in a state the rewrite itself leaves is finished to the target byte for byte, checked against the
  recorded hash, and the run stops there (`"action": "resumed"`, reporting the journalled name and
  revision; its own request, `--summon` and new prompts or media included, waits for the next
  run). A page still as before means the rewrite never ran and the run proceeds as usual. Any other
  page is someone's edit: it is kept, and the entry from before the rewrite stands whole, so the
  next run keeps it too. Every call of a rewrite first reads the page and runs only on exactly the
  page the previous call left, so an edit made between two calls stops the rewrite before its next
  write; the page is read back against the target before success is recorded, and a run that
  resumes continues from the call the journal recorded, never from the page's length.
- **Identity.** `publish` needs the agent's mxid (`--agent-mxid`, `$AGENT_MXID` or the AG2 Space
  channel env file), resolved and checked once (`@localpart:server`, whitespace trimmed) before any
  Room Commons call, and refuses to run without one: the page's ownership tag is how a later run
  finds it. A revision-only bump of the list rewrites the page once (its tag records the revision)
  and the next `--if-newer` is a no-op.
- **Screenshots.** Each screenshot is a Room Commons asset reference: the page points at the
  room's media (`mxc://`, from `--media` or the room's recorded map) when the room has the asset,
  and inlines the bundled fallback file otherwise, so a room with no uploads gets the same page as
  before. Every page variant stays under 300 KB.

Last verified: 2026-10-24, against `tests/tutorial-feature-list.test.py` and
`tests/tutorial-getting-started.test.py` at the head that introduced the feature list. The live
Room Commons round trip of `publish --if-newer` was witnessed at the previous head; the
HTML-surface rendering of `mxc://` media was not.
