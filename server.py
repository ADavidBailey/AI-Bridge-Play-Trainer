"""
Bridge Play Trainer — FastAPI backend.

Start with:
    uvicorn bridge-play-trainer.server:app --reload --port 8765

Then open http://localhost:8765/ in your browser.

Optional: set GITHUB_TOKEN (a fine-grained PAT scoped to this repo with
Issues: read/write) to enable the in-app "Report a problem" button, which
files user feedback as GitHub issues. Without it, the button reports that
feedback isn't configured and everything else works normally. See
feedback-feature-plan.md.

Declarer mode only for the MVP. Defender mode + Claude grading come next.
"""

import io
import re
import random
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from endplay.types import Player, Denom, Rank, Deal, Contract, Vul, Penalty, Card
from endplay.dds import solve_board, calc_dd_table
from endplay.parsers import pbn

import os
import time
import httpx

APP_DIR = Path(__file__).resolve().parent

# Load a gitignored .env (e.g. GITHUB_TOKEN for the feedback button) without a
# hard dependency on python-dotenv. Real environment variables win over .env.
_ENV_FILE = APP_DIR / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

DATA_ROOT = Path(os.environ.get("BRIDGE_DATA_ROOT", "/Users/adavidbailey/Practice-Bidding-Scenarios"))
REPO_ROOT = DATA_ROOT
BBA_DIR = DATA_ROOT / "bba"
COACHING_DIR = DATA_ROOT / "coaching"
CURATED_DIR = DATA_ROOT / "coaching-curated"
STATIC_DIR = APP_DIR / "static"
GITHUB_REPO = "ADavidBailey/AI-Bridge-Play-Trainer"  # in-app feedback files issues here


def _scenario_pbn_path(scenario: str) -> Path | None:
    """Resolve a scenario to its PBN, preferring the pipeline-generated
    curated lesson (coaching-curated/), then the hand-authored coaching/,
    then the raw bba/ pool."""
    for d in (CURATED_DIR, COACHING_DIR, BBA_DIR):
        p = d / f"{scenario}.pbn"
        if p.exists():
            return p
    return None

SEAT_LETTER = {Player.north: "N", Player.east: "E", Player.south: "S", Player.west: "W"}
LETTER_SEAT = {v: k for k, v in SEAT_LETTER.items()}
DENOM_LETTER = {Denom.spades: "S", Denom.hearts: "H", Denom.diamonds: "D", Denom.clubs: "C", Denom.nt: "NT"}
DENOM_SYM = {Denom.spades: "♠", Denom.hearts: "♥", Denom.diamonds: "♦", Denom.clubs: "♣", Denom.nt: "NT"}
SUIT_FROM_CHAR = {"S": Denom.spades, "H": Denom.hearts, "D": Denom.diamonds, "C": Denom.clubs}
RANK_FROM_CHAR = {"A": Rank.RA, "K": Rank.RK, "Q": Rank.RQ, "J": Rank.RJ, "T": Rank.RT,
                  "9": Rank.R9, "8": Rank.R8, "7": Rank.R7, "6": Rank.R6, "5": Rank.R5,
                  "4": Rank.R4, "3": Rank.R3, "2": Rank.R2}
HONOR_HCP = {Rank.RA: 4, Rank.RK: 3, Rank.RQ: 2, Rank.RJ: 1}


def left_of(p):
    return Player((int(p) + 1) % 4)


def partner_of(p):
    return Player((int(p) + 2) % 4)


def seat_at_auction_index(dealer: Player, idx: int) -> Player:
    """The seat that made the call at position idx (dealer calls at idx 0,
    then clockwise/left)."""
    s = dealer
    for _ in range(idx):
        s = left_of(s)
    return s


# Pronoun tokens for rotation-aware coaching. A [BID] chunk is authored in the
# second person addressing ITS OWN actor; when the student is sitting in that
# seat the tokens render second person ("you"), otherwise third person ("your
# partner"). This lets one coaching file read correctly from either seat under
# Randomly Rotate. Intro / [show NS] reflection chunks should be authored
# seat-neutral (no tokens) and pass through unchanged.
#   @S / @s        subject  → You / Your partner   (you / your partner)
#   @Your / @your  possessive → Your / Their       (your / their)
#   @v(base|third) verb agreement → base (student) / third (partner)
#   (parentheses, NOT braces — a '}' inside the token would collide with the
#   coaching block's own '}' delimiter and truncate it.)
_PRONOUN_VERB_RE = re.compile(r'@v\(([^|)]*)\|([^)]*)\)')

def fill_pronouns(text: str, is_student: bool) -> str:
    if not text or '@' not in text:
        return text
    text = _PRONOUN_VERB_RE.sub((lambda m: m.group(1)) if is_student
                                else (lambda m: m.group(2)), text)
    pairs = (([('@Your', 'Your'), ('@your', 'your'), ('@S', 'You'), ('@s', 'you')])
             if is_student else
             ([('@Your', 'Their'), ('@your', 'their'),
               ('@S', 'Your partner'), ('@s', 'your partner')]))
    for tok, rep in pairs:
        text = text.replace(tok, rep)
    return text


def parse_contract(s):
    if not s or s in ("?", "Pass"):
        return None
    level = int(s[0])
    strain = s[1:].rstrip("X")
    m = {"S": Denom.spades, "H": Denom.hearts, "D": Denom.diamonds, "C": Denom.clubs, "N": Denom.nt, "NT": Denom.nt}
    return level, m[strain]


def derive_declarer(auction, dealer, trump):
    seat = dealer
    last_bidder = None
    final = {}
    for call in auction:
        if hasattr(call, "denom"):
            last_bidder = seat
            side = "NS" if seat in (Player.north, Player.south) else "EW"
            final.setdefault((side, call.denom), seat)
        seat = left_of(seat)
    winning_side = "NS" if last_bidder in (Player.north, Player.south) else "EW"
    return final[(winning_side, trump)]


def hand_of(deal, seat):
    return [deal.north, deal.east, deal.south, deal.west][int(seat)]


def hand_to_dict(hand):
    return {
        "S": [r.abbr for r in hand.spades],
        "H": [r.abbr for r in hand.hearts],
        "D": [r.abbr for r in hand.diamonds],
        "C": [r.abbr for r in hand.clubs],
    }


def hand_hcp(hand):
    return sum(HONOR_HCP.get(r, 0)
               for it in (hand.spades, hand.hearts, hand.diamonds, hand.clubs) for r in it)


def auction_dict(auction, dealer):
    seat = dealer
    rows = []
    for call in auction:
        if hasattr(call, "denom"):
            txt = f"{call.level}NT" if call.denom == Denom.nt else f"{call.level}{DENOM_SYM[call.denom]}"
            ann = getattr(call, "announcement", None)
        else:
            penalty = getattr(call, "penalty", None)
            pn = getattr(penalty, "name", None) if penalty is not None else None
            txt = {"passed": "Pass", "doubled": "X", "redoubled": "XX"}.get(pn, "Pass")
            ann = None
        rows.append({"seat": SEAT_LETTER[seat], "call": txt, "annotation": ann})
        seat = left_of(seat)
    return rows


def card_to_str(card):
    return f"{DENOM_LETTER[card.suit]}{card.rank.abbr}"


def card_to_display(card):
    return f"{DENOM_SYM[card.suit]}{card.rank.abbr}"


def dds_pick(deal):
    sb = solve_board(deal)
    return max(sb, key=lambda x: x[1])[0]


_IMP_THRESHOLDS = [
    (20, 1), (50, 2), (90, 3), (130, 4), (170, 5), (220, 6),
    (270, 7), (320, 8), (370, 9), (430, 10), (500, 11), (600, 12),
    (750, 13), (900, 14), (1100, 15), (1300, 16), (1500, 17),
    (1750, 18), (2000, 19), (2250, 20), (2500, 21), (3000, 22),
    (3500, 23), (4000, 24),
]


