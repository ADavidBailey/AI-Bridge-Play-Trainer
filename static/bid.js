// Bidding-with-Claude page. Left panel toggles Scenario menu / Coaching chat.
// Compass-free table: You at the bottom, Claude (partner) top, LHO left, RHO right;
// auction in the center. Calls reveal one at a time via /step.
"use strict";

const SEATS = ["W", "N", "E", "S"];   // auction columns: LHO · Claude · RHO · You (W-N-E-S, clockwise)
const STRAINS = ["C", "D", "H", "S", "NT"];
const SUIT_SYM = { S: "♠", H: "♥", D: "♦", C: "♣" };
const SUIT_CLASS = { S: "suit-spade", H: "suit-heart", D: "suit-diamond", C: "suit-club" };
// Seats by role, not compass — the user always sits at the bottom; the board may rotate.
const ROLE = { S: "You", N: "Claude", W: "LHO", E: "RHO" };
const SEAT_NAME = { N: "Claude", E: "RHO", W: "LHO" };

let currentScenario = null;
let sessionId = null;
let selLevel = null;   // BBO-style bid box: the level the user tapped, awaiting a strain
// BBA-comparison pop-up: on/off + last position, both remembered across deals.
let compareEnabled = true;
let comparePos = null;
// Hand display: false = hand diagrams (suit rows), true = pictures of cards.
let cardsMode = false;
// Show ten as "T" (J T 9) instead of "10" (J 10 9).
let tenAsT = false;
// Which scenario's authored chat has been seeded into #table-chat (so we seed
// once per scenario, not on every re-render or same-scenario Redeal).
let chatScenario = null;
try {
  const e = localStorage.getItem("bid.compareEnabled");
  if (e !== null) compareEnabled = e === "1";
  const p = localStorage.getItem("bid.comparePos");
  if (p) comparePos = JSON.parse(p);
  const cm = localStorage.getItem("bid.cardsMode");
  if (cm !== null) cardsMode = cm === "1";
  const tt = localStorage.getItem("bid.tenAsT");
  if (tt !== null) tenAsT = tt === "1";
} catch (_) {}

// Rank as shown to the user — "10" or "T" per the option.
const showRank = (r) => (tenAsT && r === "10" ? "T" : r);

const $ = (id) => document.getElementById(id);
function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k === "onclick") e.onclick = v;
    else if (k === "html") e.innerHTML = v;
    else e.setAttribute(k, v);
  }
  for (const kid of kids) e.append(kid?.nodeType ? kid : document.createTextNode(kid));
  return e;
}

function callNode(call) {
  const span = el("span");
  const m = call.match(/^([1-7])(C|D|H|S)$/);
  if (m) { span.append(m[1]); span.append(el("span", { class: SUIT_CLASS[m[2]] }, SUIT_SYM[m[2]])); }
  else { span.append(call); if (call === "Pass") span.className = "pass"; }
  return span;
}

async function api(method, url, body) {
  const opt = { method, headers: { "Content-Type": "application/json" } };
  if (body) opt.body = JSON.stringify(body);
  const r = await fetch(url, opt);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return r.json();
}

// ---- left panel ----

function showSide(which) {
  $("scenario-view").hidden = which !== "scenario";
  $("coaching-view").hidden = which !== "coaching";
  $("tab-scenario").classList.toggle("active", which === "scenario");
  $("tab-coaching").classList.toggle("active", which === "coaching");
}

async function loadMenu() {
  const data = await api("GET", "/api/menu");
  const menu = $("menu");
  menu.innerHTML = "";
  for (const sec of data.sections) {
    const section = el("div", { class: "menu-section" });
    const header = el("div", { class: "menu-section-header" },
      el("span", {}, sec.title), el("span", { class: "chevron" }, "▶"));
    header.onclick = () => section.classList.toggle("open");
    section.append(header);
    const list = el("div", { class: "menu-scenarios" });
    for (const name of sec.scenarios) {
      list.append(el("button", {
        class: "menu-scenario-btn", "data-scenario": name,
        onclick: () => onScenario(name),
      }, name.replaceAll("_", " ")));
    }
    section.append(list);
    menu.append(section);
  }
}

function highlightActiveScenario(name) {
  for (const b of document.querySelectorAll(".menu-scenario-btn"))
    b.classList.toggle("active", b.getAttribute("data-scenario") === name);
}

