"""
bot.py

Football Telegram Bot

Features:
- Today's matches
- Tomorrow's matches
- This week's matches
- This month's matches
- League selection
- All Top Leagues
- 🔥 Top Matches
- Small team logos inside one compact scoreboard
- Back navigation
- Refresh
- football-data.org API

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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw, ImageFont

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


# ============================================================
# /start
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    context.user_data.clear()

    await update.message.reply_text(
        "⚽ MATCHRADAR\n\n"
        "🏆 Choose a league\n\n"
        "Football data by 5DollarFootballAPI",
        reply_markup=_league_keyboard(),
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

def _league_keyboard() -> InlineKeyboardMarkup:

    keyboard = []
    for key, league in api.LEAGUES.items():
        keyboard.append(
            [InlineKeyboardButton(f"{league['flag']} {league['name']}", callback_data=f"league:{key}")]
        )

    keyboard.append([InlineKeyboardButton("🌍 All Top Leagues", callback_data="league:all")])
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="back_to_start")])
    return InlineKeyboardMarkup(keyboard)


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
    await query.edit_message_text("🏆 Choose a league", reply_markup=_league_keyboard())


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
    height = 430

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

    odds_data = match.get("odds_1x2") or {}
    odds_home = odds_data.get("home")
    odds_draw = odds_data.get("draw")
    odds_away = odds_data.get("away")

    if any(value is not None for value in (odds_home, odds_draw, odds_away)):
        odds_label_font = _get_font(14, bold=True)
        odds_value_font = _get_font(20, bold=True)

        draw.line((120, 305, 780, 305), fill=(37, 61, 63), width=1)

        source = str(odds_data.get("bookmaker", "")).strip()
        label = "MATCH RESULT"
        if source and source != "consensus":
            label = f"MATCH RESULT • {source.upper()}"

        centered_text(label, 450, 316, odds_label_font, (139, 166, 163))

        def odds_text(label: str, value):
            return f"{label}  {value:.2f}" if value is not None else f"{label}  —"

        odds_parts = [
            (odds_text("1", odds_home), 260),
            (odds_text("X", odds_draw), 450),
            (odds_text("2", odds_away), 640),
        ]
        for text, center_x in odds_parts:
            centered_text(text, center_x, 340, odds_value_font, (241, 247, 245))

    output = io.BytesIO()
    output.name = "match_card.png"
    image.save(output, format="PNG", optimize=True)
    output.seek(0)
    return output


# ============================================================
# Download / prepare team logo
# ============================================================

def _download_logo(url: str | None):
    if not url:
        return None
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return Image.open(io.BytesIO(response.content)).convert("RGBA")
    except Exception as exc:
        logger.warning("Could not download logo: %s", exc)
        return None


def _prepare_logo(logo: Image.Image, size: int) -> Image.Image:
    logo = logo.copy()
    logo.thumbnail((size, size), Image.Resampling.LANCZOS)

    canvas = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    x = (size - logo.width) // 2
    y = (size - logo.height) // 2
    canvas.paste(logo, (x, y), logo)
    return canvas


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
        await _edit_navigation_message(
            query, context, "⚽ MATCHRADAR\n\n🏆 Choose a league", reply_markup=_league_keyboard(),
        )
        context.user_data.clear()
        return

    await _clear_previous_match_messages(query, context)
    await _edit_navigation_message(
        query, context, "⚽ MATCHRADAR\n\n🏆 Choose a league", reply_markup=_league_keyboard(),
    )
    context.user_data.clear()


async def handle_back_to_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    query = update.callback_query
    await query.answer()

    await _clear_previous_match_messages(query, context)
    await _edit_navigation_message(
        query, context, "⚽ MATCHRADAR\n\n🏆 Choose a league", reply_markup=_league_keyboard(),
    )
    context.user_data.clear()


# ============================================================
# Refresh
# ============================================================

async def handle_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    query = update.callback_query
    await query.answer("Refreshing...")

    if not context.user_data.get("mode"):
        await query.answer("Please start again with /start.", show_alert=True)
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
    """Poll for live games and games kicking off soon.

    Important: this must NOT require a match to already be LIVE at
    screen-load time — a match that is still NS but kicks off in the next
    couple of hours needs polling too, otherwise its SCHEDULED -> LIVE
    transition (and any postponement) would never be picked up.
    """
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


async def _start_live_update_loop(context: ContextTypes.DEFAULT_TYPE) -> None:
    await _stop_live_update_loop(context)

    matches = context.user_data.get("live_match_snapshot") or []
    should_poll = _should_poll_live_matches(matches)
    logger.info(
        "[LIVE] updater start check: screen=%s matches=%d should_poll=%s",
        context.user_data.get("screen"), len(matches), should_poll,
    )
    if not should_poll:
        logger.info("[LIVE] updater NOT started: no live/near-kickoff match")
        return

    context.user_data["live_update_task"] = asyncio.create_task(_live_score_update_loop(context))
    logger.info("[LIVE] updater task CREATED")


async def _live_score_update_loop(context: ContextTypes.DEFAULT_TYPE) -> None:
    # Default is 120s so the free 5Dollar plan keeps enough hourly quota for
    # initial odds requests. Set LIVE_SCORE_REFRESH_SECONDS=60 in .env if you
    # deliberately want one live poll per minute.
    refresh_seconds = max(60, int(getattr(api, "LIVE_SCORE_REFRESH_SECONDS", 120)))
    logger.info("[LIVE] updater loop running: interval=%ss", refresh_seconds)

    first_cycle = True

    try:
        while context.user_data.get("screen") == "matches":
            if not first_cycle:
                await asyncio.sleep(refresh_seconds)
            first_cycle = False

            if context.user_data.get("screen") != "matches":
                break

            matches = context.user_data.get("live_match_snapshot") or []
            message_ids = context.user_data.get("live_card_message_ids") or []
            if not matches or not message_ids:
                logger.info("[LIVE] updater stopping: no matches/message ids")
                break

            logger.info("[LIVE] cycle: checking %d visible card(s)", len(matches))

            # Final UI-side safety net. Do this BEFORE api.refresh_live_scores
            # so a stale LIVE card becomes a changed card even if api.py or
            # the external status provider still reports LIVE.
            bot_guard_changed = set()
            for idx, match in enumerate(matches):
                if _force_stale_live_status(match):
                    bot_guard_changed.add(idx)

            changed_indexes = await asyncio.to_thread(api.refresh_live_scores, matches)
            changed_indexes = set(changed_indexes) | bot_guard_changed
            logger.info("[LIVE] cycle result: changed=%s", sorted(changed_indexes))

            # IMPORTANT: render the changed cards BEFORE deciding whether the
            # polling loop should stop.  A LIVE -> DELAYED / FINISHED /
            # CANCELLED transition can make _should_poll_live_matches() false;
            # if we break first, Telegram never receives the final state.
            should_continue = _should_poll_live_matches(matches)

            if not changed_indexes:
                if not should_continue:
                    break
                continue

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
                    logger.info("Live card updated: message_id=%s index=%s", message_id, index)
                except TelegramError:
                    logger.exception("Could not update live card message %s", message_id)

            if not should_continue:
                logger.info("[LIVE] updater stopping: no live/near-kickoff matches remain")
                break
    except asyncio.CancelledError:
        logger.info("[LIVE] updater cancelled")
        raise
    except Exception:
        logger.exception("[LIVE] updater loop failed")
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
    application.add_handler(CallbackQueryHandler(handle_period_selection, pattern=r"^period:"))
    application.add_handler(CallbackQueryHandler(handle_league_selection, pattern=r"^league:"))
    application.add_handler(CallbackQueryHandler(handle_back_to_leagues, pattern=r"^back_to_leagues$"))
    application.add_handler(CallbackQueryHandler(handle_top_matches, pattern=r"^top_matches$"))
    application.add_handler(CallbackQueryHandler(handle_refresh, pattern=r"^refresh$"))
    application.add_handler(CallbackQueryHandler(handle_back, pattern=r"^back_to_previous$"))
    application.add_handler(CallbackQueryHandler(handle_back_to_start, pattern=r"^back_to_start$"))
    application.add_error_handler(error_handler)

    logger.info("Football bot is starting...")
    application.run_polling()


if __name__ == "__main__":
    main()