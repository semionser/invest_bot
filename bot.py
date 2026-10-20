"""Portfolio Telegram bot.

`python bot.py serve`  — answers menu buttons, sends price alerts and the 9:00/22:00 reports (runs for ~5h50m).
`python bot.py report` — sends one report with charts.
Only the owner's chat (TG_CHAT_ID) gets answers; everyone else is ignored.
"""
import datetime as dt, io, json, os, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET

ISS = "https://iss.moex.com/iss/engines/stock/markets/{m}/boards/{b}/securities.json?iss.meta=off&iss.only=marketdata,securities"
MSK = dt.timezone(dt.timedelta(hours=3))
P = json.load(open("portfolio.json", encoding="utf-8"))
STATE_FILE = "state/state.json"
TOKEN, OWNER = os.environ["TG_TOKEN"], str(os.environ["TG_CHAT_ID"])
MENU = {"keyboard": [["📊 Отчёт", "🖼 Дашборд"], ["🏆 Лидеры дня", "💼 Позиции"], ["💰 Выплаты", "🎯 Цели"], ["🔔 Уведомления"]],
        "resize_keyboard": True, "is_persistent": True}


# ---------- data ----------

def get(url, timeout=30):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "invest-bot"}), timeout=timeout) as r:
        return r.read()


def table(js, name):
    t = js[name]
    return [dict(zip(t["columns"], row)) for row in t["data"]]


def quotes():
    """{key: dict(name, last, ch, n, value, bond)} for the portfolio's stocks and OFZ."""
    out = {}
    js = json.loads(get(ISS.format(m="shares", b="TQBR") + "&securities=" + ",".join(P["stocks"])))
    prev = {s["SECID"]: s.get("PREVPRICE") for s in table(js, "securities")}
    for m in table(js, "marketdata"):
        last = m.get("LAST") or m.get("MARKETPRICE") or prev.get(m["SECID"])
        name, n = P["stocks"][m["SECID"]]
        if last:
            out[m["SECID"]] = dict(name=name, last=last, ch=m.get("LASTTOPREVPRICE") or 0.0, n=n, value=n * last, bond=False)
    js = json.loads(get(ISS.format(m="bonds", b="TQOB")))
    sec = {s["SECID"]: s for s in table(js, "securities")}
    for m in table(js, "marketdata"):
        code = m["SECID"][2:7]
        if m["SECID"].startswith("SU") and code in P["bonds"]:
            s = sec.get(m["SECID"], {})
            last = m.get("LAST") or m.get("MARKETPRICE") or s.get("PREVPRICE")
            if last:
                n, nkd = P["bonds"][code], s.get("ACCRUEDINT") or 0.0
                # like the broker: price in % of 1000 ₽ plus accrued coupon (НКД)
                out["OFZ" + code] = dict(name="ОФЗ " + code, last=last, ch=m.get("LASTTOPREVPRICE") or 0.0, n=n,
                                         value=n * (last * 10 + nkd), bond=True, secid=m["SECID"])
    for k, x in out.items():
        c = P.get("cost", {}).get(k)
        x["pnl"] = x["value"] - c if c else None
        x["pnl_pct"] = (x["value"] / c - 1) * 100 if c else None
    return out


_COUPONS = {}


def coupons(q, months=12):
    """Upcoming OFZ coupons for held bonds from the MOEX schedule: [(date, name, rub)]. Cached per day."""
    today = dt.datetime.now(MSK).date()
    if _COUPONS.get("day") != today:
        rows = []
        for k, x in q.items():
            if not x["bond"]:
                continue
            url = f"https://iss.moex.com/iss/statistics/engines/stock/markets/bonds/bondization/{x['secid']}.json?iss.meta=off&iss.only=coupons&limit=100"
            for c in table(json.loads(get(url)), "coupons"):
                d = dt.date.fromisoformat(c["coupondate"])
                if c.get("value_rub") or c.get("value"):
                    rows.append((d, x["name"], x["n"] * float(c.get("value_rub") or c["value"])))
        _COUPONS.update(day=today, rows=rows)
    end = today + dt.timedelta(days=months * 31)
    return [r for r in _COUPONS["rows"] if today <= r[0] <= end]


