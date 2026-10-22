"""
ИИ-соперник для шахмат — отдельный API-сервер.

Нейросеть (Claude) выбирает ход, комментирует свой ход и ход игрока,
болтает с игроком в чате / голосом.

Запуск:   python ai_server.py           (порт 8001, меняется через AI_PORT)

Переменные окружения:
  ANTHROPIC_API_KEY  — ключ Claude API (без него работает запасной офлайн-движок)
  ANTHROPIC_BASE_URL — адрес API по умолчанию (свой URL/прокси можно задать и в настройках игры)
  CLAUDE_MODEL       — модель, по умолчанию claude-opus-5-5
  AI_OFFLINE=1       — принудительно играть без нейросети (простой движок)
  AI_FALLBACKS=0     — отключить серверный фолбэк на другую модель при отказе

Эндпоинты:
  GET  /health   — статус и режим (claude / offline)
  POST /move     — ход ИИ + комментарии к ходу игрока и своему ходу
  POST /chat     — ответ на реплику игрока (умеет распознать в ней ход игрока)
  POST /comment  — комментарий к событию (конец партии, сдача и т.п.)
"""

import json
import logging
import math
import os
import random
from collections import namedtuple

import chess
from flask import Flask, jsonify, request

try:
    import anthropic
except ImportError:  # SDK не установлен — играем офлайн
    anthropic = None

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
PORT = int(os.environ.get("AI_PORT", "8001"))
FORCE_OFFLINE = os.environ.get("AI_OFFLINE") == "1"
USE_FALLBACKS = os.environ.get("AI_FALLBACKS", "1") != "0"

logging.basicConfig(level=logging.WARNING, format="[ai] %(message)s")
log = logging.getLogger("ai")
log.setLevel(logging.INFO)
logging.getLogger("werkzeug").setLevel(logging.INFO)

app = Flask(__name__)
app.json.ensure_ascii = False


def normalize_url(url):
    """Пустая строка -> None (адрес по умолчанию). Без http(s):// -> ValueError."""
    url = (url or "").strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        raise ValueError("адрес API должен начинаться с http:// или https://")
    return url or None


def connect(base_url):
    """Создаёт клиента Claude для base_url и проверяет его. Возвращает (client, причина офлайна)."""
    if FORCE_OFFLINE:
        return None, "включён AI_OFFLINE=1"
    if anthropic is None:
        return None, "не установлен пакет anthropic"
    where = base_url or "Claude API"
    try:
        # base_url=None — адрес по умолчанию (или из ANTHROPIC_BASE_URL)
        new_client = anthropic.Anthropic(base_url=base_url, timeout=180.0, max_retries=2)
        # бесплатная проверка ключа; короткий таймаут, чтобы не висеть на неверном адресе
        new_client.with_options(timeout=10.0, max_retries=0).models.retrieve(MODEL)
    except anthropic.AuthenticationError:
        return None, f"неверный ключ API для {where}"
    except anthropic.PermissionDeniedError:
        return None, f"{where} отказал в доступе (403) — возможно, нужен свой URL/прокси"
    except anthropic.APIConnectionError:
        return None, f"нет связи с {where}"
    except anthropic.APIStatusError as e:
        # прокси могут не поддерживать /v1/models — пробуем работать дальше
        log.warning("Проверка %s вернула %s — пробую работать дальше", where, e.status_code)
    except Exception:  # SDK не нашёл ни ключа, ни профиля
        return None, "не задан ANTHROPIC_API_KEY"
    return new_client, None


Api = namedtuple("Api", "client offline_reason base_url")
_clients = {}  # base_url -> проверенный клиент


def get_client(base_url=None):
    """Подключение к API по base_url (пусто — адрес по умолчанию)."""
    base_url = normalize_url(base_url)
    if base_url in _clients:
        return Api(_clients[base_url], None, base_url)
    new_client, reason = connect(base_url)
    if new_client is not None:  # неудачи не кэшируем — адрес могут поправить
        _clients[base_url] = new_client
    return Api(new_client, reason, base_url)