function applyMenuFilter(query) {
  const q = query.trim().toLowerCase();
  for (const section of document.querySelectorAll(".menu-section")) {
    let visible = 0;
    for (const btn of section.querySelectorAll(".menu-scenario-btn")) {
      const match = !q || (btn.getAttribute("data-scenario") || "").toLowerCase().includes(q)
        || btn.textContent.toLowerCase().includes(q);
      btn.classList.toggle("hidden", !match);
      if (match) visible += 1;
    }
    section.classList.toggle("hidden", visible === 0);
    if (q && visible > 0) section.classList.add("open");
  }
}

// ---- dealing / bidding ----

async function onScenario(name) {
  currentScenario = name;
  highlightActiveScenario(name);
  await deal();
}

async function deal() {
  if (!currentScenario) return;
  showErr("");
  $("setup-block").hidden = true;
  $("table-wrap").hidden = false;
  showSide("coaching");           // surface the partner's thinking while we play
  setBusy("Dealing…");
  try {
    const data = await api("POST", "/api/bid/session", { scenario: currentScenario, board_index: null });
    sessionId = data.session_id;
    render(data.state);
    seedScenarioChat(data.state);   // load this scenario's authored chat into #table-chat
    await runSteps();
  } catch (e) { showErr("Couldn't deal: " + e.message); }
}

