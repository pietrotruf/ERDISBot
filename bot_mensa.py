"""
Bot Telegram - Menu mense ERDIS (solo giorno corrente)

Installazione:
    pip install "python-telegram-bot>=21" requests beautifulsoup4

Avvio:
    token e impostazioni in variabili d'ambiente oppure in un file .env
    (una riga per variabile), ad esempio:
        TELEGRAM_TOKEN=il_tuo_token
        DAILY_NOTIFY_TIME=11:30
    poi:  python bot_mensa.py

Comandi:
    /start         -> scegli la mensa (la prima volta), poi mostra il menu di oggi
    /oggi          -> menu di oggi della mensa scelta
    /sede          -> cambia mensa
    /impostazioni  -> mensa scelta e avviso giornaliero (on/off)

Variabili configurabili (da .env o dal codice, vedi sezione CONFIGURAZIONE):
    DAILY_NOTIFY_TIME          ora dell'avviso giornaliero, HH:MM
    CACHE_TTL_MINUTES          dopo quanti minuti si riscarica la pagina
    REFRESH_MIN_AGE_MINUTES    età minima della copia perché 🔄 la riscarichi

Per aggiungere una mensa: vedi il dizionario MENSE qui sotto.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes)

logging.basicConfig(level=logging.INFO)

def _load_dotenv():
    """Carica le variabili dal file .env (stessa cartella dello script o cartella
    da cui lanci il bot). Le variabili d'ambiente già impostate hanno la precedenza."""
    for env_file in (Path(__file__).with_name(".env"), Path.cwd() / ".env"):
        if not env_file.is_file():
            continue
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:]
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.split(" #", 1)[0].strip().strip("\"'")
            os.environ.setdefault(key.strip(), value)


_load_dotenv()

TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
if not TOKEN:
    raise SystemExit("Token mancante: imposta la variabile TELEGRAM_TOKEN "
                     "oppure crea un file .env con TELEGRAM_TOKEN=il_tuo_token")

USERS_FILE = Path(os.environ.get("USERS_FILE") or Path(__file__).with_name("utenti.json"))  # scelte salvate per chat
STATE_FILE = USERS_FILE.with_name("stato.json")  # ricorda quando è partito l'ultimo avviso

# ========================================================== CONFIGURAZIONE ===
# Cambia i valori qui sotto, oppure metti nel .env (o in una variabile d'ambiente)
# la variabile con lo stesso nome: quella ha la precedenza sul valore scritto qui.
DAILY_NOTIFY_TIME_DEFAULT = "11:30"      # ora dell'avviso giornaliero (HH:MM, ora locale)
CACHE_TTL_MINUTES_DEFAULT = 60           # oltre questa età la pagina viene riscaricata
REFRESH_MIN_AGE_MINUTES_DEFAULT = 5      # il pulsante 🔄 riscarica solo se la copia ha almeno questa età
NOTIFY_IF_CLOSED = False                 # mandare l'avviso anche se la mensa è chiusa?
NOTIFY_WINDOW = timedelta(hours=2)       # se il bot era spento all'ora X, recupera entro questo tempo


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name}='{raw}' non valido: serve un numero intero")


def _env_time(name: str, default: str) -> dtime:
    raw = os.environ.get(name, default).strip()
    try:
        return datetime.strptime(raw, "%H:%M").time()
    except ValueError:
        raise SystemExit(f"{name}='{raw}' non valido: usa il formato HH:MM, es. 11:30")


DAILY_TIME = _env_time("DAILY_NOTIFY_TIME", DAILY_NOTIFY_TIME_DEFAULT)
DAILY_TIME_STR = DAILY_TIME.strftime("%H:%M")
CACHE_TTL = timedelta(minutes=_env_int("CACHE_TTL_MINUTES", CACHE_TTL_MINUTES_DEFAULT))
REFRESH_MIN_AGE = timedelta(minutes=_env_int("REFRESH_MIN_AGE_MINUTES", REFRESH_MIN_AGE_MINUTES_DEFAULT))

