"""
bot.py

Multi-Sport Telegram Bot

Features:
- Sport selection on /start
- ⚽ Football: existing league / period flow
- 🏀 Basketball: NBA, NBA preseason, and EuroLeague
- 🏒 Hockey: NHL and NHL preseason
- 🎾 Tennis: ATP/WTA top tournaments
- 🏎️ Formula 1: current-season race schedule
- Back navigation
- Refresh

Status handling: this file never re-derives its own status alias sets. All
match statuses are one of api.py's canonical codes (NS, HT, LIVE, FT, PST,
SUSP, CANC, UNKNOWN) and every display decision goes through
api.is_pre_match / api.is_live_family / api.is_finished / api.is_postponed /
api.is_cancelled / api.is_suspended / api.status_display_label. Keeping that
logic in one place (api.py) is what stops the UI from drifting out of sync
with what the live-refresh code actually decided.
"""

import asyncio
import io
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo


# ============================================================
# TERMINAL ERROR / DEBUG LOG
# ============================================================
# Everything printed to the terminal is also saved to
# matchradar_terminal.log next to bot.py.
# This includes API prints, logging output, tracebacks and errors.
# The file is reset on every bot start, so it always contains the latest run.
_TERMINAL_LOG_PATH = Path(__file__).with_name("matchradar_terminal.log")


class _TeeStream:
    def __init__(self, original, log_file):
        self.original = original
        self.log_file = log_file

    def write(self, data):
        try:
            self.original.write(data)
            self.original.flush()
        except Exception:
            pass
        try:
            self.log_file.write(data)
            self.log_file.flush()
        except Exception:
            pass

    def flush(self):
        try:
            self.original.flush()
        except Exception:
            pass
        try:
            self.log_file.flush()
        except Exception:
            pass

    def isatty(self):
        try:
            return self.original.isatty()
        except Exception:
            return False


_terminal_log_file = open(
    _TERMINAL_LOG_PATH,
    "w",
    encoding="utf-8",
    buffering=1,
)
_terminal_log_file.write("=" * 80 + "\n")
_terminal_log_file.write("MATCHRADAR TERMINAL LOG\n")
_terminal_log_file.write("If something breaks, send this file to ChatGPT.\n")
_terminal_log_file.write("=" * 80 + "\n")
_terminal_log_file.flush()

sys.stdout = _TeeStream(sys.stdout, _terminal_log_file)
sys.stderr = _TeeStream(sys.stderr, _terminal_log_file)


def _log_uncaught_exception(exc_type, exc_value, exc_traceback):
    # Keep the normal traceback in the terminal AND in the log file.
    if exc_type is KeyboardInterrupt:
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    sys.__excepthook__(exc_type, exc_value, exc_traceback)


sys.excepthook = _log_uncaught_exception

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    InputMediaPhoto,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)


import api
from config import BOT_TOKEN

# ============================================================
# Logging
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# httpx logs full Telegram Bot API URLs, which contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)
logger.info("Bookmaker odds: %s (API key loaded: %s)", api.ODDS_BOOKMAKER, bool(api.ODDS_API_KEY))


# ============================================================
# Timezone
# ============================================================

# The bot ALWAYS displays match times in Yerevan time.
YEREVAN_TZ = ZoneInfo("Asia/Yerevan")
UTC_TZ = ZoneInfo("UTC")


def _parse_match_datetime(match: dict):
    """Get the kickoff datetime and convert it to Yerevan time.

    We prefer the original UTC datetime from football-data.org, so different
    competitions/matches are never rendered in two different timezones.
    """

    raw_candidates = [
        match.get("utcDate"),
        match.get("utc_date"),
        match.get("date_utc"),
        match.get("kickoff_utc"),
        match.get("kickoff"),
        match.get("datetime"),
        match.get("date_time"),
    ]

    date_value = str(match.get("date", "")).strip()
    time_value = str(match.get("time", "")).strip()

    raw_candidates.extend([
        f"{date_value} {time_value}".strip(),
        date_value if "T" in date_value or "Z" in date_value else None,
        time_value if "T" in time_value or "Z" in time_value else None,
    ])

    for raw in raw_candidates:
        if not raw:
            continue
        try:
            value = str(raw).strip()
            if not value:
                continue
            if value.endswith("Z"):
                value = value[:-1] + "+00:00"

            dt = datetime.fromisoformat(value)

            if dt.tzinfo is not None:
                return dt.astimezone(YEREVAN_TZ)

            if raw in raw_candidates[:7] and raw not in (
                f"{date_value} {time_value}".strip(),
            ):
                return dt.replace(tzinfo=UTC_TZ).astimezone(YEREVAN_TZ)

            # Plain date/time values from the API are treated as UTC too.
            return dt.replace(tzinfo=UTC_TZ).astimezone(YEREVAN_TZ)
        except (TypeError, ValueError):
            continue

    return None


def _yerevan_date_time_text(match: dict):
    """Return display date/time in one consistent Yerevan format."""

    dt = _parse_match_datetime(match)
    if dt is not None:
        return dt.strftime("%d %b"), dt.strftime("%H:%M")

    date_text = str(match.get("date", "")).strip()
    time_text = str(match.get("time", "")).strip()
    if "T" in time_text or "Z" in time_text:
        cleaned = time_text.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(cleaned)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC_TZ)
            dt = dt.astimezone(YEREVAN_TZ)
            time_text = dt.strftime("%H:%M")
            date_text = dt.strftime("%d %b")
        except ValueError:
            pass

    return date_text, time_text


# ============================================================
# Settings
# ============================================================

PERIOD_LABELS = {
    "today": "📅 Today's matches",
    "tomorrow": "📅 Tomorrow's matches",
    "week": "🗓 This week's matches",
    "month": "📆 This month's matches",
}

SEND_DELAY_SECONDS = 0.2


# ============================================================
# Fonts
# ============================================================

def _get_font(size: int, bold: bool = False):
    possible_fonts = (
        ["C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/segoeuib.ttf"]
        if bold else
        ["C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/segoeui.ttf"]
    )

    for font_path in possible_fonts:
        try:
            return ImageFont.truetype(font_path, size)
        except OSError:
            pass

    return ImageFont.load_default()


# Tennis APIs return three-letter IOC country codes (for example FRA or POL).
# Use flag emoji in fallback Telegram text and actual flag images in PNG cards.
_TENNIS_COUNTRY_ISO2 = {
    "AFG": "AF", "ALB": "AL", "ALG": "DZ", "AND": "AD", "ANG": "AO",
    "ANT": "AG", "ARG": "AR", "ARM": "AM", "ARU": "AW", "AUS": "AU",
    "AUT": "AT", "AZE": "AZ", "BAH": "BS", "BAN": "BD", "BAR": "BB",
    "BEL": "BE", "BEN": "BJ", "BER": "BM", "BHU": "BT", "BIH": "BA",
    "BIZ": "BZ", "BLR": "BY", "BOL": "BO", "BOT": "BW", "BRA": "BR",
    "BRN": "BH", "BUL": "BG", "BUR": "BF", "CAM": "KH", "CAN": "CA",
    "CHI": "CL", "CHN": "CN", "CIV": "CI", "CMR": "CM", "COL": "CO",
    "CRC": "CR", "CRO": "HR", "CUB": "CU", "CYP": "CY", "CZE": "CZ",
    "DEN": "DK", "DOM": "DO", "ECU": "EC", "EGY": "EG", "ESA": "SV",
    "ESP": "ES", "EST": "EE", "FIN": "FI", "FRA": "FR", "GBR": "GB",
    "GEO": "GE", "GER": "DE", "GHA": "GH", "GRE": "GR", "GUA": "GT",
    "HKG": "HK", "HON": "HN", "HUN": "HU", "IND": "IN", "INA": "ID",
    "IRI": "IR", "IRL": "IE", "ISL": "IS", "ISR": "IL", "ITA": "IT",
    "JAM": "JM", "JPN": "JP", "KAZ": "KZ", "KEN": "KE", "KGZ": "KG",
    "KOR": "KR", "KOS": "XK", "KSA": "SA", "KUW": "KW", "LAT": "LV",
    "LBN": "LB", "LIB": "LB", "LIE": "LI", "LTU": "LT", "LUX": "LU",
    "MAD": "MG", "MAR": "MA", "MAS": "MY", "MDA": "MD", "MEX": "MX",
    "MKD": "MK", "MLT": "MT", "MNE": "ME", "MON": "MC", "MRI": "MU",
    "NED": "NL", "NEP": "NP", "NGR": "NG", "NOR": "NO", "NZL": "NZ",
    "PAK": "PK", "PAN": "PA", "PAR": "PY", "PER": "PE", "PHI": "PH",
    "POL": "PL", "POR": "PT", "PUR": "PR", "QAT": "QA", "ROU": "RO",
    "RSA": "ZA", "RUS": "RU", "SAM": "WS", "SEN": "SN", "SEY": "SC",
    "SGP": "SG", "SLO": "SI", "SMR": "SM", "SRB": "RS", "SRI": "LK",
    "SVK": "SK", "SWE": "SE", "SUI": "CH", "SYR": "SY", "THA": "TH",
    "TJK": "TJ", "TKM": "TM", "TPE": "TW", "TUN": "TN", "TUR": "TR",
    "UAE": "AE", "UGA": "UG", "UKR": "UA", "URU": "UY", "USA": "US",
    "UZB": "UZ", "VEN": "VE", "VIE": "VN", "ZAM": "ZM", "ZIM": "ZW",
    # ISO-3166 alpha-3 aliases some roster APIs return instead of IOC codes.
    "DEU": "DE", "NLD": "NL", "CHE": "CH", "GRC": "GR", "HRV": "HR",
    "SVN": "SI", "PRT": "PT", "URY": "UY", "ZAF": "ZA", "TWN": "TW",
    "CHL": "CL", "MYS": "MY", "IDN": "ID", "PHL": "PH", "ARE": "AE",
    "SAU": "SA", "IRN": "IR", "VNM": "VN", "ZWE": "ZW", "ZMB": "ZM",
}

