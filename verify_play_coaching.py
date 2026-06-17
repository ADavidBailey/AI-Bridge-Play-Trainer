#!/usr/bin/env python3
"""Engine-truth gate for play coaching. Read-only, stateless, idempotent: loads
each scenario via the trainer engine and screens every [PLAY] decision for
reachability + DD-soundness. Emits the AUTHOR->GATE->PROMOTE verdict (PBS-6).
  python3 verify_play_coaching.py [scenarios...]   human summary (+detail if named)
  python3 verify_play_coaching.py --json           flat verdict array to stdout
  python3 verify_play_coaching.py --write-verdicts  per-scenario json to .work/
  python3 verify_play_coaching.py <scn> --diff      diff my authored tuples vs PBS candidates"""
import sys, json
from pathlib import Path
import server
from endplay.dds import solve_board

PLAY_SCENARIOS = [
    "Play_Top_Tricks", "Play_Top_Tricks_NT", "Play_Top_Tricks_Suit",
    "Finesse_Simple", "To_Finesse_Or_Not_To_Finesse", "Two_Way_Finesse",
    "Choice_Of_Finesses", "Hold_Up_3N", "Suit_Promotion",
    "Side_Suit_Ruff_Before_Trump", "Rabbis_Rule", "Endplay_3rd_Round_Strip",
]
HONORS = {"T", "J", "Q", "K", "A"}      # a tie by one of these threatens WHY honesty
RANK_ORDER = "23456789TJQKA"            # low -> high, for the "tying card outranks authored" test


def _pbn(suit, rank):                    # (Denom, Rank) -> "HQ"
    return server.DENOM_LETTER[suit] + rank.abbr


def classify(sess, dec):
    """(status, reason, recommended_accept[pbn]) for one decision, student on play.
    KEEP only when the authored card is DD-optimal AND strictly beats every HONOR
    alternative; an honor tie -> QUARANTINE (a 'the ace loses a trick' WHY would be
    DD-false; we can't read the prose). A low-spot tie -> KEEP, but recommend that
    spot as +ACCEPT so the quiz never punishes a DD-correct play."""
    want = (server.SUIT_FROM_CHAR[dec["correct"]["suit"]],
            server.RANK_FROM_CHAR[dec["correct"]["rank"]])
    wstr = _pbn(*want)
    vals = {(c.suit, c.rank): t for c, t in solve_board(sess.deal)}
    if want not in vals:
        return "DROP", "authored card not legal at decision (unreachable)", []
    qv, maxt = vals[want], max(vals.values())
    if qv < maxt:
        return "DROP", f"reach OK; {wstr}={qv} < DD-max {maxt}: teaches a losing play", []
    worse = [v for v in vals.values() if v < qv]
    if not worse:
        return "DROP", f"reach OK; every legal card ties at {qv}: no losing alternative to quiz", []
    accept = {want} | {(server.SUIT_FROM_CHAR[a["suit"]], server.RANK_FROM_CHAR[a["rank"]])
                       for a in dec.get("accept", [])}
    ties = [k for k, v in vals.items() if v == qv and k not in accept]
    # A tie is NON-TRIVIAL if it could be the card a WHY warns against: an honor
    # (T-A), or any card ranked ABOVE the authored one (a 'top spot' counts). Those
    # make a contrasting WHY DD-false -> QUARANTINE. A low spot below the authored
    # card is trivial -> KEEP and recommend it as +ACCEPT (never ding a correct play).
    # To RELAX over-drop later: drop the RANK_ORDER clause, keep the honor clause.
    wo = RANK_ORDER.index(want[1].abbr)
    block = [k for k in ties if k[1].abbr in HONORS or RANK_ORDER.index(k[1].abbr) > wo]
    trivial = [k for k in ties if k not in block]
    if block:
        names = ", ".join(f"{_pbn(*k)}={qv}" for k in block)
        return ("QUARANTINE",
                f"reach OK; {wstr}={qv} tied by non-trivial {names}: a WHY contrasting it "
                f"would be DD-false (b31 trap)", [])
    rec = sorted(_pbn(*k) for k in trivial)
    note = f"; co-correct low spot(s) -> +ACCEPT {rec}" if rec else ""
    return "KEEP", f"reach OK; {wstr}={qv} strictly best over honors/highs (next {max(worse)}){note}", rec


def verify_scenario(scenario):
    """Returns {scenario, load_error, boards:[...]}; each board's `decisions` are
    already in the PBS-6 verdict schema."""
    out = {"scenario": scenario, "load_error": None, "boards": []}
    path = server._scenario_pbn_path(scenario)
    if path is None:
        out["load_error"] = "no pbn found"
        return out
    for bi in range(path.read_text().count('[Board "')):
        try:
            r = server.start_session(server.StartSessionBody(
                scenario=scenario, board_index=bi, role="declarer"))
        except Exception as e:
            out["boards"].append({"board_index": bi, "status": "load-skip",
                                  "detail": str(e)[:80], "decisions": []})
            continue
        sess = server.SESSIONS[r["session_id"]]
        bnum = str(sess.state().get("board_num"))
        rec = {"board_index": bi, "board_num": bnum,
               "contract": sess.state().get("contract_str"),
               "auto_lead": (server.card_to_display(sess.recommended_lead)
                             if sess.recommended_lead else None),
               "decisions": []}
        if not sess.play_coaching:
            rec["status"] = "tips-only"
            out["boards"].append(rec)
            continue
        rec["status"] = "quizzed"

        def obj(d, status, reason, rec_acc):
            return {"scenario": scenario, "board": bnum, "trick": d["trick"],
                    "seat": d["seat"], "card": d["correct"]["suit"] + d["correct"]["rank"],
                    "status": status, "reason": reason, "recommended_accept": rec_acc}

        decisions, prev, guard = sess.play_coaching, -1, 0
        while guard < 16:
            guard += 1
            dec = sess.auto_play_until_decision()
            cur = sess._play_cursor if dec is not None else len(decisions)
            for j in range(prev + 1, cur):       # any decisions jumped over were unreachable
                rec["decisions"].append(obj(decisions[j], "DROP",
                                            "not reached on the DD line (skipped)", []))
            if dec is None:
                break
            rec["decisions"].append(obj(dec, *classify(sess, dec)))
            prev = cur
            try:
                sess.play_user_card(dec["correct"]["suit"], dec["correct"]["rank"])
            except Exception:
                break
        out["boards"].append(rec)
    return out


