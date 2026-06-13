# Phase 0 CLI: Claude as your bridge partner, headless. S=you, N=Claude, E/W=robots.
# Thin driver over partner_bidding (the shared engine, also used by server.py /bid).
# Needs ANTHROPIC_API_KEY in .env; without it North falls back to BBA and says so.
#   python3 partner_spike.py --scenario 1C_WalshStyle [--south input] [--dry-claude]

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import partner_bidding as pb

APP_DIR = Path(__file__).resolve().parent

# Load the gitignored .env (ANTHROPIC_API_KEY, etc.), same as server.py.
_ENV = APP_DIR / ".env"
if _ENV.exists():
    for _line in _ENV.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))


def _prompt_south(legal: list[str]) -> str:
    while True:
        ans = input(f"  Your call (S). Legal: {' '.join(legal)}\n  > ").strip()
        ans = {"p": "Pass", "pass": "Pass", "x": "X", "xx": "XX"}.get(ans.lower(), ans.upper())
        ans = ans.replace("NTT", "NT")
        if ans in legal:
            return ans
        print("  Not a legal call; try again.")


def run(scenario: str, board_index: int | None, south_mode: str, dry_claude: bool,
        ns_cc: str, ew_cc: str) -> None:
    board, di, vul, deal_pbn, idx, n = pb.load_board(scenario, board_index)
    dealer = pb.SEATS[di]
    bba_input = pb.write_bba_input(deal_pbn, dealer, vul)

    client = None
    use_api = not dry_claude and bool(os.environ.get("ANTHROPIC_API_KEY"))
    if use_api:
        import anthropic
        client = anthropic.Anthropic()
    north_tag = "Claude/API" if use_api else "Claude(stub→BBA)"
    role = {"N": north_tag, "E": "robot/BBA", "S": "David", "W": "robot/BBA"}

    print(f"\nScenario: {scenario}  (deal {idx + 1} of {n})")
    print(f"Dealer {dealer}, vul {vul}")
    print(f"North (Claude) holds:\n{pb.hand_lines(board.deal.north)}")
    print(f"South (you) hold:\n{pb.hand_lines(board.deal.south)}\n")
    print("Auction")
    print("  " + "    ".join(pb.SEATS))

    calls: list[str] = []
    try:
        while not pb.auction_over(calls):
            seat = pb.seat_at(di, len(calls))
            legal = pb.legal_calls(calls, di)
            reason = ""
            if seat == "N" and use_api:
                call, reason = pb.claude_next_call(client, board.deal.north, calls,
                                                   dealer, vul, legal)
            elif seat == "S" and south_mode == "input":
                call = _prompt_south(legal)
            elif seat == "S" and south_mode == "pass":
                call = "Pass"
            else:                                        # robots, S=bba, or N stub
                call = pb.bba_next_call(bba_input, ns_cc, ew_cc, calls)
            if call not in legal:
                print(f"  ! {seat} produced illegal call {call!r}; using Pass")
                call = "Pass"
            tail = f"   ← {reason}" if reason else ""
            print(f"  {seat} [{role[seat]}]: {call}{tail}")
            calls.append(call)
    finally:
        Path(bba_input).unlink(missing_ok=True)

    print(f"\nFinal contract: {pb.final_contract(calls, di) or 'Passed out'}")
    print(f"Full auction:   {' '.join(calls)}\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 0 partner-play spike (headless).")
    ap.add_argument("--scenario", default="1C_WalshStyle", help="PBN scenario name (no extension)")
    ap.add_argument("--board", type=int, default=None, help="0-based deal index; default random")
    ap.add_argument("--south", choices=("bba", "pass", "input"), default="bba",
                    help="how South bids: bba robot, always pass, or type it")
    ap.add_argument("--dry-claude", action="store_true",
                    help="don't call the API; bid North with BBA (no key needed)")
    ap.add_argument("--ns-cc", default=str(pb.DEFAULT_CC), help="NS .bbsa convention card")
    ap.add_argument("--ew-cc", default=str(pb.DEFAULT_CC), help="EW .bbsa convention card")
    args = ap.parse_args()

    if not Path(pb.BBA_CLI).exists():
        sys.exit(f"bba-cli not found at {pb.BBA_CLI} (set BBA_CLI=...)")
    run(args.scenario, args.board, args.south, args.dry_claude, args.ns_cc, args.ew_cc)


if __name__ == "__main__":
    main()
