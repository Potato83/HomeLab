from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .. import config
from ..database import connect
from .economy import (
    InsufficientFunds,
    _balance,
    _change_balance,
    _ensure_balance_headroom,
    consume_insurance,
    update_stats,
    validate_bet,
)

RNG = secrets.SystemRandom()
logger = logging.getLogger(__name__)
RED_NUMBERS = {
    1,
    3,
    5,
    7,
    9,
    12,
    14,
    16,
    18,
    19,
    21,
    23,
    25,
    27,
    30,
    32,
    34,
    36,
}
RPS_MOVES = {"rock", "paper", "scissors"}
RPS_WINS_AGAINST = {
    "rock": "scissors",
    "scissors": "paper",
    "paper": "rock",
}


def get_card_value(hand: list[str] | tuple[str, ...]) -> int:
    score = 0
    aces = 0
    for card in hand:
        if card in {"J", "Q", "K"}:
            score += 10
        elif card == "A":
            aces += 1
            score += 11
        else:
            score += int(card)

    while score > 21 and aces:
        score -= 10
        aces -= 1
    return score


def _insurance_refund(
    connection,
    chat_id: int,
    user_id: int,
    bet: int,
) -> tuple[int, bool]:
    refund = bet // 2
    if refund and consume_insurance(connection, chat_id, user_id):
        return refund, True
    return 0, False


class GameError(Exception):
    """Base class for expected game state failures."""


class GameNotFound(GameError):
    pass


class GameAlreadyFinished(GameError):
    pass


class GameExpired(GameError):
    pass


class NotParticipant(GameError):
    pass


class WrongParticipant(GameError):
    pass


class InvalidPick(GameError):
    pass


class MoveAlreadyMade(GameError):
    pass


class StaleGameAction(GameError):
    pass


@dataclass(frozen=True, slots=True)
class CasinoResult:
    game_id: str
    result: str
    won: bool
    net: int
    balance: int
    insurance_used: bool = False
    multiplier: int = 0


@dataclass(frozen=True, slots=True)
class BlackjackResult:
    game_id: str
    status: Literal["active", "settled"]
    player_hand: tuple[str, ...]
    dealer_hand: tuple[str, ...]
    player_score: int
    dealer_score: int | None
    outcome: str | None
    net: int
    insurance_used: bool = False
    version: int = 0
    bet: int = 0


@dataclass(frozen=True, slots=True)
class Challenge:
    game_id: str
    kind: Literal["pvp", "rps"]
    chat_id: int
    challenger_id: int
    target_id: int
    bet: int
    status: str


@dataclass(frozen=True, slots=True)
class DuelResult:
    game_id: str
    winner_id: int
    loser_id: int
    bet: int


@dataclass(frozen=True, slots=True)
class RPSResult:
    game_id: str
    status: Literal["waiting", "settled"]
    player_id: int
    move: str
    challenger_move: str | None = None
    target_move: str | None = None
    winner_id: int | None = None
    bet: int = 0


def _game_id() -> str:
    # 12 URL-safe characters; comfortably below Telegram's callback limit.
    return secrets.token_urlsafe(9)