_TENNIS_COUNTRY_NAME_CODES = {
    "UNITED STATES": "USA", "UNITED STATES OF AMERICA": "USA",
    "UNITED KINGDOM": "GBR", "GREAT BRITAIN": "GBR", "ENGLAND": "GBR",
    "CZECH REPUBLIC": "CZE", "NETHERLANDS": "NED", "SWITZERLAND": "SUI",
    "SOUTH KOREA": "KOR", "KOREA, REPUBLIC OF": "KOR", "TAIWAN": "TPE",
    "CHINESE TAIPEI": "TPE", "RUSSIA": "RUS", "TÜRKIYE": "TUR",
    "TURKEY": "TUR", "CHINA": "CHN", "FRANCE": "FRA", "POLAND": "POL",
    "GERMANY": "GER", "SPAIN": "ESP", "ITALY": "ITA", "SERBIA": "SRB",
    "AUSTRALIA": "AUS", "CANADA": "CAN", "JAPAN": "JPN", "GREECE": "GRE",
    "BRAZIL": "BRA", "ARGENTINA": "ARG", "UK": "GBR", "USA": "USA",
    "CZECHIA": "CZE", "SLOVAKIA": "SVK", "SLOVENIA": "SLO",
    "CROATIA": "CRO", "BOSNIA AND HERZEGOVINA": "BIH",
    "BOSNIA & HERZEGOVINA": "BIH", "MONTENEGRO": "MNE",
    "NORTH MACEDONIA": "MKD", "MACEDONIA": "MKD", "MOLDOVA": "MDA",
    "HONG KONG": "HKG", "HUNGARY": "HUN", "UKRAINE": "UKR",
    "UZBEKISTAN": "UZB", "TUNISIA": "TUN", "MEXICO": "MEX",
    "PHILIPPINES": "PHI", "PUERTO RICO": "PUR", "CHILE": "CHI",
    "SOUTH AFRICA": "RSA", "NEW ZEALAND": "NZL", "ROMANIA": "ROU",
    "BULGARIA": "BUL", "INDIA": "IND", "ISRAEL": "ISR", "KAZAKHSTAN": "KAZ",
    "ARMENIA": "ARM", "AUSTRIA": "AUT", "BELGIUM": "BEL",
    "COLOMBIA": "COL", "DENMARK": "DEN", "INDONESIA": "INA",
    "LATVIA": "LAT", "THAILAND": "THA",
}


def _tennis_country_flag(country_code: str | None) -> str:
    iso2 = _tennis_country_iso2(country_code)
    if not iso2:
        return ""
    return "".join(chr(0x1F1E6 + ord(char) - ord("A")) for char in iso2)


def _tennis_country_iso2(country_code: str | None) -> str:
    raw = " ".join(str(country_code or "").strip().upper().replace(".", "").split())
    if not raw:
        return ""
    raw = _TENNIS_COUNTRY_NAME_CODES.get(raw, raw)
    iso2 = _TENNIS_COUNTRY_ISO2.get(raw, raw)
    if len(iso2) == 2 and iso2.isalpha():
        return iso2
    # Some providers return labels such as "France (FRA)" or "USA - United
    # States" instead of a single IOC/ISO code. Recover a known code token.
    for token in reversed(raw.replace("-", " ").replace("/", " ").split()):
        candidate = _TENNIS_COUNTRY_ISO2.get(token, token)
        if len(candidate) == 2 and candidate.isalpha():
            return candidate
    return ""


def _format_tennis_player_name(country_code: str | None, player_name: str) -> str:
    name = str(player_name or "Player").strip()
    flag = _tennis_country_flag(country_code)
    return f"{flag} {name}" if flag else name


def _center_tennis_player_label(
    draw,
    country_code: str | None,
    player_name: str,
    center_x: int,
    y: int,
    max_width: int,
    size: int = 26,
    prefix: str = "",
    suffix: str = "",
    canvas: Image.Image | None = None,
) -> None:
    """Draw a centered player label with a small flag image and no country code."""
    name = str(player_name or "Player").strip()
    iso2 = _tennis_country_iso2(country_code)
    flag_emoji = _tennis_country_flag(country_code)
    flag_image = (
        _download_asset(f"https://flagcdn.com/w40/{iso2.lower()}.png")
        if iso2 else None
    )
    flag_font = None
    if flag_emoji and flag_image is None and canvas is not None:
        try:
            flag_font = ImageFont.truetype("C:/Windows/Fonts/seguiemj.ttf", 20)
        except OSError:
            flag_font = _get_font(18)
    show_flag = flag_image is not None or flag_font is not None
    flag_width, flag_height = (28, 18) if show_flag and canvas is not None else (0, 0)
    gap = 7 if flag_width else 0
    for font_size in range(size, 15, -1):
        name_font = _get_font(font_size, bold=True)
        prefix_bbox = draw.textbbox((0, 0), prefix, font=name_font) if prefix else (0, 0, 0, 0)
        name_bbox = draw.textbbox((0, 0), name, font=name_font)
        suffix_bbox = draw.textbbox((0, 0), suffix, font=name_font) if suffix else (0, 0, 0, 0)
        prefix_width = prefix_bbox[2] - prefix_bbox[0]
        name_width = name_bbox[2] - name_bbox[0]
        suffix_width = suffix_bbox[2] - suffix_bbox[0]
        total_width = prefix_width + (flag_width + gap if flag_width else 0) + name_width + suffix_width
        if total_width <= max_width or font_size == 16:
            break

    x = center_x - total_width / 2
    if prefix:
        draw.text((x, y), prefix, font=name_font, fill=(244, 248, 247))
        x += prefix_width
    if flag_width and canvas is not None:
        flag_x = int(round(x))
        if flag_image is not None:
            flag_icon = ImageOps.contain(
                flag_image, (flag_width - 2, flag_height - 2), method=Image.Resampling.LANCZOS
            )
            flag_y = int(y + max(2, (font_size - flag_icon.height) // 2))
            draw.rounded_rectangle(
                (flag_x - 1, flag_y - 1, flag_x + flag_icon.width, flag_y + flag_icon.height),
                radius=2,
                fill=(244, 248, 247),
            )
            canvas.paste(
                flag_icon,
                (flag_x, flag_y),
                flag_icon if flag_icon.mode == "RGBA" else None,
            )
        elif flag_emoji:
            try:
                draw.text((flag_x, y - 1), flag_emoji, font=flag_font, embedded_color=True)
            except Exception:
                draw.text((flag_x, y - 1), flag_emoji, font=flag_font, fill=(245, 249, 248))
        x += flag_width + gap
    draw.text((x, y), name, font=name_font, fill=(244, 248, 247))
    x += name_width
    if suffix:
        draw.text((x, y), suffix, font=name_font, fill=(244, 248, 247))


# ============================================================
# /start
# ============================================================

SPORT_LABELS = {
    "tennis": "🎾 Tennis",
    "basketball": "🏀 Basketball",
    "hockey": "🏒 Hockey",
    "f1": "🏎️ Formula 1",
    "football": "⚽ Football",
}


def _sport_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(SPORT_LABELS["tennis"], callback_data="sport:tennis"),
            InlineKeyboardButton(SPORT_LABELS["basketball"], callback_data="sport:basketball"),
        ],
        [
            InlineKeyboardButton(SPORT_LABELS["hockey"], callback_data="sport:hockey"),
            InlineKeyboardButton(SPORT_LABELS["f1"], callback_data="sport:f1"),
        ],
        [InlineKeyboardButton(SPORT_LABELS["football"], callback_data="sport:football")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    context.user_data.clear()

    await update.message.reply_text(
        "🏆 MySportInfo Bot\n\n"
        "⚽ What sport would you like to follow?",
        reply_markup=_sport_keyboard(),
    )


# ============================================================
# Period selection
# ============================================================

def _period_keyboard() -> InlineKeyboardMarkup:

    keyboard = [
        [InlineKeyboardButton(label, callback_data=f"period:{key}")]
        for key, label in PERIOD_LABELS.items()
    ]
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="back_to_leagues")])
    return InlineKeyboardMarkup(keyboard)


async def handle_period_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    query = update.callback_query
    await query.answer()

    period = query.data.split(":")[1]

    context.user_data["period"] = period
    context.user_data["mode"] = "league"
    context.user_data["screen"] = "matches"

    await query.edit_message_text("⏳ Fetching matches...")
    await show_matches(query, context)


# ============================================================
# League keyboard
# ============================================================

# UEFA competitions use the EU flag instead of custom Telegram icons.
COMPETITION_BUTTON_FLAGS = {
    "CL": "🇪🇺",
    "EL": "🇪🇺",
    "ECL": "🇪🇺",
}


def _league_keyboard() -> InlineKeyboardMarkup:

    keyboard = []
    for key, league in api.LEAGUES.items():
        league_id = str(league.get("id", "")).upper()
        flag = COMPETITION_BUTTON_FLAGS.get(league_id, league.get("flag", "⚽"))
        button = InlineKeyboardButton(
            f"{flag} {league['name']}",
            callback_data=f"league:{key}",
        )
        keyboard.append([button])

    keyboard.append([InlineKeyboardButton("🌍 All Top Leagues", callback_data="league:all")])
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="back_to_start")])
    return InlineKeyboardMarkup(keyboard)


# ============================================================
# Multi-sport selection + API flow
# ============================================================

SPORT_PERIOD_LABELS = {
    "today": "📅 Today",
    "tomorrow": "📅 Tomorrow",
    "week": "🗓 This Week",
    "month": "📆 This Month",
}

SPORT_VARIANTS = {
    "basketball": (
        ("nba", "🏀 NBA"),
        ("nba_preseason", "🏀 NBA Preseason"),
        ("euroleague", "🏆 EuroLeague"),
    ),
    "hockey": (
        ("nhl", "🏒 NHL"),
        ("nhl_preseason", "🏒 NHL Preseason"),
    ),
}

SPORT_VARIANT_LABELS = {
    variant: label
    for options in SPORT_VARIANTS.values()
    for variant, label in options
}


def _tennis_tour_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎾 ATP", callback_data="tennis_tour:atp"),
            InlineKeyboardButton("🎾 WTA", callback_data="tennis_tour:wta"),
        ],
        [InlineKeyboardButton("⬅️ Sports", callback_data="back_to_start")],
    ])


def _sport_variant_keyboard(sport: str) -> InlineKeyboardMarkup:
    options = SPORT_VARIANTS.get(sport, ())
    keyboard = [
        [InlineKeyboardButton(label, callback_data=f"sport_variant:{sport}:{variant}")]
        for variant, label in options
    ]
    keyboard.append([InlineKeyboardButton("⬅️ Sports", callback_data="back_to_start")])
    return InlineKeyboardMarkup(keyboard)


def _sport_variant_title(sport: str) -> str:
    names = {"basketball": "🏀 Basketball", "hockey": "🏒 Hockey"}
    return f"{names.get(sport, SPORT_LABELS.get(sport, sport))}\n\n🏆 Choose competition"


def _sport_period_keyboard(sport: str) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton(label, callback_data=f"sport_period:{sport}:{period}")]
        for period, label in SPORT_PERIOD_LABELS.items()
    ]
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="back_to_previous")])
    return InlineKeyboardMarkup(keyboard)


def _sport_nav_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Back", callback_data="back_to_previous"),
        InlineKeyboardButton("🔄 Refresh", callback_data="refresh"),
    ]])


def _sport_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Back", callback_data="back_to_previous"),
        InlineKeyboardButton("🏠 Sports", callback_data="back_to_start"),
    ]])


def _sport_display_label(sport: str, sport_variant: str | None = None) -> str:
    if sport_variant:
        return SPORT_VARIANT_LABELS.get(
            sport_variant, SPORT_LABELS.get(sport, sport)
        )
    names = {
        "basketball": "🏀 NBA",
        "hockey": "🏒 NHL",
        "tennis": "🎾 Tennis",
    }
    return names.get(sport, SPORT_LABELS.get(sport, sport))


