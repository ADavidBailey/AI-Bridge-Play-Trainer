"use strict";

const SEATS = ["N", "E", "S", "W"];
const SEAT_INDEX = { N: 0, E: 1, S: 2, W: 3 };
const SUIT_SYMBOL = { S: "♠", H: "♥", D: "♦", C: "♣" };
const SUIT_NAME = { S: "Spades", H: "Hearts", D: "Diamonds", C: "Clubs" };
const RED_SUITS = new Set(["H", "D"]);
// 4-color deck: ♠ black, ♥ red, ♦ orange, ♣ green. CSS classes own the colors.
const SUIT_CODE_TO_CLASS = { S: "suit-spade", H: "suit-heart", D: "suit-diamond", C: "suit-club" };
const SUIT_SYM_TO_CLASS  = { "♠": "suit-spade", "♥": "suit-heart", "♦": "suit-diamond", "♣": "suit-club" };
function suitClassFromSymbol(sym) { return SUIT_SYM_TO_CLASS[sym] || ""; }

let sessionId = null;
let lastState = null;
let viewingLastTrick = false;
// When non-null, the center shows the cards from this specific trick index.
// Set by clicking a card on the tricks-strip. Clears on any other click
// (the center's existing onclick toggles back to normal).
let viewingTrickIndex = null;
let trickFreeze = null;             // { plays: [...] } while we pause to show the completed trick
let auctionAnimating = false;
let auctionVisibleCount = null;     // null = show full auction; otherwise number of bids to reveal
let auctionAnimationToken = 0;      // bumped to cancel in-flight animations when a new deal starts
let awaitingPlay = false;           // auction is fully revealed; waiting for the user to click Play

let reviewingAuction = false;       // user is holding the Review button

// Tutorial state — populated when state.coaching is non-null. Reveals are in
// the author's REAL-compass frame; applyTutorialMask maps to display seats.
let tutorialReveals = new Set();    // Set<"N"|"E"|"S"|"W"> in real-compass frame
let tutorialContinueResolve = null; // resolved by the Continue button

const TRICK_HOLD_MS = 3000;
const playedVisible = { N: false, E: false, S: false, W: false };

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function bidsInCenter() { return auctionAnimating || awaitingPlay || reviewingAuction; }

function togglePlayed(seatLetter) {
  playedVisible[seatLetter] = !playedVisible[seatLetter];
  if (lastState) render(lastState);
}

function toggleLastTrick() {
  if (!lastState || !lastState.trick_history || lastState.trick_history.length === 0) return;
  // If we're peeking at a specific trick from the strip, clicking center
  // returns to normal. Otherwise toggle the last-trick peek as before.
  if (viewingTrickIndex !== null) {
    viewingTrickIndex = null;
    render(lastState);
    return;
  }
  viewingLastTrick = !viewingLastTrick;
  render(lastState);
}

function viewTrickFromStrip(idx) {
  if (!lastState || !lastState.trick_history) return;
  // Toggle: click the same trick again to return to normal.
  viewingTrickIndex = viewingTrickIndex === idx ? null : idx;
  viewingLastTrick = false;
  render(lastState);
}

// ---------- helpers ----------

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k === "html") e.innerHTML = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const c of children) {
    if (c == null) continue;
    e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return e;
}

