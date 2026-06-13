// Bidding-with-Claude page. Left panel toggles Scenario menu / Coaching chat.
// Compass-free table: You at the bottom, Claude (partner) top, LHO left, RHO right;
// auction in the center. Calls reveal one at a time via /step.
"use strict";

const SEATS = ["N", "E", "S", "W"];
const STRAINS = ["C", "D", "H", "S", "NT"];
const SUIT_SYM = { S: "♠", H: "♥", D: "♦", C: "♣" };
const SUIT_CLASS = { S: "suit-spade", H: "suit-heart", D: "suit-diamond", C: "suit-club" };
// Seats by role, not compass — the user always sits at the bottom; the board may rotate.
const ROLE = { S: "You", N: "Claude", W: "LHO", E: "RHO" };
const SEAT_NAME = { N: "Claude", E: "RHO", W: "LHO" };

let currentScenario = null;
let sessionId = null;
let selLevel = null;   // BBO-style bid box: the level the user tapped, awaiting a strain

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
  $("setup-hint").hidden = true;
  $("table-wrap").hidden = false;
  showSide("coaching");           // surface the partner's thinking while we play
  setBusy("Dealing…");
  try {
    const data = await api("POST", "/api/bid/session", { scenario: currentScenario, board_index: null });
    sessionId = data.session_id;
    render(data.state);
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

function buildAuction(container, calls, dealer, toPlay, markLast) {
  container.innerHTML = "";
  for (const s of SEATS) container.append(el("div", { class: "head" }, ROLE[s]));
  for (let i = 0; i < SEATS.indexOf(dealer); i++) container.append(el("div", { class: "cell" }));
  calls.forEach((c, i) => {
    const cell = el("div", { class: "cell" + (markLast && i === calls.length - 1 ? " new" : "") });
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
    row.append(el("span", { class: SUIT_CLASS[suit] }, (hand[suit] || []).join(" ") || "—"));
    target.append(row);
  }
}

function faceDown(slot, role) {
  slot.className = "seat face-down";
  slot.innerHTML = "";
  slot.append(el("div", { class: "seat-name" }, role));
}

function seatHand(slot, role, hand, hcp, goldTurn) {
  slot.className = "seat";
  slot.innerHTML = "";
  const wrap = el("div", { class: "hand-wrap" });
  handRows(wrap, hand);
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
    m.append(" — " + c.reason + (c.ms ? ` (${(c.ms / 1000).toFixed(1)}s)` : ""));
    chat.append(m);
  }
  if (state.complete && state.review) {
    const m = el("div", { class: "chat-msg" });
    m.append(el("span", { class: "who" }, "Claude · review"));
    m.append(state.review);
    chat.append(m);
  }
  chat.scrollTop = chat.scrollHeight;
}

function renderResult(state) {
  $("result").hidden = false;
  $("contract-line").textContent = "Final contract: " + state.contract;
  buildAuction($("auc-ours"), state.calls, state.dealer, null);
  buildAuction($("auc-bba"), state.bba_compare, state.dealer, null);
  const t = state.timings || {};
  const secs = (ms) => (ms == null ? "—" : (ms / 1000).toFixed(1) + "s");
  const list = (a) => (a && a.length ? a.map(secs).join(", ") : "—");
  $("timing").textContent =
    `Timing — Claude bids: ${list(t.claude_bid_ms)} · robot bids: ${list(t.bba_bid_ms)}`
    + ` · BBA compare: ${secs(t.compare_ms)} · Claude review: ${secs(t.review_ms)}`;
}

function render(state) {
  window._lastState = state;
  $("status").hidden = true;
  $("setup-hint").hidden = true;
  $("table-wrap").hidden = false;

  $("meta-line").innerHTML =
    `<strong>${state.scenario.replaceAll("_", " ")}</strong> &nbsp;·&nbsp; deal ${state.board_index + 1}/${state.n_boards}`;

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
    seatHand($("slot-top"), ROLE.N, state.all_hands.N, state.all_hcp.N);
    seatHand($("slot-left"), ROLE.W, state.all_hands.W, state.all_hcp.W);
    seatHand($("slot-right"), ROLE.E, state.all_hands.E, state.all_hcp.E);
    seatHand($("slot-bottom"), ROLE.S, state.all_hands.S, state.all_hcp.S);
  } else {
    faceDown($("slot-top"), ROLE.N);
    faceDown($("slot-left"), ROLE.W);
    faceDown($("slot-right"), ROLE.E);
    seatHand($("slot-bottom"), "You", state.south_hand, state.south_hcp, state.user_to_play);
  }

  const center = $("center"); center.innerHTML = "";
  const auc = el("div", { class: "auc" });
  buildAuction(auc, state.calls, state.dealer, state.complete ? null : state.to_play, true);
  center.append(auc);

  renderChat(state);

  if (state.complete) { $("bidbox").hidden = true; renderResult(state); }
  else { $("result").hidden = true; renderBidbox(state); }
}

function setBusy(msg) {
  $("bidbox").hidden = true;
  $("status").hidden = false;
  $("status-text").textContent = msg;
}
function showErr(msg) { const e = $("page-err"); e.hidden = !msg; e.textContent = msg; }

$("tab-scenario").onclick = () => showSide("scenario");
$("tab-coaching").onclick = () => showSide("coaching");
$("redeal").onclick = deal;
$("search-input").addEventListener("input", (ev) => applyMenuFilter(ev.target.value));
loadMenu().catch((e) => showErr("Couldn't load scenarios: " + e.message));
