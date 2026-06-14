# Shared bidding engine for the partner-play trainer: legal-call computation,
# auction termination, BBA-CLI robot bids (via --auction-prefix), and the Claude
# partner seat + post-auction review. Used by server.py (the /bid page) and the
# partner_spike.py CLI. Pure logic — the caller passes in an anthropic client.

from __future__ import annotations

import io
import os
import random
import re
import subprocess
import tempfile
from pathlib import Path

from endplay.parsers import pbn

DATA_ROOT = Path(os.environ.get("BRIDGE_DATA_ROOT",
                                "/Users/adavidbailey/Practice-Bidding-Scenarios"))
BBA_CLI = os.environ.get("BBA_CLI", "/Applications/Bridge Utilities/bba-cli")
DEFAULT_CC = DATA_ROOT / "bbsa" / "21GF-GIB.bbsa"   # 2/1 GF, matches the PBN system
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-8")

SEATS = ["N", "E", "S", "W"]                 # clockwise; endplay Player order
STRAINS = ["C", "D", "H", "S", "NT"]         # ascending
STRAIN_RANK = {s: i for i, s in enumerate(STRAINS)}
SUIT_SYM = {"S": "♠", "H": "♥", "D": "♦", "C": "♣", "NT": "NT"}
VUL_PBN = {"none": "None", "both": "All", "ns": "NS", "ew": "EW"}


# ---------- call / auction helpers (PBN call form: 1C, 2D, 3NT, X, XX, Pass) ----------

def parse_bid(tok: str):
    """(level, strain_index) for a contract bid, else None."""
    if len(tok) >= 2 and tok[0] in "1234567" and tok[1:] in STRAIN_RANK:
        return int(tok[0]), STRAIN_RANK[tok[1:]]
    return None


def bid_value(tok: str) -> int:
    pb = parse_bid(tok)
    return pb[0] * 5 + pb[1] if pb else -1


def seat_at(dealer_idx: int, i: int) -> str:
    return SEATS[(dealer_idx + i) % 4]


def auction_over(calls: list[str]) -> bool:
    if len(calls) < 4:
        return False
    if len(calls) == 4 and all(c == "Pass" for c in calls):
        return True                                  # passed out
    return calls[-3:] == ["Pass", "Pass", "Pass"] and any(c != "Pass" for c in calls)


def legal_calls(calls: list[str], dealer_idx: int) -> list[str]:
    """Every legal call for the seat about to act."""
    opts = ["Pass"]
    best = max((bid_value(c) for c in calls), default=-1)
    for lvl in range(1, 8):
        for si, st in enumerate(STRAINS):
            if lvl * 5 + si > best:
                opts.append(f"{lvl}{st}")
    last_idx = next((i for i in range(len(calls) - 1, -1, -1) if calls[i] != "Pass"), None)
    if last_idx is not None:
        last = calls[last_idx]
        opp = (seat_at(dealer_idx, last_idx) in "NS") != (seat_at(dealer_idx, len(calls)) in "NS")
        if opp and parse_bid(last):
            opts.append("X")
        elif opp and last == "X":
            opts.append("XX")
    return opts


def final_contract(calls: list[str], dealer_idx: int) -> str | None:
    last_bid_idx = next((i for i in range(len(calls) - 1, -1, -1) if parse_bid(calls[i])), None)
    if last_bid_idx is None:
        return None
    lvl, si = parse_bid(calls[last_bid_idx])
    winner = seat_at(dealer_idx, last_bid_idx)
    declarer = next(seat_at(dealer_idx, i) for i, c in enumerate(calls)
                    if parse_bid(c) and parse_bid(c)[1] == si
                    and (seat_at(dealer_idx, i) in "NS") == (winner in "NS"))
    tail = calls[last_bid_idx + 1:]
    dbl = "XX" if "XX" in tail else ("X" if "X" in tail else "")
    return f"{lvl}{STRAINS[si]}{dbl} by {declarer}"


