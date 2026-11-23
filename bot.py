"""Telegram-бот: за 5 хвилин до пари кидає в групу назву пари і посилання з «Оголошення» АСУ."""
import os, re, time, html, json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from cryptography.fernet import Fernet

ASU = "https://asu.btsau.edu.ua"
TZ = ZoneInfo("Europe/Kyiv")
LEAD = timedelta(minutes=5)

LOGIN = os.environ["ASU_LOGIN"]
PASSWORD = os.environ["ASU_PASSWORD"]
TG_TOKEN = os.environ["TG_TOKEN"]
TG_CHAT = os.environ["TG_CHAT_ID"]
# Значення з фільтра розкладу на сайті (факультет, курс, група, студент)
FILTER = {
    "TimeTableForm[facultyId]": os.environ.get("ASU_FACULTY", "5"),
    "TimeTableForm[course]": os.environ.get("ASU_COURSE", "3"),
    "TimeTableForm[groupId]": os.environ.get("ASU_GROUP", "402"),
    "TimeTableForm[studentId]": os.environ.get("ASU_STUDENT", "31720"),
}

s = requests.Session()
s.headers["User-Agent"] = "Mozilla/5.0"


def csrf(page_html):
    return BeautifulSoup(page_html, "html.parser").find("input", {"name": "_csrf-frontend"})["value"]


def login():
    token = csrf(s.get(f"{ASU}/login").text)
    r = s.post(f"{ASU}/login", data={
        "_csrf-frontend": token,
        "LoginForm[username]": LOGIN,
        "LoginForm[password]": PASSWORD,
        "LoginForm[rememberMe]": "1",
    })
    if "/login" in r.url:
        raise RuntimeError("Не вдалося увійти в АСУ: перевір логін/пароль")


def fetch_schedule_html():
    url = f"{ASU}/time-table/student?type=0"
    page = s.get(url)
    if "/login" in page.url:
        login()
        page = s.get(url)
    today = datetime.now(TZ).date()
    data = dict(FILTER)
    data.update({
        "_csrf-frontend": csrf(page.text),
        "TimeTableForm[dateStart]": today.strftime("%d.%m.%Y"),
        "TimeTableForm[dateEnd]": (today + timedelta(days=7)).strftime("%d.%m.%Y"),  # тиждень наперед для /nextpara
        "TimeTableForm[indicationDays]": "5",
        "time-table-type": "1",
    })
    return s.post(url, data=data).text


def parse_lessons(page_html):
    """Повертає список (start: datetime, info: str, r1, r2) для всіх пар; r1/r2 = None, якщо немає «Оголошення»."""
    lessons = []
    soup = BeautifulSoup(page_html, "html.parser")
    for pop in soup.select('div[data-toggle="popover"]'):
        dm = re.match(r"(\d{2}\.\d{2}\.\d{4})", pop.get("title", ""))
        text = pop.get_text(" ", strip=True)
        m = re.search(r"(\d{1,2}):(\d{2})", text)
        if not dm or not m:
            continue
        btn = pop.find_previous_sibling("a", class_="btn-show-ads")
        r1, date = (btn["data-r1"], btn["data-r2"]) if btn else (None, dm[1])
        d = datetime.strptime(dm[1], "%d.%m.%Y")
        start = d.replace(hour=int(m[1]), minute=int(m[2]), tzinfo=TZ)
        lessons.append((start, text, r1, date))
    return lessons


def fetch_ads(r1, r2):
    r = s.get(f"{ASU}/time-table/show-ads", params={"r1": r1, "r2": r2},
              headers={"X-Requested-With": "XMLHttpRequest"})
    body = r.json().get("html", "")
    return BeautifulSoup(body, "html.parser").get_text(" ", strip=True)


def send(text):
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                  data={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
                        "disable_web_page_preview": "true"}, timeout=20)


def tg(method, **params):
    return requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", data=params, timeout=60).json()


def next_lesson_text(lessons, now):
    upcoming = sorted((l for l in lessons if l[0] > now), key=lambda l: l[0])
    if not upcoming:
        return "Найближчим тижнем пар немає 🎉"
    start, info, r1, r2 = upcoming[0]
    ads = (fetch_ads(r1, r2) if r1 else "") or "посилання немає"
    return f"📚 Наступна пара {start:%d.%m}:\n<b>{html.escape(info)}</b>\n\n{html.escape(ads)}"


