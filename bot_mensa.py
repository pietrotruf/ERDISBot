"""
Bot Telegram - Menu mense ERDIS (solo giorno corrente)

Installazione:
    pip install "python-telegram-bot>=21" requests beautifulsoup4

Avvio:
    token in variabile d'ambiente TELEGRAM_TOKEN oppure in un file .env
    (una riga: TELEGRAM_TOKEN=il_tuo_token), poi:
    python bot_mensa.py

Comandi:
    /start  -> scegli la mensa (la prima volta), poi mostra il menu di oggi
    /oggi   -> menu di oggi della mensa scelta
    /sede   -> cambia mensa

Per aggiungere una mensa: vedi il dizionario MENSE qui sotto.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
from datetime import date, datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes)

logging.basicConfig(level=logging.INFO)

def _load_token() -> str:
    """Legge TELEGRAM_TOKEN dalla variabile d'ambiente oppure dal file .env
    (nella stessa cartella dello script o in quella da cui lanci il bot)."""
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    if not token:
        for env_file in (Path(__file__).with_name(".env"), Path.cwd() / ".env"):
            if env_file.is_file():
                for line in env_file.read_text(encoding="utf-8-sig").splitlines():
                    line = line.strip()
                    if line.startswith("export "):
                        line = line[7:]
                    if line.startswith("TELEGRAM_TOKEN") and "=" in line:
                        token = line.split("=", 1)[1].strip().strip("\"'")
                        break
            if token:
                break
    if not token:
        raise SystemExit("Token mancante: imposta la variabile TELEGRAM_TOKEN "
                         "oppure crea un file .env con TELEGRAM_TOKEN=il_tuo_token")
    return token


TOKEN = _load_token()
USERS_FILE = Path(os.environ.get("USERS_FILE") or Path(__file__).with_name("utenti.json"))  # scelte salvate per chat

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
BANNER_TEXT = "⚠️Attenzione⚠️\nQuesto bot non è affiliato con ERDIS e affini. Controllate sempre che il menù sia giusto anche dal sito. Non mi assumo alcuna responsabilità"

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


def has_accepted(chat_id: int) -> bool:
    u = get_user(chat_id)
    return bool(u.get("accepted_at")) and u.get("banner_version") == BANNER_VERSION


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


def menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Aggiorna", callback_data="r"),
        InlineKeyboardButton("📍 Cambia mensa", callback_data="c"),
    ]])


CHOOSE_TEXT = "Scegli la mensa:"


async def build_text(mensa_id: str) -> str:
    today = date.today()
    page = await asyncio.to_thread(fetch_menu_html, mensa_id, today)
    parser = PARSERS[MENSE[mensa_id]["parser"]]
    data = parser(page) if page else None
    return format_message(MENSE[mensa_id]["name"], today, data)


async def safe_edit(q, text, kb):
    try:
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
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
    await update.message.reply_text(await build_text(mid), parse_mode="HTML",
                                    reply_markup=menu_keyboard())


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
            await safe_edit(q, await build_text(mid), menu_keyboard())
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
    await safe_edit(q, await build_text(mid), menu_keyboard())


async def on_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = update.effective_chat.id
    if not has_accepted(chat_id):
        await show_banner(update)
        return
    if q.data == "c":
        await safe_edit(q, CHOOSE_TEXT, mense_keyboard())
        return
    mid = get_choice(chat_id)
    if mid is None:
        await safe_edit(q, CHOOSE_TEXT, mense_keyboard())
    else:
        await safe_edit(q, await build_text(mid), menu_keyboard())


def build_app() -> Application:
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler(["start", "oggi"], show_today))
    app.add_handler(CommandHandler("sede", sede))
    app.add_handler(CallbackQueryHandler(on_consent, pattern=r"^a:"))
    app.add_handler(CallbackQueryHandler(on_select, pattern=r"^s:"))
    app.add_handler(CallbackQueryHandler(on_action, pattern=r"^(r|c)$"))
    return app


def main():
    build_app().run_polling()


if __name__ == "__main__":
    main()
