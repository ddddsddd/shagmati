"use strict";

// =====================================================================
// Константы и утилиты
// =====================================================================

const FILES = "abcdefgh";
const START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
const GLYPHS = { k: "♚", q: "♛", r: "♜", b: "♝", n: "♞", p: "♟" };
const TEXT_STYLE = "︎"; // чтобы ♟ не превращалась в эмодзи
const PIECE_VALUE = { p: 1, n: 3, b: 3, r: 5, q: 9, k: 0 };
const START_COUNT = { p: 8, n: 2, b: 2, r: 2, q: 1, k: 1 };
const PIECE_WORDS = { K: "король", Q: "ферзь", R: "ладья", B: "слон", N: "конь" };

const $ = (id) => document.getElementById(id);

// =====================================================================
// Настройки (в localStorage)
// =====================================================================

const SETTINGS_KEY = "chess-ai-settings";
const DEFAULT_SETTINGS = {
  personality: "friendly",
  difficulty: "normal",
  hints: true,
  voice: false,
  voiceURI: "",
  rate: 1.0,
  apiUrl: "",   // свой URL API нейросети ("" — по умолчанию)
  aiUrl: "",    // адрес ai_server.py ("" — по умолчанию)
};

let settings = loadSettings();
settings.call = false; // звонок всегда начинается выключенным

function loadSettings() {
  try {
    return { ...DEFAULT_SETTINGS, ...JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}") };
  } catch {
    return { ...DEFAULT_SETTINGS };
  }
}

function saveSettings() {
  const { call, ...persisted } = settings;
  try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(persisted)); } catch { /* приватный режим */ }
}

// =====================================================================
// Состояние
// =====================================================================

let game = null;          // состояние партии с сервера
let position = {};        // { e4: "P", ... }
let selected = null;      // выбранная клетка
let drag = null;          // текущее перетаскивание
let aiPending = false;    // ИИ думает над ходом
let movePending = false;  // отправляем ход игрока
let chatPending = 0;      // сколько реплик ждут ответа
let finalPending = false;