def metals():
    d = dt.datetime.now(MSK).date()
    q = urllib.parse.urlencode({"date_req1": (d - dt.timedelta(days=7)).strftime("%d/%m/%Y"), "date_req2": d.strftime("%d/%m/%Y")})
    res = {}
    for rec in ET.fromstring(get("https://www.cbr.ru/scripts/xml_metall.asp?" + q)).findall("Record"):
        name = {"1": "gold", "2": "silver"}.get(rec.get("Code"))
        if name:
            res[name] = float(rec.find("Buy").text.replace(",", "."))
    return res


def totals():
    q = quotes()
    try:
        m = metals()
        met = sum(n * m.get(k, 0) for k, n in P["metals"].items())
    except Exception:
        met = 0
    sec = sum(x["value"] for x in q.values())
    day = sum(x["value"] - x["value"] / (1 + x["ch"] / 100) for x in q.values())
    return q, met, sec + met + P["cash"], day


# ---------- formatting / telegram ----------

def safe(e):
    """Error text without the bot token and without long payloads (logs of a public repo are public)."""
    return type(e).__name__ + ": " + str(e).replace(TOKEN, "***")[:120]


def rub(x):
    return f"{round(x):,}".replace(",", " ") + " ₽"


def srub(x):
    return ("+" if x >= 0 else "−") + rub(abs(x))


def pct(x):
    return ("+" if x >= 0 else "−") + f"{abs(x):.1f}".replace(".", ",") + "%"


def price(x):
    return f"{x['last']:g}%" if x["bond"] else f"{x['last']:g} ₽"


def api(method, _t=40, **params):
    data = urllib.parse.urlencode({k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in params.items()}).encode()
    with urllib.request.urlopen("https://api.telegram.org/bot%s/%s" % (TOKEN, method), data, timeout=_t) as r:
        return json.loads(r.read())


def send(text, **kw):
    return api("sendMessage", chat_id=OWNER, text=text, parse_mode="HTML", disable_web_page_preview="true", **kw)


def send_photo(png, caption, **kw):
    b = "----investbot"
    fields = dict(chat_id=OWNER, caption=caption, parse_mode="HTML", **{k: json.dumps(v) for k, v in kw.items()})
    body = b"".join(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields.items())
    body += f'--{b}\r\nContent-Disposition: form-data; name="photo"; filename="r.png"\r\nContent-Type: image/png\r\n\r\n'.encode() + png + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request("https://api.telegram.org/bot%s/sendPhoto" % TOKEN, body, {"Content-Type": "multipart/form-data; boundary=" + b})
    urllib.request.urlopen(req, timeout=60)


# ---------- state (kept between runs in the Actions cache, never committed) ----------

def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {}


def save_state(s):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    json.dump(s, open(STATE_FILE, "w"), ensure_ascii=False)


S = load_state()
S.setdefault("notify", {"on": True, "step": 3})
S.setdefault("history", P.get("history_seed", []))
S.setdefault("alerts", {})
S.setdefault("sent", {})


# ---------- charts ----------

SECTOR = {"YDEX": "IT и рост", "OZON": "IT и рост", "POSI": "IT и рост", "SBER": "Финансы", "MOEX": "Финансы",
          "LKOH": "Нефть", "TATN": "Нефть", "X5": "Ритейл", "GMKN": "Металлургия"}
C = dict(ink="#1f2430", mute="#7a8394", grid="#e6e8ee", card="#f4f5f8", up="#12a150", dn="#d9483b", acc="#3b5bdb",
         cls={"Акции": "#4b4fb8", "ОФЗ": "#2f8fbf", "Металлы": "#d4a017", "Кэш": "#9aa5b1"})


def upcoming(q):
    """Coupons from MOEX plus dividends from the portfolio file, next 12 months."""
    today = dt.datetime.now(MSK).date()
    try:
        rows = coupons(q)
    except Exception:
        rows = [(dt.date.fromisoformat(p[0]), p[1], p[2]) for p in P["payouts"] if p[1].startswith("ОФЗ")]
    rows += [(dt.date.fromisoformat(p[0]), p[1], p[2]) for p in P["payouts"] if not p[1].startswith("ОФЗ")]
    return sorted(r for r in rows if r[0] >= today)