def diff_to_imps(diff: int) -> int:
    """Signed point difference → signed IMPs (WBF/ACBL table)."""
    sign = 1 if diff >= 0 else -1
    d = abs(diff)
    imps = 0
    for threshold, val in _IMP_THRESHOLDS:
        if d >= threshold:
            imps = val
        else:
            break
    return sign * imps


def _hand_to_pbn(h):
    return ".".join("".join(r.abbr for r in suit)
                    for suit in (h.spades, h.hearts, h.diamonds, h.clubs))


# ---------- coaching prose (Baker-Bridge format) ----------

def _strip_post_auction_blocks(text: str) -> str:
    """For each board, drop the first {...} block that follows [Auction "..."].
    Leaves pre-auction comment blocks ({Shape ...} etc.) intact so endplay
    doesn't see new blank lines and treat them as board terminators."""
    out = []
    pos = 0
    pattern = re.compile(r'\[Auction\s+"[^"]*"\]')
    for m in pattern.finditer(text):
        out.append(text[pos:m.end()])
        tail_start = m.end()
        # Find the first { that follows; stop searching if we hit the next
        # [Event tag (start of a new board) first.
        next_event = text.find('\n[Event ', tail_start)
        open_pos = text.find('{', tail_start)
        if open_pos == -1 or (next_event != -1 and open_pos > next_event):
            pos = tail_start
            continue
        close_pos = text.find('}', open_pos)
        if close_pos == -1:
            pos = tail_start
            continue
        out.append(text[tail_start:open_pos])
        end_pos = close_pos + 1
        # If the block sits on its own line (Baker-Bridge style — newline
        # before `{`), consume one trailing newline after `}` so the strip
        # collapses cleanly. Without this, endplay sees a blank line and
        # terminates the board early. If `{` was inline with the auction
        # (no preceding newline), keep the trailing newline as the separator
        # between auction calls and the next `[Tag]`.
        preceded_by_newline = open_pos > 0 and text[open_pos - 1] == "\n"
        if preceded_by_newline and end_pos < len(text) and text[end_pos] == "\n":
            end_pos += 1
        pos = end_pos
    out.append(text[pos:])
    return "".join(out)


def _split_pbn_by_board(text: str) -> list[str]:
    """Split raw PBN text by [Event "..."] markers (each board starts there).
    Returns the per-board slices in source order. A file preamble before the
    first [Event tag is dropped — only slices that begin with [Event are kept."""
    parts = re.split(r'(?=^\[Event\s+")', text, flags=re.MULTILINE)
    return [p for p in parts if p.lstrip().startswith("[Event")]


def _auction_pbn_calls(auction) -> list[str]:
    """Render an auction as PBN-style call strings (1C, 2D, 3NT, X, XX, Pass)."""
    out = []
    for call in auction:
        if hasattr(call, "denom"):
            if call.denom == Denom.nt:
                out.append(f"{call.level}NT")
            else:
                out.append(f"{call.level}{DENOM_LETTER[call.denom]}")
        else:
            penalty = getattr(call, "penalty", None)
            pn = getattr(penalty, "name", None) if penalty is not None else None
            out.append({"passed": "Pass", "doubled": "X", "redoubled": "XX"}.get(pn, "Pass"))
    return out


_SHOW_RE = re.compile(r'\[show\s+([^\]]+)\]', re.IGNORECASE)
_BID_RE = re.compile(r'\[BID\s+([^\]]+)\]', re.IGNORECASE)
_POST_AUCTION_RE = re.compile(r'\[POST-AUCTION\]', re.IGNORECASE)
_ROLE_STAGE_RE = re.compile(
    r'\[ROLE\s+(declarer|leader|defender)\]\s*\[STAGE\s+(auction-end|pre-lead|post-lead|post-play)\]',
    re.IGNORECASE,
)
# Tip chunks live in the same {...} block as the bidding-tutorial chunks. They
# start at the first [ROLE ...] marker; everything before that is bidding prose.
_ROLE_MARKER_RE = re.compile(r'\[ROLE\s+', re.IGNORECASE)


def _extract_reveals(prose: str) -> tuple[list[str], str]:
    reveals = []
    def collect(m):
        reveals.append(m.group(1).strip())
        return ""
    cleaned = _SHOW_RE.sub(collect, prose)
    cleaned = re.sub(r'[ \t]+\n', '\n', cleaned).strip()
    return reveals, cleaned


# A [show S] reveal only exposes the student's own hand, so it's fine while the
# auction is still on screen. Any other reveal exposes a hand the student can't
# yet see (partner's dummy, opponents), so its prose is held back.
_SELF_SEATS = {"S"}


def _split_deferred_reveal(prose: str) -> tuple[str, str]:
    """Split a chunk's prose at the first [show ...] that reveals a hand beyond
    the student's own. Text up to that token stays in place (shown while its
    bid is on screen); the token and everything after it are deferred so they
    can be folded into the post-auction chunk, where the dummy is visible. This
    keeps bidding realistic — partner's hand isn't described until it's down."""
    for m in _SHOW_RE.finditer(prose):
        seats = {c for c in m.group(1).upper() if c in "NESW"}
        if not seats <= _SELF_SEATS:
            return prose[:m.start()], prose[m.start():]
    return prose, ""


_ACCEPT_RE = re.compile(r'\[ACCEPT\s+([^\]]+)\]', re.IGNORECASE)


def _extract_accept(prose: str) -> tuple[list[str], str]:
    """Pull [ACCEPT call ...] tokens from a bid chunk. These mark extra calls
    the quiz should treat as correct alongside the bid actually made — used for
    judgment decisions where more than one call is defensible (e.g. accept Pass
    or 3NT after 1NT-2NT with a middling hand). Returns (accepts, cleaned)."""
    accepts: list[str] = []
    def collect(m):
        accepts.extend(tok for tok in m.group(1).split() if tok)
        return ""
    cleaned = _ACCEPT_RE.sub(collect, prose)
    cleaned = re.sub(r'[ \t]+\n', '\n', cleaned).strip()
    return accepts, cleaned


def _substitute_suits(text: str) -> str:
    return (text.replace("\\S", "♠").replace("\\H", "♥")
                .replace("\\D", "♦").replace("\\C", "♣"))


def _split_bidding_and_tips(body: str) -> tuple[str, str]:
    """A coaching block may hold bid-anchored tutorial chunks followed by
    role/stage-anchored card-play tips. The tips section, if any, starts at
    the first [ROLE ...] marker. Returns (bidding_section, tips_section)."""
    m = _ROLE_MARKER_RE.search(body)
    if m is None:
        return body, ""
    return body[:m.start()], body[m.start():]