// =====================================================================
// Связь с сервером
// =====================================================================

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  const res = await fetch(path, options);
  let data = {};
  try { data = await res.json(); } catch { /* пустой ответ */ }
  if (!res.ok) {
    const err = new Error(data.error || `Ошибка сервера (${res.status})`);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

const prefs = () => ({
  game_id: game?.game_id,
  personality: settings.personality,
  difficulty: settings.difficulty,
  ai_url: settings.aiUrl,
  api_base_url: settings.apiUrl,
});

function setGame(state) {
  if (!state) return;
  // не даём устаревшему ответу откатить позицию назад
  if (game && state.game_id === game.game_id && state.history.length < game.history.length) return;
  game = state;
  if (selected && !targetsFrom(selected).length) selected = null;
  render();
}

async function checkHealth() {
  const el = $("aiStatus");
  const set = (cls, text) => {
    el.className = "ai-status " + cls;
    el.querySelector("span").textContent = text;
    return text;
  };
  const query = new URLSearchParams({ ai_url: settings.aiUrl, api_url: settings.apiUrl });
  try {
    const { ai } = await api(`/api/health?${query}`);
    if (!ai.ok) return set("down", ai.error || "ИИ-сервер не запущен");
    if (ai.mode === "claude") return set("ok", `Нейросеть: ${ai.model}${ai.base_url ? " · свой URL" : ""}`);
    return set("offline", `Офлайн-движок: ${ai.reason || "нейросеть недоступна"}`);
  } catch {
    return set("down", "Игровой сервер недоступен");
  }
}

// =====================================================================
// Ход партии
// =====================================================================

async function newGame() {
  if (game && !game.game_over && game.history.length && !confirm("Начать новую партию? Текущая будет потеряна.")) return;
  stopListening();
  stopSpeaking();
  heldSpeech.length = 0;
  selected = null;
  aiPending = movePending = finalPending = false;
  $("chat").innerHTML = "";
  renderTyping();
  showOverlay("🎲 Бросаю жребий…", "Кто будет играть белыми?");
  const started = Date.now();
  let data;
  try {
    data = await api("/api/new", prefs());
  } catch (err) {
    showOverlay("Не удалось начать", err.message, "Попробовать ещё", newGame);
    return;
  }
  await new Promise((r) => setTimeout(r, Math.max(0, 700 - (Date.now() - started))));
  game = null;
  setGame(data.state);
  const white = game.player_color === "white";
  showOverlay(white ? "Ты играешь белыми ♔" : "Ты играешь чёрными ♚",
              white ? "Твой ход первый" : "Первым ходит ИИ");
  setTimeout(hideOverlay, 1300);
  addMessage({ role: "system", text: `🎲 Жребий: ты играешь ${white ? "белыми" : "чёрными"}.` });
  checkHealth();
  afterUpdate();
}

function afterUpdate() {
  if (!game) return;
  if (game.game_over) {
    showGameOver();
    if (game.needs_final_comment) requestFinalComment();
    return;
  }
  if (game.turn !== game.player_color) requestAiMove();
}

async function makeMove(from, to) {
  const options = game.legal_moves.filter((u) => u.slice(0, 4) === from + to);
  if (!options.length) return;
  let uci = options[0];
  if (options.length > 1) {
    const promo = await choosePromotion(to);
    if (!promo) { renderBoard(); return; }
    uci = from + to + promo;
  }
  movePending = true;
  try {
    const data = await api("/api/move", { ...prefs(), uci });
    setGame(data.state);
  } catch (err) {
    if (err.data?.state) setGame(err.data.state);
    toast(err.message);
    return;
  } finally {
    movePending = false;
  }
  afterUpdate();
}

async function requestAiMove() {
  if (aiPending || !game || game.game_over || game.turn === game.player_color) return;
  const gameId = game.game_id;
  aiPending = true;
  render();
  let data;
  try {
    data = await api("/api/ai_move", prefs());
  } catch (err) {
    if (game?.game_id !== gameId) return;
    aiPending = false;
    render();
    if (err.status !== 409) addSystem(`⚠️ ${err.message}`, requestAiMove);
    return;
  }
  if (game?.game_id !== gameId) return;
  aiPending = false;
  setGame(data.state);
  addMessages(data.messages);
  afterUpdate();
}

async function requestFinalComment() {
  if (finalPending) return;
  finalPending = true;
  const gameId = game.game_id;
  chatPending++;
  renderTyping();
  try {
    const data = await api("/api/final_comment", prefs());
    if (game?.game_id === gameId) {
      setGame(data.state);
      addMessages(data.messages);
    }
  } catch (err) {
    addSystem(`⚠️ ${err.message}`);
  } finally {
    chatPending--;
    finalPending = false;
    renderTyping();
  }
}

async function resign() {
  if (!game || game.game_over) return;
  if (!confirm("Точно сдаёшься?")) return;
  try {
    const data = await api("/api/resign", prefs());
    setGame(data.state);
    afterUpdate();
  } catch (err) {
    toast(err.message);
  }
}

// Реплика игрока (текстом или голосом) — может оказаться ходом
async function say(text, viaVoice = false) {
  text = text.trim();
  if (!text) return;
  if (!game) { toast("Сначала начни партию"); return; }
  addMessage({ role: "user", text: (viaVoice ? "🎙️ " : "") + text });
  const gameId = game.game_id;
  chatPending++;
  renderTyping();
  try {
    const data = await api("/api/say", { ...prefs(), text });
    if (game?.game_id !== gameId) return;
    setGame(data.state);
    addMessages(data.messages);
    if (data.moved) afterUpdate();
  } catch (err) {
    addSystem(`⚠️ ${err.message}`);
  } finally {
    chatPending--;
    renderTyping();
  }
}

// =====================================================================
// Отрисовка
// =====================================================================

function parseFen(fen) {
  const map = {};
  fen.split(" ")[0].split("/").forEach((row, i) => {
    const rank = 8 - i;
    let file = 0;
    for (const ch of row) {
      if (/\d/.test(ch)) file += Number(ch);
      else map[FILES[file++] + rank] = ch;
    }
  });
  return map;
}

const isWhitePiece = (p) => p === p.toUpperCase();
const isMine = (p) => !!game && isWhitePiece(p) === (game.player_color === "white");
const canPlay = () => !!game && !game.game_over && game.turn === game.player_color && !aiPending && !movePending;
const targetsFrom = (sq) => (game?.legal_moves || []).filter((u) => u.startsWith(sq)).map((u) => u.slice(2, 4));

function pieceEl(p, mine = false) {
  const span = document.createElement("span");
  span.className = `piece ${isWhitePiece(p) ? "w" : "b"}${mine ? " mine" : ""}`;
  span.textContent = GLYPHS[p.toLowerCase()] + TEXT_STYLE;
  return span;
}

function render() {
  position = parseFen(game ? game.fen : START_FEN);
  if (!drag) renderBoard();
  renderBars();
  renderMoves();
  renderStatus();
}

function renderBoard() {
  const board = $("board");
  board.innerHTML = "";
  const flip = game?.player_color === "black";
  const targets = selected ? targetsFrom(selected) : [];
  for (let row = 0; row < 8; row++) {
    for (let col = 0; col < 8; col++) {
      const file = flip ? 7 - col : col;
      const rank = flip ? row + 1 : 8 - row;
      const sq = FILES[file] + rank;
      const el = document.createElement("div");
      el.className = "sq " + ((file + rank) % 2 === 1 ? "dark" : "light");
      el.dataset.sq = sq;
      if (game?.last_move && (game.last_move.from === sq || game.last_move.to === sq)) el.classList.add("last");
      if (sq === selected) el.classList.add("sel");
      if (game?.check_square === sq) el.classList.add("check");
      if (settings.hints && targets.includes(sq)) {
        el.classList.add("target");
        if (position[sq]) el.classList.add("capture");
      }
      if (col === 0) el.insertAdjacentHTML("beforeend", `<span class="coord rank">${rank}</span>`);
      if (row === 7) el.insertAdjacentHTML("beforeend", `<span class="coord file">${FILES[file]}</span>`);
      const p = position[sq];
      if (p) el.appendChild(pieceEl(p, isMine(p) && canPlay()));
      board.appendChild(el);
    }
  }
}

function renderBars() {
  const meWhite = (game?.player_color || "white") === "white";
  $("meColorLabel").textContent = game ? (meWhite ? "белые" : "чёрные") : "—";
  $("aiColorLabel").textContent = game ? (meWhite ? "чёрные" : "белые") : "—";

  const myTurn = game && !game.game_over && game.turn === game.player_color;
  const aiTurn = game && !game.game_over && !myTurn;
  $("meTurn").className = "turn-dot" + (myTurn ? " on" : "");
  $("aiTurn").className = "turn-dot" + (aiTurn ? (aiPending ? " thinking" : " on") : "");

  // съеденные фигуры и перевес в материале
  const counts = {};
  for (const p of Object.values(position)) counts[p] = (counts[p] || 0) + 1;
  const lost = (white) => {
    let glyphs = "";
    let value = 0;
    for (const t of "qrbnp") {
      const n = Math.max(0, START_COUNT[t] - (counts[white ? t.toUpperCase() : t] || 0));
      glyphs += (GLYPHS[t] + TEXT_STYLE).repeat(n);
      value += n * PIECE_VALUE[t];
    }
    return { glyphs, value };
  };
  const mine = lost(!meWhite);  // съел я
  const theirs = lost(meWhite); // съел ИИ
  const diff = mine.value - theirs.value;
  $("meCaptured").innerHTML = mine.glyphs + (diff > 0 ? `<b>+${diff}</b>` : "");
  $("aiCaptured").innerHTML = theirs.glyphs + (diff < 0 ? `<b>+${-diff}</b>` : "");
}

function renderMoves() {
  const el = $("moves");
  el.innerHTML = "";
  const history = game?.history || [];
  for (let i = 0; i < history.length; i += 2) {
    el.insertAdjacentHTML("beforeend", `<span class="num">${i / 2 + 1}.</span>`);
    for (const j of [i, i + 1]) {
      const span = document.createElement("span");
      span.className = "mv" + (j === history.length - 1 ? " latest" : "");
      span.textContent = history[j]?.san || "";
      el.appendChild(span);
    }
  }
  el.scrollTop = el.scrollHeight;
}

function renderStatus() {
  const el = $("status");
  el.className = "status";
  if (!game) {
    el.textContent = "Нажми «Новая партия»";
  } else if (game.game_over) {
    el.classList.add("over");
    el.textContent = game.result_text;
  } else if (game.turn !== game.player_color) {
    el.classList.add("thinking");
    el.textContent = "ИИ думает над ходом";
  } else {
    if (game.check_square) el.classList.add("check");
    el.textContent = game.check_square ? "Шах! Твой ход" : "Твой ход";
  }
}

// =====================================================================
// Доска: клики и перетаскивание
// =====================================================================

function squareAt(x, y) {
  const el = document.elementFromPoint(x, y);
  return el ? el.closest(".sq") : null;
}

$("board").addEventListener("pointerdown", (e) => {
  if (e.button !== 0) return;
  const sqEl = e.target.closest(".sq");
  if (!sqEl || !canPlay()) return;
  const sq = sqEl.dataset.sq;

  if (selected && selected !== sq && targetsFrom(selected).includes(sq)) {
    const from = selected;
    selected = null;
    makeMove(from, sq);
    return;
  }
  const p = position[sq];
  if (p && isMine(p)) {
    const wasSelected = selected === sq;
    selected = sq;
    renderBoard();
    const ghost = pieceEl(p, true);
    ghost.classList.add("drag-ghost");
    ghost.style.display = "none";
    document.body.appendChild(ghost);
    drag = { from: sq, ghost, x: e.clientX, y: e.clientY, moved: false, wasSelected, hover: null };
    e.preventDefault();
  } else if (selected) {
    selected = null;
    renderBoard();
  }
});

document.addEventListener("pointermove", (e) => {
  if (!drag) return;
  if (!drag.moved && Math.hypot(e.clientX - drag.x, e.clientY - drag.y) > 4) {
    drag.moved = true;
    drag.ghost.style.display = "";
    document.querySelector(`.sq[data-sq="${drag.from}"] .piece`)?.classList.add("ghosted");
  }
  if (!drag.moved) return;
  drag.ghost.style.left = e.clientX + "px";
  drag.ghost.style.top = e.clientY + "px";
  const el = squareAt(e.clientX, e.clientY);
  if (el !== drag.hover) {
    drag.hover?.classList.remove("hover");
    el?.classList.add("hover");
    drag.hover = el;
  }
});

function endDrag(e, cancelled) {
  if (!drag) return;
  const d = drag;
  drag = null;
  d.ghost.remove();
  d.hover?.classList.remove("hover");
  const to = cancelled ? null : squareAt(e.clientX, e.clientY)?.dataset.sq;
  if (d.moved) {
    if (to && to !== d.from && targetsFrom(d.from).includes(to)) {
      selected = null;
      makeMove(d.from, to);
    } else {
      renderBoard();
    }
  } else if (d.wasSelected) {
    selected = null; // повторный клик снимает выделение
    renderBoard();
  }
}

document.addEventListener("pointerup", (e) => endDrag(e, false));
document.addEventListener("pointercancel", (e) => endDrag(e, true));

function choosePromotion(to) {
  return new Promise((resolve) => {
    const box = $("promo");
    const sqEl = document.querySelector(`.sq[data-sq="${to}"]`);
    box.innerHTML = "";
    box.style.left = sqEl.offsetLeft + "px";
    box.style.top = "0px";
    const white = game.player_color === "white";
    const close = (value) => {
      box.classList.add("hidden");
      document.removeEventListener("pointerdown", outside, true);
      resolve(value);
    };
    const outside = (ev) => { if (!box.contains(ev.target)) close(null); };
    for (const t of ["q", "r", "n", "b"]) {
      const btn = document.createElement("button");
      btn.title = { q: "Ферзь", r: "Ладья", n: "Конь", b: "Слон" }[t];
      btn.appendChild(pieceEl(white ? t.toUpperCase() : t));
      btn.addEventListener("click", () => close(t));
      box.appendChild(btn);
    }
    box.classList.remove("hidden");
    setTimeout(() => document.addEventListener("pointerdown", outside, true), 0);
  });
}

// =====================================================================
// Оверлей и уведомления
// =====================================================================

function showOverlay(title, sub, buttonText, onClick) {
  $("overlayTitle").textContent = title;
  $("overlaySub").textContent = sub || "";
  const btn = $("overlayBtn");
  btn.classList.toggle("hidden", !buttonText);
  btn.textContent = buttonText || "";
  btn.onclick = onClick || null;
  $("overlay").classList.remove("hidden");
}

function hideOverlay() {
  $("overlay").classList.add("hidden");
}

$("overlay").addEventListener("click", (e) => {
  if (e.target === $("overlay") && game?.game_over) hideOverlay(); // посмотреть финальную позицию
});

function showGameOver() {
  const icon = { player: "🏆", ai: "🤖", draw: "🤝" }[game.winner] || "";
  showOverlay(`${icon} ${game.result_text}`, "Клик мимо окна — посмотреть позицию", "Новая партия", newGame);
}

let toastTimer = null;
function toast(text) {
  const el = $("toast");
  el.textContent = text;
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 2800);
}