# =========================================================== MENSE (ID) =====
# L'ID è il valore usato dal sito (<option value="...">) e finisce nell'URL.
# Modello comune: {id} = ID mensa, {y} anno, {m} mese 01-12, {d} giorno 01-31
URL_TEMPLATE = "https://menu.erdis.it/Mensa_{id}/Menu_Del_Giorno_{y}_{m:02d}_{d:02d}_{id}.html"

# Per una mensa con URL o HTML diverso aggiungi "url": "..." e/o cambia "parser".
MENSE = {
    # Ancona
    "Matteotti": {"name": "Ancona - Mensa Matteotti", "parser": "erdis"},
    "Petrarca":  {"name": "Ancona - Mensa Petrarca", "parser": "erdis"},
    "Strabone":  {"name": "Ancona - Mensa Strabone di Fermo", "parser": "erdis"},
    "Tenna":     {"name": "Ancona - Mensa Torrette", "parser": "erdis"},
    # Camerino
    "Davack":    {"name": "Camerino - Mensa D'Avack", "parser": "erdis"},
    "Paradiso":  {"name": "Camerino - Mensa Colle Paradiso", "parser": "erdis"},
    "Gma":       {"name": "Camerino - Mensa G.M.A di Matelica", "parser": "erdis"},
    # Macerata
    "Accorretti": {"name": "Macerata - Mensa Accoretti", "parser": "erdis"},
    "Bertelli":  {"name": "Macerata - Mensa Polo Bertelli", "parser": "erdis"},
    # Urbino
    "Tridente":  {"name": "Urbino - Mensa Tridente", "parser": "erdis"},
    "Duca":      {"name": "Urbino - Mensa Duca", "parser": "erdis"},
}


def build_urls(mensa_id: str, day: date) -> list[str]:
    """Costruisce gli URL da provare per una mensa e un giorno."""
    tpl = MENSE[mensa_id].get("url", URL_TEMPLATE)
    url = tpl.format(id=mensa_id, y=day.year, m=day.month, d=day.day)
    urls = [url]
    if url.endswith(".html"):          # alcune pagine usano .htm
        urls.append(url[:-1])
    return urls


# ---------------------------------------------------------------- FETCH -----

def fetch_menu_html(mensa_id: str, day: date) -> str | None:
    for url in build_urls(mensa_id, day):
        try:
            r = requests.get(url, timeout=15)
        except requests.RequestException:
            continue
        if r.status_code == 200 and r.text.strip():
            r.encoding = "utf-8"
            return r.text
    return None


# ---------------------------------------------------------------- PARSER ----
# Sezioni mostrate (chiave = testo della cella "Pasto", in minuscolo).
SECTION_LABELS = {
    "primo": "🍝 Primi",
    "secondo": "🥩 Secondi",
    "contorno": "🥗 Contorni",
    # "frutta": "🍎 Frutta",
    # "dessert": "🍮 Dessert",
}
ALL_MEALS = {"primo", "secondo", "contorno", "frutta", "dessert"}


