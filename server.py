"""
Bridge Play Trainer — FastAPI backend.

Start with:
    uvicorn bridge-play-trainer.server:app --reload --port 8765

Then open http://localhost:8765/ in your browser.

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

APP_DIR = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("BRIDGE_DATA_ROOT", "/Users/adavidbailey/Practice-Bidding-Scenarios"))
REPO_ROOT = DATA_ROOT
BBA_DIR = DATA_ROOT / "bba"
COACHING_DIR = DATA_ROOT / "coaching"
STATIC_DIR = APP_DIR / "static"


def _scenario_pbn_path(scenario: str) -> Path | None:
    """Return the coaching/ file if it exists, else fall back to bba/."""
    coached = COACHING_DIR / f"{scenario}.pbn"
    if coached.exists():
        return coached
    raw = BBA_DIR / f"{scenario}.pbn"
    if raw.exists():
        return raw
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
        st["auction"] = auction_dict(self.board.auction, self.dealer)
        st["contract_str"] = f"{self.level}{DENOM_SYM[self.trump]} by {SEAT_LETTER[self.declarer]}"
        st["board_num"] = self.board.board_num
        st["scenario"] = self.board.info.get("Event", "?")
        # Coaching chunks stay in the author's real-compass frame; the
        # frontend uses rotation_shift to map [show N] → display seat.
        st["coaching"] = self.coaching
        st["tips"] = self.tips
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

    def auto_play_until_user(self):
        """After a user play, run DDS for any defender/dummy-seat-the-computer-controls turns
        until it's user's turn again, or the deal completes."""
        while not self.complete and self.deal.curplayer not in self.user_seats:
            # On the opening lead, prefer the textbook card from the leader
            # pre-lead tip (when available and legal) so the post-lead tip
            # prose matches what's actually on the table. Fall back to DDS
            # if the card isn't extractable or isn't a legal lead.
            if (self.cards_played_count == 0
                    and self.deal.curplayer == self.leader
                    and self.recommended_lead is not None
                    and self.recommended_lead in list(self.deal.legal_moves())):
                card = self.recommended_lead
            else:
                card = dds_pick(self.deal)
            self._play_card(card)

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
    # Only scenarios that have an embedded-coaching file are user-pickable.
    files = sorted(p.stem for p in COACHING_DIR.glob("*.pbn") if not p.stem.startswith("-"))
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
    # have zero coached scenarios drop out entirely.
    available = {p.stem for p in COACHING_DIR.glob("*.pbn") if not p.stem.startswith("-")}
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
    if body.randomly_rotate:
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


# ---------- static files ----------

@app.get("/")
def root():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