// =====================================================================
// Чат
// =====================================================================

function addMessages(list = []) {
  for (const m of list) addMessage(m);
}

function addMessage({ role, text, move }) {
  if (!text && !move) return;
  const chat = $("chat");
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  if (move) {
    const tag = document.createElement("span");
    tag.className = "move-tag";
    tag.textContent = move;
    el.appendChild(tag);
  }
  el.appendChild(document.createTextNode(text || ""));
  chat.insertBefore(el, $("typing"));
  chat.scrollTop = chat.scrollHeight;
  if (role === "ai" && voiceEnabled()) speak(text || `Хожу ${move}`);
  return el;
}

function addSystem(text, retry) {
  const el = addMessage({ role: "system", text });
  if (retry && el) {
    const btn = document.createElement("button");
    btn.className = "btn small retry";
    btn.textContent = "Повторить";
    btn.addEventListener("click", () => { btn.remove(); retry(); });
    el.appendChild(btn);
  }
}

function renderTyping() {
  let el = $("typing");
  if (!el) {
    el = document.createElement("div");
    el.id = "typing";
    el.className = "msg ai typing hidden";
    el.textContent = "ИИ печатает";
    $("chat").appendChild(el);
  }
  el.classList.toggle("hidden", chatPending <= 0);
  $("chat").appendChild(el); // всегда внизу
  $("chat").scrollTop = $("chat").scrollHeight;
}