function suitClass(suit) { return SUIT_CODE_TO_CLASS[suit] || ""; }

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status}: ${text}`);
  }
  return res.json();
}

// ---------- tutorial masking ----------

// Map an author's real-compass seat letter to its display seat letter,
// using the per-session rotation_shift the server hands us.
function realToDisplaySeat(realLetter, shift) {
  const realIdx = SEAT_INDEX[realLetter];
  if (realIdx === undefined) return null;
  return SEATS[(realIdx + shift) % 4];
}

// Expand reveal tokens like "N", "NS", "NSEW" into individual seat letters.
function expandRevealToken(tok) {
  const seats = [];
  for (const ch of String(tok).toUpperCase()) {
    if (ch in SEAT_INDEX) seats.push(ch);
  }
  return seats;
}

// During the tutorial phase, replace state.hands with the [show]-filtered
// initial layout — the server's visible_hands() would hide non-declarer
// seats pre-lead, but the tutorial may want to reveal whichever side the
// author called out. After startPlay clears tutorialReveals this is a
// no-op; server visibility rules take over.
//
// Mid-auction (auctionAnimating === true) we ignore any reveals beyond the
// student's own hand: real-bridge convention is that each player sees only
// their own 13 cards until the auction concludes. The accumulated reveals
// take effect on the final render once auctionAnimating flips to false.
function applyTutorialMask(state) {
  if (tutorialReveals.size === 0) return;
  const shift = state.rotation_shift || 0;
  let visibleDisplay;
  // While the bids are being revealed one-by-one, each player sees only their
  // own hand (real-bridge convention). The dummy isn't shown until after the
  // opening lead — at which point the server's visible_hands() takes over and
  // the post-auction planning coaching fires (see startPlay).
  if (auctionAnimating) {
    visibleDisplay = new Set(["S"]);
  } else {
    visibleDisplay = new Set();
    for (const real of tutorialReveals) {
      const disp = realToDisplaySeat(real, shift);
      if (disp) visibleDisplay.add(disp);
    }
  }
  const source = state.initial_hands || state.hands;
  for (const seat of SEATS) {
    state.hands[seat] = visibleDisplay.has(seat) ? source[seat] : null;
  }
}

// ---------- rendering ----------

// Render a hand as 4 suit rows of clickable card buttons. Cards are clickable
// only when it's this seat's turn and the user controls this seat — otherwise
// they appear dimmed. Illegal cards (must-follow-suit) are also dimmed.
function renderHand(hand, opts) {
  const { isCurrentSeat, userControlsSeat, legalSet } = opts;
  const rows = [];
  for (const suit of ["S", "H", "D", "C"]) {
    const ranks = (hand && hand[suit]) ? hand[suit] : [];
    const row = el("div", { class: `hand-suit ${suitClass(suit)}` });
    row.appendChild(el("span", { class: "suit-symbol" }, SUIT_SYMBOL[suit]));
    if (ranks.length === 0) {
      row.appendChild(el("span", { class: "muted" }, "—"));
    } else {
      for (const rank of ranks) {
        const isLegal = isCurrentSeat && userControlsSeat && legalSet.has(`${suit}${rank}`);
        const cls = `card-btn ${suitClass(suit)} ${isLegal ? "legal" : "illegal"}`;
        const btn = el("span", {
          class: cls,
          ...(isLegal ? { onclick: () => playCard(suit, rank) } : {}),
        }, rank);
        row.appendChild(btn);
      }
    }
    rows.push(row);
  }
  return rows;
}

// Seat letters in display frame map to fixed role labels because the table
// orientation is fixed (user at bottom, partner top, LHO left, RHO right).
const ROLE_LABEL = { S: "You", N: "Partner", W: "LHO", E: "RHO" };

function renderSeatInto(slot, seatLetter, state) {
  slot.innerHTML = "";
  slot.classList.remove("face-down", "active");
  if (seatLetter === null) {
    return;
  }

  let title = ROLE_LABEL[seatLetter] || seatLetter;
  if (seatLetter === state.declarer) title += " (declarer)";
  if (seatLetter === state.dummy) title += " (dummy)";
  if (state.to_play === seatLetter && !state.complete) {
    slot.classList.add("active");
  }
  slot.appendChild(el("div", { class: "seat-name" }, title));

  // At end of deal: reveal all four hands from the result payload.
  const hand = state.complete && state.result && state.result.all_hands
    ? state.result.all_hands[seatLetter]
    : state.hands[seatLetter];
  if (hand === null) {
    slot.classList.add("face-down");
    slot.appendChild(el("div", { class: "muted" }, "Hidden"));
    const played = (bidsInCenter() ? [] : (state.cards_played_by_seat[seatLetter] || []));
    if (played.length) {
      const strip = el("div", { class: "played-strip" });
      const visible = !!playedVisible[seatLetter];
      const toggle = el("button", {
        class: "played-toggle",
        onclick: () => togglePlayed(seatLetter),
      }, visible ? `Hide played (${played.length})` : `Show played (${played.length})`);
      strip.appendChild(toggle);
      if (visible) {
        const cards = el("div", { class: "played-cards" });
        for (const c of played) {
          const symbol = c[0];
          const klass = suitClassFromSymbol(symbol);
          cards.appendChild(el("span", { class: `played-card ${klass}` }, c));
        }
        strip.appendChild(cards);
      }
      slot.appendChild(strip);
    }
    return;
  }

  // While a completed trick is frozen on screen for review, keep the user's
  // OWN legal cards clickable (and outlined) so an eager player can lead to
  // the next trick without waiting out the pause. Engine-controlled seats
  // stay gated until the freeze clears.
  const userToPlayHere = state.user_to_play && state.to_play === seatLetter;
  const isCurrentSeat = state.to_play === seatLetter && !state.complete
    && !bidsInCenter() && (!trickFreeze || userToPlayHere);
  const userControlsSeat = state.user_to_play && isCurrentSeat;
  const legalSet = new Set(state.legal_moves.map(m => `${m.suit}${m.rank}`));
  for (const row of renderHand(hand, { isCurrentSeat, userControlsSeat, legalSet })) {
    slot.appendChild(row);
  }
  if (userControlsSeat) {
    slot.appendChild(el("div", { class: "seat-prompt" }, "Your move — click a card."));
  }
}

function fillAuctionGrid(grid, state, opts = {}) {
  const { largeStyle = false } = opts;
  grid.innerHTML = "";
  for (const s of ["W", "N", "E", "S"]) {
    grid.appendChild(el("div", { class: "auction-cell header" }, s));
  }
  const order = ["W", "N", "E", "S"];
  const dealerIdx = order.indexOf(state.dealer);
  // Pad pre-dealer columns with empty cells so the auction lines up under
  // the right column. The original used "—" placeholders, but those read as
  // "this seat bid nothing" rather than "the dealer comes later" — clearer
  // to leave them visually empty.
  for (let i = 0; i < dealerIdx; i++) {
    grid.appendChild(el("div", { class: "auction-cell empty" }));
  }
  const visibleCalls = (auctionVisibleCount === null)
    ? state.auction
    : state.auction.slice(0, auctionVisibleCount);
  visibleCalls.forEach((call, idx) => {
    const cell = el("div", { class: "auction-cell" });
    appendCallWithColoredSuit(cell, call.call);
    if (call.annotation) cell.title = call.annotation;
    if (auctionAnimating && idx === visibleCalls.length - 1) {
      cell.classList.add("auction-cell-new");
    }
    grid.appendChild(cell);
  });
}

function renderAuction(state) {
  // The auction is drawn into the centre of the table by renderTable when
  // bidsInCenter() — nothing to render outside of that.
}

function userSideLabel(state) {
  const userSeat = userPrimarySeat(state);
  return (userSeat === "N" || userSeat === "S") ? "NS" : "EW";
}

function renderTricksStrip(state) {
  const strip = document.getElementById("tricks-strip");
  strip.innerHTML = "";
  if (bidsInCenter()) return;
  const history = state.trick_history || [];
  if (history.length === 0) return;
  const ourSide = userSideLabel(state);
  history.forEach((t, idx) => {
    const winnerSide = (t.winner === "N" || t.winner === "S") ? "NS" : "EW";
    const klass = winnerSide === ourSide ? "ours" : "theirs";
    const selectedKlass = viewingTrickIndex === idx ? " trick-back-selected" : "";
    strip.appendChild(el("div", {
      class: `trick-back ${klass}${selectedKlass}`,
      title: `Trick ${t.n} — won by ${t.winner} · click to view`,
      onclick: () => viewTrickFromStrip(idx),
    }));
  });
}

function appendTextWithColoredSuits(parent, text) {
  // Walk free-form text, wrapping ♠/♥/♦/♣ in colored spans.
  const symMap = SUIT_SYM_TO_CLASS;
  for (const part of String(text).split(/([♠♥♦♣])/)) {
    if (part in symMap) parent.appendChild(el("span", { class: symMap[part] }, part));
    else if (part) parent.appendChild(document.createTextNode(part));
  }
}

function appendCallWithColoredSuit(cell, callText) {
  // Server sends bids like "1♠", "3NT", "Pass", "X". Color the suit symbol.
  const symMap = SUIT_SYM_TO_CLASS;
  const ntMatch = callText.match(/^(\d+)NT$/);
  if (ntMatch) {
    cell.appendChild(document.createTextNode(ntMatch[1] + "NT"));
    return;
  }
  const m = callText.match(/^(\d+)([♠♥♦♣])$/);
  if (m) {
    cell.appendChild(document.createTextNode(m[1]));
    cell.appendChild(el("span", { class: symMap[m[2]] }, m[2]));
    return;
  }
  cell.appendChild(document.createTextNode(callText));
}

function renderContractDisplay(state) {
  const box = document.getElementById("contract-display");
  box.innerHTML = "";
  const main = el("div", { class: "contract-line-main" });
  // Hide the final contract until the auction has fully revealed — knowing it
  // ahead of time would spoil the bidding tutorial / bid quiz.
  if (auctionAnimating) {
    main.appendChild(el("span", { class: "muted" }, "?"));
  } else {
    const sym = state.strain_symbol;  // ♠/♥/♦/♣/NT
    main.appendChild(el("span", {}, String(state.level)));
    main.appendChild(el("span", { class: suitClassFromSymbol(sym) }, sym));
  }
  box.appendChild(main);
  box.appendChild(el("div", { class: "contract-line-sub" }, `Dealer: ${state.dealer}`));
}

const SEAT_ORDER = ["N", "E", "S", "W"];
function seatOffset(seat) { return SEAT_ORDER.indexOf(seat); }
function partnerOf(seat) { return SEAT_ORDER[(seatOffset(seat) + 2) % 4]; }
function lhoOf(seat) { return SEAT_ORDER[(seatOffset(seat) + 1) % 4]; }
function rhoOf(seat) { return SEAT_ORDER[(seatOffset(seat) + 3) % 4]; }

function userPrimarySeat(state) {
  // The server's rotation always lands the user's seat at display south,
  // regardless of role (or, for coached scenarios, regardless of declarer).
  return "S";
}

function slotLayout(state) {
  // Fixed compass orientation — user (South) at bottom, partner (North) at
  // top, West on the left (LHO of South), East on the right (RHO of South).
  return { bottom: "S", top: "N", left: "W", right: "E" };
}

function renderTable(state) {
  const layout = slotLayout(state);
  renderSeatInto(document.getElementById("slot-top"), layout.top, state);
  renderSeatInto(document.getElementById("slot-bottom"), layout.bottom, state);
  renderSeatInto(document.getElementById("slot-left"), layout.left, state);
  renderSeatInto(document.getElementById("slot-right"), layout.right, state);

  const center = document.getElementById("center");
  center.innerHTML = "";
  center.classList.remove("reviewing");
  center.onclick = toggleLastTrick;

  const seatToPosition = {
    [layout.top]: "top",
    [layout.right]: "right",
    [layout.bottom]: "bottom",
    [layout.left]: "left",
  };

  const hasHistory = state.trick_history && state.trick_history.length > 0;
  const showReview = viewingLastTrick && hasHistory && !trickFreeze;

  // Review (press-and-hold auction) takes priority over trick freeze and
  // last-trick peek so the user can always recall the bidding during play.
  if (bidsInCenter()) {
    const wrap = el("div", { class: "center-auction-box" });
    const grid = el("div", { class: "center-auction-grid" });
    fillAuctionGrid(grid, state, { largeStyle: true });
    wrap.appendChild(grid);
    if (awaitingPlay) {
      const playBtn = el("button", {
        class: "primary center-play-btn",
        onclick: startPlay,
      }, "Play");
      wrap.appendChild(playBtn);
    }
    center.appendChild(wrap);
    return;
  }

  if (trickFreeze) {
    center.classList.add("reviewing");
    center.appendChild(el("div", { class: "center-trick-label" },
      `Trick #${trickFreeze.n} — ${trickFreeze.winner} won`));
    for (const p of trickFreeze.plays) {
      const symbol = p.card[0];
      const suitKlass = suitClassFromSymbol(symbol);
      const pos = seatToPosition[p.seat];
      const posKlass = pos ? `center-trick-${pos}` : "";
      center.appendChild(el("div", { class: `trick-card ${suitKlass} ${posKlass}` }, p.card));
    }
    return;
  }

  // Peek at a specific trick selected from the tricks strip — same render as
  // showReview but pinned to viewingTrickIndex instead of "last".
  if (viewingTrickIndex !== null && hasHistory && state.trick_history[viewingTrickIndex]) {
    const t = state.trick_history[viewingTrickIndex];
    center.classList.add("reviewing");
    center.appendChild(el("div", { class: "center-trick-label" },
      `Trick #${t.n} — ${t.winner} won · click to return`));
    for (const p of t.plays) {
      const symbol = p.card[0];
      const suitKlass = suitClassFromSymbol(symbol);
      const pos = seatToPosition[p.seat];
      const posKlass = pos ? `center-trick-${pos}` : "";
      center.appendChild(el("div", { class: `trick-card ${suitKlass} ${posKlass}` }, p.card));
    }
    return;
  }

  if (showReview) {
    const last = state.trick_history[state.trick_history.length - 1];
    center.classList.add("reviewing");
    center.appendChild(el("div", { class: "center-trick-label" },
      `Last trick (#${last.n}) — ${last.winner} won · click to return`));
    for (const p of last.plays) {
      const symbol = p.card[0];
      const suitKlass = suitClassFromSymbol(symbol);
      const pos = seatToPosition[p.seat];
      const posKlass = pos ? `center-trick-${pos}` : "";
      center.appendChild(el("div", { class: `trick-card ${suitKlass} ${posKlass}` }, p.card));
    }
    return;
  }

  if (state.complete) {
    const r = state.result;
    const declSym = state.strain_symbol;
    const declKlass = suitClassFromSymbol(declSym);
    const made = r.declarer_tricks >= state.tricks_needed;
    const main = el("div", { class: "result-big" });
    main.appendChild(document.createTextNode(state.level + ""));
    main.appendChild(el("span", { class: declKlass }, declSym));
    main.appendChild(document.createTextNode(
      `   ${made ? "made" : "down"} ${made ? r.declarer_tricks : (state.tricks_needed - r.declarer_tricks)}   (${r.result_str})`
    ));
    center.appendChild(main);
    if (r.score !== undefined) {
      const sign = (n) => (n > 0 ? `+${n}` : String(n));
      const ns = state.tricks_taken.NS;
      const ew = state.tricks_taken.EW;
      const sub = el("div", { class: "result-sub" });
      sub.appendChild(el("div", {}, `Tricks: NS ${ns} · EW ${ew}`));
      sub.appendChild(el("div", {}, `Score: ${sign(r.score)}`));
      sub.appendChild(el("div", {}, `IMPs vs DD: ${sign(r.imps_vs_dd)} (DD ${r.dd_tricks})`));
      center.appendChild(sub);
    }
    return;
  }
  let trickLabel = `Trick ${Math.min(state.trick_number, 13)}`;
  if (!state.user_to_play && state.to_play) trickLabel += ` · ${state.to_play} to play…`;
  if (hasHistory) trickLabel += " · click to see last";
  center.appendChild(el("div", { class: "center-trick-label" }, trickLabel));
  for (const p of state.current_trick) {
    const c = p.card;
    const symbol = c[0];
    const suitKlass = suitClassFromSymbol(symbol);
    const pos = seatToPosition[p.seat];
    const posKlass = pos ? `center-trick-${pos}` : "";
    center.appendChild(el("div", { class: `trick-card ${suitKlass} ${posKlass}` }, c));
  }
}

