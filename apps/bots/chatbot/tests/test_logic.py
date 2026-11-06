import pytest
import datetime
from chatbot.handlers.helpers import get_moscow_today, parse_bet
from chatbot.services.games import get_card_value

def test_blackjack_logic():
    # Простые карты
    assert get_card_value(['2', '3']) == 5
    # Десятки и картинки
    assert get_card_value(['10', 'J', 'Q', 'K']) == 40
    # Туз как 11
    assert get_card_value(['A', '5']) == 16
    # Туз превращается в 1 при переборе
    assert get_card_value(['A', '10', 'K']) == 21  # 11+10+10 -> 1+10+10
    # Два туза
    assert get_card_value(['A', 'A']) == 12 # 11 + 1

def test_moscow_time():
    today = get_moscow_today()
    # Проверяем формат YYYY-MM-DD
    assert len(today) == 10
    assert today[4] == "-" and today[7] == "-"

def test_parse_bet():
    assert parse_bet("/bj 100", 500) == 100
    assert parse_bet("/bj 600", 500) == 0  # Больше баланса
    assert parse_bet("/bj abc", 500) == 0  # Не число
    assert parse_bet("/bj -10", 500) == 0  # Отрицательное