$("chatForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const input = $("chatInput");
  const text = input.value;
  input.value = "";
  say(text);
});

// =====================================================================
// Голос: синтез речи (ИИ говорит)
// =====================================================================

const synth = window.speechSynthesis || null;
const voiceEnabled = () => !!synth && (settings.voice || settings.call);

function speakable(text) {
  return text
    .replace(/\bO-O-O\b|\b0-0-0\b/g, "длинная рокировка")
    .replace(/\bO-O\b|\b0-0\b/g, "короткая рокировка")
    .replace(/\b([KQRBN])([a-h]?[1-8]?)(x?)([a-h][1-8])(=[QRBN])?([+#])?/g,
      (m, piece, _from, x, to, promo, mark) =>
        `${PIECE_WORDS[piece]} ${x ? "берёт " : ""}${to}${mark === "#" ? ", мат" : mark ? ", шах" : ""}`)
    .replace(/\b([a-h])x([a-h][1-8])/g, "$1 берёт $2")
    .replace(/\p{Extended_Pictographic}/gu, "");
}

function currentVoice() {
  if (!synth) return null;
  const voices = synth.getVoices();
  return voices.find((v) => v.voiceURI === settings.voiceURI)
    || voices.find((v) => v.lang?.toLowerCase().startsWith("ru"))
    || null;
}

const heldSpeech = []; // реплики ИИ, пришедшие пока ты говоришь в микрофон

function speak(text) {
  if (!synth || !text) return;
  if (listening) { heldSpeech.push(text); return; } // чтобы ИИ не «услышал» сам себя
  const u = new SpeechSynthesisUtterance(speakable(text));
  const voice = currentVoice();
  if (voice) u.voice = voice;
  u.lang = voice?.lang || "ru-RU";
  u.rate = settings.rate;
  synth.speak(u);
}

function stopSpeaking() {
  synth?.cancel();
}

function populateVoices() {
  const select = $("setVoiceName");
  if (!synth) {
    select.innerHTML = "<option>Синтез речи не поддерживается</option>";
    select.disabled = true;
    return;
  }
  const voices = synth.getVoices();
  const ru = voices.filter((v) => v.lang?.toLowerCase().startsWith("ru"));
  const other = voices.filter((v) => !ru.includes(v));
  select.innerHTML = "";
  const auto = new Option("Авто (русский)", "");
  select.add(auto);
  for (const v of [...ru, ...other]) select.add(new Option(`${v.name} (${v.lang})`, v.voiceURI));
  select.value = settings.voiceURI;
  if (select.value !== settings.voiceURI) select.value = "";
}

if (synth) synth.addEventListener?.("voiceschanged", populateVoices);

// =====================================================================
// Голос: распознавание речи (ИИ слушает) — только по кнопке
// =====================================================================

const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition || null;
let recognizer = null;
let listening = false;

function startListening(holdMode = false) {
  if (listening) return;
  if (!Recognition) {
    toast("Браузер не умеет распознавать речь — открой игру в Chrome или Edge");
    return;
  }
  stopSpeaking(); // перебиваем ИИ, если он говорит
  let finalText = "";
  let interimText = "";
  recognizer = new Recognition();
  recognizer.lang = "ru-RU";
  recognizer.interimResults = true;
  recognizer.continuous = holdMode; // в режиме рации слушаем, пока зажат пробел
  recognizer.onresult = (e) => {
    interimText = "";
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const t = e.results[i][0].transcript;
      if (e.results[i].isFinal) finalText += t + " ";
      else interimText += t;
    }
    showInterim((finalText + interimText).trim());
  };
  recognizer.onerror = (e) => {
    if (e.error === "not-allowed" || e.error === "service-not-allowed") toast("Нет доступа к микрофону");
    else if (e.error !== "no-speech" && e.error !== "aborted") toast(`Ошибка распознавания: ${e.error}`);
  };
  recognizer.onend = () => {
    listening = false;
    recognizer = null;
    showInterim("");
    updateMicUI();
    heldSpeech.splice(0).forEach(speak);
    const text = (finalText || interimText).trim();
    if (text) say(text, true);
  };
  try {
    recognizer.start();
    listening = true;
  } catch (err) {
    toast(`Микрофон: ${err.message}`);
  }
  updateMicUI();
}

function stopListening() {
  if (recognizer && listening) recognizer.stop();
}

function showInterim(text) {
  const el = $("interim");
  el.textContent = text;
  el.classList.toggle("hidden", !text);
}

function updateMicUI() {
  const btn = $("micBtn");
  btn.classList.toggle("listening", listening);
  btn.querySelector(".mic-label").textContent = listening ? "Слушаю… (нажми, чтобы закончить)" : "Нажми, чтобы сказать";
}

$("micBtn").addEventListener("click", () => (listening ? stopListening() : startListening(false)));

// Пробел — рация (только в режиме звонка и не в поле ввода)
let spaceHeld = false;
const typingInField = () => ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName);