def _txt(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ")).strip()


def _find_table(soup, header_word: str):
    for t in soup.find_all("table"):
        ths = [_txt(th).lower() for th in t.find_all("th")]
        if any(header_word in h for h in ths):
            return t
    return None


def parse_erdis(page: str) -> dict:
    """Parser per il formato ERDIS (due tabelle: stato + menu)."""
    soup = BeautifulSoup(page, "html.parser")
    result = {"closed": False, "takeaway_only": False,
              "opening": None, "closing": None, "updated": None,
              "menu": {k: [] for k in SECTION_LABELS}}

    m = re.search(r"Aggiornamento:?\s*(\d{2}/\d{2}/\d{4})\s+(\d{1,2}:\d{2})", soup.get_text(" "))
    if m:
        result["updated"] = f"{m.group(1)} alle {m.group(2)}"

    status = _find_table(soup, "apertura")
    if status:
        for tr in status.find_all("tr"):
            cells = [_txt(td) for td in tr.find_all("td")]
            if cells and cells[0].lower() == "pranzo":
                if len(cells) > 1 and re.search(r"\d", cells[1]):
                    result["opening"] = cells[1]
                if len(cells) > 2 and re.search(r"\d", cells[2]):
                    result["closing"] = cells[2]
                if len(cells) > 3:
                    result["closed"] = cells[3].upper().startswith("S")
                if len(cells) > 4:
                    result["takeaway_only"] = cells[4].upper().startswith("S")
                break

    menu_tbl = _find_table(soup, "pasto")
    if menu_tbl:
        turno = pasto = None
        for tr in menu_tbl.find_all("tr"):
            cells = [_txt(td) for td in tr.find_all("td")]
            if not cells:
                continue
            i = 0
            if cells[i].lower() in ("pranzo", "cena"):
                turno, pasto = cells[i].lower(), None
                i += 1
            if i < len(cells) and cells[i].lower() in ALL_MEALS:
                pasto = cells[i].lower()
                i += 1
            if i >= len(cells) or turno != "pranzo" or pasto not in SECTION_LABELS:
                continue
            dish = cells[i].strip()
            if dish:
                result["menu"][pasto].append(dish.capitalize())
    return result


# Per una mensa con HTML diverso: scrivi un nuovo parser che restituisca lo
# stesso dizionario di parse_erdis, registralo qui e usa il suo nome in MENSE.
PARSERS = {
    "erdis": parse_erdis,
}


# --------------------------------------------------------------- FORMAT -----

# Rientro dei piatti rispetto al tipo. Telegram non rende bene il \t, quindi uso
# spazi "em" (larghi come una M): aggiungine o togline per cambiare il rientro.
def _esc(t: str) -> str:
    """Escape per l'HTML di Telegram (gli apostrofi restano com'è)."""
    return html.escape(t, quote=False)


INDENT = "\u2003\u2003"

DAYS_IT = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]


def _format_body(mensa_name: str, day: date, data: dict | None) -> str:
    title = f"🍽 <b>{_esc(mensa_name)}</b> — {DAYS_IT[day.weekday()]} {day.strftime('%d/%m/%Y')}\n"
    if data is None:
        return title + "\n⚠️ Menu di oggi non disponibile."
    if data["closed"]:
        return title + "\n🔴 <b>Mensa chiusa</b> a pranzo"

    out = [title]
    if data["takeaway_only"]:
        out.append("🥡 <b>Solo asporto</b>")
    if data["opening"]:
        line = f"🕐 Apre alle <b>{_esc(data['opening'])}</b>"
        if data["closing"]:
            line += f" · chiude alle <b>{_esc(data['closing'])}</b>"
        out.append(line)
    out.append("")

    for key, label in SECTION_LABELS.items():
        dishes = data["menu"][key]
        if dishes:
            out.append(f"<b>{label}</b>")
            out += [f"{INDENT}• <b>{_esc(d)}</b>" for d in dishes]
            out.append("")
    if not any(data["menu"].values()):
        out.append("Nessun piatto trovato nella pagina.")
    return "\n".join(out).strip()


def format_message(mensa_name: str, day: date, data: dict | None) -> str:
    text = _format_body(mensa_name, day, data)
    if data is None:
        return text
    if data.get("updated"):
        footer = f"🕒 Aggiornato il {data['updated']}"
    else:  # la pagina non riporta l'aggiornamento: uso l'orario di lettura
        footer = f"🕒 Controllato il {datetime.now().strftime('%d/%m/%Y alle %H:%M')}"
    return f"{text}\n\n<i>{_esc(footer)}</i>"


# ======================================================= BANNER / CONSENSO ===
# Scrivi qui il messaggio del banner. Accetta HTML di Telegram
# (<b>grassetto</b>, <i>corsivo</i>, <a href="...">link</a>).
BANNER_TEXT = "[INSERISCI QUI IL MESSAGGIO DEL BANNER]"

# Se in futuro modifichi il banner e vuoi che tutti lo riaccettino,
# aumenta questo numero.
BANNER_VERSION = 1

ACCEPT_LABEL = "✅ Accetto"
DECLINE_LABEL = "❌ Non accetto"
DECLINED_TEXT = "Senza accettare non posso mostrarti i menu. Se cambi idea, scrivi /start."


