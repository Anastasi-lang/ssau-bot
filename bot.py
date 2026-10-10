"""Бот расписания ssau.ru для Telegram — одноразовый запуск (GitHub Actions).

Каждый запуск: обрабатывает команды -> вечерняя рассылка -> напоминания -> проверка изменений.
Состояние хранится в state.json (его коммитит workflow).
Проверка без Telegram:  python bot.py --test
"""
import html
import io
import json
import math
import os
import random
import re
import sys
import traceback
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont

TOKEN = os.getenv("BOT_TOKEN", "")
GROUP_ID = os.getenv("GROUP_ID", "762608654")
TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Samara"))
DIGEST_TIME = os.getenv("DIGEST_TIME", "20:00")
REMIND_MIN = int(os.getenv("REMIND_MIN", "20"))
YEAR_START_FALLBACK = os.getenv("YEAR_START", "2026-08-31")
STATE_FILE = Path("state.json")
USE_IMAGES = os.getenv("USE_IMAGES", "1") == "1"  # 1 — картинки, 0 — обычный текст
GROUP_NAME = "5104-450302D"
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



# ---------- картинка с расписанием ----------
HERE = Path(__file__).resolve().parent
_FONT_DIRS = [HERE, Path("/usr/share/fonts/truetype/dejavu")]
_fonts: dict = {}
CITY = {"lat": 53.2, "lon": 50.15}  # Самара — для прогноза погоды


def font(size: int, bold: bool = False):
    key = (size, bold)
    if key not in _fonts:
        name = "DejaVuSansCondensed-Bold.ttf" if bold else "DejaVuSansCondensed.ttf"
        for d in _FONT_DIRS:
            if (d / name).exists():
                _fonts[key] = ImageFont.truetype(str(d / name), size)
                break
        else:
            raise FileNotFoundError(f"Не найден шрифт {name}")
    return _fonts[key]


def hx(c: str) -> tuple:
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


# --- темы оформления ---
BASE = dict(top="#EEF1F7", bottom="#EEF1F7", head="#1E2A4A", head_sub="#AEB9D6", accent="#FFD166",
            card="#FFFFFF", ink="#111827", gray="#6B7280", foot="#9CA3AF", border=None,
            pattern=None, pc="#FFFFFF", strip=None)


def T(**kw) -> dict:
    return {**BASE, **kw}


# стили, которые можно выбрать вручную (/style)
THEMES = {
    "classic": T(name="Классика"),
    "sunset": T(name="Закат", top="#FFE8D6", bottom="#FFC2D4", head="#7A2E5C", head_sub="#F6C3DD",
                accent="#FFE0A8", card="#FFFAF7", pattern="bokeh", pc="#FFFFFF"),
    "mint": T(name="Мята", top="#E3F7EF", bottom="#C9EEE0", head="#0F5C4D", head_sub="#A9E4D2",
              accent="#C9F7E6", card="#FFFFFF", pattern="bokeh", pc="#FFFFFF"),
    "night": T(name="Ночь", top="#0F172A", bottom="#1E1B4B", head="#312E81", head_sub="#A5B4FC",
               accent="#FDE68A", card="#1E2540", ink="#F1F5F9", gray="#94A3B8", foot="#64748B",
               pattern="stars", pc="#FFFFFF"),
    "sakura": T(name="Сакура", top="#FFF0F5", bottom="#FFD9E6", head="#B83B6B", head_sub="#FFD1E1",
                accent="#FFE3EC", pattern="petals", pc="#FF9EBB"),
    "minimal": T(name="Минимал", top="#FFFFFF", bottom="#F3F4F6", head="#111827", head_sub="#9CA3AF",
                 border="#E5E7EB"),
}