async function makeCall(call) {
  try {
    const data = await api("POST", `/api/bid/session/${sessionId}/call`, { call });
    render(data.state);
    await runSteps();
  } catch (e) { showErr("Bid rejected: " + e.message); if (window._lastState) render(window._lastState); }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function runSteps() {
  while (true) {
    const st = window._lastState;
    if (!st || st.complete || st.user_to_play) return;
    setBusy(`${SEAT_NAME[st.to_play] || st.to_play} ${st.to_play === "N" ? "is thinking…" : "is bidding…"}`);
    let data;
    try { data = await api("POST", `/api/bid/session/${sessionId}/step`); }
    catch (e) { showErr("Bidding error: " + e.message); return; }
    render(data.state);
    if (!data.state.complete && !data.state.user_to_play) await sleep(500);
  }
}

// ---- rendering ----

function buildAuction(container, calls, dealer, toPlay, markLast, diffIndex = -1) {
  container.innerHTML = "";
  for (const s of SEATS) container.append(el("div", { class: "head" }, ROLE[s]));
  for (let i = 0; i < SEATS.indexOf(dealer); i++) container.append(el("div", { class: "cell" }));
  calls.forEach((c, i) => {
    let cls = "cell";
    if (markLast && i === calls.length - 1) cls += " new";
    if (i === diffIndex) cls += " diff";
    const cell = el("div", { class: cls });
    cell.append(callNode(c.call));
    container.append(cell);
  });
  if (toPlay) container.append(el("div", { class: "cell turn" }, "?"));
}

function handRows(target, hand) {
  target.innerHTML = "";
  for (const suit of ["S", "H", "D", "C"]) {
    const row = el("div", { class: `hand-suit ${SUIT_CLASS[suit]}` });
    row.append(el("span", { class: "suit-symbol" }, SUIT_SYM[suit]));
    row.append(el("span", { class: SUIT_CLASS[suit] }, (hand[suit] || []).map(showRank).join(" ") || "—"));
    target.append(row);
  }
}

function faceDown(slot, role) {
  slot.className = "seat face-down";
  slot.innerHTML = "";
  slot.append(el("div", { class: "seat-name" }, role));
}

// One clipped card corner: rank (bigger) over suit pip, colored via .suit-*.
function cardEl(rank, suit) {
  const c = el("div", { class: "pcard " + SUIT_CLASS[suit] });
  c.append(el("span", { class: "rank" }, showRank(rank)));
  c.append(el("span", { class: "pip" }, SUIT_SYM[suit]));
  return c;
}
function cardRow(pairs) {
  const r = el("div", { class: "pcard-row" });
  for (const [rk, st] of pairs) r.append(cardEl(rk, st));
  return r;
}
// Pictures-of-cards layout. fanned: one overlapping row of all 13 (You/Claude);
// otherwise one row per suit (LHO/RHO) — matches BBO's "pictures of cards".
function handCards(target, hand, fanned) {
  target.innerHTML = "";
  if (fanned) {
    const pairs = [];
    for (const s of ["S", "H", "C", "D"]) for (const rk of (hand[s] || [])) pairs.push([rk, s]);
    target.append(pairs.length ? cardRow(pairs) : el("div", {}, "—"));
  } else {
    for (const s of ["S", "H", "D", "C"]) {
      if ((hand[s] || []).length) target.append(cardRow(hand[s].map((rk) => [rk, s])));
    }
  }
}

function seatHand(slot, role, hand, hcp, goldTurn, fanned) {
  slot.className = "seat";
  slot.innerHTML = "";
  const wrap = el("div", { class: "hand-wrap" });
  if (cardsMode) handCards(wrap, hand, fanned);
  else handRows(wrap, hand);
  slot.append(wrap);
  slot.append(el("div", { class: "seat-name" + (goldTurn ? " your-turn" : ""),
                          html: `${role} — <span class="hcp">${hcp} HCP</span>` }));
}

function renderBidbox(state) {
  const box = $("bidbox"), row1 = $("bidrow1"), row2 = $("bidrow2");
  if (!state.user_to_play) { box.hidden = true; selLevel = null; return; }
  box.hidden = false;
  const legal = new Set(state.legal);
  row1.innerHTML = ""; row2.innerHTML = "";

  const pass = el("button", { class: "bidbtn" }, "Pass");
  pass.disabled = !legal.has("Pass");
  if (!pass.disabled) pass.onclick = () => { selLevel = null; makeCall("Pass"); };
  row1.append(pass);
  for (let n = 1; n <= 7; n++) {
    const b = el("button", { class: "bidbtn" + (selLevel === n ? " sel" : "") }, String(n));
    b.disabled = !STRAINS.some((st) => legal.has(`${n}${st}`));
    if (!b.disabled) b.onclick = () => { selLevel = n; renderBidbox(state); };
    row1.append(b);
  }
  for (const st of STRAINS) {
    const label = st === "NT" ? el("span", {}, "NT")
                              : el("span", { class: SUIT_CLASS[st] }, SUIT_SYM[st]);
    const b = el("button", { class: "bidbtn" }, label);
    const active = selLevel !== null && legal.has(`${selLevel}${st}`);
    b.disabled = !active;
    if (active) b.onclick = () => { const lv = selLevel; selLevel = null; makeCall(`${lv}${st}`); };
    row2.append(b);
  }
  for (const [c, label] of [["X", "X"], ["XX", "XX"]]) {
    const b = el("button", { class: "bidbtn" }, label);
    b.disabled = !legal.has(c);
    if (!b.disabled) b.onclick = () => { selLevel = null; makeCall(c); };
    row2.append(b);
  }
}

const GLYPH_SUIT = { "♠": "S", "♥": "H", "♦": "D", "♣": "C" };
// Append `text` to `node`, wrapping any ♠♥♦♣ glyph in its suit color.
function appendColoredSuits(node, text) {
  for (const part of String(text).split(/([♠♥♦♣])/)) {
    if (GLYPH_SUIT[part]) node.append(el("span", { class: SUIT_CLASS[GLYPH_SUIT[part]] }, part));
    else if (part) node.append(part);
  }
}

function renderChat(state) {
  const chat = $("chat");
  chat.innerHTML = "";
  const msgs = state.calls.filter((c) => c.seat === "N" && c.reason);
  if (!msgs.length && !(state.complete && state.review)) {
    chat.append(el("div", { class: "chat-empty" }, "Your partner's thinking will appear here as the auction goes."));
    return;
  }
  for (const c of msgs) {
    const m = el("div", { class: "chat-msg" });
    m.append(el("span", { class: "who" }, "Claude"));
    m.append("Bid "); m.append(callNode(c.call));
    m.append(" — ");
    appendColoredSuits(m, c.reason);
    if (c.ms) m.append(` (${(c.ms / 1000).toFixed(1)}s)`);
    chat.append(m);
  }
  if (state.complete && state.review) {
    const m = el("div", { class: "chat-msg" });
    m.append(el("span", { class: "who" }, "Claude · review"));
    appendColoredSuits(m, state.review);
    chat.append(m);
  }
  chat.scrollTop = chat.scrollHeight;
}

// Render the double-dummy grid: tricks each declarer (rows) makes per strain
// (cols), with the best result on the board flagged green.
function renderDDTable(dd) {
  const tbl = $("dd-table");
  tbl.innerHTML = "";
  if (!dd) { tbl.hidden = true; return; }
  tbl.hidden = false;
  const head = el("tr");
  head.append(el("th", {}, ""));
  for (const s of dd.strains) {
    head.append(el("th", {}, s === "NT" ? el("span", {}, "NT")
                                        : el("span", { class: SUIT_CLASS[s] }, SUIT_SYM[s])));
  }
  tbl.append(head);
  for (const row of dd.rows) {
    const tr = el("tr");
    tr.append(el("th", {}, ROLE[row.seat]));
    for (const n of row.tricks) {
      tr.append(el("td", { class: n === dd.max ? "best" : "" }, String(n)));
    }
    tbl.append(tr);
  }
}

// ---- BBA-comparison pop-up (draggable, toggle-able) ----

function positionComparePopup() {
  const p = $("compare-popup");
  if (comparePos) {
    p.style.left = comparePos.left + "px"; p.style.top = comparePos.top + "px"; p.style.right = "auto";
    clampComparePopup();
  } else {                       // default: upper-right, clear of the table
    p.style.left = "auto"; p.style.right = "1.2rem"; p.style.top = "5rem";
  }
}
function clampComparePopup() {
  const p = $("compare-popup");
  if (p.style.left && p.style.left !== "auto") {
    const x = Math.max(0, Math.min(parseInt(p.style.left), window.innerWidth - p.offsetWidth));
    const y = Math.max(0, Math.min(parseInt(p.style.top), window.innerHeight - p.offsetHeight));
    p.style.left = x + "px"; p.style.top = y + "px";
  }
}
function showComparePopup() { $("compare-popup").hidden = false; positionComparePopup(); updateReopenBtn(); }
function hideComparePopup() { $("compare-popup").hidden = true; updateReopenBtn(); }

// Offer a "Show BBA comparison" button whenever the auction's done but the
// pop-up is closed — so dismissing the dialog (✕) is never a dead end.
function updateReopenBtn() {
  const complete = window._lastState && window._lastState.complete;
  $("show-compare-btn").hidden = !(complete && $("compare-popup").hidden);
}

// The checkbox is the "auto-show when the auction ends" preference (persisted).
function setCompareEnabled(on) {
  compareEnabled = on;
  $("compare-toggle").checked = on;
  try { localStorage.setItem("bid.compareEnabled", on ? "1" : "0"); } catch (_) {}
  if (on && window._lastState && window._lastState.complete) showComparePopup();
  else hideComparePopup();
}

function makeDraggable(popup, handle, onDrop) {
  let dragging = false, offX = 0, offY = 0;
  handle.addEventListener("mousedown", (e) => {
    if (e.target.closest(".popup-close")) return;   // ✕ isn't a drag handle
    dragging = true;
    const r = popup.getBoundingClientRect();
    offX = e.clientX - r.left; offY = e.clientY - r.top;
    popup.style.left = r.left + "px"; popup.style.top = r.top + "px"; popup.style.right = "auto";
    e.preventDefault();
  });
  window.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    const x = Math.max(0, Math.min(e.clientX - offX, window.innerWidth - popup.offsetWidth));
    const y = Math.max(0, Math.min(e.clientY - offY, window.innerHeight - popup.offsetHeight));
    popup.style.left = x + "px"; popup.style.top = y + "px";
  });
  window.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false;
    if (onDrop) onDrop({ left: parseInt(popup.style.left), top: parseInt(popup.style.top) });
  });
}