document.addEventListener("keydown", (e) => {
  if (e.code === "Escape") closeSettings();
  if (e.code !== "Space" || !settings.call || typingInField() || !$("settingsModal").classList.contains("hidden")) return;
  e.preventDefault();
  if (!e.repeat && !spaceHeld) {
    spaceHeld = true;
    startListening(true);
  }
});

document.addEventListener("keyup", (e) => {
  if (e.code !== "Space" || !spaceHeld) return;
  e.preventDefault();
  spaceHeld = false;
  stopListening();
});

// =====================================================================
// Режим звонка
// =====================================================================

let callStarted = 0;
let callTicker = null;

function setCall(on, greet = true) {
  settings.call = on;
  $("setCall").checked = on;
  $("callCard").classList.toggle("hidden", !on);
  $("callBtn").classList.toggle("active", on);
  clearInterval(callTicker);
  if (on) {
    callStarted = Date.now();
    callTicker = setInterval(updateCallUI, 250);
    updateCallUI();
    if (!Recognition) toast("Распознавание речи работает в Chrome и Edge — здесь можно только слушать ИИ");
    if (greet) speak("Алло! Я на связи. Нажми на микрофон, когда захочешь что-то сказать.");
  } else {
    stopListening();
    if (!settings.voice) stopSpeaking();
  }
}

