"""
Bridge Play Trainer — FastAPI backend.

Start with:
    uvicorn bridge-play-trainer.server:app --reload --port 8765

Then open http://localhost:8765/ in your browser.

Declarer mode only for the MVP. Defender mode + Claude grading come next.
"""

import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from endplay.types import Player, Denom, Rank, Deal, Contract, Vul, Penalty
from endplay.dds import solve_board, calc_dd_table
from endplay.parsers import pbn

REPO_ROOT = Path(__file__).resolve().parent.parent
BBA_DIR = REPO_ROOT / "bba"
STATIC_DIR = Path(__file__).resolve().parent / "static"

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
        # Full log of cards played, in chronological order. We rebuild deal
        # state from scratch when undoing, since endplay's unplay() can't
        # cross trick boundaries.
        self.move_log: list = []
        # Each /play (and /claim) records the cards_played_count BEFORE its
        # action. Undo pops the top and replays from move_log up to that count.
        self.undo_stack = []

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
        # been played, regardless of which side the student is on.
        if self.role == "declarer":
            if self.cards_played_count >= 1:
                return {self.declarer, self.dummy}
            return {self.declarer}
        me = next(iter(self.user_seats))
        if self.cards_played_count >= 1:
            return {me, self.dummy}
        return {me}

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
    files = sorted(p.stem for p in BBA_DIR.glob("*.pbn") if not p.stem.startswith("-"))
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
    available = {p.stem for p in BBA_DIR.glob("*.pbn") if not p.stem.startswith("-")}
    if layout_path is None:
        # Fallback: flat alphabetical
        return {"sections": [{"title": "Scenarios", "scenarios": sorted(available)}]}
    sections = parse_layout(layout_path.read_text())
    # Filter to scenarios that actually have a playable bba/*.pbn
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


@app.post("/api/session")
def start_session(body: StartSessionBody):
    path = BBA_DIR / f"{body.scenario}.pbn"
    if not path.exists():
        raise HTTPException(404, f"scenario not found: {body.scenario}")
    with open(path) as f:
        boards = list(pbn.load(f))
    if not boards:
        raise HTTPException(500, "scenario has no deals")
    idx = body.board_index % len(boards)
    try:
        sess = Session(boards[idx], role=body.role)
    except ValueError as e:
        raise HTTPException(400, str(e))
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


def _ground_truth_payload(sess):
    """Pure helper version of the /ground-truth endpoint body — needed by
    /preview-after-lead too. Returns dict suitable for JSON response."""
    R = sess._rl
    declarer_d = R(SEAT_LETTER[sess.declarer])
    dummy_d = R(SEAT_LETTER[sess.dummy])
    user_d = "S"
    partner_d = "N"
    # Play-state-aware: before opening lead, defenders cannot see dummy.
    pre_lead = sess.cards_played_count == 0
    if sess.role == "declarer":
        if pre_lead:
            # Auction just ended; opening lead not yet played, so dummy is
            # still face-down even for declarer.
            hidden_seats_d = [dummy_d, "E", "W"]
            hidden_labels = [f"dummy ({dummy_d}, face-down)"] + \
                            [f"defender ({s})" for s in ("E", "W")]
            role_desc = (f"Student is declarer ({user_d}). Dummy is {dummy_d} "
                         f"(still face-down — opening lead not yet played).")
        else:
            hidden_seats_d = ["E", "W"]
            hidden_labels = [f"defender ({s})" for s in hidden_seats_d]
            role_desc = f"Student is declarer ({user_d}). Dummy is {dummy_d}."
    elif pre_lead:
        # Defender on or before opening lead: dummy is still face-down.
        hidden_seats_d = [declarer_d, dummy_d, partner_d]
        hidden_labels = [
            f"declarer ({declarer_d})",
            f"dummy ({dummy_d}, face-down)",
            f"partner ({partner_d})",
        ]
        on_lead = R(SEAT_LETTER[sess.leader]) == user_d
        lead_status = "on opening lead" if on_lead else "awaiting opening lead from partner"
        role_desc = (
            f"Student is defender ({user_d}), {lead_status}. "
            f"Partner is {partner_d}. Declarer is {declarer_d}, dummy is {dummy_d} (still face-down)."
        )
    else:
        hidden_seats_d = [declarer_d, partner_d]
        hidden_labels = [f"declarer ({declarer_d})", f"partner ({partner_d})"]
        role_desc = (f"Student is defender ({user_d}). Partner is {partner_d}. "
                     f"Declarer is {declarer_d}, dummy is {dummy_d}.")
    return {
        "role_desc": role_desc,
        "hidden_seats": hidden_seats_d,
        "hidden_labels": hidden_labels,
        "initial_hands": {R(SEAT_LETTER[p]): hand_to_dict(sess.initial_hands[p])
                          for p in (Player.north, Player.east, Player.south, Player.west)},
    }


@app.get("/api/session/{sid}/ground-truth")
def ground_truth(sid: str):
    """Initial hands and role metadata in the user's South-at-bottom frame."""
    sess = SESSIONS.get(sid)
    if sess is None:
        raise HTTPException(404, "session not found")
    return _ground_truth_payload(sess)


@app.get("/api/session/{sid}/preview-after-lead")
def preview_after_lead(sid: str):
    """Speculatively play the DDS opening lead, return the resulting state +
    ground truth (as if the lead had been made), then REVERT so the live
    session is unchanged. Lets the client pre-warm the after-lead coaching
    call while the user is still reviewing the auction overlay.

    Returns {"applicable": false} when the prefetch doesn't make sense:
    cards already played, deal complete, or the user is on lead (then we
    can't predict the card)."""
    sess = SESSIONS.get(sid)
    if sess is None:
        raise HTTPException(404, "session not found")
    if sess.cards_played_count > 0 or sess.complete:
        return {"applicable": False, "reason": "play already started"}
    if sess.leader in sess.user_seats:
        return {"applicable": False, "reason": "user is on lead"}
    card = dds_pick(sess.deal)
    sess._play_card(card)
    state = sess.state()
    gt = _ground_truth_payload(sess)
    sess._rebuild_to(0)
    return {"applicable": True, "state": state, "ground_truth": gt}


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
