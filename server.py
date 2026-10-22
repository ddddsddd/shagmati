"""
Игровой сервер шахмат: отдаёт сайт (HTML/CSS/JS), хранит партии,
проверяет ходы (python-chess) и общается с ИИ-сервером (ai_server.py).

Запуск:   python server.py      ->  http://127.0.0.1:8000
ИИ-сервер запускается отдельно:   python ai_server.py
"""

import os
import random
import re
import threading
import uuid

import chess
import requests
from flask import Flask, jsonify, request, send_from_directory

AI_URL = os.environ.get("AI_SERVER_URL", "http://127.0.0.1:8001").rstrip("/")
PORT = int(os.environ.get("PORT", "8000"))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
app.json.ensure_ascii = False


def ai_down(url):
    return f"ИИ-сервер {url} недоступен. Запусти его в отдельном окне: python ai_server.py"


def ai_server_url(value):
    """Адрес ИИ-сервера из настроек; пустой или кривой — адрес по умолчанию."""
    url = (value or "").strip().rstrip("/")
    return url if url.startswith(("http://", "https://")) else AI_URL


class Game:
    def __init__(self, player_color, personality, difficulty):
        self.id = uuid.uuid4().hex[:12]
        self.board = chess.Board()
        self.player_color = player_color
        self.personality = personality
        self.difficulty = difficulty
        self.chat = []                 # [{"role": "user"|"ai", "text": ...}]
        self.resigned = False
        self.ai_busy = False
        self.final_commented = False
        self.ai_url = AI_URL           # адрес ИИ-сервера (можно сменить в настройках)
        self.api_base_url = ""         # свой URL API нейросети ("" — по умолчанию)
        self.lock = threading.RLock()

    @property
    def ai_color(self):
        return not self.player_color

    def outcome(self):
        if self.resigned:
            return "resign"
        return self.board.outcome(claim_draw=True)

    def is_over(self):
        return self.outcome() is not None


GAMES = {}


def color_name(color):
    return "white" if color == chess.WHITE else "black"


def result_info(game):
    """(winner, текст) — winner: player / ai / draw / None."""
    outcome = game.outcome()
    if outcome is None:
        return None, None
    if outcome == "resign":
        return "ai", "Ты сдался — победа ИИ"
    if outcome.winner is not None:
        who = "player" if outcome.winner == game.player_color else "ai"
        return who, "Мат! Ты победил 🎉" if who == "player" else "Мат! Победил ИИ"
    reasons = {
        chess.Termination.STALEMATE: "Пат — ничья",
        chess.Termination.INSUFFICIENT_MATERIAL: "Ничья — не хватает материала для мата",
        chess.Termination.THREEFOLD_REPETITION: "Ничья — троекратное повторение",
        chess.Termination.FIVEFOLD_REPETITION: "Ничья — пятикратное повторение",
        chess.Termination.FIFTY_MOVES: "Ничья — правило 50 ходов",
        chess.Termination.SEVENTYFIVE_MOVES: "Ничья — правило 75 ходов",
    }
    return "draw", reasons.get(outcome.termination, "Ничья")


def state(game):
    with game.lock:
        return _state(game)


def _state(game):
    board = game.board
    over = game.is_over()
    winner, result_text = result_info(game)
    replay = chess.Board()
    history = []
    for move in board.move_stack:
        history.append({"san": replay.san(move), "uci": move.uci(), "color": color_name(replay.turn)})
        replay.push(move)
    last = board.peek() if board.move_stack else None
    check_square = None
    if board.is_check():
        check_square = chess.square_name(board.king(board.turn))
    player_turn = board.turn == game.player_color and not over
    return {
        "game_id": game.id,
        "fen": board.fen(),
        "player_color": color_name(game.player_color),
        "turn": color_name(board.turn),
        "legal_moves": [m.uci() for m in board.legal_moves] if player_turn else [],
        "history": history,
        "last_move": {"from": chess.square_name(last.from_square), "to": chess.square_name(last.to_square)} if last else None,
        "check_square": check_square,
        "game_over": over,
        "winner": winner,
        "result_text": result_text,
        "needs_final_comment": over and not game.final_commented,
        "ai_busy": game.ai_busy,
    }


def apply_settings(game, data):
    # настройки можно менять прямо во время партии
    if data.get("personality"):
        game.personality = data["personality"]
    if data.get("difficulty"):
        game.difficulty = data["difficulty"]
    if "ai_url" in data:
        game.ai_url = ai_server_url(data["ai_url"])
    if "api_base_url" in data:
        game.api_base_url = (data["api_base_url"] or "").strip()


def get_game(data):
    game = GAMES.get((data or {}).get("game_id"))
    if game is not None:
        apply_settings(game, data)
    return game