function updateCallUI() {
  const sec = Math.floor((Date.now() - callStarted) / 1000);
  $("callTimer").textContent = `${String(Math.floor(sec / 60)).padStart(2, "0")}:${String(sec % 60).padStart(2, "0")}`;
  const speaking = !!synth?.speaking;
  $("callAvatar").classList.toggle("speaking", speaking);
  let status = "На связи";
  if (listening) status = "Слушаю тебя…";
  else if (speaking) status = "ИИ говорит…";
  else if (chatPending > 0) status = "ИИ думает над ответом…";
  else if (aiPending) status = "ИИ думает над ходом…";
  $("callStatus").textContent = status;
}

$("callBtn").addEventListener("click", () => setCall(!settings.call));
$("hangupBtn").addEventListener("click", () => setCall(false));

// =====================================================================
// Окно настроек
// =====================================================================

function openSettings() {
  $("setPersonality").value = settings.personality;
  $("setDifficulty").value = settings.difficulty;
  $("setHints").checked = settings.hints;
  $("setVoice").checked = settings.voice;
  $("setCall").checked = settings.call;
  $("setRate").value = settings.rate;
  $("setApiUrl").value = settings.apiUrl;
  $("setAiUrl").value = settings.aiUrl;
  $("apiCheckResult").textContent = "";
  $("rateValue").textContent = Number(settings.rate).toFixed(1);
  populateVoices();
  $("voiceSupport").textContent =
    `Озвучка: ${synth ? "есть ✅" : "нет ❌"} · Распознавание речи: ${Recognition ? "есть ✅" : "нет ❌ (нужен Chrome или Edge)"}`;
  $("settingsModal").classList.remove("hidden");
}