class AIError(Exception):
    pass


# ---------------------------------------------------------------------------
# Настройки характера и силы игры
# ---------------------------------------------------------------------------

PERSONALITIES = {
    "friendly": "Ты дружелюбный и остроумный соперник: шутишь по-доброму, хвалишь удачные ходы, подбадриваешь.",
    "toxic": "Ты дерзкий соперник-трэштокер: подкалываешь, хвастаешься, язвишь в духе стримера. "
             "Без мата и без настоящих оскорблений — это игра.",
    "coach": "Ты шахматный тренер: коротко объясняешь идеи ходов, честно указываешь на ошибки игрока "
             "и подсказываешь, на что обратить внимание.",
}

# сложность -> (effort нейросети, инструкция по силе игры, глубина офлайн-движка)
DIFFICULTIES = {
    "easy": ("low", "Играй слабо, как новичок-любитель: не ищи сильнейший ход, иногда допускай неточности.", 1),
    "normal": ("medium", "Играй как крепкий любитель: аккуратно, без явных зевков.", 2),
    "hard": ("high", "Играй в полную силу: ищи сильнейший ход, считай тактику.", 3),
}

PIECE_NAMES = {
    chess.PAWN: "пешка", chess.KNIGHT: "конь", chess.BISHOP: "слон",
    chess.ROOK: "ладья", chess.QUEEN: "ферзь", chess.KING: "король",
}
PIECE_NAMES_ACC = {
    chess.PAWN: "пешку", chess.KNIGHT: "коня", chess.BISHOP: "слона",
    chess.ROOK: "ладью", chess.QUEEN: "ферзя", chess.KING: "короля",
}
COLOR_NAMES = {chess.WHITE: "белыми", chess.BLACK: "чёрными"}


# ---------------------------------------------------------------------------
# Описание позиции для нейросети
# ---------------------------------------------------------------------------

def parse_payload(data):
    """Восстанавливает доску из списка ходов (UCI), настройки и подключение к API."""
    api = get_client(data.get("api_base_url"))
    board = chess.Board()
    for uci in data.get("moves", []):
        board.push_uci(uci)
    ai_color = chess.WHITE if data.get("ai_color") == "white" else chess.BLACK
    personality = data.get("personality") if data.get("personality") in PERSONALITIES else "friendly"
    difficulty = data.get("difficulty") if data.get("difficulty") in DIFFICULTIES else "normal"
    return board, ai_color, personality, difficulty, api


def board_ascii(board):
    rows = []
    for rank in range(7, -1, -1):
        cells = []
        for file in range(8):
            piece = board.piece_at(chess.square(file, rank))
            cells.append(piece.symbol() if piece else ".")
        rows.append(f"{rank + 1} " + " ".join(cells))
    rows.append("  a b c d e f g h")
    return "\n".join(rows)


def history_text(board):
    replay = chess.Board()
    parts = []
    for move in board.move_stack:
        if replay.turn == chess.WHITE:
            parts.append(f"{replay.fullmove_number}.")
        parts.append(replay.san(move))
        replay.push(move)
    return " ".join(parts) or "(ходов ещё не было)"


def describe_move(board_before, move):
    """«конь g1-f3, взятие пешки, шах» — человеческое описание хода."""
    piece = board_before.piece_at(move.from_square)
    text = f"{PIECE_NAMES[piece.piece_type]} {chess.square_name(move.from_square)}-{chess.square_name(move.to_square)}"
    if board_before.is_castling(move):
        text = "рокировка в длинную сторону" if chess.square_file(move.to_square) == 2 else "рокировка в короткую сторону"
    elif board_before.is_capture(move):
        victim = chess.PAWN if board_before.is_en_passant(move) else board_before.piece_type_at(move.to_square)
        text += f", взятие: {PIECE_NAMES_ACC[victim]}"
    if move.promotion:
        text += f", превращение в {PIECE_NAMES_ACC[move.promotion]}"
    board_after = board_before.copy()
    board_after.push(move)
    if board_after.is_checkmate():
        text += ", МАТ"
    elif board_after.is_check():
        text += ", шах"
    return text