def parse_coaching(raw_pbn_text: str, auction_pbn_calls: list[str],
                   student_indices: set[int] | None = None) -> list[dict] | None:
    """Parse the post-auction { ... } coaching block out of a single board's
    raw PBN text. Returns None if no such block exists; otherwise an ordered
    list of {"bid_index": int|None, "reveals": list[str], "text": str}.

    student_indices are the auction positions belonging to the student. When a
    [BID X] call name is ambiguous (e.g. several Pass calls), the student's own
    call is preferred so judgment prose / [ACCEPT] tags anchor to the student's
    decision rather than an opponent's identical call.

    Pre-auction `{Shape ...} {HCP ...} {Losers ...}` comment-style tags from
    the existing bba files are ignored — we only look at the slice AFTER the
    [Auction "..."] tag."""
    auction_match = re.search(r'\[Auction\s+"[^"]*"\]', raw_pbn_text)
    if not auction_match:
        return None
    tail = raw_pbn_text[auction_match.end():]
    open_pos = tail.find('{')
    if open_pos == -1:
        return None
    close_pos = tail.find('}', open_pos)
    if close_pos == -1:
        return None
    body = _substitute_suits(tail[open_pos + 1:close_pos])
    body, _ = _split_bidding_and_tips(body)

    # Split out [POST-AUCTION] section if present — chunks here fire after the
    # auction has fully revealed (after the last bid is quizzed/animated),
    # before the awaitingPlay/Play button. Lets contract-summary prose follow
    # the student's final bid rather than spoiling it.
    post_m = _POST_AUCTION_RE.search(body)
    if post_m:
        post_body = body[post_m.end():]
        body = body[:post_m.start()]
    else:
        post_body = ""

    parts = _BID_RE.split(body)
    chunks: list[dict] = []
    # Prose introduced by a non-self [show] (partner/opponent reveal) is moved
    # out of the mid-auction chunks and folded into the post-auction chunk, so
    # a hand is only described once it's visible. See _split_deferred_reveal.
    deferred_parts: list[str] = []

    intro_mid, intro_deferred = _split_deferred_reveal(parts[0])
    if intro_deferred:
        deferred_parts.append(intro_deferred)
    intro_reveals, intro_text = _extract_reveals(intro_mid)
    if intro_text or intro_reveals:
        chunks.append({"bid_index": None, "reveals": intro_reveals, "text": intro_text})

    def _norm_call(s: str) -> str:
        # Treat [BID 1N] and [BID 1NT] as the same call so authors don't have
        # to know which spelling the auction normaliser uses.
        s = s.strip().upper()
        if re.fullmatch(r"\d+N", s):
            s += "T"
        return s

    used: set[int] = set()
    for i in range(1, len(parts), 2):
        bid_name = _norm_call(parts[i])
        prose = parts[i + 1] if i + 1 < len(parts) else ""
        mid_prose, deferred = _split_deferred_reveal(prose)
        if deferred:
            deferred_parts.append(deferred)
        accepts, mid_prose = _extract_accept(mid_prose)
        reveals, text = _extract_reveals(mid_prose)
        # Among unused indices matching this call name, prefer one belonging to
        # the student so ambiguous calls (Pass) anchor to the student's own.
        candidates = [j for j, call in enumerate(auction_pbn_calls)
                      if j not in used and _norm_call(call) == bid_name]
        if student_indices:
            student_cands = [j for j in candidates if j in student_indices]
            candidates = student_cands or candidates
        bid_idx = candidates[0] if candidates else None
        if bid_idx is None:
            # Degrade to the previous successfully-anchored chunk so the prose
            # still surfaces. If there's no previous chunk, fall back to intro.
            if chunks:
                merged = (chunks[-1]["text"] + "\n\n" + text).strip() if text else chunks[-1]["text"]
                chunks[-1]["text"] = merged
                chunks[-1]["reveals"].extend(reveals)
                if accepts:
                    chunks[-1].setdefault("accept", []).extend(accepts)
            else:
                chunks.append({"bid_index": None, "reveals": reveals, "text": text,
                               "accept": accepts})
        else:
            used.add(bid_idx)
            chunks.append({"bid_index": bid_idx, "reveals": reveals, "text": text,
                           "accept": accepts})

    # Deferred partner/opponent prose leads the post-auction chunk (introduce
    # the hand), followed by the authored [POST-AUCTION] body (the play plan).
    deferred_text = "\n\n".join(p.strip() for p in deferred_parts if p.strip())
    combined_post = "\n\n".join(s for s in (deferred_text, post_body.strip()) if s)
    if combined_post.strip():
        reveals, text = _extract_reveals(combined_post)
        if text or reveals:
            chunks.append({"bid_index": "post-auction", "reveals": reveals, "text": text})

    return chunks if chunks else None


# Extract the first specific card mention from a leader pre-lead tip — e.g.
# "Lead the ♥2" → (Denom.hearts, Rank.R2). Returns None if no card-shaped
# token can be found; callers fall back to DDS for the opening lead.
_LEAD_CARD_RE = re.compile(r'(?:^|\s|—)([♠♥♦♣])([2-9TJQKA])\b')
_DENOM_FROM_SYM = {"♠": Denom.spades, "♥": Denom.hearts, "♦": Denom.diamonds, "♣": Denom.clubs}

def extract_recommended_lead(tips: list[dict]) -> Card | None:
    """Find the textbook opening-lead card the leader pre-lead tip recommends.
    Convention: every leader pre-lead tip starts with 'Lead the ♥2' or similar,
    so the first ♠/♥/♦/♣<rank> token is the recommended card."""
    pre = next((t for t in tips if t.get("role") == "leader" and t.get("stage") == "pre-lead"), None)
    if pre is None:
        return None
    m = _LEAD_CARD_RE.search(pre.get("text", ""))
    if not m:
        return None
    sym, rank_ch = m.group(1), m.group(2)
    denom = _DENOM_FROM_SYM.get(sym)
    rank = RANK_FROM_CHAR.get(rank_ch)
    if denom is None or rank is None:
        return None
    return Card(suit=denom, rank=rank)


def parse_tips(raw_pbn_text: str) -> list[dict]:
    """Parse role/stage-anchored card-play tip chunks out of the post-auction
    {...} block. Returns [] if no [ROLE ...] markers are present.

    Each tip: {"role": "declarer"|"leader"|"defender",
               "stage": "auction-end"|"pre-lead"|"post-lead",
               "reveals": list[str],   # real-compass seat letters, usually unused
               "text": str}            # suit escapes already substituted

    Tips are written from a single role's vantage and only describe information
    the role can see at that stage — the offline generator enforces this; the
    parser does no information-leakage check."""
    auction_match = re.search(r'\[Auction\s+"[^"]*"\]', raw_pbn_text)
    if not auction_match:
        return []
    tail = raw_pbn_text[auction_match.end():]
    open_pos = tail.find('{')
    if open_pos == -1:
        return []
    close_pos = tail.find('}', open_pos)
    if close_pos == -1:
        return []
    body = _substitute_suits(tail[open_pos + 1:close_pos])
    _, tips_section = _split_bidding_and_tips(body)
    # [PLAY ...] interactive-quiz decisions live after the whole-hand tips in the
    # same block; cut them off so they don't leak into the last tip's prose
    # (parse_play_coaching reads them separately).
    pm = _PLAY_RE.search(tips_section)
    if pm:
        tips_section = tips_section[:pm.start()]
    if not tips_section:
        return []

    tips: list[dict] = []
    matches = list(_ROLE_STAGE_RE.finditer(tips_section))
    for i, m in enumerate(matches):
        role = m.group(1).lower()
        stage = m.group(2).lower()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(tips_section)
        prose = tips_section[start:end]
        reveals, text = _extract_reveals(prose)
        if text:
            tips.append({"role": role, "stage": stage, "reveals": reveals, "text": text})
    return tips


# A per-decision play-coaching marker: [PLAY <trick> <seat> <card>] anchors an
# interactive quiz to one student play (mirrors bidding's [BID]). [WHY] splits
# the prose shown BEFORE the play (withholds the answer) from the prose shown on
# success / 2nd-miss reveal. [ACCEPT <card> ...] lists co-correct cards. Suits
# are already substituted to symbols when this runs (see parse_play_coaching).
_PLAY_RE = re.compile(
    r'\[PLAY\s+(\d+)\s+([NESW])\s+([♠♥♦♣])\s*([2-9TJQKA])\]', re.IGNORECASE)
_WHY_RE = re.compile(r'\[WHY\]', re.IGNORECASE)
_PLAY_CARD_TOKEN_RE = re.compile(r'([♠♥♦♣])\s*([2-9TJQKA])', re.IGNORECASE)


def _card_dict(sym: str, rank_ch: str) -> dict:
    """A card as {suit: letter, rank: char} — the same shape the frontend gets
    in state.legal_moves, so it can compare a clicked card directly."""
    return {"suit": DENOM_LETTER[_DENOM_FROM_SYM[sym]], "rank": rank_ch.upper()}