function renderResult(state) {
  // Populate the pop-up's contents; it's shown only if the toggle is on.
  // Show only BBA's auction, flagging the first call where it diverges from
  // the table's actual auction (which is already on the board above).
  const actual = state.calls.map((c) => c.call);
  const bba = state.bba_compare.map((c) => c.call);
  let diff = -1;
  for (let i = 0; i < Math.max(actual.length, bba.length); i++) {
    if (actual[i] !== bba[i]) { diff = i; break; }
  }
  buildAuction($("auc-bba"), state.bba_compare, state.dealer, null, false, diff);

  const note = $("compare-note");
  note.innerHTML = "";
  if (diff === -1) {
    note.textContent = "BBA would have bid exactly the same.";
  } else {
    note.append("First difference: ");
    if (bba[diff] !== undefined) { note.append("BBA bid "); note.append(callNode(bba[diff])); }
    else note.append("BBA's auction ended sooner");
    if (actual[diff] !== undefined) {
      note.append(" where the table bid ");
      note.append(callNode(actual[diff]));
    }
    note.append(".");
  }

  renderDDTable(state.dd);

  const t = state.timings || {};
  const secs = (ms) => (ms == null ? "—" : (ms / 1000).toFixed(1) + "s");
  const list = (a) => (a && a.length ? a.map(secs).join(", ") : "—");
  $("timing").textContent =
    `Timing — Claude bids: ${list(t.claude_bid_ms)} · robot bids: ${list(t.bba_bid_ms)}`
    + ` · BBA compare: ${secs(t.compare_ms)} · Claude review: ${secs(t.review_ms)}`;

  if (compareEnabled) showComparePopup(); else hideComparePopup();
}