def _serialize(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _deserialize(value: str) -> dict:
    return json.loads(value or "{}")


def _require_game(connection, game_id: str, kind: str | None = None):
    row = connection.execute(
        "SELECT * FROM games WHERE id = ?",
        (game_id,),
    ).fetchone()
    if row is None or (kind is not None and row["kind"] != kind):
        raise GameNotFound("game does not exist")
    return row


def _require_open(row, now: int) -> None:
    if row["status"] == "expired":
        raise GameExpired("game expired")
    if (
        row["status"] in {"pending", "active"}
        and int(row["expires_at"]) <= now
    ):
        raise GameExpired("game expired")
    if row["status"] not in {"pending", "active"}:
        raise GameAlreadyFinished(row["status"])


def roulette_multiplier(pick: str, result: int) -> int:
    normalized = pick.lower()
    if result < 0 or result > 36:
        raise ValueError("roulette result must be between 0 and 36")
    if not is_valid_roulette_pick(normalized):
        raise InvalidPick("invalid roulette pick")

    color = (
        "green"
        if result == 0
        else ("red" if result in RED_NUMBERS else "black")
    )
    if normalized == str(result) or (normalized == "green" and result == 0):
        return 36
    if normalized == color and normalized in {"red", "black"}:
        return 2
    if normalized == "even" and result != 0 and result % 2 == 0:
        return 2
    if normalized == "odd" and result != 0 and result % 2 != 0:
        return 2
    if normalized == "1-18" and 1 <= result <= 18:
        return 2
    if normalized == "19-36" and 19 <= result <= 36:
        return 2
    if normalized == "1st" and 1 <= result <= 12:
        return 3
    if normalized == "2nd" and 13 <= result <= 24:
        return 3
    if normalized == "3rd" and 25 <= result <= 36:
        return 3
    return 0


def is_valid_roulette_pick(pick: str) -> bool:
    normalized = pick.lower()
    if normalized.isdigit():
        return 0 <= int(normalized) <= 36 and normalized == str(int(normalized))
    return normalized in {
        "red",
        "black",
        "green",
        "even",
        "odd",
        "1st",
        "2nd",
        "3rd",
        "1-18",
        "19-36",
    }


def _insert_immediate_game(
    connection,
    *,
    game_id: str,
    kind: str,
    chat_id: int,
    user_id: int,
    bet: int,
    payload: dict,
    result: str,
    now: int,
) -> None:
    connection.execute(
        """
        INSERT INTO games (
            id, kind, chat_id, challenger_id, bet, status,
            challenger_reserved, payload, result, created_at,
            expires_at, settled_at
        )
        VALUES (?, ?, ?, ?, ?, 'settled', 0, ?, ?, ?, ?, ?)
        """,
        (
            game_id,
            kind,
            chat_id,
            user_id,
            bet,
            _serialize(payload),
            result,
            now,
            now,
            now,
        ),
    )


def play_coinflip(
    chat_id: int,
    user_id: int,
    bet: int,
    *,
    operation_key: str,
    won: bool | None = None,
    db_path: str | Path | None = None,
) -> CasinoResult:
    validate_bet(bet)
    now = int(time.time())
    game_id = operation_key

    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT * FROM games WHERE id = ?",
            (game_id,),
        ).fetchone()
        if existing:
            if (
                existing["kind"] != "flip"
                or int(existing["chat_id"]) != chat_id
                or int(existing["challenger_id"]) != user_id
                or int(existing["bet"]) != bet
            ):
                raise ValueError("operation key was reused for another game")
            payload = _deserialize(existing["payload"])
            return CasinoResult(
                game_id=game_id,
                result=existing["result"],
                won=bool(payload["won"]),
                net=int(payload["net"]),
                balance=_balance(connection, chat_id, user_id),
                insurance_used=bool(payload["insurance_used"]),
            )

        starting_balance = _balance(connection, chat_id, user_id)
        _ensure_balance_headroom(connection, chat_id, user_id, bet)
        is_win = RNG.choice((True, False)) if won is None else won
        _change_balance(
            connection,
            operation_key=f"{game_id}:stake",
            chat_id=chat_id,
            user_id=user_id,
            delta=-bet,
            kind="game_stake",
            reference_id=game_id,
        )

        insurance_used = False
        payout = 0
        if is_win:
            payout = bet * 2
            _change_balance(
                connection,
                operation_key=f"{game_id}:payout",
                chat_id=chat_id,
                user_id=user_id,
                delta=payout,
                kind="game_payout",
                reference_id=game_id,
            )
        else:
            payout, insurance_used = _insurance_refund(
                connection, chat_id, user_id, bet
            )
            if payout:
                _change_balance(
                    connection,
                    operation_key=f"{game_id}:insurance",
                    chat_id=chat_id,
                    user_id=user_id,
                    delta=payout,
                    kind="insurance_refund",
                    reference_id=game_id,
                )

        update_stats(connection, chat_id, user_id, won=is_win)
        final_balance = _balance(connection, chat_id, user_id)
        net = final_balance - starting_balance
        result = "heads" if is_win else "tails"
        payload = {
            "won": is_win,
            "net": net,
            "insurance_used": insurance_used,
        }
        _insert_immediate_game(
            connection,
            game_id=game_id,
            kind="flip",
            chat_id=chat_id,
            user_id=user_id,
            bet=bet,
            payload=payload,
            result=result,
            now=now,
        )
        return CasinoResult(
            game_id=game_id,
            result=result,
            won=is_win,
            net=net,
            balance=final_balance,
            insurance_used=insurance_used,
        )


def play_roulette(
    chat_id: int,
    user_id: int,
    bet: int,
    pick: str,
    *,
    operation_key: str,
    result_number: int | None = None,
    db_path: str | Path | None = None,
) -> CasinoResult:
    validate_bet(bet)
    normalized_pick = pick.lower()
    if not is_valid_roulette_pick(normalized_pick):
        raise InvalidPick("invalid roulette pick")
    now = int(time.time())
    game_id = operation_key

    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT * FROM games WHERE id = ?",
            (game_id,),
        ).fetchone()
        if existing:
            if (
                existing["kind"] != "roulette"
                or int(existing["chat_id"]) != chat_id
                or int(existing["challenger_id"]) != user_id
                or int(existing["bet"]) != bet
            ):
                raise ValueError("operation key was reused for another game")
            payload = _deserialize(existing["payload"])
            if payload.get("pick") != normalized_pick:
                raise ValueError("operation key was reused for another game")
            return CasinoResult(
                game_id=game_id,
                result=existing["result"],
                won=bool(payload["won"]),
                net=int(payload["net"]),
                balance=_balance(connection, chat_id, user_id),
                insurance_used=bool(payload["insurance_used"]),
                multiplier=int(payload["multiplier"]),
            )

        if normalized_pick.isdigit() or normalized_pick == "green":
            max_multiplier = 36
        elif normalized_pick in {"1st", "2nd", "3rd"}:
            max_multiplier = 3
        else:
            max_multiplier = 2
        _ensure_balance_headroom(
            connection,
            chat_id,
            user_id,
            bet * (max_multiplier - 1),
        )
        starting_balance = _balance(connection, chat_id, user_id)
        number = RNG.randrange(37) if result_number is None else result_number
        multiplier = roulette_multiplier(normalized_pick, number)
        _change_balance(
            connection,
            operation_key=f"{game_id}:stake",
            chat_id=chat_id,
            user_id=user_id,
            delta=-bet,
            kind="game_stake",
            reference_id=game_id,
        )
        won = multiplier > 0
        insurance_used = False
        if won:
            _change_balance(
                connection,
                operation_key=f"{game_id}:payout",
                chat_id=chat_id,
                user_id=user_id,
                delta=bet * multiplier,
                kind="game_payout",
                reference_id=game_id,
            )
        else:
            refund, insurance_used = _insurance_refund(
                connection, chat_id, user_id, bet
            )
            if refund:
                _change_balance(
                    connection,
                    operation_key=f"{game_id}:insurance",
                    chat_id=chat_id,
                    user_id=user_id,
                    delta=refund,
                    kind="insurance_refund",
                    reference_id=game_id,
                )

        update_stats(connection, chat_id, user_id, won=won)
        final_balance = _balance(connection, chat_id, user_id)
        net = final_balance - starting_balance
        color = (
            "green"
            if number == 0
            else ("red" if number in RED_NUMBERS else "black")
        )
        result = f"{number}:{color}"
        payload = {
            "pick": normalized_pick,
            "won": won,
            "net": net,
            "insurance_used": insurance_used,
            "multiplier": multiplier,
        }
        _insert_immediate_game(
            connection,
            game_id=game_id,
            kind="roulette",
            chat_id=chat_id,
            user_id=user_id,
            bet=bet,
            payload=payload,
            result=result,
            now=now,
        )
        return CasinoResult(
            game_id=game_id,
            result=result,
            won=won,
            net=net,
            balance=final_balance,
            insurance_used=insurance_used,
            multiplier=multiplier,
        )