def _extract_play_accepts(prose: str) -> tuple[list[dict], str]:
    """Pull [ACCEPT <card> ...] card tokens from a play chunk (co-correct cards),
    returning (cards, cleaned_prose)."""
    accepts: list[dict] = []
    def collect(m):
        for tok in _PLAY_CARD_TOKEN_RE.finditer(m.group(1)):
            accepts.append(_card_dict(tok.group(1), tok.group(2)))
        return ""
    cleaned = _ACCEPT_RE.sub(collect, prose)
    cleaned = re.sub(r'[ \t]+\n', '\n', cleaned).strip()
    return accepts, cleaned


def parse_play_coaching(raw_pbn_text: str) -> list[dict] | None:
    """Parse [PLAY ...] interactive play-quiz decisions from a board's
    post-auction { ... } block. Returns an ordered list of
    {"trick": int, "seat": str (real compass), "correct": {suit,rank},
     "accept": [{suit,rank}...], "present": str, "why": str}, or None if the
    board carries no [PLAY] markers (then play behaves exactly as before)."""
    auction_match = re.search(r'\[Auction\s+"[^"]*"\]', raw_pbn_text)
    if not auction_match:
        return None
    tail = raw_pbn_text[auction_match.end():]
    open_pos = tail.find('{')
    if open_pos == -1:
        return None
    close_pos = tail.find('}', open_pos)
    if close_pos == -1:
        return None
    body = _substitute_suits(tail[open_pos + 1:close_pos])
    matches = list(_PLAY_RE.finditer(body))
    if not matches:
        return None
    out: list[dict] = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        chunk = body[start:end]
        wm = _WHY_RE.search(chunk)
        present, why = (chunk[:wm.start()], chunk[wm.end():]) if wm else (chunk, "")
        accepts, present = _extract_play_accepts(present)
        out.append({
            "trick": int(m.group(1)),
            "seat": m.group(2).upper(),
            "correct": _card_dict(m.group(3), m.group(4)),
            "accept": accepts,
            "present": present.strip(),
            "why": why.strip(),
        })
    return out or None


# ---------- session state ----------