def call_display(call: str) -> str:
    """PBN call → human form with suit glyph (1C → 1♣, 3NT → 3NT, X/XX/Pass)."""
    pb = parse_bid(call)
    if pb:
        st = STRAINS[pb[1]]
        return f"{pb[0]}NT" if st == "NT" else f"{pb[0]}{SUIT_SYM[st]}"
    return call


# ---------- deal loading ----------

def _scenario_path(scenario: str) -> Path | None:
    for d in ("coaching-curated", "coaching", "bba"):
        p = DATA_ROOT / d / f"{scenario}.pbn"
        if p.exists():
            return p
    return None


def _strip_post_auction_blocks(text: str) -> str:
    """Drop the first {...} coaching block after each [Auction] (endplay chokes
    on it). Consumes one trailing newline when the block was on its own line, so
    endplay doesn't see a blank line and spawn phantom empty boards. Mirrors
    server.py's stripper — must stay behaviourally identical."""
    out, pos = [], 0
    for m in re.compile(r'\[Auction\s+"[^"]*"\]').finditer(text):
        out.append(text[pos:m.end()])
        tail = m.end()
        next_event = text.find('\n[Event ', tail)
        open_pos = text.find('{', tail)
        if open_pos == -1 or (next_event != -1 and open_pos > next_event):
            pos = tail
            continue
        close_pos = text.find('}', open_pos)
        if close_pos == -1:
            pos = tail
            continue
        out.append(text[tail:open_pos])
        end_pos = close_pos + 1
        if open_pos > 0 and text[open_pos - 1] == "\n" and end_pos < len(text) and text[end_pos] == "\n":
            end_pos += 1
        pos = end_pos
    out.append(text[pos:])
    return "".join(out)


def load_board(scenario: str, board_index: int | None):
    """Return (board, dealer_idx, vul_str, deal_pbn, chosen_index, n_boards).
    board_index None → random deal within the scenario."""
    path = _scenario_path(scenario)
    if path is None:
        raise FileNotFoundError(f"scenario not found under {DATA_ROOT}: {scenario}")
    boards = list(pbn.load(io.StringIO(_strip_post_auction_blocks(path.read_text()))))
    if not boards:
        raise ValueError(f"scenario has no deals: {scenario}")
    idx = random.randrange(len(boards)) if board_index is None else board_index % len(boards)
    board = boards[idx]
    deal_pbn = board.deal.to_pbn()
    # Some PBNs omit [Dealer]; fall back to the seat the deal string lists first.
    if board.dealer is not None:
        di = int(board.dealer)
    else:
        first = deal_pbn.split(":", 1)[0].strip()
        di = SEATS.index(first) if first in SEATS else 0
    vul = VUL_PBN.get(board.vul.name, "None") if board.vul is not None else "None"
    return (board, di, vul, deal_pbn, idx, len(boards))


# ---------- BBA-CLI robot bidder ----------

def write_bba_input(deal_pbn: str, dealer_letter: str, vul_str: str) -> str:
    """Write a one-deal PBN for bba-cli; returns the temp path (caller keeps it)."""
    with tempfile.NamedTemporaryFile("w", suffix=".pbn", delete=False) as f:
        f.write(f'[Event "spike"]\n[Board "1"]\n[Dealer "{dealer_letter}"]\n'
                f'[Vulnerable "{vul_str}"]\n[Deal "{deal_pbn}"]\n')
        return f.name


def _norm_call(tok: str) -> str | None:
    if tok in ("Pass", "X", "XX"):
        return tok
    m = re.match(r"^([1-7])(NT|N|C|D|H|S)$", tok)
    if m:
        st = "NT" if m.group(2) == "N" else m.group(2)
        return f"{m.group(1)}{st}"
    return None                                       # annotation like =1=, drop it