function renderTrickSummary(state) {
  const div = document.getElementById("trick-summary");
  const ns = state.tricks_taken.NS;
  const ew = state.tricks_taken.EW;
  const need = state.tricks_needed;
  const declarerSide = (state.declarer === "N" || state.declarer === "S") ? "NS" : "EW";
  const declarerHas = declarerSide === "NS" ? ns : ew;
  div.textContent = `Tricks: NS ${ns} · EW ${ew}   (${state.declarer} needs ${need}; has ${declarerHas})`;
}

function renderResult(state) {
  // Result is drawn into the centre of the table by renderTable; the bottom
  // panel stays hidden.
  document.getElementById("result-panel").hidden = true;
}

function render(state) {
  lastState = state;
  bumpImpsIfNeeded(state);
  applyTutorialMask(state);
  // Same spoiler rule as renderContractDisplay — drop the contract from the
  // status line until the auction is fully revealed.
  const statusContract = auctionAnimating ? "?" : state.contract_str;
  document.getElementById("status-line").textContent =
    `${state.scenario} · Deal ${state.board_num} · ${statusContract}`;
  document.getElementById("game").hidden = false;
  document.getElementById("claim-btn").disabled = state.complete;
  document.getElementById("undo-btn").disabled = !state.can_undo;
  document.getElementById("hint-btn").disabled =
    state.complete || !(state.trick_history && state.trick_history.length > 0);
  renderAuction(state);
  renderContractDisplay(state);
  renderTable(state);
  renderTricksStrip(state);
  renderTrickSummary(state);
  renderResult(state);
  renderCoachingPanel();
  if (state.complete) maybeFirePostPlayTip(state);
}

