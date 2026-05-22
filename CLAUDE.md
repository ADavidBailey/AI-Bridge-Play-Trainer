# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

```
python3 -m uvicorn server:app --reload --port 8765
```

Then open http://localhost:8765/. There is no test suite, lint config, or build step.

### External data dependency (important)

The repo ships code only. Scenario PBNs and the menu-layout files live **outside** this repo, by default at `/Users/adavidbailey/Practice-Bidding-Scenarios/`. The server expects:

- `<DATA_ROOT>/bba/*.pbn` — one PBN per scenario; `<scenario>.pbn` is loaded by name.
- `<DATA_ROOT>/btn/-button-layout-release.txt` (or `-beta.txt`) — defines the menu sections and which scenarios appear under each.

Override the location with `BRIDGE_DATA_ROOT=/some/path` ([server.py:29](server.py#L29)). If neither button-layout file is found, the menu falls back to a flat alphabetical list of all PBNs.

### Embedded coaching prose

PBN files may carry a Baker-Bridge–style tutorial block after `[Auction "..."]`:

```
[Auction "N"]
1H Pass 2D Pass 2NT Pass 3NT Pass Pass Pass
{[show S]
After North's 1\H opening South counts 12 HCP plus one length point for the fifth \D. What do you bid?[BID 2D]
That is enough for a 2/1 response so South says 2\D. ...[BID 3NT]
South is happy to play in Notrump ...
[show NS]}
```

Recognised markers:
- `[show X]` — `X` is one or more seat letters (`N`, `EW`, `NSEW`, …) in **real** compass; reveals accumulate.
- `[BID xxx]` — anchors the chunk to the next unconsumed matching PBN-form call in the auction (`1C`, `2D`, `3NT`, `X`, `XX`). Case-insensitive. Prose before the first `[BID]` is the intro chunk (bid_index=None). Unmatched `[BID]`s degrade to the previous successfully-anchored chunk.
- `\S \H \D \C` — substituted server-side to `♠ ♥ ♦ ♣`.

Pre-auction `{Shape ...} {HCP ...} {Losers ...}` comment blocks in the existing `bba/*.pbn` files are **ignored** — the parser only inspects the curly block immediately following `[Auction]`. `_strip_post_auction_blocks` ([server.py:153](server.py#L153)) removes that block before handing off to `endplay.parsers.pbn.load` (endplay tries to parse `{[show` as a bid call and chokes); `parse_coaching` ([server.py:223](server.py#L223)) reads from the unstripped raw text and produces the ordered chunk list stored on `Session.coaching`. Frontends consume `state.coaching` + `state.rotation_shift` to step bid-by-bid (`animateAuction` in [static/app.js](static/app.js)) and `state.initial_hands` to apply `[show]`-based masking via `applyTutorialMask`.

Scenarios without embedded coaching just play normally — `state.coaching === null` skips the tutorial path entirely. There are no outbound calls to any AI service; all coaching prose is precomputed upstream.

### CLI prototypes

`pipe1.py` … `pipe5.py` are standalone CLI walkers, in evolutionary order — they predate the web UI and are kept as references / scratch tools. Run any of them directly with `python3 pipeN.py`. Pipe5 supports `--role declarer|defender_e|defender_w`.

## Architecture

### One file: server.py

All backend logic lives in [server.py](server.py). A `Session` class ([server.py:279](server.py#L279)) owns the full state of one in-progress deal; sessions are kept in an in-memory `SESSIONS` dict keyed by an opaque token (no persistence, no auth, single-user local app).

The frontend is plain JS in [static/](static/) — no build, no framework. `app.js` polls the session via `fetch` and re-renders.

### Display rotation: user is always South

The user's actual compass seat depends on which side they're playing, but the frontend always renders them at the bottom (South). `Session._rotation_shift` ([server.py:310](server.py#L310)) is computed once at session start; `_rotate_state_for_user` ([server.py:492](server.py#L492)) relabels every seat reference in the outgoing state payload. **The underlying `Deal`, DDS calls, and dealer engine all use real compass** — only the API boundary rotates. When debugging, remember: a seat letter in `state()` output is in display frame; a `Player` enum inside the Session is real compass. Coaching reveals in `state.coaching` stay in the **author's real-compass frame** (so PBNs are portable); the frontend maps them via `state.rotation_shift`.

### DDS drives non-user seats

Defenders (and the dummy when the user isn't declarer) are auto-played by `dds_pick` ([server.py:119](server.py#L119)), which calls `endplay.dds.solve_board` and picks the card with the highest tricks-remaining count. `auto_play_until_user` ([server.py:551](server.py#L551)) loops this after every user play until control returns. The deal's double-dummy table is also cached once per session for scoring.

### Undo: replay from scratch

`endplay`'s `unplay()` cannot cross trick boundaries, so undo is implemented by keeping a full `move_log` of every card played and an `undo_stack` of "card-count before this checkpoint" markers. `undo_to_checkpoint` ([server.py:606](server.py#L606)) rebuilds a fresh `Deal` from `initial_hands` and replays `move_log[:target]`. Only `/play` and `/claim` push checkpoints — DDS auto-plays do not, so one undo rewinds the user's last decision plus everything DDS did in response.

### Scoring is reported as IMPs vs double-dummy

`_scoring` ([server.py:349](server.py#L349)) returns (actual_score, dd_tricks, dd_score, imps_vs_dd) all from the **student's** perspective — signs are flipped when the student is defending. The trainer doesn't model doubles; all contracts are scored as undoubled. `diff_to_imps` uses the WBF/ACBL table.

### Roles

`role` is one of `declarer` (user controls declarer + dummy), `leader` (user is the opening leader / defender on lead), or `defender` (user is the leader's partner). Defender modes are exercised by the API but the UI today focuses on declarer.

## Design docs

- [data-model.md](data-model.md) — original design doc. The "HTTP API" table there is **partially out of date** (e.g. it lists `/api/session/{id}/inference` and `/api/session/{id}/next_deal`, which aren't implemented in server.py). Treat it as design intent, not a contract; verify against `@app.get`/`@app.post` decorators in [server.py](server.py).
- [wireframe.md](wireframe.md) — ASCII UI mockup. Predates the current design; the inference + trainer-feedback panels it shows were never built. The inference panel slot is now used to render bid-by-bid coaching prose from the PBN tutorial block.
- [pbn-coaching-replacement-plan.md](pbn-coaching-replacement-plan.md) — design notes for the swap from a Claude-API coaching path to embedded PBN prose. Kept for context; the migration is done.