def _sport_period_title(
    sport: str,
    period: str,
    sport_variant: str | None = None,
) -> str:
    return f"{_sport_display_label(sport, sport_variant)}\n\n📅 Choose a period"


async def _sport_messages(
    sport: str,
    period: str | None = None,
    tennis_tour: str | None = None,
    sport_variant: str | None = None,
):
    """Fetch normalized non-football event objects from api.py."""
    if sport == "f1":
        return await asyncio.to_thread(api.get_sport_messages, sport)

    period = period or "today"
    date_from, date_to = api.get_date_range(period)
    if sport == "tennis":
        if period == "today":
            # Football keeps its wider UTC boundary window and filters it
            # afterwards; tennis cards should never pull yesterday's stale
            # odds-only fixtures into today's list.
            local_today = datetime.now(YEREVAN_TZ).date().isoformat()
            date_from = date_to = local_today
        return await asyncio.to_thread(
            api.get_tennis_messages, date_from, date_to, tennis_tour
        )
    if sport == "basketball":
        fetcher = {
            "nba_preseason": api.get_nba_preseason_messages,
            "euroleague": api.get_euroleague_messages,
        }.get(sport_variant, api.get_nba_messages)
        return await asyncio.to_thread(fetcher, date_from, date_to)
    if sport == "hockey" and sport_variant == "nhl_preseason":
        return await asyncio.to_thread(
            api.get_nhl_preseason_messages, date_from, date_to
        )
    return await asyncio.to_thread(api.get_sport_messages, sport, date_from, date_to)


async def handle_sport_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    sport = query.data.split(":", 1)[1]
    context.user_data.clear()
    context.user_data["sport"] = sport
    context.user_data["mode"] = f"sport_{sport}"

    if sport == "football":
        context.user_data["mode"] = "league"
        context.user_data["screen"] = "league_selection"
        await query.edit_message_text(
            "⚽ Football\n\n🏆 Choose a league",
            reply_markup=_league_keyboard(),
        )
        return

    if sport == "f1":
        await _show_sport_matches(query, context, sport, context.user_data.get("period"))
        return

    if sport == "tennis":
        context.user_data["screen"] = "tennis_tour_selection"
        await query.edit_message_text(
            "🎾 Tennis\n\n🏆 Choose tour",
            reply_markup=_tennis_tour_keyboard(),
        )
        return

    if sport in SPORT_VARIANTS:
        context.user_data["screen"] = "sport_variant_selection"
        await query.edit_message_text(
            _sport_variant_title(sport),
            reply_markup=_sport_variant_keyboard(sport),
        )
        return

    context.user_data["screen"] = "sport_period_selection"
    await query.edit_message_text(
        _sport_period_title(sport, "today"),
        reply_markup=_sport_period_keyboard(sport),
    )