// Fire the post-play assessment tip(s) once per session, as soon as the deal
// completes. Fire-and-forget — presentTipsForStage manages its own Continue
// gate, and the render loop will keep working in parallel.
let postPlayTipFiredFor = null;
async function maybeFirePostPlayTip(state) {
  if (!sessionId || postPlayTipFiredFor === sessionId) return;
  postPlayTipFiredFor = sessionId;
  const tips = Array.isArray(state.tips) ? state.tips : [];
  const role = state.role || "declarer";
  // Runtime context: prepend a small line showing the actual final result.
  // The offline tip prose was authored against BBA's [Result] which may
  // differ from how the user played — this line keeps it accurate.
  const prefix = postPlayContextLine(state);
  await presentTipsForStage(tips, "post-play", role, prefix);
}

function postPlayContextLine(state) {
  if (!state || !state.result) return "";
  const r = state.result;
  const made = r.declarer_tricks;
  const dd = r.dd_tricks;
  const offset = r.result_offset;
  const tag = offset === 0 ? "made exactly"
            : offset > 0  ? `made +${offset}`
            : `down ${-offset}`;
  return `Declarer took ${made} tricks (${tag}; double-dummy par: ${dd}).`;
}

// ---------- actions ----------

let currentScenario = null;

async function loadMenu() {
  const data = await api("/api/menu");
  const menu = document.getElementById("menu");
  menu.innerHTML = "";
  for (const sec of data.sections) {
    const section = el("div", { class: "menu-section" });
    const header = el("div", { class: "menu-section-header" });
    header.appendChild(el("span", {}, sec.title));
    header.appendChild(el("span", { class: "chevron" }, "▶"));
    header.addEventListener("click", () => section.classList.toggle("open"));
    section.appendChild(header);

    const list = el("div", { class: "menu-scenarios" });
    for (const name of sec.scenarios) {
      const btn = el("button", {
        class: "menu-scenario-btn",
        "data-scenario": name,
        onclick: () => onScenarioClick(name, btn),
      }, name.replaceAll("_", " "));
      list.appendChild(btn);
    }
    section.appendChild(list);
    menu.appendChild(section);
  }
}

function highlightActiveScenario(name) {
  for (const b of document.querySelectorAll(".menu-scenario-btn")) {
    b.classList.toggle("active", b.getAttribute("data-scenario") === name);
  }
}

function applyMenuFilter(query) {
  const q = query.trim().toLowerCase();
  for (const section of document.querySelectorAll(".menu-section")) {
    let visibleCount = 0;
    for (const btn of section.querySelectorAll(".menu-scenario-btn")) {
      const name = (btn.getAttribute("data-scenario") || "").toLowerCase();
      const label = btn.textContent.toLowerCase();
      const match = !q || name.includes(q) || label.includes(q);
      btn.classList.toggle("hidden", !match);
      if (match) visibleCount += 1;
    }
    section.classList.toggle("hidden", visibleCount === 0);
    // Auto-open sections during an active search so matches are visible.
    if (q && visibleCount > 0) section.classList.add("open");
  }
}

async function onScenarioClick(name) {
  currentScenario = name;
  highlightActiveScenario(name);
  // When the user clicks a new scenario, reset board index to 0 and start.
  document.getElementById("board-index").value = "0";
  await startSession();
}

async function startSession() {
  if (!currentScenario) return;
  const boardIndex = parseInt(document.getElementById("board-index").value, 10) || 0;
  const role = document.getElementById("role-select").value || "declarer";
  try {
    const data = await api("/api/session", {
      method: "POST",
      body: JSON.stringify({ scenario: currentScenario, board_index: boardIndex, role }),
    });
    sessionId = data.session_id;
    closeScenariosView();
    if (data.board_index != null) {
      document.getElementById("board-index").value = String(data.board_index);
    }
    coachingTips = [];
    tutorialReveals = new Set();
    tutorialContinueResolve = null;
    bidQuizResolve = null;
    startPlayInFlight = false;
    postPlayTipFiredFor = null;
    document.getElementById("inference-panel").hidden = true;
    document.getElementById("result-panel").hidden = true;
    document.getElementById("next-deal-btn").disabled = false;
    document.getElementById("claim-btn").disabled = false;
    document.getElementById("replay-btn").disabled = false;
    document.getElementById("review-btn").disabled = false;
    document.getElementById("picker-hint").textContent = "";
    viewingLastTrick = false;
    viewingTrickIndex = null;
    trickFreeze = null;
    awaitingPlay = false;
    render(data.state);
    animateAuction(data.state);
  } catch (e) {
    alert("Could not start session: " + e.message);
  }
}

async function playCard(suit, rank) {
  if (!sessionId) return;
  // An eager click while the previous trick is still frozen on screen should
  // skip the remaining review pause and play right away.
  if (trickFreeze) endTrickHoldEarly();
  try {
    const data = await api(`/api/session/${sessionId}/play`, {
      method: "POST",
      body: JSON.stringify({ suit, rank }),
    });
    await advanceWithTrickHold(data.state);
  } catch (e) {
    alert("Couldn't play card: " + e.message);
  }
}

// The trick-hold pause is interruptible: endTrickHoldEarly() resolves the
// in-flight hold immediately so the user's next card lands without waiting.
let trickHoldResolve = null;

function endTrickHoldEarly() {
  const r = trickHoldResolve;
  trickHoldResolve = null;
  if (r) r();
}

function holdTrick(ms) {
  return new Promise(resolve => {
    const timer = setTimeout(() => { trickHoldResolve = null; resolve(); }, ms);
    trickHoldResolve = () => { clearTimeout(timer); resolve(); };
  });
}

// If the new state completes a trick, show the four cards in their seats for
// TRICK_HOLD_MS before letting the trick collapse to the next one. The hold is
// skippable — clicking your next card ends it early (see playCard).
async function advanceWithTrickHold(newState) {
  const oldLen = (lastState && lastState.trick_history && lastState.trick_history.length) || 0;
  const newLen = (newState.trick_history || []).length;
  if (newLen > oldLen) {
    trickFreeze = newState.trick_history[newLen - 1];
    render(newState);
    await holdTrick(TRICK_HOLD_MS);
    trickFreeze = null;
  }
  render(newState);
}

async function nextDeal() {
  if (!currentScenario) return;
  const cur = parseInt(document.getElementById("board-index").value, 10) || 0;
  document.getElementById("board-index").value = String(cur + 1);
  await startSession();
}

async function claimRest() {
  if (!sessionId || !lastState || lastState.complete) return;
  const remaining = 13 - (lastState.trick_history || []).length;
  if (remaining < 1) return;
  const ans = prompt(
    `How many tricks are you claiming?\n` +
    `(${remaining} trick${remaining === 1 ? "" : "s"} remaining)`,
    String(remaining)
  );
  if (ans === null) return;  // user cancelled
  const count = parseInt(ans.trim(), 10);
  if (!Number.isFinite(count) || count < 1) {
    alert("Please enter a positive whole number.");
    return;
  }
  try {
    const data = await api(`/api/session/${sessionId}/claim`, {
      method: "POST",
      body: JSON.stringify({ count }),
    });
    render(data.state);
  } catch (e) {
    // Backend's HTTPException detail comes through in e.message; surface it as-is.
    alert(e.message.replace(/^\d+:\s*/, "").replace(/^\{"detail":"|"\}$/g, ""));
  }
}