# автоматические темы под погоду
WEATHER_THEMES = {
    "sun": T(top="#D9EEFF", bottom="#FFF3CF", head="#1F6FB5", head_sub="#BFE0FA", accent="#FFE08A",
             pattern="bokeh", pc="#FFFFFF"),
    "cloud": T(top="#E6EAF1", bottom="#CFD7E4", head="#4B5B77", head_sub="#C3CCDD", accent="#FFE08A",
               pattern="bokeh", pc="#FFFFFF"),
    "rain": T(top="#DCE6F0", bottom="#BCCDE0", head="#2F4B6B", head_sub="#B5C9DF", accent="#FFE08A",
              pattern="rain", pc="#6F93BA"),
    "snow": T(top="#EAF3FB", bottom="#F8FBFF", head="#3A5A87", head_sub="#C2D6EE", accent="#FFE08A",
              pattern="snow", pc="#FFFFFF"),
    "storm": T(top="#111827", bottom="#312E4A", head="#3B3B66", head_sub="#B9B9E0", accent="#FACC15",
               card="#1F2740", ink="#F1F5F9", gray="#9AA7BD", foot="#6B7788", pattern="rain", pc="#7F8FB8"),
}

# праздничные темы
HOLIDAY_THEMES = {
    "newyear": T(top="#0B2447", bottom="#1C3F78", head="#B3263E", head_sub="#FFD9DF", accent="#FFFFFF",
                 foot="#9DB4D8", pattern="snow", pc="#FFFFFF"),
    "students": T(top="#EDE9FE", bottom="#DDD6FE", head="#5B21B6", head_sub="#DDD0FF", accent="#FDE68A",
                  pattern="confetti"),
    "defender": T(top="#E8EFE2", bottom="#CFDCC2", head="#3F5A3A", head_sub="#C9DBC2", accent="#FFE08A",
                  pattern="stars", pc="#C9A227"),
    "womens": T(top="#FFF1F2", bottom="#FFD3E0", head="#BE185D", head_sub="#FFD3E6", accent="#FFF1A8",
                pattern="petals", pc="#FF8FB1"),
    "space": T(top="#050816", bottom="#1B1464", head="#2B2A6E", head_sub="#B4B3F0", accent="#FFD166",
               card="#141B3A", ink="#F1F5F9", gray="#9AA7BD", foot="#6B7788", pattern="stars", pc="#FFFFFF"),
    "victory": T(top="#F5F5F4", bottom="#E0DEDB", head="#1C1917", head_sub="#D6D3D1", accent="#FB923C",
                 strip="ribbon"),
    "russia": T(top="#F8FAFC", bottom="#E2E8F0", head="#1D4ED8", head_sub="#C7D6FB", accent="#FFFFFF",
                strip="tricolor"),
    "knowledge": T(top="#FFF7CC", bottom="#CFE6FF", head="#1E40AF", head_sub="#C7D7FB", accent="#FDE047",
                   pattern="confetti"),
}
for _group, _items in (("s", THEMES), ("w", WEATHER_THEMES), ("h", HOLIDAY_THEMES)):
    for _k, _v in _items.items():
        _v["id"] = f"{_group}:{_k}"

STYLE_LABELS = {"auto": "🎲 Авто (погода и праздники)", **{k: v["name"] for k, v in THEMES.items()}}


def holiday_for(d: date):
    """Возвращает (ключ темы, поздравление) или None."""
    md = (d.month, d.day)
    if md >= (12, 25):
        return "newyear", "С наступающим Новым годом!"
    if md <= (1, 8):
        return "newyear", "С Новым годом!"
    fixed = {(1, 25): ("students", "С Днём студента!"), (2, 23): ("defender", "С 23 февраля!"),
             (3, 8): ("womens", "С 8 марта!"), (4, 12): ("space", "С Днём космонавтики!"),
             (5, 9): ("victory", "С Днём Победы!"), (6, 12): ("russia", "С Днём России!"),
             (9, 1): ("knowledge", "С Днём знаний!"), (11, 4): ("russia", "С Днём народного единства!")}
    return fixed.get(md)


# --- погода (Open-Meteo, без ключа) ---
_weather: dict | None = None


def weather_kind(code: int) -> str:
    if code in (0, 1):
        return "sun"
    if code == 2:
        return "partly"
    if code >= 95:
        return "storm"
    if 71 <= code <= 77 or code in (85, 86):
        return "snow"
    if 51 <= code <= 67 or 80 <= code <= 82:
        return "rain"
    return "cloud"


