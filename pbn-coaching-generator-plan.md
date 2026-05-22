# Plan: Generate embedded coaching prose for bba-filtered PBN files

## Context

The trainer (this repo) now consumes Baker-Bridge-style `{[show X] ... [BID xxx] ...}` coaching blocks embedded in each board's PBN. The 343 existing `bba/` scenarios ship with no such blocks and fall through to the instant-reveal path. To bring coaching to those scenarios, an upstream offline job needs to read each scenario's metadata + filtered boards and generate the prose, writing augmented files into a parallel `coaching/` directory.

Confirmed scope:

- **Pilot** — the 8 scenarios in the "Beginners Bidding" section of `btn/-button-layout-release.txt`:
  `Basic_What_To_Open`, `Basic_Overcall`, `Basic_Minor`, `Basic_Major`, `Basic_NT`, `Basic_Takeout_Double`, `Basic_Weak_2`, `Play_Top_Tricks`.
- **Source** — `Practice-Bidding-Scenarios/bba-filtered/<scenario>.pbn`. First **30 boards only** per file.
- **Bidding-system context** — the `/*@chat ... @chat*/` block from `Practice-Bidding-Scenarios/btn/<scenario>.btn`. Hand-written, names the convention (e.g. "Basic Bridge"), already in instructional voice. **No bbsa convention card** in the prompt — concise wins.
- **Model** — Claude Code session model (whatever I'm running as). **No Anthropic API key, no offline script for the pilot.** Each scenario is delegated to a Task subagent so all 8 run in parallel.
- **Output** — `Practice-Bidding-Scenarios/coaching/<scenario>.pbn`, structured identically to the input PBN but with a `{...}` coaching block inserted after each board's `[Auction "..."]` block (and its auction lines).
- **Pipeline later** — once the prose quality + format are dialed in, productionize as a script using the Claude Agent SDK so the upstream `Practice-Bidding-Scenarios` pipeline can call it per scenario. Out of scope for this PR.

## The 8 scenarios

```
Basic_What_To_Open   Basic_Overcall
Basic_Minor          Basic_Major
Basic_NT             Basic_Takeout_Double
Basic_Weak_2         Play_Top_Tricks
```

All 8 have both `bba-filtered/<x>.pbn` and `btn/<x>.btn` present.

## Generation: 8 parallel Task subagents

I orchestrate from the main session. For each of the 8 scenarios, I spawn one `Agent` (general-purpose subagent) whose self-contained prompt contains:

- the **scenario brief** (the `/*@chat */` body from `btn/<scenario>.btn`),
- the **first 30 boards** of `bba-filtered/<scenario>.pbn` (raw text, sliced via the same `[Event "..."]` split server.py uses),
- the **format spec + authoring rules** (the system-prompt content below),
- the **expected output path** (`Practice-Bidding-Scenarios/coaching/<scenario>.pbn`).

The subagent's job: for each of the 30 boards, write a `{...}` block; assemble the full augmented PBN (30 coached boards + the unchanged remainder of the file passed through verbatim); write the result via `Write` to the expected path. The agent returns a short status report (boards coached / boards passed through / any validation failures).

All 8 agents fire in **one** message with 8 `Agent` tool calls so they run concurrently.

### Subagent prompt template

```
You're writing concise, conversational bridge-teaching prose to embed in a PBN
file. The trainer renders your output chunk-by-chunk during the auction replay,
pausing for "Continue" between chunks.

## Format

You emit the body of a single {...} block per board (no leading {, no trailing
}, no code fences). Use these markers:

  [show X]   reveal hand(s). X is one or more seat letters from {N, E, S, W}.
             Letters refer to REAL compass — N is the actual North seat in the
             PBN [Deal] string. Reveals accumulate across the board.

  [BID xxx]  anchor the following prose to a specific bid in the auction. xxx
             uses PBN form: 1C, 2D, 3NT, X, XX. Prose between two [BID] markers
             attaches to the FIRST one. Prose BEFORE the first [BID] is the
             intro chunk — shown before the auction starts.

  \S \H \D \C   suit escapes that render as ♠ ♥ ♦ ♣.

## Authoring rules

- Emit one intro chunk and an anchored chunk for EACH non-pass call from the
  student's side (typically South). Skip anchoring on Pass.
- Intro chunk starts with [show S] to reveal the student's hand. Subsequent
  [show ...] directives may reveal partner / opponents as the narrative
  requires (often [show NS] near the end of the auction).
- Refer to seats from the student's perspective: "you" (S), "partner" (N),
  "LHO" (W), "RHO" (E).
- Each chunk: 2-4 short sentences, conversational, second-person.
- Ground prose in the scenario brief. If the brief teaches a decision tree
  (e.g. "5+ spades → 1S"), walk the student through that decision for THIS
  hand.
- Never include the {} braces in the per-board block. Never anchor [BID Pass].
- Every [BID xxx] must match an unconsumed PBN call in the auction (case-
  insensitive). If you can't find a clean match, drop the marker rather than
  inventing one.

## Scenario brief

{paste of the /*@chat */ body verbatim}

## Boards to coach (first 30)

{paste of the 30 board slices verbatim, separated by ---}

## Boards to pass through (remainder)

{paste of the rest of the file verbatim — these get appended unchanged}

## Task

For each of the 30 boards above:
  1. Extract the auction in PBN call form.
  2. Author the {...} block body per the rules above.
  3. Splice it into the board's text RIGHT AFTER the [Auction "..."] block
     (after the auction call lines, before any subsequent [Tag] like
     [BidSystemEW]).

Assemble: 30 coached boards (in original order) + the pass-through remainder
(verbatim). Write the result to:

  /Users/adavidbailey/Practice-Bidding-Scenarios/coaching/<scenario>.pbn

Then return a 3-line status: boards coached / boards passed through / any
warnings (e.g. [BID] markers you couldn't anchor).
```

### Validation (done by the subagent)

Before writing, each subagent self-checks every `[BID xxx]` it emitted against the actual auction. If a marker doesn't match, the subagent drops it (and includes a warning in its return summary). The main session spot-checks a sample of outputs by re-running `server.parse_coaching` against them and confirming chunk counts + bid-index assignments look sane.

### System prompt (sketch)

```
You are writing concise, conversational bridge-teaching prose to embed in a PBN
file. The trainer renders your output chunk-by-chunk during the auction replay,
pausing for "Continue" between chunks.

Output format — emit only the body of a single {...} block (no leading {, no
trailing }, no code fences). Use these markers:

  [show X]   reveal hand(s). X is one or more seat letters from {N, E, S, W}.
             Letters refer to REAL compass — N is the actual North seat in the
             PBN [Deal] string. Reveals accumulate across the board.

  [BID xxx]  anchor the following prose to a specific bid in the auction. xxx
             uses PBN form: 1C, 2D, 3NT, X, XX. Prose between two [BID] markers
             attaches to the FIRST one. Prose BEFORE the first [BID] is the
             intro chunk — shown before the auction starts.

  \S \H \D \C   suit escapes that render as ♠ ♥ ♦ ♣.

Authoring rules:

- Always emit one intro chunk and an anchored chunk for EACH non-pass call from
  the student's side (in this scenario, South). Skip anchoring on Pass.
- The intro chunk starts with [show S] to reveal the student's hand only.
  Subsequent [show ...] directives may reveal partner / opponents as the
  pedagogical narrative requires (often [show NS] near the end of the auction).
- Refer to seats from the student's perspective: "you" (S), "partner" (N),
  "LHO" (W), "RHO" (E).
- Keep each chunk to 2-4 short sentences. Conversational, second-person.
- Ground the prose in the scenario brief below. If the brief teaches a specific
  decision tree (e.g. "5+ spades → 1S"), walk the student through that decision
  for THIS hand.
- Do NOT include the {} braces in your output.
```

The scenario brief (the `/*@chat */` body) is prepended to the system prompt with prompt caching (`cache_control: ephemeral`) so we pay for it once per scenario, not once per board.

### User message per board

```
Dealer: {dealer}
Vulnerability: {vul}

Hands (real compass):
  N: ♠... ♥... ♦... ♣...
  E: ♠... ♥... ♦... ♣...
  S: ♠... ♥... ♦... ♣...
  W: ♠... ♥... ♦... ♣...

Auction (in PBN form, dealer first):
  {bid1} {bid2} {bid3} ...

Contract: {contract} by {declarer}

Write the coaching prose for this board.
```

### Validation

After each successful API response, before splicing:

1. Strip any wrapping `{ ... }` (model sometimes adds them).
2. Strip code fences if present.
3. Walk the prose, find every `[BID xxx]`. For each, find the next unconsumed PBN call in the actual auction with case-insensitive match. If any `[BID]` can't be matched, retry the API call once with `"\n\nYour previous [BID] markers didn't all match the auction: {bid_x, bid_y}. The auction is exactly: {full auction}. Re-emit using only those bids."` If the retry also fails, log and skip that board (write a `% coaching-generation-failed` comment in its place).
4. Sanity-check that suit escapes are well-formed.

### Output splicing

The simplest correct splice: re-implement `_split_pbn_by_board` from server.py (split on `[Event "..."]` lookahead), find the position right after the last `[Auction ...]` line + auction lines (look for the first blank line or next `[Tag` after `[Auction`), and insert `\n{<generated body>}\n` there. For boards beyond the 30-board limit, copy them through unchanged.

## Server integration

Add a `COACHING_DIR = DATA_ROOT / "coaching"` constant. In `start_session` ([server.py:684](server.py#L684)), prefer `COACHING_DIR / "<scenario>.pbn"` if it exists; otherwise fall back to `BBA_DIR / "<scenario>.pbn"`. Everything else (parsing, session, frontend rendering) is unchanged — the coached files have the same structure as the Baker-Bridge sample we already verified.

Menu ([server.py:526](server.py#L526)) keeps listing scenarios from `bba/` (the canonical inventory). Whether a given scenario has coaching is invisible to the menu — the frontend just notices `state.coaching` is non-null at session start.

## Order of changes

1. **Pilot one scenario.** Spawn one subagent on `Basic_NT` to produce `coaching/Basic_NT.pbn` (30 boards). Read the output, spot-check 2–3 boards by hand, run `server.parse_coaching` on them to confirm chunk structure.
2. **Update server fallback** so the trainer prefers `coaching/<x>.pbn` when present. Verify in the browser end-to-end on `Basic_NT`.
3. **If prose quality is acceptable**, fan out: spawn the remaining 7 subagents in **one message** (parallel). Each writes its own `coaching/<scenario>.pbn`.
4. **If quality needs work**, iterate on the subagent prompt template before fanning out. The format-spec and authoring-rules sections are the levers.
5. **Spot-check 3 scenarios in the browser**: intro fires, [BID] anchors land, [show] reveals make sense, Play hands off to the existing play flow.
6. **Commit** the 8 generated PBNs (in `Practice-Bidding-Scenarios/coaching/`) + the server fallback change.

## Open questions

- **Idempotence**: if `coaching/<x>.pbn` already exists, the subagent overwrites unless we say otherwise. For the pilot, overwriting is fine — we only have empty `coaching/` today. Productionizing later may want a skip-if-exists default.
- **Pipeline integration**: out of scope for this PR. The subagent prompt is portable; later it can be invoked by the Claude Agent SDK from the upstream pipeline.

## Critical files

- `Practice-Bidding-Scenarios/btn/<scenario>.btn` — scenario brief source.
- `Practice-Bidding-Scenarios/bba-filtered/<scenario>.pbn` — board source.
- `Practice-Bidding-Scenarios/coaching/<scenario>.pbn` — subagent output (new).
- [server.py:684](server.py#L684) — coaching-dir lookup add point.
