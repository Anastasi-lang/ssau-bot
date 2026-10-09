"""Бот расписания ssau.ru для Telegram — одноразовый запуск (GitHub Actions).

Каждый запуск: обрабатывает команды -> вечерняя рассылка -> напоминания -> проверка изменений.
Состояние хранится в state.json (его коммитит workflow).
Проверка без Telegram:  python bot.py --test
"""
import html
import json
import os
import re
import sys
import traceback
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

TOKEN = os.getenv("BOT_TOKEN", "")
GROUP_ID = os.getenv("GROUP_ID", "762608654")
TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Samara"))
DIGEST_TIME = os.getenv("DIGEST_TIME", "20:00")
REMIND_MIN = int(os.getenv("REMIND_MIN", "20"))
YEAR_START_FALLBACK = os.getenv("YEAR_START", "2026-08-31")
STATE_FILE = Path("state.json")
PARSER_VERSION = 2  # при изменении парсера старый снимок расписания сбрасывается
BASE_URL = "https://ssau.ru/rasp"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ssau-schedule-bot/1.0)"}
DAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def log(*a):
    print(*a, flush=True)


# ---------- парсер ----------
@dataclass
class Lesson:
    day: date
    start: str
    end: str
    kind: str
    title: str
    place: str
    teacher: str


def _text(el) -> str:
    return " ".join(el.get_text(" ", strip=True).split()) if el else ""


def parse_week(page: str) -> list[Lesson]:
    soup = BeautifulSoup(page, "html.parser")
    box = soup.select_one(".schedule__items")
    if not box:
        return []
    days, lessons, times, col = [], [], None, 0
    for child in box.find_all(recursive=False):
        cls = child.get("class", [])
        if "schedule__head" in cls:
            m = re.search(r"\d{2}\.\d{2}\.\d{4}", child.get_text())
            if m:  # пустая угловая ячейка без даты днём не считается
                days.append(datetime.strptime(m.group(), "%d.%m.%Y").date())
        elif "schedule__time" in cls:
            t = [_text(x) for x in child.select(".schedule__time-item")]
            times = (t[0], t[1]) if len(t) >= 2 else None
            col = 0
        elif "schedule__item" in cls and times:
            if col < len(days) and days[col]:
                for les in child.select(".schedule__lesson"):
                    lessons.append(Lesson(
                        day=days[col], start=times[0], end=times[1],
                        kind=_text(les.select_one(".schedule__lesson-type")),
                        title=_text(les.select_one(".schedule__discipline")),
                        place=_text(les.select_one(".schedule__place")),
                        teacher=_text(les.select_one(".schedule__teacher")),
                    ))
            col += 1
    return lessons


# ---------- загрузка расписания ----------
_http = httpx.Client(headers=HEADERS, timeout=25, follow_redirects=True)
_weeks: dict[int, list[Lesson]] = {}
_year_start: date | None = None


def _get(**params) -> str:
    r = _http.get(BASE_URL, params={"groupId": GROUP_ID, **params})
    r.raise_for_status()
    return r.text


def year_start() -> date:
    global _year_start
    if _year_start:
        return _year_start
    try:
        m = re.search(r"Начало учебного года:\s*(\d{2}\.\d{2}\.\d{4})", _get())
        if m:
            _year_start = datetime.strptime(m.group(1), "%d.%m.%Y").date()
            return _year_start
    except Exception as e:
        log("Не удалось получить начало учебного года:", e)
    return date.fromisoformat(YEAR_START_FALLBACK)


def week_number(d: date) -> int:
    return (d - year_start()).days // 7 + 1


def get_week(week: int) -> list[Lesson]:
    if week not in _weeks:
        _weeks[week] = parse_week(_get(selectedWeek=week)) if week >= 1 else []
    return _weeks[week]


def get_day(d: date) -> list[Lesson]:
    return sorted((l for l in get_week(week_number(d)) if l.day == d), key=lambda l: l.start)


# ---------- форматирование ----------
def fmt_day(d: date, lessons: list[Lesson]) -> str:
    head = f"📅 <b>{DAYS_RU[d.weekday()].capitalize()}, {d:%d.%m.%Y}</b>"
    if not lessons:
        return head + "\n\nПар нет 🎉"
    out, e = [head], html.escape
    for l in lessons:
        line = f"\n🕗 <b>{l.start}–{l.end}</b>  <i>{e(l.kind)}</i>\n{e(l.title)}"
        if l.place:
            line += f"\n📍 {e(l.place)}"
        if l.teacher:
            line += f"\n👤 {e(l.teacher)}"
        out.append(line)
    return "\n".join(out)


def fmt_reminder(l: Lesson, mins: int) -> str:
    e = html.escape
    txt = f"⏰ <b>Через {mins} мин пара</b>\n{l.start}–{l.end} · {e(l.kind)}\n<b>{e(l.title)}</b>"
    if l.place:
        txt += f"\n📍 {e(l.place)}"
    if l.teacher:
        txt += f"\n👤 {e(l.teacher)}"
    return txt


def _key(l: Lesson) -> str:
    return f"{l.day:%Y-%m-%d}|{l.start}|{l.kind}|{l.title}|{l.place}|{l.teacher}"


def _fmt_key(k: str) -> str:
    d, start, _kind, title, place, _t = k.split("|")
    dd = datetime.strptime(d, "%Y-%m-%d")
    return f"{dd:%d.%m} {start} — {html.escape(title)} ({html.escape(place)})"


# ---------- Telegram ----------
def tg(method: str, **params) -> dict:
    r = httpx.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=params, timeout=30)
    return r.json()


def send(chat_id: int, text: str) -> dict:
    return tg("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML")