async def handle_sport_variant_selection(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    _, sport, variant = query.data.split(":", 2)
    allowed = {key for key, _ in SPORT_VARIANTS.get(sport, ())}
    if variant not in allowed:
        await query.answer("Unknown competition.", show_alert=True)
        return

    context.user_data["sport"] = sport
    context.user_data["sport_variant"] = variant
    context.user_data["mode"] = f"sport_{sport}"
    context.user_data["screen"] = "sport_period_selection"
    await query.edit_message_text(
        _sport_period_title(sport, "today", variant),
        reply_markup=_sport_period_keyboard(sport),
    )


async def handle_tennis_tour_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    tour = query.data.split(":", 1)[1].lower()
    if tour not in {"atp", "wta"}:
        await query.answer("Unknown tennis tour.", show_alert=True)
        return

    context.user_data["sport"] = "tennis"
    context.user_data["tennis_tour"] = tour
    context.user_data["mode"] = "sport_tennis"
    context.user_data["screen"] = "sport_period_selection"

    tour_label = tour.upper()
    await query.edit_message_text(
        f"🎾 Tennis — {tour_label}\n\n📅 Choose a period",
        reply_markup=_sport_period_keyboard("tennis"),
    )


async def handle_sport_period_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    _, sport, period = query.data.split(":", 2)
    context.user_data["sport"] = sport
    context.user_data["mode"] = f"sport_{sport}"
    context.user_data["period"] = period
    context.user_data["screen"] = "sport_matches"

    await _show_sport_matches(
        query,
        context,
        sport,
        period,
        context.user_data.get("tennis_tour"),
        context.user_data.get("sport_variant"),
    )


async def _show_sport_matches(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    sport: str,
    period: str | None = None,
    tennis_tour: str | None = None,
    sport_variant: str | None = None,
) -> None:
    await _clear_previous_match_messages(query, context)

    context.user_data["sport"] = sport
    context.user_data["mode"] = f"sport_{sport}"
    context.user_data["screen"] = "sport_matches"
    if period and sport != "f1":
        context.user_data["period"] = period
    if sport == "tennis" and tennis_tour:
        context.user_data["tennis_tour"] = tennis_tour.lower()
    if sport_variant:
        context.user_data["sport_variant"] = sport_variant
    sport_variant = context.user_data.get("sport_variant")
    sport_label = _sport_display_label(sport, sport_variant)

    if sport == "f1":
        loading_text = "🏎️ Formula 1\n\n⏳ Loading current Grand Prix..."
    else:
        period = period or context.user_data.get("period", "today")
        period_name = SPORT_PERIOD_LABELS.get(period, "📅 Today")
        loading_text = f"{sport_label}\n\n{period_name}\n\n⏳ Fetching games..."

    await _edit_navigation_message(
        query,
        context,
        loading_text,
        reply_markup=_sport_nav_keyboard(),
    )

    fetch_started = time.perf_counter()
    try:
        items = await _sport_messages(
            sport,
            period,
            context.user_data.get("tennis_tour"),
            sport_variant,
        )
        logger.info(
            "[SPORT] %s %s returned %d event(s) in %.1fs",
            sport,
            period or "current",
            len(items),
            time.perf_counter() - fetch_started,
        )
    except Exception as exc:
        logger.exception("Could not fetch %s data", sport)
        await _edit_navigation_message(
            query,
            context,
            f"{sport_label}\n\n⚠️ Could not fetch data.\n\n{exc}",
            reply_markup=_sport_nav_keyboard(),
        )
        return

    if not items:
        if sport == "f1":
            empty_title = "🏎️ Formula 1"
        else:
            empty_title = f"{sport_label}\n\n{SPORT_PERIOD_LABELS.get(period or 'today', '📅 Today')}"
        await _edit_navigation_message(
            query,
            context,
            f"{empty_title}\n\n😔 No events found.",
            reply_markup=_sport_nav_keyboard(),
        )
        context.user_data["match_message_ids"] = []
        return

    if sport == "f1":
        item_label = "🏎️ Current Grand Prix"
    elif sport == "tennis":
        tour_label = str(context.user_data.get("tennis_tour") or "ATP").upper()
        item_label = f"🎾 Tennis — {tour_label} • {SPORT_PERIOD_LABELS.get(period or 'today', '📅 Today')}"
    else:
        item_label = f"{sport_label} • {SPORT_PERIOD_LABELS.get(period or 'today', '📅 Today')}"

    await _edit_navigation_message(
        query,
        context,
        f"{item_label}\n\n📊 {len(items)} event(s)",
        reply_markup=_sport_nav_keyboard(),
    )

    sent_ids = []
    chat_id = query.message.chat_id

    for index, (fallback_text, item) in enumerate(items):
        keyboard = _sport_back_keyboard() if index == len(items) - 1 else None
        try:
            card_started = time.perf_counter()
            card = await asyncio.to_thread(_create_sport_card, item)
            card_elapsed = time.perf_counter() - card_started
            if card_elapsed >= 2.5:
                logger.warning(
                    "[SPORT] %s card %d/%d took %.1fs to render",
                    sport,
                    index + 1,
                    len(items),
                    card_elapsed,
                )
            message = await context.bot.send_photo(
                chat_id=chat_id,
                photo=InputFile(card, filename="sport_card.png"),
                reply_markup=keyboard,
            )
            sent_ids.append(message.message_id)
        except Exception:
            logger.exception("Could not create/send %s visual card", sport)
            try:
                message = await context.bot.send_message(
                    chat_id=chat_id,
                    text=str(fallback_text or _sport_fallback_text(item)),
                    reply_markup=keyboard,
                )
                sent_ids.append(message.message_id)
            except Exception:
                logger.exception("Could not send %s fallback", sport)
        await asyncio.sleep(SEND_DELAY_SECONDS)

    context.user_data["match_message_ids"] = sent_ids
    context.user_data["chat_id"] = chat_id


# ============================================================
# League selection
# ============================================================

async def handle_league_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    query = update.callback_query
    await query.answer()

    league_key = query.data.split(":")[1]

    context.user_data["league_key"] = league_key
    context.user_data["mode"] = "league"
    context.user_data["screen"] = "period_selection"

    await query.edit_message_text("📅 Choose a period", reply_markup=_period_keyboard())


async def handle_back_to_leagues(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    query = update.callback_query
    await query.answer()

    context.user_data["screen"] = "league_selection"
    await query.edit_message_text("⚽ Football\n\n🏆 Choose a league", reply_markup=_league_keyboard())


# ============================================================
# 🔥 Top Matches
# ============================================================

async def handle_top_matches(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    query = update.callback_query
    await query.answer()

    context.user_data["mode"] = "top"
    context.user_data["screen"] = "matches"

    await query.edit_message_text("⏳ Fetching today's top matches...")
    await show_matches(query, context)


# ============================================================
# Show matches
# ============================================================

async def show_matches(query, context: ContextTypes.DEFAULT_TYPE) -> None:

    await _clear_previous_match_messages(query, context)

    mode = context.user_data.get("mode", "league")

    try:
        if mode == "top":
            date_from, date_to = api.get_date_range("today")
            matches = api.get_top_matches(date_from, date_to)
            matches = api.filter_matches_for_today(matches)
            title = "🔥 Top Matches Today"
        else:
            period = context.user_data.get("period", "today")
            league_key = context.user_data.get("league_key", "all")
            date_from, date_to = api.get_date_range(period)

            if league_key == "all":
                matches = api.get_fixtures_all_leagues(date_from, date_to)
            else:
                league = api.LEAGUES[league_key]
                matches = api.get_fixtures_by_date_range(league["id"], date_from, date_to)

            if period == "today":
                matches = api.filter_matches_for_today(matches)

            period_label = PERIOD_LABELS.get(period, period)
            if " " in period_label:
                period_label = period_label.split(" ", 1)[1]

            league_label = "All Top Leagues" if league_key == "all" else api.LEAGUES[league_key]["name"]
            title = f"🏆 {league_label} — {period_label}"

        # Odds are optional: if unavailable, normal match cards still show.
        try:
            matches = api.enrich_matches_with_odds(matches)
        except api.OddsAPIError:
            logger.exception("Could not fetch bookmaker odds")

    except api.FootballAPIError as exc:
        await query.edit_message_text(
            f"⚠️ Could not fetch matches:\n\n{exc}", reply_markup=_matches_nav_keyboard(),
        )
        return

    except Exception:
        logger.exception("Unexpected error while fetching matches")
        await query.edit_message_text(
            "⚠️ Something went wrong while fetching matches.", reply_markup=_matches_nav_keyboard(),
        )
        return

    if not matches:
        await query.edit_message_text(
            f"{title}\n\n😔 No matches found for this selection.",
            reply_markup=_matches_nav_keyboard(),
        )
        return

    total_matches = len(matches)
    matches_to_show = matches  # no display limit

    header_text = f"{title}\n\n⚽ Found {total_matches} match(es)."

    await _edit_navigation_message(query, context, header_text, reply_markup=_matches_nav_keyboard())

    context.user_data.setdefault("header_message_id", query.message.message_id)

    sent_message_ids = []
    chat_id = query.message.chat_id

    for index, match in enumerate(matches_to_show):

        is_last_match = index == len(matches_to_show) - 1
        bottom_keyboard = _bottom_back_keyboard() if is_last_match else None

        # Always derive the displayed kickoff through the same UTC ->
        # Asia/Yerevan conversion, on every fetch including refresh.
        local_date, local_time = _yerevan_date_time_text(match)
        match = dict(match)
        match["date"] = local_date
        match["time"] = local_time

        message_id = await _send_compact_match_card(context, chat_id, match, reply_markup=bottom_keyboard)

        if message_id is not None:
            sent_message_ids.append(message_id)

        await asyncio.sleep(SEND_DELAY_SECONDS)

    context.user_data["match_message_ids"] = sent_message_ids
    context.user_data["chat_id"] = chat_id
    context.user_data["live_match_snapshot"] = matches_to_show
    context.user_data["live_card_message_ids"] = sent_message_ids
    await _start_live_update_loop(context)


# ============================================================
# Create compact scoreboard image
# ============================================================

def _force_stale_live_status(match: dict) -> bool:
    """Bot-side final safety net for a stale LIVE card.

    This intentionally runs independently of the provider status. If a card
    is still LIVE/HT but its kickoff was more than 180 minutes ago, mark it
    DELAYED immediately. This protects the UI even if api.py is stale or a
    provider status check is unavailable.
    """
    status = str(match.get("status_short", "")).strip().upper()
    if status not in {"LIVE", "HT"}:
        return False

    kickoff = _parse_match_datetime(match)
    if kickoff is None:
        return False

    now_utc = datetime.now(UTC_TZ)
    kickoff_utc = kickoff.astimezone(UTC_TZ)
    minutes = (now_utc - kickoff_utc).total_seconds() / 60

    if minutes <= 180:
        return False

    match["status_short"] = "PST"
    match["minute"] = None
    match["elapsed"] = None
    logger.warning(
        "[LIVE] BOT STALE GUARD: %s vs %s is %.0f min past kickoff -> DELAYED",
        match.get("home_team"), match.get("away_team"), minutes,
    )
    return True


def _create_match_card(match: dict) -> io.BytesIO:
    """Create a larger, phone-readable premium football scoreboard card."""

    width = 900
    height = 500

    image = Image.new("RGB", (width, height))
    pixels = image.load()

    top = (6, 17, 23)
    bottom = (5, 34, 29)

    for y in range(height):
        t = y / max(height - 1, 1)
        r = int(top[0] * (1 - t) + bottom[0] * t)
        g = int(top[1] * (1 - t) + bottom[1] * t)
        b = int(top[2] * (1 - t) + bottom[2] * t)
        for x in range(width):
            edge = abs(x - width / 2) / (width / 2)
            glow = int(4 * (1 - edge))
            pixels[x, y] = (r + glow, g + glow, b + glow)

    draw = ImageDraw.Draw(image)

    draw.rounded_rectangle(
        (4, 4, width - 5, height - 5), radius=24, fill=(7, 24, 28), outline=(42, 67, 70), width=2,
    )
    draw.rounded_rectangle((26, 15, width - 26, 18), radius=2, fill=(24, 174, 105))

    league_name = str(match.get("league_name", "Football"))

    home_name = api.normalize_team_display_name(match.get("home_team", "Home"))
    away_name = api.normalize_team_display_name(match.get("away_team", "Away"))

    # Never use raw match["date"] / match["time"] directly — one timezone
    # only, Asia/Yerevan.
    date_text, time_text = _yerevan_date_time_text(match)
    _force_stale_live_status(match)
    status_short = str(match.get("status_short", "NS")).strip().upper()

    league_font = _get_font(24, bold=True)
    meta_font = _get_font(19, bold=False)
    score_font = _get_font(48, bold=True)
    status_font = _get_font(16, bold=True)

    draw.text((30, 35), league_name.upper(), fill=(245, 249, 248), font=league_font)

    short_date = date_text
    month_map = {
        "January": "Jan", "February": "Feb", "March": "Mar", "April": "Apr",
        "May": "May", "June": "Jun", "July": "Jul", "August": "Aug",
        "September": "Sep", "October": "Oct", "November": "Nov", "December": "Dec",
    }
    for full_month, short_month in month_map.items():
        short_date = short_date.replace(full_month, short_month)

    header_right = "  •  ".join(value for value in (short_date, time_text) if value)
    if header_right:
        bbox = draw.textbbox((0, 0), header_right, font=meta_font)
        draw.text(
            (870 - (bbox[2] - bbox[0]), 38), header_right, fill=(190, 208, 207), font=meta_font,
        )

    draw.line((30, 78, 870, 78), fill=(37, 61, 63), width=1)

    def fit_team_font(text: str, max_width: int):
        size = 32
        while size >= 17:
            font = _get_font(size, bold=True)
            bbox = draw.textbbox((0, 0), text, font=font)
            if bbox[2] - bbox[0] <= max_width:
                return font
            size -= 1
        return _get_font(17, bold=True)

    def centered_text(text, center_x, y, font, fill):
        bbox = draw.textbbox((0, 0), text, font=font)
        draw.text((center_x - (bbox[2] - bbox[0]) / 2, y), text, font=font, fill=fill)

    home_center = 165
    away_center = 735
    logo_y = 178
    logo_size = 108

    home_logo = _download_logo(match.get("home_logo"))
    away_logo = _download_logo(match.get("away_logo"))

    def paste_logo(logo, center_x, center_y):
        draw.ellipse(
            (center_x - 60, center_y - 60, center_x + 60, center_y + 60),
            fill=(9, 29, 33), outline=(42, 68, 69), width=1,
        )
        if logo:
            prepared = _prepare_logo(logo, logo_size)
            x = center_x - prepared.width // 2
            y = center_y - prepared.height // 2
            image.paste(prepared, (x, y), prepared)

    paste_logo(home_logo, home_center, logo_y)
    paste_logo(away_logo, away_center, logo_y)

    home_team_font = fit_team_font(home_name, 260)
    away_team_font = fit_team_font(away_name, 260)

    centered_text(home_name, home_center, 250, home_team_font, (244, 248, 247))
    centered_text(away_name, away_center, 250, away_team_font, (244, 248, 247))

    home_goals = match.get("goals_home")
    away_goals = match.get("goals_away")

    # A match that hasn't started must always show VS, even if the API
    # represents the empty score as 0-0.
    if api.is_pre_match(status_short):
        score_text = "VS"
    else:
        score_text = (
            f"{home_goals if home_goals is not None else 0}  -  "
            f"{away_goals if away_goals is not None else 0}"
        )

    draw.rounded_rectangle(
        (360, 110, 540, 178), radius=17, fill=(16, 36, 42), outline=(43, 69, 72), width=1,
    )
    centered_text(score_text, 450, 116, score_font, (250, 252, 251))

    minute = match.get("minute") or match.get("elapsed")
    status_upper = api.status_display_label(status_short, minute)

    if status_short == "LIVE":
        pill_fill, pill_text = (18, 160, 96), (245, 255, 249)
    elif status_short == "HT":
        pill_fill, pill_text = (18, 130, 150), (235, 252, 255)
    elif api.is_postponed(status_short):
        pill_fill, pill_text = (137, 91, 27), (255, 244, 214)
    elif api.is_cancelled(status_short) or api.is_suspended(status_short):
        pill_fill, pill_text = (101, 55, 62), (255, 230, 234)
    elif api.is_finished(status_short):
        pill_fill, pill_text = (61, 79, 85), (239, 246, 247)
    else:
        pill_fill, pill_text = (27, 65, 91), (220, 239, 255)

    bbox = draw.textbbox((0, 0), status_upper, font=status_font)
    pill_w = max(125, (bbox[2] - bbox[0]) + 32)

    draw.rounded_rectangle(
        (450 - pill_w // 2, 190, 450 + pill_w // 2, 225), radius=14, fill=pill_fill,
    )
    centered_text(status_upper, 450, 195, status_font, pill_text)

    if api.is_pre_match(status_short):
        footer = f"Kick-off  •  {time_text}" if time_text else "Kick-off"
        centered_text(footer, 450, 265, status_font, (105, 137, 134))

    # ========================================================
    # ODDS
    # ========================================================

    odds_data = match.get("odds_1x2") or {}

    odds_home = odds_data.get("home")
    odds_draw = odds_data.get("draw")
    odds_away = odds_data.get("away")

    first_half = match.get("odds_1h_1x2") or {}

    fh_home = first_half.get("home")
    fh_draw = first_half.get("draw")
    fh_away = first_half.get("away")

    has_full_match = any(
        value is not None
        for value in (
            odds_home,
            odds_draw,
            odds_away,
        )
    )

    has_first_half = any(
        value is not None
        for value in (
            fh_home,
            fh_draw,
            fh_away,
        )
    )

    if has_full_match or has_first_half:
        odds_label_font = _get_font(14, bold=True)
        odds_value_font = _get_font(20, bold=True)

        draw.line(
            (120, 305, 780, 305),
            fill=(37, 61, 63),
            width=1,
        )

        source = str(
            odds_data.get("bookmaker", "")
        ).strip()

        if source and source.lower() != "consensus":
            label = f"MATCH RESULT • {source.upper()}"
        else:
            label = "MATCH RESULT"

        centered_text(
            label,
            450,
            316,
            odds_label_font,
            (139, 166, 163),
        )

        def odds_text(label_text: str, value):
            if value is None:
                return f"{label_text}  —"

            try:
                return f"{label_text}  {float(value):.2f}"
            except (TypeError, ValueError):
                return f"{label_text}  —"

        full_match_parts = [
            (odds_text("1", odds_home), 260),
            (odds_text("X", odds_draw), 450),
            (odds_text("2", odds_away), 640),
        ]

        for text_value, center_x in full_match_parts:
            centered_text(
                text_value,
                center_x,
                340,
                odds_value_font,
                (241, 247, 245),
            )

        if has_first_half:
            centered_text(
                "1ST HALF",
                450,
                380,
                odds_label_font,
                (139, 166, 163),
            )

            first_half_parts = [
                (odds_text("1", fh_home), 260),
                (odds_text("X", fh_draw), 450),
                (odds_text("2", fh_away), 640),
            ]

            for text_value, center_x in first_half_parts:
                centered_text(
                    text_value,
                    center_x,
                    404,
                    odds_value_font,
                    (241, 247, 245),
                )


    output = io.BytesIO()
    output.name = "match_card.png"
    image.save(output, format="PNG", optimize=True)
    output.seek(0)
    return output


# ============================================================
# Download / prepare team logo
# ============================================================

def _download_logo(url: str | None):
    return _download_asset(url)


def _prepare_logo(logo: Image.Image, size: int) -> Image.Image:
    logo = logo.copy()
    logo.thumbnail((size, size), Image.Resampling.LANCZOS)

    canvas = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    x = (size - logo.width) // 2
    y = (size - logo.height) // 2
    canvas.paste(logo, (x, y), logo)
    return canvas


# ============================================================
# Visual cards for non-football sports
# ============================================================


@lru_cache(maxsize=512)
def _download_asset(url: str | None, timeout: float | None = None):
    if not url:
        return None
    try:
        headers = {
            "User-Agent": "MySportInfoBot/1.0 (sports match-card image previews)",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        }
        if "upload.wikimedia.org" in url:
            headers["Referer"] = "https://en.wikipedia.org/"
        elif "sofascore.com" in url:
            headers["Referer"] = "https://www.sofascore.com/"
        # Tennis API player photos are served from the RapidAPI host and
        # require the same RapidAPI authorization as the JSON endpoints.
        if "tennis-api-atp-wta-itf.p.rapidapi.com" in url:
            rapid_key = os.getenv("TENNIS_RAPIDAPI_KEY") or os.getenv("RAPIDAPI_KEY")
            if rapid_key:
                headers.update({
                    "X-RapidAPI-Key": rapid_key,
                    "X-RapidAPI-Host": "tennis-api-atp-wta-itf.p.rapidapi.com",
                })
        response = requests.get(
            url, timeout=10 if timeout is None else timeout, headers=headers
        )
        response.raise_for_status()
        content_type = str(response.headers.get("Content-Type", "")).lower()
        data = response.content

        if url.lower().endswith(".svg") or "image/svg" in content_type:
            try:
                import cairosvg
                data = cairosvg.svg2png(bytestring=data, output_width=500, output_height=500)
            except Exception:
                return None

        return Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception as exc:
        logger.warning("Could not download sport asset: %s", exc)
        return None


def _download_tennis_player_asset(item: dict, side: str):
    """Try the provider photo first, then fall back to the player's Wikipedia portrait."""
    logo_url = item.get(f"{side}_logo")
    asset = _download_asset(logo_url, timeout=4)
    if asset is not None:
        return asset

    player_name = str(
        item.get(f"{side}_profile_name") or item.get(f"{side}_name") or ""
    ).strip()
    image_lookup = getattr(api, "_wikipedia_player_image", None)
    if not player_name or not callable(image_lookup):
        return None

    player_id = item.get(f"{side}_player_id")
    player_tour = item.get("tour")
    try:
        # Keep card rendering bounded when a provider omits the portrait.
        fallback_url = image_lookup(player_name, player_id, player_tour, fast=True)
    except TypeError:
        try:
            fallback_url = image_lookup(player_name, player_id, player_tour)
        except TypeError:
            # Keep compatibility with older one-argument image resolvers.
            fallback_url = image_lookup(player_name)
    except Exception:
        logger.debug("Could not resolve fallback portrait for %s", player_name, exc_info=True)
        return None
    if not fallback_url or fallback_url == logo_url:
        return None
    return _download_asset(fallback_url, timeout=4)


def _paste_round_asset(image: Image.Image, asset, center_x: int, center_y: int, size: int):
    frame = draw = ImageDraw.Draw(image)
    draw.ellipse(
        (center_x - size // 2 - 5, center_y - size // 2 - 5,
         center_x + size // 2 + 5, center_y + size // 2 + 5),
        fill=(10, 30, 35),
        outline=(48, 74, 76),
        width=2,
    )
    if asset is None:
        return

    try:
        prepared = ImageOps.fit(asset, (size, size), method=Image.Resampling.LANCZOS)
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
        image.paste(prepared, (center_x - size // 2, center_y - size // 2), mask)
    except Exception:
        pass


def _sport_status_label(item: dict) -> str:
    status = str(item.get("status") or "NS").upper()
    if status == "LIVE":
        extra = []
        if item.get("period"):
            extra.append(f"P{item['period']}")
        if item.get("clock"):
            extra.append(str(item["clock"]))
        if item.get("live_text"):
            extra.append(str(item["live_text"]))
        return "LIVE" + (" • " + " • ".join(extra) if extra else "")
    if status == "FT":
        return "FINISHED"
    return "NOT STARTED"


def _sport_score(item: dict) -> str:
    status = str(item.get("status") or "NS").upper()
    if status == "NS":
        return "VS"
    if item.get("type") == "player_match":
        return str(item.get("score_text") or ("FINISHED" if status == "FT" else "LIVE"))
    home = item.get("home_score")
    away = item.get("away_score")
    if home is None and away is None:
        return "VS"
    return f"{home if home is not None else 0}  -  {away if away is not None else 0}"


def _draw_sport_odds(
    draw, centered_text, item: dict, center_y: int = 360, canvas: Image.Image | None = None
):
    odds = item.get("odds") or {}
    left = odds.get("left")
    right = odds.get("right")
    middle = odds.get("draw")
    if left is None and right is None and middle is None:
        if item.get("type") == "player_match":
            draw.line((120, center_y - 18, 780, center_y - 18), fill=(37, 61, 63), width=1)
            centered_text(
                "ODDS NOT AVAILABLE",
                450,
                center_y + 8,
                _get_font(15, bold=True),
                (139, 166, 163),
            )
            return center_y + 40
        if str(item.get("sport") or "").lower() == "hockey":
            draw.line((120, center_y - 18, 780, center_y - 18), fill=(37, 61, 63), width=1)
            centered_text("ODDS", 450, center_y, _get_font(14, bold=True), (139, 166, 163))
            centered_text(
                "No bookmaker prices available",
                450,
                center_y + 26,
                _get_font(16),
                (169, 194, 189),
            )
            return center_y + 66
        return

    draw.line((120, center_y - 18, 780, center_y - 18), fill=(37, 61, 63), width=1)
    bookmaker = str(odds.get("bookmaker") or "").strip()
    source = bookmaker.upper()
    label = "WINNER COEFFICIENT" + (f" • {source.upper()}" if source else "")
    label_font = _get_font(14, bold=True)
    value_font = _get_font(20, bold=True)
    centered_text(label, 450, center_y, label_font, (139, 166, 163))

    def val(x):
        if x is None:
            return "—"
        try:
            return f"{float(x):.2f}"
        except (TypeError, ValueError):
            return "—"

    if middle is not None:
        parts = [
            (f"1  {val(left)}", 230),
            (f"X  {val(middle)}", 450),
            (f"2  {val(right)}", 670),
        ]
    elif item.get("type") == "player_match":
        _center_tennis_player_label(
            draw,
            item.get("home_country"),
            str(item.get("home_name") or "Player 1"),
            250,
            center_y + 28,
            360,
            size=20,
            suffix=f"  {val(left)}",
            canvas=canvas,
        )
        _center_tennis_player_label(
            draw,
            item.get("away_country"),
            str(item.get("away_name") or "Player 2"),
            650,
            center_y + 28,
            360,
            size=20,
            suffix=f"  {val(right)}",
            canvas=canvas,
        )
        return center_y + 70
    else:
        parts = [
            (f"{str(item.get('home_name') or '1')[:18]}  {val(left)}", 250),
            (f"{str(item.get('away_name') or '2')[:18]}  {val(right)}", 650),
        ]
    for txt, x in parts:
        centered_text(txt, x, center_y + 28, value_font, (241, 247, 245))
    return center_y + 70


def _create_sport_team_card(item: dict) -> io.BytesIO:
    width, height = 900, 535
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    top = (6, 17, 23)
    bottom = (5, 34, 29)
    for y in range(height):
        t = y / max(height - 1, 1)
        r = int(top[0] * (1 - t) + bottom[0] * t)
        g = int(top[1] * (1 - t) + bottom[1] * t)
        b = int(top[2] * (1 - t) + bottom[2] * t)
        for x in range(width):
            edge = abs(x - width / 2) / (width / 2)
            glow = int(4 * (1 - edge))
            pixels[x, y] = (r + glow, g + glow, b + glow)

    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 4, width - 5, height - 5), radius=24, fill=(7, 24, 28), outline=(42, 67, 70), width=2)
    draw.rounded_rectangle((26, 15, width - 26, 18), radius=2, fill=(24, 174, 105))

    icon = str(item.get("sport_icon") or "🏆")
    competition = str(item.get("competition") or "")
    title = f"{icon} {competition.upper()}"
    date_text = str(item.get("date") or "")
    time_text = str(item.get("time") or "")
    title_font = _get_font(24, bold=True)
    meta_font = _get_font(19)
    score_font = _get_font(48, bold=True)
    status_font = _get_font(16, bold=True)

    draw.text((30, 35), title, fill=(245, 249, 248), font=title_font)
    header_right = "  •  ".join(x for x in (date_text, time_text) if x)
    if header_right:
        bbox = draw.textbbox((0, 0), header_right, font=meta_font)
        draw.text((870 - (bbox[2] - bbox[0]), 38), header_right, fill=(190, 208, 207), font=meta_font)
    draw.line((30, 78, 870, 78), fill=(37, 61, 63), width=1)

    def fit_text(text: str, max_width: int):
        for size in range(30, 16, -1):
            font = _get_font(size, bold=True)
            if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
                return font
        return _get_font(17, bold=True)

    def center(text, x, y, font, fill=(244, 248, 247)):
        bbox = draw.textbbox((0, 0), text, font=font)
        draw.text((x - (bbox[2] - bbox[0]) / 2, y), text, font=font, fill=fill)

    home_name = str(item.get("home_name") or "Home")
    away_name = str(item.get("away_name") or "Away")
    home_center, away_center = 165, 735
    logo_y, logo_size = 180, 110

    _paste_round_asset(image, _download_asset(item.get("home_logo")), home_center, logo_y, logo_size)
    _paste_round_asset(image, _download_asset(item.get("away_logo")), away_center, logo_y, logo_size)
    center(home_name, home_center, 250, fit_text(home_name, 260))
    center(away_name, away_center, 250, fit_text(away_name, 260))

    score = _sport_score(item)
    draw.rounded_rectangle((360, 112, 540, 180), radius=17, fill=(16, 36, 42), outline=(43, 69, 72), width=1)
    center(score, 450, 117, score_font)

    status = _sport_status_label(item)
    if item.get("status") == "LIVE":
        pill_fill, pill_text = (18, 160, 96), (245, 255, 249)
    elif item.get("status") == "FT":
        pill_fill, pill_text = (61, 79, 85), (239, 246, 247)
    else:
        pill_fill, pill_text = (27, 65, 91), (220, 239, 255)
    bbox = draw.textbbox((0, 0), status, font=status_font)
    pill_w = max(125, bbox[2] - bbox[0] + 32)
    draw.rounded_rectangle((450 - pill_w // 2, 195, 450 + pill_w // 2, 230), radius=14, fill=pill_fill)
    center(status, 450, 200, status_font, pill_text)

    if item.get("status") == "FT" and item.get("winner"):
        winner = str(item["winner"])
        center(f"🏆 Winner: {winner}", 450, 268, _get_font(18, bold=True), (231, 244, 239))

    odds_end = _draw_sport_odds(draw, center, item, 325)
    if odds_end is None:
        center("Kick-off  •  " + time_text if time_text and item.get("status") == "NS" else "", 450, 325, _get_font(16), (105, 137, 134))

    output = io.BytesIO()
    output.name = "sport_card.png"
    image.save(output, format="PNG", optimize=True)
    output.seek(0)
    return output


def _create_sport_tennis_card(item: dict) -> io.BytesIO:
    width, height = 900, 565
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    top, bottom = (6, 17, 23), (5, 34, 29)
    for y in range(height):
        t = y / max(height - 1, 1)
        r = int(top[0] * (1 - t) + bottom[0] * t)
        g = int(top[1] * (1 - t) + bottom[1] * t)
        b = int(top[2] * (1 - t) + bottom[2] * t)
        for x in range(width):
            edge = abs(x - width / 2) / (width / 2)
            glow = int(4 * (1 - edge))
            pixels[x, y] = (r + glow, g + glow, b + glow)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 4, width - 5, height - 5), radius=24, fill=(7, 24, 28), outline=(42, 67, 70), width=2)
    draw.rounded_rectangle((26, 15, width - 26, 18), radius=2, fill=(24, 174, 105))

    title = f"🎾 {item.get('tour', 'ATP')} • {item.get('competition', 'Tennis')}"
    title_font = _get_font(22, bold=True)
    meta_font = _get_font(18)
    score_font = _get_font(40, bold=True)
    status_font = _get_font(16, bold=True)
    draw.text((30, 35), str(title), fill=(245, 249, 248), font=title_font)
    header_right = "  •  ".join(x for x in (str(item.get('date') or ''), str(item.get('time') or '')) if x)
    if header_right:
        bbox = draw.textbbox((0, 0), header_right, font=meta_font)
        draw.text((870 - (bbox[2] - bbox[0]), 38), header_right, fill=(190, 208, 207), font=meta_font)
    draw.line((30, 78, 870, 78), fill=(37, 61, 63), width=1)

    p1, p2 = str(item.get("home_name") or "Player 1"), str(item.get("away_name") or "Player 2")
    details_lookup = getattr(api, "_live_tennis_player_details", None)
    country_lookup = getattr(api, "_live_tennis_player_country", None)
    for side, player_name in (("home", p1), ("away", p2)):
        country_key = f"{side}_country"
        try:
            profile = {}
            if callable(details_lookup) and (
                not item.get(country_key) or not item.get(f"{side}_logo")
            ):
                try:
                    profile = details_lookup(
                        player_name,
                        item.get(f"{side}_player_id"),
                        timeout=2.5,
                    ) or {}
                except TypeError:
                    profile = details_lookup(
                        player_name, item.get(f"{side}_player_id")
                    ) or {}
            if not item.get(country_key) and profile.get("country"):
                item[country_key] = profile["country"]
            if not item.get(f"{side}_logo") and profile.get("image"):
                item[f"{side}_logo"] = profile["image"]
            if not item.get(f"{side}_player_id") and profile.get("id"):
                item[f"{side}_player_id"] = profile["id"]
            if profile.get("name"):
                item[f"{side}_profile_name"] = profile["name"]
        except Exception:
            logger.debug("Could not resolve tennis profile for %s", player_name, exc_info=True)
        if not item.get(country_key) and callable(country_lookup):
            try:
                try:
                    item[country_key] = country_lookup(
                        player_name, item.get("tour"), timeout=2.5
                    )
                except TypeError:
                    item[country_key] = country_lookup(
                        player_name, item.get("tour")
                    )
            except TypeError:
                try:
                    item[country_key] = country_lookup(player_name)
                except Exception:
                    logger.debug("Could not resolve tennis nationality for %s", player_name, exc_info=True)
            except Exception:
                logger.debug("Could not resolve tennis nationality for %s", player_name, exc_info=True)
    _paste_round_asset(image, _download_tennis_player_asset(item, "home"), 165, 170, 125)
    _paste_round_asset(image, _download_tennis_player_asset(item, "away"), 735, 170, 125)

    def center(text, x, y, font, fill=(244, 248, 247)):
        bbox = draw.textbbox((0, 0), text, font=font)
        draw.text((x - (bbox[2] - bbox[0]) / 2, y), text, font=font, fill=fill)

    _center_tennis_player_label(
        draw, item.get("home_country"), p1, 165, 245, 270, size=28, canvas=image
    )
    _center_tennis_player_label(
        draw, item.get("away_country"), p2, 735, 245, 270, size=28, canvas=image
    )

    score = _sport_score(item)
    draw.rounded_rectangle((325, 112, 575, 180), radius=17, fill=(16, 36, 42), outline=(43, 69, 72), width=1)
    center(score, 450, 124, score_font)

    status = _sport_status_label(item)
    if item.get("status") == "LIVE":
        pill_fill, pill_text = (18, 160, 96), (245, 255, 249)
    elif item.get("status") == "FT":
        pill_fill, pill_text = (61, 79, 85), (239, 246, 247)
    else:
        pill_fill, pill_text = (27, 65, 91), (220, 239, 255)
    bbox = draw.textbbox((0, 0), status, font=status_font)
    pill_w = max(125, bbox[2] - bbox[0] + 32)
    draw.rounded_rectangle((450 - pill_w // 2, 195, 450 + pill_w // 2, 230), radius=14, fill=pill_fill)
    center(status, 450, 200, status_font, pill_text)

    if item.get("winner"):
        winner = str(item["winner"])
        winner_country = ""
        if winner.casefold() == p1.casefold():
            winner_country = str(item.get("home_country") or "")
        elif winner.casefold() == p2.casefold():
            winner_country = str(item.get("away_country") or "")
        _center_tennis_player_label(
            draw,
            winner_country,
            winner,
            450,
            270,
            600,
            size=18,
            prefix="🏆 Winner: ",
            canvas=image,
        )
    if item.get("round"):
        center(f"🏟 {item['round']}", 450, 298, _get_font(16, bold=True), (139, 166, 163))

    _draw_sport_odds(draw, center, item, 352, canvas=image)
    output = io.BytesIO()
    output.name = "tennis_card.png"
    image.save(output, format="PNG", optimize=True)
    output.seek(0)
    return output


def _create_f1_card(item: dict) -> io.BytesIO:
    """Compact F1 race card: next session + Top 10 driver rows with photos/odds."""
    width, height = 900, 1040
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    top, bottom = (5, 16, 22), (5, 32, 28)
    for y in range(height):
        t = y / max(height - 1, 1)
        r = int(top[0] * (1 - t) + bottom[0] * t)
        g = int(top[1] * (1 - t) + bottom[1] * t)
        b = int(top[2] * (1 - t) + bottom[2] * t)
        for x in range(width):
            edge = abs(x - width / 2) / (width / 2)
            glow = int(4 * (1 - edge))
            pixels[x, y] = (r + glow, g + glow, b + glow)

    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (4, 4, width - 5, height - 5),
        radius=24,
        fill=(7, 24, 28),
        outline=(42, 67, 70),
        width=2,
    )
    draw.rounded_rectangle((26, 15, width - 26, 19), radius=2, fill=(24, 174, 105))

    title_font = _get_font(24, bold=True)
    race_font = _get_font(34, bold=True)
    meta_font = _get_font(16)
    next_font = _get_font(16, bold=True)
    label_font = _get_font(14, bold=True)
    driver_font = _get_font(17, bold=True)
    points_font = _get_font(14)
    odds_font = _get_font(16, bold=True)

    # Header — avoid emoji glyphs because they can render as empty squares on
    # Pillow/Windows font combinations.
    draw.text((30, 35), "FORMULA 1", fill=(245, 249, 248), font=title_font)
    draw.text((30, 76), str(item.get("competition") or "Grand Prix"), fill=(238, 246, 243), font=race_font)

    location = " • ".join(
        x for x in (
            str(item.get("circuit") or ""),
            str(item.get("locality") or ""),
            str(item.get("country") or ""),
        ) if x
    )
    if location:
        draw.text((30, 116), location, fill=(169, 194, 189), font=meta_font)

    race_date = str(item.get("race_date") or "").strip()
    race_time = str(item.get("race_time") or "").strip()
    race_when = " • ".join(x for x in (race_date, race_time) if x)
    if race_when:
        draw.text(
            (30, 137),
            f"RACE  •  {race_when} Yerevan time",
            fill=(139, 166, 163),
            font=_get_font(13, bold=True),
        )

    # Current stage/status + next session.
    stage_label = str(item.get("stage") or "Schedule unavailable")
    stage_detail = str(item.get("stage_detail") or "").strip()
    draw.text((30, 160), "CURRENT STAGE", fill=(132, 164, 159), font=label_font)
    draw.text((30, 183), stage_label, fill=(232, 244, 242), font=next_font)
    if stage_detail:
        draw.text((30, 204), stage_detail, fill=(169, 194, 189), font=meta_font)

    next_session = item.get("next_session") or {}
    next_label = str(next_session.get("label") or item.get("stage") or "Schedule unavailable")
    next_date = str(next_session.get("date") or "")
    next_time = str(next_session.get("time") or "")
    draw.text((30, 235), "NEXT SESSION", fill=(132, 164, 159), font=label_font)
    session_line = next_label
    if next_date or next_time:
        session_line += "  •  " + " • ".join(x for x in (next_date, next_time) if x)
    draw.text((30, 258), session_line, fill=(232, 244, 242), font=next_font)

    # Pit-stop summary is shown from real race data only. Before the race,
    # explicitly state that no stops exist yet.
    pit = item.get("pit_stop_summary") or {}
    pit_y = 295
    draw.line((30, pit_y, 870, pit_y), fill=(37, 61, 63), width=1)
    draw.text((30, pit_y + 15), "PIT STOP SUMMARY", fill=(139, 166, 163), font=label_font)
    pit_status = str(pit.get("status") or "unavailable")
    if pit_status == "available":
        pit_line = f"{int(pit.get('total_stops') or 0)} stops • {int(pit.get('drivers') or 0)} drivers"
        fastest = pit.get("fastest") or {}
        if fastest.get("duration"):
            pit_line += f" • Fastest {fastest.get('duration')}"
    elif str(item.get("stage_status") or "") == "pre_race":
        pit_line = "Race not started • no pit stops yet"
    elif str(item.get("stage_status") or "") == "in_progress":
        pit_line = "Race in progress • pit-stop data pending"
    else:
        pit_line = "No pit-stop data available"
    draw.text((30, pit_y + 38), pit_line, fill=(232, 244, 242), font=meta_font)

    header_y = 355
    draw.line((30, header_y, 870, header_y), fill=(37, 61, 63), width=1)
    draw.text((30, header_y + 15), "CHAMPIONSHIP TOP 10", fill=(139, 166, 163), font=label_font)
    draw.text((650, header_y + 15), "POINTS", fill=(108, 136, 134), font=label_font)
    odds_column_title = "ODDS · KROK" if item.get("odds_provider") else "RACE ODDS"
    draw.text((770, header_y + 15), odds_column_title, fill=(108, 136, 134), font=label_font)

    rows = item.get("standings") or []
    if not any((row.get("odds") or {}).get("price") is not None for row in rows):
        draw.text(
            (770, header_y + 32),
            "NO LINES",
            fill=(139, 166, 163),
            font=_get_font(10, bold=True),
        )
    y = header_y + 39
    row_h = 56
    for row in rows[:10]:
        position = int(row.get("position") or 0)
        name = str(row.get("name") or "Driver")
        pts = str(row.get("points") or "0")
        photo_url = str(row.get("photo_url") or "").strip()
        odd = row.get("odds") or {}

        draw.rounded_rectangle(
            (30, y, 870, y + row_h),
            radius=11,
            fill=(10, 31, 36),
            outline=(29, 54, 57),
            width=1,
        )

        # Small circular driver headshot.
        asset = _download_asset(photo_url) if photo_url else None
        _paste_round_asset(image, asset, 58, y + row_h // 2, 40)

        draw.text((86, y + 18), f"{position}. {name}", fill=(238, 246, 245), font=driver_font)

        pts_text = f"{pts}"
        bbox = draw.textbbox((0, 0), pts_text, font=points_font)
        draw.text((715 - (bbox[2] - bbox[0]), y + 19), pts_text, fill=(164, 187, 183), font=points_font)

        coefficient = "—"
        bookmaker = ""
        if odd.get("price") is not None:
            try:
                coefficient = f"{float(odd['price']):.2f}"
            except (TypeError, ValueError):
                coefficient = "—"
        bookmaker = str(odd.get("bookmaker") or "").strip()
        if bookmaker:
            draw.text(
                (770, y + 5),
                bookmaker[:14].upper(),
                fill=(139, 166, 163),
                font=_get_font(9, bold=True),
            )
        bbox = draw.textbbox((0, 0), coefficient, font=odds_font)
        draw.text((850 - (bbox[2] - bbox[0]), y + 25), coefficient, fill=(241, 247, 245), font=odds_font)

        y += row_h + 7

    output = io.BytesIO()
    output.name = "f1_card.png"
    image.save(output, format="PNG", optimize=True)
    output.seek(0)
    return output


def _create_sport_card(item: dict) -> io.BytesIO:
    if item.get("type") == "f1":
        return _create_f1_card(item)
    if item.get("type") == "player_match":
        return _create_sport_tennis_card(item)
    return _create_sport_team_card(item)


def _sport_fallback_text(item: dict) -> str:
    icon = str(item.get("sport_icon") or "🏆")
    competition = str(item.get("competition") or "Sport")
    date_text = str(item.get("date") or "")
    time_text = str(item.get("time") or "")
    if item.get("type") == "f1":
        race_when = " • ".join(
            x for x in (str(item.get("race_date") or ""), str(item.get("race_time") or "")) if x
        )
        lines = [
            "🏎️ Formula 1",
            "",
            str(item.get("competition") or "Grand Prix"),
            " • ".join(x for x in (
                str(item.get("circuit") or ""), str(item.get("locality") or ""),
                str(item.get("country") or ""),
            ) if x),
            f"🏁 {item.get('stage') or 'Schedule available'}",
        ]
        if race_when:
            lines.append(f"🏎️ Race: {race_when} Yerevan time")
        if item.get("stage_detail"):
            lines.append(str(item["stage_detail"]))
        next_session = item.get("next_session") or {}
        if next_session.get("label"):
            lines.append("➡️ Next: " + str(next_session["label"]))
        for row in (item.get("standings") or [])[:10]:
            odds = row.get("odds") or {}
            price = odds.get("price")
            coefficient = f"{float(price):.2f}" if price is not None else "—"
            lines.append(f"{row.get('position')}. {row.get('name')} — {row.get('points')} pts • {coefficient}")
        return "\n".join(lines)
    if item.get("type") == "player_match":
        home_name = _format_tennis_player_name(
            item.get("home_country"), str(item.get("home_name") or "Player 1")
        )
        away_name = _format_tennis_player_name(
            item.get("away_country"), str(item.get("away_name") or "Player 2")
        )
    else:
        home_name = str(item.get("home_name") or "")
        away_name = str(item.get("away_name") or "")
    lines = [f"{icon} {competition}", "", f"{home_name} vs {away_name}", _sport_status_label(item)]
    score = _sport_score(item)
    if score != "VS":
        lines.append(score)
    if item.get("winner"):
        winner = str(item["winner"])
        if item.get("type") == "player_match":
            if winner.casefold() == str(item.get("home_name") or "").casefold():
                winner = _format_tennis_player_name(item.get("home_country"), winner)
            elif winner.casefold() == str(item.get("away_name") or "").casefold():
                winner = _format_tennis_player_name(item.get("away_country"), winner)
        lines.append(f"🏆 Winner: {winner}")
    odds = item.get("odds") or {}
    if odds:
        bookmaker = str(odds.get("bookmaker") or "").strip()
        source = f" • {bookmaker}" if bookmaker else ""
        lines.append(
            f"💰 Coefficient{source}: "
            f"{odds.get('left', '—')} / {odds.get('right', '—')}"
        )
    when = " • ".join(x for x in (date_text, time_text) if x)
    if when:
        lines.append(f"📅 {when}")
    return "\n".join(lines)


# ============================================================
# Send compact match card
# ============================================================

async def _send_compact_match_card(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    match: dict,
    reply_markup=None,
):

    try:
        card = _create_match_card(match)
        message = await context.bot.send_photo(
            chat_id=chat_id,
            photo=InputFile(card, filename="match_card.png"),
            reply_markup=reply_markup,
        )
        return message.message_id

    except Exception:
        logger.exception("Could not create/send compact match card")

        try:
            message = await context.bot.send_message(
                chat_id=chat_id, text=_fallback_match_text(match), reply_markup=reply_markup,
            )
            return message.message_id
        except Exception:
            logger.exception("Could not send fallback message")
            return None


def _fallback_match_text(match: dict) -> str:
    date_text, time_text = _yerevan_date_time_text(match)
    league = str(match.get("league_name", "Football"))
    home = api.normalize_team_display_name(match.get("home_team", "Home"))
    away = api.normalize_team_display_name(match.get("away_team", "Away"))
    status = str(match.get("status_short", "NS")).strip().upper()

    if api.is_pre_match(status):
        score = "VS"
    else:
        hg = match.get("goals_home")
        ag = match.get("goals_away")
        score = f"{hg if hg is not None else 0} - {ag if ag is not None else 0}"

    minute = match.get("minute") or match.get("elapsed")
    state = api.status_display_label(status, minute)

    when = " • ".join(v for v in (date_text, time_text) if v)
    return (
        f"⚽ {league}\n\n"
        f"{home}  {score}  {away}\n\n"
        f"{state}"
        + (f"\n📅 {when}" if when else "")
    )


def _get_league_flag(league_id: str | None) -> str:
    for league in api.LEAGUES.values():
        if league["id"] == league_id:
            return league["flag"]
    return "⚽"


# ============================================================
# Navigation
# ============================================================

def _matches_nav_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Back", callback_data="back_to_previous"),
        InlineKeyboardButton("🔄 Refresh", callback_data="refresh"),
    ]])


def _bottom_back_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Back", callback_data="back_to_previous"),
        InlineKeyboardButton("🔄 Refresh", callback_data="refresh"),
        InlineKeyboardButton("🏠 Start", callback_data="back_to_start"),
    ]])


async def _edit_navigation_message(
    query, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None,
) -> None:
    """Edit the navigation/header message safely from any callback source."""

    chat_id = query.message.chat_id
    header_message_id = context.user_data.get("header_message_id")

    if header_message_id is not None:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=header_message_id, text=text, reply_markup=reply_markup,
            )
            return
        except TelegramError:
            pass

    if getattr(query.message, "text", None):
        try:
            await query.edit_message_text(text, reply_markup=reply_markup)
            context.user_data["header_message_id"] = query.message.message_id
            return
        except TelegramError:
            pass

    sent = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
    context.user_data["header_message_id"] = sent.message_id