// ---------- auction animation + tutorial ----------

// Reveal the auction with optional bid-by-bid pauses for coaching prose.
// - Non-coached scenarios (state.coaching === null): show the full auction
//   immediately and wait for the user to click Play. Same as before.
// - Coached scenarios: walk through bids one at a time, pausing for the
//   anchored coaching chunk (if any). The intro chunk fires first.
// Varied, encouraging quiz feedback — senior-friendly: warm on a hit, gentle
// on a miss, never harsh. Picked at random so it doesn't feel canned.
const PRAISE_LINES = [
  "Nice — that's the one.",
  "Well done — that's spot on.",
  "Lovely call.",
  "Exactly right — nicely judged.",
  "That's the winner.",
  "Good eye — that's it.",
  "Perfect.",
];
const RETRY_LINES = [
  "Take another look at your hand and try again.",
  "So close — have another look, you've got this.",
  "Not this time — give your hand another glance and try again.",
];
const pickOne = (arr) => arr[Math.floor(Math.random() * arr.length)];

async function animateAuction(state) {
  auctionAnimationToken += 1;
  const myToken = auctionAnimationToken;
  // If a previous deal's loop is parked on a Continue promise (or a bid
  // quiz), unblock it so it can wake up, see the token mismatch, and exit
  // cleanly.
  if (tutorialContinueResolve) {
    const r = tutorialContinueResolve;
    tutorialContinueResolve = null;
    r();
  }
  if (bidQuizResolve) {
    const r = bidQuizResolve;
    bidQuizResolve = null;
    r("Pass");  // arbitrary; the abandoned loop will exit before using it
  }

  if (!state.coaching || state.coaching.length === 0) {
    auctionAnimating = false;
    auctionVisibleCount = null;
    awaitingPlay = (state.auction || []).length > 0;
    render(lastState);
    return;
  }

  // Bid-by-bid mode. Group coaching chunks by bid_index for fast lookup.
  // post-auction chunks are skipped here — they fire after the opening lead
  // (see startPlay), not during the auction animation.
  const chunkByBid = new Map();
  const introChunks = [];
  for (const ch of state.coaching) {
    if (ch.bid_index === null || ch.bid_index === undefined) introChunks.push(ch);
    else if (ch.bid_index === "post-auction") continue;
    else {
      if (!chunkByBid.has(ch.bid_index)) chunkByBid.set(ch.bid_index, []);
      chunkByBid.get(ch.bid_index).push(ch);
    }
  }

  auctionAnimating = true;
  auctionVisibleCount = 0;
  render(lastState);

  // Intro chunk(s): push them into the coaching panel passively (no Continue
  // gate). The student's first bid quiz appears immediately after, so the
  // user reads the intro and makes the opening call without an extra click.
  // Text-empty chunks (a bare [show S] reveal) skip the panel entirely.
  for (const ch of introChunks) {
    if (myToken !== auctionAnimationToken) return;
    if (!ch.text || !ch.text.trim()) {
      for (const tok of (ch.reveals || [])) {
        for (const seat of expandRevealToken(tok)) tutorialReveals.add(seat);
      }
      if (lastState) render(lastState);
      continue;
    }
    await presentChunkPassive(ch, 0);
  }

  const totalCalls = (state.auction || []).length;
  // After a non-student call we briefly pause if the NEXT call is the
  // student's, so the user can see the opponent/partner bid land on the
  // grid before the bid box appears. Long enough to register, short enough
  // not to feel like dead time.
  const AUCTION_PRE_QUIZ_MS = 800;
  for (let i = 0; i < totalCalls; i++) {
    if (myToken !== auctionAnimationToken) return;
    const call = state.auction[i];
    const chunks = chunkByBid.get(i) || [];
    // Quiz the student on every call from their seat — including Pass. A
     // Pass after partner's raise is a real decision (sign off vs. push to
     // game) and the user has asked to be prompted on it.
    const isStudentBid = call.seat === "S";

    // Quiz the student before each of their non-pass calls. Display-S is
    // always the student after the server's rotation override. Two attempts:
    // first miss prompts "try again"; second miss reveals the textbook call
    // (the anchored coaching chunk follows either way). Pause briefly if
    // the prior bid was revealed without a chunk — the chunk's Continue gate
    // gives a natural pause, so we only need one when there isn't one.
    if (isStudentBid) {
      const prevCall = i > 0 ? state.auction[i - 1] : null;
      const prevHadChunk = i > 0 && chunkByBid.has(i - 1);
      if (prevCall && !prevHadChunk) {
        await sleep(AUCTION_PRE_QUIZ_MS);
        if (myToken !== auctionAnimationToken) return;
      }
      // Acceptable answers: the call actually made, plus any [ACCEPT ...]
      // alternatives on this bid's chunk(s) — for judgment decisions where more
      // than one call is defensible (e.g. Pass or 3NT after 1NT-2NT with 16).
      const altCalls = [];
      for (const ch of chunks) for (const a of (ch.accept || [])) altCalls.push(a);
      const acceptDisplay = [call.call, ...altCalls];
      const acceptableNorm = new Set(acceptDisplay.map(normaliseBidForCompare));
      const multiOk = acceptableNorm.size > 1;
      const acceptPhrase = acceptDisplay.map(formatBidForDisplay).join(" or ");

      let revealed = false;
      for (let attempt = 1; attempt <= 2; attempt++) {
        const pick = await presentBidQuiz(call.call);
        if (myToken !== auctionAnimationToken) return;
        const ok = acceptableNorm.has(normaliseBidForCompare(pick));
        if (ok) {
          coachingTips.push({
            quizResult: "ok",
            text: multiOk
              ? `✓ ${pickOne(PRAISE_LINES)} You bid ${formatBidForDisplay(pick)} — either ${acceptPhrase} is fine here.`
              : `✓ ${pickOne(PRAISE_LINES)} You bid ${formatBidForDisplay(pick)}.`,
          });
          revealed = true;
          break;
        }
        if (attempt === 1) {
          coachingTips.push({
            quizResult: "miss",
            text: `✗ You bid ${formatBidForDisplay(pick)}. ${pickOne(RETRY_LINES)}`,
          });
          if (lastState) render(lastState);
        } else {
          coachingTips.push({
            quizResult: "miss",
            text: multiOk
              ? `✗ You bid ${formatBidForDisplay(pick)}. No worries — here either ${acceptPhrase} works.`
              : `✗ You bid ${formatBidForDisplay(pick)}. No worries — the textbook call here is ${formatBidForDisplay(call.call)}.`,
          });
          revealed = true;
        }
      }
    }

    auctionVisibleCount = i + 1;
    render(lastState);
    // Bid-anchored chunks fire passively — the text lands in the coaching
    // panel and the auction continues after a brief pause. No Continue
    // gate, so partner's response (and the user's own bid explanation)
    // flow naturally with the auction rather than forcing clicks.
    for (const ch of chunks) {
      if (myToken !== auctionAnimationToken) return;
      await presentChunkPassive(ch, AUCTION_CHUNK_PAUSE_MS);
    }
  }

  if (myToken !== auctionAnimationToken) return;
  // Post-auction planning chunks are intentionally NOT shown here — they fire
  // after the opening lead (see startPlay), so the coaching follows the lead
  // and the dummy is on the table when "count your winners" appears.
  auctionAnimating = false;
  auctionVisibleCount = null;
  awaitingPlay = totalCalls > 0;
  render(lastState);
  // Auto-trigger play after a short pause so the user has a beat to absorb
  // the final auction. Clicking Play (or anything that fires startPlay)
  // before the timer fires just runs it sooner — the in-flight guard
  // prevents double-firing.
  if (awaitingPlay) {
    await sleep(AUCTION_END_AUTO_PLAY_MS);
    if (myToken !== auctionAnimationToken) return;
    if (awaitingPlay && !startPlayInFlight) startPlay();
  }
}