# -------------------------------------------------------- SCELTE UTENTI -----
# Formato di utenti.json:
#   {"<chat_id>": {"mensa": "Davack", "accepted_at": "2026-10-12T12:00:00", "banner_version": 1}}

def load_users() -> dict:
    try:
        data = json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    # migrazione dal vecchio formato {"<chat_id>": "mensa"}
    return {k: (v if isinstance(v, dict) else {"mensa": v}) for k, v in data.items()}


def get_user(chat_id: int) -> dict:
    return load_users().get(str(chat_id), {})


def update_user(chat_id: int, **fields):
    users = load_users()
    users.setdefault(str(chat_id), {}).update(fields)
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    USERS_FILE.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8")


def _accepted(user: dict) -> bool:
    return bool(user.get("accepted_at")) and user.get("banner_version") == BANNER_VERSION


def has_accepted(chat_id: int) -> bool:
    return _accepted(get_user(chat_id))


def set_accepted(chat_id: int):
    update_user(chat_id, accepted_at=datetime.now().isoformat(timespec="seconds"),
                banner_version=BANNER_VERSION)


def get_choice(chat_id: int) -> str | None:
    m = get_user(chat_id).get("mensa")
    return m if m in MENSE else None


def set_choice(chat_id: int, mensa_id: str):
    update_user(chat_id, mensa=mensa_id)


# ------------------------------------------------------------ TASTIERE ------

def consent_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(ACCEPT_LABEL, callback_data="a:y"),
        InlineKeyboardButton(DECLINE_LABEL, callback_data="a:n"),
    ]])


def mense_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(m["name"], callback_data=f"s:{mid}")] for mid, m in MENSE.items()])