# ============================================================
# Back button
# ============================================================

async def handle_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    query = update.callback_query
    await query.answer()

    destination = query.data
    mode = context.user_data.get("mode")
    screen = context.user_data.get("screen")

    if destination == "back_to_previous" and mode == "league" and screen == "period_selection":
        context.user_data["screen"] = "league_selection"
        await _edit_navigation_message(query, context, "🏆 Choose a league", reply_markup=_league_keyboard())
        return

    if destination == "back_to_previous" and mode == "league" and screen == "matches":
        await _clear_previous_match_messages(query, context)
        context.user_data["screen"] = "league_selection"
        await _edit_navigation_message(query, context, "🏆 Choose a league", reply_markup=_league_keyboard())
        return

    if destination == "back_to_previous" and mode == "top":
        await _clear_previous_match_messages(query, context)
        context.user_data.clear()
        await _edit_navigation_message(
            query, context, "🏆 MySportInfo Bot\n\n⚽ What sport would you like to follow?",
            reply_markup=_sport_keyboard(),
        )
        return

    if destination == "back_to_previous" and str(mode or "").startswith("sport_"):
        sport = context.user_data.get("sport")

        if screen == "sport_period_selection" and sport in SPORT_VARIANTS:
            context.user_data.pop("sport_variant", None)
            context.user_data["screen"] = "sport_variant_selection"
            await _edit_navigation_message(
                query,
                context,
                _sport_variant_title(sport),
                reply_markup=_sport_variant_keyboard(sport),
            )
            return

        if screen == "sport_period_selection" and sport == "tennis":
            context.user_data["screen"] = "tennis_tour_selection"
            await _edit_navigation_message(
                query,
                context,
                "🎾 Tennis\n\n🏆 Choose tour",
                reply_markup=_tennis_tour_keyboard(),
            )
            return

        if screen == "sport_matches" and sport != "f1":
            await _clear_previous_match_messages(query, context)
            context.user_data["screen"] = "sport_period_selection"
            await _edit_navigation_message(
                query,
                context,
                (
                    f"🎾 Tennis — {str(context.user_data.get('tennis_tour') or 'ATP').upper()}\n\n📅 Choose a period"
                    if sport == "tennis"
                    else _sport_period_title(
                        sport,
                        context.user_data.get("period", "today"),
                        context.user_data.get("sport_variant"),
                    )
                ),
                reply_markup=_sport_period_keyboard(sport),
            )
            return

        await _clear_previous_match_messages(query, context)
        context.user_data.clear()
        await _edit_navigation_message(
            query, context, "🏆 MySportInfo Bot\n\n⚽ What sport would you like to follow?",
            reply_markup=_sport_keyboard(),
        )
        return

    await _clear_previous_match_messages(query, context)
    context.user_data.clear()
    await _edit_navigation_message(
        query, context, "🏆 MySportInfo Bot\n\n⚽ What sport would you like to follow?",
        reply_markup=_sport_keyboard(),
    )


