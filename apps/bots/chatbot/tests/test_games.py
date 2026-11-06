from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from chatbot.services.economy import get_balance, reconcile_account
from chatbot.services.games import (
    GameAlreadyFinished,
    GameExpired,
    MoveAlreadyMade,
    StaleGameAction,
    WrongParticipant,
    accept_pvp,
    accept_rps,
    blackjack_hit,
    blackjack_stay,
    create_challenge,
    decline_challenge,
    expire_games,
    make_rps_move,
    play_coinflip,
    roulette_multiplier,
    start_blackjack,
)


@pytest.mark.parametrize(
    ("pick", "number", "multiplier"),
    (
        ("0", 0, 36),
        ("green", 0, 36),
        ("red", 1, 2),
        ("black", 2, 2),
        ("even", 2, 2),
        ("odd", 3, 2),
        ("1st", 12, 3),
        ("2nd", 13, 3),
        ("3rd", 36, 3),
        ("red", 2, 0),
    ),
)
def test_roulette_rules(pick: str, number: int, multiplier: int) -> None:
    assert roulette_multiplier(pick, number) == multiplier


def test_coinflip_duplicate_delivery_pays_once(funded_db: Path) -> None:
    for _ in range(2):
        result = play_coinflip(
            -100,
            1,
            10,
            operation_key="flip:same-update",
            won=True,
            db_path=funded_db,
        )
        assert result.net == 10

    assert get_balance(-100, 1, db_path=funded_db) == 110
    assert reconcile_account(-100, 1, db_path=funded_db) == (110, 110)


def test_pvp_reserves_both_stakes_and_settles_once(funded_db: Path) -> None:
    challenge = create_challenge(
        "pvp",
        -100,
        1,
        2,
        20,
        db_path=funded_db,
    )
    assert get_balance(-100, 1, db_path=funded_db) == 80
    result = accept_pvp(
        challenge.game_id,
        -100,
        2,
        winner_id=1,
        db_path=funded_db,
    )
    assert result.winner_id == 1
    assert get_balance(-100, 1, db_path=funded_db) == 120
    assert get_balance(-100, 2, db_path=funded_db) == 80
    replay = accept_pvp(
        challenge.game_id,
        -100,
        2,
        winner_id=1,
        db_path=funded_db,
    )
    assert replay == result


def test_concurrent_pvp_accept_has_one_settlement(funded_db: Path) -> None:
    challenge = create_challenge(
        "pvp",
        -100,
        1,
        2,
        25,
        db_path=funded_db,
    )

    def accept() -> str:
        try:
            accept_pvp(
                challenge.game_id,
                -100,
                2,
                winner_id=1,
                db_path=funded_db,
            )
            return "settled"
        except GameAlreadyFinished:
            return "duplicate"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = sorted(executor.map(lambda _: accept(), range(2)))

    assert results == ["settled", "settled"]
    assert get_balance(-100, 1, db_path=funded_db) == 125
    assert get_balance(-100, 2, db_path=funded_db) == 75


def test_third_party_cannot_decline_challenge(funded_db: Path) -> None:
    challenge = create_challenge(
        "rps",
        -100,
        1,
        2,
        10,
        db_path=funded_db,
    )
    with pytest.raises(WrongParticipant):
        decline_challenge(
            challenge.game_id,
            -100,
            3,
            db_path=funded_db,
        )
    assert get_balance(-100, 1, db_path=funded_db) == 90


def test_rps_moves_persist_and_settle(funded_db: Path) -> None:
    challenge = create_challenge(
        "rps",
        -100,
        1,
        2,
        10,
        db_path=funded_db,
    )
    accept_rps(challenge.game_id, -100, 2, db_path=funded_db)
    waiting = make_rps_move(
        challenge.game_id,
        -100,
        1,
        "rock",
        db_path=funded_db,
    )
    assert waiting.status == "waiting"
    with pytest.raises(MoveAlreadyMade):
        make_rps_move(
            challenge.game_id,
            -100,
            1,
            "paper",
            db_path=funded_db,
        )
    settled = make_rps_move(
        challenge.game_id,
        -100,
        2,
        "scissors",
        db_path=funded_db,
    )
    assert settled.winner_id == 1
    assert get_balance(-100, 1, db_path=funded_db) == 110
    assert get_balance(-100, 2, db_path=funded_db) == 90


def test_expired_challenge_refunds_reservation(funded_db: Path) -> None:
    challenge = create_challenge(
        "pvp",
        -100,
        1,
        2,
        15,
        now=100,
        db_path=funded_db,
    )
    assert get_balance(-100, 1, db_path=funded_db) == 85
    assert expire_games(now=10_000, db_path=funded_db) == 1
    assert get_balance(-100, 1, db_path=funded_db) == 100
    with pytest.raises(GameExpired):
        accept_pvp(
            challenge.game_id,
            -100,
            2,
            now=10_001,
            db_path=funded_db,
        )
    assert get_balance(-100, 1, db_path=funded_db) == 100
    assert get_balance(-100, 2, db_path=funded_db) == 100


def test_blackjack_reserve_version_and_push(funded_db: Path) -> None:
    # pop order: player 10, 5; dealer 10, 7; next hit 2.
    deck = ["9", "8", "2", "7", "10", "5", "10"]
    game = start_blackjack(
        -100,
        1,
        10,
        deck=deck,
        db_path=funded_db,
    )
    assert game.status == "active"
    assert get_balance(-100, 1, db_path=funded_db) == 90
    hit = blackjack_hit(
        game.game_id,
        -100,
        1,
        expected_version=0,
        db_path=funded_db,
    )
    assert hit.player_score == 17
    assert hit.version == 1
    with pytest.raises(StaleGameAction):
        blackjack_hit(
            game.game_id,
            -100,
            1,
            expected_version=0,
            db_path=funded_db,
        )
    settled = blackjack_stay(
        game.game_id,
        -100,
        1,
        expected_version=1,
        db_path=funded_db,
    )
    assert settled.outcome == "push"
    assert get_balance(-100, 1, db_path=funded_db) == 100


def test_natural_blackjack_keeps_integer_balance(funded_db: Path) -> None:
    # pop order: player A, K; dealer 9, 5.
    result = start_blackjack(
        -100,
        1,
        5,
        deck=["5", "9", "K", "A"],
        db_path=funded_db,
    )
    assert result.outcome == "blackjack"
    assert result.net == 7
    assert get_balance(-100, 1, db_path=funded_db) == 107