def threatened(board, color):
    """Фигуры цвета color, которые атакованы и не защищены (или атакованы более дешёвой фигурой)."""
    result = []
    for square, piece in board.piece_map().items():
        if piece.color != color or piece.piece_type == chess.KING:
            continue
        attackers = board.attackers(not color, square)
        if not attackers:
            continue
        cheapest = min(PIECE_VALUES[board.piece_type_at(a)] for a in attackers)
        if not board.attackers(color, square) or cheapest < PIECE_VALUES[piece.piece_type]:
            result.append(f"{PIECE_NAMES[piece.piece_type]} {chess.square_name(square)}")
    return ", ".join(result) or "нет"


def chat_text(chat):
    lines = []
    for item in chat[-10:]:
        who = "Игрок" if item.get("role") == "user" else "Ты"
        lines.append(f"{who}: {item.get('text', '')}")
    return "\n".join(lines) or "(пока не разговаривали)"


def position_block(board, ai_color, chat):
    return (
        f"Ты играешь {COLOR_NAMES[ai_color]}, игрок (человек) — {COLOR_NAMES[not ai_color]}.\n"
        f"Позиция (FEN): {board.fen()}\n"
        f"Доска (заглавные буквы — белые, строчные — чёрные, 8-я горизонталь сверху):\n{board_ascii(board)}\n"
        f"Партия: {history_text(board)}\n"
        f"Сейчас ход: {'твой' if board.turn == ai_color else 'игрока'}\n"
        f"Подсказка — твои фигуры под ударом: {threatened(board, ai_color)}\n"
        f"Подсказка — фигуры игрока под ударом: {threatened(board, not ai_color)}\n"
        f"Недавний разговор:\n{chat_text(chat)}\n"
    )


def system_prompt(ai_color, personality, difficulty):
    return (
        f"Ты — ИИ-соперник в шахматной партии против человека, играешь {COLOR_NAMES[ai_color]}.\n"
        f"{PERSONALITIES[personality]}\n"
        f"{DIFFICULTIES[difficulty][1]}\n\n"
        "Как говорить:\n"
        "- Пиши по-русски, живо и коротко: 1–2 предложения на реплику — их озвучивает синтезатор речи.\n"
        "- Без markdown, списков и эмодзи. Ходы называй по-человечески: «конь на f3», «пешка e4», «рокировка».\n"
        "- Комментируй только то, что реально есть на доске, ничего не выдумывай.\n"
    )


# ---------------------------------------------------------------------------
# Вызов нейросети
# ---------------------------------------------------------------------------

