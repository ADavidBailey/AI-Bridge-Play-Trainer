# Plan — Claude as your bridge partner, in the AI-Bridge-Play-Trainer (no BBO)

**Goal:** David + Claude play as partners vs robots — full hands (bid + play) — inside David's own trainer, to get better at conventions, judgment, and defense. No BBO, no ToS issue, and fast (programmatic, no browser automation).

## Why the trainer instead of BBO
- It's **David's software**, so Claude is an *intended* seat, not a prohibited bot (BBO's ToS forbids bots/automation; this sidesteps that entirely).
- **Direct integration** — the server just asks Claude for a bid/card; no reconnecting or screen-scraping → the tempo problem largely disappears.
- The hard pieces already exist: the trainer server, **PBS scenarios** as the deal source (via `BRIDGE_DATA_ROOT`), **DDS** for analysis, the **coaching** prose, and **Rick's BBA-CLI** for bids.

## Architecture
- A **table** = 4 seats: **S = David** (web UI), **N = Claude** (an "engine seat" called via the Claude API), **E/W = robots**.
- The server (FastAPI, `server.py`) runs the hand: **deal → auction → play → review**.
- **Claude seat:** the server calls the Claude API with that seat's *legal view only* — its own hand + the auction/trick so far (plus dummy once declaring). Fair by construction: Claude never sees hidden hands.
- **Robot seats:** bids from **BBA-CLI**; card play from **DDS** (or a single-dummy engine for a more human feel).
- **Deal source:** PBS scenarios (already wired into the trainer).

## Phases
- **Phase 0 — spike (headless):** deal a scenario; get bids from David(stub) + Claude(API) + BBA-CLI(robots); print the auction. Proves the Claude-seat call and the BBA-CLI integration. No UI.
- **Phase 1 — bidding table + review:** UI to bid your own seat; Claude partners; robots bid via BBA-CLI; after the auction, **Auction Compare vs BBA** + a short Claude review. (Reuses existing trainer UI patterns.)
- **Phase 2 — card play:** extend to the play — Claude + David play their cards, robots defend (DDS/engine), trick UI reused from the trainer; fully programmatic, so brisk.
- **Phase 3 — learning loop + polish:** post-hand review (DDS + coaching + "what to take away"); convention-card selection; scenario picker; support the **partner-needed conventions** (Spiral Raise, etc.) that robots can't play — the whole point of having a real partner.

## Tempo (the fix that prompted this)
- Programmatic per-bid/per-card API calls — **no ~1s browser reconnect, no card-by-card terminal round-trips.**
- Auto-play the forced/obvious cards; Claude pauses only at genuine decisions.
- Expected: bidding near-normal; play brisk.

## Open questions / dependencies
- **BBA-CLI:** can it return *one seat's* bid given (hand + auction-so-far + convention card), callable live in a loop? (Ask Rick.)
- **Robot card play:** DDS (installed) vs a single-dummy engine (fairer feel for defenders).
- **Claude-API cost/latency:** a hand is a few dozen short calls — small.

## Repo note
This is **trainer-repo work** (`~/AI-Bridge-Play-Trainer`), not PBS. PBS stays the content/deal source. (Memory is per-launch-dir, so build it from the trainer directory.)