async def handle_back_to_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    query = update.callback_query
    await query.answer()

    await _clear_previous_match_messages(query, context)
    context.user_data.clear()
    await _edit_navigation_message(
        query, context, "🏆 MySportInfo Bot\n\n⚽ What sport would you like to follow?",
        reply_markup=_sport_keyboard(),
    )


# ============================================================
# Refresh
# ============================================================

async def handle_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    query = update.callback_query
    await query.answer("Refreshing...")

    mode = context.user_data.get("mode")
    if not mode:
        await query.answer("Please start again with /start.", show_alert=True)
        return

    if str(mode).startswith("sport_"):
        sport = context.user_data.get("sport")
        if sport:
            await _show_sport_matches(
                query, context, sport, context.user_data.get("period"),
                context.user_data.get("tennis_tour"),
                context.user_data.get("sport_variant"),
            )
        return

    await _edit_navigation_message(query, context, "⏳ Refreshing...")
    await show_matches(query, context)


# ============================================================
# Fast live score updater
# ============================================================

async def _stop_live_update_loop(context: ContextTypes.DEFAULT_TYPE) -> None:
    task = context.user_data.get("live_update_task")
    if task is None:
        return

    context.user_data["live_update_task"] = None
    if task is asyncio.current_task():
        return

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("Live update task stopped with an error")