const AUCTION_END_AUTO_PLAY_MS = 3000;
const POST_LEAD_TIP_DELAY_MS = 2500;

// Push a coaching chunk's text into the panel, merge its [show] reveals,
// re-render to update the mask, and pause until the user clicks Continue.
async function presentChunk(chunk) {
  if (chunk.text) {
    coachingTips.push({ text: chunk.text });
  }
  for (const tok of (chunk.reveals || [])) {
    for (const seat of expandRevealToken(tok)) {
      tutorialReveals.add(seat);
    }
  }
  if (lastState) render(lastState);
  await waitForContinue();
}

// Passive variant: push chunk text + reveals into the panel without a
// Continue gate. Used for bid-anchored chunks during the auction animation
// — the user reads the explanation as the auction flows past, no clicks.
async function presentChunkPassive(chunk, pauseMs) {
  if (chunk.text) {
    coachingTips.push({ text: chunk.text });
  }
  for (const tok of (chunk.reveals || [])) {
    for (const seat of expandRevealToken(tok)) {
      tutorialReveals.add(seat);
    }
  }
  if (lastState) render(lastState);
  renderCoachingPanel();
  if (pauseMs > 0) await sleep(pauseMs);
}

const AUCTION_CHUNK_PAUSE_MS = 3000;

// Card-play tip labels — prefix each chunk so the panel header signals which
// trigger point the prose belongs to. The tip prose itself stays vantage-pure.
function tipStageLabel(stage, role) {
  if (stage === "auction-end") return "After the bidding";
  if (stage === "pre-lead") return "Choosing your opening lead";
  if (stage === "post-lead") return role === "declarer" ? "Planning the play" : "Reading the lead";
  if (stage === "post-play") return "Looking back";
  return "";
}

async function presentTipsForStage(tips, stage, role, prefix) {
  for (const t of tips) {
    if (t.stage !== stage) continue;
    const label = tipStageLabel(stage, role);
    const head = label ? `${label} — ` : "";
    const pre = prefix ? `${prefix} ` : "";
    const text = `${head}${pre}${t.text}`;
    await presentChunk({ text, reveals: t.reveals || [] });
  }
}

function waitForContinue() {
  return new Promise(resolve => {
    tutorialContinueResolve = resolve;
    renderCoachingPanel();
  });
}

function onContinueClick() {
  const r = tutorialContinueResolve;
  tutorialContinueResolve = null;
  if (r) r();
}

// --- bidding quiz ---
//
// At each of the student's non-pass calls we surface a bidding box and let
// them pick a call. The result is compared to the PBN's actual call; either
// way the trainer reveals the call and shows the anchored coaching chunk.
// Free entry, no legality grayout — feedback is the chunk prose itself.

let bidQuizResolve = null;             // resolve() of the in-flight quiz Promise
const STRAINS = ["C", "D", "H", "S", "NT"];
const STRAIN_GLYPH = { C: "♣", D: "♦", H: "♥", S: "♠", NT: "NT" };

function presentBidQuiz(actualCall) {
  return new Promise(resolve => {
    bidQuizResolve = resolve;
    renderCoachingPanel();
  });
}

function onBidQuizClick(callCode) {
  const r = bidQuizResolve;
  bidQuizResolve = null;
  if (r) r(callCode);
}

// Normalise a call code so "1NT" / "1N" / "1nt" all compare equal, and the
// server's "1♣" → "1C" form lines up with what the bid box emits ("1C").
function normaliseBidForCompare(s) {
  if (!s) return "";
  let t = String(s).toUpperCase().trim();
  t = t.replace("♣", "C").replace("♦", "D").replace("♥", "H").replace("♠", "S");
  if (/^\d+N$/.test(t)) t += "T";
  if (t === "PASS") t = "PASS";
  return t;
}

// Render a bid in user-facing form: "1NT", "1♠", "X", "XX", "Pass".
function formatBidForDisplay(s) {
  if (!s) return "?";
  const norm = normaliseBidForCompare(s);
  if (norm === "PASS") return "Pass";
  if (norm === "X" || norm === "XX") return norm;
  const m = norm.match(/^(\d+)(NT|C|D|H|S)$/);
  if (!m) return s;
  if (m[2] === "NT") return `${m[1]}NT`;
  return `${m[1]}${STRAIN_GLYPH[m[2]]}`;
}

// Rank ordering: 1C=1, 1D=2, 1H=3, 1S=4, 1NT=5, 2C=6, …, 7NT=35. Pass/X/XX
// don't raise the bar — only suit/NT bids do.
const STRAIN_RANK = { C: 1, D: 2, H: 3, S: 4, NT: 5 };
function bidRank(call) {
  const norm = normaliseBidForCompare(call);
  if (norm === "PASS" || norm === "X" || norm === "XX") return 0;
  const m = norm.match(/^(\d+)(NT|C|D|H|S)$/);
  if (!m) return 0;
  return (parseInt(m[1], 10) - 1) * 5 + STRAIN_RANK[m[2]];
}

// Highest level+strain bid revealed so far in the current auction animation.
// auctionVisibleCount is the index of the call ABOUT to be quizzed, so prior
// calls live at indices 0 … auctionVisibleCount-1.
function highestBidSoFarRank() {
  if (!lastState || !Array.isArray(lastState.auction)) return 0;
  let hi = 0;
  const upto = Math.min(auctionVisibleCount || 0, lastState.auction.length);
  for (let i = 0; i < upto; i++) {
    const r = bidRank(lastState.auction[i].call);
    if (r > hi) hi = r;
  }
  return hi;
}