def menu_keyboard(day: date) -> InlineKeyboardMarkup:
    """Il giorno mostrato finisce nel callback_data del pulsante Aggiorna."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Aggiorna", callback_data=f"r:{day:%Y%m%d}"),
         InlineKeyboardButton("📍 Cambia mensa", callback_data="c")],
        [InlineKeyboardButton("⚙️ Impostazioni", callback_data="i")],
    ])


CHOOSE_TEXT = "Scegli la mensa:"


# ---- Cache in memoria: (mensa, giorno) -> (momento del download, dati letti)
# La pagina HTML viene riscaricata solo se la copia ha più di CACHE_TTL.
_cache: dict = {}
_locks: dict = {}


async def get_menu_data(mensa_id: str, day: date, force: bool = False) -> dict | None:
    """Dati del menu: dalla cache se la copia è recente, altrimenti dal sito.
    force=True (pulsante Aggiorna) riscarica se la copia ha almeno REFRESH_MIN_AGE."""
    key = (mensa_id, day)
    lock = _locks.setdefault(key, asyncio.Lock())   # un solo download alla volta per mensa
    async with lock:
        now = datetime.now()
        entry = _cache.get(key)
        if entry:
            fetched_at, data = entry
            age = now - fetched_at
            ttl = CACHE_TTL if data is not None else REFRESH_MIN_AGE  # "non disponibile": ritenta presto
            if age < ttl and not (force and age >= REFRESH_MIN_AGE):
                return data

        page = await asyncio.to_thread(fetch_menu_html, mensa_id, day)
        if page:
            data = PARSERS[MENSE[mensa_id]["parser"]](page)
            for k in [k for k in _cache if k[1] != day]:   # butto le copie dei giorni passati
                _cache.pop(k, None)
                _locks.pop(k, None)
            _cache[key] = (now, data)
            return data

        if entry and entry[1] is not None:
            # sito non raggiungibile: tengo la vecchia copia e ritento tra REFRESH_MIN_AGE
            _cache[key] = (now - CACHE_TTL + REFRESH_MIN_AGE, entry[1])
            return entry[1]
        _cache[key] = (now, None)
        return None


def render_view(mensa_id: str, day: date, data: dict | None) -> tuple[str, InlineKeyboardMarkup]:
    return format_message(MENSE[mensa_id]["name"], day, data), menu_keyboard(day)


async def build_view(mensa_id: str, force: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    today = date.today()
    data = await get_menu_data(mensa_id, today, force)
    return render_view(mensa_id, today, data)


async def safe_edit(q, text, kb) -> bool:
    """Modifica il messaggio. False se il contenuto era identico."""
    try:
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        return True
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return False
        raise


async def show_banner(update: Update):
    """Mostra il banner (da comando o da pulsante)."""
    if update.callback_query:
        await safe_edit(update.callback_query, BANNER_TEXT, consent_keyboard())
    else:
        await update.message.reply_text(BANNER_TEXT, parse_mode="HTML",
                                        reply_markup=consent_keyboard())


# ------------------------------------------------------------- HANDLERS -----
# Ogni handler (tranne on_consent) controlla prima l'accettazione del banner.

async def show_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not has_accepted(chat_id):
        await show_banner(update)
        return
    mid = get_choice(chat_id)
    if mid is None:
        await update.message.reply_text(CHOOSE_TEXT, reply_markup=mense_keyboard())
        return
    text, kb = await build_view(mid)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def sede(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not has_accepted(update.effective_chat.id):
        await show_banner(update)
        return
    await update.message.reply_text(CHOOSE_TEXT, reply_markup=mense_keyboard())


async def on_consent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = update.effective_chat.id
    if q.data == "a:y":
        set_accepted(chat_id)
        mid = get_choice(chat_id)
        if mid:
            text, kb = await build_view(mid)
            await safe_edit(q, text, kb)
        else:
            await safe_edit(q, CHOOSE_TEXT, mense_keyboard())
    else:
        await safe_edit(q, DECLINED_TEXT, None)


async def on_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = update.effective_chat.id
    if not has_accepted(chat_id):
        await show_banner(update)
        return
    mid = q.data.split(":", 1)[1]
    if mid not in MENSE:
        return
    set_choice(chat_id, mid)
    text, kb = await build_view(mid)
    await safe_edit(q, text, kb)


def shown_day(q) -> date:
    """Giorno del menu mostrato nel messaggio su cui è stato premuto Aggiorna."""
    m = re.fullmatch(r"r:(\d{8})", q.data)
    if m:
        return datetime.strptime(m.group(1), "%Y%m%d").date()
    # pulsante di un messaggio vecchio, senza data: uso la data del messaggio
    return q.message.date.astimezone().date()


async def on_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id = update.effective_chat.id
    if not has_accepted(chat_id):
        await q.answer()
        await show_banner(update)
        return
    if q.data == "c":
        await q.answer()
        await safe_edit(q, CHOOSE_TEXT, mense_keyboard())
        return

    # --- Aggiorna
    mid = get_choice(chat_id)
    if mid is None:
        await q.answer()
        await safe_edit(q, CHOOSE_TEXT, mense_keyboard())
        return
    text, kb = await build_view(mid, force=True)
    if shown_day(q) == date.today():
        # stesso giorno: aggiorno lo stesso messaggio
        changed = await safe_edit(q, text, kb)
        await q.answer("Menu aggiornato" if changed else "Il menu è già aggiornato")
    else:
        # giorno diverso: nuovo messaggio, e tolgo i pulsanti dal vecchio
        await q.answer()
        await ctx.bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            pass


# ============================================================ IMPOSTAZIONI ===

def settings_view(chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    user = get_user(chat_id)
    mid = get_choice(chat_id)
    notify = bool(user.get("notify"))
    lines = [
        "⚙️ <b>Impostazioni</b>",
        "",
        f"📍 Mensa: <b>{_esc(MENSE[mid]['name']) if mid else 'non scelta'}</b>",
        f"🔔 Avviso giornaliero: <b>{'attivo' if notify else 'disattivato'}</b>",
        f"🕐 Orario dell'avviso: <b>{DAILY_TIME_STR}</b>",
    ]
    if notify and not mid:
        lines += ["", "⚠️ Per ricevere l'avviso scegli prima una mensa."]
    toggle = "🔕 Disattiva avviso" if notify else f"🔔 Attiva avviso ({DAILY_TIME_STR})"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle, callback_data="n")],
        [InlineKeyboardButton("📍 Cambia mensa", callback_data="c")],
        [InlineKeyboardButton("⬅️ Indietro", callback_data="b")],
    ])
    return "\n".join(lines), kb


async def impostazioni(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not has_accepted(chat_id):
        await show_banner(update)
        return
    text, kb = settings_view(chat_id)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def on_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id = update.effective_chat.id
    if not has_accepted(chat_id):
        await q.answer()
        await show_banner(update)
        return
    if q.data == "n":                      # attiva / disattiva l'avviso
        on = not get_user(chat_id).get("notify")
        update_user(chat_id, notify=on)
        await q.answer(f"Avviso attivato alle {DAILY_TIME_STR}" if on else "Avviso disattivato")
    else:
        await q.answer()
    if q.data == "b":                      # torna al menu
        mid = get_choice(chat_id)
        if mid:
            text, kb = await build_view(mid)
            await safe_edit(q, text, kb)
        else:
            await safe_edit(q, CHOOSE_TEXT, mense_keyboard())
        return
    text, kb = settings_view(chat_id)
    await safe_edit(q, text, kb)


# ========================================================= AVVISO GIORNALIERO ===

def _read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def should_send_now(now: datetime, last_sent: str | None) -> bool:
    """True se è l'ora dell'avviso (o entro NOTIFY_WINDOW dopo) e oggi non è ancora partito."""
    target = datetime.combine(now.date(), DAILY_TIME)
    return last_sent != now.date().isoformat() and target <= now < target + NOTIFY_WINDOW


async def send_daily(app: Application):
    """Manda il menu di oggi a chi ha attivato l'avviso. Un solo download per mensa."""
    today = date.today()
    by_mensa: dict[str, list[int]] = {}
    for chat_key, u in load_users().items():
        if u.get("notify") and u.get("mensa") in MENSE and _accepted(u):
            by_mensa.setdefault(u["mensa"], []).append(int(chat_key))

    sent = skipped = 0
    for mid, chat_ids in by_mensa.items():
        data = await get_menu_data(mid, today)
        if data is None or (data["closed"] and not NOTIFY_IF_CLOSED):
            skipped += len(chat_ids)       # menu non pubblicato o mensa chiusa: niente avviso
            continue
        text, kb = render_view(mid, today, data)
        for chat_id in chat_ids:
            try:
                await app.bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)
                sent += 1
            except Forbidden:              # l'utente ha bloccato il bot
                update_user(chat_id, notify=False)
            except TelegramError as e:
                logging.warning("Avviso non inviato a %s: %s", chat_id, e)
            await asyncio.sleep(0.05)      # resto sotto il limite di invii di Telegram
    logging.info("Avviso giornaliero: %d inviati, %d saltati", sent, skipped)