def broadcast(st: dict, text: str) -> None:
    for cid in list(st["subs"]):
        res = send(cid, text)
        if not res.get("ok") and res.get("error_code") == 403:  # пользователь заблокировал бота
            st["subs"].remove(cid)


# ---------- состояние ----------
def load_state() -> dict:
    try:
        st = json.loads(STATE_FILE.read_text())
    except Exception:
        st = {}
    st.setdefault("offset", 0)
    st.setdefault("subs", [])
    st.setdefault("sent", [])
    st.setdefault("digest_date", "")
    st.setdefault("snapshot", None)
    return st


def save_state(st: dict) -> None:
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1, sort_keys=True))


# ---------- шаги ----------
HELP = (
    "Привет! Я присылаю расписание группы 5104-450302D.\n\n"
    f"🔔 Вечером около {DIGEST_TIME} — расписание на завтра\n"
    f"⏰ Примерно за {REMIND_MIN} мин до пары — напоминание\n"
    "🔄 Если расписание на сайте изменится — сообщу\n\n"
    "/today — сегодня\n/tomorrow — завтра\n/week — неделя\n/stop — отписаться\n\n"
    "Отвечаю с задержкой до 10 минут."
)


def reply_day(chat: int, d: date) -> None:
    try:
        send(chat, fmt_day(d, get_day(d)))
    except Exception:
        log(traceback.format_exc())
        send(chat, "Не удалось получить расписание с ssau.ru, попробуйте позже.")


def handle_updates(st: dict) -> None:
    res = tg("getUpdates", offset=st["offset"], timeout=0, allowed_updates=["message"])
    if not res.get("ok"):
        log("getUpdates error:", res)
        return
    today = datetime.now(TZ).date()
    for u in res.get("result", []):
        st["offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat = (msg.get("chat") or {}).get("id")
        if not chat or not text.startswith("/"):
            continue
        cmd = text.split()[0].split("@")[0].lower()
        if cmd == "/start":
            if chat not in st["subs"]:
                st["subs"].append(chat)
            send(chat, HELP)
        elif cmd == "/stop":
            if chat in st["subs"]:
                st["subs"].remove(chat)
            send(chat, "Уведомления отключены. Вернуть: /start")
        elif cmd in ("/today", "/update"):
            reply_day(chat, today)
        elif cmd == "/tomorrow":
            reply_day(chat, today + timedelta(days=1))
        elif cmd == "/week":
            monday = today - timedelta(days=today.weekday())
            for i in range(6):
                reply_day(chat, monday + timedelta(days=i))


def step_digest(st: dict, now: datetime) -> None:
    hh, mm = map(int, DIGEST_TIME.split(":"))
    if now.time() >= dtime(hh, mm) and st["digest_date"] != str(now.date()):
        d = now.date() + timedelta(days=1)
        broadcast(st, "🔔 <b>Расписание на завтра</b>\n\n" + fmt_day(d, get_day(d)))
        st["digest_date"] = str(now.date())


def step_remind(st: dict, now: datetime) -> None:
    sent = set(st["sent"])
    for l in get_day(now.date()):
        h, m = map(int, l.start.split(":"))
        mins = (datetime.combine(l.day, dtime(h, m), TZ) - now).total_seconds() / 60
        key = f"{l.day}|{l.start}|{l.title}"
        if 0 < mins <= REMIND_MIN and key not in sent:
            broadcast(st, fmt_reminder(l, max(1, round(mins))))
            sent.add(key)
    st["sent"] = sorted(k for k in sent if k.startswith(str(now.date())))


def step_changes(st: dict, now: datetime) -> None:
    if st.get("parser_version") != PARSER_VERSION:
        old = None  # парсер обновился: пересоздаём снимок без уведомлений
        st["parser_version"] = PARSER_VERSION
    else:
        old = st["snapshot"]
    cur = week_number(now.date())
    new = {}
    for w in (cur, cur + 1):
        if w < 1:
            continue
        ls = get_week(w)
        if not ls and old and old.get(str(w)):
            new[str(w)] = old[str(w)]  # сбой разбора страницы — не считаем изменением
        else:
            new[str(w)] = sorted(_key(l) for l in ls)
    st["snapshot"] = new
    if old is None:
        return
    iso, lines = str(now.date()), []
    for w, keys in new.items():
        if w not in old:
            continue
        o, n = set(old[w]), set(keys)
        lines += [f"➖ {_fmt_key(k)}" for k in sorted(o - n) if k[:10] >= iso]
        lines += [f"➕ {_fmt_key(k)}" for k in sorted(n - o) if k[:10] >= iso]
    if lines:
        broadcast(st, "🔄 <b>Расписание изменилось</b>\n\n" + "\n".join(lines))


def main() -> None:
    if not TOKEN:
        sys.exit("Не задан BOT_TOKEN")
    st = load_state()
    now = datetime.now(TZ)
    for name, fn in [("команды", lambda: handle_updates(st)),
                     ("рассылка", lambda: step_digest(st, now)),
                     ("напоминания", lambda: step_remind(st, now)),
                     ("изменения", lambda: step_changes(st, now))]:
        try:
            fn()
        except Exception:
            log(f"ERROR в шаге «{name}»:\n{traceback.format_exc()}")
    save_state(st)
    log("Готово. Подписчиков:", len(st["subs"]))


def selftest() -> None:
    today = datetime.now(TZ).date()
    for d in (today, today + timedelta(days=1)):
        ls = get_day(d)
        print(f"{d}: найдено пар — {len(ls)}")
        for l in ls:
            print("  ", l.start, l.kind, "|", l.title, "|", l.place, "|", l.teacher)


if __name__ == "__main__":
    selftest() if "--test" in sys.argv else main()