function renderBidBox() {
  const box = el("div", { class: "bid-quiz-box" });
  box.appendChild(el("div", { class: "bid-quiz-prompt" }, "What do you bid?"));

  const minLegalRank = highestBidSoFarRank() + 1;
  const grid = el("div", { class: "bid-quiz-grid" });
  for (let level = 1; level <= 7; level++) {
    for (const strain of STRAINS) {
      const code = `${level}${strain}`;
      const sufficient = bidRank(code) >= minLegalRank;
      const attrs = {
        class: `bid-quiz-btn ${suitClass(strain)}`,
      };
      if (sufficient) {
        attrs.onclick = () => onBidQuizClick(code);
      } else {
        attrs.disabled = true;
      }
      const cell = el("button", attrs);
      cell.appendChild(document.createTextNode(level + ""));
      cell.appendChild(el("span", {}, STRAIN_GLYPH[strain]));
      grid.appendChild(cell);
    }
  }
  box.appendChild(grid);

  const calls = el("div", { class: "bid-quiz-calls" });
  for (const [code, label] of [["Pass", "Pass"], ["X", "Double"], ["XX", "Redouble"]]) {
    calls.appendChild(el("button", {
      class: "bid-quiz-btn bid-quiz-call",
      onclick: () => onBidQuizClick(code),
    }, label));
  }
  box.appendChild(calls);
  return box;
}

let startPlayInFlight = false;
async function startPlay() {
  // Re-entry guard: the Play button stays visible until the post-lead tips
  // finish; a second click (or any render that re-paints the button) would
  // otherwise push the auction-end tip a second time.
  if (startPlayInFlight) return;
  startPlayInFlight = true;
  // Skip past any tutorial pause that's still up, then close the auction
  // overlay so play can begin.
  if (tutorialContinueResolve) {
    const r = tutorialContinueResolve;
    tutorialContinueResolve = null;
    r();
  }

  // Hide the Play button immediately so it can't be re-clicked during the
  // tip Continue gates.
  awaitingPlay = false;
  auctionVisibleCount = null;
  auctionAnimating = false;
  if (lastState) render(lastState);

  // Card-play tips, role-filtered on the server. Stage ordering depends on
  // role:
  //   leader     → auction-end + pre-lead fire BEFORE the lead (user picks)
  //   declarer/defender → lead happens automatically; tips fire AFTER, so the
  //                       user sees the lead + dummy without click-throughs
  const tips = (lastState && Array.isArray(lastState.tips)) ? lastState.tips : [];
  const role = (lastState && lastState.role) || "declarer";

  if (role === "leader") {
    await presentTipsForStage(tips, "auction-end", role);
    await presentTipsForStage(tips, "pre-lead", role);
  }

  // Tutorial reveals end with the auction — server's visible_hands() rule
  // takes over (e.g. dummy hidden until after the opening lead).
  tutorialReveals = new Set();
  if (lastState) render(lastState);
  if (sessionId) {
    try {
      const data = await api(`/api/session/${sessionId}/start-play`, { method: "POST" });
      render(data.state);
      // For declarer/defender the server's auto_play_until_user has just
      // played the opening lead and dummy is now visible — pause briefly so
      // the user can absorb the lead + dummy, then fire the planning coaching.
      if (role === "declarer" || role === "defender") {
        await sleep(POST_LEAD_TIP_DELAY_MS);
        // Post-auction planning chunks fire here (after the lead) so the dummy
        // is on the table. Show TEXT ONLY — the server already reveals the
        // dummy post-lead, so re-applying the chunk's [show] reveals would pin
        // hands to their initial state and re-show cards as play continues.
        const postAuctionChunks = ((lastState && lastState.coaching) || [])
          .filter(c => c.bid_index === "post-auction");
        for (const ch of postAuctionChunks) {
          if (ch.text) await presentChunk({ text: ch.text });
        }
        await presentTipsForStage(tips, "auction-end", role);
        await presentTipsForStage(tips, "post-lead", role);
      }
    } catch (e) {
      console.warn("start-play failed:", e);
    }
  }
  startPlayInFlight = false;
}

function startReview() {
  if (!lastState) return;
  reviewingAuction = true;
  render(lastState);
}

function endReview() {
  if (!reviewingAuction) return;
  reviewingAuction = false;
  if (lastState) render(lastState);
}

async function replayDeal() {
  if (!sessionId || trickFreeze) return;
  try {
    const data = await api(`/api/session/${sessionId}/replay`, { method: "POST" });
    viewingLastTrick = false;
    viewingTrickIndex = null;
    trickFreeze = null;
    render(data.state);
  } catch (e) {
    alert("Couldn't replay: " + e.message);
  }
}

// On-demand Hint: scan the trick history and surface the bridge bookkeeping
// the user can deduce from what's been played — cards still out per suit,
// who has shown out of which suit, and the location of high cards already
// played. No "AI" — just counting. Renders as a coaching-panel card.
function showHint() {
  if (!lastState || !lastState.trick_history) return;
  const lines = computeHintLines(lastState);
  if (lines.length === 0) {
    coachingTips.push({ text: "Hint — nothing inferable yet from the trick history." });
  } else {
    coachingTips.push({ text: "Hint — " + lines.join(" ") });
  }
  renderCoachingPanel();
}

function computeHintLines(state) {
  const SUITS = [
    { sym: "♠", key: "spades" },
    { sym: "♥", key: "hearts" },
    { sym: "♦", key: "diamonds" },
    { sym: "♣", key: "clubs" },
  ];
  const ALL_RANKS = "AKQJT98765432".split("");
  const RANK_ORDER = Object.fromEntries(ALL_RANKS.map((r, i) => [r, i]));

  // 1. Cards played per seat (from the server state)
  const playedBySeat = state.cards_played_by_seat || { N:[], E:[], S:[], W:[] };

  // 2. Show-outs: any seat that failed to follow suit on a trick where the
  //    led suit was something else. (Suit symbol is the first char of card.)
  const showouts = { N:new Set(), E:new Set(), S:new Set(), W:new Set() };
  for (const t of (state.trick_history || [])) {
    if (!t.plays || t.plays.length === 0) continue;
    const ledSuit = t.plays[0].card[0];
    for (const p of t.plays) {
      if (p.card[0] !== ledSuit) showouts[p.seat].add(ledSuit);
    }
  }

  // 3. For each suit, compute outstanding (unseen + unplayed) high cards.
  //    "Unseen": not in any hand that's visible to the user AND not played.
  const lines = [];
  for (const s of SUITS) {
    // Collect all ranks played in this suit
    const played = new Set();
    for (const cards of Object.values(playedBySeat)) {
      for (const c of cards) {
        if (c[0] === s.sym) played.add(c.slice(1));
      }
    }
    // Collect all ranks visible in the hands the user can see
    const visible = new Set();
    for (const seat of ["N", "E", "S", "W"]) {
      const hand = (state.hands || {})[seat];
      if (!hand) continue;
      const ranks = hand[s.key] || "";
      for (const r of ranks.split("")) visible.add(r);
    }
    // Outstanding = all - played - visible
    const outstanding = ALL_RANKS.filter(r => !played.has(r) && !visible.has(r));
    if (outstanding.length === 0) continue;
    // Pick out honors (A,K,Q,J,T) and total length
    const honors = outstanding.filter(r => "AKQJT".includes(r)).sort((a,b) => RANK_ORDER[a] - RANK_ORDER[b]);
    const totalOut = outstanding.length;
    let line = `${s.sym}${honors.join("")}${honors.length < totalOut ? "+" : ""} out (${totalOut} card${totalOut===1?"":"s"})`;
    lines.push(line + ".");
  }

  // 4. Show-out callouts
  for (const seat of ["N", "E", "S", "W"]) {
    if (showouts[seat].size === 0) continue;
    const labeled = userRelativeLabel(seat, state);
    if (!labeled) continue;  // only call out hidden hands (LHO/RHO from user's seat)
    const suits = Array.from(showouts[seat]).join("");
    lines.push(`${labeled} void in ${suits}.`);
  }

  return lines;
}