def _run_bba(input_pbn: str, ns_cc: str, ew_cc: str, prefix: list[str]) -> list[str]:
    with tempfile.NamedTemporaryFile("r", suffix=".pbn", delete=False) as f:
        out_path = f.name
    cmd = [BBA_CLI, "-i", input_pbn, "-o", out_path,
           "--ns-conventions", ns_cc, "--ew-conventions", ew_cc]
    if prefix:
        cmd += ["--auction-prefix", " ".join(prefix)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"bba-cli failed ({r.returncode}): {r.stderr.strip()}")
        text = Path(out_path).read_text()
    finally:
        Path(out_path).unlink(missing_ok=True)
    out, in_auction = [], False
    for line in text.splitlines():
        if line.startswith("[Auction"):
            in_auction = True
            continue
        if in_auction:
            if line.startswith("["):
                break
            out.extend(c for c in (_norm_call(t) for t in line.split()) if c)
    return out


def bba_next_call(input_pbn: str, ns_cc: str, ew_cc: str, prefix: list[str]) -> str:
    """The next seat's call given the auction so far (forced as a prefix)."""
    auction = _run_bba(input_pbn, ns_cc, ew_cc, prefix)
    return auction[len(prefix)] if len(auction) > len(prefix) else "Pass"


def full_bba_auction(input_pbn: str, ns_cc: str, ew_cc: str) -> list[str]:
    """BBA's own complete auction for the deal — the 'compare vs BBA' baseline."""
    return _run_bba(input_pbn, ns_cc, ew_cc, prefix=[])


# ---------- Claude partner seat ----------

CLAUDE_SYSTEM = (
    "You are an expert duplicate bridge player sitting NORTH. Your partner is "
    "SOUTH; East and West are opponents. North-South play 2/1 Game Force "
    "(Standard American style: 5-card majors, strong 1NT 15-17, standard "
    "responses and rebids). You see only your own 13 cards and the auction so "
    "far — never any hidden hand. Choose the single best call for North now. "
    "Bid soundly and in partnership style; do not invent a system. Return only "
    "the structured result."
)


_HCP = {"A": 4, "K": 3, "Q": 2, "J": 1}


def hand_hcp_shape(hand) -> tuple[int, str]:
    """High-card points and distribution (e.g. '♠2 ♥2 ♦4 ♣5') for a hand —
    given to Claude so it never has to count its own cards (humans don't)."""
    hcp, parts = 0, []
    for suit, attr in (("S", "spades"), ("H", "hearts"), ("D", "diamonds"), ("C", "clubs")):
        cards = list(getattr(hand, attr))
        hcp += sum(_HCP.get(r.abbr, 0) for r in cards)
        parts.append(f"{SUIT_SYM[suit]}{len(cards)}")
    return hcp, " ".join(parts)


def hand_lines(hand) -> str:
    rows = []
    for suit, attr in (("S", "spades"), ("H", "hearts"), ("D", "diamonds"), ("C", "clubs")):
        ranks = "".join(r.abbr for r in getattr(hand, attr))
        rows.append(f"  {SUIT_SYM[suit]} {ranks or '—'}")
    return "\n".join(rows)


def _auction_text(calls: list[str], dealer_idx: int) -> str:
    if not calls:
        return "  (no calls yet — you are first to bid)"
    return "\n".join(f"  {seat_at(dealer_idx, i)}: {c}" for i, c in enumerate(calls))