class Session:
    def __init__(self, board, role: str):
        self.board = board
        self.deal = board.deal
        parsed = parse_contract(board.info.get("Contract", "?"))
        self.level, self.trump = parsed
        self.dealer = Player(board.dealer) if board.dealer is not None else Player.north
        self.declarer = derive_declarer(board.auction, self.dealer, self.trump)
        self.dummy = partner_of(self.declarer)
        self.leader = left_of(self.declarer)
        self.deal.first = self.leader
        self.deal.trump = self.trump
        self.role = role
        self.tricks_needed = 6 + self.level

        if role == "declarer":
            # Curation guard: a declarer lesson must be declared by the student's
            # own side. Competitive auctions in the raw bba pool sometimes push
            # the final contract to the opponents (e.g. 3SX by East) — admitting
            # such a board would silently seat the student at the Student tag as
            # a *defender*, contradicting the role. Reject it here so it never
            # starts (start_session turns the ValueError into a 400). [#31]
            student_seat = LETTER_SEAT.get(board.info.get("Student", "S"), Player.south)
            if self.declarer not in (student_seat, partner_of(student_seat)):
                raise ValueError(
                    f"board {board.board_num}: declarer-role scenario but the "
                    f"contract is declared by {SEAT_LETTER[self.declarer]} "
                    f"(opponents of student {SEAT_LETTER[student_seat]}); a "
                    f"declarer lesson must be played by the student's side"
                )
            self.user_seats = {self.declarer, self.dummy}
            self._user_seat = self.declarer
        elif role == "leader":
            self.user_seats = {self.leader}
            self._user_seat = self.leader
        elif role == "defender":
            partner = partner_of(self.leader)
            self.user_seats = {partner}
            self._user_seat = partner
        else:
            raise ValueError(f"unknown role {role}")

        # Display rotation: relabel every seat so the user always appears as
        # South in the browser. Real compass seats (and the underlying deal)
        # are unchanged — DDS and the dealer engine still use real compass.
        self._rotation_shift = (int(Player.south) - int(self._user_seat)) % 4

        self.initial_hands = {
            Player.north: self.deal.north.copy(),
            Player.east:  self.deal.east.copy(),
            Player.south: self.deal.south.copy(),
            Player.west:  self.deal.west.copy(),
        }
        self.trick_history = []
        self.current_trick_plays = []
        self.cards_played_count = 0
        self.ns_tricks = 0
        self.ew_tricks = 0
        self.complete = False
        # Set by start_session after parsing the PBN's post-auction prose block.
        # None means the scenario file has no embedded tutorial — frontend then
        # uses the existing instant-reveal path.
        self.coaching: list[dict] | None = None
        # Card-play tips, already filtered to this session's role. Stages:
        # "auction-end" (all roles), "pre-lead" (leader only), "post-lead"
        # (declarer + defender). Empty when the PBN ships no [ROLE]/[STAGE]
        # markers — frontend just skips the tip-phase pause.
        self.tips: list[dict] = []
        # Interactive play-quiz decisions ([PLAY ...] markers), in author
        # real-compass frame. None when the board has no [PLAY] markers, in
        # which case play behaves exactly as before (manual, no quiz). The
        # cursor tracks how many decisions have been reached/consumed so the
        # auto-play-between-decisions walk knows what to stop at next.
        self.play_coaching: list[dict] | None = None
        self._play_cursor: int = 0
        # Textbook opening lead card, extracted from the leader pre-lead tip
        # at session start. When role != leader, auto_play_until_user uses
        # this in place of dds_pick for the opening lead, so the table
        # matches the prose tips (DDS otherwise picks a tactical lead that
        # might disagree with the textbook reasoning).
        self.recommended_lead: Card | None = None
        # Full log of cards played, in chronological order. We rebuild deal
        # state from scratch when undoing, since endplay's unplay() can't
        # cross trick boundaries.
        self.move_log: list = []
        # Each /play (and /claim) records the cards_played_count BEFORE its
        # action. Undo pops the top and replays from move_log up to that count.
        self.undo_stack = []

    def set_student_seat(self, student: Player):
        """Override the user's seat (typically Player.south for coached
        scenarios). Recomputes user_seats so the user controls whichever
        side the student is on — declaring side if student is declarer or
        dummy, otherwise just the student's seat (defender). The rotation
        is also recomputed so the student appears at display south."""
        self._user_seat = student
        self._rotation_shift = (int(Player.south) - int(student)) % 4
        if student in (self.declarer, self.dummy):
            self.user_seats = {self.declarer, self.dummy}
        else:
            self.user_seats = {student}

    def _dd_tricks_for_declarer(self) -> int:
        """Double-dummy tricks for the contract's declarer/strain on the
        original (pre-play) layout. Cached after first call."""
        if not hasattr(self, "_dd_cache"):
            pbn_str = "N:" + " ".join(
                _hand_to_pbn(self.initial_hands[p])
                for p in (Player.north, Player.east, Player.south, Player.west)
            )
            pristine = Deal(pbn_str)
            pristine.trump = self.trump
            self._dd_cache = calc_dd_table(pristine)
        return self._dd_cache[self.trump, self.declarer]

    def _scoring(self, decl_tricks: int):
        """Return (actual_score, dd_tricks, dd_score, imps_vs_dd), all from
        the STUDENT's perspective (sign flipped when student is defending)."""
        vul = Vul(int(getattr(self.board, "vul", 0) or 0))
        # Trainer doesn't track doubles; treat all contracts as undoubled.
        c = Contract(level=self.level, denom=self.trump,
                     declarer=self.declarer, penalty=Penalty.passed)
        c.result = decl_tricks - self.tricks_needed
        actual_decl = c.score(vul)
        dd_tricks = self._dd_tricks_for_declarer()
        c.result = dd_tricks - self.tricks_needed
        dd_decl = c.score(vul)
        student_is_declarer = self.role == "declarer"
        actual = actual_decl if student_is_declarer else -actual_decl
        dd = dd_decl if student_is_declarer else -dd_decl
        return actual, dd_tricks, dd, diff_to_imps(actual - dd)

    def max_tricks_remaining_for_user_side(self) -> int:
        """Double-dummy max tricks the student's side can take from the
        current deal state (counting any partially-played trick as 1 of the
        remaining)."""
        sb = solve_board(self.deal)
        # solve_board returns {Card: tricks-for-side-to-play-from-here}
        best_for_to_play = max(t for _, t in sb)
        to_play = self.deal.curplayer
        decl_ns = self.declarer in (Player.north, Player.south)
        to_play_ns = to_play in (Player.north, Player.south)
        remaining = 13 - len(self.trick_history)
        if to_play_ns == decl_ns:
            decl_remaining = best_for_to_play
        else:
            decl_remaining = remaining - best_for_to_play
        if self.role == "declarer":
            return decl_remaining
        return remaining - decl_remaining

    def _rotate_seat(self, seat: Player) -> Player:
        return Player((int(seat) + self._rotation_shift) % 4)

    def _rl(self, letter: str) -> str:
        """Rotate a seat letter into the user's South-at-bottom display frame."""
        return SEAT_LETTER[self._rotate_seat(LETTER_SEAT[letter])]

    def visible_hands(self):
        # Real-bridge order: dummy is tabled only AFTER the opening lead has
        # been played. Pre-lead the student sees only their own hand; after
        # the lead they also see dummy (and, if they're on the declaring
        # side, also the partner's hand they're playing).
        if self.cards_played_count == 0:
            return {self._user_seat}
        return self.user_seats | {self.dummy}

    def cards_played_by_seat(self):
        by_seat = {p: [] for p in (Player.north, Player.east, Player.south, Player.west)}
        for t in self.trick_history:
            for p in t["plays"]:
                by_seat[LETTER_SEAT[p["seat"]]].append(p["card"])
        for p in self.current_trick_plays:
            by_seat[LETTER_SEAT[p["seat"]]].append(p["card"])
        return {SEAT_LETTER[p]: cards for p, cards in by_seat.items()}

    def known_defender_table(self):
        """Declarer-frame KNOWN facts about the two defenders, derived ONLY from
        public information — the cards each has already played, plus dummy and
        declarer's own hand. Never consults the defenders' unplayed cards, so it
        cannot 'cheat'. Seat letters are rotated to the display frame by
        _rotate_state_for_user. Returns None unless the student is declarer."""
        if self.role != "declarer":
            return None
        decl, dummy = self.declarer, self.dummy
        lho, rho = left_of(decl), left_of(dummy)   # the two defenders
        dummy_seen = dummy in self.visible_hands()
        defence_hcp = (40 - hand_hcp(self.initial_hands[decl])
                       - hand_hcp(self.initial_hands[dummy])) if dummy_seen else None
        HCP = {"A": 4, "K": 3, "Q": 2, "J": 1}
        sym2letter = {sym: DENOM_LETTER[d] for d, sym in DENOM_SYM.items()}
        played = self.cards_played_by_seat()        # {seat_letter: [display cards]}
        defender_letters = {SEAT_LETTER[lho], SEAT_LETTER[rho]}
        shown_out = {sl: set() for sl in defender_letters}
        tricks = list(self.trick_history)
        if self.current_trick_plays:
            tricks.append({"plays": self.current_trick_plays})
        for t in tricks:
            plays = t["plays"]
            if not plays:
                continue
            led = sym2letter.get(plays[0]["card"][0])
            for p in plays[1:]:                     # the leader can't 'show out'
                sl = p["seat"]
                if sl in defender_letters and sym2letter.get(p["card"][0]) != led:
                    shown_out[sl].add(led)
        def defender(seat):
            sl = SEAT_LETTER[seat]
            cards = played.get(sl, [])
            suits = {}
            for L in ("S", "H", "D", "C"):
                cnt = sum(1 for c in cards if sym2letter.get(c[0]) == L)
                known = L in shown_out[sl]          # voided -> original length fixed
                suits[L] = {"played": cnt, "length": cnt if known else None}
            return {"seat": sl,
                    "hcp_shown": sum(HCP.get(c[1], 0) for c in cards),
                    "cards_left": 13 - len(cards),
                    "suits": suits}
        return {"available": dummy_seen, "defence_hcp": defence_hcp,
                "defenders": {"lho": defender(lho), "rho": defender(rho)}}

    def state(self):
        visible = self.visible_hands()
        hands = {}
        for p in (Player.north, Player.east, Player.south, Player.west):
            if p in visible:
                hands[SEAT_LETTER[p]] = hand_to_dict(hand_of(self.deal, p))
            else:
                hands[SEAT_LETTER[p]] = None

        current_to_play = self.deal.curplayer if not self.complete else None
        legal = []
        if current_to_play is not None and current_to_play in self.user_seats and not self.complete:
            legal = [{"suit": DENOM_LETTER[c.suit], "rank": c.rank.abbr, "display": card_to_display(c)}
                     for c in self.deal.legal_moves()]

        st = {
            "level": self.level,
            "strain": DENOM_LETTER[self.trump],
            "strain_symbol": DENOM_SYM[self.trump],
            "declarer": SEAT_LETTER[self.declarer],
            "dummy": SEAT_LETTER[self.dummy],
            "leader": SEAT_LETTER[self.leader],
            "dealer": SEAT_LETTER[self.dealer],
            "role": self.role,
            "trick_number": 13 if self.complete else len(self.trick_history) + 1,
            "tricks_taken": {"NS": self.ns_tricks, "EW": self.ew_tricks},
            "tricks_needed": self.tricks_needed,
            "hands": hands,
            "cards_played_by_seat": self.cards_played_by_seat(),
            "current_trick": [
                {"seat": p["seat"], "card": p["card"]}
                for p in self.current_trick_plays
            ],
            # Deep-copy so _rotate_state_for_user can mutate without corrupting
            # the session's stored history (it would re-rotate on every call).
            "trick_history": [
                {**t, "plays": [{"seat": p["seat"], "card": p["card"]} for p in t["plays"]]}
                for t in self.trick_history
            ],
            "to_play": SEAT_LETTER[current_to_play] if current_to_play is not None else None,
            "user_to_play": current_to_play in self.user_seats if current_to_play is not None else False,
            "legal_moves": legal,
            "complete": self.complete,
            "can_undo": len(self.undo_stack) > 0,
        }
        if self.complete:
            decl_tricks = self.ns_tricks if self.declarer in (Player.north, Player.south) else self.ew_tricks
            off = decl_tricks - self.tricks_needed
            actual_score, dd_tricks, dd_score, imps = self._scoring(decl_tricks)
            st["result"] = {
                "declarer_tricks": decl_tricks,
                "result_offset": off,
                "result_str": "=" if off == 0 else (f"+{off}" if off > 0 else str(off)),
                "score": actual_score,
                "dd_tricks": dd_tricks,
                "dd_score": dd_score,
                "imps_vs_dd": imps,
                "all_hands": {SEAT_LETTER[p]: hand_to_dict(self.initial_hands[p])
                              for p in self.initial_hands},
                "all_hcp": {SEAT_LETTER[p]: hand_hcp(self.initial_hands[p])
                            for p in self.initial_hands},
            }
        st["opponent_table"] = self.known_defender_table()
        st["auction"] = auction_dict(self.board.auction, self.dealer)
        st["contract_str"] = f"{self.level}{DENOM_SYM[self.trump]} by {SEAT_LETTER[self.declarer]}"
        st["board_num"] = self.board.board_num
        st["scenario"] = self.board.info.get("Event", "?")
        # Coaching chunks stay in the author's real-compass frame; the
        # frontend uses rotation_shift to map [show N] → display seat.
        st["coaching"] = self.coaching
        st["tips"] = self.tips
        # Play-quiz decisions stay in author real-compass frame too; only each
        # decision's `seat` is rotated to the display frame below (the cards are
        # frame-invariant). Shallow-copy each dict so the rotation doesn't mutate
        # the session's stored real-compass seats.
        st["play_coaching"] = ([dict(d) for d in self.play_coaching]
                               if self.play_coaching else None)
        st["play_cursor"] = self._play_cursor
        st["rotation_shift"] = self._rotation_shift
        # All four initial hands — only consulted by the frontend during the
        # tutorial phase, where the [show X] directives need to reveal hands
        # the server's visible_hands() would otherwise hide.
        st["initial_hands"] = {
            SEAT_LETTER[p]: hand_to_dict(self.initial_hands[p])
            for p in (Player.north, Player.east, Player.south, Player.west)
        }
        return self._rotate_state_for_user(st)

    def _rotate_state_for_user(self, st):
        R = self._rl

        def rotate_keys(d):
            return {R(k): v for k, v in d.items()}

        for k in ("declarer", "dummy", "leader", "dealer"):
            if st.get(k):
                st[k] = R(st[k])
        if st.get("to_play"):
            st["to_play"] = R(st["to_play"])

        st["hands"] = rotate_keys(st["hands"])
        st["cards_played_by_seat"] = rotate_keys(st["cards_played_by_seat"])
        if "initial_hands" in st:
            st["initial_hands"] = rotate_keys(st["initial_hands"])

        for play in st.get("current_trick", []):
            play["seat"] = R(play["seat"])
        for trick in st.get("trick_history", []):
            trick["leader"] = R(trick["leader"])
            trick["winner"] = R(trick["winner"])
            for play in trick["plays"]:
                play["seat"] = R(play["seat"])
        for call in st.get("auction", []):
            call["seat"] = R(call["seat"])

        # Play decisions: rotate only the acting seat (cards are frame-invariant).
        for dec in (st.get("play_coaching") or []):
            dec["seat"] = R(dec["seat"])

        ot = st.get("opponent_table")
        if ot and ot.get("defenders"):
            for d in ot["defenders"].values():
                if d and d.get("seat"):
                    d["seat"] = R(d["seat"])

        # Present trick totals as user-pair = NS in displayed frame.
        decl_total = self.ns_tricks if self.declarer in (Player.north, Player.south) else self.ew_tricks
        opp_total = (self.ns_tricks + self.ew_tricks) - decl_total
        if self.role == "declarer":
            st["tricks_taken"] = {"NS": decl_total, "EW": opp_total}
        else:
            st["tricks_taken"] = {"NS": opp_total, "EW": decl_total}

        if "result" in st:
            st["result"]["all_hands"] = rotate_keys(st["result"]["all_hands"])
            st["result"]["all_hcp"] = rotate_keys(st["result"]["all_hcp"])

        parts = st.get("contract_str", "").rsplit(" by ", 1)
        if len(parts) == 2:
            st["contract_str"] = f"{parts[0]} by {R(parts[1])}"
        return st

    def play_user_card(self, suit_letter: str, rank_letter: str):
        if self.complete:
            raise HTTPException(400, "deal complete")
        if self.deal.curplayer not in self.user_seats:
            raise HTTPException(400, f"not your turn — {SEAT_LETTER[self.deal.curplayer]} to play")
        suit = SUIT_FROM_CHAR.get(suit_letter.upper())
        rank = RANK_FROM_CHAR.get(rank_letter.upper())
        if suit is None or rank is None:
            raise HTTPException(400, f"bad card {suit_letter}{rank_letter}")
        legal = list(self.deal.legal_moves())
        match = next((c for c in legal if c.suit == suit and c.rank == rank), None)
        if match is None:
            raise HTTPException(400, "card is not a legal move")
        self._play_card(match)

    def _auto_card(self):
        """The card the trainer auto-plays at the current position: the textbook
        opening lead on trick 1 (so the table matches the pre-lead prose), else
        DDS's best card."""
        if (self.cards_played_count == 0
                and self.deal.curplayer == self.leader
                and self.recommended_lead is not None
                and self.recommended_lead in list(self.deal.legal_moves())):
            return self.recommended_lead
        return dds_pick(self.deal)

    def auto_play_until_user(self):
        """After a user play, run DDS for any defender/dummy-seat-the-computer-controls turns
        until it's user's turn again, or the deal completes."""
        while not self.complete and self.deal.curplayer not in self.user_seats:
            self._play_card(self._auto_card())

    def _decision_card_legal(self, dec) -> bool:
        suit = SUIT_FROM_CHAR.get(dec["correct"]["suit"])
        rank = RANK_FROM_CHAR.get(dec["correct"]["rank"])
        return any(c.suit == suit and c.rank == rank for c in self.deal.legal_moves())

    def auto_play_until_decision(self):
        """Quiz mode (boards with [PLAY] markers). Auto-play EVERY seat — including
        the student's declarer and dummy — until the next authored decision's
        position is reached (its trick + seat, with its correct card legal), then
        stop so the frontend can quiz it. The student only acts at decisions; the
        routine cards in between play themselves, exactly as the auction animates
        between the student's bids. A decision the board never reaches (divergence
        from the authored line) is skipped — graceful degradation, like an
        unmatched [BID]. Returns the pending decision dict, or None when the deal
        has been played to completion with no further decision."""
        decisions = self.play_coaching or []
        while self._play_cursor < len(decisions):
            dec = decisions[self._play_cursor]
            target = LETTER_SEAT[dec["seat"]]
            while not self.complete:
                cur_trick = len(self.trick_history) + 1
                if cur_trick > dec["trick"]:
                    break  # overshot this decision (resolved or diverged) → skip
                if cur_trick == dec["trick"] and self.deal.curplayer == target:
                    if self._decision_card_legal(dec):
                        return dec
                    break  # at the seat but correct card not legal → diverged
                self._play_card(self._auto_card())
            self._play_cursor += 1
        while not self.complete:          # no more decisions: play it out
            self._play_card(self._auto_card())
        return None

    def _apply_card(self, card):
        """Advance the deal + bookkeeping by one card. Does NOT touch move_log."""
        seat = self.deal.curplayer
        self.current_trick_plays.append({"seat": SEAT_LETTER[seat], "card": card_to_display(card)})
        self.deal.play(card)
        self.cards_played_count += 1
        if len(self.current_trick_plays) == 4:
            winner = self.deal.curplayer
            if winner in (Player.north, Player.south):
                self.ns_tricks += 1
            else:
                self.ew_tricks += 1
            trick_n = len(self.trick_history) + 1
            self.trick_history.append({
                "n": trick_n,
                "leader": self.current_trick_plays[0]["seat"],
                "plays": list(self.current_trick_plays),
                "winner": SEAT_LETTER[winner],
            })
            self.current_trick_plays = []
            if trick_n == 13:
                self.complete = True

    def _play_card(self, card):
        """Record card in the move log and apply it. Used by all play paths."""
        self.move_log.append(card)
        self._apply_card(card)

    def _rebuild_to(self, target: int):
        """Reset the deal to its initial state, then replay move_log[:target]."""
        new_deal = Deal()
        for seat in (Player.north, Player.east, Player.south, Player.west):
            new_deal[seat] = str(self.initial_hands[seat])
        new_deal.first = self.leader
        new_deal.trump = self.trump
        self.deal = new_deal
        kept = self.move_log[:target]
        self.move_log = []
        self.trick_history = []
        self.current_trick_plays = []
        self.cards_played_count = 0
        self.ns_tricks = 0
        self.ew_tricks = 0
        self.complete = False
        for c in kept:
            self.move_log.append(c)
            self._apply_card(c)

    def undo_to_checkpoint(self):
        """Pop the most recent /play (or /claim) checkpoint and rebuild to it.
        Returns True if any cards were undone."""
        if not self.undo_stack:
            return False
        target = self.undo_stack.pop()
        if target >= self.cards_played_count:
            return False
        self._rebuild_to(target)
        return True