// BBO-style left column. Board badge (number + dealer "D" + vulnerability shown
// by red edges) and the contract with E/W tricks left, N/S tricks below. Always
// visible (unlike BBO, which hides the contract until there is one).
// Wrap any ♠♥♦♣ glyph in its suit-color class, returning an HTML string.
function colorSuitHtml(text) {
  return String(text).replace(/[♠♥♦♣]/g, (g) => `<span class="${SUIT_CLASS[GLYPH_SUIT[g]]}">${g}</span>`);
}

function renderTableInfo(state) {
  const v = (state.vul || "").toLowerCase().replace(/[^a-z]/g, "");
  const both = v === "both" || v === "all";
  const nsVul = both || v === "ns";
  const ewVul = both || v === "ew";
  const RED = "#c0392b", PALE = "#f3f3f3";
  const nv = nsVul ? RED : PALE, ev = ewVul ? RED : PALE;
  const dealer = (state.dealer || "N").toUpperCase();
  const dealerOnRed = ((dealer === "N" || dealer === "S") && nsVul) ||
                      ((dealer === "E" || dealer === "W") && ewVul);
  $("board-badge").innerHTML =
    `<div class="bb-diamond" style="--nv:${nv};--sv:${nv};--ev:${ev};--wv:${ev}"></div>` +
    // Diagonals from each outer corner to the matching inner-square corner. The
    // inner square is inset 24%, so its corners sit at 24%/76% of the box.
    `<svg class="bb-lines" viewBox="0 0 100 100" preserveAspectRatio="none">` +
      `<line x1="0" y1="0" x2="24" y2="24"></line>` +
      `<line x1="100" y1="0" x2="76" y2="24"></line>` +
      `<line x1="100" y1="100" x2="76" y2="76"></line>` +
      `<line x1="0" y1="100" x2="24" y2="76"></line>` +
    `</svg>` +
    `<div class="bb-center">${state.board_index + 1}</div>` +
    `<div class="bb-d ${dealer.toLowerCase()}${dealerOnRed ? " on-red" : ""}">D</div>`;

  // Contract + tricks appear only once the auction has produced a contract;
  // trick boxes are blank (no card play yet — they fill in during Phase 2).
  const ct = $("contract-tricks");
  if (state.complete && state.contract && state.contract !== "Passed out") {
    const [call, decl] = state.contract.split(" by ");
    ct.hidden = false;
    ct.innerHTML =
      `<div class="ct-trick ct-ew" title="E-W tricks won"></div>` +
      `<div class="ct-contract" title="Contract">${colorSuitHtml(call)}` +
        `<div class="ct-decl">${decl ? (ROLE[decl] || decl) : ""}</div></div>` +
      `<div class="ct-trick ct-ns" title="N-S tricks won"></div>`;
  } else {
    ct.hidden = true;
  }
}