def claude_next_call(client, hand, calls: list[str], dealer_letter: str,
                     vul_str: str, legal: list[str]) -> tuple[str, str]:
    dealer_idx = SEATS.index(dealer_letter)
    ns_vul = "vulnerable" if vul_str in ("All", "NS") else "not vulnerable"
    ew_vul = "vulnerable" if vul_str in ("All", "EW") else "not vulnerable"
    hcp, shape = hand_hcp_shape(hand)
    prompt = (
        f"Dealer: {dealer_letter}. Vulnerability: NS {ns_vul}, EW {ew_vul}.\n\n"
        f"Auction so far (each line is seat: call):\n{_auction_text(calls, dealer_idx)}\n\n"
        f"Your hand (North) — {hcp} HCP, distribution {shape}:\n{hand_lines(hand)}\n\n"
        "What is your call?"
    )
    schema = {
        "type": "object",
        "properties": {
            "call": {"type": "string", "enum": legal,
                     "description": "Your call in PBN form, e.g. 1C, 2D, 3NT, X, XX, Pass."},
            "reason": {"type": "string", "description": "One sentence of bridge reasoning."},
        },
        "required": ["call", "reason"],
        "additionalProperties": False,
    }
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        # Headroom for adaptive thinking + the JSON answer. At 2000, deep
        # thinking could exhaust the budget (stop_reason "max_tokens"), leaving
        # the response with no text block — the extraction below then raised
        # StopIteration and 500'd the /step request mid-auction.
        max_tokens=8000,
        thinking={"type": "adaptive"},
        output_config={"format": {"type": "json_schema", "schema": schema}},
        system=CLAUDE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    import json
    text = next((b.text for b in resp.content if b.type == "text"), "")
    if not text:
        raise RuntimeError(f"Claude returned no call (stop_reason={resp.stop_reason})")
    data = json.loads(text)
    return data["call"], data.get("reason", "")


def claude_review(client, hands_by_seat: dict, calls: list[str],
                  dealer_letter: str, vul_str: str, bba_calls: list[str]) -> str:
    """A short post-auction review from North's (partner's) perspective.

    hands_by_seat maps 'N'/'E'/'S'/'W' to hand objects. HCP and distribution
    are computed here and handed to Claude alongside the cards, so it never
    counts points or honors by eye — it was getting both wrong (e.g. calling a
    16-count "balanced 18", or "two top diamonds" with AKQ)."""
    dealer_idx = SEATS.index(dealer_letter)

    def labeled(cs):
        return ("\n".join(f"  {seat_at(dealer_idx, i)}: {call_display(c)}"
                          for i, c in enumerate(cs)) or "  (passed out)")

    def hand_block(s):
        hcp, shape = hand_hcp_shape(hands_by_seat[s])
        return f"{s} — {hcp} HCP, distribution {shape}:\n{hand_lines(hands_by_seat[s])}"

    hands = "\n".join(hand_block(s) for s in ("N", "E", "S", "W"))
    prompt = (
        f"The hand is over. Dealer {dealer_letter}, vulnerability {vul_str}. "
        "You sat NORTH; your partner was SOUTH; East/West were robot opponents.\n\n"
        f"All four hands — the HCP and distribution for each are computed for you; "
        f"use those figures and do not recount:\n{hands}\n\n"
        f"Our auction (each line is 'seat: call' — your calls are the N lines, "
        f"partner's are the S lines):\n{labeled(calls)}\n"
        f"Final contract: {final_contract(calls, dealer_idx) or 'Passed out'}\n\n"
        f"For comparison, a bidding engine (BBA) bid this same layout:\n{labeled(bba_calls)}\n\n"
        "Give a SHORT review (2-4 sentences) for your partner South: how did our "
        "auction go, is the final contract sound, and one concrete takeaway. Be "
        "accurate about who bid what, and precise about high cards — rely on the HCP "
        "and distribution given above, and when you mention honors read each suit's "
        "exact cards rather than estimating. Warm, specific, encouraging. Plain text, no markdown."
    )
    # No extended thinking here: it's a short summary, and adaptive thinking can
    # eat the whole budget and truncate (or empty out) the visible review.
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1000,
        system="You are an encouraging expert bridge teacher reviewing a hand with your partner.",
        messages=[{"role": "user", "content": prompt}],
    )
    return next((b.text for b in resp.content if b.type == "text"), "").strip()
