#!/usr/bin/env python3
"""Engine-truth gate for play coaching. Read-only: loads each scenario via the
trainer engine and screens every [PLAY] decision for reachability + DD-soundness.
Verdicts KEEP / DROP / QUARANTINE — bias to drop (see project_coaching_at_scale)."""
import sys, json
import server
from endplay.dds import solve_board

PLAY_SCENARIOS = [
    "Play_Top_Tricks", "Play_Top_Tricks_NT", "Play_Top_Tricks_Suit",
    "Finesse_Simple", "To_Finesse_Or_Not_To_Finesse", "Two_Way_Finesse",
    "Choice_Of_Finesses", "Hold_Up_3N", "Suit_Promotion",
    "Side_Suit_Ruff_Before_Trump", "Rabbis_Rule", "Endplay_3rd_Round_Strip",
]


def _key(card):
    return (card.suit, card.rank)


def _disp(suit, rank):
    return server.card_to_display(server.Card(suit=suit, rank=rank))


def classify(sess, dec):
    """(verdict, detail, recommend_accept) for one decision, student on play."""
    want = (server.SUIT_FROM_CHAR[dec["correct"]["suit"]],
            server.RANK_FROM_CHAR[dec["correct"]["rank"]])
    vals = {_key(c): t for c, t in solve_board(sess.deal)}
    if want not in vals:
        return "DROP", "authored card not legal at decision (unreachable)", []
    qv, maxt = vals[want], max(vals.values())
    if qv < maxt:
        return "DROP", f"authored {_disp(*want)}={qv} < DD-max {maxt}: teaches a losing play", []
    accept = {want} | {(server.SUIT_FROM_CHAR[a["suit"]], server.RANK_FROM_CHAR[a["rank"]])
                       for a in dec.get("accept", [])}
    worse = [v for v in vals.values() if v < qv]
    if not worse:
        return "QUARANTINE", f"no losing alternative — every legal card ties at {qv}: vacuous", []
    ties = [k for k, v in vals.items() if v == qv and k not in accept]
    if ties:
        return ("QUARANTINE",
                f"non-accepted card(s) tie the answer ({_disp(*want)}={qv}): "
                f"quiz would punish a DD-correct play",
                [_disp(*k) for k in ties])
    return "KEEP", f"{_disp(*want)}={qv} uniquely best (next {max(worse)})", []


def verify_scenario(scenario):
    out = {"scenario": scenario, "load_error": None, "boards": []}
    path = server._scenario_pbn_path(scenario)
    if path is None:
        out["load_error"] = "no pbn found"
        return out
    nboards = path.read_text().count('[Board "')
    for bi in range(nboards):
        try:
            r = server.start_session(server.StartSessionBody(
                scenario=scenario, board_index=bi, role="declarer"))
        except Exception as e:
            out["boards"].append({"board_index": bi, "status": "load-skip",
                                  "detail": str(e)[:80]})
            continue
        sess = server.SESSIONS[r["session_id"]]
        rec = {"board_index": bi, "board_num": sess.state().get("board_num"),
               "contract": sess.state().get("contract_str"),
               "auto_lead": (server.card_to_display(sess.recommended_lead)
                             if sess.recommended_lead else None),
               "decisions": []}
        if not sess.play_coaching:
            rec["status"] = "tips-only"
            out["boards"].append(rec)
            continue
        rec["status"] = "quizzed"
        decisions, prev, guard = sess.play_coaching, -1, 0
        while guard < 16:
            guard += 1
            dec = sess.auto_play_until_decision()
            cur = sess._play_cursor
            stop = cur if dec is not None else len(decisions)
            for j in range(prev + 1, stop):       # any decisions jumped over were unreachable
                d = decisions[j]
                rec["decisions"].append({
                    "authored": f"trick {d['trick']} {d['seat']} {d['correct']['suit']}{d['correct']['rank']}",
                    "verdict": "DROP", "detail": "not reached on the DD line (skipped)",
                    "recommend_accept": []})
            if dec is None:
                break
            verdict, detail, rec_acc = classify(sess, dec)
            rec["decisions"].append({
                "authored": f"trick {dec['trick']} {dec['seat']} {dec['correct']['suit']}{dec['correct']['rank']}",
                "verdict": verdict, "detail": detail, "recommend_accept": rec_acc})
            prev = cur
            try:
                sess.play_user_card(dec["correct"]["suit"], dec["correct"]["rank"])
            except Exception:
                break
        out["boards"].append(rec)
    return out


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
            if b["status"] == "quizzed":
                q += 1
            for d in b.get("decisions", []):
                s[d["verdict"]] += 1
                tally[d["verdict"]] += 1
        flag = f"  !! {rep['load_error']}" if rep["load_error"] else ""
        print(f"{rep['scenario']:<30} {len(rep['boards']):>6} {q:>5} "
              f"{s['KEEP']:>5} {s['DROP']:>5} {s['QUARANTINE']:>5}{flag}")
    print("-" * 62)
    total = sum(tally.values())
    print(f"decisions: {total}  KEEP {tally['KEEP']}  DROP {tally['DROP']}  "
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
            rec = f"  +ACCEPT {d['recommend_accept']}" if d["recommend_accept"] else ""
            print(f"    [{d['authored']}] {d['verdict']}: {d['detail']}{rec}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    as_json = "--json" in sys.argv[1:]
    scenarios = args or PLAY_SCENARIOS
    reports = [verify_scenario(s) for s in scenarios]
    if as_json:
        print(json.dumps(reports, indent=2))
    else:
        summarize(reports)
        if args:                       # detailed view when scenarios named explicitly
            for rep in reports:
                detail(rep)