def dashboard(q, met, total, day, history):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    plt.rcParams.update({"font.size": 10, "font.family": "DejaVu Sans", "axes.spines.top": False, "axes.spines.right": False,
                         "axes.edgecolor": C["grid"], "axes.labelcolor": C["ink"], "xtick.color": C["mute"], "ytick.color": C["ink"],
                         "text.color": C["ink"], "axes.titlesize": 12, "axes.titleweight": "bold", "axes.titlelocation": "left"})
    now = dt.datetime.now(MSK)
    rows = list(q.values())
    stocks = [x for x in rows if not x["bond"]]
    classes = {"Акции": sum(x["value"] for x in stocks), "ОФЗ": sum(x["value"] for x in rows if x["bond"]), "Металлы": met, "Кэш": P["cash"]}
    pnl_rows = [x for x in rows if x.get("pnl") is not None]
    pnl = sum(x["pnl"] for x in pnl_rows)
    pays = upcoming(q)
    year_income = sum(p[2] for p in pays)
    hist = history if len(history) >= 2 else []

    fig = plt.figure(figsize=(10, 15.5 if hist else 13), dpi=120, facecolor="white")
    gs = fig.add_gridspec(5 if hist else 4, 2, height_ratios=[0.5, 1.15, 1.25, 1, 1] if hist else [0.5, 1.15, 1.25, 1],
                          hspace=0.42, wspace=0.34, left=0.1, right=0.97, top=0.95, bottom=0.04)
    fig.suptitle(f"Портфель · {now:%d.%m.%Y %H:%M} МСК", x=0.1, ha="left", fontsize=16, fontweight="bold")

    # KPI tiles
    ax = fig.add_subplot(gs[0, :])
    ax.axis("off")
    tiles = [("Капитал", rub(total), None), ("За день", srub(day), day), ("С покупки", srub(pnl) if pnl_rows else "—", pnl if pnl_rows else None),
             ("Выплаты за 12 мес", rub(year_income), None)]
    for i, (lbl, val, sign) in enumerate(tiles):
        x0 = i * 0.25 + 0.005
        ax.add_patch(plt.Rectangle((x0, 0.02), 0.235, 0.96, transform=ax.transAxes, color=C["card"], zorder=0))
        ax.text(x0 + 0.015, 0.68, lbl, transform=ax.transAxes, color=C["mute"], fontsize=10)
        col = C["ink"] if sign is None else (C["up"] if sign >= 0 else C["dn"])
        ax.text(x0 + 0.015, 0.22, val, transform=ax.transAxes, color=col, fontsize=15, fontweight="bold")

    # structure donut
    ax = fig.add_subplot(gs[1, 0])
    lab = [k for k, v in classes.items() if v > 0]
    tot = sum(classes.values())
    ax.pie([classes[k] for k in lab], colors=[C["cls"][k] for k in lab], startangle=90, counterclock=False,
           wedgeprops={"width": 0.36, "edgecolor": "white", "linewidth": 2})
    ax.text(0, 0.08, rub(tot), ha="center", fontsize=12, fontweight="bold")
    ax.text(0, -0.14, "всего", ha="center", color=C["mute"], fontsize=9)
    ax.legend([f"{k} · {classes[k] / tot * 100:.0f}%" for k in lab], loc="upper center", bbox_to_anchor=(0.5, 0.02), ncol=2, frameon=False, fontsize=9)
    ax.set_title("Структура")

    # position weights
    ax = fig.add_subplot(gs[1, 1])
    w = sorted(rows, key=lambda x: x["value"])[-10:]
    ax.barh([x["name"] for x in w], [x["value"] / total * 100 for x in w],
            color=[C["cls"]["ОФЗ"] if x["bond"] else C["cls"]["Акции"] for x in w])
    ax.axvline(15, color=C["dn"], ls="--", lw=1)
    ax.text(15, -0.9, "15% — порог перевеса", color=C["dn"], fontsize=8, ha="right", va="center")
    ax.set_xlim(0, max(18, max(x["value"] / total * 100 for x in w) + 4))
    for i, x in enumerate(w):
        ax.text(x["value"] / total * 100, i, f" {x['value'] / total * 100:.1f}%", va="center", fontsize=8, color=C["mute"])
    ax.set_xticks([])
    ax.set_title("Доли позиций, % капитала")

    # day change
    ax = fig.add_subplot(gs[2, 0])
    r = sorted(stocks, key=lambda x: x["ch"])
    ax.barh([x["name"] for x in r], [x["ch"] for x in r], color=[C["up"] if x["ch"] >= 0 else C["dn"] for x in r])
    ax.axvline(0, color=C["mute"], lw=0.8)
    for i, x in enumerate(r):
        ax.text(x["ch"], i, f" {x['ch']:+.1f}% ", va="center", ha="left" if x["ch"] >= 0 else "right", fontsize=8)
    ax.margins(x=0.3)
    ax.set_xticks([])
    bch = [x["ch"] for x in rows if x["bond"]]
    ax.set_title("Акции за день, %" + (f"  ·  ОФЗ в среднем {sum(bch) / len(bch):+.1f}%" if bch else ""))

    # result since purchase
    ax = fig.add_subplot(gs[2, 1])
    if pnl_rows:
        r = sorted(pnl_rows, key=lambda x: x["pnl"])
        ax.barh([x["name"] for x in r], [x["pnl"] for x in r], color=[C["up"] if x["pnl"] >= 0 else C["dn"] for x in r])
        ax.axvline(0, color=C["mute"], lw=0.8)
        for i, x in enumerate(r):
            ax.text(x["pnl"], i, f" {srub(x['pnl'])} ({x['pnl_pct']:+.0f}%) ", va="center", ha="left" if x["pnl"] >= 0 else "right", fontsize=8)
        ax.margins(x=0.7)
        ax.set_xticks([])
    else:
        ax.axis("off")
        ax.text(0.5, 0.5, "Нет цен покупки", ha="center", transform=ax.transAxes, color=C["mute"])
    ax.set_title("Результат с покупки, ₽")

    # capital history
    row = 3
    if hist:
        ax = fig.add_subplot(gs[row, :])
        d = [dt.date.fromisoformat(h[0]) for h in hist]
        v = [h[1] for h in hist]
        ax.plot(d, v, color=C["acc"], lw=2.2, marker="o", ms=3)
        ax.fill_between(d, v, min(v) * 0.995, color=C["acc"], alpha=0.1)
        ax.annotate(rub(v[-1]), (d[-1], v[-1]), xytext=(-6, 8), textcoords="offset points", ha="right", fontweight="bold")
        ax.grid(axis="y", color=C["grid"])
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
        ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda y, _: f"{y / 1000:.0f}k"))
        ax.set_title("Капитал по вечерам")
        row += 1

    # payouts by month
    ax = fig.add_subplot(gs[row, :])
    months = [(now.year + (now.month - 1 + i) // 12, (now.month - 1 + i) % 12 + 1) for i in range(12)]
    cpn = [sum(p[2] for p in pays if (p[0].year, p[0].month) == m and p[1].startswith("ОФЗ")) for m in months]
    div = [sum(p[2] for p in pays if (p[0].year, p[0].month) == m and not p[1].startswith("ОФЗ")) for m in months]
    names = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
    xl = [names[m - 1] for _, m in months]
    ax.bar(xl, cpn, color=C["cls"]["ОФЗ"], label="Купоны ОФЗ")
    ax.bar(xl, div, bottom=cpn, color=C["cls"]["Металлы"], label="Дивиденды")
    for i, (a, b) in enumerate(zip(cpn, div)):
        if a + b:
            ax.text(i, a + b, f"{a + b:.0f}", ha="center", va="bottom", fontsize=8)
    ax.set_yticks([])
    ax.set_ylim(0, max(max(a + b for a, b in zip(cpn, div)), 1) * 1.22)
    ax.spines["left"].set_visible(False)
    ax.legend(frameon=False, fontsize=9, loc="upper right")
    ax.set_title(f"Выплаты по месяцам · {rub(year_income)} за 12 мес")

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


# ---------- screens ----------

def report(with_chart=True, record=False):
    q, met, total, day = totals()
    now = dt.datetime.now(MSK)
    if record:
        S["history"] = [h for h in S["history"] if h[0] != now.date().isoformat()] + [[now.date().isoformat(), round(total)]]
        S["history"] = S["history"][-400:]
    L = sorted(q.values(), key=lambda x: x["ch"], reverse=True)
    head = "🌅 Утро" if now.hour < 12 else "📊 Портфель" if now.hour < 19 else "🌙 Итоги дня"
    lines = [f"<b>{head} · {now:%d.%m %H:%M}</b>", f"Капитал: <b>{rub(total)}</b>", f"За день: <b>{srub(day)}</b>"]
    pr = [x for x in q.values() if x.get("pnl") is not None]
    if pr:
        lines.append(f"С покупки (акции и ОФЗ): <b>{srub(sum(x['pnl'] for x in pr))}</b>")
    week = [h for h in S["history"] if dt.date.fromisoformat(h[0]) <= now.date() - dt.timedelta(days=7)]
    if week:
        lines.append(f"За неделю: <b>{srub(total - week[-1][1])}</b>")
    lines += ["", "📈 <b>Лидеры</b>"] + [f"{x['name']} {pct(x['ch'])}" for x in L[:3]]
    lines += ["", "📉 <b>Аутсайдеры</b>"] + [f"{x['name']} {pct(x['ch'])}" for x in L[-3:][::-1]]
    soon = [p for p in P["payouts"] if 0 <= (dt.date.fromisoformat(p[0]) - now.date()).days <= 14]
    if soon:
        lines += ["", "💰 <b>Скоро выплаты</b>"] + [f"{dt.date.fromisoformat(p[0]):%d.%m} {p[1]} · {rub(p[2])}" for p in soon]
    lines += ["", "<i>Не инвестиционная рекомендация</i>"]
    text = "\n".join(lines)
    if with_chart:
        try:
            return send_photo(dashboard(q, met, total, day, S["history"][-90:]), text, reply_markup=MENU)
        except Exception as e:
            print("chart failed:", type(e).__name__)
    send(text, reply_markup=MENU)


def leaders():
    q = quotes()
    L = sorted(q.values(), key=lambda x: x["ch"], reverse=True)
    w = max(len(x["name"]) for x in L)
    body = "\n".join(f"{'🟢' if x['ch'] > 0 else '🔴' if x['ch'] < 0 else '⚪'} {x['name'].ljust(w)} {pct(x['ch']):>7}" for x in L)
    send("<b>🏆 Изменение за день</b>\n<pre>" + body + "</pre>")


def positions():
    keys = list(P["stocks"]) + ["OFZ" + c for c in P["bonds"]]
    name = lambda k: "ОФЗ " + k[3:] if k.startswith("OFZ") else P["stocks"][k][0]
    kb = [[{"text": name(k), "callback_data": "pos:" + k} for k in keys[i:i + 3]] for i in range(0, len(keys), 3)]
    send("<b>💼 Выбери бумагу</b>", reply_markup={"inline_keyboard": kb})


def position(key):
    q, met, total, day = totals()
    x = q.get(key)
    if not x:
        return send("Нет котировки, попробуй позже.")
    lines = [f"<b>{x['name']}</b>", f"Цена: <b>{price(x)}</b> ({pct(x['ch'])} за день)", f"У тебя: {x['n']} шт. · {rub(x['value'])}",
             f"Доля в капитале: {x['value'] / total * 100:.1f}%".replace(".", ",")]
    if x.get("pnl") is not None:
        lines.append(f"С покупки: <b>{srub(x['pnl'])}</b> ({pct(x['pnl_pct'])})")
    for k, side, p, _ in P.get("targets", []):
        if k == key:
            lines.append(f"🎯 Цель {'≤' if side == 'below' else '≥'} {p} ₽ · осталось {pct((p / x['last'] - 1) * 100)}")
    send("\n".join(lines))


def payouts():
    today = dt.datetime.now(MSK).date()
    up = [p for p in P["payouts"] if dt.date.fromisoformat(p[0]) >= today]
    lines = [f"{dt.date.fromisoformat(p[0]):%d.%m} · {p[1]} · <b>{rub(p[2])}</b> (через {(dt.date.fromisoformat(p[0]) - today).days} дн.)" for p in up]
    send("<b>💰 Ближайшие выплаты</b>\n" + ("\n".join(lines) or "Пока ничего не ждём.") + f"\n\nВсего: <b>{rub(sum(p[2] for p in up))}</b>")


def targets():
    q = quotes()
    lines = [f"{q[k]['name']}: сейчас {price(q[k])}, цель {'≤' if s == 'below' else '≥'} {p} ₽ · осталось {pct((p / q[k]['last'] - 1) * 100)}"
             for k, s, p, _ in P.get("targets", []) if k in q]
    send("<b>🎯 Цели</b>\n" + ("\n".join(lines) or "Целей пока нет."))


def notify_menu(edit_id=None):
    n = S["notify"]
    text = (f"<b>🔔 Уведомления</b>\nСейчас: <b>{'включены' if n['on'] else 'выключены'}</b>, порог <b>{n['step']}%</b> за день.\n"
            "Проверка каждые 15 минут, 10:00–19:00 по будням.")
    kb = {"inline_keyboard": [[{"text": "🔕 Выключить" if n["on"] else "🔔 Включить", "callback_data": "n:toggle"}],
                              [{"text": ("✅ " if n["step"] == v else "") + f"{v}%", "callback_data": f"n:{v}"} for v in (2, 3, 5)]]}
    if edit_id:
        return api("editMessageText", chat_id=OWNER, message_id=edit_id, text=text, parse_mode="HTML", reply_markup=kb)
    send(text, reply_markup=kb)


ROUTES = {"📊 Отчёт": lambda: report(with_chart=False), "🖼 Дашборд": report, "🖼 Диаграммы": report, "🏆 Лидеры дня": leaders, "💼 Позиции": positions,
          "💰 Выплаты": payouts, "🎯 Цели": targets, "🔔 Уведомления": notify_menu}


def handle(u):
    msg = u.get("message") or (u.get("callback_query") or {}).get("message") or {}
    chat = msg.get("chat", {})
    if str(chat.get("id")) != OWNER or chat.get("type") != "private":
        return  # not the owner's private chat: ignore silently
    if "callback_query" in u:
        cq = u["callback_query"]
        api("answerCallbackQuery", callback_query_id=cq["id"])
        d = cq.get("data", "")
        if d.startswith("pos:"):
            return position(d[4:])
        if d.startswith("n:"):
            if d == "n:toggle":
                S["notify"]["on"] = not S["notify"]["on"]
            else:
                S["notify"]["step"] = int(d[2:])
            save_state(S)
            return notify_menu(cq["message"]["message_id"])
        return
    text = (msg.get("text") or "").strip()
    if text in ("/start", "/menu"):
        return send("Меню 👇", reply_markup=MENU)
    fn = ROUTES.get(text)
    if fn:
        return fn()
    send("Выбери действие на клавиатуре 👇", reply_markup=MENU)


# ---------- background jobs ----------

def alerts():
    n = S["notify"]
    if not n["on"]:
        return
    today = dt.datetime.now(MSK).date().isoformat()
    a = S["alerts"] if S["alerts"].get("day") == today else {"day": today, "lvl": {}, "targets": []}
    q, msgs = quotes(), []
    for k, x in q.items():
        lvl = int(abs(x["ch"]) // n["step"]) * (1 if x["ch"] >= 0 else -1)
        if lvl and abs(lvl) > abs(a["lvl"].get(k, 0)):
            msgs.append(f"{'🚀' if x['ch'] > 0 else '🔻'} <b>{x['name']}</b> {pct(x['ch'])} за день · {price(x)}")
            a["lvl"][k] = lvl
    for k, side, p, text in P.get("targets", []):
        if k in q and k not in a["targets"] and (q[k]["last"] <= p if side == "below" else q[k]["last"] >= p):
            msgs.append("🎯 " + text)
            a["targets"].append(k)
    S["alerts"] = a
    if msgs:
        send("\n".join(msgs))


def tick():
    """Scheduled work, called between Telegram polls."""
    now = dt.datetime.now(MSK)
    weekday = now.weekday() < 5
    for name, hour in (("morning", 9), ("evening", 22)):
        key = f"{name}:{now.date()}"
        if weekday and now.hour == hour and now.minute < 30 and key not in S["sent"]:
            report(record=(name == "evening"))
            S["sent"] = {k: v for k, v in S["sent"].items() if k.endswith(str(now.date()))}
            S["sent"][key] = 1
    slot = f"{now:%Y-%m-%d %H}:{now.minute // 15}"
    if weekday and 10 <= now.hour < 19 and S.get("alert_slot") != slot:
        S["alert_slot"] = slot
        alerts()
    save_state(S)


def serve(limit_s):
    api("setMyCommands", commands=[{"command": "start", "description": "Показать меню"}])
    api("deleteWebhook")
    end, offset = time.time() + limit_s, None
    while time.time() < end:
        try:
            tick()
        except Exception as e:
            print("tick failed:", type(e).__name__)
        try:
            res = api("getUpdates", _t=70, offset=offset or 0, timeout=50, allowed_updates=["message", "callback_query"])
        except Exception as e:
            print("poll failed:", type(e).__name__)
            time.sleep(5)
            continue
        for u in res.get("result", []):
            offset = u["update_id"] + 1
            try:
                handle(u)
            except Exception as e:
                print("handle failed:", type(e).__name__)
                try:
                    send("⚠️ Не получилось, попробуй ещё раз через минуту. (" + safe(e) + ")")
                except Exception:
                    pass
    save_state(S)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if mode == "report":
        report()
        save_state(S)
    else:
        serve(float(os.environ.get("SERVE_SECONDS", 5 * 3600 + 50 * 60)))