def _should_poll_live_matches(matches: list) -> bool:
    """Poll live/near-kickoff matches for score/status changes."""
    if not matches:
        return False

    now = datetime.now(UTC_TZ)
    soon = now + timedelta(minutes=120)

    for match in matches:
        status = str(match.get("status_short", "")).upper()

        if api.is_live_family(status) or api.is_suspended(status):
            return True

        if api.is_finished(status) or api.is_cancelled(status) or api.is_postponed(status):
            continue

        kickoff = _parse_match_datetime(match)
        if kickoff is not None:
            kickoff_utc = kickoff.astimezone(UTC_TZ)
            if now - timedelta(minutes=15) <= kickoff_utc <= soon:
                return True

    return False


def _has_missing_uefa_odds(matches: list) -> bool:
    """Return True while a visible UEFA fixture still has no complete 1X2."""
    if not getattr(api, "THE_ODDS_API_KEY", None):
        return False

    for match in matches or []:
        if str(match.get("league_id", "")).upper() not in {"CL", "EL", "ECL"}:
            continue

        status = str(match.get("status_short", "")).upper()
        if api.is_finished(status) or api.is_cancelled(status) or api.is_postponed(status):
            continue

        odds = match.get("odds_1x2") or {}
        if not all(odds.get(k) is not None for k in ("home", "draw", "away")):
            return True

    return False