def _new_deck() -> list[str]:
    deck = [
        "2",
        "3",
        "4",
        "5",
        "6",
        "7",
        "8",
        "9",
        "10",
        "J",
        "Q",
        "K",
        "A",
    ] * 4
    RNG.shuffle(deck)
    return deck


def _active_blackjack_result(row) -> BlackjackResult:
    payload = _deserialize(row["payload"])
    player_hand = tuple(payload["player_hand"])
    dealer_hand = tuple(payload["dealer_hand"])
    return BlackjackResult(
        game_id=row["id"],
        status="active",
        player_hand=player_hand,
        dealer_hand=dealer_hand[:1],
        player_score=get_card_value(player_hand),
        dealer_score=None,
        outcome=None,
        net=0,
        version=int(row["version"]),
        bet=int(row["bet"]),
    )


def start_blackjack(
    chat_id: int,
    user_id: int,
    bet: int,
    *,
    now: int | None = None,
    deck: list[str] | None = None,
    operation_key: str | None = None,
    db_path: str | Path | None = None,
) -> BlackjackResult:
    validate_bet(bet)
    current_time = int(time.time()) if now is None else now
    cards = list(deck) if deck is not None else _new_deck()
    if len(cards) < 4:
        raise ValueError("blackjack deck must contain at least four cards")
    player_hand = [cards.pop(), cards.pop()]
    dealer_hand = [cards.pop(), cards.pop()]
    game_id = operation_key or _game_id()

    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT * FROM games WHERE id = ?",
            (game_id,),
        ).fetchone()
        if existing:
            if (
                existing["kind"] != "blackjack"
                or int(existing["chat_id"]) != chat_id
                or int(existing["challenger_id"]) != user_id
                or int(existing["bet"]) != bet
            ):
                raise ValueError("operation key was reused for another game")
            if existing["status"] == "settled":
                return _settled_blackjack_result(connection, existing)
            if existing["status"] == "expired":
                raise GameExpired("blackjack game expired")
            if existing["status"] == "active":
                _require_open(existing, current_time)
            if existing["status"] == "active" and existing["message_id"] is None:
                return _active_blackjack_result(existing)
            raise GameAlreadyFinished("this blackjack request was processed")

        active = connection.execute(
            """
            SELECT * FROM games
            WHERE kind = 'blackjack' AND status = 'active'
              AND chat_id = ? AND challenger_id = ?
            """,
            (chat_id, user_id),
        ).fetchone()
        if active:
            _require_open(active, current_time)
            return _active_blackjack_result(active)

        player_score = get_card_value(player_hand)
        dealer_score = get_card_value(dealer_hand)
        player_natural = player_score == 21
        dealer_natural = dealer_score == 21
        if player_natural:
            maximum_net_credit = 0 if dealer_natural else (bet * 3) // 2
        elif dealer_natural:
            maximum_net_credit = bet // 2
        else:
            maximum_net_credit = bet
        _ensure_balance_headroom(
            connection,
            chat_id,
            user_id,
            maximum_net_credit,
        )

        _change_balance(
            connection,
            operation_key=f"{game_id}:stake",
            chat_id=chat_id,
            user_id=user_id,
            delta=-bet,
            kind="game_stake",
            reference_id=game_id,
        )
        payload = {
            "deck": cards,
            "player_hand": player_hand,
            "dealer_hand": dealer_hand,
        }
        connection.execute(
            """
            INSERT INTO games (
                id, kind, chat_id, challenger_id, bet, status,
                challenger_reserved, payload, created_at, expires_at
            )
            VALUES (?, 'blackjack', ?, ?, ?, 'active', 1, ?, ?, ?)
            """,
            (
                game_id,
                chat_id,
                user_id,
                bet,
                _serialize(payload),
                current_time,
                current_time + config.GAME_TTL_SECONDS,
            ),
        )

        if player_natural or dealer_natural:
            insurance_used = False
            if player_natural and dealer_natural:
                outcome, payout, won = "push", bet, False
                payout_kind = "game_refund"
            elif player_natural:
                payout = bet + (bet * 3) // 2
                outcome, won = "blackjack", True
                payout_kind = "game_payout"
            else:
                outcome, won = "loss", False
                payout, insurance_used = _insurance_refund(
                    connection, chat_id, user_id, bet
                )
                payout_kind = "insurance_refund"

            updated = connection.execute(
                """
                UPDATE games
                SET status = 'settled', challenger_reserved = 0,
                    result = ?, settled_at = ?, version = version + 1
                WHERE id = ? AND status = 'active'
                """,
                (outcome, current_time, game_id),
            )
            if updated.rowcount != 1:
                raise GameAlreadyFinished("blackjack game was already settled")
            if payout:
                _change_balance(
                    connection,
                    operation_key=f"{game_id}:payout",
                    chat_id=chat_id,
                    user_id=user_id,
                    delta=payout,
                    kind=payout_kind,
                    reference_id=game_id,
                )
            update_stats(connection, chat_id, user_id, won=won)
            return BlackjackResult(
                game_id=game_id,
                status="settled",
                player_hand=tuple(player_hand),
                dealer_hand=tuple(dealer_hand),
                player_score=player_score,
                dealer_score=dealer_score,
                outcome=outcome,
                net=payout - bet,
                insurance_used=insurance_used,
                version=1,
                bet=bet,
            )

        return BlackjackResult(
            game_id=game_id,
            status="active",
            player_hand=tuple(player_hand),
            dealer_hand=(dealer_hand[0],),
            player_score=player_score,
            dealer_score=None,
            outcome=None,
            net=0,
            version=0,
            bet=bet,
        )


