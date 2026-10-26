"""Досудебка — Telegram-бот для самозанятых и фрилансеров (aiogram 3)."""
import asyncio
import html
import logging
import os
import re
import time
from datetime import date, datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           LabeledPrice, Message, PreCheckoutQuery)
from dotenv import load_dotenv

import content as T
import db
import docgen
from calc import fee_order, interest_395, money

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN", "")          # токен ЮKassa из @BotFather → Payments
DEMO = not PROVIDER_TOKEN                                  # без токена оплата эмулируется
e = html.escape
router = Router()


class S(StatesGroup):
    text = State()        # ввод текста в мастере
    choice = State()      # выбор кнопкой в мастере
    d_city = State()      # диагностика: город
    d_evid = State()      # диагностика: мульти-выбор
    court_name = State()
    chrono = State()
    partner = State()
    review = State()
    check = State()


# ───────────────────────── UI-хелперы ─────────────────────────
def kb(*rows) -> InlineKeyboardMarkup:
    def b(t, d):
        return InlineKeyboardButton(text=t, url=d) if d.startswith("http") else InlineKeyboardButton(text=t, callback_data=d)
    return InlineKeyboardMarkup(inline_keyboard=[[b(t, d) for t, d in row] for row in rows])


def bar(i: int, n: int) -> str:
    return "▰" * i + "▱" * (n - i)


def mof(ev):
    return ev.message if isinstance(ev, CallbackQuery) else ev


async def show(ev, text, markup=None):
    """Для кнопок — редактируем текущее сообщение (чистый экран), для текста — новое."""
    if isinstance(ev, CallbackQuery):
        try:
            await ev.message.edit_text(text, reply_markup=markup)
            return
        except TelegramBadRequest as ex:
            if "not modified" in str(ex):
                return
    await mof(ev).answer(text, reply_markup=markup)


async def send_doc(ev, data: bytes, name: str, caption: str = ""):
    await mof(ev).answer_document(BufferedInputFile(data, filename=name), caption=caption)


async def notify_admin(bot: Bot, text: str):
    if ADMIN_ID:
        try:
            await bot.send_message(ADMIN_ID, text)
        except Exception:
            logging.exception("admin notify failed")


MENU_KB = kb(
    [("🩺 Диагностика · бесплатно", "d:start")],
    [("📄 Претензия", "claim:start"), ("⚖️ Суд и приказ", "court:start")],
    [("🧾 Доказательства", "evid"), ("🛡 Щит · договоры", "shield")],
    [("📂 Мои дела", "cases"), ("🤝 Юрист-партнёр", "partner")],
    [("ℹ️ О сервисе", "about")],
)


async def main_menu(ev, state: FSMContext):
    await state.clear()
    await show(ev, T.MENU, MENU_KB)


@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    db.user(m.from_user.id, m.from_user.full_name)
    await m.answer(T.START, reply_markup=kb([("🚀 Поехали", "menu")], [("🩺 Сразу к диагностике", "d:start")]))


@router.message(Command("menu"))
async def menu_cmd(m: Message, state: FSMContext):
    await main_menu(m, state)


@router.callback_query(F.data == "menu")
async def menu_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await main_menu(cb, state)


@router.callback_query(F.data == "about")
async def about(cb: CallbackQuery):
    await cb.answer()
    await show(cb, T.ABOUT, kb([("🏠 Меню", "menu")]))


# ───────────────────────── Оплата ─────────────────────────
PRICES = {
    "1": (990, "Претензия", "Претензия + расчёт процентов + таймеры"),
    "2": (2990, "Претензия + Суд", "Претензия, судебный приказ, запасной иск, гайды по взысканию"),
    "3": (4990, "Максимум", "Всё из пакета «Претензия + Суд» + персональный разбор юристом"),
    "s30": (299, "Щит на 1 месяц", "Конструктор договоров, акты, проверка заказчиков"),
    "s365": (2490, "Щит на год", "Конструктор договоров, акты, проверка заказчиков"),
}


def paywall_kb(*keys):
    rows = [[(f"{PRICES[k][1]} — {PRICES[k][0]:,} ₽".replace(",", " "), f"buy:{k}")] for k in keys]
    return kb(*rows, [("🏠 Меню", "menu")])


async def paywall(ev, kind):
    if kind == "claim":
        await show(ev, "🔒 <b>Нужен пакет</b>\n\nПретензия с расчётом процентов по ст. 395 ГК, описью и таймерами.\n"
                       "Выбери пакет 👇", paywall_kb("1", "2", "3"))
    elif kind == "court":
        await show(ev, "🔒 <b>Судебный модуль</b>\n\nЗаявление о приказе, расчёт пошлины, запасной иск и гайды "
                       "по взысканию входят в пакет «Претензия + Суд».", paywall_kb("2", "3"))
    else:
        await show(ev, T.SHIELD_INFO + "\n\n🔒 Нужна подписка:", paywall_kb("s30", "s365"))


@router.callback_query(F.data.startswith("buy:"))
async def buy(cb: CallbackQuery, bot: Bot):
    await cb.answer()
    k = cb.data.split(":")[1]
    price, title, desc = PRICES[k]
    if DEMO:
        await show(cb, f"💳 <b>{title}</b> — {price:,} ₽".replace(",", " ") + f"\n\n{desc}\n\n"
                       "🧪 <i>Демо-режим: платёжный провайдер не подключён. Нажми кнопку, чтобы эмулировать оплату.</i>",
                   kb([("✅ Эмулировать оплату", f"pay:{k}")], [("⬅️ Назад", "menu")]))
        return
    await bot.send_invoice(cb.message.chat.id, title=title, description=desc, payload=k,
                           provider_token=PROVIDER_TOKEN, currency="RUB",
                           prices=[LabeledPrice(label=title, amount=price * 100)])


