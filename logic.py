# logic.py
from sqlalchemy.orm import Session
from models import (
    User, Gift, InventoryItem, Match, Bet, Currency, MatchStatus, SessionLocal
)
from typing import Dict, Tuple
import random
from datetime import datetime, timedelta

COMMISSION_PCT = 0.05

# --- утилиты ---

def get_or_create_user(s: Session, tg_id: int, username: str | None = None) -> User:
    user = s.query(User).filter_by(tg_id=tg_id).one_or_none()
    if not user:
        user = User(tg_id=tg_id, username=username or None, stars_balance=100)  # стартовый бонус
        s.add(user)
        s.commit()
    return user

def add_stars(s: Session, user: User, amount: int):
    user.stars_balance += amount
    s.commit()

def take_stars(s: Session, user: User, amount: int) -> bool:
    if user.stars_balance < amount:
        return False
    user.stars_balance -= amount
    s.commit()
    return True

def inventory_delta(s: Session, user: User, gift_code: str, qty_delta: int) -> bool:
    gift = s.query(Gift).filter_by(code=gift_code).one_or_none()
    if not gift:
        return False
    row = s.query(InventoryItem).filter_by(user_id=user.id, gift_id=gift.id).one_or_none()
    if not row:
        row = InventoryItem(user_id=user.id, gift_id=gift.id, qty=0)
        s.add(row)
        s.flush()
    if row.qty + qty_delta < 0:
        return False
    row.qty += qty_delta
    s.commit()
    return True

def parse_gifts_blob(blob: str) -> Dict[str, int]:
    result: Dict[str, int] = {}
    if not blob:
        return result
    parts = [p.strip() for p in blob.split(",") if p.strip()]
    for p in parts:
        code, qty = p.split(":")
        result[code.strip().upper()] = result.get(code.strip().upper(), 0) + int(qty)
    return result

def gifts_value(s: Session, gifts: Dict[str, int]) -> int:
    total = 0
    for code, qty in gifts.items():
        gift = s.query(Gift).filter_by(code=code).one_or_none()
        if not gift:
            continue
        total += gift.value_stars * qty
    return total

def cheapest_gift_in_pool(s: Session, gifts: Dict[str, int]) -> Tuple[str | None, int]:
    cheapest_code = None
    cheapest_value = 10**9
    for code, qty in gifts.items():
        gift = s.query(Gift).filter_by(code=code).one_or_none()
        if gift and qty > 0 and gift.value_stars < cheapest_value:
            cheapest_value = gift.value_stars
            cheapest_code = code
    if cheapest_code is None:
        return None, 0
    return cheapest_code, cheapest_value

# --- PvP ---

def create_match(s: Session, currency: Currency) -> Match:
    m = Match(status=MatchStatus.OPEN, currency=currency)
    s.add(m)
    s.commit()
    return m

def place_bet_stars(s: Session, m: Match, user: User, amount: int) -> Tuple[bool, str]:
    if m.status != MatchStatus.OPEN:
        return False, "Матч недоступен для ставок."
    if amount <= 0:
        return False, "Ставка должна быть больше нуля."
    if not take_stars(s, user, amount):
        return False, "Недостаточно звёзд."
    bet = Bet(match_id=m.id, user_id=user.id, amount_stars=amount, value_stars=amount)
    s.add(bet)
    m.total_value_stars += amount
    # если это вторая ставка — блокируем матч, чтобы не заливали третьи
    if len(m.bets) + 1 >= 2:
        m.status = MatchStatus.LOCKED
    s.commit()
    return True, f"Ставка {amount} ⭐️ принята."

def place_bet_gifts(s: Session, m: Match, user: User, gifts: Dict[str, int]) -> Tuple[bool, str]:
    if m.status != MatchStatus.OPEN:
        return False, "Матч недоступен для ставок."
    # списываем подарки из инвентаря
    # проверим, хватает ли
    for code, qty in gifts.items():
        if qty <= 0: return False, "Кол-во подарков должно быть > 0."
        gift_row = s.query(Gift).filter_by(code=code).one_or_none()
        if not gift_row:
            return False, f"Подарок {code} не существует."
        inv = s.query(InventoryItem).filter_by(user_id=user.id, gift_id=gift_row.id).one_or_none()
        if not inv or inv.qty < qty:
            return False, f"Недостаточно подарков {code}."
    # списание
    for code, qty in gifts.items():
        inventory_delta(s, user, code, -qty)
    val = gifts_value(s, gifts)
    blob = ",".join([f"{c}:{q}" for c, q in gifts.items()])
    bet = Bet(match_id=m.id, user_id=user.id, gifts_blob=blob, value_stars=val)
    s.add(bet)
    m.total_value_stars += val
    if len(m.bets) + 1 >= 2:
        m.status = MatchStatus.LOCKED
    s.commit()
    return True, f"Ставка подарками на {val} ⭐️ (номинал) принята."

def resolve_match(s: Session, m: Match) -> Tuple[int, int, int, dict]:
    """
    Возвращает: (winner_user_id, pool, commission_taken_stars, commission_detail)
    commission_detail: {type: "stars"|"gift", "gift_code"?: str, "gift_value"?: int}
    """
    if m.status not in (MatchStatus.LOCKED, MatchStatus.OPEN):
        raise ValueError("Матч уже завершен/отменен.")
    if len(m.bets) != 2:
        raise ValueError("Для розыгрыша нужны 2 ставки.")

    b1, b2 = m.bets
    pool = b1.value_stars + b2.value_stars

    # взвешенное случайное распределение шансов
    weights = [b1.value_stars, b2.value_stars]
    draw = random.random() * (weights[0] + weights[1])
    winner_bet = b1 if draw < weights[0] else b2
    loser_bet = b2 if winner_bet is b1 else b1
    winner_user_id = winner_bet.user_id

    # комиссия
    commission_stars = int(pool * COMMISSION_PCT)
    commission_detail = {"type": "stars", "value": commission_stars}

    if m.currency == Currency.GIFTS:
        # при ставках подарками комиссия: 5% от пула ИЛИ самый дешёвый подарок из общего пула,
        # если его стоимость больше, чем 5% пула — тогда забираем подарок
        # соберём общий пул подарков
        all_gifts: Dict[str, int] = {}
        for b in [b1, b2]:
            if b.gifts_blob:
                for code, qty in (item.split(":") for item in b.gifts_blob.split(",") if item):
                    all_gifts[code] = all_gifts.get(code, 0) + int(qty)
        cheapest_code, cheapest_val = cheapest_gift_in_pool(s, all_gifts)
        if cheapest_code and cheapest_val > commission_stars:
            commission_detail = {"type": "gift", "gift_code": cheapest_code, "gift_value": cheapest_val}
            commission_stars = cheapest_val

    payout = pool - commission_stars
    # начисляем победителю
    winner = s.query(User).get(winner_user_id)
    winner.stars_balance += payout

    m.status = MatchStatus.RESOLVED
    m.winner_user_id = winner_user_id
    m.resolved_at = datetime.utcnow()
    s.commit()
    return winner_user_id, pool, commission_stars, commission_detail

# --- антифрод (минимальный) ---

def can_start_match(s: Session, user: User) -> bool:
    """
    Простейший лимитер: не больше 1 нового матча в 10 секунд.
    """
    from models import Match, Bet
    ten_sec_ago = datetime.utcnow() - timedelta(seconds=10)
    recent = (
        s.query(Match)
        .join(Bet, Bet.match_id == Match.id)
        .filter(Bet.user_id == user.id, Match.created_at >= ten_sec_ago)
        .count()
    )
    return recent == 0