def handle_commands(offset, lessons):
    """Чекає повідомлення до 50 с (замість sleep), відповідає на /nextpara у групі. Повертає новий offset."""
    for u in tg("getUpdates", offset=offset, timeout=50).get("result", []):
        offset = u["update_id"] + 1
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != TG_CHAT:
            continue
        # Telegram не дає боту список учасників: збираємо тих, хто пише в чат, і тих, хто зайшов
        for usr in [msg.get("from") or {}] + msg.get("new_chat_members", []):
            if usr.get("id") and not usr.get("is_bot"):
                add_member(usr["id"], usr.get("first_name", "студент"))
        text = msg.get("text", "")
        if text.startswith("/nextpara"):
            send(next_lesson_text(lessons, datetime.now(TZ)))
        elif text.startswith("/reg"):
            send("✅ Записав, тепер я буду тегати тебе перед парами")
    return offset


# Репо публічний: список учасників зберігаємо зашифрованим ключем із секрету MEMBERS_KEY
MEMBERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "members.enc")
FERNET = Fernet(os.environ["MEMBERS_KEY"]) if os.environ.get("MEMBERS_KEY") else None


def load_members():
    if not FERNET:
        return {}
    try:
        with open(MEMBERS_FILE, "rb") as f:
            return json.loads(FERNET.decrypt(f.read()))
    except FileNotFoundError:
        return {}


def add_member(uid, name):
    m = load_members()
    if FERNET and m.get(str(uid)) != name:
        m[str(uid)] = name
        with open(MEMBERS_FILE, "wb") as f:
            f.write(FERNET.encrypt(json.dumps(m, ensure_ascii=False).encode()))


def mentions():
    return " ".join(f'<a href="tg://user?id={i}">{html.escape(n)}</a>' for i, n in load_members().items())


def main():
    login()
    sent, lessons, loaded_at = set(), [], None
    # MAX_HOURS: для GitHub Actions (ліміт джобу 6 год) — бот сам виходить після N годин
    max_hours = float(os.environ.get("MAX_HOURS", "0"))
    deadline = datetime.now(TZ) + timedelta(hours=max_hours)
    tg("setMyCommands", commands=json.dumps([
        {"command": "nextpara", "description": "Яка наступна пара"},
        {"command": "reg", "description": "Тегати мене перед парами"}]))
    # пропускаємо старі команди, що прийшли, поки бот спав
    old = tg("getUpdates", offset=-1).get("result", [])
    offset = old[-1]["update_id"] + 1 if old else None
    while True:
        try:
            now = datetime.now(TZ)
            if max_hours and now >= deadline:
                print("done for now")
                return
            # shortcut: розклад перечитуємо раз на 30 хв, змінений в останні хвилини розклад може не підтягнутися
            if not loaded_at or now - loaded_at > timedelta(minutes=30):
                lessons, loaded_at = parse_lessons(fetch_schedule_html()), now
            for start, info, r1, r2 in lessons:
                key = (start, info)  # одна пара може стояти в двох слотах: шлемо раз
                if key not in sent and start - LEAD <= now < start:
                    ads = (fetch_ads(r1, r2) if r1 else "") or "посилання немає"
                    send(f"⏰ Через {int((start - now).total_seconds() // 60) + 1} хв пара:\n"
                         f"<b>{html.escape(info)}</b>\n\n{html.escape(ads)}\n\n{mentions()}")
                    sent.add(key)
            offset = handle_commands(offset, lessons)
        except Exception as e:
            print("error:", e)
            loaded_at = None
            time.sleep(30)


if __name__ == "__main__":
    if os.environ.get("SELFTEST"):
        sample = '''<div class="lesson-2"><a class="btn-show-ads" href="#" data-r1="128053" data-r2="23.11.2026">Оголошення</a>
        <div data-toggle="popover" title="23.11.2026 2 пара">10:35-11:35<br> Маркетинг<strong>[Пз]</strong><br><strong>ауд. Онлайн</strong></div></div>
        <div class="lesson-4"><div data-toggle="popover" title="23.11.2026 4 пара">14:05-15:05<br> Мікр.та макр.</div></div>'''
        (st, info, r1, r2), (st2, _, r1b, _) = parse_lessons(sample)
        assert st == datetime(2026, 11, 23, 10, 35, tzinfo=TZ) and r1 == "128053" and "Маркетинг" in info
        assert st2 == datetime(2026, 11, 23, 14, 5, tzinfo=TZ) and r1b is None
        import tempfile
        FERNET, MEMBERS_FILE = Fernet(Fernet.generate_key()), os.path.join(tempfile.mkdtemp(), "m.enc")
        add_member(42, "Паша<3")
        assert b"42" not in open(MEMBERS_FILE, "rb").read()
        assert mentions() == '<a href="tg://user?id=42">Паша&lt;3</a>'
        print("selftest ok")
    else:
        main()