# ---------- API ----------

app = FastAPI()
SESSIONS: dict[str, Session] = {}


@app.get("/api/scenarios")
def list_scenarios():
    # Only scenarios that have an embedded-coaching file are user-pickable
    # (curated lessons and hand-authored coaching both count).
    files = sorted({p.stem for d in (CURATED_DIR, COACHING_DIR)
                    for p in d.glob("*.pbn") if not p.stem.startswith("-")})
    return {"scenarios": files}


LAYOUT_PATHS = [
    REPO_ROOT / "btn" / "-button-layout-release.txt",
    REPO_ROOT / "btn" / "-button-layout-beta.txt",
]


def parse_layout(text: str):
    """Parse the .btn/-button-layout-*.txt format into ordered sections.
    Returns: list of {"title": str, "scenarios": [str, ...]} in source order."""
    import re
    sections = []
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"\[Section\]\s*(.+)$", line)
        if m:
            current = {"title": m.group(1).strip(), "scenarios": []}
            sections.append(current)
            continue
        if line.startswith("[Major]") or line.startswith("[Action]"):
            continue
        if line.startswith("---"):
            continue
        if current is None:
            continue
        # Scenario line — possibly with parenthesized groups, :color suffixes, --- as placeholder.
        # Collapse parens, then split by commas.
        flat = line.replace("(", "").replace(")", "")
        for part in flat.split(","):
            tok = part.strip()
            if not tok or tok == "---":
                continue
            # Strip :color or :width suffix
            tok = tok.split(":", 1)[0].strip()
            if tok and tok not in current["scenarios"]:
                current["scenarios"].append(tok)
    return sections