def link_game_message(
    game_id: str,
    message_id: int,
    *,
    db_path: str | Path | None = None,
) -> None:
    with connect(db_path) as connection:
        connection.execute(
            "UPDATE games SET message_id = ? WHERE id = ?",
            (message_id, game_id),
        )


def _verify_blackjack_actor(
    row,
    *,
    chat_id: int,
    user_id: int,
    message_id: int | None,
) -> None:
    if int(row["chat_id"]) != chat_id or int(row["challenger_id"]) != user_id:
        raise NotParticipant("not your blackjack game")
    if row["message_id"] is not None and message_id != int(row["message_id"]):
        raise GameNotFound("callback does not belong to this game message")


def _settled_blackjack_result(connection, row) -> BlackjackResult:
    payload = _deserialize(row["payload"])
    ledger = connection.execute(
        """
        SELECT
            COALESCE(SUM(delta), 0) AS net,
            MAX(kind = 'insurance_refund') AS insurance_used
        FROM operations
        WHERE chat_id = ? AND user_id = ? AND reference_id = ?
        """,
        (row["chat_id"], row["challenger_id"], row["id"]),
    ).fetchone()
    player_hand = tuple(payload["player_hand"])
    dealer_hand = tuple(payload["dealer_hand"])
    return BlackjackResult(
        game_id=row["id"],
        status="settled",
        player_hand=player_hand,
        dealer_hand=dealer_hand,
        player_score=get_card_value(player_hand),
        dealer_score=get_card_value(dealer_hand),
        outcome=row["result"],
        net=int(ledger["net"]),
        insurance_used=bool(ledger["insurance_used"]),
        version=int(row["version"]),
        bet=int(row["bet"]),
    )


def get_blackjack_state(
    game_id: str,
    chat_id: int,
    user_id: int,
    *,
    message_id: int | None = None,
    now: int | None = None,
    db_path: str | Path | None = None,
) -> BlackjackResult:
    current_time = int(time.time()) if now is None else now
    with connect(db_path) as connection:
        row = _require_game(connection, game_id, "blackjack")
        _verify_blackjack_actor(
            row,
            chat_id=chat_id,
            user_id=user_id,
            message_id=message_id,
        )
        if row["status"] == "settled":
            return _settled_blackjack_result(connection, row)
        _require_open(row, current_time)
        return _active_blackjack_result(row)


def blackjack_hit(
    game_id: str,
    chat_id: int,
    user_id: int,
    *,
    message_id: int | None = None,
    expected_version: int | None = None,
    now: int | None = None,
    db_path: str | Path | None = None,
) -> BlackjackResult:
    current_time = int(time.time()) if now is None else now
    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _require_game(connection, game_id, "blackjack")
        _verify_blackjack_actor(
            row,
            chat_id=chat_id,
            user_id=user_id,
            message_id=message_id,
        )
        if row["status"] == "settled":
            return _settled_blackjack_result(connection, row)
        _require_open(row, current_time)
        if (
            expected_version is not None
            and int(row["version"]) != expected_version
        ):
            raise StaleGameAction("this blackjack button is stale")
        payload = _deserialize(row["payload"])
        deck = payload["deck"]
        player_hand = payload["player_hand"]
        dealer_hand = payload["dealer_hand"]
        player_hand.append(deck.pop())
        score = get_card_value(player_hand)
        payload["deck"] = deck
        payload["player_hand"] = player_hand

        if score < 21:
            connection.execute(
                """
                UPDATE games
                SET payload = ?, version = version + 1
                WHERE id = ? AND status = 'active'
                """,
                (_serialize(payload), game_id),
            )
            return BlackjackResult(
                game_id=game_id,
                status="active",
                player_hand=tuple(player_hand),
                dealer_hand=(dealer_hand[0],),
                player_score=score,
                dealer_score=None,
                outcome=None,
                net=0,
                version=int(row["version"]) + 1,
                bet=int(row["bet"]),
            )
        if score == 21:
            return _settle_blackjack(
                connection,
                row,
                payload,
                current_time,
            )

        bet = int(row["bet"])
        refund, insurance_used = _insurance_refund(
            connection, chat_id, user_id, bet
        )
        updated = connection.execute(
            """
            UPDATE games
            SET status = 'settled', challenger_reserved = 0,
                payload = ?, result = 'bust', settled_at = ?,
                version = version + 1
            WHERE id = ? AND status = 'active'
            """,
            (_serialize(payload), current_time, game_id),
        )
        if updated.rowcount != 1:
            raise GameAlreadyFinished("blackjack game was already settled")
        if refund:
            _change_balance(
                connection,
                operation_key=f"{game_id}:insurance",
                chat_id=chat_id,
                user_id=user_id,
                delta=refund,
                kind="insurance_refund",
                reference_id=game_id,
            )
        update_stats(connection, chat_id, user_id, won=False)
        return BlackjackResult(
            game_id=game_id,
            status="settled",
            player_hand=tuple(player_hand),
            dealer_hand=tuple(dealer_hand),
            player_score=score,
            dealer_score=get_card_value(dealer_hand),
            outcome="bust",
            net=-(bet - refund),
            insurance_used=insurance_used,
            version=int(row["version"]) + 1,
            bet=bet,
        )