@router.callback_query(F.data.startswith("pay:"))
async def pay_demo(cb: CallbackQuery, bot: Bot):
    if not DEMO:
        return await cb.answer("Недоступно", show_alert=True)
    await cb.answer("Оплата эмулирована ✅")
    await grant(cb, bot, cb.data.split(":")[1])


@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)


@router.message(F.successful_payment)
async def paid(m: Message, bot: Bot):
    await grant(m, bot, m.successful_payment.invoice_payload)


async def grant(ev, bot: Bot, k: str):
    uid = ev.from_user.id
    db.user(uid)
    if k == "1":
        db.add_credits(uid, claim=1)
    elif k == "2":
        db.add_credits(uid, claim=1, court=1)
    elif k == "3":
        db.add_credits(uid, claim=1, court=1)
        await notify_admin(bot, f"💎 Пакет «Максимум» — нужен разбор дела. Пользователь: {ev.from_user.full_name} "
                                f"@{ev.from_user.username} id={uid}")
    elif k == "s30":
        db.add_shield(uid, 30)
    elif k == "s365":
        db.add_shield(uid, 365)
    await notify_admin(bot, f"💰 Оплата {PRICES[k][1]} ({PRICES[k][0]} ₽) от id={uid}{' [ДЕМО]' if DEMO else ''}")
    nxt = ("🛡 Открыть «Щит»", "shield") if k.startswith("s") else ("📄 Создать претензию", "claim:start")
    extra = "\n\n💎 Юрист свяжется с тобой для разбора дела." if k == "3" else ""
    await show(ev, f"✅ <b>Оплачено: {PRICES[k][1]}</b>{extra}\n\nМожно приступать 👇", kb([nxt], [("🏠 Меню", "menu")]))


# ───────────────────────── Диагностика ─────────────────────────
QS = [
    ("who", "Кто заказчик?", "Для претензии и суда должника нужно идентифицировать.",
     [("👤 Физлицо", "fiz"), ("🏢 ИП или ООО", "biz"), ("❓ Не знаю", "unk")]),
    ("contract", "Договор был?", "Письменный договор — самый сильный козырь, но не обязательный.",
     [("📄 Письменный", "written"), ("💬 Только переписка", "chat"), ("🗣 Только на словах", "oral")]),
    ("amount", "Сумма долга?", "От суммы зависят суд (мировой/арбитраж) и пошлина.",
     [("до 10 тыс.", "lt10"), ("10–50 тыс.", "10-50"), ("50–100 тыс.", "50-100"), ("100–500 тыс.", "100-500"),
      ("500 тыс. и больше", "500p")]),
    ("when", "Когда нужно было заплатить?", "Срок исковой давности — 3 года.",
     [("меньше 30 дней назад", "lt30"), ("1–6 месяцев", "1-6"), ("6 мес. – 3 года", "6-36"), ("больше 3 лет", "gt36")]),
    ("evid", "Что у тебя есть из доказательств?", "Отметь всё подходящее и жми «Готово».",
     [("💬 Переписка о задании и цене", "chat"), ("📦 Передал результат", "handover"), ("📝 Акт", "act"),
      ("🧾 Чек «Мой налог»", "check"), ("💸 Частичные оплаты", "partial"), ("👥 Свидетели", "witness")]),
    ("now", "Что сейчас с заказчиком?", "От этого зависит тактика.",
     [("🙂 На связи, обещает", "promises"), ("🙅 На связи, отказывает", "refuses"), ("🔇 Молчит", "silent"),
      ("🚫 Заблокировал", "blocked")]),
    ("status", "Твой статус?", "Определяет суд и вид документов.",
     [("🧾 Самозанятый", "npd"), ("👤 Физлицо", "fiz"), ("💼 ИП", "ip")]),
    ("result", "Результат остался у заказчика?", "Если не передан — работает право удержания (ст. 712 ГК).",
     [("✅ Да, пользуется", "uses"), ("🔒 Не передал", "notgiven")]),
]
N_Q = len(QS) + 1  # + город


def d_text(i):
    key, q, why, _ = QS[i]
    return f"<b>{bar(i + 1, N_Q)}  Вопрос {i + 1} из {N_Q}</b>\n\n{q}\n\n💡 <i>{why}</i>"


async def d_ask(ev, state: FSMContext, i: int):
    data = await state.get_data()
    ans = data.get("d", {})
    key, _, _, opts = QS[i]
    rows = []
    if key == "evid":
        await state.set_state(S.d_evid)
        sel = set(data.get("d_evid", []))
        rows = [[(("✅ " if v in sel else "⬜ ") + l, f"d:t:{v}")] for l, v in opts]
        rows.append([("✔️ Готово", "d:ok")])
    else:
        await state.set_state(None)
        rows = [[(l, f"d:a:{i}:{v}")] for l, v in opts]
    rows.append([("⬅️ Назад", f"d:b:{i}"), ("🏠 Меню", "menu")] if i > 0 else [("🏠 Меню", "menu")])
    await show(ev, d_text(i), kb(*rows))


