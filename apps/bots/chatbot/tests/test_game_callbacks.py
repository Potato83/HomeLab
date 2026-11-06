"""Round trips through Telegram handlers and the real SQLite game ledger."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from potato_bot import database
from potato_bot.handlers import pve, pvp
from potato_bot.services import games
from potato_bot.services.economy import get_balance


def callback(data, actor=2, message_id=51):
    message=SimpleNamespace(chat=SimpleNamespace(id=-100),message_id=message_id,edit_text=AsyncMock())
    return SimpleNamespace(data=data,message=message,from_user=SimpleNamespace(id=actor,full_name='Player'),answer=AsyncMock())


@pytest.fixture
def game_db(funded_db,monkeypatch):
    monkeypatch.setattr(database,'DEFAULT_DB_PATH',funded_db)
    return funded_db


def row(game_db,game_id):
    with database.connect(game_db) as c:
        return dict(c.execute('SELECT * FROM games WHERE id=?',(game_id,)).fetchone())


@pytest.mark.parametrize('operation_key',[None,'pvp:-100:50'])
def test_pvp_accept_roundtrip_preserves_full_id_and_pays_once(game_db,operation_key):
    challenge=games.create_challenge('pvp',-100,1,2,20,operation_key=operation_key)
    query=callback('pvp:a:'+challenge.game_id)
    asyncio.run(pvp.pvp_accept(query))
    assert row(game_db,challenge.game_id)['status']=='settled'
    balances=[get_balance(-100,i) for i in (1,2)]
    assert sorted(balances)==[80,120]
    assert query.answer.await_args.args==()
    asyncio.run(pvp.pvp_accept(query))
    assert [get_balance(-100,i) for i in (1,2)]==balances


def test_decline_roundtrip_refunds_once(game_db):
    game=games.create_challenge('pvp',-100,1,2,20,operation_key='pvp:-100:50')
    query=callback('game:d:'+game.game_id)
    asyncio.run(pvp.challenge_decline(query))
    asyncio.run(pvp.challenge_decline(query))
    assert row(game_db,game.game_id)['status']=='declined'
    assert [get_balance(-100,i) for i in (1,2)]==[100,100]


def test_rps_accept_and_moves_roundtrip_keep_id_and_settle_once(game_db):
    game=games.create_challenge('rps',-100,1,2,10,operation_key='rps:-100:50')
    accepted=callback('rps:a:'+game.game_id)
    asyncio.run(pvp.rps_accepted(accepted))
    assert row(game_db,game.game_id)['status']=='active'
    assert accepted.answer.await_args.args==()
    first=callback('rps:m:rock:'+game.game_id,actor=1)
    second=callback('rps:m:scissors:'+game.game_id,actor=2)
    asyncio.run(pvp.rps_move(first))
    assert row(game_db,game.game_id)['status']=='active'
    asyncio.run(pvp.rps_move(second))
    assert row(game_db,game.game_id)['status']=='settled'
    assert [get_balance(-100,i) for i in (1,2)]==[110,90]
    asyncio.run(pvp.rps_move(second))
    assert [get_balance(-100,i) for i in (1,2)]==[110,90]


def blackjack():
    game=games.start_blackjack(-100,1,10,deck=['9','8','2','7','10','5','10'],operation_key='bj:-100:50')
    games.link_game_message(game.game_id,51)
    return game


def test_blackjack_roundtrip_hit_stale_button_stay_and_replay(game_db):
    game=blackjack()
    data=pve._blackjack_keyboard(game).inline_keyboard[0][0].callback_data
    query=callback(data,actor=1)
    asyncio.run(pve.bj_hit_handler(query))
    state=games.get_blackjack_state(game.game_id,-100,1,message_id=51)
    assert (state.player_score,state.version)==(17,1)
    assert get_balance(-100,1)==90
    asyncio.run(pve.bj_hit_handler(query))
    assert games.get_blackjack_state(game.game_id,-100,1,message_id=51).version==1
    assert 'обновлено' in query.answer.await_args.args[0]
    stay=callback(pve._blackjack_keyboard(state).inline_keyboard[0][1].callback_data,actor=1)
    asyncio.run(pve.bj_stay_handler(stay))
    assert row(game_db,game.game_id)['status']=='settled'
    assert get_balance(-100,1)==100
    asyncio.run(pve.bj_stay_handler(stay))
    assert get_balance(-100,1)==100


@pytest.mark.parametrize('actor,message_id,expected',[(3,51,'не ваша'),(1,999,'не найдена')])
def test_blackjack_rejects_foreign_actor_or_message_without_changing_game(game_db,actor,message_id,expected):
    game=blackjack()
    query=callback(pve._blackjack_keyboard(game).inline_keyboard[0][0].callback_data,actor=actor,message_id=message_id)
    asyncio.run(pve.bj_hit_handler(query))
    assert expected in query.answer.await_args.args[0]
    assert row(game_db,game.game_id)['version']==0
    assert get_balance(-100,1)==90


def test_challenger_cannot_accept_own_pvp_challenge(game_db):
    game=games.create_challenge('pvp',-100,1,2,20,operation_key='pvp:-100:50')
    query=callback('pvp:a:'+game.game_id,actor=1)
    asyncio.run(pvp.pvp_accept(query))
    assert 'не твой' in query.answer.await_args.args[0]
    assert row(game_db,game.game_id)['status']=='pending'
    assert [get_balance(-100,i) for i in (1,2)]==[80,100]


@pytest.mark.parametrize('data',['bj:h:','bj:h:game:bad','bj:h::0','bj:h:game:-1'])
def test_malformed_blackjack_callback_is_not_reported_as_finished(data):
    query=callback(data,actor=1)
    asyncio.run(pve.bj_hit_handler(query))
    assert query.answer.await_args.args[0]=='Некорректная игровая кнопка.'


@pytest.mark.parametrize('error,text',[(games.GameNotFound(),'не найдена'),(games.GameAlreadyFinished(),'уже завершена'),(games.GameExpired(),'Время игры истекло')])
def test_pvp_errors_distinguish_missing_finished_and_expired(error,text):
    query=callback('pvp:a:missing')
    asyncio.run(pvp._challenge_error(query,error))
    assert text in query.answer.await_args.args[0]