function render(state) {
  window._lastState = state;
  $("status").hidden = true;
  $("setup-block").hidden = true;   // startup intro is shown only before the first deal
  $("table-wrap").hidden = false;
  $("table-grid").classList.toggle("cards", cardsMode);   // wide fanned hands + narrow table

  $("meta-line").innerHTML =
    `<strong>${state.scenario.replaceAll("_", " ")}</strong> &nbsp;·&nbsp; deal ${state.board_index + 1}/${state.n_boards}`;
  renderTableInfo(state);

  $("contract-display").innerHTML = "";
  if (state.complete) {
    const main = el("div", { class: "contract-line-main" });
    main.append(callNode(state.contract.split(" ")[0].replace(/(X+)$/, "")));
    $("contract-display").append(main, el("div", { class: "contract-line-sub" }, state.contract));
  } else {
    $("contract-display").append(
      el("div", { class: "contract-line-sub" }, `Dealer ${ROLE[state.dealer]}`),
      el("div", { class: "contract-line-sub" }, `${state.vul} vul`));
  }

  // Seats: reveal all four hands when the auction ends; otherwise hide the others.
  if (state.complete) {
    seatHand($("slot-top"), ROLE.N, state.all_hands.N, state.all_hcp.N, false, true);
    seatHand($("slot-left"), ROLE.W, state.all_hands.W, state.all_hcp.W, false, false);
    seatHand($("slot-right"), ROLE.E, state.all_hands.E, state.all_hcp.E, false, false);
    seatHand($("slot-bottom"), ROLE.S, state.all_hands.S, state.all_hcp.S, false, true);
  } else {
    faceDown($("slot-top"), ROLE.N);
    faceDown($("slot-left"), ROLE.W);
    faceDown($("slot-right"), ROLE.E);
    seatHand($("slot-bottom"), "You", state.south_hand, state.south_hcp, state.user_to_play, true);
  }

  const center = $("center"); center.innerHTML = "";
  const auc = el("div", { class: "auc" });
  buildAuction(auc, state.calls, state.dealer, state.complete ? null : state.to_play, true);
  center.append(auc);

  renderChat(state);

  // Undo is offered only when it's safe to take back: the user has bid, and
  // we're not mid-auto-step (their turn, or the auction's finished).
  $("undo-btn").disabled = !(state.can_undo && (state.user_to_play || state.complete));

  if (state.complete) { $("bidbox").hidden = true; renderResult(state); }
  else { hideComparePopup(); renderBidbox(state); }
}

// ---- report a problem ----
// The note is all the user supplies; the server reads scenario/deal/auction
// from the live session and files a GitHub issue (labelled "user-feedback").

function openReportModal() {
  if (!sessionId) return;
  const sendBtn = $("report-send");
  $("report-text").value = "";
  $("report-status").textContent = "";
  sendBtn.disabled = false;
  sendBtn.textContent = "Send";
  delete sendBtn.dataset.sent;
  $("report-cancel").hidden = false;
  const p = $("report-popup");
  if (!p.style.left) {   // first open: center-ish; afterwards reopen where left
    p.style.left = Math.max(0, (window.innerWidth - 400) / 2) + "px";
    p.style.top = "110px"; p.style.right = "auto";
  }
  p.hidden = false;
  $("report-text").focus();
}

function closeReportModal() { $("report-popup").hidden = true; }

async function sendReport() {
  if (!sessionId) return;
  const sendBtn = $("report-send");
  // After a successful send the button reads "Close" and just dismisses the dialog.
  if (sendBtn.dataset.sent) { closeReportModal(); return; }
  const note = ($("report-text").value || "").trim();
  const statusEl = $("report-status");
  if (!note) { statusEl.textContent = "Please describe what looks wrong."; return; }
  sendBtn.disabled = true;
  statusEl.textContent = "Sending…";
  try {
    await api("POST", `/api/bid/session/${sessionId}/report`, { note });
    statusEl.textContent = "Thanks — your report was sent.";
    sendBtn.textContent = "Close";
    sendBtn.dataset.sent = "1";
    $("report-cancel").hidden = true;
  } catch (e) {
    statusEl.textContent = "Couldn't send — " + e.message;
  } finally {
    sendBtn.disabled = false;
  }
}

// Take back the user's last call and everything the robots/Claude did after
// it. The server returns the user-to-play state, so there are no steps to run.
async function undo() {
  if (!sessionId) return;
  showErr("");
  $("undo-btn").disabled = true;
  try {
    const data = await api("POST", `/api/bid/session/${sessionId}/undo`);
    render(data.state);
  } catch (e) { showErr("Couldn't undo: " + e.message); }
}

function setBusy(msg) {
  $("bidbox").hidden = true;
  $("status").hidden = false;
  $("status-text").textContent = msg;
  $("undo-btn").disabled = true;   // no undo while robots/Claude are bidding
}
function showErr(msg) { const e = $("page-err"); e.hidden = !msg; e.textContent = msg; }