@router.callback_query(F.data == "d:start")
async def d_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await state.update_data(d={}, d_evid=[])
    await show(cb, "🩺 <b>Диагностика</b> · 9 вопросов · ~2 минуты\n\nОценю шансы вернуть деньги и дам план "
                   "на первые 48 часов. Это бесплатно.", kb([("▶️ Начать", "d:go")], [("🏠 Меню", "menu")]))


@router.callback_query(F.data == "d:go")
async def d_go(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await d_ask(cb, state, 0)


@router.callback_query(F.data.startswith("d:b:"))
async def d_back(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await d_ask(cb, state, int(cb.data.split(":")[2]) - 1)


@router.callback_query(F.data.startswith("d:a:"))
async def d_answer(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    _, _, i, v = cb.data.split(":")
    i = int(i)
    data = await state.get_data()
    d = data.get("d", {})
    d[QS[i][0]] = v
    await state.update_data(d=d)
    if i + 1 < len(QS):
        await d_ask(cb, state, i + 1)
    else:
        await state.set_state(S.d_city)
        await show(cb, f"<b>{bar(N_Q, N_Q)}  Вопрос {N_Q} из {N_Q}</b>\n\n🏙 В каком городе ты и должник?\n"
                       "Напиши сообщением, например: <i>Казань</i>\n\n💡 <i>Нужно для определения подсудности.</i>",
                   kb([("⬅️ Назад", f"d:b:{len(QS)}")]))


@router.callback_query(S.d_evid, F.data.startswith("d:t:"))
async def d_toggle(cb: CallbackQuery, state: FSMContext):
    v = cb.data.split(":")[2]
    data = await state.get_data()
    sel = set(data.get("d_evid", []))
    sel ^= {v}
    await state.update_data(d_evid=list(sel))
    await cb.answer()
    await d_ask(cb, state, 4)


@router.callback_query(S.d_evid, F.data == "d:ok")
async def d_evid_ok(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    d = data.get("d", {})
    d["evid"] = data.get("d_evid", [])
    await state.update_data(d=d)
    await d_ask(cb, state, 5)


def score(d: dict):
    s, good, bad = 0, [], []
    c = {"written": 25, "chat": 15, "oral": 0}[d["contract"]]
    s += c
    (good if c >= 15 else bad).append({"written": "есть письменный договор", "chat": "условия зафиксированы в переписке",
                                       "oral": "нет договора и переписки по условиям"}[d["contract"]])
    ev = set(d.get("evid", []))
    pts = {"chat": 15, "handover": 15, "act": 10, "check": 10, "partial": 10, "witness": 3}
    es = min(45, sum(pts[x] for x in ev))
    s += es
    if "handover" in ev:
        good.append("подтверждена передача результата")
    else:
        bad.append("нет подтверждения передачи результата")
    if "partial" in ev:
        good.append("были частичные оплаты")
    if "chat" not in ev and d["contract"] != "written":
        bad.append("нечем доказать согласование цены")
    s += {"promises": 10, "refuses": 0, "silent": 0, "blocked": -5}[d["now"]]
    if d["now"] == "promises":
        good.append("заказчик признаёт долг/обещает платить")
    if d["now"] == "blocked":
        bad.append("заказчик заблокировал — фиксируй сейчас")
    if d["who"] == "unk":
        s -= 10
        bad.append("должник не идентифицирован (нужны ФИО/ИНН)")
    else:
        s += 10
    if d["result"] == "notgiven":
        s += 5
        good.append("результат у тебя — можно удержать")
    pct = max(5, min(95, s + 5))
    if d["when"] == "gt36":
        pct = min(pct, 15)
        bad.insert(0, "срок исковой давности (3 года) мог истечь")
    level = "high" if pct >= 65 else "mid" if pct >= 40 else "low"
    return pct, level, good, bad


@router.message(S.d_city)
async def d_city(m: Message, state: FSMContext):
    data = await state.get_data()
    d = data["d"]
    d["city"] = m.text.strip()[:60]
    await state.update_data(d=d)
    pct, level, good, bad = score(d)
    icon, word = {"high": ("🟢", "ВЫСОКИЕ"), "mid": ("🟡", "СРЕДНИЕ"), "low": ("🔴", "НИЗКИЕ")}[level]
    lines = [f"✅ {x}" for x in good[:2]] + [f"⚠️ {x}" for x in bad[:2]]
    cl = list(T.CHECKLIST_BASE)
    if d["now"] == "blocked":
        cl[0] = "Заказчик заблокировал — пиши ему на e-mail/почтой, переписку из других каналов сохрани."
    tip = ""
    if d["when"] == "gt36":
        tip = ("\n\n⏳ <b>Срок давности:</b> 3 года с даты неоплаты. Если должник признавал долг письменно или платил "
               "частями — срок мог начаться заново (ст. 203 ГК). Нужен разбор юриста.")
    elif d["amount"] == "500p":
        tip = "\n\n💼 <b>Сумма >500 тыс. ₽</b> — приказное производство недоступно, лучше передать дело юристу-партнёру."
    text = (f"🩺 <b>Вердикт</b>\n\nШансы взыскания: {icon} <b>{word}</b> (~{pct}%)\n\n" + "\n".join(lines) +
            f"{tip}\n\n📋 <b>План на 48 часов</b>\n" + "\n".join(f"{i + 1}. {x}" for i, x in enumerate(cl)) +
            "\n\n<i>Оценка ориентировочная и не является гарантией результата.</i>")
    rows = [[("📄 Претензия — 990 ₽", "buy:1")], [("⚖️ Претензия + Суд — 2 990 ₽", "buy:2")],
            [("💎 Максимум — 4 990 ₽", "buy:3")]]
    if d["amount"] == "500p" or level == "low":
        rows.append([("🤝 Передать юристу", "partner")])
    rows.append([("🧾 Как собрать доказательства", "evid"), ("🏠 Меню", "menu")])
    await state.set_state(None)
    await m.answer(text, reply_markup=kb(*rows))


# ───────────────────────── Мастер (пошаговый ввод) ─────────────────────────
Z = lambda key, prompt, kind="text", opts=None: (key, prompt, kind, opts or [])
FLOWS = {
    "claim": [
        Z("debtor_type", "Кто должник?", "choice", [("👤 Физлицо", "fiz"), ("💼 ИП", "ip"), ("🏢 ООО / юрлицо", "ooo")]),
        Z("debtor_name", "Укажи <b>ФИО или название</b> должника (+ ИНН, если знаешь)."),
        Z("debtor_addr", "Адрес должника (для юрлица — юридический адрес из ЕГРЮЛ)."),
        Z("basis", "На чём основана договорённость?", "choice",
          [("📄 Письменный договор", "contract"), ("💬 Переписка в мессенджере", "chat"), ("🗣 Устная", "oral")]),
        Z("service", "Что ты сделал? Опиши коротко.\n<i>Например: монтаж рекламного ролика 60 сек.</i>"),
        Z("amount", "💰 Сумма долга в рублях (без процентов).", "money"),
        Z("due", "📅 Когда заказчик должен был заплатить?\nФормат: <b>ДД.ММ.ГГГГ</b>", "date"),
        Z("my_status", "Твой статус?", "choice", [("🧾 Самозанятый", "npd"), ("👤 Физлицо", "fiz"), ("💼 ИП", "ip")]),
        Z("my_name", "Твои <b>ФИО</b> полностью."),
        Z("my_addr", "Твой почтовый адрес."),
        Z("my_bank", "Реквизиты для оплаты (банк, БИК, счёт, получатель — или номер карты/СБП)."),
    ],
    "contract": [
        Z("exec_name", "ФИО исполнителя (тебя)."),
        Z("cust_name", "ФИО/название заказчика."),
        Z("subject", "Что делаешь? Предмет договора."),
        Z("term", "Срок выполнения.\n<i>Например: 10 рабочих дней с момента предоплаты</i>"),
        Z("price", "💰 Стоимость в рублях.", "money"),
        Z("prepay", "Размер предоплаты?", "choice", [("30%", "30"), ("50% (рекомендую)", "50"), ("100%", "100")]),
        Z("exec_contact", "Твои контакты для переписки (телефон/@ник/e-mail)."),
        Z("cust_contact", "Контакты заказчика (телефон/@ник/e-mail)."),
    ],
    "act": [
        Z("exec_name", "ФИО исполнителя."), Z("cust_name", "ФИО/название заказчика."),
        Z("contract_date", "Дата договора (ДД.ММ.ГГГГ)."), Z("subject", "Что передано заказчику?"),
        Z("price", "💰 Стоимость в рублях.", "money"),
    ],
}


def parse_money(t: str):
    t = re.sub(r"[^\d.,]", "", t.replace(" ", "")).replace(",", ".")
    try:
        v = float(t)
        return v if v > 0 else None
    except ValueError:
        return None


def parse_date(t: str):
    try:
        return datetime.strptime(t.strip(), "%d.%m.%Y").date()
    except ValueError:
        return None


async def w_start(ev, state: FSMContext, flow: str, extra=None):
    await state.clear()
    await state.update_data(flow=flow, i=0, w={}, extra=extra or {})
    await w_ask(ev, state)


async def w_ask(ev, state: FSMContext):
    data = await state.get_data()
    steps, i = FLOWS[data["flow"]], data["i"]
    key, prompt, kind, opts = steps[i]
    rows = []
    if kind == "choice":
        await state.set_state(S.choice)
        rows = [[(l, f"w:c:{j}")] for j, (l, _) in enumerate(opts)]
    else:
        await state.set_state(S.text)
    rows.append(([("⬅️ Назад", "w:b")] if i > 0 else []) + [("✖️ Отмена", "menu")])
    await show(ev, f"<b>{bar(i, len(steps))}  Шаг {i + 1} из {len(steps)}</b>\n\n{prompt}", kb(*rows))


async def w_store(ev, state: FSMContext, value):
    data = await state.get_data()
    w = data["w"]
    w[FLOWS[data["flow"]][data["i"]][0]] = value
    i = data["i"] + 1
    await state.update_data(w=w, i=i)
    if i >= len(FLOWS[data["flow"]]):
        await FINISH[data["flow"]](ev, state, w, data.get("extra", {}))
    else:
        await w_ask(ev, state)


@router.callback_query(S.choice, F.data.startswith("w:c:"))
async def w_choice(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    _, _, _, opts = FLOWS[data["flow"]][data["i"]]
    await w_store(cb, state, opts[int(cb.data.split(":")[2])][1])


@router.callback_query(F.data == "w:b")
async def w_back(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if "flow" not in data:
        return await main_menu(cb, state)
    await state.update_data(i=max(0, data["i"] - 1))
    await w_ask(cb, state)


@router.message(S.text, F.text)
async def w_text(m: Message, state: FSMContext):
    data = await state.get_data()
    _, _, kind, _ = FLOWS[data["flow"]][data["i"]]
    t = m.text.strip()
    if kind == "money":
        v = parse_money(t)
        if v is None:
            return await m.answer("🤔 Не понял сумму. Напиши числом, например <b>35000</b>.")
        return await w_store(m, state, v)
    if kind == "date":
        v = parse_date(t)
        if v is None:
            return await m.answer("🤔 Формат даты: <b>ДД.ММ.ГГГГ</b>, например 25.03.2026.")
        if v >= date.today():
            return await m.answer("⏳ Срок оплаты ещё не наступил — претензию можно писать после просрочки.")
        return await w_store(m, state, v.isoformat())
    await w_store(m, state, t[:300])


# ───────────────────────── Претензия ─────────────────────────
@router.callback_query(F.data == "claim:start")
async def claim_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if db.user(cb.from_user.id)["claim_credits"] <= 0:
        return await paywall(cb, "claim")
    await w_start(cb, state, "claim")


async def finish_claim(ev, state: FSMContext, w: dict, extra):
    due = date.fromisoformat(w["due"])
    rows, total = interest_395(float(w["amount"]), due, date.today())
    await state.update_data(pending=w)
    await state.set_state(None)
    await show(ev, (
        f"🔎 <b>Проверь данные</b>\n\n"
        f"Должник: <b>{e(w['debtor_name'])}</b>\nУслуга: {e(w['service'])}\n"
        f"Долг: <b>{money(w['amount'])}</b> · срок оплаты {docgen.d2s(due)}\n"
        f"Проценты (ст. 395 ГК, {len(rows)} период.): <b>{money(total)}</b>\n"
        f"────────────\nИтого к требованию: <b>{money(float(w['amount']) + total)}</b>\n\n"
        f"Всё верно?"), kb([("✅ Сформировать претензию", "claim:go")], [("✏️ Заполнить заново", "claim:start")],
                            [("🏠 Меню", "menu")]))


@router.callback_query(F.data == "claim:go")
async def claim_go(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    w = data.get("pending")
    if not w:
        return await main_menu(cb, state)
    uid = cb.from_user.id
    if not db.use_credit(uid, "claim"):
        return await paywall(cb, "claim")
    doc, total, grand = docgen.claim_doc(w)
    w["created_iso"] = date.today().isoformat()
    cid = db.add_case(uid, f"{w['debtor_name'][:30]} · {money(w['amount'])}", w)
    db.add_reminder(uid, cid, 10, f"⏰ <b>Прошло 10 дней</b> с отправки претензии «{e(w['debtor_name'][:30])}». "
                                  f"Заказчик ответил или заплатил?")
    db.add_reminder(uid, cid, 30, f"🚨 <b>30 дней без оплаты</b> по делу «{e(w['debtor_name'][:30])}». "
                                  f"Пора подавать заявление о судебном приказе.")
    await state.clear()
    await send_doc(cb, doc, "pretenziya.docx",
                   f"📄 Претензия готова. К требованию: {money(grand)} (долг {money(w['amount'])} + проценты {money(total)})")
    await mof(cb).answer("✅ <b>Дело создано</b>, таймеры на 10 и 30 дней включены.\nЧто дальше?",
                         reply_markup=kb([("📬 Как отправить", "guide:send")], [("💬 Заказчик ответил", "guide:neg")],
                                         [("⚖️ Перейти к суду", f"court:case:{cid}")],
                                         [("📂 Открыть дело", f"case:{cid}"), ("🏠 Меню", "menu")]))


@router.callback_query(F.data.in_({"guide:send", "guide:neg", "guide:bank", "guide:fssp"}))
async def guides(cb: CallbackQuery):
    await cb.answer()
    txt = {"guide:send": T.SEND_GUIDE, "guide:neg": T.NEGOTIATE, "guide:bank": T.EXEC_BANK, "guide:fssp": T.EXEC_FSSP}[cb.data]
    await show(cb, txt, kb([("📂 Мои дела", "cases"), ("🏠 Меню", "menu")]))


# ───────────────────────── Мои дела ─────────────────────────
@router.callback_query(F.data == "cases")
async def cases(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    u = db.user(cb.from_user.id)
    until = "активен до " + datetime.fromtimestamp(u["shield_until"]).strftime("%d.%m.%Y") \
        if u["shield_until"] > time.time() else "не активен"
    head = (f"📂 <b>Мои дела</b>\n\n🎟 Кредиты: претензий <b>{u['claim_credits']}</b> · судов <b>{u['court_credits']}</b>\n"
            f"🛡 Щит: {until}\n")
    cs = db.cases(cb.from_user.id)
    if not cs:
        return await show(cb, head + "\nДел пока нет.", kb([("📄 Создать претензию", "claim:start")], [("🏠 Меню", "menu")]))
    rows = [[(f"📁 #{c['id']} · {c['title']} · {c['status']}", f"case:{c['id']}")] for c in cs[:10]]
    await show(cb, head + "\nВыбери дело:", kb(*rows, [("🏠 Меню", "menu")]))


@router.callback_query(F.data.regexp(r"^case:\d+$"))
async def case_view(cb: CallbackQuery):
    await cb.answer()
    c = db.case(int(cb.data.split(":")[1]), cb.from_user.id)
    if not c:
        return await cb.message.answer("Дело не найдено.")
    d = c["data"]
    rows, total = interest_395(float(d["amount"]), date.fromisoformat(d["due"]), date.today())
    await show(cb, (f"📁 <b>Дело #{c['id']}</b>\nСтатус: <b>{c['status']}</b>\n\nДолжник: {e(d['debtor_name'])}\n"
                    f"Долг: {money(d['amount'])} · проценты на сегодня: {money(total)}\n"
                    f"Срок оплаты: {docgen.d2s(d['due'])}"),
               kb([("📥 Претензия заново", f"case:doc:{c['id']}")], [("⚖️ Суд и приказ", f"court:case:{c['id']}")],
                  [("🏦 Банк", "guide:bank"), ("👮 ФССП", "guide:fssp")], [("💰 Деньги пришли", f"case:paid:{c['id']}")],
                  [("⬅️ К делам", "cases")]))


@router.callback_query(F.data.startswith("case:doc:"))
async def case_doc(cb: CallbackQuery):
    await cb.answer()
    c = db.case(int(cb.data.split(":")[2]), cb.from_user.id)
    if c:
        doc, _, _ = docgen.claim_doc(c["data"])
        await send_doc(cb, doc, "pretenziya.docx", "📄 Претензия с пересчитанными на сегодня процентами")


@router.callback_query(F.data.startswith("case:paid:"))
async def case_paid(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    cid = int(cb.data.split(":")[2])
    c = db.case(cid, cb.from_user.id)
    if not c:
        return
    db.set_status(cid, "оплачено ✅")
    db.cancel_reminders(cid)
    await state.set_state(S.review)
    await state.update_data(review_case=cid, review_bonus=c["status"] != "оплачено ✅")
    await show(cb, "🎉 <b>Поздравляю, деньги вернулись!</b>\n\nНапиши отзыв-кейс одним сообщением: сколько вернул "
                   "и за сколько дней. За отзыв — <b>3 месяца «Щита» в подарок</b> 🎁",
               kb([("⏭ Пропустить", "menu")]))


@router.message(S.review, F.text)
async def review(m: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await notify_admin(bot, f"⭐️ Отзыв от {m.from_user.full_name} (дело #{data.get('review_case')}):\n{m.text}")
    if data.get("review_bonus"):
        db.add_shield(m.from_user.id, 90)
    await state.clear()
    await m.answer("🙏 Спасибо! 3 месяца «Щита» начислены 🎁" if data.get("review_bonus") else "🙏 Спасибо за отзыв!",
                   reply_markup=kb([("🛡 Открыть «Щит»", "shield"), ("🏠 Меню", "menu")]))


# ───────────────────────── Суд и приказ ─────────────────────────
@router.callback_query(F.data == "court:start")
async def court_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    cs = db.cases(cb.from_user.id)
    intro = ("⚖️ <b>Суд и приказ</b>\n\n<b>Судебный приказ</b> — суд без заседаний за 5–10 дней, должнику даже не нужно "
             "приходить. Лимит: до 500 тыс. ₽ (мировой судья) / до 750 тыс. ₽ (арбитраж, для ИП и юрлиц).\n\n")
    if not cs:
        return await show(cb, intro + "Сначала нужна претензия — она создаст дело.",
                          kb([("📄 Создать претензию", "claim:start")], [("🏠 Меню", "menu")]))
    await show(cb, intro + "Выбери дело:", kb(*[[(f"📁 #{c['id']} · {c['title']}", f"court:case:{c['id']}")] for c in cs[:8]],
                                               [("🏠 Меню", "menu")]))


@router.callback_query(F.data.startswith("court:case:"))
async def court_case(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    c = db.case(int(cb.data.split(":")[2]), cb.from_user.id)
    if not c:
        return
    d = c["data"]
    arbitr = d["my_status"] == "ip" and d["debtor_type"] in ("ip", "ooo")
    amount = float(d["amount"])
    if amount > (750_000 if arbitr else 500_000):
        return await show(cb, "💼 Сумма выше лимита приказного производства — нужен иск. Рекомендую юриста-партнёра.",
                          kb([("🤝 Передать юристу", "partner")], [("🏠 Меню", "menu")]))
    if not d.get("court_done") and db.user(cb.from_user.id)["court_credits"] <= 0:
        return await paywall(cb, "court")
    warn = ""
    if arbitr and time.time() - c["created"] < 30 * 86400:
        warn = "\n\n⚠️ <b>Для арбитража нужно выдержать 30 дней</b> с отправки претензии (ст. 4 АПК) — иначе заявление вернут."
    where = ("<b>арбитражный суд</b> (по адресу должника; подача через «Мой арбитр»)" if arbitr else
             "<b>мировой судья</b> (по адресу должника, если иное не прописано в договоре)")
    await state.set_state(S.court_name)
    await state.update_data(court_case=c["id"], arbitr=arbitr)
    await mof(cb).answer(f"⚖️ Подсудность: {where}.{warn}\n\nНапиши <b>название суда</b> для шапки заявления.\n"
                         f"<i>Узнать: sudrf.ru → «Определение подсудности» (мировые судьи) или kad.arbitr.ru.</i>\n"
                         f"Пример: <i>Мировому судье судебного участка № 5 Вахитовского района г. Казани</i>",
                         reply_markup=kb([("✖️ Отмена", "menu")]))


@router.message(S.court_name, F.text)
async def court_name(m: Message, state: FSMContext):
    data = await state.get_data()
    c = db.case(data["court_case"], m.from_user.id)
    arbitr = data["arbitr"]
    d = c["data"]
    if not d.get("court_done"):
        if not db.use_credit(m.from_user.id, "court"):
            await state.clear()
            return await paywall(m, "court")
    court = m.text.strip()[:200]
    order, fee = docgen.order_doc(d, court, arbitr)
    suit = docgen.lawsuit_doc(d, court, arbitr)
    db.update_case_data(c["id"], court_done=True, court=court)
    db.set_status(c["id"], "приказ подан")
    db.add_reminder(m.from_user.id, c["id"], 10, f"📬 Проверь статус заявления о приказе по делу «{e(d['debtor_name'][:30])}»: "
                                                f"суд обычно выносит его за 5–10 дней.")
    await state.clear()
    await send_doc(m, order, "zayavlenie_o_sudebnom_prikaze.docx", "⚖️ Заявление о выдаче судебного приказа")
    await send_doc(m, suit, "zapasnoy_isk.docx", "🛟 Запасной иск — на случай, если должник отменит приказ")
    where = "«Мой арбитр» (my.arbitr.ru, нужна ЭЦП/Госуслуги)" if arbitr else "ГАС «Правосудие» (через Госуслуги) или почтой/лично в суд"
    await m.answer(
        f"💰 <b>Госпошлина за приказ ≈ {money(fee)}</b> (50% от пошлины иска, взыскивается с должника).\n"
        f"<i>Сверь с актуальной ст. 333.19/333.21 НК РФ.</i>\n\n"
        f"<b>Дальше:</b>\n1. Оплати пошлину (Госуслуги/банк) и приложи квитанцию.\n"
        f"2. Подай через {where}: заявление, расчёт, претензия, чек/опись отправки, доказательства.\n"
        f"3. Приказ ~5–10 дней. Должник вправе возразить в течение 10 дней после получения.\n"
        f"4. Нет возражений → приказ вступает в силу → в банк или ФССП.\n"
        f"5. Отменил → подавай запасной иск (в пакете).",
        reply_markup=kb([("🏦 Приказ вынесен — взыскать", "guide:bank")], [("👮 Через ФССП", "guide:fssp")],
                        [("❌ Должник отменил приказ", "guide:cancel")], [("🏠 Меню", "menu")]))


@router.callback_query(F.data == "guide:cancel")
async def guide_cancel(cb: CallbackQuery):
    await cb.answer()
    await show(cb, "❌ <b>Приказ отменён — это не конец</b>\n\nВозражения должника просто переводят дело в исковое "
                   "производство. Подай <b>запасной иск</b> (файл уже у тебя) в тот же суд, приложив определение об отмене "
                   "и доплатив пошлину (уплаченная за приказ зачитывается). Если спор о качестве работы — лучше юрист-партнёр.",
               kb([("🤝 Юрист-партнёр", "partner")], [("🏠 Меню", "menu")]))


# ───────────────────────── Доказательства ─────────────────────────
@router.callback_query(F.data == "evid")
async def evid(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await show(cb, "🧾 <b>Доказательства: «Переписка = деньги»</b>\n\nГлавный вопрос суда — «чем докажешь?». Выбери раздел:",
               kb([("📸 Протокол фиксации", "ev:protocol"), ("💬 Спасающее сообщение", "ev:saver")],
                  [("🧾 Чек «Мой налог»", "ev:npd"), ("🔍 Что принимает суд", "ev:kinds")],
                  [("🗓 Собрать акт-хронологию", "ev:chrono")], [("🏠 Меню", "menu")]))


@router.callback_query(F.data.in_({"ev:protocol", "ev:saver", "ev:npd", "ev:kinds"}))
async def ev_texts(cb: CallbackQuery):
    await cb.answer()
    t = {"ev:protocol": T.EVID_PROTOCOL, "ev:saver": T.EVID_SAVER, "ev:npd": T.EVID_NPD, "ev:kinds": T.EVID_KINDS}[cb.data]
    await show(cb, t, kb([("⬅️ К доказательствам", "evid"), ("🏠 Меню", "menu")]))


@router.callback_query(F.data == "ev:chrono")
async def ev_chrono(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(S.chrono)
    await state.update_data(events=[])
    await show(cb, "🗓 <b>Акт-хронология</b>\n\nПришли события по одному (или списком, по строке на событие) в формате:\n"
                   "<code>12.03.2026 | согласовали ТЗ и цену 35 000 ₽</code>\n<code>20.03.2026 | передал готовый ролик</code>\n"
                   "<code>25.03.2026 | заказчик пообещал оплатить</code>\n\nКогда всё добавлено — жми «Собрать».",
               kb([("📄 Собрать документ", "ev:chrono:done")], [("✖️ Отмена", "evid")]))


@router.message(S.chrono, F.text)
async def chrono_add(m: Message, state: FSMContext):
    data = await state.get_data()
    ev_ = data.get("events", [])
    for line in m.text.splitlines():
        if "|" in line:
            ev_.append(line.strip())
    await state.update_data(events=ev_)
    await m.answer(f"➕ Добавлено. Всего событий: <b>{len(ev_)}</b>" if ev_ else "Формат: <code>ДД.ММ.ГГГГ | событие</code>",
                   reply_markup=kb([("📄 Собрать документ", "ev:chrono:done")]))


@router.callback_query(F.data == "ev:chrono:done")
async def chrono_done(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    evs = data.get("events", [])
    if not evs:
        return await cb.message.answer("Сначала добавь хотя бы одно событие.")

    def k(x):
        try:
            return datetime.strptime(x.split("|")[0].strip(), "%d.%m.%Y")
        except ValueError:
            return datetime.max
    doc = docgen.chronology_doc("Хронология взаимоотношений сторон", sorted(evs, key=k), cb.from_user.full_name)
    await state.clear()
    await send_doc(cb, doc, "akt_khronologiya.docx", "🗓 Акт-хронология — приложи к заявлению в суд")
    await mof(cb).answer("Готово! Рядом с каждым событием укажи номер скриншота.", reply_markup=kb([("🏠 Меню", "menu")]))


# ───────────────────────── Щит ─────────────────────────
def shield_active(uid) -> bool:
    return db.user(uid)["shield_until"] > time.time()


@router.callback_query(F.data == "shield")
async def shield(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    ok = shield_active(cb.from_user.id)
    head = T.SHIELD_INFO + ("\n\n✅ <b>Подписка активна</b>" if ok else "\n\n🔒 Подписка не активна")
    rows = [[("📝 Конструктор договора", "sh:contract")], [("🧾 Акт сдачи-приёмки", "sh:act")],
            [("🔎 Проверить заказчика", "sh:check")]]
    if not ok:
        rows.append([("💳 299 ₽/мес", "buy:s30"), ("💳 2 490 ₽/год", "buy:s365")])
    rows.append([("🏠 Меню", "menu")])
    await show(cb, head, kb(*rows))


@router.callback_query(F.data == "sh:contract")
async def sh_contract(cb: CallbackQuery):
    await cb.answer()
    if not shield_active(cb.from_user.id):
        return await paywall(cb, "shield")
    ks = list(docgen.CONTRACT_TYPES)
    rows = [[(docgen.CONTRACT_TYPES[k][0], f"ct:{k}")] for k in ks]
    await show(cb, "📝 <b>Конструктор договора</b>\nВыбери тип 👇", kb(*rows, [("⬅️ Назад", "shield")]))


@router.callback_query(F.data.startswith("ct:"))
async def ct_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await w_start(cb, state, "contract", {"kind": cb.data.split(":")[1]})


async def finish_contract(ev, state, w, extra):
    doc = docgen.contract_doc(extra["kind"], w)
    await state.clear()
    await send_doc(ev, doc, "dogovor.docx", "📝 Договор готов. Защита вшита: предоплата, неустойка, удержание результата, "
                                          "переписка = доказательство.")
    await mof(ev).answer("⚠️ Перед подписанием перечитай и при необходимости покажи юристу.\n" + T.DISCLAIMER,
                         reply_markup=kb([("🧾 Сделать акт", "sh:act"), ("🏠 Меню", "menu")]))


@router.callback_query(F.data == "sh:act")
async def sh_act(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if not shield_active(cb.from_user.id):
        return await paywall(cb, "shield")
    await w_start(cb, state, "act")


async def finish_act(ev, state, w, extra):
    doc = docgen.act_doc(w)
    await state.clear()
    await send_doc(ev, doc, "akt.docx", "🧾 Акт сдачи-приёмки. Подпиши у заказчика или сделай фото с подтверждением в чате.")
    await mof(ev).answer("Готово ✅", reply_markup=kb([("🏠 Меню", "menu")]))


@router.callback_query(F.data == "sh:check")
async def sh_check(cb: CallbackQuery):
    await cb.answer()
    await show(cb, T.CUSTOMER_CHECK,
               kb([("🏛 ЕГРЮЛ", "https://egrul.nalog.ru"), ("⚖️ Арбитраж", "https://kad.arbitr.ru")],
                  [("📉 ЕФРСБ", "https://bankrot.fedresurs.ru"), ("👮 ФССП", "https://fssp.gov.ru/iss/ip")],
                  [("⬅️ Назад", "shield")]))


FINISH = {"claim": finish_claim, "contract": finish_contract, "act": finish_act}


# ───────────────────────── Юрист-партнёр ─────────────────────────
@router.callback_query(F.data == "partner")
async def partner(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await show(cb, T.PARTNER, kb([("📨 Передать дело", "partner:go")], [("🏠 Меню", "menu")]))


@router.callback_query(F.data == "partner:go")
async def partner_go(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(S.partner)
    await show(cb, "Опиши дело одним сообщением: кто должник, сумма, что есть из документов, город.",
               kb([("✖️ Отмена", "menu")]))


@router.message(S.partner, F.text)
async def partner_msg(m: Message, state: FSMContext, bot: Bot):
    await notify_admin(bot, f"🤝 Лид партнёру от {m.from_user.full_name} @{m.from_user.username} id={m.from_user.id}:\n\n{m.text}")
    await state.clear()
    await m.answer("✅ Принято! Юрист свяжется с тобой в Telegram. Комиссия — только с взысканного.",
                   reply_markup=kb([("🏠 Меню", "menu")]))


# ───────────────────────── Прочее ─────────────────────────
@router.message(StateFilter(None))
async def fallback(m: Message):
    await m.answer("Выбери действие в меню 👇", reply_markup=MENU_KB)


async def reminder_loop(bot: Bot):
    while True:
        try:
            for r in db.due_reminders():
                db.mark_sent(r["id"])
                try:
                    await bot.send_message(r["user_id"], r["text"], reply_markup=kb(
                        [("💰 Заказчик заплатил", f"case:paid:{r['case_id']}")],
                        [("⚖️ Подать приказ", f"court:case:{r['case_id']}"), ("📂 Дело", f"case:{r['case_id']}")]))
                except Exception:
                    logging.exception("reminder send failed")
        except Exception:
            logging.exception("reminder loop")
        await asyncio.sleep(60)


async def main():
    logging.basicConfig(level=logging.INFO)
    if not TOKEN:
        raise SystemExit("Укажи BOT_TOKEN в файле .env")
    db.init()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    from aiogram.types import BotCommand
    await bot.set_my_commands([BotCommand(command="start", description="Запустить бота"),
                               BotCommand(command="menu", description="Главное меню")])
    asyncio.create_task(reminder_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