def _settle_blackjack(
    connection,
    row,
    payload: dict,
    settled_at: int,
) -> BlackjackResult:
    game_id = row["id"]
    chat_id = int(row["chat_id"])
    user_id = int(row["challenger_id"])
    deck = payload["deck"]
    player_hand = payload["player_hand"]
    dealer_hand = payload["dealer_hand"]
    player_score = get_card_value(player_hand)
    while get_card_value(dealer_hand) < 17:
        dealer_hand.append(deck.pop())
    dealer_score = get_card_value(dealer_hand)
    bet = int(row["bet"])

    insurance_used = False
    if dealer_score > 21 or player_score > dealer_score:
        outcome = "win"
        payout = bet * 2
        won = True
        payout_kind = "game_payout"
    elif player_score == dealer_score:
        outcome = "push"
        payout = bet
        won = False
        payout_kind = "game_refund"
    else:
        outcome = "loss"
        payout, insurance_used = _insurance_refund(
            connection,
            chat_id,
            user_id,
            bet,
        )
        won = False
        payout_kind = "insurance_refund"

    payload["deck"] = deck
    payload["dealer_hand"] = dealer_hand
    updated = connection.execute(
        """
        UPDATE games
        SET status = 'settled', challenger_reserved = 0,
            payload = ?, result = ?, settled_at = ?,
            version = version + 1
        WHERE id = ? AND status = 'active'
        """,
        (_serialize(payload), outcome, settled_at, game_id),
    )
    if updated.rowcount != 1:
        raise GameAlreadyFinished("blackjack game was already settled")
    if payout:
        _change_balance(
            connection,
            operation_key=f"{game_id}:payout",
            chat_id=chat_id,
            user_id=user_id,
            delta=payout,
            kind=payout_kind,
            reference_id=game_id,
        )
    update_stats(connection, chat_id, user_id, won=won)
    return BlackjackResult(
        game_id=game_id,
        status="settled",
        player_hand=tuple(player_hand),
        dealer_hand=tuple(dealer_hand),
        player_score=player_score,
        dealer_score=dealer_score,
        outcome=outcome,
        net=payout - bet,
        insurance_used=insurance_used,
        version=int(row["version"]) + 1,
        bet=bet,
    )


def blackjack_stay(
    game_id: str,
    chat_id: int,
    user_id: int,
    *,
    message_id: int | None = None,
    expected_version: int | None = None,
    now: int | None = None,
    db_path: str | Path | None = None,
) -> BlackjackResult:
    current_time = int(time.time()) if now is None else now
    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _require_game(connection, game_id, "blackjack")
        _verify_blackjack_actor(
            row,
            chat_id=chat_id,
            user_id=user_id,
            message_id=message_id,
        )
        if row["status"] == "settled":
            return _settled_blackjack_result(connection, row)
        _require_open(row, current_time)
        if (
            expected_version is not None
            and int(row["version"]) != expected_version
        ):
            raise StaleGameAction("this blackjack button is stale")
        payload = _deserialize(row["payload"])
        return _settle_blackjack(connection, row, payload, current_time)


