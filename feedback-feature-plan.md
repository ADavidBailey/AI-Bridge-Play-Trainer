# In-App User Feedback → GitHub Issues — Feature Plan

## Goal

Let Bridge Play Trainer users report a problem from inside the app — the
same way David reports issues to Claude: *here's what I'm looking at, and
here's why I think it's wrong*. Each report becomes a **GitHub issue** in
`ADavidBailey/AI-Bridge-Play-Trainer`, which David can triage and (later)
have Claude batch-process into fixes.

The audience is mostly seniors, so the user-facing side must be trivial:
one button, one text box, done.

## Key design decision: capture *state*, not just a screenshot

A raw screenshot loses the context that makes a report actionable — you'd
be squinting at pixels trying to reconstruct which board and which line of
prose is at fault. But the trainer already **knows** the full situation at
the moment the user clicks report: the scenario, the board number, the
deal, the auction, the current trick, the play mode, and the exact coaching
text on screen.

So a report bundles the user's note **plus** that structured state. The
`scenario · board` pair alone points straight at the offending file and
board in the content repo (`coaching/<Scenario>.pbn`), turning "something
looks wrong" into a one-line fix.

We deliberately **skip the pixel screenshot in v1**: the GitHub issues API
can't attach a binary image cleanly (you'd have to host it somewhere), and
the structured state is strictly more useful. An image path can be added
later if it ever earns its keep.

## Flow

```
User (in trainer)
  │  clicks 🚩 "Report a problem", types a note
  ▼
Frontend  (static/app.js)
  │  gathers note + live state from `lastState`
  │  POST /api/session/{sid}/report  { note }
  ▼
Backend  (server.py)
  │  reads the session, formats title + body + label
  │  httpx → GitHub Issues API  (auth: GITHUB_TOKEN)
  ▼
GitHub issue created  ──►  returns { ok, issue_url } to the UI
```

## Backend

New endpoint in `server.py`:

```
POST /api/session/{sid}/report
body: { "note": "<free text>" }
→ 200 { "ok": true,  "issue_url": "https://github.com/.../issues/123" }
→ 503 { "ok": false, "error": "feedback not configured" }   # token missing
```

- Adds `import httpx` (not currently used anywhere in the server) and calls
  `POST https://api.github.com/repos/ADavidBailey/AI-Bridge-Play-Trainer/issues`
  with an `httpx.AsyncClient`.
- Reads the token from `GITHUB_TOKEN` (same `os.environ.get(...)` pattern as
  `BRIDGE_DATA_ROOT`). **If the token is absent, the endpoint fails
  gracefully with a clear message** — every other part of the app keeps
  working, and the button just reports "feedback isn't configured yet."
- Pulls the report context from the live `Session` object server-side
  (don't trust the client to assemble it): scenario, board number, role,
  contract, trick number / stage, auction, current trick, the coaching/tip
  text currently in scope, and the deal (PBN) for reconstruction.

## Frontend

A **🚩 Report a problem** button in the below-table button row of
`static/index.html` (next to undo / hint / review), available both
mid-deal and after the result is shown. Clicking it opens a small modal
(same show/hide pattern as the existing scenario-picker panel) with one
textarea — "What looks wrong here?" — and a Send button. On success it
shows the new issue's link; on the not-configured error it says so plainly.

The frontend only needs to send the note + session id; the server already
holds everything else.

## The issue

**Title:** `Feedback: <scenario> · board <n> · <role>`

**Body:**

> **User says:** <the note>
>
> ---
> *auto-captured:*
> - Scenario: **Basic_Major** · Board: **14** · Role: **declarer**
> - Contract: 4♠ by S · trick 3, post-lead
> - Auction: 1♠ P 2♠ P / 4♠ P P P
> - Coaching shown: "…the marker text on screen…"
> - Deal (PBN): `N:… E:… S:… W:…`

**Label:** `user-feedback`

## The payoff: a triage-then-fix loop

The label is the hinge. Reports pile up as a filterable queue, and later
David can say "process the open feedback" and Claude will:

```
gh issue list --label user-feedback        # in the trainer repo
→ for each: open coaching/<Scenario>.pbn at the named board,
            fix the prose in the *content* repo, close the issue
```

So the feature spans both repos by design: **issues are filed in the
trainer repo; the actual content fixes land in Practice-Bidding-Scenarios**
(`coaching/*.pbn`), where the prose lives.

## Setup prerequisites (one-time, David only)

1. **GitHub token.** Create a **fine-grained PAT** scoped to *only*
   `ADavidBailey/AI-Bridge-Play-Trainer`, with **Issues: read/write** and
   nothing else. Put it in the environment the server runs under:
   `export GITHUB_TOKEN=…`. Setup notes will go in the README.
2. **`gh` CLI** is **not currently installed** on the Mac. It isn't needed
   to *file* issues (the server uses the REST API directly), but the
   batch-processing loop above wants `gh`. Install with `brew install gh`
   when we get to that stage.

## v1 scope

- **In:** the button + modal, the endpoint, structured-state issues, the
  `user-feedback` label, graceful no-token behavior, README setup notes.
- **Out (for now):** pixel screenshots (needs image hosting); abuse / rate
  limiting beyond requiring a valid live session (small, friendly audience —
  revisit if the app is ever deployed publicly).

## Files touched

- `server.py` — new `/api/session/{sid}/report` endpoint + `httpx` import
- `static/index.html` — the button + modal markup
- `static/app.js` — modal show/hide, POST, result handling
- `README` / setup docs — `GITHUB_TOKEN` instructions