def weather_for(d: date):
    """(вид погоды, t_max, t_min) на дату или None."""
    global _weather
    if _weather is None:
        _weather = {}
        try:
            r = _http.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": CITY["lat"], "longitude": CITY["lon"], "past_days": 7, "forecast_days": 8,
                "daily": "weather_code,temperature_2m_max,temperature_2m_min",
                "timezone": os.getenv("TZ_NAME", "Europe/Samara")})
            r.raise_for_status()
            day = r.json()["daily"]
            for t, c, hi, lo in zip(day["time"], day["weather_code"],
                                    day["temperature_2m_max"], day["temperature_2m_min"]):
                if c is not None and hi is not None and lo is not None:
                    _weather[t] = (weather_kind(int(c)), hi, lo)
        except Exception as e:
            log("Погоду получить не удалось:", e)
    return _weather.get(d.isoformat())


def pick_theme(style: str, d: date):
    """Тема и поздравление для даты с учётом выбранного стиля."""
    hol = holiday_for(d)
    greet = hol[1] if hol else None
    if style in THEMES:
        return THEMES[style], greet
    if hol:
        return HOLIDAY_THEMES[hol[0]], greet
    w = weather_for(d)
    if w:
        return WEATHER_THEMES["sun" if w[0] == "partly" else w[0]], None
    return THEMES["classic"], None


# --- рисование ---
def gradient(w: int, h: int, c1: str, c2: str) -> Image.Image:
    img = Image.new("RGBA", (w, h))
    d, a, b = ImageDraw.Draw(img), hx(c1), hx(c2)
    for y in range(h):
        t = y / max(h - 1, 1)
        d.line((0, y, w, y), fill=tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3)) + (255,))
    return img


