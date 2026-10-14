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
MENU = {"keyboard": [["📊 Отчёт", "🖼 Диаграммы"], ["🏆 Лидеры дня", "💼 Позиции"], ["💰 Выплаты", "🎯 Цели"], ["🔔 Уведомления"]],
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
    prev = {s["SECID"]: s.get("PREVPRICE") for s in table(js, "securities")}
    for m in table(js, "marketdata"):
        code = m["SECID"][2:7]
        if m["SECID"].startswith("SU") and code in P["bonds"]:
            last = m.get("LAST") or m.get("MARKETPRICE") or prev.get(m["SECID"])
            if last:
                n = P["bonds"][code]
                out["OFZ" + code] = dict(name="ОФЗ " + code, last=last, ch=m.get("LASTTOPREVPRICE") or 0.0, n=n, value=n * last * 10, bond=True)
    return out


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

def chart(q, met, history):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
    rows = list(q.values())
    classes = {"Акции": sum(x["value"] for x in rows if not x["bond"]), "ОФЗ": sum(x["value"] for x in rows if x["bond"]), "Металлы": met, "Кэш": P["cash"]}
    big = len(history) >= 2
    fig = plt.figure(figsize=(9, 8.4 if big else 4.6), dpi=130)
    gs = fig.add_gridspec(2 if big else 1, 2, width_ratios=[1, 1.35])
    ax = fig.add_subplot(gs[0, 0])
    cols = {"Акции": "#4b4fb8", "ОФЗ": "#2f8fbf", "Металлы": "#d4a017", "Кэш": "#9aa5b1"}
    lab = [k for k, v in classes.items() if v > 0]
    tot = sum(classes.values())
    ax.pie([classes[k] for k in lab], labels=[f"{k}\n{classes[k] / tot * 100:.0f}%" for k in lab], colors=[cols[k] for k in lab],
           startangle=90, counterclock=False, wedgeprops={"width": 0.38, "edgecolor": "white"})
    ax.set_title("Структура", fontweight="bold")
    ax = fig.add_subplot(gs[0, 1])
    r = sorted(rows, key=lambda x: x["ch"])
    ax.barh([x["name"] for x in r], [x["ch"] for x in r], color=["#0e9f6e" if x["ch"] >= 0 else "#d9483b" for x in r])
    ax.axvline(0, color="#888", lw=0.8)
    for i, x in enumerate(r):
        ax.text(x["ch"], i, f" {x['ch']:+.1f}% ", va="center", ha="left" if x["ch"] >= 0 else "right", fontsize=8)
    ax.set_title("Изменение за день, %", fontweight="bold")
    ax.margins(x=0.25)
    ax.set_xticks([])
    if big:
        ax = fig.add_subplot(gs[1, :])
        d = [dt.date.fromisoformat(h[0]) for h in history]
        v = [h[1] for h in history]
        ax.plot(d, v, color="#2c5ff0", lw=2.2, marker="o", ms=3)
        ax.fill_between(d, v, min(v) * 0.995, color="#2c5ff0", alpha=0.12)
        ax.annotate(rub(v[-1]), (d[-1], v[-1]), xytext=(-6, 8), textcoords="offset points", ha="right", fontweight="bold")
        ax.set_title("Капитал", fontweight="bold")
        ax.grid(axis="y", alpha=0.3)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
        ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda y, _: f"{y / 1000:.0f}k"))
    fig.tight_layout()
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
            return send_photo(chart(q, met, S["history"][-60:]), text, reply_markup=MENU)
        except Exception as e:
            print("chart failed:", e)
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


ROUTES = {"📊 Отчёт": lambda: report(with_chart=False), "🖼 Диаграммы": report, "🏆 Лидеры дня": leaders, "💼 Позиции": positions,
          "💰 Выплаты": payouts, "🎯 Цели": targets, "🔔 Уведомления": notify_menu}


def handle(u):
    msg = u.get("message") or (u.get("callback_query") or {}).get("message") or {}
    if str(msg.get("chat", {}).get("id")) != OWNER:
        return  # not the owner: ignore silently
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
    fn = ROUTES.get((msg.get("text") or "").strip())
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
            print("tick failed:", e)
        try:
            res = api("getUpdates", _t=70, offset=offset or 0, timeout=50, allowed_updates=["message", "callback_query"])
        except Exception as e:
            print("poll failed:", e)
            time.sleep(5)
            continue
        for u in res.get("result", []):
            offset = u["update_id"] + 1
            try:
                handle(u)
            except Exception as e:
                print("handle failed:", e)
                try:
                    send("⚠️ Не получилось: " + str(e)[:200])
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