async def _start_live_update_loop(context: ContextTypes.DEFAULT_TYPE) -> None:
    await _stop_live_update_loop(context)

    matches = context.user_data.get("live_match_snapshot") or []
    should_poll_live = _should_poll_live_matches(matches)
    should_poll_odds = _has_missing_uefa_odds(matches)
    logger.info(
        "[UPDATER] start check: screen=%s matches=%d live=%s odds_watch=%s",
        context.user_data.get("screen"), len(matches), should_poll_live, should_poll_odds,
    )

    if not should_poll_live and not should_poll_odds:
        logger.info("[UPDATER] NOT started: no live/near-kickoff match and no missing UEFA odds")
        return

    context.user_data["live_update_task"] = asyncio.create_task(_live_score_update_loop(context))
    logger.info("[UPDATER] task CREATED")


async def _live_score_update_loop(context: ContextTypes.DEFAULT_TYPE) -> None:
    live_refresh_seconds = max(60, int(getattr(api, "LIVE_SCORE_REFRESH_SECONDS", 120)))
    # Odds are deliberately refreshed much less often than live scores so the
    # free The Odds API quota is not burned by minute-by-minute polling.
    odds_refresh_seconds = max(900, int(os.getenv("ODDS_AUTO_REFRESH_SECONDS", "10800")))
    logger.info(
        "[UPDATER] loop running: live_interval=%ss odds_interval=%ss",
        live_refresh_seconds, odds_refresh_seconds,
    )

    first_cycle = True
    last_odds_refresh = time.monotonic()

    try:
        while context.user_data.get("screen") == "matches":
            matches = context.user_data.get("live_match_snapshot") or []
            if not matches:
                logger.info("[UPDATER] stopping: no matches")
                break

            should_poll_live = _should_poll_live_matches(matches)
            should_poll_odds = _has_missing_uefa_odds(matches)

            if not first_cycle:
                sleep_for = live_refresh_seconds if should_poll_live else odds_refresh_seconds
                await asyncio.sleep(sleep_for)
            first_cycle = False

            if context.user_data.get("screen") != "matches":
                break

            matches = context.user_data.get("live_match_snapshot") or []
            message_ids = context.user_data.get("live_card_message_ids") or []
            if not matches or not message_ids:
                logger.info("[UPDATER] stopping: no matches/message ids")
                break

            logger.info("[UPDATER] cycle: checking %d visible card(s)", len(matches))
            changed_indexes = set()

            if _should_poll_live_matches(matches):
                bot_guard_changed = set()
                for idx, match in enumerate(matches):
                    if _force_stale_live_status(match):
                        bot_guard_changed.add(idx)

                live_changed = await asyncio.to_thread(api.refresh_live_scores, matches)
                changed_indexes |= set(live_changed) | bot_guard_changed
                logger.info("[LIVE] cycle result: changed=%s", sorted(set(live_changed) | bot_guard_changed))

            # Retry missing UEFA odds periodically. If a provider publishes a
            # line later, the already-open Telegram cards get the coefficients
            # automatically without the user pressing Refresh.
            now_mono = time.monotonic()
            if (
                _has_missing_uefa_odds(matches)
                and now_mono - last_odds_refresh >= odds_refresh_seconds
            ):
                before = []
                for match in matches:
                    full = match.get("odds_1x2") or {}
                    first = match.get("odds_1h_1x2") or {}
                    before.append((
                        full.get("home"), full.get("draw"), full.get("away"),
                        first.get("home"), first.get("draw"), first.get("away"),
                        full.get("bookmaker"),
                    ))

                logger.info("[ODDS-AUTO] retrying missing UEFA odds")
                try:
                    await asyncio.to_thread(api.enrich_matches_with_odds, matches)
                except Exception:
                    logger.exception("[ODDS-AUTO] odds refresh failed")
                else:
                    for idx, match in enumerate(matches):
                        full = match.get("odds_1x2") or {}
                        first = match.get("odds_1h_1x2") or {}
                        after = (
                            full.get("home"), full.get("draw"), full.get("away"),
                            first.get("home"), first.get("draw"), first.get("away"),
                            full.get("bookmaker"),
                        )
                        if idx < len(before) and after != before[idx]:
                            changed_indexes.add(idx)
                            logger.info(
                                "[ODDS-AUTO] updated: %s vs %s",
                                match.get("home_team"), match.get("away_team"),
                            )
                finally:
                    last_odds_refresh = time.monotonic()

            should_continue = _should_poll_live_matches(matches) or _has_missing_uefa_odds(matches)

            if changed_indexes:
                chat_id = context.user_data.get("chat_id")
                if chat_id is None:
                    break

                for index in sorted(changed_indexes):
                    if index >= len(message_ids):
                        continue

                    message_id = message_ids[index]
                    try:
                        card = _create_match_card(matches[index])
                        keyboard = _bottom_back_keyboard() if index == len(message_ids) - 1 else None

                        await context.bot.edit_message_media(
                            chat_id=chat_id,
                            message_id=message_id,
                            media=InputMediaPhoto(media=InputFile(card, filename="match_card.png")),
                            reply_markup=keyboard,
                        )
                        logger.info("Card updated: message_id=%s index=%s", message_id, index)
                    except TelegramError:
                        logger.exception("Could not update card message %s", message_id)

            if not should_continue:
                logger.info("[UPDATER] stopping: no live/near-kickoff matches and no missing UEFA odds")
                break
    except asyncio.CancelledError:
        logger.info("[UPDATER] cancelled")
        raise
    except Exception:
        logger.exception("[UPDATER] loop failed")
    finally:
        if context.user_data.get("live_update_task") is asyncio.current_task():
            context.user_data["live_update_task"] = None


# ============================================================
# Clear previous match cards
# ============================================================

async def _clear_previous_match_messages(query, context: ContextTypes.DEFAULT_TYPE) -> None:

    await _stop_live_update_loop(context)

    old_ids = context.user_data.get("match_message_ids", [])
    chat_id = query.message.chat_id

    for message_id in old_ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramError:
            pass

    context.user_data["match_message_ids"] = []


# ============================================================
# Error handler
# ============================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception:", exc_info=context.error)


# ============================================================
# Main
# ============================================================

def main() -> None:

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_sport_selection, pattern=r"^sport:"))
    application.add_handler(CallbackQueryHandler(handle_sport_variant_selection, pattern=r"^sport_variant:"))
    application.add_handler(CallbackQueryHandler(handle_tennis_tour_selection, pattern=r"^tennis_tour:"))
    application.add_handler(CallbackQueryHandler(handle_sport_period_selection, pattern=r"^sport_period:"))
    application.add_handler(CallbackQueryHandler(handle_period_selection, pattern=r"^period:"))
    application.add_handler(CallbackQueryHandler(handle_league_selection, pattern=r"^league:"))
    application.add_handler(CallbackQueryHandler(handle_back_to_leagues, pattern=r"^back_to_leagues$"))
    application.add_handler(CallbackQueryHandler(handle_top_matches, pattern=r"^top_matches$"))
    application.add_handler(CallbackQueryHandler(handle_refresh, pattern=r"^refresh$"))
    application.add_handler(CallbackQueryHandler(handle_back, pattern=r"^back_to_previous$"))
    application.add_handler(CallbackQueryHandler(handle_back_to_start, pattern=r"^back_to_start$"))
    application.add_error_handler(error_handler)

    logger.info("Multi-sport bot is starting...")
    application.run_polling()


if __name__ == "__main__":
    main()
    