def ask_claude(api, system, prompt, effort, schema=None):
    """Один запрос к Claude через клиента api. Со schema возвращает dict, без неё — текст."""
    if api.client is None:
        raise AIError(api.offline_reason or "нейросеть не подключена")
    client = api.client
    output_config = {"effort": effort}
    if schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    kwargs = {}
    if USE_FALLBACKS:
        # при отказе модели запрос сам перезапустится на рекомендованной запасной модели
        kwargs = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config=output_config,
            **kwargs,
        )
    except anthropic.AuthenticationError:
        raise AIError("неверный или отсутствующий ANTHROPIC_API_KEY")
    except anthropic.RateLimitError:
        raise AIError("превышен лимит запросов к Claude API")
    except anthropic.APIStatusError as e:
        raise AIError(f"ошибка Claude API {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        raise AIError("нет связи с Claude API")
    except Exception as e:
        raise AIError(f"сбой запроса к Claude: {e}")

    if response.stop_reason == "refusal":
        raise AIError("модель отказалась отвечать")
    if response.stop_reason == "max_tokens":
        raise AIError("ответ модели обрезан (max_tokens)")
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    if schema is None:
        return text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise AIError("модель вернула некорректный JSON")


# ---------------------------------------------------------------------------
# Запасной офлайн-движок (альфа-бета), если нейросеть недоступна
# ---------------------------------------------------------------------------

PIECE_VALUES = {
    chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330,
    chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 20000,
}
MATE_SCORE = 100000


def _positional(piece, square):
    file, rank = chess.square_file(square), chess.square_rank(square)
    if piece.color == chess.BLACK:
        rank = 7 - rank
    center = 3.5 - max(abs(file - 3.5), abs(rank - 3.5))  # 0 на краю .. 3 в центре
    kind = piece.piece_type
    if kind == chess.PAWN:
        return rank * 8 + (center * 5 if 2 <= rank <= 5 else 0)
    if kind in (chess.KNIGHT, chess.BISHOP):
        return center * 10
    if kind == chess.ROOK:
        return 15 if rank == 6 else 0
    if kind == chess.QUEEN:
        return center * 3
    return -center * 8  # король в миттельшпиле прячется


def evaluate(board):
    """Оценка с точки зрения стороны, которая ходит."""
    if board.is_checkmate():
        return -MATE_SCORE
    if board.is_stalemate() or board.is_insufficient_material():
        return 0
    score = 0
    for square, piece in board.piece_map().items():
        value = PIECE_VALUES[piece.piece_type] + _positional(piece, square)
        score += value if piece.color == board.turn else -value
    return score


def _ordered(board, moves):
    def key(move):
        score = 0
        if board.is_capture(move):
            victim = chess.PAWN if board.is_en_passant(move) else board.piece_type_at(move.to_square)
            score += 10000 + 10 * PIECE_VALUES[victim] - PIECE_VALUES[board.piece_type_at(move.from_square)]
        if move.promotion:
            score += 9000
        return -score
    return sorted(moves, key=key)


def _quiesce(board, alpha, beta, depth):
    stand = evaluate(board)
    if stand >= beta:
        return beta
    alpha = max(alpha, stand)
    if depth == 0:
        return alpha
    for move in _ordered(board, [m for m in board.legal_moves if board.is_capture(m)]):
        board.push(move)
        score = -_quiesce(board, -beta, -alpha, depth - 1)
        board.pop()
        if score >= beta:
            return beta
        alpha = max(alpha, score)
    return alpha


def _negamax(board, depth, alpha, beta):
    if board.is_game_over():
        return evaluate(board)
    if depth == 0:
        return _quiesce(board, alpha, beta, 4)
    best = -math.inf
    for move in _ordered(board, list(board.legal_moves)):
        board.push(move)
        score = -_negamax(board, depth - 1, -beta, -alpha)
        board.pop()
        best = max(best, score)
        alpha = max(alpha, score)
        if alpha >= beta:
            break
    return best


def engine_move(board, difficulty):
    depth = DIFFICULTIES[difficulty][2]
    margin = 150 if difficulty == "easy" else 15  # из каких «почти лучших» ходов выбирать случайно
    work = board.copy()
    best = -math.inf
    scored = []
    for move in _ordered(work, list(work.legal_moves)):
        work.push(move)
        score = -_negamax(work, depth - 1, -math.inf, -(best - margin))
        work.pop()
        best = max(best, score)
        scored.append((score, move))
    candidates = [move for score, move in scored if score >= best - margin]
    return random.choice(candidates)


PHRASES_PLAYER = {
    "mate": ["Мат... Ну ты дал! Признаю, красиво.", "Всё, я заматован. Респект."],
    "check": ["Шах? Ничего, король уйдёт.", "Шах — неприятно, но не смертельно."],
    "capture": ["Забрал {piece}? Ладно, я это запомню.", "Минус {piece}. Посмотрим, чем ответить.",
                "Ты съел {piece}. Смело."],
    "castle": ["Рокировка — король в домике.", "Спрятал короля, разумно."],
    "promo": ["Новая фигура? Это было неизбежно...", "Превращение! Становится жарко."],
    "quiet": ["Ход {san}. Интересно, что ты задумал.", "Так, {san}. Принято.", "Хм, {san}. Посмотрим."],
}
PHRASES_AI = {
    "mate": ["Мат! Спасибо за игру.", "И это мат. Хорошая была партия."],
    "check": ["Шах! Куда пойдёт король?", "Объявляю шах."],
    "capture": ["Забираю {piece}.", "А {piece} я, пожалуй, съем."],
    "castle": ["Делаю рокировку — безопасность прежде всего.", "Король — в укрытие."],
    "promo": ["Пешка становится ферзём!", "Превращаю пешку."],
    "quiet": ["Хожу {san}.", "Мой ход — {san}.", "Пусть будет {san}."],
}


def offline_comment(board_before, move, phrases):
    san = board_before.san(move)
    after = board_before.copy()
    after.push(move)
    if after.is_checkmate():
        kind = "mate"
    elif move.promotion:
        kind = "promo"
    elif after.is_check():
        kind = "check"
    elif board_before.is_castling(move):
        kind = "castle"
    elif board_before.is_capture(move):
        kind = "capture"
    else:
        kind = "quiet"
    victim = chess.PAWN if board_before.is_en_passant(move) else board_before.piece_type_at(move.to_square)
    piece = PIECE_NAMES_ACC.get(victim, "фигуру")
    return random.choice(phrases[kind]).format(san=san, piece=piece)


# ---------------------------------------------------------------------------
# Эндпоинты
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    try:
        api = get_client(request.args.get("base_url"))
    except ValueError as e:
        return jsonify(ok=True, mode="offline", reason=str(e))
    if api.client:
        return jsonify(ok=True, mode="claude", model=MODEL, base_url=api.base_url)
    return jsonify(ok=True, mode="offline", reason=api.offline_reason, base_url=api.base_url)


@app.post("/move")
def move():
    data = request.get_json(force=True)
    try:
        board, ai_color, personality, difficulty, api = parse_payload(data)
    except ValueError as e:
        return jsonify(error=f"некорректный запрос: {e}"), 400
    if board.turn != ai_color or board.is_game_over(claim_draw=True):
        return jsonify(error="сейчас не ход ИИ"), 400

    player_move = None
    before_player = None
    if board.move_stack:
        player_move = board.peek()
        before_player = board.copy()
        before_player.pop()

    legal = {board.san(m): m for m in board.legal_moves}
    note = None
    try:
        if player_move:
            last = (f"Последний ход игрока: {before_player.san(player_move)} "
                    f"({describe_move(before_player, player_move)}).")
            about_player = "прокомментируй последний ход игрока: удачный он или ошибка и почему"
        else:
            last = "Игрок ещё не ходил — ты начинаешь партию белыми."
            about_player = "поприветствуй игрока перед партией"
        prompt = (
            position_block(board, ai_color, data.get("chat", []))
            + f"{last}\n"
            + f"Твои допустимые ходы: {', '.join(legal)}\n\n"
            + "Заполни ответ:\n"
            + f"- comment_on_player_move — {about_player};\n"
            + "- move — твой ход, строго из списка допустимых;\n"
            + "- comment_on_my_move — прокомментируй свой ход (если это мат — отпразднуй).\n"
        )
        schema = {
            "type": "object",
            "properties": {
                "comment_on_player_move": {"type": "string"},
                "move": {"type": "string", "enum": list(legal)},
                "comment_on_my_move": {"type": "string"},
            },
            "required": ["comment_on_player_move", "move", "comment_on_my_move"],
            "additionalProperties": False,
        }
        effort = DIFFICULTIES[difficulty][0]
        answer = ask_claude(api, system_prompt(ai_color, personality, difficulty), prompt, effort, schema)
        chosen = legal.get(answer.get("move"))
        if chosen is None:
            raise AIError(f"нейросеть выбрала недопустимый ход {answer.get('move')!r}")
        comment_player = answer.get("comment_on_player_move", "").strip()
        comment_ai = answer.get("comment_on_my_move", "").strip()
        source = "claude"
    except AIError as e:
        log.warning("Ход офлайн-движком: %s", e)
        # без ключа и своего адреса молча играем движком, иначе объясняем, что сломалось
        if api.client is not None or api.base_url:
            note = f"Нейросеть не ответила ({e}), сходил запасной движок."
        chosen = engine_move(board, difficulty)
        comment_player = offline_comment(before_player, player_move, PHRASES_PLAYER) if player_move \
            else "Привет! Начинаем, я играю белыми."
        comment_ai = offline_comment(board, chosen, PHRASES_AI)
        source = "engine"

    return jsonify(
        move=chosen.uci(),
        san=board.san(chosen),
        comment_player=comment_player,
        comment_ai=comment_ai,
        source=source,
        note=note,
    )


@app.post("/chat")
def chat():
    data = request.get_json(force=True)
    try:
        board, ai_color, personality, difficulty, api = parse_payload(data)
    except ValueError as e:
        return jsonify(error=f"некорректный запрос: {e}"), 400
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify(error="пустое сообщение"), 400

    player_turn = board.turn != ai_color and not board.is_game_over(claim_draw=True)
    player_legal = [board.san(m) for m in board.legal_moves] if player_turn else []

    properties = {"reply": {"type": "string"}}
    required = ["reply"]
    task = "Ответь игроку коротко (1–3 предложения) в поле reply."
    if player_turn:
        properties["player_move"] = {"type": "string", "enum": [""] + player_legal}
        required.append("player_move")
        task += (
            f"\nСейчас ход игрока. Его допустимые ходы: {', '.join(player_legal)}.\n"
            "Если игрок в этой реплике явно делает ход (например «конь f3», «е2 е4», «рокировка», «бью ферзя»), "
            "верни этот ход в player_move строго из списка, а в reply коротко отреагируй. "
            "Если игрок хода не называет или ход неоднозначен — player_move пустая строка "
            "(при неоднозначности переспроси в reply). Не делай ход за игрока по своей инициативе."
        )
    prompt = (
        position_block(board, ai_color, data.get("chat", []))
        + f"\nИгрок говорит тебе (распознано из голоса или набрано текстом): «{message}»\n\n{task}"
    )
    schema = {"type": "object", "properties": properties, "required": required, "additionalProperties": False}

    try:
        answer = ask_claude(api, system_prompt(ai_color, personality, difficulty), prompt, "low", schema)
    except AIError as e:
        log.warning("Чат недоступен: %s", e)
        return jsonify(reply="Я сейчас без нейросети и могу только играть. Ходи кликом или скажи ход, например «е2 е4».",
                       player_move=None, source="engine")

    player_move = None
    if answer.get("player_move"):
        try:
            player_move = board.parse_san(answer["player_move"]).uci()
        except ValueError:
            player_move = None
    return jsonify(reply=answer.get("reply", "").strip(), player_move=player_move, source="claude")


@app.post("/comment")
def comment():
    data = request.get_json(force=True)
    try:
        board, ai_color, personality, difficulty, api = parse_payload(data)
    except ValueError as e:
        return jsonify(error=f"некорректный запрос: {e}"), 400
    event = data.get("event") or "партия закончилась"
    prompt = (
        position_block(board, ai_color, data.get("chat", []))
        + f"\nСобытие: {event}\nОтреагируй на это одной-двумя фразами."
    )
    try:
        text = ask_claude(api, system_prompt(ai_color, personality, difficulty), prompt, "low")
        return jsonify(comment=text, source="claude")
    except AIError as e:
        log.warning("Комментарий офлайн: %s", e)
        return jsonify(comment=data.get("fallback") or "Спасибо за партию!", source="engine")


if __name__ == "__main__":
    default_api = get_client(None)
    mode = f"нейросеть {MODEL}" if default_api.client else f"офлайн-движок ({default_api.offline_reason})"
    log.info("ИИ-сервер на http://127.0.0.1:%s — режим: %s", PORT, mode)
    app.run(host="127.0.0.1", port=PORT, threaded=True)