@app.get("/api/menu")
def get_menu():
    layout_path = next((p for p in LAYOUT_PATHS if p.exists()), None)
    # Only scenarios with embedded coaching are user-pickable; sections that
    # have zero coached scenarios drop out entirely. Curated lessons
    # (coaching-curated/) and hand-authored coaching/ both qualify.
    available = {p.stem for d in (CURATED_DIR, COACHING_DIR)
                 for p in d.glob("*.pbn") if not p.stem.startswith("-")}
    if layout_path is None:
        # Fallback: flat alphabetical
        return {"sections": [{"title": "Scenarios", "scenarios": sorted(available)}]}
    sections = parse_layout(layout_path.read_text())
    out = []
    for sec in sections:
        scenarios = [s for s in sec["scenarios"] if s in available]
        if scenarios:
            out.append({"title": sec["title"], "scenarios": scenarios})
    return {"sections": out}


class StartSessionBody(BaseModel):
    scenario: str
    board_index: int = 0
    role: str = "declarer"
    # When true, the student is seated randomly among the bidding partnership's
    # seats (each board), so a bidding lesson is faced from both opener and
    # responder. Coaching pronoun tokens (@S/@s/@your/@v{}) render per seat.
    randomly_rotate: bool = False


@app.post("/api/session")
def start_session(body: StartSessionBody):
    path = _scenario_pbn_path(body.scenario)
    if path is None:
        raise HTTPException(404, f"scenario not found: {body.scenario}")
    raw_text = path.read_text()
    # endplay's PBN parser chokes on inline {...} prose blocks after [Auction]
    # — it tries to parse "{[show" as a bid call. Strip those post-auction
    # blocks before handing off to endplay. We leave pre-auction comments
    # (e.g. {Shape ...} {HCP ...} {Losers ...} between [Deal] and [Declarer]
    # in the existing bba files) alone — replacing them with empty strings
    # introduces blank lines that endplay would treat as board terminators.
    endplay_text = _strip_post_auction_blocks(raw_text)
    boards = list(pbn.load(io.StringIO(endplay_text)))
    if not boards:
        raise HTTPException(500, "scenario has no deals")
    idx = body.board_index % len(boards)
    try:
        sess = Session(boards[idx], role=body.role)
    except ValueError as e:
        raise HTTPException(400, str(e))
    board_slices = _split_pbn_by_board(raw_text)
    # Decide which seat the student occupies. Default: the PBN's Student tag
    # (convention S). With randomly_rotate, pick randomly among the bidding
    # partnership's seats that made a non-pass call, so the student faces the
    # decision from either seat across boards.
    student_letter = boards[idx].info.get("Student", "S")
    student_seat = LETTER_SEAT.get(student_letter, Player.south)
    # Randomly Rotate only applies to ROTATION-READY coaching — i.e. prose that
    # uses the @-pronoun tokens so it reads correctly from either seat. On
    # hand-authored coaching (no tokens) the prose hardcodes "you" = the
    # authored seat, so reseating the student would garble it; leave such
    # scenarios on their authored seat.
    rotation_ready = idx < len(board_slices) and ('@S' in board_slices[idx]
                        or '@s' in board_slices[idx] or '@v(' in board_slices[idx])
    if body.randomly_rotate and rotation_ready:
        calls = _auction_pbn_calls(boards[idx].auction)
        acted = set()
        for j, call in enumerate(calls):
            s = seat_at_auction_index(sess.dealer, j)
            if call.upper() != "PASS" and s in (student_seat, partner_of(student_seat)):
                acted.add(s)
        choices = sorted(acted, key=int) or [student_seat]
        student_seat = random.choice(choices)
    if idx < len(board_slices):
        # Auction positions belonging to the student, so ambiguous [BID Pass]
        # chunks anchor to the student's own call.
        seat = sess.dealer
        student_indices = set()
        for j in range(len(boards[idx].auction)):
            if seat == student_seat:
                student_indices.add(j)
            seat = left_of(seat)
        sess.coaching = parse_coaching(
            board_slices[idx], _auction_pbn_calls(boards[idx].auction),
            student_indices=student_indices,
        )
        all_tips = parse_tips(board_slices[idx])
        sess.tips = [t for t in all_tips if t["role"] == sess.role]
        # Read the recommended lead from the leader pre-lead tip across ALL
        # roles' tips (not just the user's) — the user might be playing
        # declarer or defender but we still need the leader's textbook card.
        sess.recommended_lead = extract_recommended_lead(all_tips)
        # Interactive play-quiz decisions — declarer role only for now (the
        # student controls declarer + dummy, the seats the [PLAY] decisions
        # anchor to). Other roles play normally.
        if sess.role == "declarer":
            sess.play_coaching = parse_play_coaching(board_slices[idx])
    # When the scenario ships with embedded coaching, the bidding tutorial
    # addresses the student as "you" — convention is Student=S (real). Override
    # the role-derived seat so south sits at the bottom of the table and the
    # user controls whichever side south ends up on. Only meaningful when the
    # user is playing the student's role (declarer in these scenarios); for
    # role=leader/defender we keep the user at their chosen seat and suppress
    # the bidding tutorial (which is student-addressed) so it doesn't read as
    # "you bid 1H" to someone who didn't bid. Card-play tips are role-filtered
    # on the server and still flow.
    if sess.coaching is not None:
        if sess.role == "declarer":
            sess.set_student_seat(student_seat)
            # Render pronoun tokens per chunk: a [BID] chunk authored in the
            # second person reads "you" when the student made that call, else
            # "your partner". Intro/reflection chunks are seat-neutral (no
            # tokens) and pass through unchanged.
            for ch in sess.coaching:
                bi = ch.get("bid_index")
                is_student = (isinstance(bi, int)
                              and seat_at_auction_index(sess.dealer, bi) == student_seat)
                if not isinstance(bi, int):
                    is_student = True  # neutral chunks: no-op fill
                ch["text"] = fill_pronouns(ch.get("text", ""), is_student)
        else:
            sess.coaching = None
    # NOTE: deliberately NOT calling sess.auto_play_until_user() here. The
    # client calls /start-play once the user clicks the Play button so any
    # end-of-auction coaching has a chance to fire while cards_played_count
    # is still 0. /start-play is idempotent (no-op when it's already the
    # user's turn).
    sid = secrets.token_urlsafe(12)
    SESSIONS[sid] = sess
    return {"session_id": sid, "state": sess.state(), "board_index": idx}


@app.get("/api/session/{sid}")
def get_state(sid: str):
    sess = SESSIONS.get(sid)
    if sess is None:
        raise HTTPException(404, "session not found")
    return {"state": sess.state()}


