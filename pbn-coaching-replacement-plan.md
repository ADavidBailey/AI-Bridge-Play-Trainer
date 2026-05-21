# Plan: Replace Claude API coaching with embedded PBN coaching

## Context

The Bridge Play Trainer (at `/Users/adavidbailey/AI-Bridge-Play-Trainer`) currently calls the Anthropic API directly from the browser to provide two forms of coaching: a Tricks/Promotion/Risks plan after the opening lead, and an end-of-play audit. There is also a manual "testing" mode that grades user-submitted reads on hidden hands. A 7-step API-key wizard, a settings modal, and a per-session cost ticker support this.

The user is exploring a different approach. Their upstream pipeline (`Practice-Bidding-Scenarios`) that generates the 343 `bba/*.pbn` scenarios will be extended to also generate **bidding-tutorial coaching content** embedded in each PBN file, in the [Baker-Bridge format](https://github.com/Rick-Wilson/Baker-Bridge/blob/main/Package/2over1.pbn). The trainer should consume this prose during the existing auction-replay animation — bid by bid — and drop all Claude API calls entirely.

Confirmed scope (user-selected):
1. **Replace all Claude calls**, not just one. No proxy, no fallback.
2. **Bid-by-bid presentation** during the auction-replay animation (not quiz mode).
3. Scenarios without embedded coaching just play normally — no coaching panel, no AI substitute.

## Format spec

After a board's standard PBN tags + `[Auction "..."]` block (and the auction lines following it), an optional single curly-brace block may contain free prose. Markers inside the block:

- `[show X]` — reveal hand(s). `X ∈ {N, E, S, W, NS, EW, NE, SW, …}`. Effects accumulate for the remainder of the tutorial.
- `[BID xxx]` — anchor. `xxx` matches an auction bid in PBN form (`1C`, `2D`, `3NT`, `X`, `XX`). Passes are not anchored.
- Suit escapes `\S \H \D \C` → `♠ ♥ ♦ ♣`.

**Chunking rule.** Prose before the first `[BID]` is an **intro chunk** (no bid anchor — displayed before the auction starts). Each subsequent chunk runs from one `[BID xxx]` marker to the next (or to end of block), and is anchored to the bid named by its leading marker. `[show X]` directives inside a chunk attach to that chunk's `reveals` set. Repeated bids resolve left-to-right against the auction; a `[BID]` with no remaining match degrades to the previous successfully-anchored chunk (warning logged) — keeps prose visible somewhere instead of dropping it.

The existing `{Shape ...} {HCP ...} {Losers ...}` lines that appear between `[Deal]` and `[Declarer]` in current `bba/*.pbn` files are **not** coaching and must be ignored — the parser must look specifically at the post-auction tail.

## Server changes (server.py)

1. **`parse_coaching(raw_pbn_text: str, auction_pbn_calls: list[str]) -> list[dict] | None`** — inline in `server.py` (no new module). Returns `None` if no post-auction `{ ... }` block exists; otherwise an ordered list of chunks `{ bid_index: int|None, reveals: list[str], text: str }` with suit escapes already substituted and `[show]` markers stripped.

2. **`_split_pbn_by_board(text: str) -> list[str]`** — splits the file's raw text by `[Event ...]` so the right slice can be passed to `parse_coaching` for the chosen `board_index`. `endplay.parsers.pbn.load` strips the curly-brace block, so the raw text must be parsed alongside.

3. **`start_session`** ([server.py:549](server.py#L549)): after `boards = list(pbn.load(f))`, also read raw text, slice to the chosen board, derive `auction_pbn_calls` from `boards[idx].auction` (use existing `auction_dict` shape but in PBN form), call `parse_coaching`, and store result on the new `Session.coaching` attribute.

4. **`Session.state()`** ([server.py:281](server.py#L281)): append two fields — `st["coaching"] = self.coaching` and `st["rotation_shift"] = self._rotation_shift`. The shift lets the frontend map `[show N]` (real-compass, as authored) to its display seat. No server-side rotation of `reveals` — keep them in author's real-compass frame so the file is portable.

5. **Visibility stays a frontend concern.** Server's `visible_hands()` ([server.py:260](server.py#L260)) is left untouched and continues to enforce real-bridge rules during the play phase (e.g., dummy hidden pre-lead). During the tutorial phase the user is reading prose, not playing — having the frontend mask hands per `[show]` directives is simpler than duplicating tutorial state in `Session`.

6. **Delete** the now-unused endpoints: `/api/session/{sid}/ground-truth` ([server.py:632](server.py#L632)) and `/api/session/{sid}/preview-after-lead` ([server.py:641](server.py#L641)), plus their helpers (`_ground_truth_payload` and supporting code in that range).

## Frontend changes

### Delete in `static/app.js`

- `CLAUDE_MODEL`, `bumpDealCost`, the pricing table.
- All system prompt constants (`COACH_AFTER_LEAD_TPR_PROMPT` ~1196, `COACH_END_OF_PLAY_PROMPT` ~1326, `FREE_SYSTEM_PROMPT` ~1158, `STRUCTURED_SYSTEM_PROMPT` ~1175).
- `callClaude` (~1438), `callClaudeCoach` (~1278), `parseJsonFromClaude`, `runCoachingTip`, `submitInference`, `buildCoachingPayload`, `buildFreePayload`, `buildStructuredPayload`, `prefetchAfterLeadTip`, `pendingCoachingTrigger`, `ensureGroundTruth`, `inferenceInputMode`.
- Grading-mode plumbing: `gradingMode`, `gradingEnabled`, `gradingKey`, `isCoaching`, and the `getPrefs/setPrefs` keys tied to the API key + settings handlers.

### Delete in `static/index.html`

- Cost ticker `#session-cost` (~line 17).
- Settings button `#settings-btn` (~line 18).
- `#settings-modal` (~lines 108-143).
- `#wizard-modal` and all 7 wizard steps (~lines 145-303).

### Keep / reuse

- `#inference-panel` HTML stays — repurposed as the prose display slot.
- `coachingTips[]` state and `renderCoachingPanel()` stay — same rendering path, different source (PBN chunks instead of Claude responses).
- `appendTextWithColoredSuits` ([static/app.js:208](static/app.js#L208)) handles `♠♥♦♣` already; suit escapes are substituted server-side before they hit JS.

### Add (~80 LOC in app.js)

- `let tutorialChunkIdx = 0;`
- `let tutorialReveals = new Set();`
- `function applyTutorialMask(state)` — given `tutorialReveals` and `state.rotation_shift`, null out `state.hands[seat]` for seats not yet revealed, **before** `render(state)` consumes the state. The author's real-compass seat letter maps to its display letter via `(realIndex + shift) % 4`, then index→letter.
- Modify the auction-replay loop (around `auctionVisibleCount` / `auctionAnimationToken` / `awaitingPlay`): step bid-by-bid, and after each increment, look up `state.coaching` for a chunk with `bid_index === auctionVisibleCount - 1`. If found: push `text` into `coachingTips`, merge `reveals` into `tutorialReveals`, re-render to mask, pause until user clicks "Continue" before resuming. The intro chunk (`bid_index === null`) plays once before the loop starts.
- `startPlay()` clears `tutorialReveals` and re-renders — control reverts to the server's `visible_hands`.

If `state.coaching === null` (a non-coached board), skip the bid-by-bid pause loop entirely and use the existing instant-reveal path. `applyTutorialMask` is a no-op when `tutorialReveals` is empty. The 343 existing scenarios therefore behave exactly as today, minus the AI calls.

## Order of changes

1. Add `parse_coaching` + `_split_pbn_by_board` in `server.py`; expose on `Session.coaching` and via `state()`. Add `rotation_shift` to `state()`.
2. Delete `_ground_truth_payload`, the `/ground-truth` and `/preview-after-lead` endpoints.
3. Delete settings/wizard HTML + cost ticker in `index.html`.
4. Delete all Claude code paths in `app.js`.
5. Add `applyTutorialMask` + the bid-stepped animation loop; wire prose chunks into the existing coaching panel.
6. Verify (below).
7. Update `CLAUDE.md` to drop references to Claude API coaching and document the embedded-coaching format.

## Critical files

- [server.py](server.py) — `start_session` at L549, `Session.state` at L281, `visible_hands` at L260, endpoints to delete at L632/L641.
- [static/app.js](static/app.js) — auction-replay machinery around `auctionAnimating`/`auctionVisibleCount`/`auctionAnimationToken`/`awaitingPlay` (declared L11-15); all Claude code paths in the L1158-1469 range.
- [static/index.html](static/index.html) — header L17-18, settings/wizard modals L108-303, inference panel L79-89.
- [CLAUDE.md](CLAUDE.md) — update after the change lands.

## Verification

1. Download `https://raw.githubusercontent.com/Rick-Wilson/Baker-Bridge/main/Package/2over1.pbn` to `~/test-bba/bba/2over1.pbn`.
2. Start the server pointed at it: `BRIDGE_DATA_ROOT=~/test-bba python3 -m uvicorn server:app --reload --port 8765`.
3. In a browser, open `http://localhost:8765`, pick `2over1`, board 1. Expect:
   - Intro prose appears before the auction animation begins.
   - Each bid in the auction triggers its anchored prose chunk; a "Continue" gate pauses between chunks.
   - `[show S]` reveals South only; other hands stay masked until later `[show]` directives.
   - Clicking "Play" clears the tutorial state, and pre-lead visibility reverts to the server's normal rule (declarer face-up, dummy hidden until after the opening lead).
4. Spot-check non-coached fallback: `BRIDGE_DATA_ROOT=~/Practice-Bidding-Scenarios python3 -m uvicorn server:app --reload --port 8765`, load `1C_WalshStyle` board 1. Expect: auction reveals instantly, no coaching panel, play proceeds as before, **zero** outbound calls to `api.anthropic.com` (verify in DevTools Network tab).
5. Ad-hoc parser check: `python3 -c "from server import parse_coaching, _split_pbn_by_board; raw=open('~/test-bba/bba/2over1.pbn').read(); boards=_split_pbn_by_board(raw); ...; print(chunks)"` — confirm chunk count, `bid_index` resolutions, and that the intro chunk has `bid_index is None`.