def ai_payload(game, **extra):
    payload = {
        "moves": [m.uci() for m in game.board.move_stack],
        "ai_color": color_name(game.ai_color),
        "personality": game.personality,
        "difficulty": game.difficulty,
        "chat": game.chat[-12:],
        "api_base_url": game.api_base_url,
    }
    payload.update(extra)
    return payload


def call_ai(game, path, payload, timeout=300):
    try:
        response = requests.post(f"{game.ai_url}{path}", json=payload, timeout=timeout)
    except requests.RequestException:
        raise ConnectionError(ai_down(game.ai_url))
    try:
        data = response.json()
    except ValueError:
        raise ConnectionError(f"ИИ-сервер вернул не JSON (код {response.status_code})")
    if response.status_code != 200:
        raise ConnectionError(data.get("error", f"ИИ-сервер вернул {response.status_code}"))
    return data


def add_chat(game, role, text):
    if text:
        game.chat.append({"role": role, "text": text})


# ---------------------------------------------------------------------------
# Разбор хода из речи / текста: «е2 е4», «e2-e4», «Nf3», «O-O»
# ---------------------------------------------------------------------------

SQUARE_RE = re.compile(r"(?<![a-zа-яё])([a-hабцсдеэёфгжх])\s*-?\s*([1-8])(?![0-9])")
CYR_FILES = {"а": "a", "б": "b", "ц": "c", "с": "c", "д": "d", "е": "e", "э": "e", "ё": "e",
             "ф": "f", "г": "g", "ж": "g", "х": "h"}
FILLER_WORDS = {
    "ход", "хожу", "иду", "пойду", "давай", "ну", "я", "и", "с", "со", "на", "так", "тогда", "бью", "беру",
    "пешка", "пешкой", "конь", "конём", "конем", "слон", "слоном", "ладья", "ладьёй", "ладьей",
    "ферзь", "ферзём", "ферзем", "король", "королём", "королем",
}


def quick_parse_move(board, text):
    """Быстрый разбор простых команд без нейросети. None — если это не (только) ход."""
    raw = text.strip()
    try:
        return board.parse_san(raw.replace("0", "O"))
    except ValueError:
        pass
    lowered = raw.lower()
    squares = [CYR_FILES.get(f, f) + r for f, r in SQUARE_RE.findall(lowered)]
    if len(squares) != 2 or "?" in lowered:
        return None
    rest = re.findall(r"[a-zа-яё]+", SQUARE_RE.sub(" ", lowered))
    if any(word not in FILLER_WORDS for word in rest):
        return None  # во фразе есть что-то кроме хода — пусть разбирается нейросеть
    for promo in ("", "q"):
        try:
            move = chess.Move.from_uci(squares[0] + squares[1] + promo)
        except ValueError:
            return None
        if move in board.legal_moves:
            return move
    return None


# ---------------------------------------------------------------------------
# Маршруты
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/api/health")
def health():
    ai_url = ai_server_url(request.args.get("ai_url"))
    try:
        ai = requests.get(f"{ai_url}/health", params={"base_url": request.args.get("api_url", "")}, timeout=15).json()
    except (requests.RequestException, ValueError):
        ai = {"ok": False, "error": ai_down(ai_url)}
    return jsonify(ok=True, ai=ai, ai_url=ai_url)


@app.post("/api/new")
def new_game():
    data = request.get_json(force=True) or {}
    player_color = random.choice([chess.WHITE, chess.BLACK])  # жребий!
    game = Game(player_color, data.get("personality", "friendly"), data.get("difficulty", "normal"))
    apply_settings(game, data)
    GAMES[game.id] = game
    while len(GAMES) > 50:  # старые партии выкидываем
        GAMES.pop(next(iter(GAMES)))
    return jsonify(state=state(game))


@app.get("/api/state/<game_id>")
def get_state(game_id):
    game = GAMES.get(game_id)
    if game is None:
        return jsonify(error="партия не найдена"), 404
    return jsonify(state=state(game))


def apply_player_move(game, move):
    """Ход игрока под замком. Возвращает текст ошибки или None."""
    with game.lock:
        if game.is_over():
            return "партия уже закончена"
        if game.board.turn != game.player_color:
            return "сейчас ход ИИ"
        if move not in game.board.legal_moves:
            return "недопустимый ход"
        game.board.push(move)
    return None


@app.post("/api/move")
def player_move():
    data = request.get_json(force=True)
    game = get_game(data)
    if game is None:
        return jsonify(error="партия не найдена"), 404
    try:
        move = chess.Move.from_uci(data.get("uci", ""))
    except ValueError:
        return jsonify(error="некорректный ход"), 400
    error = apply_player_move(game, move)
    if error:
        return jsonify(error=error, state=state(game)), 400
    return jsonify(state=state(game))