def create_challenge(
    kind: Literal["pvp", "rps"],
    chat_id: int,
    challenger_id: int,
    target_id: int,
    bet: int,
    *,
    now: int | None = None,
    operation_key: str | None = None,
    db_path: str | Path | None = None,
) -> Challenge:
    if kind not in {"pvp", "rps"}:
        raise ValueError("unsupported challenge kind")
    if challenger_id == target_id:
        raise WrongParticipant("cannot challenge yourself")
    validate_bet(bet)
    current_time = int(time.time()) if now is None else now
    game_id = operation_key or _game_id()

    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT * FROM games WHERE id = ?",
            (game_id,),
        ).fetchone()
        if existing:
            if (
                existing["kind"] != kind
                or int(existing["chat_id"]) != chat_id
                or int(existing["challenger_id"]) != challenger_id
                or int(existing["target_id"]) != target_id
                or int(existing["bet"]) != bet
            ):
                raise ValueError("operation key was reused for another game")
            return Challenge(
                game_id=game_id,
                kind=kind,
                chat_id=chat_id,
                challenger_id=challenger_id,
                target_id=target_id,
                bet=bet,
                status=existing["status"],
            )
        pending = connection.execute(
            """
            SELECT * FROM games
            WHERE kind = ? AND status = 'pending'
              AND chat_id = ? AND challenger_id = ?
              AND target_id = ? AND bet = ? AND expires_at > ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (kind, chat_id, challenger_id, target_id, bet, current_time),
        ).fetchone()
        if pending:
            return Challenge(
                game_id=pending["id"],
                kind=kind,
                chat_id=chat_id,
                challenger_id=challenger_id,
                target_id=target_id,
                bet=bet,
                status="pending",
            )
        _ensure_balance_headroom(connection, chat_id, challenger_id, bet)
        _ensure_balance_headroom(connection, chat_id, target_id, bet)
        _change_balance(
            connection,
            operation_key=f"{game_id}:challenger_stake",
            chat_id=chat_id,
            user_id=challenger_id,
            delta=-bet,
            kind="game_stake",
            reference_id=game_id,
        )
        connection.execute(
            """
            INSERT INTO games (
                id, kind, chat_id, challenger_id, target_id, bet,
                status, challenger_reserved, payload, created_at,
                expires_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'pending', 1, '{}', ?, ?)
            """,
            (
                game_id,
                kind,
                chat_id,
                challenger_id,
                target_id,
                bet,
                current_time,
                current_time + config.GAME_TTL_SECONDS,
            ),
        )
    return Challenge(
        game_id=game_id,
        kind=kind,
        chat_id=chat_id,
        challenger_id=challenger_id,
        target_id=target_id,
        bet=bet,
        status="pending",
    )


def _settle_duel(connection, row, *, winner_id: int, now: int) -> DuelResult:
    challenger_id = int(row["challenger_id"])
    target_id = int(row["target_id"])
    loser_id = target_id if winner_id == challenger_id else challenger_id
    bet = int(row["bet"])
    game_id = row["id"]
    updated = connection.execute(
        """
        UPDATE games
        SET status = 'settled', challenger_reserved = 0,
            target_reserved = 0, result = ?, settled_at = ?,
            version = version + 1
        WHERE id = ? AND status IN ('pending', 'active')
        """,
        (f"winner:{winner_id}", now, game_id),
    )
    if updated.rowcount != 1:
        raise GameAlreadyFinished("duel was already settled")
    _change_balance(
        connection,
        operation_key=f"{game_id}:payout",
        chat_id=int(row["chat_id"]),
        user_id=winner_id,
        delta=bet * 2,
        kind="game_payout",
        reference_id=game_id,
    )
    update_stats(
        connection,
        int(row["chat_id"]),
        winner_id,
        won=True,
    )
    update_stats(
        connection,
        int(row["chat_id"]),
        loser_id,
        won=False,
    )
    return DuelResult(
        game_id=game_id,
        winner_id=winner_id,
        loser_id=loser_id,
        bet=bet,
    )


def accept_pvp(
    game_id: str,
    chat_id: int,
    actor_id: int,
    *,
    now: int | None = None,
    winner_id: int | None = None,
    db_path: str | Path | None = None,
) -> DuelResult:
    current_time = int(time.time()) if now is None else now
    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _require_game(connection, game_id, "pvp")
        if int(row["chat_id"]) != chat_id or int(row["target_id"]) != actor_id:
            raise WrongParticipant("only the challenged player can accept")
        if row["status"] == "settled" and (row["result"] or "").startswith(
            "winner:"
        ):
            winner_id = int(row["result"].split(":", 1)[1])
            loser_id = (
                int(row["target_id"])
                if winner_id == int(row["challenger_id"])
                else int(row["challenger_id"])
            )
            return DuelResult(
                game_id=game_id,
                winner_id=winner_id,
                loser_id=loser_id,
                bet=int(row["bet"]),
            )
        _require_open(row, current_time)
        _ensure_balance_headroom(
            connection,
            chat_id,
            actor_id,
            int(row["bet"]),
        )
        _change_balance(
            connection,
            operation_key=f"{game_id}:target_stake",
            chat_id=chat_id,
            user_id=actor_id,
            delta=-int(row["bet"]),
            kind="game_stake",
            reference_id=game_id,
        )
        connection.execute(
            "UPDATE games SET target_reserved = 1 WHERE id = ?",
            (game_id,),
        )
        selected_winner = (
            winner_id
            if winner_id is not None
            else RNG.choice((int(row["challenger_id"]), actor_id))
        )
        if selected_winner not in {
            int(row["challenger_id"]),
            actor_id,
        }:
            raise WrongParticipant("winner must participate in the duel")
        return _settle_duel(
            connection,
            row,
            winner_id=selected_winner,
            now=current_time,
        )


def play_instant_pvp(
    chat_id: int,
    challenger_id: int,
    target_id: int,
    bet: int,
    *,
    now: int | None = None,
    operation_key: str | None = None,
    winner_id: int | None = None,
    db_path: str | Path | None = None,
) -> DuelResult:
    if challenger_id == target_id:
        raise WrongParticipant("cannot challenge yourself")
    validate_bet(bet)
    current_time = int(time.time()) if now is None else now
    game_id = operation_key or _game_id()
    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT * FROM games WHERE id = ?",
            (game_id,),
        ).fetchone()
        if existing:
            if (
                existing["kind"] != "pvp"
                or int(existing["chat_id"]) != chat_id
                or int(existing["challenger_id"]) != challenger_id
                or int(existing["target_id"]) != target_id
                or int(existing["bet"]) != bet
                or existing["status"] != "settled"
                or not (existing["result"] or "").startswith("winner:")
            ):
                raise ValueError("operation key was reused for another game")
            winner_id = int(existing["result"].split(":", 1)[1])
            loser_id = target_id if winner_id == challenger_id else challenger_id
            return DuelResult(
                game_id=game_id,
                winner_id=winner_id,
                loser_id=loser_id,
                bet=bet,
            )
        _ensure_balance_headroom(connection, chat_id, challenger_id, bet)
        _ensure_balance_headroom(connection, chat_id, target_id, bet)
        _change_balance(
            connection,
            operation_key=f"{game_id}:challenger_stake",
            chat_id=chat_id,
            user_id=challenger_id,
            delta=-bet,
            kind="game_stake",
            reference_id=game_id,
        )
        _change_balance(
            connection,
            operation_key=f"{game_id}:target_stake",
            chat_id=chat_id,
            user_id=target_id,
            delta=-bet,
            kind="game_stake",
            reference_id=game_id,
        )
        connection.execute(
            """
            INSERT INTO games (
                id, kind, chat_id, challenger_id, target_id, bet,
                status, challenger_reserved, target_reserved,
                payload, created_at, expires_at
            )
            VALUES (?, 'pvp', ?, ?, ?, ?, 'active', 1, 1, '{}', ?, ?)
            """,
            (
                game_id,
                chat_id,
                challenger_id,
                target_id,
                bet,
                current_time,
                current_time + config.GAME_TTL_SECONDS,
            ),
        )
        row = _require_game(connection, game_id, "pvp")
        selected_winner = (
            winner_id
            if winner_id is not None
            else RNG.choice((challenger_id, target_id))
        )
        if selected_winner not in {challenger_id, target_id}:
            raise WrongParticipant("winner must participate in the duel")
        return _settle_duel(
            connection,
            row,
            winner_id=selected_winner,
            now=current_time,
        )


def accept_rps(
    game_id: str,
    chat_id: int,
    actor_id: int,
    *,
    now: int | None = None,
    db_path: str | Path | None = None,
) -> Challenge:
    current_time = int(time.time()) if now is None else now
    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _require_game(connection, game_id, "rps")
        if int(row["chat_id"]) != chat_id or int(row["target_id"]) != actor_id:
            raise WrongParticipant("only the challenged player can accept")
        if row["status"] == "active":
            _require_open(row, current_time)
            return Challenge(
                game_id=game_id,
                kind="rps",
                chat_id=chat_id,
                challenger_id=int(row["challenger_id"]),
                target_id=actor_id,
                bet=int(row["bet"]),
                status="active",
            )
        _require_open(row, current_time)
        _ensure_balance_headroom(
            connection,
            chat_id,
            actor_id,
            int(row["bet"]),
        )
        _change_balance(
            connection,
            operation_key=f"{game_id}:target_stake",
            chat_id=chat_id,
            user_id=actor_id,
            delta=-int(row["bet"]),
            kind="game_stake",
            reference_id=game_id,
        )
        updated = connection.execute(
            """
            UPDATE games
            SET status = 'active', target_reserved = 1,
                payload = '{"moves":{}}', version = version + 1
            WHERE id = ? AND status = 'pending'
            """,
            (game_id,),
        )
        if updated.rowcount != 1:
            raise GameAlreadyFinished("challenge was already accepted")
        return Challenge(
            game_id=game_id,
            kind="rps",
            chat_id=chat_id,
            challenger_id=int(row["challenger_id"]),
            target_id=actor_id,
            bet=int(row["bet"]),
            status="active",
        )


def decline_challenge(
    game_id: str,
    chat_id: int,
    actor_id: int,
    *,
    now: int | None = None,
    db_path: str | Path | None = None,
) -> Challenge:
    current_time = int(time.time()) if now is None else now
    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _require_game(connection, game_id)
        participants = {int(row["challenger_id"]), int(row["target_id"])}
        if int(row["chat_id"]) != chat_id or actor_id not in participants:
            raise WrongParticipant("only a participant can decline")
        if row["status"] == "declined":
            return Challenge(
                game_id=game_id,
                kind=row["kind"],
                chat_id=chat_id,
                challenger_id=int(row["challenger_id"]),
                target_id=int(row["target_id"]),
                bet=int(row["bet"]),
                status="declined",
            )
        _require_open(row, current_time)
        if row["status"] != "pending":
            raise GameAlreadyFinished("an active game cannot be declined")
        updated = connection.execute(
            """
            UPDATE games
            SET status = 'declined', challenger_reserved = 0,
                settled_at = ?, version = version + 1
            WHERE id = ? AND status = 'pending'
            """,
            (current_time, game_id),
        )
        if updated.rowcount != 1:
            raise GameAlreadyFinished("challenge was already declined")
        if row["challenger_reserved"]:
            _change_balance(
                connection,
                operation_key=f"{game_id}:challenger_refund",
                chat_id=chat_id,
                user_id=int(row["challenger_id"]),
                delta=int(row["bet"]),
                kind="game_refund",
                reference_id=game_id,
            )
        return Challenge(
            game_id=game_id,
            kind=row["kind"],
            chat_id=chat_id,
            challenger_id=int(row["challenger_id"]),
            target_id=int(row["target_id"]),
            bet=int(row["bet"]),
            status="declined",
        )


def make_rps_move(
    game_id: str,
    chat_id: int,
    actor_id: int,
    move: str,
    *,
    now: int | None = None,
    db_path: str | Path | None = None,
) -> RPSResult:
    normalized_move = move.lower()
    if normalized_move not in RPS_MOVES:
        raise InvalidPick("invalid RPS move")
    current_time = int(time.time()) if now is None else now

    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _require_game(connection, game_id, "rps")
        challenger_id = int(row["challenger_id"])
        target_id = int(row["target_id"])
        if int(row["chat_id"]) != chat_id or actor_id not in {
            challenger_id,
            target_id,
        }:
            raise NotParticipant("not an RPS participant")

        payload = _deserialize(row["payload"])
        moves = payload.setdefault("moves", {})
        actor_key = str(actor_id)
        if row["status"] == "settled":
            if moves.get(actor_key) != normalized_move:
                raise GameAlreadyFinished("RPS game is already settled")
            challenger_move = moves[str(challenger_id)]
            target_move = moves[str(target_id)]
            result = row["result"] or ""
            winner_id = (
                None
                if result == "draw"
                else int(result.split(":", 1)[1])
            )
            return RPSResult(
                game_id=game_id,
                status="settled",
                player_id=actor_id,
                move=normalized_move,
                challenger_move=challenger_move,
                target_move=target_move,
                winner_id=winner_id,
                bet=int(row["bet"]),
            )
        _require_open(row, current_time)
        if row["status"] != "active":
            raise GameAlreadyFinished("RPS challenge has not been accepted")

        if actor_key in moves:
            raise MoveAlreadyMade("move already accepted")
        moves[actor_key] = normalized_move

        challenger_move = moves.get(str(challenger_id))
        target_move = moves.get(str(target_id))
        if not challenger_move or not target_move:
            connection.execute(
                """
                UPDATE games
                SET payload = ?, version = version + 1
                WHERE id = ? AND status = 'active'
                """,
                (_serialize(payload), game_id),
            )
            return RPSResult(
                game_id=game_id,
                status="waiting",
                player_id=actor_id,
                move=normalized_move,
                challenger_move=challenger_move,
                target_move=target_move,
                bet=int(row["bet"]),
            )

        bet = int(row["bet"])
        if challenger_move == target_move:
            winner_id = None
            result = "draw"
        else:
            challenger_won = (
                RPS_WINS_AGAINST[challenger_move] == target_move
            )
            winner_id = challenger_id if challenger_won else target_id
            result = f"winner:{winner_id}"

        updated = connection.execute(
            """
            UPDATE games
            SET status = 'settled', challenger_reserved = 0,
                target_reserved = 0, payload = ?, result = ?,
                settled_at = ?, version = version + 1
            WHERE id = ? AND status = 'active'
            """,
            (_serialize(payload), result, current_time, game_id),
        )
        if updated.rowcount != 1:
            raise GameAlreadyFinished("RPS game was already settled")

        if winner_id is None:
            for participant, suffix in (
                (challenger_id, "challenger"),
                (target_id, "target"),
            ):
                _change_balance(
                    connection,
                    operation_key=f"{game_id}:{suffix}_refund",
                    chat_id=chat_id,
                    user_id=participant,
                    delta=bet,
                    kind="game_refund",
                    reference_id=game_id,
                )
            update_stats(connection, chat_id, challenger_id, won=False)
            update_stats(connection, chat_id, target_id, won=False)
        else:
            loser_id = target_id if challenger_won else challenger_id
            _change_balance(
                connection,
                operation_key=f"{game_id}:payout",
                chat_id=chat_id,
                user_id=winner_id,
                delta=bet * 2,
                kind="game_payout",
                reference_id=game_id,
            )
            update_stats(connection, chat_id, winner_id, won=True)
            update_stats(connection, chat_id, loser_id, won=False)
        return RPSResult(
            game_id=game_id,
            status="settled",
            player_id=actor_id,
            move=normalized_move,
            challenger_move=challenger_move,
            target_move=target_move,
            winner_id=winner_id,
            bet=bet,
        )


def expire_games(
    *,
    now: int | None = None,
    db_path: str | Path | None = None,
    limit: int = 100,
) -> int:
    current_time = int(time.time()) if now is None else now
    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            """
            SELECT *
            FROM games
            WHERE status IN ('pending', 'active') AND expires_at <= ?
            ORDER BY expires_at
            LIMIT ?
            """,
            (current_time, limit),
        ).fetchall()
        for row in rows:
            game_id = row["id"]
            chat_id = int(row["chat_id"])
            bet = int(row["bet"])
            updated = connection.execute(
                """
                UPDATE games
                SET status = 'expired', challenger_reserved = 0,
                    target_reserved = 0, settled_at = ?,
                    version = version + 1
                WHERE id = ? AND status IN ('pending', 'active')
                """,
                (current_time, game_id),
            )
            if updated.rowcount != 1:
                continue
            if row["challenger_reserved"]:
                _change_balance(
                    connection,
                    operation_key=f"{game_id}:expiry_challenger_refund",
                    chat_id=chat_id,
                    user_id=int(row["challenger_id"]),
                    delta=bet,
                    kind="game_refund",
                    reference_id=game_id,
                )
            if row["target_reserved"] and row["target_id"] is not None:
                _change_balance(
                    connection,
                    operation_key=f"{game_id}:expiry_target_refund",
                    chat_id=chat_id,
                    user_id=int(row["target_id"]),
                    delta=bet,
                    kind="game_refund",
                    reference_id=game_id,
                )
        return len(rows)


async def run_game_cleanup_worker(*, interval_seconds: int = 30) -> None:
    while True:
        try:
            await asyncio.to_thread(expire_games)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Game cleanup worker iteration failed")
        await asyncio.sleep(interval_seconds)