function userRelativeLabel(seat, state) {
  // Translate a display-frame seat letter into user-relative term. The user
  // always sits at display-S; LHO=W, Partner=N, RHO=E in display frame.
  // We only emit hints about seats the user can't see (opponents).
  const map = { W: "LHO", E: "RHO" };
  return map[seat] || null;  // skip N (partner/dummy) and S (own)
}

async function undoLast() {
  if (!sessionId || trickFreeze) return;
  try {
    const data = await api(`/api/session/${sessionId}/undo`, { method: "POST" });
    viewingLastTrick = false;
    viewingTrickIndex = null;
    render(data.state);
  } catch (e) {
    alert("Couldn't undo: " + e.message);
  }
}

// ---------- session totals ----------

let sessionImps = 0;                 // Running IMPs vs DD since this tab was opened
let impsCountedForSession = null;    // session_id whose IMPs have already been folded in

function renderSessionImps() {
  const el = document.getElementById("session-imps");
  if (!el) return;
  const sign = sessionImps > 0 ? "+" : "";
  el.textContent = `Session IMPs: ${sign}${sessionImps}`;
}
function bumpImpsIfNeeded(state) {
  if (!state || !state.complete) return;
  if (impsCountedForSession === sessionId) return;
  const imps = state.result && state.result.imps_vs_dd;
  if (typeof imps !== "number") return;
  sessionImps += imps;
  impsCountedForSession = sessionId;
  renderSessionImps();
}

// ---------- coaching panel ----------

let coachingTips = [];               // Array<{text}> — accumulates across the deal

// The coaching panel is a fixed overlay sitting on top of the sidebar's slot,
// so the sidebar is hidden whenever the panel is visible.
function syncSidebarVisibility() {
  const infVis = !document.getElementById("inference-panel").hidden;
  document.getElementById("sidebar").hidden = infVis;
}

// The coaching panel's title bar doubles as a Coaching/Scenarios toggle. Since
// the panel overlays the sidebar, we can't just reveal the sidebar underneath;
// instead we move the scenario search + menu into the panel itself. Clicking
// "Coaching" switches to the scenario picker; the picker title is NOT a toggle
// — picking a scenario starts a deal and returns to Coaching.
let scenariosViewOpen = false;

function setInferenceTitle(text, toggleable) {
  document.getElementById("inference-title-text").textContent = text;
  document.getElementById("inference-title").classList.toggle("no-toggle", !toggleable);
}

function openScenariosView() {
  if (scenariosViewOpen) return;
  scenariosViewOpen = true;
  const panelScenarios = document.getElementById("panel-scenarios");
  panelScenarios.appendChild(document.getElementById("sidebar-search"));
  panelScenarios.appendChild(document.getElementById("menu"));
  const body = document.getElementById("inference-body");
  body.hidden = true;
  body.style.display = "none";  // .coaching-tips-list sets display:flex, which beats [hidden]
  panelScenarios.hidden = false;
  document.getElementById("inference-panel").hidden = false;
  setInferenceTitle("Scenarios", false);
  syncSidebarVisibility();
  const search = document.getElementById("search-input");
  if (search) search.focus();
}

function closeScenariosView() {
  if (!scenariosViewOpen) return;
  scenariosViewOpen = false;
  // Return the search + menu to the sidebar (after its heading).
  const sidebar = document.getElementById("sidebar");
  sidebar.appendChild(document.getElementById("sidebar-search"));
  sidebar.appendChild(document.getElementById("menu"));
  document.getElementById("panel-scenarios").hidden = true;
  const body = document.getElementById("inference-body");
  body.hidden = false;
  body.style.display = "";
  setInferenceTitle("Coaching", true);
}

function onInferenceTitleClick() {
  // Only "Coaching" toggles to the scenario picker; "Scenarios" is not a toggle.
  if (!scenariosViewOpen) openScenariosView();
}

function renderCoachingPanel() {
  const panel = document.getElementById("inference-panel");
  const body = document.getElementById("inference-body");
  // While the scenario picker is up, keep the panel visible and leave the tip
  // log alone — it reappears when a scenario is chosen (closeScenariosView).
  if (scenariosViewOpen) {
    panel.hidden = false;
    syncSidebarVisibility();
    return;
  }
  if (coachingTips.length === 0 && !tutorialContinueResolve && !bidQuizResolve) {
    panel.hidden = true;
    syncSidebarVisibility();
    return;
  }
  panel.hidden = false;
  body.innerHTML = "";
  body.classList.add("coaching-tips-list");
  for (const tip of coachingTips) {
    const cls = "coach-tip-card" +
      (tip.quizResult === "ok" ? " coach-tip-ok" :
       tip.quizResult === "miss" ? " coach-tip-miss" : "");
    const card = el("div", { class: cls });
    const textDiv = el("div", { class: "coach-tip-text" });
    appendTextWithColoredSuits(textDiv, tip.text);
    card.appendChild(textDiv);
    body.appendChild(card);
  }
  if (bidQuizResolve) {
    body.appendChild(renderBidBox());
  } else if (tutorialContinueResolve) {
    // When the deal is over (post-play tip is the final thing), Continue
    // does nothing useful — relabel and rewire it to start the next deal.
    const isEndOfDeal = lastState && lastState.complete;
    const cont = el("button", {
      id: "coach-continue-btn",
      class: "primary",
      onclick: isEndOfDeal ? () => { onContinueClick(); nextDeal(); } : onContinueClick,
    }, isEndOfDeal ? "Next deal" : "Continue");
    body.appendChild(cont);
  }
  // Auto-scroll to the newest tip so older ones can be reached above.
  body.scrollTop = body.scrollHeight;
  syncSidebarVisibility();
}

// ---------- init ----------

document.addEventListener("DOMContentLoaded", async () => {
  document.getElementById("next-deal-btn").addEventListener("click", nextDeal);
  document.getElementById("claim-btn").addEventListener("click", claimRest);
  document.getElementById("undo-btn").addEventListener("click", undoLast);
  document.getElementById("replay-btn").addEventListener("click", replayDeal);
  document.getElementById("hint-btn").addEventListener("click", showHint);
  document.getElementById("inference-title").addEventListener("click", onInferenceTitleClick);
  const reviewBtn = document.getElementById("review-btn");
  // Pointer capture keeps pointerup firing on the button even if the cursor
  // drifts off mid-press, so the auction stays up while the user holds.
  reviewBtn.addEventListener("pointerdown", (ev) => {
    if (reviewBtn.disabled) return;
    reviewBtn.setPointerCapture(ev.pointerId);
    startReview();
  });
  reviewBtn.addEventListener("pointerup", endReview);
  reviewBtn.addEventListener("pointercancel", endReview);
  window.addEventListener("blur", endReview);
  document.getElementById("search-input").addEventListener("input", (ev) => {
    applyMenuFilter(ev.target.value);
  });
  document.getElementById("role-select").addEventListener("change", () => {
    if (currentScenario) nextDeal();
  });

  await loadMenu();
});