async def daily_loop(app: Application):
    while True:
        try:
            now = datetime.now()
            state = _read_state()
            if should_send_now(now, state.get("last_daily")):
                _write_state({**state, "last_daily": now.date().isoformat()})  # prima di inviare: niente doppioni
                await send_daily(app)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Errore nell'avviso giornaliero")
        await asyncio.sleep(30)


async def post_init(app: Application):
    app.bot_data["daily_task"] = asyncio.create_task(daily_loop(app))
    logging.info("Avviso giornaliero programmato alle %s", DAILY_TIME_STR)


async def post_shutdown(app: Application):
    task = app.bot_data.get("daily_task")
    if task:
        task.cancel()


def build_app() -> Application:
    app = (Application.builder().token(TOKEN)
           .post_init(post_init).post_shutdown(post_shutdown).build())
    app.add_handler(CommandHandler(["start", "oggi"], show_today))
    app.add_handler(CommandHandler("sede", sede))
    app.add_handler(CommandHandler("impostazioni", impostazioni))
    app.add_handler(CallbackQueryHandler(on_consent, pattern=r"^a:"))
    app.add_handler(CallbackQueryHandler(on_select, pattern=r"^s:"))
    app.add_handler(CallbackQueryHandler(on_action, pattern=r"^(r(:\d{8})?|c)$"))
    app.add_handler(CallbackQueryHandler(on_settings, pattern=r"^(i|n|b)$"))
    return app


def main():
    build_app().run_polling()


if __name__ == "__main__":
    main()