function closeSettings() {
  $("settingsModal").classList.add("hidden");
}

$("settingsBtn").addEventListener("click", openSettings);
$("settingsClose").addEventListener("click", closeSettings);
$("settingsModal").addEventListener("click", (e) => { if (e.target === $("settingsModal")) closeSettings(); });

$("setPersonality").addEventListener("change", (e) => { settings.personality = e.target.value; saveSettings(); });
$("setDifficulty").addEventListener("change", (e) => { settings.difficulty = e.target.value; saveSettings(); });
$("setHints").addEventListener("change", (e) => { settings.hints = e.target.checked; saveSettings(); renderBoard(); });
$("setVoice").addEventListener("change", (e) => {
  settings.voice = e.target.checked;
  saveSettings();
  if (!voiceEnabled()) stopSpeaking();
});
$("setCall").addEventListener("change", (e) => setCall(e.target.checked));
$("setVoiceName").addEventListener("change", (e) => { settings.voiceURI = e.target.value; saveSettings(); });
$("setRate").addEventListener("input", (e) => {
  settings.rate = Number(e.target.value);
  $("rateValue").textContent = settings.rate.toFixed(1);
  saveSettings();
});
function bindUrlSetting(inputId, key) {
  const input = $(inputId);
  input.addEventListener("change", () => {
    const url = input.value.trim().replace(/\/+$/, "");
    const valid = !url || /^https?:\/\/\S+$/.test(url);
    input.classList.toggle("invalid", !valid);
    if (!valid) {
      $("apiCheckResult").textContent = "Адрес должен начинаться с http:// или https://";
      return;
    }
    input.value = url;
    settings[key] = url;
    saveSettings();
    checkHealth();
  });
}

bindUrlSetting("setApiUrl", "apiUrl");
bindUrlSetting("setAiUrl", "aiUrl");
$("checkApiBtn").addEventListener("click", async () => {
  const out = $("apiCheckResult");
  out.textContent = "Проверяю…";
  out.textContent = await checkHealth();
});

$("testVoiceBtn").addEventListener("click", () => {
  stopSpeaking();
  speak("Привет! Вот так я буду комментировать нашу партию. Конь на f3 — и тебе шах.");
});

// =====================================================================
// Старт
// =====================================================================

$("newGameBtn").addEventListener("click", newGame);
$("resignBtn").addEventListener("click", resign);

render();
renderTyping();
showOverlay("Шахматы против нейросети", "Цвет фигур определит жребий", "Начать партию", newGame);
checkHealth();
setInterval(checkHealth, 20000);