class PlayBody(BaseModel):
    suit: str
    rank: str


@app.post("/api/session/{sid}/play")
def play(sid: str, body: PlayBody):
    sess = SESSIONS.get(sid)
    if sess is None:
        raise HTTPException(404, "session not found")
    sess.undo_stack.append(sess.cards_played_count)  # checkpoint BEFORE user play
    sess.play_user_card(body.suit, body.rank)
    sess.auto_play_until_user()
    return {"state": sess.state()}


@app.post("/api/session/{sid}/start-play")
def start_play(sid: str):
    """Called by the client when the user clicks Play in the auction overlay.
    Auto-plays any non-user seats (e.g., LHO's opening lead) until it's the
    user's turn. Idempotent — no-op when user is already on play."""
    sess = SESSIONS.get(sid)
    if sess is None:
        raise HTTPException(404, "session not found")
    sess.auto_play_until_user()
    return {"state": sess.state()}


@app.post("/api/session/{sid}/advance-to-decision")
def advance_to_decision(sid: str):
    """Quiz mode (boards with [PLAY] markers): auto-play every seat to the next
    authored play decision and stop. Returns the new state plus pending_decision
    = the index into state.play_coaching the frontend should now quiz, or null
    when the deal was played out with no further decision."""
    sess = SESSIONS.get(sid)
    if sess is None:
        raise HTTPException(404, "session not found")
    dec = sess.auto_play_until_decision()
    pending = sess._play_cursor if dec is not None else None
    return {"state": sess.state(), "pending_decision": pending}


@app.post("/api/session/{sid}/undo")
def undo(sid: str):
    sess = SESSIONS.get(sid)
    if sess is None:
        raise HTTPException(404, "session not found")
    sess.undo_to_checkpoint()
    return {"state": sess.state()}


@app.post("/api/session/{sid}/replay")
def replay(sid: str):
    sess = SESSIONS.get(sid)
    if sess is None:
        raise HTTPException(404, "session not found")
    sess._rebuild_to(0)
    sess.undo_stack = []
    sess.auto_play_until_user()  # re-do the opening lead
    return {"state": sess.state()}


class ClaimBody(BaseModel):
    count: int


@app.post("/api/session/{sid}/claim")
def claim(sid: str, body: ClaimBody):
    sess = SESSIONS.get(sid)
    if sess is None:
        raise HTTPException(404, "session not found")
    if sess.complete:
        raise HTTPException(400, "deal already complete")
    n = body.count
    remaining = 13 - len(sess.trick_history)
    if n < 1 or n > remaining:
        raise HTTPException(400, f"Claim must be between 1 and {remaining} (tricks remaining).")
    # Validate against double-dummy: can your side actually take that many?
    max_for_us = sess.max_tricks_remaining_for_user_side()
    if n > max_for_us:
        plural = "s" if max_for_us != 1 else ""
        raise HTTPException(
            400,
            f"Claim of {n} rejected — from this position, the best your side can do is {max_for_us} more trick{plural}."
        )
    # Accept: play out optimally for both sides. Checkpoint so the user can
    # undo the claim if they want to keep playing.
    sess.undo_stack.append(sess.cards_played_count)
    while not sess.complete:
        card = dds_pick(sess.deal)
        sess._play_card(card)
    return {"state": sess.state()}


@app.delete("/api/session/{sid}")
def end_session(sid: str):
    SESSIONS.pop(sid, None)
    return {"ok": True}


# ---------- user feedback → GitHub issue ----------

# Abuse limits — a feedback note is untrusted input from anyone who can reach
# the app. Cap its length, neutralize @mentions / #refs (so a note can't ping
# GitHub users or cross-link issues), and rate-limit filing across the server.
MAX_NOTE_LEN = 2000
REPORT_RATE_MAX = 20        # at most this many issues ...
REPORT_RATE_WINDOW = 3600   # ... filed per this many seconds (rolling window)
_REPORT_TIMES: list[float] = []
_SIGIL_RE = re.compile(r"([@#])(?=\w)")


def _sanitize_note(note: str) -> str:
    note = (note or "").strip()
    if len(note) > MAX_NOTE_LEN:
        note = note[:MAX_NOTE_LEN].rstrip() + " … (truncated)"
    # A zero-width space after @ or # stops GitHub auto-linking the token (no
    # notification, no cross-ref) while staying invisible in the rendered text.
    note = _SIGIL_RE.sub(lambda m: m.group(1) + "\u200b", note)
    return note or "_(no note provided)_"


def _rate_limited() -> bool:
    """True once REPORT_RATE_MAX issues have been filed within the rolling
    window. Records the timestamp only when it allows the call."""
    now = time.monotonic()
    cutoff = now - REPORT_RATE_WINDOW
    while _REPORT_TIMES and _REPORT_TIMES[0] < cutoff:
        _REPORT_TIMES.pop(0)
    if len(_REPORT_TIMES) >= REPORT_RATE_MAX:
        return True
    _REPORT_TIMES.append(now)
    return False


class ReportBody(BaseModel):
    note: str = ""


def _deal_pbn(sess) -> str:
    """The board's four hands in the author's real-compass frame, so the issue
    points at the exact deal in coaching-curated/<scenario>.pbn."""
    order = (Player.north, Player.east, Player.south, Player.west)
    return " ".join(f"{SEAT_LETTER[p]}:{sess.initial_hands[p]}" for p in order)


def _build_issue(sess, note: str):
    """Compose a GitHub issue (title, body) from the live session. Real-compass
    frame throughout — frame-stable and matches the source PBN."""
    scenario = sess.board.info.get("Event", "?")
    board_num = sess.board.board_num
    role = sess.role
    contract = f"{sess.level}{DENOM_SYM[sess.trump]} by {SEAT_LETTER[sess.declarer]}"
    auction = " ".join(c["call"] for c in auction_dict(sess.board.auction, sess.dealer)) or "(none)"
    if sess.complete:
        phase = "deal complete"
    elif sess.cards_played_count == 0:
        phase = "auction / pre-play"
    else:
        phase = f"playing — trick {len(sess.trick_history) + 1}"
    note_clean = _sanitize_note(note)
    title = f"Feedback: {scenario} · board {board_num} · {role}"
    body = (
        f"**User says:** {note_clean}\n\n"
        f"---\n"
        f"*auto-captured:*\n"
        f"- Scenario: **{scenario}** · Board: **{board_num}** · Role: **{role}**\n"
        f"- Contract: {contract} · {phase}\n"
        f"- Auction: {auction}\n"
        f"- Deal (PBN): `{_deal_pbn(sess)}`\n"
    )
    return title, body


@app.post("/api/session/{sid}/report")
async def report_feedback(sid: str, body: ReportBody):
    sess = SESSIONS.get(sid)
    if sess is None:
        raise HTTPException(404, "session not found")
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        # Feature isn't wired up yet — say so plainly rather than a 500.
        raise HTTPException(503, "Feedback isn't configured yet (GITHUB_TOKEN is unset).")
    if _rate_limited():
        raise HTTPException(429, "Thanks — lots of reports have just come in. Please try again in a little while.")
    title, issue_body = _build_issue(sess, body.note)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Best-effort: make sure the label exists (422 if it already does — fine).
            await client.post(
                f"https://api.github.com/repos/{GITHUB_REPO}/labels",
                json={"name": "user-feedback", "color": "0e8a16",
                      "description": "Reported from inside the Play Trainer"},
                headers=headers,
            )
            resp = await client.post(
                f"https://api.github.com/repos/{GITHUB_REPO}/issues",
                json={"title": title, "body": issue_body, "labels": ["user-feedback"]},
                headers=headers,
            )
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Couldn't reach GitHub: {e}")
    if resp.status_code >= 300:
        raise HTTPException(502, f"GitHub rejected the report ({resp.status_code}): {resp.text[:300]}")
    return {"ok": True, "issue_url": resp.json().get("html_url")}


# ---------- static files ----------

@app.get("/")
def root():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
