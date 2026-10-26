"""Расчёты: проценты по ст. 395 ГК РФ и госпошлины."""
import json
from calendar import isleap
from datetime import date, timedelta
from pathlib import Path

_RATES = sorted(
    (date.fromisoformat(d), r)
    for d, r in json.loads((Path(__file__).parent / "rates.json").read_text("utf-8"))["rates"]
)


def rate_on(d: date) -> float:
    cur = _RATES[0][1]
    for start, r in _RATES:
        if start <= d:
            cur = r
        else:
            break
    return cur


def interest_395(amount: float, due: date, until: date):
    """Проценты со дня, следующего за сроком оплаты, по дату `until` включительно.
    Возвращает (строки, итого). Строка: (с, по, дней, ставка, дней_в_году, сумма)."""
    start = due + timedelta(days=1)
    if until < start:
        return [], 0.0
    rows, d = [], start
    cur_key, cur_from, cur_days = None, start, 0
    while d <= until:
        key = (rate_on(d), 366 if isleap(d.year) else 365)
        if key != cur_key and cur_key is not None:
            rows.append((cur_from, d - timedelta(days=1), cur_days, *cur_key))
            cur_from, cur_days = d, 0
        cur_key = key
        cur_days += 1
        d += timedelta(days=1)
    rows.append((cur_from, until, cur_days, *cur_key))
    out, total = [], 0.0
    for f, t, n, rate, ydays in rows:
        s = round(amount * n * rate / 100 / ydays, 2)
        total += s
        out.append((f, t, n, rate, ydays, s))
    return out, round(total, 2)


def fee_full(amount: float, arbitr: bool) -> float:
    """Госпошлина по иску. ПРИБЛИЗИТЕЛЬНО — сверяйте с актуальной ст. 333.19 / 333.21 НК РФ."""
    if arbitr:
        if amount <= 100_000:
            return max(10_000, amount * 0.10)
        if amount <= 1_000_000:
            return 10_000 + (amount - 100_000) * 0.05
        return 55_000 + (amount - 1_000_000) * 0.03
    if amount <= 100_000:
        return max(4_000, amount * 0.04)
    if amount <= 300_000:
        return 4_000 + (amount - 100_000) * 0.03
    if amount <= 500_000:
        return 10_000 + (amount - 300_000) * 0.02
    return 14_000 + (amount - 500_000) * 0.01


def fee_order(amount: float, arbitr: bool) -> float:
    """Пошлина за судебный приказ = 50% от пошлины иска."""
    return round(fee_full(amount, arbitr) / 2, 2)


def money(x: float) -> str:
    return f"{x:,.2f}".replace(",", " ").replace(".00", "") + " ₽"