def flat(reports):
    return [d for rep in reports for b in rep["boards"] for d in b["decisions"]]


def write_verdicts(reports):
    for rep in reports:
        path = server._scenario_pbn_path(rep["scenario"])
        if path is None:
            continue
        work = path.parent / ".work"
        work.mkdir(parents=True, exist_ok=True)
        decs = [d for b in rep["boards"] for d in b["decisions"]]
        target = work / f"{rep['scenario']}-play-verdict.json"
        target.write_text(json.dumps(decs, indent=2) + "\n")
        print(f"wrote {target}  ({len(decs)} decisions)")


def diff_candidates(reports):
    """Cross-check identity tuples (board,trick,seat,card): my gate's AUTHORED
    [PLAY] decisions vs PBS's dd_line candidate sidecar. Both wrap the same DDS, so
    they SHOULD agree -> this catches WRAPPER/pipeline bugs, not bridge-fact errors.
    An authored tuple absent from candidates is the divergence that parks needs-David."""
    for rep in reports:
        scn = rep["scenario"]
        path = server._scenario_pbn_path(scn)
        cf = path.parent / ".work" / f"{scn}-play-candidates.json" if path else None
        if cf is None or not cf.exists():
            print(f"=== {scn}: no candidates sidecar ({cf}) ===")
            continue
        cand = {(c["board"], c["trick"], c["seat"], c["card"]) for c in json.loads(cf.read_text())}
        gate = {(d["board"], d["trick"], d["seat"], d["card"])
                for b in rep["boards"] for d in b["decisions"]}
        both, only_gate, only_cand = gate & cand, gate - cand, cand - gate
        print(f"=== {scn}: diff gate(authored) vs candidates ===")
        print(f"  authored: {len(gate)}   candidates: {len(cand)}   in both: {len(both)}")
        print(f"  only in gate (authored, NOT a candidate) -> REVIEW: {sorted(only_gate) or 'none'}")
        print(f"  only in candidates (proposed, not yet authored): {len(only_cand)}")
        print(f"  -> {'DIVERGENCE (needs David)' if only_gate else 'clean: wrappers agree on every authored tuple'}")


def summarize(reports):
    tally = {"KEEP": 0, "DROP": 0, "QUARANTINE": 0}
    bstat = {"quizzed": 0, "tips-only": 0, "load-skip": 0}
    print(f"{'scenario':<30} {'boards':>6} {'quiz':>5} {'KEEP':>5} {'DROP':>5} {'QUAR':>5}")
    print("-" * 62)
    for rep in reports:
        s = {"KEEP": 0, "DROP": 0, "QUARANTINE": 0}
        q = 0
        for b in rep["boards"]:
            bstat[b["status"]] = bstat.get(b["status"], 0) + 1
            q += b["status"] == "quizzed"
            for d in b["decisions"]:
                s[d["status"]] += 1
                tally[d["status"]] += 1
        flag = f"  !! {rep['load_error']}" if rep["load_error"] else ""
        print(f"{rep['scenario']:<30} {len(rep['boards']):>6} {q:>5} "
              f"{s['KEEP']:>5} {s['DROP']:>5} {s['QUARANTINE']:>5}{flag}")
    print("-" * 62)
    print(f"decisions: {sum(tally.values())}  KEEP {tally['KEEP']}  DROP {tally['DROP']}  "
          f"QUARANTINE {tally['QUARANTINE']}")
    print(f"boards: quizzed {bstat['quizzed']}  tips-only {bstat['tips-only']}  "
          f"load-skip {bstat['load-skip']}")


def detail(rep):
    print(f"\n=== {rep['scenario']} ===" + (f"  !! {rep['load_error']}" if rep["load_error"] else ""))
    for b in rep["boards"]:
        if b["status"] != "quizzed":
            continue
        print(f"  b{b['board_num']} ({b['contract']}) lead {b['auto_lead']}:")
        for d in b["decisions"]:
            ra = f"  +ACCEPT {d['recommended_accept']}" if d["recommended_accept"] else ""
            print(f"    [t{d['trick']} {d['seat']} {d['card']}] {d['status']}: {d['reason']}{ra}")


if __name__ == "__main__":
    flags = {a for a in sys.argv[1:] if a.startswith("-")}
    named = [a for a in sys.argv[1:] if not a.startswith("-")]
    reports = [verify_scenario(s) for s in (named or PLAY_SCENARIOS)]
    if "--json" in flags:
        print(json.dumps(flat(reports), indent=2))
    elif "--write-verdicts" in flags:
        write_verdicts(reports)
    elif "--diff" in flags:
        diff_candidates(reports)
    else:
        summarize(reports)
        if named:
            for rep in reports:
                detail(rep)