// Bounded-responsive table: scale the whole table (--k) to fill the main area,
// leaving gutters for the Undo/Redeal and Report buttons, clamped so cards stay
// readable on a small window and don't get cartoonish on a big one.
function fitTable() {
  const main = document.querySelector("main");
  if (!main) return;
  const avail = main.clientWidth - 240;     // ~room for the side-control gutters
  let k = avail / 680;                       // 680px is the k=1 table footprint
  k = Math.max(0.7, Math.min(k, 1.8));       // floor (readable) … cap (not huge)
  document.documentElement.style.setProperty("--k", k.toFixed(3));
}
window.addEventListener("resize", fitTable);

$("tab-scenario").onclick = () => showSide("scenario");
$("tab-coaching").onclick = () => showSide("coaching");
$("redeal").onclick = deal;
$("undo-btn").onclick = undo;
$("report-btn").onclick = openReportModal;
$("report-cancel").onclick = closeReportModal;
$("report-send").onclick = sendReport;
$("report-popup-close").onclick = closeReportModal;
makeDraggable($("report-popup"), $("report-popup-bar"));   // drag (resize via CSS)
$("search-input").addEventListener("input", (ev) => applyMenuFilter(ev.target.value));

// ---- Table chat (local-only for now) ----
// Renders a message at the bottom of the scrollable log and scrolls to it. No
// networking yet: nothing is transmitted to other players. This is the bottom
// chat panel, separate from the sidebar Coaching view (renderChat / #chat).
function appendTableChat(text, who) {
  const log = $("tc-log");
  const empty = log.querySelector(".tc-empty");
  if (empty) empty.remove();
  const m = el("div", { class: "tc-msg" });
  if (who) m.append(el("span", { class: "tc-who" }, who));
  m.append(document.createTextNode(text));
  log.append(m);
  log.scrollTop = log.scrollHeight;   // newest message is at the bottom
}
$("tc-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const input = $("tc-input");
  const text = (input.value || "").trim();
  if (!text) return;
  appendTableChat(text, "You");
  input.value = "";
  input.focus();
});

// Seed the scenario's authored chat (state.chat, from btn/<scenario>.btn) into
// the panel. Done once per scenario: switching scenarios clears and re-seeds;
// a same-scenario Redeal keeps the existing thread (incl. user messages).
function seedScenarioChat(state) {
  if (!state || state.scenario === chatScenario) return;
  chatScenario = state.scenario;
  const log = $("tc-log");
  log.innerHTML = "";
  const lines = state.chat || [];
  if (!lines.length) {
    log.append(el("div", { class: "tc-empty" },
      "No messages yet — chat with the person you're playing with here."));
    return;
  }
  for (const text of lines) appendTableChat(text, "Coach");
}

// BBA-comparison pop-up wiring.
// ---- Options modal ----
$("options-btn").onclick = () => { $("settings-modal").hidden = false; };
$("settings-close").onclick = () => { $("settings-modal").hidden = true; };
$("settings-modal").addEventListener("click", (ev) => {
  if (ev.target.id === "settings-modal") $("settings-modal").hidden = true;   // backdrop closes
});

$("cards-toggle").checked = cardsMode;
$("cards-toggle").onchange = (ev) => {
  cardsMode = ev.target.checked;
  try { localStorage.setItem("bid.cardsMode", cardsMode ? "1" : "0"); } catch (_) {}
  if (window._lastState) render(window._lastState);   // redraw the current deal in the new mode
};

$("ten-toggle").checked = tenAsT;
$("ten-toggle").onchange = (ev) => {
  tenAsT = ev.target.checked;
  try { localStorage.setItem("bid.tenAsT", tenAsT ? "1" : "0"); } catch (_) {}
  if (window._lastState) render(window._lastState);
};

$("compare-toggle").checked = compareEnabled;
$("compare-toggle").onchange = (ev) => setCompareEnabled(ev.target.checked);
$("compare-popup-close").onclick = hideComparePopup;   // dismiss for now; reopen below
$("show-compare-btn").onclick = showComparePopup;
makeDraggable($("compare-popup"), $("compare-popup-bar"), (pos) => {
  comparePos = pos;
  try { localStorage.setItem("bid.comparePos", JSON.stringify(pos)); } catch (_) {}
});

fitTable();
loadMenu().catch((e) => showErr("Couldn't load scenarios: " + e.message));