@app.post("/api/ai_move")
def ai_move():
    data = request.get_json(force=True)
    game = get_game(data)
    if game is None:
        return jsonify(error="партия не найдена"), 404
    with game.lock:
        if game.is_over() or game.board.turn != game.ai_color:
            return jsonify(state=state(game), messages=[])
        if game.ai_busy:
            return jsonify(error="ИИ уже думает", state=state(game)), 409
        game.ai_busy = True
        ply = len(game.board.move_stack)
        payload = ai_payload(game)
    try:
        answer = call_ai(game, "/move", payload)
    except ConnectionError as e:
        game.ai_busy = False
        return jsonify(error=str(e), state=state(game)), 502

    with game.lock:
        game.ai_busy = False
        if game.resigned or len(game.board.move_stack) != ply:
            return jsonify(state=state(game), messages=[])
        move = chess.Move.from_uci(answer["move"])
        if move not in game.board.legal_moves:
            return jsonify(error="ИИ прислал недопустимый ход", state=state(game)), 502
        game.board.push(move)
        if game.is_over():
            game.final_commented = True  # ИИ уже прокомментировал свой победный/ничейный ход
        messages = []
        if answer.get("note"):
            messages.append({"role": "system", "text": answer["note"]})
        if answer.get("comment_player"):
            messages.append({"role": "ai", "text": answer["comment_player"]})
        messages.append({"role": "ai", "text": answer.get("comment_ai", ""), "move": answer["san"]})
        add_chat(game, "ai", answer.get("comment_player"))
        add_chat(game, "ai", f"(сходил {answer['san']}) {answer.get('comment_ai', '')}")
    return jsonify(state=state(game), messages=messages, source=answer.get("source"))


@app.post("/api/say")
def say():
    """Реплика игрока (голос или текст). Может оказаться ходом."""
    data = request.get_json(force=True)
    game = get_game(data)
    if game is None:
        return jsonify(error="партия не найдена"), 404
    text = (data.get("text") or "").strip()[:500]
    if not text:
        return jsonify(error="пустое сообщение"), 400

    player_turn = game.board.turn == game.player_color and not game.is_over()
    if player_turn:
        move = quick_parse_move(game.board, text)
        if move is not None and apply_player_move(game, move) is None:
            add_chat(game, "user", text)
            return jsonify(state=state(game), messages=[], moved=True)

    payload = ai_payload(game, message=text)
    add_chat(game, "user", text)
    try:
        answer = call_ai(game, "/chat", payload, timeout=120)
    except ConnectionError as e:
        return jsonify(error=str(e), state=state(game)), 502

    moved = False
    if answer.get("player_move") and player_turn:
        moved = apply_player_move(game, chess.Move.from_uci(answer["player_move"])) is None
    reply = answer.get("reply", "")
    add_chat(game, "ai", reply)
    return jsonify(state=state(game), messages=[{"role": "ai", "text": reply}], moved=moved)


@app.post("/api/resign")
def resign():
    data = request.get_json(force=True)
    game = get_game(data)
    if game is None:
        return jsonify(error="партия не найдена"), 404
    with game.lock:
        if not game.is_over():
            game.resigned = True
    return jsonify(state=state(game))


@app.post("/api/final_comment")
def final_comment():
    """Комментарий ИИ к концу партии, если её закончил ход игрока или сдача."""
    data = request.get_json(force=True)
    game = get_game(data)
    if game is None:
        return jsonify(error="партия не найдена"), 404
    with game.lock:
        if not game.is_over() or game.final_commented:
            return jsonify(state=state(game), messages=[])
        game.final_commented = True
    winner, result_text = result_info(game)
    # event — для нейросети, fallback — готовая фраза для офлайн-режима
    if game.resigned:
        event, fallback = "игрок сдался, ты победил", "Сдаёшься? Принято. Спасибо за партию!"
    elif winner == "player":
        event, fallback = "игрок поставил тебе мат — ты проиграл", "Ты меня переиграл, поздравляю! Реванш?"
    else:
        event, fallback = f"партия закончилась: {result_text}", f"{result_text}. Хорошо сыграли!"
    try:
        answer = call_ai(game, "/comment", ai_payload(game, event=event, fallback=fallback), timeout=120)
    except ConnectionError as e:
        return jsonify(error=str(e), state=state(game)), 502
    add_chat(game, "ai", answer.get("comment"))
    return jsonify(state=state(game), messages=[{"role": "ai", "text": answer.get("comment", "")}])


if __name__ == "__main__":
    print(f"Шахматы: http://127.0.0.1:{PORT}  (ИИ-сервер ожидается на {AI_URL})")
    app.run(host="127.0.0.1", port=PORT, threaded=True)