def draw_pattern(img: Image.Image, kind: str | None, pc: str, rng: random.Random) -> None:
    if not kind:
        return
    w, h = img.size
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    r_, g_, b_ = hx(pc)
    if kind == "snow":
        for _ in range(h // 11):
            x, y, r = rng.randrange(w), rng.randrange(h), rng.choice((3, 4, 5, 7))
            d.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255, rng.randrange(120, 230)))
    elif kind == "rain":
        for _ in range(h // 9):
            x, y = rng.randrange(w + 40), rng.randrange(h)
            d.line((x, y, x - 12, y + 38), fill=(r_, g_, b_, rng.randrange(70, 150)), width=3)
    elif kind == "stars":
        for _ in range(h // 13):
            x, y, r = rng.randrange(w), rng.randrange(h), rng.choice((3, 4, 6, 9, 12))
            s = r * 0.28
            pts = [(x, y - r), (x + s, y - s), (x + r, y), (x + s, y + s),
                   (x, y + r), (x - s, y + s), (x - r, y), (x - s, y - s)]
            d.polygon(pts, fill=(r_, g_, b_, rng.randrange(110, 230)))
    elif kind == "petals":
        for _ in range(h // 28):
            x, y = rng.randrange(w), rng.randrange(h)
            a, b = rng.choice((16, 20, 26)), rng.choice((9, 12, 14))
            d.ellipse((x - a, y - b, x + a, y + b), fill=(r_, g_, b_, rng.randrange(80, 170)))
    elif kind == "confetti":
        cols = ["#F43F5E", "#F59E0B", "#10B981", "#3B82F6", "#8B5CF6", "#EC4899"]
        for _ in range(h // 16):
            x, y, s = rng.randrange(w), rng.randrange(h), rng.choice((7, 10, 13))
            c = hx(rng.choice(cols)) + (rng.randrange(120, 220),)
            if rng.random() < 0.5:
                d.ellipse((x, y, x + s, y + s), fill=c)
            else:
                d.rectangle((x, y, x + s, y + s // 2 + 2), fill=c)
    elif kind == "bokeh":
        for _ in range(h // 60):
            x, y, r = rng.randrange(w), rng.randrange(h), rng.choice((30, 50, 80, 110))
            d.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255, rng.randrange(25, 60)))
    img.alpha_composite(ov)


def draw_cloud(d, cx, cy, s, fill):
    d.ellipse((cx - .55 * s, cy - .02 * s, cx - .05 * s, cy + .38 * s), fill=fill)
    d.ellipse((cx - .33 * s, cy - .36 * s, cx + .22 * s, cy + .30 * s), fill=fill)
    d.ellipse((cx - .02 * s, cy - .14 * s, cx + .52 * s, cy + .38 * s), fill=fill)
    d.rounded_rectangle((cx - .42 * s, cy + .08 * s, cx + .40 * s, cy + .38 * s), int(.15 * s), fill=fill)


def draw_sun(d, cx, cy, s, fill="#FFC933"):
    r = .26 * s
    for i in range(8):
        a = i * math.pi / 4
        d.line((cx + math.cos(a) * r * 1.4, cy + math.sin(a) * r * 1.4,
                cx + math.cos(a) * r * 1.9, cy + math.sin(a) * r * 1.9), fill=fill, width=max(4, int(s * .06)))
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=fill)


def draw_weather_icon(d, kind, cx, cy, s):
    if kind == "sun":
        draw_sun(d, cx, cy, s)
    elif kind == "partly":
        draw_sun(d, cx + .22 * s, cy - .2 * s, s * .8)
        draw_cloud(d, cx - .05 * s, cy + .1 * s, s * .9, "#F1F5F9")
    elif kind == "cloud":
        draw_cloud(d, cx, cy, s, "#E2E8F0")
    elif kind == "rain":
        draw_cloud(d, cx, cy - .14 * s, s, "#CBD5E1")
        for i in (-.3, 0, .3):
            d.line((cx + i * s, cy + .30 * s, cx + i * s - .08 * s, cy + .52 * s), fill="#60A5FA", width=5)
    elif kind == "snow":
        draw_cloud(d, cx, cy - .14 * s, s, "#E2E8F0")
        for i in (-.3, 0, .3):
            r = .05 * s
            d.ellipse((cx + i * s - r, cy + .44 * s - r, cx + i * s + r, cy + .44 * s + r), fill="#FFFFFF")
    elif kind == "storm":
        draw_cloud(d, cx, cy - .16 * s, s, "#94A3B8")
        pts = [(.02, .18), (-.16, .5), (-.02, .5), (-.1, .68), (.18, .4), (.04, .4)]
        d.polygon([(cx + x * s, cy + y * s) for x, y in pts], fill="#FACC15")


def kind_color(kind: str) -> str:
    return {"лекция": "#3B82F6", "практика": "#10B981", "лабораторная": "#F59E0B",
            "экзамен": "#EF4444", "зачёт": "#8B5CF6", "зачет": "#8B5CF6",
            "консультация": "#14B8A6"}.get(kind.strip().lower(), "#64748B")


def wrap(text: str, fnt, max_w: int, max_lines: int = 3) -> list[str]:
    lines, cur = [], ""
    for w in text.split():
        trial = f"{cur} {w}".strip()
        if fnt.getlength(trial) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(" .,") + "…"
    return lines or [""]


def pretty_place(place: str) -> str:
    m = re.match(r"^(.+?)\s*-\s*(\d+)$", place.strip())
    return f"ауд. {m.group(1)}, корп. {m.group(2)}" if m else place


def render_day_image(d: date, lessons: list[Lesson], theme: dict | None = None,
                     greeting: str | None = None) -> bytes:
    th = theme or THEMES["classic"]
    W, M = 1080, 40
    f_day, f_sub = font(54, True), font(30)
    f_time, f_time2 = font(44, True), font(28)
    f_kind, f_title, f_info = font(26, True), font(36, True), font(29)
    text_x = M + 40 + 190
    text_w = W - M - 30 - text_x
    w = weather_for(d)

    cards = []
    for l in lessons:
        title = wrap(l.title, f_title, text_w, 3)
        info = ([pretty_place(l.place)] if l.place else []) + ([l.teacher] if l.teacher else [])
        h = 28 + 34 + 8 + len(title) * 46 + (10 + len(info) * 40 if info else 0) + 28
        cards.append((l, title, info, max(h, 150)))

    head_h = 170 + (44 if greeting else 0)
    strip_h = 26 if th["strip"] else 0
    body_h = sum(c[3] + 24 for c in cards) if cards else 184
    H = M + head_h + 28 + strip_h + body_h + 70

    img = gradient(W, H, th["top"], th["bottom"])
    draw_pattern(img, th["pattern"], th["pc"], random.Random(d.toordinal()))
    dr = ImageDraw.Draw(img)

    # шапка
    dr.rounded_rectangle((M, M, W - M, M + head_h), 36, fill=th["head"])
    dr.text((M + 40, M + 26), DAYS_RU[d.weekday()].capitalize(), font=f_day, fill="#FFFFFF")
    dr.text((M + 40, M + 98), f"{d:%d.%m.%Y}  ·  {GROUP_NAME}", font=f_sub, fill=th["head_sub"])
    if greeting:
        dr.text((M + 40, M + 150), greeting, font=font(32, True), fill=th["accent"])
    if w:
        kind, hi, lo = w
        draw_weather_icon(dr, kind, W - M - 118, M + 54, 88)
        t = f"{round(hi):+d}° / {round(lo):+d}°"
        dr.text((W - M - 40 - font(34, True).getlength(t), M + 118), t, font=font(34, True), fill="#FFFFFF")

    y = M + head_h + 28
    if th["strip"] == "ribbon":
        for i, x in enumerate(range(M, W - M, 26)):
            dr.rectangle((x, y - 6, min(x + 26, W - M), y + 10), fill="#F97316" if i % 2 == 0 else "#111827")
        y += strip_h - 4
    elif th["strip"] == "tricolor":
        for i, c in enumerate(("#FFFFFF", "#1D4ED8", "#DC2626")):
            dr.rectangle((M, y - 6 + i * 9, W - M, y + 2 + i * 9), fill=c, outline="#CBD5E1" if i == 0 else None)
        y += strip_h - 4

    # карточки
    if not cards:
        dr.rounded_rectangle((M, y, W - M, y + 160), 28, fill=th["card"], outline=th["border"], width=2)
        dr.text((M + 40, y + 48), "Пар нет — можно отдыхать", font=font(40, True), fill=th["ink"])
    for l, title, info, h in cards:
        col = kind_color(l.kind)
        dr.rounded_rectangle((M, y, W - M, y + h), 28, fill=th["card"], outline=th["border"], width=2)
        dr.rounded_rectangle((M, y, M + 16, y + h), 8, fill=col)
        dr.text((M + 44, y + 26), l.start, font=f_time, fill=th["ink"])
        dr.text((M + 44, y + 80), l.end, font=f_time2, fill=th["gray"])
        ty = y + 26
        dr.text((text_x, ty), (l.kind or "Занятие").upper(), font=f_kind, fill=col)
        ty += 42
        for line in title:
            dr.text((text_x, ty), line, font=f_title, fill=th["ink"])
            ty += 46
        ty += 6
        for line in info:
            dr.text((text_x, ty), line, font=f_info, fill=th["gray"])
            ty += 40
        y += h + 24

    foot = f"обновлено {datetime.now(TZ):%H:%M}"
    dr.text((W - M - f_info.getlength(foot), H - 56), foot, font=font(24), fill=th["foot"])
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG", optimize=True)
    return buf.getvalue()


def send_photo(chat_id: int, png: bytes, caption: str = "") -> dict:
    r = httpx.post(f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
                   data={"chat_id": chat_id, "caption": caption},
                   files={"photo": ("schedule.png", png, "image/png")}, timeout=60)
    return r.json()


def send_day(chat_id: int, d: date, lessons: list[Lesson], st: dict,
             caption: str = "", cache: dict | None = None) -> None:
    """Картинка в стиле, выбранном пользователем; при сбое — обычный текст."""
    if USE_IMAGES:
        try:
            theme, greet = pick_theme(st["styles"].get(str(chat_id), "auto"), d)
            key = (theme["id"], greet)
            if cache is not None and key in cache:
                png = cache[key]
            else:
                png = render_day_image(d, lessons, theme, greet)
                if cache is not None:
                    cache[key] = png
            if send_photo(chat_id, png, caption).get("ok"):
                return
        except Exception:
            log(traceback.format_exc())
    send(chat_id, (caption + "\n\n" if caption else "") + fmt_day(d, lessons))


def broadcast_day(st: dict, d: date, lessons: list[Lesson], caption: str) -> None:
    cache: dict = {}
    for cid in list(st["subs"]):
        try:
            send_day(cid, d, lessons, st, caption, cache)
        except Exception:
            log(traceback.format_exc())


# ---------- Telegram ----------
def tg(method: str, **params) -> dict:
    r = httpx.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=params, timeout=30)
    return r.json()


def send(chat_id: int, text: str, **extra) -> dict:
    return tg("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", **extra)


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
    st.setdefault("styles", {})
    return st


def save_state(st: dict) -> None:
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1, sort_keys=True))


# ---------- шаги ----------
HELP = (
    "Привет! Я присылаю расписание группы 5104-450302D.\n\n"
    f"🔔 Вечером около {DIGEST_TIME} — расписание на завтра\n"
    f"⏰ Примерно за {REMIND_MIN} мин до пары — напоминание\n"
    "🔄 Если расписание на сайте изменится — сообщу\n\n"
    "/today — сегодня\n/tomorrow — завтра\n/week — неделя\n"
    "/style — оформление картинок\n/stop — отписаться\n\n"
    "Отвечаю с задержкой до 10 минут."
)


def reply_day(chat: int, d: date, st: dict) -> None:
    try:
        send_day(chat, d, get_day(d), st)
    except Exception:
        log(traceback.format_exc())
        send(chat, "Не удалось получить расписание с ssau.ru, попробуйте позже.")


def style_keyboard() -> dict:
    keys = list(STYLE_LABELS)
    rows = [[{"text": STYLE_LABELS["auto"], "callback_data": "style:auto"}]]
    rest = keys[1:]
    for i in range(0, len(rest), 2):
        rows.append([{"text": STYLE_LABELS[k], "callback_data": f"style:{k}"} for k in rest[i:i + 2]])
    return {"inline_keyboard": rows}


def handle_updates(st: dict) -> None:
    res = tg("getUpdates", offset=st["offset"], timeout=0,
             allowed_updates=["message", "callback_query"])
    if not res.get("ok"):
        log("getUpdates error:", res)
        return
    today = datetime.now(TZ).date()
    for u in res.get("result", []):
        st["offset"] = u["update_id"] + 1

        cq = u.get("callback_query")
        if cq:  # нажатие кнопки выбора стиля
            data = cq.get("data", "")
            chat = ((cq.get("message") or {}).get("chat") or {}).get("id")
            key = data[6:] if data.startswith("style:") else ""
            if chat and key in STYLE_LABELS:
                st["styles"][str(chat)] = key
                try:
                    tg("answerCallbackQuery", callback_query_id=cq["id"], text="Готово!")
                except Exception:
                    pass
                send(chat, f"Стиль: <b>{html.escape(STYLE_LABELS[key])}</b>. Вот как выглядит сегодня:")
                reply_day(chat, today, st)
            continue

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
        elif cmd == "/style":
            send(chat, "Выберите оформление картинок. В режиме «Авто» бот сам подбирает "
                       "тему под погоду и праздники.", reply_markup=style_keyboard())
        elif cmd in ("/today", "/update"):
            reply_day(chat, today, st)
        elif cmd == "/tomorrow":
            reply_day(chat, today + timedelta(days=1), st)
        elif cmd == "/week":
            monday = today - timedelta(days=today.weekday())
            for i in range(6):
                reply_day(chat, monday + timedelta(days=i), st)


def step_digest(st: dict, now: datetime) -> None:
    hh, mm = map(int, DIGEST_TIME.split(":"))
    if now.time() >= dtime(hh, mm) and st["digest_date"] != str(now.date()):
        d = now.date() + timedelta(days=1)
        broadcast_day(st, d, get_day(d), "🔔 Расписание на завтра")
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
