"""
api.py

Football API functions for football-data.org, plus fast live score/odds
enrichment from 5DollarFootballAPI.

ARCHITECTURE (read this before touching the live-update code)
----------------------------------------------------------------
Two providers are involved and they are given very different authority:

- football-data.org is the SINGLE SOURCE OF TRUTH for whether a fixture is
  happening at all: NOT STARTED / LIVE / HALFTIME / FINISHED / POSTPONED /
  SUSPENDED / CANCELLED. Every status ever shown to the user traces back to
  this provider, either from the normal fixture fetch or from a targeted
  `verify_fixture_status()` call.

- 5DollarFootballAPI (the "odds" / "live" provider) is used ONLY to refine
  the score and minute of a fixture that football-data.org has ALREADY put
  in the live family (LIVE/HT). It is never allowed to invent a live match,
  and it is never allowed to postpone/cancel one — only football-data.org
  can do that. This is what stops a postponed match from ever showing as
  "LIVE 0-0": the fast provider simply isn't consulted for status on a
  fixture football-data.org hasn't confirmed as live.

- Scores are always taken from the freshest reliable number available (the
  fixture's current aggregate `goals` field), never blended with an older
  snapshot via something like max(new, old). `period_score` events are
  informational only (they describe a single half) and are never used to
  set the running score — mixing them into the total is what previously
  caused a live match to get stuck on a stale score like 2-1 while the
  real score was already 3-1.

All match statuses used anywhere in this codebase are one of exactly:
NS, HT, LIVE, FT, PST, SUSP, CANC, UNKNOWN — see `_convert_status()`. Every
other function in this file and in bot.py can rely on that closed set
instead of re-guessing aliases like "TIMED"/"SCHEDULED_TIME"/"POSTPONED".
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import difflib
import html
import os
import re
import threading
import time
import unicodedata
from functools import lru_cache
from urllib.parse import quote, urlsplit, urlunsplit

import requests
from dotenv import load_dotenv

from config import TIMEZONE

# Non-football API runtime constants (needed by NBA/NHL/Tennis/F1 section)
YEREVAN_TZ = ZoneInfo(TIMEZONE)
UTC_TZ = ZoneInfo("UTC")
TIMEOUT = int(os.getenv("SPORT_API_TIMEOUT", "20"))


# ============================================================
# CONFIG
# ============================================================

load_dotenv()

FOOTBALL_DATA_TOKEN = os.getenv("FOOTBALL_DATA_TOKEN")

BASE_URL = "https://api.football-data.org/v4"

HEADERS = {
    "X-Auth-Token": FOOTBALL_DATA_TOKEN,
}


# ============================================================
# LEAGUES
# ============================================================

LEAGUES = {
    "premier_league": {"id": "PL", "name": "Premier League", "flag": "🇬🇧"},
    "la_liga": {"id": "PD", "name": "La Liga", "flag": "🇪🇸"},
    "serie_a": {"id": "SA", "name": "Serie A", "flag": "🇮🇹"},
    "bundesliga": {"id": "BL1", "name": "Bundesliga", "flag": "🇩🇪"},
    "ligue_1": {"id": "FL1", "name": "Ligue 1", "flag": "🇫🇷"},
    "champions_league": {"id": "CL", "name": "Champions League", "flag": "🇪🇺"},
    "europa_league": {"id": "EL", "name": "Europa League", "flag": "🟠"},
    "conference_league": {"id": "ECL", "name": "Conference League", "flag": "🟢"},
}


# ============================================================
# TOP 25 TEAMS
# ============================================================

TOP_TEAMS = {
    "Arsenal", "Bayern Munich", "Manchester City", "Paris Saint-Germain",
    "Liverpool", "Real Madrid", "Barcelona", "Inter Milan", "Chelsea",
    "Bayer Leverkusen", "Aston Villa", "Atlético Madrid", "Borussia Dortmund",
    "Newcastle United", "Manchester United", "Atalanta", "Napoli", "Juventus",
    "Sporting CP", "Benfica", "AC Milan", "RB Leipzig", "Tottenham Hotspur",
    "AS Roma", "Villarreal",
}


# ============================================================
# TOP TEAM ALIASES
# ============================================================

TOP_TEAM_ALIASES = {
    "arsenal": "Arsenal",
    "arsenal fc": "Arsenal",

    "bayern munich": "Bayern Munich",
    "fc bayern munich": "Bayern Munich",
    "bayern münchen": "Bayern Munich",
    "fc bayern münchen": "Bayern Munich",

    "manchester city": "Manchester City",
    "manchester city fc": "Manchester City",

    "paris saint-germain": "Paris Saint-Germain",
    "paris saint-germain fc": "Paris Saint-Germain",
    "psg": "Paris Saint-Germain",
    "paris sg": "Paris Saint-Germain",

    "liverpool": "Liverpool",
    "liverpool fc": "Liverpool",

    "real madrid": "Real Madrid",
    "real madrid cf": "Real Madrid",

    "barcelona": "Barcelona",
    "fc barcelona": "Barcelona",

    "inter milan": "Inter Milan",
    "inter": "Inter Milan",
    "inter milano": "Inter Milan",
    "fc internazionale milano": "Inter Milan",
    "internazionale": "Inter Milan",

    "chelsea": "Chelsea",
    "chelsea fc": "Chelsea",

    "bayer leverkusen": "Bayer Leverkusen",
    "bayer 04 leverkusen": "Bayer Leverkusen",

    "aston villa": "Aston Villa",
    "aston villa fc": "Aston Villa",

    "atlético madrid": "Atlético Madrid",
    "atletico madrid": "Atlético Madrid",
    "club atlético de madrid": "Atlético Madrid",
    "club atletico de madrid": "Atlético Madrid",

    "borussia dortmund": "Borussia Dortmund",
    "bvb": "Borussia Dortmund",

    "newcastle united": "Newcastle United",
    "newcastle united fc": "Newcastle United",

    "manchester united": "Manchester United",
    "manchester united fc": "Manchester United",

    "atalanta": "Atalanta",
    "atalanta bc": "Atalanta",

    "napoli": "Napoli",
    "ssc napoli": "Napoli",

    "juventus": "Juventus",
    "juventus fc": "Juventus",

    "sporting cp": "Sporting CP",
    "sporting lisbon": "Sporting CP",
    "sporting clube de portugal": "Sporting CP",

    "benfica": "Benfica",
    "sl benfica": "Benfica",
    "sport lisboa e benfica": "Benfica",

    "ac milan": "AC Milan",
    "milan": "AC Milan",
    "ac milan spa": "AC Milan",

    "rb leipzig": "RB Leipzig",
    "rasenballsport leipzig": "RB Leipzig",

    "tottenham hotspur": "Tottenham Hotspur",
    "tottenham hotspur fc": "Tottenham Hotspur",
    "tottenham": "Tottenham Hotspur",

    "as roma": "AS Roma",
    "roma": "AS Roma",
    "associazione sportiva roma": "AS Roma",

    "villarreal": "Villarreal",
    "villarreal cf": "Villarreal",
}


# ============================================================
# COMPETITION DISPLAY NAMES
# ============================================================

COMPETITION_NAMES = {
    "PL": "Premier League",
    "PD": "La Liga",
    "SA": "Serie A",
    "BL1": "Bundesliga",
    "FL1": "Ligue 1",
    "CL": "Champions League",
    "EL": "Europa League",
    "ECL": "Conference League",
}


# ============================================================
# ERRORS
# ============================================================

class FootballAPIError(Exception):
    """Raised when the football-data.org request fails."""
    pass


class OddsAPIError(Exception):
    """Raised when the 5DollarFootballAPI request fails."""
    pass


# ============================================================
# CANONICAL STATUS VOCABULARY
# ============================================================
# Every status_short in this codebase is one of these seven values, always
# produced by _convert_status(). Nothing else should invent new status
# strings — that duplication (matching against ad-hoc alias sets in
# multiple places) is what made the old status handling fragile.

def is_pre_match(status_short) -> bool:
    return str(status_short).upper() in {"NS", "TBD"}


def is_live_family(status_short) -> bool:
    return str(status_short).upper() in {"LIVE", "HT"}


def is_finished(status_short) -> bool:
    return str(status_short).upper() == "FT"


def is_postponed(status_short) -> bool:
    return str(status_short).upper() == "PST"


def is_cancelled(status_short) -> bool:
    return str(status_short).upper() == "CANC"


def is_suspended(status_short) -> bool:
    return str(status_short).upper() == "SUSP"


_STATUS_EMOJI = {
    "NS": "🕐", "HT": "⏸️", "LIVE": "🔴", "FT": "🟢",
    "PST": "⏳", "CANC": "❌", "SUSP": "⛔",
}


def status_display_label(status_short: str, minute: str | None = None) -> str:
    code = str(status_short).upper()
    if code in {"NS", "TBD"}:
        return "NOT STARTED"
    if code == "HT":
        return "HALFTIME"
    if code == "LIVE":
        return f"LIVE — {minute}'" if minute else "LIVE"
    if code == "FT":
        return "FINISHED"
    if code == "PST":
        return "DELAYED"
    if code == "CANC":
        return "CANCELLED"
    if code == "SUSP":
        return "SUSPENDED"
    return code


# ============================================================
# TEAM DISPLAY NAME NORMALIZATION
# ============================================================

def normalize_team_display_name(name: str) -> str:
    """Normalize the club-suffix style for display only.

    "Real Madrid CF" -> "Real Madrid FC", "Villarreal CF" -> "Villarreal FC",
    "Valencia CF" -> "Valencia FC". Names that already end in "FC" (e.g.
    "Chelsea FC") are left untouched so they never become "FC FC". This is
    idempotent, so it's safe to call more than once on the same name.
    """
    cleaned = " ".join(str(name).strip().split())
    if cleaned[-3:].upper() == " CF":
        cleaned = cleaned[:-3].rstrip() + " FC"
    return cleaned


# ============================================================
# BASIC API REQUEST (football-data.org)
# ============================================================

def _request(endpoint: str, params: dict | None = None) -> list:

    url = f"{BASE_URL}{endpoint}"

    try:
        response = requests.get(url, headers=HEADERS, params=params, timeout=10)
    except requests.exceptions.RequestException as exc:
        raise FootballAPIError(f"Could not reach the football API: {exc}")

    if response.status_code == 401:
        raise FootballAPIError("Invalid football-data.org API token.")

    if response.status_code == 403:
        raise FootballAPIError("Your football-data.org plan does not allow this request.")

    if response.status_code == 429:
        reset_raw = response.headers.get("X-RequestCounter-Reset")
        available_raw = response.headers.get("X-Requests-Available-Minute")

        try:
            reset_seconds = max(1, int(float(reset_raw))) if reset_raw else 60
        except (TypeError, ValueError):
            reset_seconds = 60

        reset_at = datetime.now(ZoneInfo(TIMEZONE)) + timedelta(seconds=reset_seconds)
        reset_text = reset_at.strftime("%H:%M:%S")
        available_text = available_raw if available_raw is not None else "0"

        print(
            "[FOOTBALL-DATA] RATE LIMIT REACHED — "
            f"reset in {reset_seconds}s (~{reset_seconds / 60:.1f} min). "
            f"Reset at {reset_text}. Requests available: {available_text}."
        )

        raise FootballAPIError(
            "API rate limit reached. "
            f"Reset in {reset_seconds}s (~{reset_seconds / 60:.1f} min), at {reset_text}."
        )

    if response.status_code != 200:
        raise FootballAPIError(f"Football API returned status {response.status_code}.")

    data = response.json()
    return data.get("matches", [])


def _request_competition(competition_code: str, date_from: str, date_to: str) -> list:
    endpoint = f"/competitions/{competition_code}/matches"
    params = {"dateFrom": date_from, "dateTo": date_to}
    return _request(endpoint, params)


def _convert_to_local_time(iso_date_string: str) -> datetime:
    utc_dt = datetime.fromisoformat(iso_date_string.replace("Z", "+00:00"))
    return utc_dt.astimezone(ZoneInfo(TIMEZONE))


# ============================================================
# FIXTURES
# ============================================================

def get_fixtures_by_date_range(league_id: str, date_from: str, date_to: str) -> list:
    if league_id in {"EL", "ECL"}:
        return _get_propline_fixtures_by_date_range(league_id, date_from, date_to)
    raw_matches = _request_competition(league_id, date_from, date_to)
    return [_parse_fixture(match) for match in raw_matches]


def get_fixtures_all_leagues(date_from: str, date_to: str) -> list:
    all_fixtures = []
    for league in LEAGUES.values():
        all_fixtures.extend(get_fixtures_by_date_range(league["id"], date_from, date_to))
    return all_fixtures


def _get_propline_fixtures_by_date_range(league_id: str, date_from: str, date_to: str) -> list:
    if not PROP_LINE_API_KEY:
        print(f"[PROPLINE] {league_id}: PROP_LINE_API_KEY is missing")
        return []
    sport = PROP_LINE_SPORTS.get(league_id)
    if not sport:
        return []
    try:
        events = _request_propline(f"/sports/{sport}/events") or []
    except OddsAPIError as exc:
        print(f"[PROPLINE] {league_id}: fixture request failed — {exc}")
        return []
    if isinstance(events, dict):
        events = events.get("data") or events.get("events") or []
    start = datetime.strptime(date_from, "%Y-%m-%d").date()
    end = datetime.strptime(date_to, "%Y-%m-%d").date()
    return [_parse_propline_fixture(e, league_id) for e in events if _propline_event_in_range(e, start, end)]


def _propline_event_in_range(event: dict, start, end) -> bool:
    commence = event.get("commence_time")
    if not commence:
        return False
    try:
        dt = datetime.fromisoformat(str(commence).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        local_date = dt.astimezone(ZoneInfo(TIMEZONE)).date()
        return start <= local_date <= end
    except (TypeError, ValueError):
        return False


def _parse_propline_fixture(event: dict, league_id: str) -> dict:
    dt = datetime.fromisoformat(str(event["commence_time"]).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    local_dt = dt.astimezone(ZoneInfo(TIMEZONE))
    status = "LIVE" if event.get("live") else "NS"
    return {
        "league_id": league_id, "league_name": COMPETITION_NAMES.get(league_id, league_id),
        "date": local_dt.strftime("%d %B"), "date_iso": local_dt.strftime("%Y-%m-%d"),
        "time": local_dt.strftime("%H:%M"),
        "home_team": normalize_team_display_name(event.get("home_team", "Unknown Team")),
        "away_team": normalize_team_display_name(event.get("away_team", "Unknown Team")),
        "home_logo": event.get("home_team_logo_url"), "away_logo": event.get("away_team_logo_url"),
        "home_team_id": event.get("home_team_id"), "away_team_id": event.get("away_team_id"),
        "status_short": status, "source_status_short": status,
        "status_long": "IN_PLAY" if event.get("live") else "SCHEDULED",
        "elapsed": None, "minute": None, "goals_home": None, "goals_away": None,
        "half_time_home": None, "half_time_away": None,
        "match_id": event.get("id"), "utc_date": event.get("commence_time"),
    }


def _parse_fixture(raw: dict) -> dict:

    competition = raw.get("competition", {})
    home_team = raw.get("homeTeam", {})
    away_team = raw.get("awayTeam", {})
    score = raw.get("score", {})
    full_time = score.get("fullTime", {})
    half_time = score.get("halfTime", {})

    local_dt = _convert_to_local_time(raw["utcDate"])
    competition_code = competition.get("code")

    # football-data.org returns PD as "Primera Division"; we display "La Liga".
    league_name = COMPETITION_NAMES.get(
        competition_code, competition.get("name", "Unknown Competition")
    )

    status_short = _convert_status(raw.get("status"))
    minute = raw.get("minute")

    return {
        "league_id": competition_code,
        "league_name": league_name,

        "date": local_dt.strftime("%d %B"),
        "date_iso": local_dt.strftime("%Y-%m-%d"),
        "time": local_dt.strftime("%H:%M"),

        "home_team": normalize_team_display_name(home_team.get("name", "Unknown Team")),
        "away_team": normalize_team_display_name(away_team.get("name", "Unknown Team")),
        "home_logo": home_team.get("crest"),
        "away_logo": away_team.get("crest"),
        "home_team_id": home_team.get("id"),
        "away_team_id": away_team.get("id"),

        "status_short": status_short,
        # football-data.org remains authoritative for postponements/
        # cancellations; the fast live provider is only ever allowed to
        # refine score/minute, never to change this.
        "source_status_short": status_short,
        "status_long": raw.get("status", "UNKNOWN"),

        # Both keys are kept in sync so every consumer can rely on either
        # one; the old code stored the initial minute under "elapsed" but
        # live refreshes wrote to "minute", which meant a freshly refreshed
        # minute was silently ignored wherever "elapsed" was read instead.
        "elapsed": minute,
        "minute": minute,

        "goals_home": full_time.get("home"),
        "goals_away": full_time.get("away"),
        "half_time_home": half_time.get("home"),
        "half_time_away": half_time.get("away"),

        "match_id": raw.get("id"),
        "utc_date": raw.get("utcDate"),
    }


def _convert_status(status: str | None) -> str:

    status_mapping = {
        "SCHEDULED": "NS",
        "TIMED": "NS",
        "IN_PLAY": "LIVE",
        "LIVE": "LIVE",
        "PAUSED": "HT",
        "FINISHED": "FT",
        "POSTPONED": "PST",
        "SUSPENDED": "SUSP",
        "CANCELLED": "CANC",
    }

    return status_mapping.get(status, status or "UNKNOWN")


def format_match_status(match: dict) -> str:
    code = str(match.get("status_short", "UNKNOWN")).upper()
    minute = match.get("minute") or match.get("elapsed")
    label = status_display_label(code, minute)
    emoji = _STATUS_EMOJI.get(code, "⚽")
    return f"{emoji} {label}"


def format_match_message(match: dict, league_flag: str = "⚽") -> str:

    status_line = format_match_status(match)
    code = str(match.get("status_short", "")).upper()

    if is_pre_match(code):
        score_line = f"{match['home_team']} 🆚 {match['away_team']}"
    else:
        home_goals = match["goals_home"] if match["goals_home"] is not None else 0
        away_goals = match["goals_away"] if match["goals_away"] is not None else 0
        score_line = f"{match['home_team']} {home_goals} - {away_goals} {match['away_team']}"

    return (
        f"{league_flag} {match['league_name'].upper()}\n\n"
        f"📅 {match['date']}\n"
        f"⏰ {match['time']}\n\n"
        f"{status_line}\n\n"
        f"{score_line}"
    )


# ============================================================
# TOP TEAM MATCHING
# ============================================================

def _normalize_team_name(team_name: str) -> str:
    name = team_name.strip().lower()
    name = name.replace(".", "").replace(",", "")
    return " ".join(name.split())


def _get_top_team_name(team_name: str) -> str | None:
    normalized = _normalize_team_name(team_name)

    if normalized in TOP_TEAM_ALIASES:
        return TOP_TEAM_ALIASES[normalized]

    for top_team in TOP_TEAMS:
        if normalized == _normalize_team_name(top_team):
            return top_team

    for top_team in TOP_TEAMS:
        top_normalized = _normalize_team_name(top_team)
        if top_normalized in normalized or normalized in top_normalized:
            return top_team

    return None


def _is_top_match(match: dict) -> bool:
    home_top_team = _get_top_team_name(match["home_team"])
    away_top_team = _get_top_team_name(match["away_team"])
    return home_top_team is not None or away_top_team is not None


def get_top_matches(date_from: str, date_to: str) -> list:
    """Top Matches: included if at least one team is from our Top 25 list."""

    matches = get_fixtures_all_leagues(date_from, date_to)
    top_matches = [match for match in matches if _is_top_match(match)]

    unique_matches = {}
    for match in top_matches:
        match_id = match.get("match_id")
        if match_id is not None:
            unique_matches[match_id] = match
    top_matches = list(unique_matches.values())

    competition_priority = {"CL": 100, "EL": 98, "ECL": 96, "PL": 90, "PD": 85, "SA": 80, "BL1": 75, "FL1": 70}

    top_matches.sort(
        key=lambda match: (
            -competition_priority.get(match["league_id"], 0),
            match["date_iso"],
            match["time"],
        )
    )

    return top_matches[:10]


# ============================================================
# 5DOLLARFOOTBALLAPI — REAL BET365 1X2 ODDS
# ============================================================

ODDS_API_KEY = os.getenv("ODDS_API_KEY")
ODDS_BOOKMAKER = os.getenv("ODDS_BOOKMAKER", "bet365").strip().lower() or "bet365"
ODDS_BASE_URL = "https://api.5dollarfootballapi.com/v1"
LIVE_SCORE_REFRESH_SECONDS = int(
    os.getenv("LIVE_SCORE_REFRESH_SECONDS", os.getenv("LIVE_REFRESH_SECONDS", "60"))
)

PROP_LINE_API_KEY = os.getenv("PROP_LINE_API_KEY")

# Optional second odds source for UEFA matches that PropLine does not expose.
# Add THE_ODDS_API_KEY to .env to enable this fallback.
THE_ODDS_API_KEY = os.getenv("THE_ODDS_API_KEY")
THE_ODDS_API_BASE_URL = "https://api.the-odds-api.com/v4"
THE_ODDS_SPORTS = {
    "CL": "soccer_uefa_champs_league",
    "EL": "soccer_uefa_europa_league",
    "ECL": "soccer_uefa_europa_conference_league",
}
PROP_LINE_BASE_URL = "https://api.prop-line.com/v1"
PROP_LINE_LIVE_INTERVAL = int(os.getenv("PROP_LINE_LIVE_INTERVAL", "120"))
_PROP_LINE_LAST_LIVE_FETCH = 0.0
PROP_LINE_SPORTS = {
    "CL": "soccer_uefa_champions_league",
    "EL": "soccer_uefa_europa_league",
    "ECL": "soccer_uefa_conference_league",
}

# Free plan coverage: Premier League, La Liga, Serie A, Bundesliga, Ligue 1.
ODDS_LEAGUE_NAMES = {
    "PL": "Premier League", "PD": "La Liga", "SA": "Serie A",
    "BL1": "Bundesliga", "FL1": "Ligue 1",
}

_odds_league_ids: dict = {}


def _strip_accents(value: str) -> str:
    """Fold accented characters to their plain ASCII form for matching.

    NOTE: only used for the odds-matching key — never for display. A card
    still shows "Málaga FC"; only the internal join key becomes "malaga".
    """
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


_ODDS_TEAM_ALIASES = {
    # UEFA teams with common provider-name variations.
    "racing lens": "lens",
    "rc lens": "lens",
    "lens": "lens",
    "sporting clube portugal": "sporting",
    "sporting cp": "sporting",
    "sporting lisbon": "sporting",
    "sabah": "sabah",
    "sabah fk": "sabah",
    "slavia praha": "slavia prague",
    "slavia prague": "slavia prague",
    "atletico madrid": "atletico madrid",
    "manchester united": "manchester united",
    "man united": "manchester united",
    "manchester city": "manchester city",
    "man city": "manchester city",
    "paris saint germain": "psg",
    "paris saint germain fc": "psg",
    "psg": "psg",
    "shakhtar donetsk": "shakhtar donetsk",
    "aek": "aek",
    "aek athens": "aek",
    "bodo glimt": "bodo glimt",
    "borussia dortmund": "borussia dortmund",
    "fenerbahce": "fenerbahce",
    "stuttgart": "stuttgart",
    "slovan bratislava": "slovan bratislava",
    "porto": "porto",
    "psv": "psv",
    "napoli": "napoli",
    "liverpool": "liverpool",
    "real madrid": "real madrid",
    "rb leipzig": "rb leipzig",
    "bayern munich": "bayern munich",
    "arsenal": "arsenal",
    "villarreal": "villarreal",
    "inter milan": "inter milan",
    "internazionale milano": "inter milan",
    "fc internazionale milano": "inter milan",
    "galatasaray": "galatasaray",
    "barcelona": "barcelona",
    "aston villa": "aston villa",
    "real betis": "real betis",
    "feyenoord": "feyenoord",
    "como": "como",
    "lask linz": "lask",
    "lask": "lask",
}

def _normalize_odds_team_name(team_name: str | None) -> str:
    if not team_name:
        return ""
    # IMPORTANT: strip accents BEFORE the alnum regex.
    value = _strip_accents(str(team_name)).lower()
    value = re.sub(r"[^a-z0-9]+", " ", value).strip()
    tokens = [
        token for token in value.split()
        if token not in {
            "fc", "cf", "afc", "sc", "ac", "cd", "rcd", "de", "del",
            "da", "dos", "do", "the", "club",
        }
    ]
    normalized = " ".join(tokens)
    return _ODDS_TEAM_ALIASES.get(normalized, normalized)


def _decimal_odds(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 2) if number >= 1.0 else None


def _safe_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _request_odds_api(path: str, params: dict | None = None):
    if not ODDS_API_KEY:
        return None

    headers = {"Authorization": f"Bearer {ODDS_API_KEY}", "Accept": "application/json"}

    try:
        response = requests.get(
            f"{ODDS_BASE_URL}{path}", headers=headers, params=params or {}, timeout=12,
        )
    except requests.exceptions.RequestException as exc:
        raise OddsAPIError(f"Could not reach 5DollarFootballAPI: {exc}") from exc

    if response.status_code == 401:
        raise OddsAPIError("Invalid 5DollarFootballAPI key.")
    if response.status_code == 403:
        raise OddsAPIError("5DollarFootballAPI returned 403. Check your free-plan coverage/key.")
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        reset_raw = response.headers.get("X-RateLimit-Reset")
        wait_seconds = None

        if retry_after:
            try:
                wait_seconds = max(1, int(float(retry_after)))
            except (TypeError, ValueError):
                pass

        if wait_seconds is None and reset_raw:
            try:
                reset_value = float(reset_raw)
                wait_seconds = (
                    max(1, int(reset_value - time.time()))
                    if reset_value > time.time()
                    else max(1, int(reset_value))
                )
            except (TypeError, ValueError):
                pass

        if wait_seconds is None:
            wait_seconds = 3600

        raise OddsAPIError(
            f"5DollarFootballAPI rate limit reached. Retry in {wait_seconds}s "
            f"(~{wait_seconds / 60:.1f} min)."
        )
    if response.status_code != 200:
        try:
            payload = response.json()
            message = payload.get("message") or payload.get("error")
        except Exception:
            message = None
        raise OddsAPIError(
            f"5DollarFootballAPI HTTP {response.status_code}" + (f": {message}" if message else "")
        )

    payload = response.json()
    if payload.get("success") in (0, False):
        raise OddsAPIError(
            payload.get("message") or payload.get("error") or "5DollarFootballAPI request failed."
        )
    return payload.get("data")


def _load_odds_league_ids() -> dict:
    """Return the current 5DollarFootballAPI league IDs used by the free plan."""
    global _odds_league_ids
    if _odds_league_ids or not ODDS_API_KEY:
        return _odds_league_ids

    _odds_league_ids = {
        "PL": 4160026622,
        "PD": 4212821298,
        "SA": 3405541143,
        "BL1": 686337048,
        "FL1": 3614399544,
    }
    return _odds_league_ids


def _unix_range_for_dates(date_from: str, date_to: str):
    start_local = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=ZoneInfo(TIMEZONE))
    end_local = (datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)).replace(
        tzinfo=ZoneInfo(TIMEZONE)
    )
    return int(start_local.timestamp()), int(end_local.timestamp())


def _same_team(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True

    aa, bb = set(a.split()), set(b.split())
    if aa and bb and len(aa & bb) >= max(1, min(len(aa), len(bb)) - 1):
        return True

    # Fallback for naming differences that don't share a clean token (rare
    # transliteration/spelling quirks). A high similarity ratio catches
    # these without risking a false match between two different clubs.
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.86


def _events_match_fixture(event: dict, match: dict) -> bool:
    teams = event.get("teams") or {}
    home_obj = teams.get("home") or {}
    away_obj = teams.get("away") or {}
    home_event = _normalize_odds_team_name(home_obj.get("name"))
    away_event = _normalize_odds_team_name(away_obj.get("name"))
    home_match = _normalize_odds_team_name(match.get("home_team"))
    away_match = _normalize_odds_team_name(match.get("away_team"))

    if not (_same_team(home_event, home_match) and _same_team(away_event, away_match)):
        return False

    starts_at = event.get("kickoff_utc") or event.get("kickoff")
    utc_date = match.get("utc_date")
    if starts_at and utc_date:
        try:
            event_dt = datetime.fromisoformat(str(starts_at).replace("Z", "+00:00"))
            match_dt = datetime.fromisoformat(str(utc_date).replace("Z", "+00:00"))
            if event_dt.tzinfo is None:
                event_dt = event_dt.replace(tzinfo=ZoneInfo("UTC"))
            if match_dt.tzinfo is None:
                match_dt = match_dt.replace(tzinfo=ZoneInfo("UTC"))
            if abs((event_dt - match_dt).total_seconds()) > 12 * 3600:
                return False
        except (TypeError, ValueError):
            pass

    return True


def _extract_1x2(odds_block: dict | None):
    if not isinstance(odds_block, dict):
        return None
    one_x_two = odds_block.get("1x2") or {}
    if not isinstance(one_x_two, dict):
        return None

    for stage_name in ("inplay", "current", "closing", "opening"):
        stage = one_x_two.get(stage_name)
        if not isinstance(stage, dict):
            continue
        home = _decimal_odds(stage.get("home"))
        draw = _decimal_odds(stage.get("draw"))
        away = _decimal_odds(stage.get("away"))
        if any(v is not None for v in (home, draw, away)):
            return {"home": home, "draw": draw, "away": away}

    home = _decimal_odds(one_x_two.get("home"))
    draw = _decimal_odds(one_x_two.get("draw"))
    away = _decimal_odds(one_x_two.get("away"))
    if any(v is not None for v in (home, draw, away)):
        return {"home": home, "draw": draw, "away": away}
    return None


def _request_league_fixtures(league_id, date_from: str, date_to: str) -> list:
    start_ts, end_ts = _unix_range_for_dates(date_from, date_to)
    fixtures = []
    page = 1

    while True:
        data = _request_odds_api(
            f"/leagues/{league_id}/fixtures",
            {"start_time": start_ts, "end_time": end_ts, "status": "all", "page": page, "per_page": 100},
        ) or []
        fixtures.extend(data)
        if len(data) < 100:
            break
        page += 1
        if page > 20:
            break
    return fixtures


def _request_fixture_odds(fixture_id) -> dict | None:
    try:
        fixture = _request_odds_api(f"/fixtures/{fixture_id}") or {}
        odds = _extract_1x2(fixture.get("odds"))
        if odds:
            odds["bookmaker"] = "Bet 365"
            return odds
    except OddsAPIError:
        pass

    data = _request_odds_api(
        f"/fixtures/{fixture_id}/odds", {"bookmakers": "bet365", "market": "1x2"},
    ) or {}

    for bookmaker in data.get("bookmakers") or []:
        if str(bookmaker.get("slug", "")).lower() != "bet365":
            continue
        odds = _extract_1x2(bookmaker.get("odds"))
        if odds:
            odds["bookmaker"] = bookmaker.get("name") or "Bet 365"
            return odds

    odds = _extract_1x2(data.get("odds"))
    if odds:
        odds["bookmaker"] = "Bet 365"
        return odds

    return None


def _enrich_top5_with_5dollar(matches: list) -> list:
    """Attach real Bet365 1X2 odds for the five domestic leagues via 5Dollar.

    Every match in every covered league gets its own independent attempt —
    a failure or a "no odds yet" result on one fixture must never affect
    any other fixture. This is deliberately structured so no single
    exception can propagate past one match: the previous version let one
    fixture's odds request raise OddsAPIError, which unwound out of this
    whole function and left every fixture processed AFTER it (in
    dict-iteration order) with no odds at all — the exact "first two
    matches have odds, the third doesn't" symptom.
    """
    if not matches:
        return matches
    if not ODDS_API_KEY:
        print("[ODDS] ODDS_API_KEY is missing — skipping odds for all matches")
        return matches

    league_ids = _load_odds_league_ids()
    if not league_ids:
        print("[ODDS] Could not resolve any covered league IDs")
        return matches

    grouped = {}
    for match in matches:
        match["odds_1x2"] = None
        code = match.get("league_id")
        if code in league_ids:
            grouped.setdefault(code, []).append(match)
        else:
            print(
                f"[ODDS] {match.get('home_team')} vs {match.get('away_team')}: "
                f"league {code} is not covered by the odds provider's plan"
            )

    for code, group in grouped.items():
        dates = [m.get("date_iso") for m in group if m.get("date_iso")]
        if not dates:
            continue

        try:
            fixtures = _request_league_fixtures(league_ids[code], min(dates), max(dates))
        except OddsAPIError as exc:
            # A failure loading the league's fixture list only affects this
            # one league's matches — other leagues already grouped continue
            # to be processed normally on the next loop iteration.
            print(f"[ODDS] {ODDS_LEAGUE_NAMES.get(code, code)}: could not load fixtures — {exc}")
            continue

        for match in group:
            matched_fixture = next(
                (fixture for fixture in fixtures if _events_match_fixture(fixture, match)), None,
            )

            if not matched_fixture:
                print(
                    f"[ODDS] no provider fixture matched for "
                    f"{match.get('home_team')} vs {match.get('away_team')} "
                    f"({match.get('date_iso')} {match.get('time')}) — "
                    f"checked {len(fixtures)} {ODDS_LEAGUE_NAMES.get(code, code)} fixtures"
                )
                continue

            fixture_id = matched_fixture.get("id")
            if fixture_id is None:
                print(
                    f"[ODDS] matched fixture for {match.get('home_team')} vs "
                    f"{match.get('away_team')} has no id — cannot fetch odds"
                )
                continue

            # Each fixture's odds request is isolated: one rate limit or
            # transient failure here costs only this fixture's odds, never
            # the rest of the batch.
            try:
                odds = _request_fixture_odds(fixture_id)
            except OddsAPIError as exc:
                print(
                    f"[ODDS] {match.get('home_team')} vs {match.get('away_team')}: "
                    f"odds request failed — {exc}"
                )
                continue

            if odds:
                match["odds_1x2"] = odds
            else:
                print(
                    f"[ODDS] {match.get('home_team')} vs {match.get('away_team')}: "
                    f"fixture matched (id={fixture_id}) but the provider has no "
                    f"Bet365 1X2 line for it yet"
                )

    return matches


# ============================================================
# PROPLINE — UEFA ODDS (CL / EUROPA / CONFERENCE)
# ============================================================

def _request_propline(path: str, params: dict | None = None):
    if not PROP_LINE_API_KEY:
        return None
    try:
        response = requests.get(f"{PROP_LINE_BASE_URL}{path}", headers={"X-API-Key": PROP_LINE_API_KEY, "Accept": "application/json"}, params=params or {}, timeout=10)
    except requests.exceptions.RequestException as exc:
        raise OddsAPIError(f"Could not reach PropLine: {exc}") from exc
    if response.status_code == 401:
        raise OddsAPIError("Invalid PropLine API key.")
    if response.status_code == 429:
        raise OddsAPIError("PropLine daily request limit reached.")
    if response.status_code != 200:
        raise OddsAPIError(f"PropLine HTTP {response.status_code}.")
    return response.json()


def _propline_sport_for_match(match: dict) -> str | None:
    return PROP_LINE_SPORTS.get(str(match.get("league_id") or "").upper())


def _propline_team_names(event: dict, side: str) -> list[str]:
    """Collect team-name variants from the different PropLine event shapes."""
    if not isinstance(event, dict):
        return []

    names = []

    def add(value):
        if isinstance(value, dict):
            for key in ("name", "title", "display_name", "short_name"):
                if value.get(key):
                    names.append(str(value[key]))
        elif value:
            names.append(str(value))

    if side == "home":
        keys = ("home_team", "home", "home_name", "home_team_name")
    else:
        keys = ("away_team", "away", "away_name", "away_team_name")

    for key in keys:
        add(event.get(key))

    teams = event.get("teams")
    if isinstance(teams, dict):
        add(teams.get(side))

    # A few APIs nest participants instead of teams.
    participants = event.get("participants")
    if isinstance(participants, list):
        for participant in participants:
            if not isinstance(participant, dict):
                continue
            participant_side = str(
                participant.get("position")
                or participant.get("side")
                or participant.get("home_away")
                or ""
            ).lower()
            if participant_side == side:
                add(participant)

    return list(dict.fromkeys(
        _normalize_odds_team_name(name)
        for name in names
        if _normalize_odds_team_name(name)
    ))


_PROPLINE_TEAM_ALIASES = {
    # Madrid / Spain
    "atletico de madrid": "atletico madrid",
    "atletico madrid cf": "atletico madrid",
    "club atletico madrid": "atletico madrid",

    # Manchester
    "manchester united fc": "manchester united",
    "manchester city fc": "manchester city",

    # France
    "paris saint germain": "psg",
    "paris saint germain fc": "psg",
    "paris sg": "psg",

    # Italy
    "internazionale": "inter milan",
    "internazionale milano": "inter milan",
    "inter milano": "inter milan",
    "inter": "inter milan",
    "as roma": "roma",

    # Spain
    "fc barcelona": "barcelona",
    "real madrid cf": "real madrid",

    # Portugal / England / Germany
    "sporting clube de portugal": "sporting cp",
    "sporting lisbon": "sporting cp",
    "bayern munchen": "bayern munich",
    "borussia dortmund": "borussia dortmund",

    # Common short forms
    "psv eindhoven": "psv",
    "psv eindhoven fc": "psv",
    "fenerbahce istanbul": "fenerbahce",
    "slavia praha": "slavia prague",
    "shakhtar donetsk": "shakhtar",
    "fc porto": "porto",

    # Europa League / UEFA common provider names
    "olympique marseille": "marseille",
    "olympique de marseille": "marseille",
    "marseille fc": "marseille",
    "stade rennais": "rennes",
    "stade rennais fc": "rennes",
    "rennes fc": "rennes",
    "ofi crete": "ofi",
    "ofi": "ofi",
    "olympiakos": "olympiacos",
    "olympiacos fc": "olympiacos",
    "sl benfica": "benfica",
    "sport lisboa e benfica": "benfica",
    "celtic glasgow": "celtic",
    "celtic fc": "celtic",
}


def _propline_canonical_team(name: str) -> str:
    normalized = _normalize_odds_team_name(name)
    return _PROPLINE_TEAM_ALIASES.get(normalized, normalized)


def _propline_team_similarity(a: str, b: str) -> float:
    a = _propline_canonical_team(a)
    b = _propline_canonical_team(b)

    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.95

    aa, bb = set(a.split()), set(b.split())
    if aa and bb:
        overlap = len(aa & bb) / max(len(aa), len(bb))
        if overlap >= 0.75:
            return 0.90

    return difflib.SequenceMatcher(None, a, b).ratio()


def _propline_event_match_score(event: dict, match: dict):
    """Return (score, kickoff_difference_hours) or None.

    We deliberately keep home/away orientation strict: reversing them would
    silently put the home odds under 2 and the away odds under 1.
    """
    event_homes = _propline_team_names(event, "home")
    event_aways = _propline_team_names(event, "away")

    match_home = _normalize_odds_team_name(match.get("home_team"))
    match_away = _normalize_odds_team_name(match.get("away_team"))

    if not event_homes or not event_aways or not match_home or not match_away:
        return None

    home_score = max(
        (_propline_team_similarity(name, match_home) for name in event_homes),
        default=0.0,
    )
    away_score = max(
        (_propline_team_similarity(name, match_away) for name in event_aways),
        default=0.0,
    )

    # Both teams must be convincing. This avoids matching a fixture merely
    # because one famous club name happens to be similar.
    if home_score < 0.78 or away_score < 0.78:
        return None

    score = (home_score + away_score) / 2
    kickoff_hours = None

    starts_at = (
        event.get("commence_time")
        or event.get("kickoff_utc")
        or event.get("kickoff")
        or event.get("start_time")
    )
    utc_date = match.get("utc_date")

    if starts_at and utc_date:
        try:
            a = datetime.fromisoformat(str(starts_at).replace("Z", "+00:00"))
            b = datetime.fromisoformat(str(utc_date).replace("Z", "+00:00"))
            if a.tzinfo is None:
                a = a.replace(tzinfo=ZoneInfo("UTC"))
            if b.tzinfo is None:
                b = b.replace(tzinfo=ZoneInfo("UTC"))

            kickoff_hours = abs((a - b).total_seconds()) / 3600

            # Exact team matches can safely tolerate a larger kickoff shift
            # because providers sometimes publish the event on a neighboring
            # UTC/local-date boundary. Fuzzy matches remain stricter.
            max_hours = 36 if score >= 0.97 else 18
            if kickoff_hours > max_hours:
                return None

            # Small time difference improves selection when the provider has
            # multiple similarly named events.
            score += max(0.0, 0.10 * (1.0 - min(kickoff_hours, 12) / 12))
        except (TypeError, ValueError):
            pass

    return score, kickoff_hours


def _propline_event_matches(event: dict, match: dict) -> bool:
    return _propline_event_match_score(event, match) is not None


def _american_to_decimal(price):
    try: p=float(price)
    except (TypeError,ValueError): return None
    if p > 0: return round(1+p/100,2)
    if p < 0: return round(1+100/abs(p),2)
    return None


def _propline_extract_h2h(payload: dict | None) -> dict | None:
    """
    Extract both full-match 1X2 and first-half 1X2 odds.

    The provider may identify outcomes as:
      - home / draw / away
      - 1 / X / 2
      - team names in `description`
    """

    if not isinstance(payload, dict):
        return None

    home_team = _normalize_odds_team_name(
        payload.get("home_team")
    )
    away_team = _normalize_odds_team_name(
        payload.get("away_team")
    )

    result = {
        "home": None,
        "draw": None,
        "away": None,
        "first_half": {
            "home": None,
            "draw": None,
            "away": None,
        },
        "bookmaker": "PropLine",
    }

    full_market_keys = {
        "h2h",
        "moneyline",
        "1x2",
        "match_result",
        "match_winner",
        "fulltime_result",
    }

    first_half_market_keys = {
        "h2h_1st_half",
        "h2h_first_half",
        "h2h_h1",
        "1st_half",
        "first_half",
        "first_half_1x2",
        "1x2_1st_half",
        "1x2_first_half",
    }

    def extract_market(market: dict) -> dict:
        values = {
            "home": None,
            "draw": None,
            "away": None,
        }

        for outcome in market.get("outcomes") or []:
            if not isinstance(outcome, dict):
                continue

            # Some providers return American prices, others decimal.
            dec = _american_to_decimal(outcome.get("price"))

            if dec is None:
                dec = _decimal_odds(outcome.get("price"))

            if dec is None:
                dec = _decimal_odds(outcome.get("value"))

            if dec is None:
                continue

            name = str(
                outcome.get("name", "")
            ).strip().lower()

            description = _normalize_odds_team_name(
                outcome.get("description")
            )

            if name in {"draw", "tie", "x"}:
                values["draw"] = dec

            elif name in {"home", "1"}:
                values["home"] = dec

            elif name in {"away", "2"}:
                values["away"] = dec

            elif _same_team(description, home_team):
                values["home"] = dec

            elif _same_team(description, away_team):
                values["away"] = dec

        return values

    for bookmaker in payload.get("bookmakers") or []:
        if not isinstance(bookmaker, dict):
            continue

        bookmaker_name = (
            bookmaker.get("title")
            or bookmaker.get("name")
            or bookmaker.get("key")
        )

        if bookmaker_name:
            result["bookmaker"] = bookmaker_name

        for market in bookmaker.get("markets") or []:
            if not isinstance(market, dict):
                continue

            market_key = str(
                market.get("key", "")
            ).strip().lower()

            # PropLine uses the normal `h2h` market key for period markets
            # and puts the period in `market["period"]` (h1/h2 for soccer).
            # Older/provider-specific feeds may instead expose a dedicated
            # first-half market key, so support both shapes.
            period = str(
                market.get("period") or ""
            ).strip().lower()

            is_first_half_market = (
                period == "h1"
                or market_key in first_half_market_keys
                or (
                    ("half" in market_key or "1h" in market_key or "1st" in market_key)
                    and any(
                        token in market_key
                        for token in ("h2h", "1x2", "result", "winner", "moneyline")
                    )
                )
            )

            is_full_market = (
                (
                    not period
                    and market_key in full_market_keys
                )
                or market_key in {"3way", "three_way", "three_way_result"}
                    and not period
            )

            if is_first_half_market:
                values = extract_market(market)
                for key in ("home", "draw", "away"):
                    if values[key] is not None:
                        result["first_half"][key] = values[key]

            elif is_full_market:
                values = extract_market(market)

                # Keep the first complete/partially available full-market
                # result rather than overwriting it with an empty market.
                for key in ("home", "draw", "away"):
                    if values[key] is not None:
                        result[key] = values[key]

    has_full = any(
        result[key] is not None
        for key in ("home", "draw", "away")
    )

    has_first_half = any(
        result["first_half"][key] is not None
        for key in ("home", "draw", "away")
    )

    if not has_full and not has_first_half:
        return None

    return result


def _propline_bulk_odds(sport_key: str, period: str | None = None) -> list:
    """Get PropLine bulk h2h odds.

    PropLine exposes first-half soccer odds with the normal `h2h` market and
    `period=h1`; it is not a separate `h2h_1st_half` market in the current API.
    """
    params = {"markets": "h2h"}
    if period:
        params["period"] = period

    payload = _request_propline(
        f"/sports/{sport_key}/odds",
        params,
    )

    if isinstance(payload, list):
        return payload

    if isinstance(payload, dict):
        return (
            payload.get("data")
            or payload.get("events")
            or []
        )

    return []


def _propline_event_odds(
    sport_key: str,
    event_id,
    period: str | None = None,
) -> dict | None:
    """Fetch odds for one PropLine event as a fallback.

    This is important because the bulk odds feed only contains events for which
    the requested market is currently available. The events feed can contain
    a fixture even when it is absent from the bulk odds response.
    """
    if not event_id:
        return None

    params = {"markets": "h2h"}
    if period:
        params["period"] = period

    payload = _request_propline(
        f"/sports/{sport_key}/events/{event_id}/odds",
        params,
    )

    return payload if isinstance(payload, dict) else None


def _propline_events(sport_key: str) -> list:
    payload = _request_propline(
        f"/sports/{sport_key}/events",
        {},
    )

    if isinstance(payload, list):
        return payload

    if isinstance(payload, dict):
        return (
            payload.get("data")
            or payload.get("events")
            or []
        )

    return []

def _propline_live_scores(sport_key: str) -> list:
    payload = _request_propline(
        f"/sports/{sport_key}/scores",
        {
            "days_from": 1,
        },
    )

    if isinstance(payload, list):
        return payload

    if isinstance(payload, dict):
        return (
            payload.get("data")
            or payload.get("events")
            or []
        )

    return []


def _best_propline_event(events: list, match: dict):
    best_event = None
    best_score = -1.0
    best_hours = None

    for candidate in events:
        scored = _propline_event_match_score(candidate, match)
        if scored is None:
            continue
        score, kickoff_hours = scored
        if score > best_score:
            best_event = candidate
            best_score = score
            best_hours = kickoff_hours

    return best_event, best_score, best_hours


_THE_ODDS_SPORT_CACHE: dict[str, list[str]] = {}


def _the_odds_http_get(path: str, params: dict | None = None):
    """Small shared GET helper for The Odds API."""
    if not THE_ODDS_API_KEY:
        return None, None

    query = dict(params or {})
    query["apiKey"] = THE_ODDS_API_KEY

    try:
        response = requests.get(
            f"{THE_ODDS_API_BASE_URL}{path}",
            params=query,
            timeout=15,
        )
    except requests.exceptions.RequestException as exc:
        print(f"[ODDS-FALLBACK] request failed: {exc}")
        return None, None

    remaining = response.headers.get("x-requests-remaining")
    if remaining is not None:
        print(f"[ODDS-FALLBACK] credits remaining={remaining}")

    if response.status_code != 200:
        print(
            f"[ODDS-FALLBACK] HTTP {response.status_code}: "
            f"{response.text[:500]}"
        )
        return None, response.status_code

    try:
        return response.json(), response.status_code
    except ValueError:
        print("[ODDS-FALLBACK] invalid JSON response")
        return None, response.status_code


def _request_the_odds_sports() -> list:
    """Return the current in-season sports list.

    This endpoint does not consume odds quota. We use it as a safety net
    instead of assuming that a league sport key will never change.
    """
    payload, status = _the_odds_http_get("/sports/", {})
    if status != 200 or not isinstance(payload, list):
        return []
    return payload


def _resolve_the_odds_sport_keys(league_id: str) -> list[str]:
    """Resolve one or more currently valid Odds API sport keys for UEFA."""
    league_id = str(league_id or "").upper()
    preferred = THE_ODDS_SPORTS.get(league_id)
    if preferred and league_id in _THE_ODDS_SPORT_CACHE:
        return _THE_ODDS_SPORT_CACHE[league_id]

    keys = []
    if preferred:
        keys.append(preferred)

    sports = _request_the_odds_sports()

    wanted = {
        "CL": ("champions league", "uefa champions"),
        "EL": ("europa league", "uefa europa"),
        "ECL": ("conference league", "uefa conference"),
    }.get(league_id, ())

    for item in sports:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "")
        title = str(item.get("title") or "").lower()
        group = str(item.get("group") or "").lower()
        description = str(item.get("description") or "").lower()

        if not key:
            continue
        haystack = f"{title} {group} {description} {key.lower()}"
        if "soccer" in key.lower() and any(term in haystack for term in wanted):
            if key not in keys:
                keys.append(key)

    _THE_ODDS_SPORT_CACHE[league_id] = keys
    return keys


def _request_the_odds_api(sport_key: str, markets: str = "h2h") -> list:
    """Fetch soccer odds from The Odds API as a fallback source."""
    if not THE_ODDS_API_KEY:
        print("[ODDS-FALLBACK] THE_ODDS_API_KEY is missing")
        return []

    params = {
        "regions": "eu",
        "markets": markets,
        "oddsFormat": "decimal",
    }
    payload, status = _the_odds_http_get(
        f"/sports/{sport_key}/odds/",
        params,
    )
    return payload if status == 200 and isinstance(payload, list) else []


def _request_the_odds_events(sport_key: str) -> list:
    """Get the event catalog. This endpoint does not consume odds quota."""
    payload, status = _the_odds_http_get(
        f"/sports/{sport_key}/events/",
        {},
    )
    return payload if status == 200 and isinstance(payload, list) else []


def _request_the_odds_event_odds(
    sport_key: str,
    event_id: str,
    markets: str = "h2h",
) -> dict | None:
    """Get odds for exactly one event after matching it from the event feed."""
    payload, status = _the_odds_http_get(
        f"/sports/{sport_key}/odds/",
        {
            "regions": "eu",
            "markets": markets,
            "oddsFormat": "decimal",
            "eventIds": str(event_id),
        },
    )
    if status != 200 or not isinstance(payload, list) or not payload:
        return None
    return payload[0] if isinstance(payload[0], dict) else None


def _the_odds_extract_h2h(event: dict) -> dict | None:
    """Pick the best available 1X2 line from The Odds API event."""
    if not isinstance(event, dict):
        return None

    home = _normalize_odds_team_name(event.get("home_team"))
    away = _normalize_odds_team_name(event.get("away_team"))
    best = None

    for bookmaker in event.get("bookmakers") or []:
        for market in bookmaker.get("markets") or []:
            key = str(market.get("key", "")).lower()
            if key not in {"h2h", "h2h_h1", "h2h_1h", "h2h_first_half"}:
                continue

            prices = {"home": None, "draw": None, "away": None}

            for outcome in market.get("outcomes") or []:
                name = _normalize_odds_team_name(
                    outcome.get("name") or outcome.get("description")
                )
                price = _decimal_odds(outcome.get("price"))
                if price is None:
                    continue

                if name == home:
                    prices["home"] = price
                elif name == away:
                    prices["away"] = price
                elif name in {"draw", "x"}:
                    prices["draw"] = price

            if any(prices.values()):
                item = {
                    **prices,
                    "bookmaker": (
                        bookmaker.get("title")
                        or bookmaker.get("key")
                        or "The Odds API"
                    ),
                    "market_key": key,
                }
                if best is None:
                    best = item

                if all(
                    prices[k] is not None
                    for k in ("home", "draw", "away")
                ):
                    return item

    return best


def _the_odds_enrich_missing_uefa(matches: list) -> None:
    """Fill missing UEFA 1X2 odds from The Odds API.

    PropLine remains primary. For every UEFA fixture that PropLine did not
    fill, we:
      1. try the normal odds feed;
      2. if the event is absent there, load the free event catalog;
      3. match the exact event;
      4. request odds for that event ID only.

    The event catalog is important because the bulk odds feed can contain
    only events currently carrying bookmaker lines.
    """
    if not THE_ODDS_API_KEY:
        print("[ODDS-FALLBACK] disabled: THE_ODDS_API_KEY is missing")
        return

    grouped = {}
    for match in matches:
        full = match.get("odds_1x2") or {}
        if all(
            full.get(k) is not None
            for k in ("home", "draw", "away")
        ):
            continue

        league_id = str(match.get("league_id") or "").upper()
        if league_id in THE_ODDS_SPORTS:
            grouped.setdefault(league_id, []).append(match)

    print(
        f"[ODDS-FALLBACK] API key loaded=True; "
        f"UEFA groups needing fallback={len(grouped)}"
    )

    for league_id, group in grouped.items():
        sport_keys = _resolve_the_odds_sport_keys(league_id)

        if not sport_keys:
            print(
                f"[ODDS-FALLBACK] {league_id}: "
                "no current The Odds API sport key found"
            )
            continue

        print(
            f"[ODDS-FALLBACK] {league_id}: candidate sport keys={sport_keys}"
        )

        # Try every resolved sport key until one actually exposes events.
        odds_events = []
        selected_sport = None

        for sport_key in sport_keys:
            odds_events = _request_the_odds_api(sport_key, "h2h")
            print(
                f"[ODDS-FALLBACK] {sport_key}: "
                f"bulk_events={len(odds_events)}"
            )
            if odds_events:
                selected_sport = sport_key
                break

        # Even if the bulk feed is empty, the event catalog may still contain
        # the requested fixtures. Try the sport keys one by one.
        event_catalog = []
        if selected_sport is None:
            for sport_key in sport_keys:
                event_catalog = _request_the_odds_events(sport_key)
                print(
                    f"[ODDS-FALLBACK] {sport_key}: "
                    f"catalog_events={len(event_catalog)}"
                )
                if event_catalog:
                    selected_sport = sport_key
                    break
        else:
            # We need the catalog only for matches absent from the bulk feed.
            event_catalog = None

        if selected_sport is None:
            for match in group:
                print(
                    f"[ODDS-FALLBACK] NO PROVIDER EVENT: "
                    f"{match.get('home_team')} vs {match.get('away_team')}"
                )
            continue

        for match in group:
            # First try the already-loaded bulk odds event.
            full_event, score, hours = _best_propline_event(
                odds_events,
                match,
            )

            # If bulk odds missed it, use the event catalog. This is the key
            # fix for games like Lens-Sporting / Sabah-Slavia / City-PSG.
            if full_event is None:
                if event_catalog is None:
                    event_catalog = _request_the_odds_events(selected_sport)
                    print(
                        f"[ODDS-FALLBACK] {selected_sport}: "
                        f"catalog_events={len(event_catalog)}"
                    )

                catalog_event, score, hours = _best_propline_event(
                    event_catalog,
                    match,
                )

                if catalog_event is not None:
                    event_id = catalog_event.get("id")
                    if event_id:
                        full_event = _request_the_odds_event_odds(
                            selected_sport,
                            event_id,
                            "h2h",
                        )
                        if full_event:
                            print(
                                f"[ODDS-FALLBACK] EVENT-ID FALLBACK: "
                                f"{match.get('home_team')} vs "
                                f"{match.get('away_team')} id={event_id}"
                            )

            if full_event is None:
                print(
                    f"[ODDS-FALLBACK] NO MATCH: "
                    f"{match.get('home_team')} vs {match.get('away_team')}"
                )
                continue

            full = _the_odds_extract_h2h(full_event)
            if not full:
                print(
                    f"[ODDS-FALLBACK] NO H2H: "
                    f"{match.get('home_team')} vs {match.get('away_team')}"
                )
                continue

            current = match.get("odds_1x2") or {}
            match["odds_1x2"] = {
                "home": (
                    current.get("home")
                    if current.get("home") is not None
                    else full.get("home")
                ),
                "draw": (
                    current.get("draw")
                    if current.get("draw") is not None
                    else full.get("draw")
                ),
                "away": (
                    current.get("away")
                    if current.get("away") is not None
                    else full.get("away")
                ),
                "bookmaker": (
                    current.get("bookmaker")
                    or full.get("bookmaker")
                    or "The Odds API"
                ),
            }

            print(
                f"[ODDS-FALLBACK] MATCHED: "
                f"{match.get('home_team')} vs {match.get('away_team')} "
                f"score={score:.3f}"
                + (
                    f" kickoff_diff={hours:.1f}h"
                    if hours is not None else ""
                )
            )
            print(
                f"[ODDS-FALLBACK] "
                f"{match.get('home_team')} vs {match.get('away_team')} "
                f"FULL={match['odds_1x2']}"
            )


def _enrich_uefa_with_propline(matches: list) -> list:
    if not matches or not PROP_LINE_API_KEY:
        return matches

    grouped = {}

    for match in matches:
        sport = _propline_sport_for_match(match)
        if sport:
            grouped.setdefault(sport, []).append(match)

    for sport, group in grouped.items():
        try:
            # Full-game h2h.
            full_events = _propline_bulk_odds(sport)

            # First-half soccer h2h. PropLine documents this as period=h1.
            first_half_events = _propline_bulk_odds(sport, period="h1")

        except OddsAPIError as exc:
            print(
                f"[PROPLINE] {sport}: odds request failed — {exc}"
            )
            continue

        print(
            f"[PROPLINE] {sport}: "
            f"full_events={len(full_events)} "
            f"h1_events={len(first_half_events)}"
        )

        # We only call the events endpoint if bulk odds missed something.
        # This keeps the normal path cheap while allowing an event-level
        # fallback for fixtures that are present in PropLine but absent from
        # the bulk odds response.
        event_catalog = None

        for match in group:
            full_event, full_score, full_hours = _best_propline_event(
                full_events, match
            )

            # Fallback 1: locate the fixture in the events feed.
            if full_event is None:
                if event_catalog is None:
                    try:
                        event_catalog = _propline_events(sport)
                    except OddsAPIError as exc:
                        print(
                            f"[PROPLINE] {sport}: events fallback failed — {exc}"
                        )
                        event_catalog = []

                catalog_event, catalog_score, catalog_hours = _best_propline_event(
                    event_catalog, match
                )

                if catalog_event is not None:
                    event_id = catalog_event.get("id")
                    try:
                        full_event_payload = _propline_event_odds(
                            sport,
                            event_id,
                        )
                    except OddsAPIError as exc:
                        print(
                            f"[PROPLINE] {match.get('home_team')} vs "
                            f"{match.get('away_team')}: event odds failed — {exc}"
                        )
                        full_event_payload = None

                    if full_event_payload:
                        # The event-odds response is itself the event object.
                        full_event = full_event_payload
                        full_score = catalog_score
                        full_hours = catalog_hours
                        print(
                            f"[PROPLINE] EVENT FALLBACK: "
                            f"{match.get('home_team')} vs {match.get('away_team')} "
                            f"id={event_id}"
                        )

            if full_event is None:
                print(
                    f"[PROPLINE] NO MATCH: "
                    f"{match.get('home_team')} vs {match.get('away_team')}"
                )
                continue

            odds = _propline_extract_h2h(full_event)

            # If the bulk event exists but currently has no full h2h market,
            # try the event-level endpoint once.
            if not odds or not any(
                odds.get(k) is not None for k in ("home", "draw", "away")
            ):
                event_id = full_event.get("id")
                try:
                    event_payload = _propline_event_odds(sport, event_id)
                except OddsAPIError as exc:
                    print(
                        f"[PROPLINE] {match.get('home_team')} vs "
                        f"{match.get('away_team')}: event odds failed — {exc}"
                    )
                    event_payload = None

                if event_payload:
                    full_event = event_payload
                    odds = _propline_extract_h2h(full_event)

            if not odds:
                print(
                    f"[PROPLINE] NO H2H ODDS: "
                    f"{match.get('home_team')} vs {match.get('away_team')}"
                )
                continue

            # First-half odds: first try the dedicated h1 bulk response.
            h1_event = None

            # Event ids are stable, so prefer direct id matching.
            full_id = full_event.get("id")
            if full_id is not None:
                for candidate in first_half_events:
                    if str(candidate.get("id")) == str(full_id):
                        h1_event = candidate
                        break

            if h1_event is None:
                h1_event, _, _ = _best_propline_event(
                    first_half_events, match
                )

            h1_odds = (
                _propline_extract_h2h(h1_event)
                if h1_event
                else None
            )

            first_half = (
                h1_odds.get("first_half")
                if h1_odds
                else {
                    "home": None,
                    "draw": None,
                    "away": None,
                }
            )

            # Final fallback: query the event directly for period=h1.
            if not any(first_half.get(k) is not None for k in ("home", "draw", "away")):
                event_id = full_event.get("id")
                if event_id:
                    try:
                        h1_payload = _propline_event_odds(
                            sport,
                            event_id,
                            period="h1",
                        )
                    except OddsAPIError:
                        h1_payload = None

                    if h1_payload:
                        h1_direct = _propline_extract_h2h(h1_payload)
                        if h1_direct:
                            first_half = h1_direct.get(
                                "first_half",
                                first_half,
                            )

            match["odds_1x2"] = {
                "home": odds.get("home"),
                "draw": odds.get("draw"),
                "away": odds.get("away"),
                "bookmaker": odds.get("bookmaker") or "PropLine",
            }

            match["odds_1h_1x2"] = first_half

            print(
                f"[PROPLINE] MATCHED: "
                f"{match.get('home_team')} vs {match.get('away_team')} "
                f"score={full_score:.3f}"
                + (
                    f" kickoff_diff={full_hours:.1f}h"
                    if full_hours is not None else ""
                )
            )
            print(
                f"[PROPLINE] "
                f"{match.get('home_team')} vs {match.get('away_team')} "
                f"FULL={match['odds_1x2']} "
                f"1H={match['odds_1h_1x2']}"
            )

    return matches

def enrich_matches_with_odds(matches: list) -> list:
    """
    Hybrid odds:
      - 5DollarFootballAPI / Bet365 for the five domestic leagues
      - PropLine for UEFA competitions

    Every match gets a predictable odds structure so bot.py
    can render missing values as '—'.
    """

    if not matches:
        return matches

    for match in matches:
        match["odds_1x2"] = None
        match["odds_1h_1x2"] = {
            "home": None,
            "draw": None,
            "away": None,
        }

    _enrich_top5_with_5dollar(matches)
    _enrich_uefa_with_propline(matches)

    # PropLine is the primary UEFA source. Any UEFA fixture still missing a
    # complete 1X2 line is filled from The Odds API, including Europa League.
    _the_odds_enrich_missing_uefa(matches)

    return matches


# ============================================================
# FOOTBALL-DATA.ORG STATUS VERIFICATION (rate-limit aware)
# ============================================================
# football-data.org's free plan has a tight per-minute request budget.
# `verify_fixture_status` self-throttles so a burst of matches needing a
# recheck at the same moment can never blow through that budget — extra
# requests are simply deferred to the next polling cycle rather than
# risking a 429 that would otherwise block the whole refresh.

_STATUS_CHECK_WINDOW_SECONDS = 60
_STATUS_CHECK_MAX_PER_WINDOW = 8  # headroom under the ~10/min free-tier cap
_status_check_history: list[float] = []


def _status_check_budget_available() -> bool:
    now = time.monotonic()
    global _status_check_history
    _status_check_history = [t for t in _status_check_history if now - t < _STATUS_CHECK_WINDOW_SECONDS]
    return len(_status_check_history) < _STATUS_CHECK_MAX_PER_WINDOW


def verify_fixture_status(match_id) -> str | None:
    """Ask football-data.org for one fixture's authoritative current status.

    Returns None ("no information — don't change anything") when the match
    id is missing, this cycle's request budget is used up, or the request
    fails for any reason. Callers must never treat None as a status to
    apply; it only ever means "try again next cycle". This is also the
    honest answer when football-data.org itself simply hasn't updated a
    fixture yet — the bot can't know a match is delayed before its
    authoritative source says so.
    """
    if match_id is None or not FOOTBALL_DATA_TOKEN:
        return None
    if not _status_check_budget_available():
        return None

    _status_check_history.append(time.monotonic())

    url = f"{BASE_URL}/matches/{match_id}"
    try:
        response = requests.get(url, headers=HEADERS, timeout=8)
    except requests.exceptions.RequestException as exc:
        print(f"[FOOTBALL-DATA] status check failed: {exc}")
        return None

    if response.status_code == 401:
        print("[FOOTBALL-DATA] status check: invalid token")
        return None
    if response.status_code == 403:
        print("[FOOTBALL-DATA] status check: plan does not allow this request")
        return None
    if response.status_code == 429:
        print("[FOOTBALL-DATA] status check: rate limit reached")
        return None
    if response.status_code != 200:
        print(f"[FOOTBALL-DATA] status check returned {response.status_code}")
        return None

    try:
        raw_status = response.json().get("status")
    except (TypeError, ValueError):
        return None

    return _convert_status(raw_status)


# ============================================================
# FAST LIVE SCORE REFRESH
# ============================================================

def _kickoff_datetime(match: dict):
    """Return kickoff as UTC, with a local date/time fallback.

    Some match objects can lose/omit utc_date while still having the
    already-formatted local date/time.  The live stale guard must not fail
    silently just because that one field is missing.
    """
    raw = match.get("utc_date")
    if raw:
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo("UTC"))
            return dt.astimezone(ZoneInfo("UTC"))
        except (TypeError, ValueError):
            pass

    # Fallback: the normal fixture parser stores the displayed local date
    # and time.  Interpret those in the configured bot timezone.
    date_iso = match.get("date_iso")
    clock = match.get("time")
    if date_iso and clock:
        try:
            local_dt = datetime.strptime(
                f"{date_iso} {clock}", "%Y-%m-%d %H:%M"
            ).replace(tzinfo=ZoneInfo(TIMEZONE))
            return local_dt.astimezone(ZoneInfo("UTC"))
        except (TypeError, ValueError):
            pass

    return None


def _estimate_minute(match: dict) -> str | None:
    kickoff = _kickoff_datetime(match)
    if kickoff is None:
        return None
    elapsed = int((datetime.now(ZoneInfo("UTC")) - kickoff).total_seconds() // 60)
    return str(max(1, min(elapsed, 130)))


def _map_provider_status(status: str | None, status_code: str | None) -> tuple[str, str | None]:
    """Map a 5DollarFootballAPI live status to our canonical vocabulary.

    This mapping is only ever consulted for a fixture football-data.org has
    already confirmed is live (see refresh_live_scores) — so it only needs
    to distinguish LIVE / HT / FT, never NS/PST/CANC/SUSP.
    """
    s = str(status or "").strip().lower()
    code = str(status_code or "").strip()

    if s == "in_play":
        if code.lower() in {"half", "ht"}:
            return "HT", None
        if code.isdigit():
            return "LIVE", code
        return "LIVE", None

    if s == "finished":
        return "FT", None

    if s in {"paused", "pause", "halftime"}:
        return "HT", None

    if s in {"live", "1h", "2h", "et", "p", "int"}:
        return "LIVE", code or None

    return "UNKNOWN", None


def _extract_goal_count(fixture: dict) -> tuple[int | None, int | None]:
    """Return the freshest reliable (home, away) goal count for a fixture.

    Preference order:
      1. The fixture's own aggregate `goals` object — the provider's current
         live truth, trusted outright and never blended with anything older.
      2. A straight count of `goal` events, used only when the aggregate is
         completely missing.
    `period_score` snapshots describe a single half, not the running total,
    and are deliberately never used to set the score — averaging/maxing
    them against the aggregate is exactly what caused stale totals before.
    """
    aggregate = fixture.get("goals") or {}
    home = _safe_int(aggregate.get("home"))
    away = _safe_int(aggregate.get("away"))
    if home is not None or away is not None:
        return home, away

    home_events = away_events = 0
    saw_goal = False
    for event in fixture.get("events") or []:
        if str(event.get("type", "")).strip().lower() != "goal":
            continue
        saw_goal = True
        team = str(event.get("team", "")).strip().lower()
        count = _safe_int(event.get("count", 1)) or 1
        count = max(1, count)
        if team == "home":
            home_events += count
        elif team == "away":
            away_events += count

    if saw_goal:
        return home_events, away_events
    return None, None


def _score_signature(match: dict) -> tuple:
    return (
        match.get("goals_home"),
        match.get("goals_away"),
        match.get("status_short"),
        match.get("minute"),
    )


def _apply_live_fixture(match: dict, fixture: dict) -> None:
    new_status, new_minute = _map_provider_status(fixture.get("status"), fixture.get("status_code"))

    # The fast provider's status can only move a fixture within the live
    # family (LIVE <-> HT) or forward to FT — it is never trusted to
    # postpone/cancel a match. That authority stays with football-data.org.
    if new_status in {"LIVE", "HT", "FT"}:
        match["status_short"] = new_status
        match["source_status_short"] = new_status

    home, away = _extract_goal_count(fixture)
    if home is not None:
        match["goals_home"] = home
    if away is not None:
        match["goals_away"] = away

    if new_minute:
        match["minute"] = new_minute
        match["elapsed"] = new_minute
    elif match["status_short"] == "LIVE":
        estimated = _estimate_minute(match)
        match["minute"] = estimated
        match["elapsed"] = estimated


def refresh_live_scores(matches: list) -> set[int]:
    """Refresh status/score/minute for in-progress or about-to-start matches.

    Returns the set of indexes into `matches` whose displayed state
    (score, status, or minute) actually changed, so the caller only has to
    re-render those cards.
    """
    if not matches:
        print("[LIVE] refresh skipped: no matches")
        return set()

    changed_indexes: set[int] = set()
    now_utc = datetime.now(ZoneInfo("UTC"))
    print(f"[LIVE] refresh cycle started: {len(matches)} visible matches")

    # ------------------------------------------------------------
    # 1) Targeted football-data.org verification — the only thing allowed
    #    to move a match into/out of PST/SUSP/CANC/FT/NS, and the only
    #    thing allowed to confirm a match may enter the live family.
    # ------------------------------------------------------------
    for index, match in enumerate(matches):
        status = str(match.get("status_short", "")).upper()

        if is_finished(status) or is_cancelled(status):
            continue  # terminal states are never re-checked

        needs_check = False

        if is_pre_match(status):
            kickoff = _kickoff_datetime(match)
            if kickoff is not None and kickoff - timedelta(minutes=10) <= now_utc:
                needs_check = True
        elif is_live_family(status) or is_suspended(status):
            # Cheap safety net: confirms a live match hasn't been
            # postponed/cancelled/finished behind the fast provider's back.
            needs_check = True

        if not needs_check:
            continue

        verified = verify_fixture_status(match.get("match_id"))
        if verified is None:
            print(
                f"[FOOTBALL-DATA] {match.get('home_team')} vs {match.get('away_team')}: "
                f"status check returned no result (deferred/failed)"
            )
            # Do not let a failed/deferred status request keep an obviously
            # stale LIVE card forever.  The 180-minute guard below is based
            # only on kickoff time and is intentionally independent of the
            # football-data response.
            if is_live_family(status):
                kickoff = _kickoff_datetime(match)
                if kickoff is not None:
                    minutes_since_kickoff = (now_utc - kickoff).total_seconds() / 60
                    if minutes_since_kickoff > 180:
                        old_signature = _score_signature(match)
                        match["status_short"] = "PST"
                        match["source_status_short"] = "PST"
                        match["minute"] = None
                        match["elapsed"] = None
                        if _score_signature(match) != old_signature:
                            changed_indexes.add(index)
                        print(
                            f"[LIVE] STALE GUARD (no status response): "
                            f"{match.get('home_team')} vs {match.get('away_team')} "
                            f"is {minutes_since_kickoff:.0f} min past kickoff -> DELAYED"
                        )
            continue  # no budget / no answer this cycle — try again later

        print(
            f"[FOOTBALL-DATA] {match.get('home_team')} vs {match.get('away_team')}: "
            f"{status} -> {verified}"
        )

        # Safety net: a football match cannot realistically remain in LIVE/HT
        # indefinitely. This guard is deliberately independent of the exact
        # provider status value: if the current card is LIVE/HT and kickoff
        # is more than 180 minutes old, stop displaying a stale LIVE card.
        if is_live_family(status):
            kickoff = _kickoff_datetime(match)
            if kickoff is not None:
                minutes_since_kickoff = (now_utc - kickoff).total_seconds() / 60
                if minutes_since_kickoff > 180:
                    print(
                        f"[LIVE] stale LIVE guard: {match.get('home_team')} vs "
                        f"{match.get('away_team')} is {minutes_since_kickoff:.0f} min past kickoff "
                        f"-> DELAYED"
                    )
                    verified = "PST"

        if verified != status:
            old_signature = _score_signature(match)
            match["status_short"] = verified
            match["source_status_short"] = verified
            if verified in {"PST", "CANC"}:
                match["minute"] = None
                match["elapsed"] = None
            if _score_signature(match) != old_signature:
                changed_indexes.add(index)

    # ------------------------------------------------------------
    # 2) Fast score/minute refinement — only for fixtures football-data.org
    #    has already confirmed are LIVE or at HALFTIME.
    # ------------------------------------------------------------
    live_targets = [
        (i, m) for i, m in enumerate(matches)
        if is_live_family(str(m.get("status_short", "")).upper())
    ]

    print(
        f"[LIVE] canonical live targets: {len(live_targets)}; "
        f"5Dollar key loaded: {bool(ODDS_API_KEY)}"
    )

    if live_targets and ODDS_API_KEY:
        try:
            live_fixtures = _request_odds_api(
                "/fixtures", {"status": "live", "include": "events", "per_page": 50},
            ) or []
        except OddsAPIError as exc:
            print(f"[LIVE] Could not fetch live scores with events: {exc}")
            live_fixtures = None
            # Some provider-side errors have occurred on the expanded live
            # payload.  The plain live endpoint is enough for score/minute
            # updates and is a safe one-call fallback for this cycle.
            try:
                live_fixtures = _request_odds_api(
                    "/fixtures", {"status": "live", "per_page": 500},
                ) or []
                print(f"[LIVE] Fallback live request succeeded: {len(live_fixtures)} fixtures")
            except OddsAPIError as fallback_exc:
                print(f"[LIVE] Fallback live request failed: {fallback_exc}")
                live_fixtures = None

        if live_fixtures is not None:
            print(f"[LIVE] 5Dollar returned {len(live_fixtures)} live fixture(s)")
            for index, match in live_targets:
                fixture = next(
                    (item for item in live_fixtures if _events_match_fixture(item, match)), None,
                )
                if fixture is None:
                    print(
                        f"[LIVE] no 5Dollar fixture match for "
                        f"{match.get('home_team')} vs {match.get('away_team')}"
                    )

                    # FINAL STALE-LIVE SAFETY NET:
                    # If football-data still says LIVE but the live provider
                    # has completely dropped this fixture, and the kickoff is
                    # already more than 180 minutes old, stop displaying a
                    # stale LIVE card.  This is deliberately checked here,
                    # after the 5Dollar response, because the combination
                    # "very old kickoff + absent from live feed" is much
                    # stronger evidence than the football-data LIVE flag alone.
                    kickoff = _kickoff_datetime(match)
                    if kickoff is not None:
                        minutes_since_kickoff = (
                            datetime.now(ZoneInfo("UTC")) - kickoff
                        ).total_seconds() / 60
                        if minutes_since_kickoff > 180:
                            old_signature = _score_signature(match)
                            match["status_short"] = "PST"
                            match["source_status_short"] = "PST"
                            match["minute"] = None
                            match["elapsed"] = None
                            if _score_signature(match) != old_signature:
                                changed_indexes.add(index)
                            print(
                                f"[LIVE] STALE GUARD: {match.get('home_team')} vs "
                                f"{match.get('away_team')} is {minutes_since_kickoff:.0f} min "
                                f"past kickoff and absent from 5Dollar live feed -> DELAYED"
                            )
                    continue

                old_signature = _score_signature(match)
                _apply_live_fixture(match, fixture)
                if _score_signature(match) != old_signature:
                    changed_indexes.add(index)
                    print(
                        f"[LIVE] {match.get('home_team')} vs {match.get('away_team')} -> "
                        f"{match.get('goals_home')}-{match.get('goals_away')} "
                        f"{match.get('status_short')} {match.get('minute') or ''}".strip()
                    )

    # UEFA live updates use PropLine. Top-5 live updates remain on 5Dollar.
    global _PROP_LINE_LAST_LIVE_FETCH
    uefa_targets=[(i,m) for i,m in enumerate(matches) if _propline_sport_for_match(m) and is_live_family(str(m.get("status_short","")).upper())]
    now_m=time.monotonic()
    if uefa_targets and PROP_LINE_API_KEY and now_m-_PROP_LINE_LAST_LIVE_FETCH >= PROP_LINE_LIVE_INTERVAL:
        _PROP_LINE_LAST_LIVE_FETCH=now_m
        by_sport={}
        for i,m in uefa_targets: by_sport.setdefault(_propline_sport_for_match(m),[]).append((i,m))
        for sport,targets in by_sport.items():
            try: scores=_propline_live_scores(sport)
            except OddsAPIError as exc: print(f"[PROPLINE LIVE] {sport}: scores failed — {exc}"); scores=[]
            try: odds_events=_propline_bulk_odds(sport)
            except OddsAPIError as exc: print(f"[PROPLINE LIVE] {sport}: odds failed — {exc}"); odds_events=[]
            for index,match in targets:
                se=next((e for e in scores if _propline_event_matches(e,match)),None)
                oe=next((e for e in odds_events if _propline_event_matches(e,match)),None)
                old=_score_signature(match)
                if se:
                    st=str(se.get("status","")).lower()
                    if st in {"in_progress","live"}: match["status_short"]="LIVE"
                    elif st in {"halftime","paused"}: match["status_short"]="HT"
                    elif st in {"final","finished"}: match["status_short"]="FT"
                    match["source_status_short"]=match["status_short"]
                    h=_safe_int(se.get("home_score")); a=_safe_int(se.get("away_score"))
                    if h is not None: match["goals_home"]=h
                    if a is not None: match["goals_away"]=a
                    if match["status_short"]=="LIVE":
                        minute=_estimate_minute(match); match["minute"]=minute; match["elapsed"]=minute
                if oe:
                    o=_propline_extract_h2h(oe)
                    if o: match["odds_1x2"]=o
                if _score_signature(match)!=old: changed_indexes.add(index)
        print(f"[PROPLINE LIVE] refreshed {len(uefa_targets)} UEFA match(es); interval={PROP_LINE_LIVE_INTERVAL}s")

    print(f"[LIVE] refresh cycle finished: changed={sorted(changed_indexes)}")
    return changed_indexes


# ============================================================
# TODAY FILTER / YEREVAN MIDNIGHT FIX
# ============================================================

def filter_matches_for_today(matches: list) -> list:
    """Keep today's matches plus late-night matches from the previous day."""
    now_local = datetime.now(ZoneInfo(TIMEZONE))
    today = now_local.date()
    yesterday = today - timedelta(days=1)
    cutoff = now_local - timedelta(hours=5)

    result = []
    for match in matches:
        raw_date = match.get("date_iso")
        if not raw_date:
            continue

        try:
            match_date = datetime.strptime(str(raw_date), "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue

        if match_date == today:
            result.append(match)
            continue

        if match_date != yesterday:
            continue

        try:
            kickoff = datetime.strptime(
                f"{raw_date} {match.get('time', '00:00')}", "%Y-%m-%d %H:%M",
            ).replace(tzinfo=ZoneInfo(TIMEZONE))
        except (TypeError, ValueError):
            continue

        if kickoff.hour >= 22 and kickoff >= cutoff:
            result.append(match)

    return result


# ============================================================
# DATE RANGE
# ============================================================

def get_date_range(period: str) -> tuple:

    today_local = datetime.now(ZoneInfo(TIMEZONE)).date()

    if period == "today":
        date_from = today_local - timedelta(days=1)
        date_to = today_local
    elif period == "tomorrow":
        date_from = today_local + timedelta(days=1)
        date_to = date_from
    elif period == "week":
        date_from = today_local
        date_to = today_local + timedelta(days=7)
    elif period == "month":
        date_from = today_local.replace(day=1)
        if date_from.month == 12:
            next_month = date_from.replace(year=date_from.year + 1, month=1)
        else:
            next_month = date_from.replace(month=date_from.month + 1)
        date_to = next_month - timedelta(days=1)
    else:
        raise ValueError(f"Unknown period: {period}")

    return date_from.strftime("%Y-%m-%d"), date_to.strftime("%Y-%m-%d")


# ============================================================
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

# EMBEDDED NON-FOOTBALL API — NBA / NHL / TENNIS / F1
# ============================================================
# The original Football API above is preserved in full.
# The following section is the complete non-football API layer.

# ============================================================
# Providers
# ============================================================

NBA_SCOREBOARD_URL = "https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json"
NBA_SCHEDULE_URL = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
NBA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
    "Accept": "application/json,text/plain,*/*",
}
NHL_SCORE_URL = "https://api-web.nhle.com/v1/score"
F1_BASE_URL = "https://api.jolpi.ca/ergast/f1"

TENNIS_HOST = "tennis-api-atp-wta-itf.p.rapidapi.com"
TENNIS_BASE_URL = f"https://{TENNIS_HOST}"
LIVE_TENNIS_BASE_URL = "https://api.livetennisapi.com/api/public/v1"

# API Tennis - dedicated tennis bookmaker odds provider
API_TENNIS_BASE_URL = "https://api.api-tennis.com/tennis/"
API_TENNIS_KEY = (os.getenv("API_TENNIS_API_KEY") or os.getenv("API_TENNIS_KEY") or "").strip()
API_TENNIS_BOOKMAKER = (os.getenv("API_TENNIS_BOOKMAKER") or ODDS_BOOKMAKER).strip().lower()
API_TENNIS_SINGLES_EVENT_TYPE_KEYS = {"atp": "265", "wta": "266"}
_API_TENNIS_EVENT_TYPE_KEYS_CACHE: tuple[float, dict[str, str]] | None = None
_API_TENNIS_ODDS_CACHE: dict[tuple[str, str, str], tuple[float, list[dict]]] = {}
_API_TENNIS_ODDS_CACHE_TTL = 180.0

THE_ODDS_API_BASE_URL = "https://api.the-odds-api.com/v4"
THE_ODDS_API_KEY = (os.getenv("THE_ODDS_API_KEY") or os.getenv("MULTI_SPORT_ODDS_API_KEY") or "").strip()
THE_ODDS_BOOKMAKER = os.getenv("MULTI_SPORT_ODDS_BOOKMAKER", "").strip().lower()
NBA_ODDS_REGION = os.getenv("MULTI_SPORT_ODDS_REGION_NBA", "us").strip() or "us"
EUROLEAGUE_ODDS_REGION = (
    os.getenv("MULTI_SPORT_ODDS_REGION_EUROLEAGUE", "eu,uk").strip()
    or "eu,uk"
)
NHL_ODDS_REGION = (
    os.getenv("MULTI_SPORT_ODDS_REGION_NHL", "us,uk,eu").strip()
    or "us,uk,eu"
)
# Tennis odds: use regions rather than the global ODDS_BOOKMAKER setting.
# The Odds API currently documents its supported bookmakers by region, and
# Bet365 is not listed in its current bookmaker catalogue.  A specific
# bookmaker can be requested with TENNIS_ODDS_BOOKMAKERS when supported.
TENNIS_ODDS_REGION = os.getenv("MULTI_SPORT_ODDS_REGION_TENNIS", "uk,eu").strip() or "uk,eu"
TENNIS_ODDS_BOOKMAKERS = os.getenv("TENNIS_ODDS_BOOKMAKERS", "").strip()

KROK_BASE_URL = "https://krokodds.com.au/api/v1"
KROK_API_KEY = (os.getenv("KROK_ODDS_API_KEY") or "").strip()

# OddsPapi — primary NHL odds provider
ODDSPAPI_BASE_URL = "https://api.oddspapi.io/v4"
ODDSPAPI_API_KEY = (
    os.getenv("ODDSPAPI_API_KEY")
    or os.getenv("ODDSPAPI_KEY")
    or os.getenv("ODDS_PAPI_API_KEY")
    or ""
).strip()
ODDSPAPI_NHL_BOOKMAKERS = (
    os.getenv("ODDSPAPI_NHL_BOOKMAKERS")
    or os.getenv("ODDS_BOOKMAKER")
    or "bet365"
).strip().lower()
ODDSPAPI_NHL_SPORT_ID = (os.getenv("ODDSPAPI_NHL_SPORT_ID") or "").strip()
ODDSPAPI_NHL_TOURNAMENT_ID = (
    os.getenv("ODDSPAPI_NHL_TOURNAMENT_ID") or "234"
).strip()
# OddsPapi's NHL catalogue defines market 151 as the winner including OT;
# outcomes 151 and 152 are participant 1 (home) and participant 2 (away).
ODDSPAPI_NHL_MONEYLINE_MARKET_ID = "151"
ODDSPAPI_NHL_HOME_OUTCOME_ID = "151"
ODDSPAPI_NHL_AWAY_OUTCOME_ID = "152"
ODDSPAPI_TENNIS_BOOKMAKERS = (
    os.getenv("ODDSPAPI_TENNIS_BOOKMAKERS")
    or os.getenv("ODDS_BOOKMAKER")
    or "bet365"
).strip().lower()
ODDSPAPI_TENNIS_SPORT_ID = "12"


# ============================================================
# Generic helpers
# ============================================================


def _get_json(
    url: str,
    *,
    headers: dict | None = None,
    params: dict | None = None,
    timeout: float | None = None,
):
    try:
        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=TIMEOUT if timeout is None else timeout,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        # Provider errors often echo the request URL, including apiKey query
        # parameters. Keep diagnostic details without leaking credentials.
        safe_message = re.sub(
            r"(?i)([?&](?:apiKey|api_key|key|token|access_token)=)[^&\s]+",
            r"\1[REDACTED]",
            str(exc),
        )
        raise RuntimeError(safe_message) from None
    payload = response.json()
    if isinstance(payload, dict):
        if payload.get("error") or payload.get("err"):
            raise RuntimeError(str(payload.get("error") or payload.get("err")))
    return payload


def _parse_dt(raw) -> datetime | None:
    if not raw:
        return None
    try:
        value = str(raw).strip()
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC_TZ)
        return dt.astimezone(YEREVAN_TZ)
    except (TypeError, ValueError):
        return None


def _local_date(raw) -> date | None:
    dt = _parse_dt(raw)
    return dt.date() if dt else None


def _date_time(raw) -> tuple[str, str]:
    dt = _parse_dt(raw)
    if not dt:
        return "", ""
    return dt.strftime("%d %b"), dt.strftime("%H:%M")


def _norm(value: str | None) -> str:
    value = str(value or "").strip().lower()
    value = " ".join(value.replace("/", " ").split())
    return value


def _similar(a: str | None, b: str | None) -> float:
    aa = _norm(a)
    bb = _norm(b)
    if not aa or not bb:
        return 0.0
    if aa == bb:
        return 1.0
    if aa in bb or bb in aa:
        return 0.95
    return difflib.SequenceMatcher(None, aa, bb).ratio()


def _winner_by_score(home: str, away: str, home_score, away_score) -> str | None:
    try:
        h = float(home_score)
        a = float(away_score)
    except (TypeError, ValueError):
        return None
    if h > a:
        return home
    if a > h:
        return away
    return "Draw"


def _decimal(value):
    try:
        value = float(value)
        return round(value, 2) if value >= 1 else None
    except (TypeError, ValueError):
        return None


def _days(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


# ============================================================
# The Odds API — NBA / NHL moneyline
# ============================================================

# ============================================================
# NBA
# ============================================================


def _nba_team(team: dict | None = None, game: dict | None = None, side: str = "") -> dict:
    team = team or {}
    if not team and game:
        prefix = f"{side}Team_"
        team = {
            "teamId": game.get(prefix + "teamId"),
            "teamName": game.get(prefix + "teamName"),
            "teamCity": game.get(prefix + "teamCity"),
            "teamTricode": game.get(prefix + "teamTricode"),
            "score": game.get(prefix + "score"),
        }
    return team


def _nba_name(team: dict) -> str:
    city = str(team.get("teamCity") or "").strip()
    name = str(team.get("teamName") or team.get("teamTricode") or "Team").strip()
    return f"{city} {name}".strip()


NBA_TEAM_CODES = {
    "atlanta hawks": "atl", "boston celtics": "bos", "brooklyn nets": "bkn",
    "charlotte hornets": "cha", "chicago bulls": "chi", "cleveland cavaliers": "cle",
    "dallas mavericks": "dal", "denver nuggets": "den", "detroit pistons": "det",
    "golden state warriors": "gs", "houston rockets": "hou", "indiana pacers": "ind",
    "la clippers": "lac", "los angeles clippers": "lac", "los angeles lakers": "lal",
    "memphis grizzlies": "mem", "miami heat": "mia", "milwaukee bucks": "mil",
    "minnesota timberwolves": "min", "new orleans pelicans": "no",
    "new york knicks": "ny", "oklahoma city thunder": "okc", "orlando magic": "orl",
    "philadelphia 76ers": "phi", "phoenix suns": "phx", "portland trail blazers": "por",
    "sacramento kings": "sac", "san antonio spurs": "sa", "toronto raptors": "tor",
    "utah jazz": "utah", "washington wizards": "wsh",
}


def _nba_logo(team: dict) -> str | None:
    tricode = str(team.get("teamTricode") or team.get("abbreviation") or "").strip().lower()
    if not tricode:
        name = _nba_name(team).lower()
        tricode = NBA_TEAM_CODES.get(name, "")
    if tricode:
        # Raster logo endpoint is used because the Telegram card renderer is
        # local and should not depend on the NBA CDN.
        return f"https://a.espncdn.com/i/teamlogos/nba/500/{tricode}.png"
    team_id = team.get("teamId") or team.get("id")
    if team_id:
        return f"https://cdn.nba.com/logos/nba/{team_id}/global/L/logo.svg"
    return None


def _nba_get_json(url: str):
    """NBA CDN request with the current WAF-friendly browser headers."""
    return _get_json(url, headers=NBA_HEADERS)


def _nba_games_from_odds_fallback(date_from: date, date_to: date) -> list[dict]:
    """Fallback schedule source when NBA's static CDN schedule is WAF-blocked.

    The Odds API only exposes current/live/upcoming odds events, so this is
    intentionally a fallback for current/upcoming dates, not a historical
    schedule database.
    """
    if not THE_ODDS_API_KEY:
        return []
    try:
        events = _odds_api_get(
            "/sports/basketball_nba/odds",
            {
                "regions": NBA_ODDS_REGION,
                "markets": "h2h",
                "oddsFormat": "decimal",
                "dateFormat": "iso",
            },
        ) or []
    except Exception:
        return []

    out = []
    for event in events:
        dt = _parse_dt(event.get("commence_time"))
        if not dt:
            continue
        if date_from <= dt.date() <= date_to:
            # Convert into the same flat game shape consumed by _nba_normalize.
            out.append({
                "gameId": str(event.get("id") or ""),
                "gameDateTimeUTC": str(event.get("commence_time") or ""),
                "gameTimeUTC": str(event.get("commence_time") or ""),
                "gameStatus": 1,
                "gameStatusText": "Scheduled",
                "homeTeam": {
                    "teamCity": "",
                    "teamName": str(event.get("home_team") or "Home"),
                    "teamTricode": "",
                    "teamId": None,
                    "score": None,
                },
                "awayTeam": {
                    "teamCity": "",
                    "teamName": str(event.get("away_team") or "Away"),
                    "teamTricode": "",
                    "teamId": None,
                    "score": None,
                },
                "_odds_event": event,
            })
    return out


def _merge_odds_scores_for_sport(
    sport_key: str,
    date_from: date,
    date_to: date,
    regions: str = "us",
):
    """Load current/upcoming odds plus live/recent scores from The Odds API."""
    if not THE_ODDS_API_KEY:
        return [], []

    odds_events = []
    score_events = []

    try:
        odds_events = _odds_api_get(
            f"/sports/{sport_key}/odds",
            {
                "regions": regions,
                "markets": "h2h",
                "oddsFormat": "decimal",
                "dateFormat": "iso",
            },
        ) or []
    except Exception:
        odds_events = []

    try:
        score_params = {"dateFormat": "iso"}
        # The Odds API can return up to 3 days of completed games when daysFrom
        # is supplied, plus live/upcoming games.
        if date_to >= datetime.now(YEREVAN_TZ).date() - timedelta(days=3):
            score_params["daysFrom"] = 3
        score_events = _odds_api_get(
            f"/sports/{sport_key}/scores", score_params
        ) or []
    except Exception:
        score_events = []

    return odds_events, score_events


def _score_lookup(score_events: list[dict]) -> dict[str, dict]:
    return {str(e.get("id")): e for e in score_events if e.get("id")}


def _get_nba_games(date_from: date, date_to: date) -> list[dict]:
    """NBA schedule + score source without relying on NBA CDN endpoints.

    The NBA CDN has recently returned HTTP 403 to automated requests.
    The Odds API provides NBA events, odds, and live/recent scores, so it is
    used as the primary source here once THE_ODDS_API_KEY is configured.
    """
    odds_events, score_events = _merge_odds_scores_for_sport(
        "basketball_nba", date_from, date_to
    )
    scores = _score_lookup(score_events)

    by_id: dict[str, dict] = {}
    for source_event in list(odds_events) + list(score_events):
        event_id = str(source_event.get("id") or "")
        if not event_id:
            continue
        dt = _parse_dt(source_event.get("commence_time"))
        if not dt or not (date_from <= dt.date() <= date_to):
            continue
        by_id[event_id] = source_event

    games = []
    for event_id, event in by_id.items():
        score_event = scores.get(event_id, {})
        merged = dict(event)
        if score_event:
            merged.update({
                "completed": score_event.get("completed"),
                "scores": score_event.get("scores"),
            })
        games.append({
            "gameId": event_id,
            "gameDateTimeUTC": event.get("commence_time"),
            "gameStatus": 3 if merged.get("completed") else 1,
            "gameStatusText": "Final" if merged.get("completed") else "Scheduled",
            "homeTeam": {
                "teamCity": "",
                "teamName": str(event.get("home_team") or "Home"),
                "teamTricode": "",
                "teamId": None,
                "score": _event_score(merged, event.get("home_team")),
            },
            "awayTeam": {
                "teamCity": "",
                "teamName": str(event.get("away_team") or "Away"),
                "teamTricode": "",
                "teamId": None,
                "score": _event_score(merged, event.get("away_team")),
            },
            "_odds_event": event,
        })

    games.sort(key=lambda g: str(g.get("gameDateTimeUTC") or ""))
    return games


def _event_score(event: dict, team_name: str | None):
    for row in event.get("scores") or []:
        if _similar(row.get("name"), team_name) >= 0.90:
            return row.get("score")
    return None


_ODDS_SPORT_GAMES_CACHE: dict[tuple[str, str, str, str], tuple[float, list[dict], list[dict]]] = {}
_ODDS_SPORT_GAMES_CACHE_TTL = 60.0


def _get_odds_api_sport_games(
    sport_key: str,
    date_from: date,
    date_to: date,
    regions: str,
) -> tuple[list[dict], list[dict]]:
    """Load fixture catalog, available odds, and recent scores for one league.

    The Odds API has separate preseason sport keys. Its events endpoint can
    provide fixtures even when bookmakers have not posted a moneyline yet, so
    odds are enrichment rather than the fixture source for these leagues.
    """
    if not THE_ODDS_API_KEY:
        print(f"[SPORT FEED] {sport_key}: The Odds API key is missing")
        return [], []

    cache_key = (
        sport_key,
        date_from.isoformat(),
        date_to.isoformat(),
        regions,
    )
    now_ts = time.monotonic()
    cached = _ODDS_SPORT_GAMES_CACHE.get(cache_key)
    if cached and now_ts - cached[0] < _ODDS_SPORT_GAMES_CACHE_TTL:
        games, odds_events = cached[1], cached[2]
        return [dict(game) for game in games], [dict(event) for event in odds_events]

    try:
        catalog_events = _odds_api_get(
            f"/sports/{sport_key}/events",
            {"dateFormat": "iso"},
        ) or []
    except Exception as exc:
        print(f"[SPORT FEED] {sport_key} events request failed: {exc}")
        catalog_events = []

    odds_events, score_events = _merge_odds_scores_for_sport(
        sport_key, date_from, date_to, regions=regions
    )
    scores = _score_lookup(score_events)

    by_id: dict[str, dict] = {}
    for source_event in list(catalog_events) + list(odds_events) + list(score_events):
        if not isinstance(source_event, dict):
            continue
        event_id = str(source_event.get("id") or "").strip()
        if not event_id:
            continue
        event_day = _local_date(source_event.get("commence_time"))
        if event_day is None or not (date_from <= event_day <= date_to):
            continue
        by_id.setdefault(event_id, {}).update(source_event)

    games = []
    for event_id, event in by_id.items():
        score_event = scores.get(event_id) or {}
        if score_event:
            event.update({
                "completed": score_event.get("completed", event.get("completed")),
                "scores": score_event.get("scores") or event.get("scores") or [],
            })
        has_score = any(
            row.get("score") not in (None, "")
            for row in event.get("scores") or []
            if isinstance(row, dict)
        )
        completed = bool(event.get("completed"))
        status_code = 3 if completed else (2 if has_score else 1)
        game_status = "Final" if completed else ("Live" if has_score else "Scheduled")
        home_name = str(event.get("home_team") or "Home").strip()
        away_name = str(event.get("away_team") or "Away").strip()
        games.append({
            "gameId": event_id,
            "gameDateTimeUTC": event.get("commence_time"),
            "gameTimeUTC": event.get("commence_time"),
            "gameStatus": status_code,
            "gameStatusText": game_status,
            "homeTeam": {
                "teamCity": "",
                "teamName": home_name,
                "teamTricode": "",
                "teamId": None,
                "score": _event_score(event, home_name),
            },
            "awayTeam": {
                "teamCity": "",
                "teamName": away_name,
                "teamTricode": "",
                "teamId": None,
                "score": _event_score(event, away_name),
            },
            "_odds_event": event,
        })

    games.sort(key=lambda game: str(game.get("gameDateTimeUTC") or ""))
    _ODDS_SPORT_GAMES_CACHE[cache_key] = (
        now_ts,
        [dict(game) for game in games],
        [dict(event) for event in odds_events if isinstance(event, dict)],
    )
    priced_count = sum(
        bool(_attach_odds(
            odds_events,
            str(game["homeTeam"]["teamName"]),
            str(game["awayTeam"]["teamName"]),
            _parse_dt(game.get("gameDateTimeUTC")),
        ))
        for game in games
    )
    print(
        f"[SPORT FEED] {sport_key}: fixtures={len(games)}; "
        f"catalog={len(catalog_events)}; odds={len(odds_events)}; "
        f"scores={len(score_events)}; priced={priced_count}"
    )
    return games, odds_events


EUROLEAGUE_LIVE_API_BASE_URL = "https://api-live.euroleague.net/v2"


def _euroleague_season_year(target: date) -> int:
    """Return the starting year for the EuroLeague season containing a date."""
    return target.year if target.month >= 7 else target.year - 1


@lru_cache(maxsize=8)
def _euroleague_clubs_for_season(season_year: int) -> tuple[dict, ...]:
    """Load official club crests once per season from EuroLeague's own feed."""
    season_code = f"E{int(season_year)}"
    url = (
        f"{EUROLEAGUE_LIVE_API_BASE_URL}/competitions/E/"
        f"seasons/{season_code}/clubs"
    )
    try:
        payload = _get_json(url)
    except Exception as exc:
        print(
            f"[EUROLEAGUE] club catalog {season_code} unavailable: "
            f"{type(exc).__name__}"
        )
        return ()

    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return ()
    clubs = tuple(row for row in rows if isinstance(row, dict))
    print(f"[EUROLEAGUE] official club logo catalog {season_code}: {len(clubs)} clubs")
    return clubs


def _euroleague_name_key(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    return " ".join(re.findall(r"[a-z0-9]+", text))


@lru_cache(maxsize=512)
def _euroleague_team_logo(team_name: str, season_year: int) -> str | None:
    """Match a sportsbook team name to the official seasonal EuroLeague crest."""
    wanted = _euroleague_name_key(team_name)
    if not wanted:
        return None

    best_score = 0.0
    best_logo = None
    for club in _euroleague_clubs_for_season(season_year):
        names = []
        for field in (
            "clubPermanentName", "clubPermanentAlias", "name",
            "abbreviatedName", "editorialName", "tvCode", "code",
        ):
            value = club.get(field)
            if isinstance(value, (list, tuple)):
                names.extend(value)
            elif value:
                names.append(value)

        score = 0.0
        for name in names:
            candidate = _euroleague_name_key(name)
            if not candidate:
                continue
            if wanted == candidate:
                score = 1.0
                break
            if wanted in candidate or candidate in wanted:
                score = max(score, 0.94)
            score = max(
                score,
                difflib.SequenceMatcher(None, wanted, candidate).ratio(),
            )

        if score > best_score:
            images = club.get("images") or {}
            logo = images.get("crest") if isinstance(images, dict) else None
            if logo:
                best_score = score
                best_logo = str(logo)

    # Avoid assigning an unrelated crest if a provider changes its team labels.
    return best_logo if best_score >= 0.60 else None


def _nba_normalize(game: dict, odds_events: list[dict]) -> dict:
    away = _nba_team(game.get("awayTeam"), game, "away")
    home = _nba_team(game.get("homeTeam"), game, "home")
    away_name = _nba_name(away)
    home_name = _nba_name(home)

    try:
        status_code = int(game.get("gameStatus") or 0)
    except (TypeError, ValueError):
        status_code = 0

    if status_code == 2:
        status = "LIVE"
    elif status_code == 3:
        status = "FT"
    else:
        status = "NS"

    away_score = away.get("score")
    home_score = home.get("score")
    winner = _winner_by_score(home_name, away_name, home_score, away_score) if status == "FT" else None
    raw_dt = game.get("gameDateTimeUTC") or game.get("gameTimeUTC")
    odds = _attach_odds(odds_events, home_name, away_name, _parse_dt(raw_dt))
    date_text, time_text = _date_time(raw_dt)

    return {
        "sport": "basketball",
        "type": "team_match",
        "sport_icon": "🏀",
        "competition": "NBA",
        "home_name": home_name,
        "away_name": away_name,
        "home_logo": _nba_logo(home),
        "away_logo": _nba_logo(away),
        "home_score": home_score,
        "away_score": away_score,
        "status": status,
        "period": game.get("period"),
        "status_text": game.get("gameStatusText"),
        "winner": winner,
        "odds": odds,
        "date": date_text,
        "time": time_text,
        "match_id": str(game.get("gameId") or ""),
    }


def get_nba_messages(date_from: str | None = None, date_to: str | None = None):
    start = date.fromisoformat(date_from) if date_from else datetime.now(YEREVAN_TZ).date()
    end = date.fromisoformat(date_to) if date_to else start
    games = _get_nba_games(start, end)

    odds_events = []
    if THE_ODDS_API_KEY:
        try:
            odds_events = _odds_api_get(
                "/sports/basketball_nba/odds",
                {
                    "regions": NBA_ODDS_REGION,
                    "markets": "h2h",
                    "oddsFormat": "decimal",
                    "dateFormat": "iso",
                },
            )
        except Exception:
            odds_events = []

    return [("NBA event", _nba_normalize(game, odds_events)) for game in games]





def _get_odds_basketball_messages(
    sport_key: str,
    competition: str,
    date_from: str | None,
    date_to: str | None,
    regions: str,
):
    start = date.fromisoformat(date_from) if date_from else datetime.now(YEREVAN_TZ).date()
    end = date.fromisoformat(date_to) if date_to else start
    games, odds_events = _get_odds_api_sport_games(
        sport_key, start, end, regions=regions
    )
    result = []
    for game in games:
        item = _nba_normalize(game, odds_events)
        item["competition"] = competition
        result.append((f"{competition} event", item))
    return result


def get_nba_preseason_messages(
    date_from: str | None = None,
    date_to: str | None = None,
):
    """NBA preseason fixtures from The Odds API's dedicated preseason feed."""
    return _get_odds_basketball_messages(
        "basketball_nba_preseason",
        "NBA Preseason",
        date_from,
        date_to,
        NBA_ODDS_REGION,
    )


def get_euroleague_messages(
    date_from: str | None = None,
    date_to: str | None = None,
):
    """EuroLeague fixtures from The Odds API's dedicated league feed."""
    start = date.fromisoformat(date_from) if date_from else datetime.now(YEREVAN_TZ).date()
    end = date.fromisoformat(date_to) if date_to else start
    messages = _get_odds_basketball_messages(
        "basketball_euroleague",
        "EuroLeague",
        start.isoformat(),
        end.isoformat(),
        EUROLEAGUE_ODDS_REGION,
    )
    season_year = _euroleague_season_year(start)
    for _, item in messages:
        for side in ("home", "away"):
            item[f"{side}_logo"] = _euroleague_team_logo(
                str(item.get(f"{side}_name") or ""), season_year
            )
    return messages


def _odds_api_get(
    path: str, params: dict | None = None, *, timeout: float | None = None
):
    if not THE_ODDS_API_KEY:
        return []
    query = dict(params or {})
    query["apiKey"] = THE_ODDS_API_KEY
    return _get_json(
        f"{THE_ODDS_API_BASE_URL}{path}",
        params=query,
        timeout=TIMEOUT if timeout is None else timeout,
    )


def _odds_team_key(name: str | None) -> str:
    """Normalize common NHL provider aliases before matching event names."""
    normalized = _normalize_odds_team_name(name)
    aliases = {
        "utah hockey": "utah mammoth",
        "ny islanders": "new york islanders",
        "ny rangers": "new york rangers",
        "la kings": "los angeles kings",
        "la ducks": "anaheim ducks",
    }
    return aliases.get(normalized, normalized)


def _odds_event_matches(event: dict, left: str, right: str) -> bool:
    home = event.get("home_team")
    away = event.get("away_team")
    return (
        (
            _similar(home, left) >= 0.88
            or _odds_team_key(home) == _odds_team_key(left)
        ) and (
            _similar(away, right) >= 0.88
            or _odds_team_key(away) == _odds_team_key(right)
        )
    ) or (
        (
            _similar(home, right) >= 0.88
            or _odds_team_key(home) == _odds_team_key(right)
        ) and (
            _similar(away, left) >= 0.88
            or _odds_team_key(away) == _odds_team_key(left)
        )
    )


def _extract_h2h(event: dict, left: str, right: str) -> dict | None:
    bookmakers = event.get("bookmakers") or []
    if not bookmakers:
        return None

    preferred = []
    if THE_ODDS_BOOKMAKER:
        preferred = [
            b for b in bookmakers
            if str(b.get("key") or b.get("title") or "").lower() == THE_ODDS_BOOKMAKER
        ]
    ordered = preferred + [b for b in bookmakers if b not in preferred]

    for bookmaker in ordered:
        for market in bookmaker.get("markets") or []:
            if str(market.get("key") or "").lower() != "h2h":
                continue
            values = {}
            for outcome in market.get("outcomes") or []:
                name = str(outcome.get("name") or "").strip()
                price = _decimal(outcome.get("price"))
                if price is None:
                    continue
                if _similar(name, left) >= 0.88 or _odds_team_key(name) == _odds_team_key(left):
                    values["left"] = price
                elif _similar(name, right) >= 0.88 or _odds_team_key(name) == _odds_team_key(right):
                    values["right"] = price
                elif name.lower() in {"draw", "tie"}:
                    values["draw"] = price
            if "left" in values and "right" in values:
                return {
                    "left": values["left"],
                    "right": values["right"],
                    "draw": values.get("draw"),
                    "bookmaker": bookmaker.get("title") or bookmaker.get("key") or "Odds",
                    "provider": "The Odds API",
                }
    return None


def _attach_odds(
    events: list[dict], left: str, right: str, start_dt: datetime | None = None,
) -> dict | None:
    for event in events:
        if not _odds_event_matches(event, left, right):
            continue
        event_dt = _parse_dt(event.get("commence_time"))
        if start_dt and event_dt and abs((start_dt - event_dt).total_seconds()) > 18 * 3600:
            continue
        return _extract_h2h(event, left, right)
    return None


def _oddspapi_get(path: str, params: dict | None = None):
    """GET from OddsPapi v4 using the API key query parameter."""
    if not ODDSPAPI_API_KEY:
        return []
    query = dict(params or {})
    query["apiKey"] = ODDSPAPI_API_KEY
    return _get_json(f"{ODDSPAPI_BASE_URL}{path}", params=query)


def _oddspapi_market_outcomes(bookmaker_data: dict) -> list[tuple[str, float]]:
    """Extract outcome labels/prices from OddsPapi's nested bookmakerOdds shape."""
    results = []
    markets = bookmaker_data.get("markets") or {}
    if not isinstance(markets, dict):
        return results

    for market_id, market in markets.items():
        if not isinstance(market, dict) or market.get("marketActive") is False:
            continue
        outcomes = market.get("outcomes") or {}
        if not isinstance(outcomes, dict):
            continue
        for outcome_id, outcome in outcomes.items():
            if not isinstance(outcome, dict):
                continue
            players = outcome.get("players") or {}
            if not isinstance(players, dict):
                continue
            for player in players.values():
                if not isinstance(player, dict) or player.get("active") is False:
                    continue
                price = _decimal(player.get("price"))
                if price is None:
                    continue
                label = " ".join(
                    str(x or "") for x in [
                        outcome_id,
                        player.get("bookmakerOutcomeId"),
                        player.get("playerName"),
                    ]
                ).strip()
                results.append((label, price))
    return results


def _oddspapi_extract_nhl_moneyline(event: dict, home: str, away: str) -> dict | None:
    """Extract the NHL winner-including-OT line from OddsPapi bookmakerOdds.

    OddsPapi uses numeric market/outcome IDs in the odds payload instead of
    human-readable names. For NHL, market 151 is the full-game winner market;
    outcome 151 is participant 1 and outcome 152 is participant 2.
    """
    if not isinstance(event, dict) or not event.get("hasOdds"):
        return None

    bookmakers = event.get("bookmakerOdds") or {}
    if not isinstance(bookmakers, dict):
        return None

    ordered = list(bookmakers.items())
    if ODDSPAPI_NHL_BOOKMAKERS:
        wanted = {x.strip().lower() for x in ODDSPAPI_NHL_BOOKMAKERS.split(",") if x.strip()}
        preferred = [(k, v) for k, v in ordered if str(k).lower() in wanted]
        rest = [(k, v) for k, v in ordered if str(k).lower() not in wanted]
        ordered = preferred + rest

    for bookmaker_key, bookmaker in ordered:
        if not isinstance(bookmaker, dict) or bookmaker.get("suspended") is True:
            continue
        markets = bookmaker.get("markets") or {}
        if not isinstance(markets, dict):
            continue

        market = markets.get(ODDSPAPI_NHL_MONEYLINE_MARKET_ID)
        if not isinstance(market, dict) or market.get("marketActive") is False:
            continue
        outcomes = market.get("outcomes") or {}
        if not isinstance(outcomes, dict):
            continue

        def outcome_price(outcome_id: str) -> float | None:
            outcome = outcomes.get(outcome_id)
            if not isinstance(outcome, dict):
                return None
            players = outcome.get("players") or {}
            if not isinstance(players, dict):
                return None
            for player in players.values():
                if not isinstance(player, dict) or player.get("active") is False:
                    continue
                price = _decimal(player.get("price"))
                if price is not None:
                    return price
            return None

        home_price = outcome_price(ODDSPAPI_NHL_HOME_OUTCOME_ID)
        away_price = outcome_price(ODDSPAPI_NHL_AWAY_OUTCOME_ID)
        if home_price is not None and away_price is not None:
            return {
                "left": home_price,
                "right": away_price,
                "draw": None,
                "bookmaker": str(bookmaker_key) or "OddsPapi",
                "provider": "OddsPapi",
            }

    return None


def _oddspapi_nhl_tournament_ids() -> list[str]:
    """Return the configured NHL tournament ID without a rate-limited lookup.

    OddsPapi currently documents NHL as tournament 234. Keep an environment
    override for future catalog changes or accounts with a different ID.
    """
    configured = [
        value.strip()
        for value in ODDSPAPI_NHL_TOURNAMENT_ID.split(",")
        if value.strip()
    ]
    return list(dict.fromkeys(configured or ["234"]))


def _oddspapi_nhl_odds(events: list[dict]) -> dict[str, dict]:
    """Load NHL fixtures + bookmaker odds from OddsPapi and index by team names.

    OddsPapi's /odds-by-tournaments response contains fixture IDs and odds but
    does not include participant names in the response documented for v4. We
    therefore fetch /fixtures once to resolve fixtureId -> participant names,
    then join those rows to /odds-by-tournaments.
    """
    if not ODDSPAPI_API_KEY:
        return {}

    fixture_meta = {}
    odds_rows = []
    joined_rows = 0
    for tournament_id in _oddspapi_nhl_tournament_ids():
        try:
            fixtures_payload = _oddspapi_get(
                "/fixtures", {"tournamentId": tournament_id, "language": "en"}
            )
            if isinstance(fixtures_payload, dict):
                fixture_rows = (
                    fixtures_payload.get("data")
                    or fixtures_payload.get("fixtures")
                    or fixtures_payload.get("events")
                    or []
                )
            else:
                fixture_rows = fixtures_payload or []
            if not isinstance(fixture_rows, list):
                fixture_rows = []

            for row in fixture_rows:
                if not isinstance(row, dict):
                    continue
                fixture_id = str(row.get("fixtureId") or "").strip()
                p1 = row.get("participant1Name") or row.get("participant1ShortName")
                p2 = row.get("participant2Name") or row.get("participant2ShortName")
                if fixture_id and p1 and p2:
                    fixture_meta[fixture_id] = {
                        "home": str(p1),
                        "away": str(p2),
                        "home_abbr": row.get("participant1Abbr"),
                        "away_abbr": row.get("participant2Abbr"),
                        "startTime": row.get("startTime"),
                    }

            params = {
                "tournamentIds": tournament_id,
                "language": "en",
                "verbosity": 3,
                "oddsFormat": "decimal",
            }
            if ODDSPAPI_NHL_BOOKMAKERS:
                params["bookmakers"] = ODDSPAPI_NHL_BOOKMAKERS
            payload = _oddspapi_get("/odds-by-tournaments", params)
            if isinstance(payload, dict):
                rows = payload.get("data") or payload.get("events") or payload.get("fixtures") or []
                if not rows and payload.get("fixtureId"):
                    rows = [payload]
            else:
                rows = payload or []
            if isinstance(rows, list):
                odds_rows.extend(rows)
        except Exception as exc:
            print(f"[NHL ODDS] OddsPapi tournament {tournament_id} failed: {exc}")

    index = {}
    for row in odds_rows:
        if not isinstance(row, dict):
            continue
        fixture_id = str(row.get("fixtureId") or "").strip()
        meta = fixture_meta.get(fixture_id)
        if not meta:
            continue
        joined_rows += 1
        odds = _oddspapi_extract_nhl_moneyline(row, meta["home"], meta["away"])
        if not odds:
            continue
        key = (_norm(meta["home"]), _norm(meta["away"]))
        index[key] = {
            **odds,
            "startTime": row.get("startTime") or meta.get("startTime"),
            "fixtureId": fixture_id,
            "home": meta["home"],
            "away": meta["away"],
            "home_abbr": meta.get("home_abbr"),
            "away_abbr": meta.get("away_abbr"),
        }

    print(
        f"[NHL ODDS] OddsPapi tournaments={','.join(_oddspapi_nhl_tournament_ids())}, "
        f"fixtures={len(fixture_meta)}, odds_rows={len(odds_rows)}, "
        f"joined_rows={joined_rows}, "
        f"fixtures_with_moneyline={len(index)}"
    )
    return index


def _oddspapi_find_nhl_odds(index: dict[str, dict], home: str, away: str, start_dt=None) -> dict | None:
    home_key = _odds_team_key(home)
    away_key = _odds_team_key(away)
    best = None
    best_score = 0.0
    for (p1, p2), odds in index.items():
        p1_key = _odds_team_key(p1)
        p2_key = _odds_team_key(p2)
        score = max(
            (_similar(p1_key, home_key) + _similar(p2_key, away_key)) / 2,
            (_similar(p1_key, away_key) + _similar(p2_key, home_key)) / 2,
        )
        if score < 0.82:
            continue
        if start_dt and odds.get("startTime"):
            provider_dt = _parse_dt(odds["startTime"])
            if provider_dt and abs((provider_dt - start_dt).total_seconds()) > 36 * 3600:
                continue
        if score > best_score:
            best_score = score
            best = odds
    return best


def _krok_sport_odds(sport_key: str, left: str, right: str,
                     start_dt: datetime | None = None) -> dict | None:
    """Find moneyline odds for one event from KrokOdds direct feed.

    KrokOdds direct-feed events contain bookmakers -> markets -> outcomes,
    so this parser deliberately handles that real response shape.  It also
    resolves the NHL sport slug instead of assuming one fixed slug.
    """
    if not KROK_API_KEY:
        return None

    try:
        # Resolve the provider's actual sport slug.  Krok can use a slug such
        # as hockey_nhl/icehockey_nhl depending on the feed version.
        sport_keys = [sport_key]
        try:
            sports_payload = _krok_get("/sports") or {}
            sports_rows = (
                sports_payload.get("data")
                if isinstance(sports_payload, dict)
                else sports_payload
            )
            if isinstance(sports_rows, list):
                discovered = []
                for row in sports_rows:
                    if not isinstance(row, dict):
                        continue
                    key = str(
                        row.get("sport") or row.get("key") or row.get("sport_key") or ""
                    ).strip()
                    label = str(
                        row.get("label") or row.get("title") or row.get("name") or ""
                    ).lower()
                    category = str(
                        row.get("category") or row.get("group") or ""
                    ).lower()
                    haystack = f"{key} {label} {category}"
                    if "nhl" in haystack or (sport_key in haystack):
                        if key and key not in discovered:
                            discovered.append(key)
                # Prefer a key containing nhl, then the supplied fallback.
                discovered.sort(key=lambda x: ("nhl" not in x.lower(), len(x)))
                sport_keys = discovered + [k for k in sport_keys if k not in discovered]
        except Exception as exc:
            print(f"[KROK] sport discovery failed: {exc}")

        best = None
        best_score = 0.0
        best_book_score = 0.0
        total_events = 0

        for resolved_sport in sport_keys[:5]:
            payload = _krok_get(
                f"/odds-feed/sports/{resolved_sport}",
                {"markets": "true", "limit": 100},
            ) or {}
            events = payload.get("data") if isinstance(payload, dict) else payload
            if not isinstance(events, list):
                continue
            total_events += len(events)

            for event in events:
                if not isinstance(event, dict):
                    continue
                event_home = event.get("home_team") or event.get("homeTeam")
                event_away = event.get("away_team") or event.get("awayTeam")
                if not event_home or not event_away:
                    continue

                score = max(
                    _similar(event_home, left) + _similar(event_away, right),
                    _similar(event_home, right) + _similar(event_away, left),
                ) / 2
                if score < 0.80:
                    continue

                if start_dt is not None:
                    raw_start = (
                        event.get("commence_time") or event.get("start_time")
                        or event.get("startTime") or event.get("kickoff_utc")
                    )
                    if raw_start:
                        try:
                            event_dt = datetime.fromisoformat(
                                str(raw_start).replace("Z", "+00:00")
                            )
                            if event_dt.tzinfo is None:
                                event_dt = event_dt.replace(tzinfo=UTC_TZ)
                            check_dt = start_dt
                            if check_dt.tzinfo is None:
                                check_dt = check_dt.replace(tzinfo=UTC_TZ)
                            if abs((event_dt - check_dt).total_seconds()) > 36 * 3600:
                                continue
                        except (TypeError, ValueError):
                            pass

                bookmakers = event.get("bookmakers") or []
                if not isinstance(bookmakers, list):
                    bookmakers = []

                # Some Krok responses expose markets directly on the event.
                # Keep that as a fallback for compatibility.
                if not bookmakers and event.get("markets"):
                    bookmakers = [{
                        "key": event.get("bookmaker_key") or "krokodds",
                        "title": event.get("bookmaker_title") or "KrokOdds",
                        "markets": event.get("markets"),
                    }]

                for bookmaker in bookmakers:
                    if not isinstance(bookmaker, dict):
                        continue
                    bookmaker_key = str(
                        bookmaker.get("key") or bookmaker.get("name") or ""
                    ).strip().lower()
                    bookmaker_title = str(
                        bookmaker.get("title") or bookmaker.get("name")
                        or bookmaker_key or "KrokOdds"
                    ).strip()

                    # Prefer bet365 when Krok has it, otherwise accept any
                    # bookmaker with a complete moneyline.
                    bookmaker_pref = 1.0 if "bet365" in bookmaker_key or "bet365" in bookmaker_title.lower() else 0.0

                    for market in bookmaker.get("markets") or []:
                        if not isinstance(market, dict):
                            continue
                        market_key = str(
                            market.get("key") or market.get("market_key") or market.get("market") or ""
                        ).lower()
                        if market_key and market_key not in {
                            "h2h", "moneyline", "match_winner", "three_way_result"
                        }:
                            continue

                        outcomes = market.get("outcomes") or market.get("selections") or []
                        if not isinstance(outcomes, list):
                            continue

                        values = {}
                        for selection in outcomes:
                            if not isinstance(selection, dict):
                                continue
                            name = str(
                                selection.get("name") or selection.get("selection")
                                or selection.get("description") or ""
                            ).strip()
                            price = _decimal(
                                selection.get("price")
                                if selection.get("price") is not None
                                else selection.get("odds")
                            )
                            if not name or price is None:
                                continue
                            if _similar(name, left) >= 0.88:
                                values["left"] = price
                            elif _similar(name, right) >= 0.88:
                                values["right"] = price
                            elif name.lower() in {"draw", "tie", "x"}:
                                values["draw"] = price

                        if "left" not in values or "right" not in values:
                            continue

                        candidate = {
                            "left": values["left"],
                            "right": values["right"],
                            "draw": values.get("draw"),
                            "bookmaker": bookmaker_title,
                            "provider": "KrokOdds",
                        }
                        # First maximize team/event match, then bookmaker
                        # preference (bet365 if present).
                        if score > best_score or (
                            score == best_score and bookmaker_pref > best_book_score
                        ):
                            best_score = score
                            best_book_score = bookmaker_pref
                            best = candidate

            if best is not None and best_score >= 0.95 and best_book_score >= 1.0:
                break

        print(
            f"[NHL ODDS] Krok feed: events={total_events}, "
            f"matched={'YES' if best else 'NO'}"
        )
        return best
    except Exception as exc:
        print(f"[KROK] {sport_key} odds failed: {exc}")
        return None


def _nhl_team_name(team: dict) -> str:
    """Return a stable NHL team name from the official NHL score feed."""
    team = team or {}
    abbrev = str(team.get("abbrev") or "").strip().upper()
    place = team.get("placeName")
    place = place.get("default") if isinstance(place, dict) else place
    common = team.get("commonName")
    common = common.get("default") if isinstance(common, dict) else common
    full = " ".join(
        x for x in (str(place or "").strip(), str(common or "").strip()) if x
    ).strip()
    return full or NHL_TEAM_NAMES_BY_ABBREV.get(abbrev, abbrev or "Team")


NHL_TEAM_NAMES_BY_ABBREV = {
    "ANA": "Anaheim Ducks",
    "BOS": "Boston Bruins",
    "BUF": "Buffalo Sabres",
    "CGY": "Calgary Flames",
    "CAR": "Carolina Hurricanes",
    "CHI": "Chicago Blackhawks",
    "COL": "Colorado Avalanche",
    "CBJ": "Columbus Blue Jackets",
    "DAL": "Dallas Stars",
    "DET": "Detroit Red Wings",
    "EDM": "Edmonton Oilers",
    "FLA": "Florida Panthers",
    "LAK": "Los Angeles Kings",
    "MIN": "Minnesota Wild",
    "MTL": "Montreal Canadiens",
    "NSH": "Nashville Predators",
    "NJD": "New Jersey Devils",
    "NYI": "New York Islanders",
    "NYR": "New York Rangers",
    "OTT": "Ottawa Senators",
    "PHI": "Philadelphia Flyers",
    "PIT": "Pittsburgh Penguins",
    "SJS": "San Jose Sharks",
    "SEA": "Seattle Kraken",
    "STL": "St. Louis Blues",
    "TBL": "Tampa Bay Lightning",
    "TOR": "Toronto Maple Leafs",
    "UTA": "Utah Mammoth",
    "VAN": "Vancouver Canucks",
    "VGK": "Vegas Golden Knights",
    "WPG": "Winnipeg Jets",
    "WSH": "Washington Capitals",
}


NHL_ESPN_LOGO_SLUG = {
    "NJD": "nj", "NYI": "nyi", "NYR": "nyr", "PHI": "phi", "PIT": "pit",
    "BOS": "bos", "BUF": "buf", "MTL": "mtl", "OTT": "ott", "TOR": "tor",
    "CAR": "car", "FLA": "fla", "TBL": "tb", "WSH": "wsh", "CHI": "chi",
    "DET": "det", "NSH": "nsh", "STL": "stl", "CGY": "cgy", "COL": "col",
    "EDM": "edm", "VAN": "van", "ANA": "ana", "DAL": "dal", "LAK": "la",
    "SJS": "sj", "CBJ": "cbj", "MIN": "min", "WPG": "wpg", "VGK": "vgk",
    "SEA": "sea", "UTA": "utah",
}


def _nhl_logo(team: dict) -> str | None:
    """Return a raster NHL logo URL that bot.py can render on Windows.

    bot.py's generic asset loader expects a URL string. NHL's official logo
    feed is SVG, but the user's Windows setup does not have a working Cairo
    renderer, so returning the ESPN PNG fallback here prevents the NHL logos
    from disappearing from cards.
    """
    team = team or {}
    abbrev = str(team.get("abbrev") or "").strip().upper()
    slug = NHL_ESPN_LOGO_SLUG.get(abbrev, abbrev.lower())
    if len(abbrev) == 3:
        return f"https://a.espncdn.com/i/teamlogos/nhl/500/{slug}.png"
    logo = team.get("logo") or team.get("darkLogo")
    return str(logo) if logo else None


def _nhl_normalize(game: dict, odds_events: list[dict]) -> dict:
    away = game.get("awayTeam") or {}
    home = game.get("homeTeam") or {}
    away_name = _nhl_team_name(away)
    home_name = _nhl_team_name(home)
    state = str(game.get("gameState") or "").upper()

    if state in {"LIVE", "CRIT"}:
        status = "LIVE"
    elif state in {"FINAL", "OFF"}:
        status = "FT"
    else:
        status = "NS"

    away_score = away.get("score")
    home_score = home.get("score")
    winner = _winner_by_score(home_name, away_name, home_score, away_score) if status == "FT" else None
    odds = _attach_odds(
        odds_events, home_name, away_name, _parse_dt(game.get("startTimeUTC"))
    )
    date_text, time_text = _date_time(game.get("startTimeUTC"))
    period = game.get("period") or (game.get("periodDescriptor") or {}).get("number")
    clock = (game.get("clock") or {}).get("timeRemaining")

    return {
        "sport": "hockey",
        "type": "team_match",
        "sport_icon": "🏒",
        "competition": "NHL",
        "home_name": home_name,
        "away_name": away_name,
        "home_logo": _nhl_logo(home),
        "away_logo": _nhl_logo(away),
        "home_score": home_score,
        "away_score": away_score,
        "status": status,
        "period": period,
        "clock": clock,
        "status_text": game.get("gameState"),
        "winner": winner,
        "odds": odds,
        "date": date_text,
        "time": time_text,
        "match_id": str(game.get("id") or ""),
    }


def _nhl_odds_team(name: str, score):
    normalized = _norm(name)
    abbrev = next(
        (code for code, full_name in NHL_TEAM_NAMES_BY_ABBREV.items()
         if _norm(full_name) == normalized),
        None,
    )
    if abbrev is None:
        candidates = [
            (_similar(name, full_name), code)
            for code, full_name in NHL_TEAM_NAMES_BY_ABBREV.items()
        ]
        if candidates:
            similarity, candidate = max(candidates)
            if similarity >= 0.90:
                abbrev = candidate
    if abbrev:
        return {"abbrev": abbrev, "score": score}
    return {
        "placeName": {"default": name},
        "commonName": {"default": ""},
        "score": score,
    }


def get_nhl_preseason_messages(
    date_from: str | None = None,
    date_to: str | None = None,
):
    """NHL preseason fixtures from The Odds API's dedicated preseason feed."""
    start = date.fromisoformat(date_from) if date_from else datetime.now(YEREVAN_TZ).date()
    end = date.fromisoformat(date_to) if date_to else start
    games, odds_events = _get_odds_api_sport_games(
        "icehockey_nhl_preseason", start, end, regions=NHL_ODDS_REGION
    )

    result = []
    for source in games:
        game = dict(source)
        status_code = int(game.get("gameStatus") or 1)
        game["gameState"] = "FINAL" if status_code == 3 else (
            "LIVE" if status_code == 2 else "FUT"
        )
        game["startTimeUTC"] = game.get("gameDateTimeUTC")
        game["homeTeam"] = _nhl_odds_team(
            str((game.get("homeTeam") or {}).get("teamName") or "Home"),
            (game.get("homeTeam") or {}).get("score"),
        )
        game["awayTeam"] = _nhl_odds_team(
            str((game.get("awayTeam") or {}).get("teamName") or "Away"),
            (game.get("awayTeam") or {}).get("score"),
        )
        item = _nhl_normalize(game, odds_events)
        item["competition"] = "NHL Preseason"
        result.append(("NHL preseason event", item))
    return result


def _get_nhl_day(target: date) -> list[dict]:
    payload = _get_json(f"{NHL_SCORE_URL}/{target.isoformat()}")
    return payload.get("games", []) or []


def get_nhl_messages(date_from: str | None = None, date_to: str | None = None):
    start = date.fromisoformat(date_from) if date_from else datetime.now(YEREVAN_TZ).date()
    end = date.fromisoformat(date_to) if date_to else start

    games_by_id = {}
    with ThreadPoolExecutor(max_workers=min(8, max(1, (end - start).days + 1))) as pool:
        futures = {pool.submit(_get_nhl_day, day): day for day in _days(start, end)}
        for future in as_completed(futures):
            for game in future.result():
                game_id = str(game.get("id") or f"{futures[future]}-{id(game)}")
                games_by_id[game_id] = game

    odds_events = []
    oddspapi_index = {}
    print(
        f"[NHL ODDS] OddsPapi key loaded: {bool(ODDSPAPI_API_KEY)}; "
        f"The Odds API key loaded: {bool(THE_ODDS_API_KEY)}; "
        f"KrokOdds key loaded: {bool(KROK_API_KEY)}"
    )
    if ODDSPAPI_API_KEY:
        oddspapi_index = _oddspapi_nhl_odds(games_by_id.values())
    if THE_ODDS_API_KEY:
        try:
            odds_events = _odds_api_get(
                "/sports/icehockey_nhl/odds",
                {
                    "regions": NHL_ODDS_REGION,
                    "markets": "h2h",
                    "oddsFormat": "decimal",
                    "dateFormat": "iso",
                },
            )
        except Exception as exc:
            print(f"[NHL ODDS] The Odds API request failed: {exc}")
            odds_events = []
    print(f"[NHL ODDS] The Odds API events: {len(odds_events)}")

    games = list(games_by_id.values())
    games.sort(key=lambda g: str(g.get("startTimeUTC") or ""))

    result = []
    for game in games:
        item = _nhl_normalize(game, odds_events)

        # OddsPapi is the primary NHL odds source. Its tournament endpoint
        # returns the whole NHL board in one request, including bookmaker odds.
        if oddspapi_index:
            try:
                start_dt = _parse_dt(game.get("startTimeUTC"))
                oddspapi_odds = _oddspapi_find_nhl_odds(
                    oddspapi_index, item["home_name"], item["away_name"], start_dt
                )
                if oddspapi_odds:
                    item["odds"] = {
                        "left": oddspapi_odds["left"],
                        "right": oddspapi_odds["right"],
                        "draw": oddspapi_odds.get("draw"),
                        "bookmaker": oddspapi_odds.get("bookmaker") or "OddsPapi",
                    }
            except Exception as exc:
                print(f"[NHL ODDS] OddsPapi enrichment failed: {exc}")

        # Existing providers remain as fallbacks if configured.
        if KROK_API_KEY and not item.get("odds"):
            try:
                start_raw = game.get("startTimeUTC")
                start_dt = _parse_dt(start_raw) if start_raw else None
                krok_odds = _krok_sport_odds(
                    "icehockey_nhl",
                    item["home_name"],
                    item["away_name"],
                    start_dt,
                )
                if krok_odds:
                    item["odds"] = krok_odds
            except Exception as exc:
                print(f"[KROK] NHL odds enrichment failed: {exc}")
        result.append(("NHL event", item))

    attached = sum(1 for _, item in result if item.get("odds"))
    missing = [
        f"{item.get('home_name')} vs {item.get('away_name')}"
        for _, item in result
        if not item.get("odds")
    ]
    message = f"[NHL] Odds attached: {attached}/{len(result)}"
    if missing:
        message += "; no prices for: " + " | ".join(missing)
    print(message)
    return result


# ============================================================
# Tennis
# ============================================================


def _tennis_key() -> str:
    return (os.getenv("TENNIS_RAPIDAPI_KEY") or os.getenv("RAPIDAPI_KEY") or "").strip()


def _tennis_get(path: str, params: dict | None = None):
    key = _tennis_key()
    if not key:
        raise RuntimeError("Tennis API key is missing. Add TENNIS_RAPIDAPI_KEY=YOUR_KEY to .env")
    return _get_json(
        f"{TENNIS_BASE_URL}{path}",
        headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": TENNIS_HOST},
        params=params,
        timeout=min(TIMEOUT, 8),
    )


def _tennis_tournament(row: dict) -> dict:
    value = row.get("tournament") or {}
    return value if isinstance(value, dict) else {}


def _tennis_rank(row: dict) -> int:
    tournament = _tennis_tournament(row)
    rank = tournament.get("rank")
    if isinstance(rank, dict):
        rank = rank.get("rank_id") or rank.get("id") or rank.get("rankId")
    try:
        return int(rank or tournament.get("rankId") or 0)
    except (TypeError, ValueError):
        return 0


def _tennis_tournament_name(row: dict) -> str:
    tournament = _tennis_tournament(row)
    return str(tournament.get("name") or tournament.get("tournamentName") or f"Tournament {row.get('tournamentId', '')}").strip()


def _tennis_player_id(player: dict) -> int | None:
    for key in ("id", "playerId", "player_id", "idPlayer"):
        value = player.get(key)
        try:
            player_id = int(value)
            if player_id > 0:
                return player_id
        except (TypeError, ValueError):
            continue
    return None


def _wikipedia_player_thumbnail(source: str, width: int = 480) -> str:
    """Use a sized Wikimedia thumbnail instead of downloading full-resolution photos."""
    parsed = urlsplit(str(source or ""))
    marker = "/wikipedia/commons/"
    if parsed.netloc.lower() != "upload.wikimedia.org" or marker not in parsed.path:
        return str(source)

    relative = parsed.path.split(marker, 1)[1]
    if relative.startswith("thumb/"):
        # API thumbnail URLs can be tagged "thumbnail_unscaled" even though
        # their path is the original image. Existing /thumb/ URLs are already
        # sized and should be kept as supplied.
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))

    parts = relative.split("/")
    if len(parts) < 3:
        return str(source)
    filename = parts[-1]
    thumbnail_name = f"{width}px-{filename}"
    if filename.lower().endswith(".svg"):
        thumbnail_name += ".png"
    prefix = parsed.path.split(marker, 1)[0]
    thumbnail_path = (
        f"{prefix}{marker}thumb/{'/'.join(parts[:-1])}/"
        f"{filename}/{thumbnail_name}"
    )
    return urlunsplit((parsed.scheme, parsed.netloc, thumbnail_path, "", ""))


@lru_cache(maxsize=512)
def _atp_tour_player_search(
    player_name: str, timeout: float | None = None
) -> tuple[dict, ...]:
    """Fetch and cache ATP directory matches shared by portrait and flag lookup."""
    name = " ".join(str(player_name or "").split()).strip()
    if not name:
        return ()
    try:
        payload = _get_json(
            "https://www.atptour.com/-/ajax/playersearch/PlayerUrlSearch",
            headers={
                "User-Agent": "MatchRadar/1.0 (ATP tennis player portraits)",
                "Referer": "https://www.atptour.com/en/players/",
                "Accept": "application/json,text/plain,*/*",
            },
            params={"SearchTerm": name},
            timeout=TIMEOUT if timeout is None else timeout,
        ) or {}
    except Exception:
        return ()

    matches = payload.get("items") or payload.get("Items") or [] if isinstance(payload, dict) else []
    return tuple(dict(player) for player in matches if isinstance(player, dict))


@lru_cache(maxsize=512)
def _atp_tour_player_image(
    player_name: str, timeout: float | None = None
) -> str | None:
    """Resolve an ATP Tour headshot through ATP's own player directory."""
    name = " ".join(str(player_name or "").split()).strip()
    if not name:
        return None
    known_player_ids = {"cruz hewitt": "h0k0", "alexander zverev": "z355"}
    known_id = known_player_ids.get(_tennis_name_key(name))
    if not known_id:
        initials, surname = _tennis_name_identity(name)
        if initials == "a" and surname == "zverev":
            known_id = "z355"
    if known_id:
        return f"https://www.atptour.com/-/media/alias/player-headshot/{known_id}"

    matches = _atp_tour_player_search(name, timeout)
    # Prefer a full-name match before the looser initial/surname matcher.
    # For example, the ATP search for Alexander Zverev can list Alexander
    # Zverev Sr. first, which is a different player.
    for player in matches:
        result_name = str(player.get("Key") or player.get("key") or player.get("name") or "").strip()
        profile_path = str(player.get("Value") or player.get("value") or player.get("url") or "")
        if not result_name or not profile_path:
            continue
        if _tennis_name_key(name) != _tennis_name_key(result_name):
            continue
        player_slug_id = re.search(r"/([a-z0-9]{4})/(?:overview|player-stats)(?:[/?#]|$)", profile_path, re.IGNORECASE)
        if player_slug_id:
            return f"https://www.atptour.com/-/media/alias/player-headshot/{player_slug_id.group(1)}"

    for player in matches:
        result_name = str(player.get("Key") or player.get("key") or player.get("name") or "").strip()
        profile_path = str(player.get("Value") or player.get("value") or player.get("url") or "")
        if not result_name or not profile_path or not _tennis_names_match(name, result_name):
            continue
        player_slug_id = re.search(r"/([a-z0-9]{4})/(?:overview|player-stats)(?:[/?#]|$)", profile_path, re.IGNORECASE)
        if player_slug_id:
            return f"https://www.atptour.com/-/media/alias/player-headshot/{player_slug_id.group(1)}"
    return None


@lru_cache(maxsize=512)
def _atp_tour_player_country(
    player_name: str, timeout: float | None = None
) -> str:
    """Resolve nationality from the ATP player directory for odds-only rows."""
    name = " ".join(str(player_name or "").split()).strip()
    if not name:
        return ""
    for player in _atp_tour_player_search(name, timeout):
        result_name = str(
            player.get("Key") or player.get("key") or player.get("name") or ""
        ).strip()
        if not result_name or not _tennis_names_match(name, result_name):
            continue
        country = _tennis_profile_country(player)
        if country:
            return country

        # Card-time enrichment is best effort and must not hold up the whole
        # match list on a slow profile page.
        if timeout is not None:
            continue

        profile_path = str(player.get("Value") or player.get("value") or player.get("url") or "").strip()
        if not profile_path:
            continue
        profile_url = profile_path if profile_path.startswith("https://") else f"https://www.atptour.com{profile_path}"
        try:
            response = requests.get(
                profile_url,
                headers={"User-Agent": "MatchRadar/1.0 (ATP tennis player nationality)"},
                timeout=min(TIMEOUT, 8),
            )
            response.raise_for_status()
            source = html.unescape(response.text)
        except Exception:
            continue

        country = _tennis_country_from_profile_html(source)
        if country:
            return country
    return ""


@lru_cache(maxsize=512)
def _wta_tour_player_search(
    player_name: str, timeout: float | None = None
) -> tuple[dict, ...]:
    """Fetch and cache WTA directory rows shared by portrait and flag lookup."""
    name = " ".join(str(player_name or "").split()).strip()
    if not name:
        return ()
    try:
        payload = _get_json(
            "https://api.wtatennis.com/tennis/players/ranked",
            headers={"User-Agent": "MatchRadar/1.0 (WTA tennis player nationality)"},
            params={
                "page": 0,
                "pageSize": 50,
                "type": "rankSingles",
                "metric": "SINGLES",
                "name": name,
            },
            timeout=TIMEOUT if timeout is None else timeout,
        )
    except Exception:
        return ()

    candidates = []

    def collect(value):
        if isinstance(value, list):
            for child in value:
                collect(child)
        elif isinstance(value, dict):
            candidates.append(value)
            for key in ("player", "players", "data", "content", "results", "items"):
                child = value.get(key)
                if isinstance(child, (list, dict)):
                    collect(child)

    collect(payload)
    return tuple(dict(row) for row in candidates)


@lru_cache(maxsize=512)
def _wta_tour_player_country(
    player_name: str, timeout: float | None = None
) -> str:
    """Resolve nationality from the WTA player directory for odds-only rows."""
    name = " ".join(str(player_name or "").split()).strip()
    if not name:
        return ""

    for player in _wta_tour_player_search(name, timeout):
        candidate_name = str(player.get("fullName") or player.get("name") or player.get("playerName") or "").strip()
        if not candidate_name:
            candidate_name = " ".join(str(player.get(key) or "").strip() for key in ("firstName", "lastName")).strip()
        if candidate_name and _tennis_names_match(name, candidate_name):
            country = _tennis_profile_country(player)
            if country:
                return country
            if timeout is not None:
                continue
            player_id = player.get("id") or player.get("playerId")
            if player_id is not None:
                profile_html = _wta_tour_profile_html(player_id, candidate_name)
                country = _tennis_country_from_profile_html(profile_html)
                if country:
                    return country
    return ""


@lru_cache(maxsize=512)
def _wta_tour_profile_html(player_id: str | int, canonical_name: str) -> str:
    """Fetch one WTA profile page for both image and nationality metadata."""
    slug = "-" + re.sub(r"[^a-z0-9]+", "-", canonical_name.lower()).strip("-") + "-"
    profile_url = f"https://www.wtatennis.com/players/{quote(str(player_id))}/{quote(slug)}"
    try:
        response = requests.get(
            profile_url,
            headers={"User-Agent": "MatchRadar/1.0 (WTA tennis player profile)"},
            timeout=min(TIMEOUT, 8),
        )
        response.raise_for_status()
        return response.text
    except Exception:
        return ""


@lru_cache(maxsize=512)
def _wta_tour_player_image(player_name: str) -> str | None:
    """Resolve a WTA player portrait from her official profile page."""
    name = " ".join(str(player_name or "").split()).strip()
    if not name:
        return None
    player_id = None
    canonical_name = ""
    for row in _wta_tour_player_search(name):
        candidate_name = str(
            row.get("fullName") or row.get("name") or row.get("playerName") or ""
        ).strip()
        if not candidate_name:
            first = str(row.get("firstName") or "").strip()
            last = str(row.get("lastName") or "").strip()
            candidate_name = " ".join(x for x in (first, last) if x)
        if candidate_name and _tennis_names_match(name, candidate_name):
            player_id = row.get("id") or row.get("playerId")
            canonical_name = candidate_name
            if player_id is not None:
                break
    if player_id is None:
        return None

    profile_html = _wta_tour_profile_html(player_id, canonical_name)
    tags = re.findall(r"<meta\b[^>]*>", profile_html, flags=re.IGNORECASE)
    for tag in tags:
        property_match = re.search(
            r"(?:property|name)=[\"'](?:og:image|twitter:image)[\"']",
            tag,
            flags=re.IGNORECASE,
        )
        content_match = re.search(
            r"content=[\"']([^\"']+)[\"']", tag, flags=re.IGNORECASE
        )
        if property_match and content_match:
            image_url = html.unescape(content_match.group(1)).strip()
            if image_url.startswith("https://") and "wtatennis.com" in image_url:
                return image_url
    return None


@lru_cache(maxsize=512)
def _wikipedia_player_image(
    player_name: str,
    player_id: str | int | None = None,
    tour: str | None = None,
    fast: bool = False,
) -> str | None:
    """Resolve a real player portrait, expanding abbreviated feed names by roster id."""
    name = " ".join(str(player_name or "").split()).strip()
    if not name:
        return None

    if str(tour or "").strip().lower() == "atp":
        initials, surname = _tennis_name_identity(name)
        if (
            _tennis_name_key(name) == "alexander zverev"
            or (initials == "a" and surname == "zverev")
        ):
            # Use the confirmed ATP photo even when a live profile includes a
            # stale image URL that would otherwise stop the fallback chain.
            official_image = _atp_tour_player_image(name, 2.5 if fast else None)
            if official_image:
                return official_image

    if fast:
        # A missing portrait should never delay all ATP/WTA match cards.
        # Use at most one official directory lookup and one Wikipedia summary.
        if str(tour or "").strip().lower() == "atp":
            official_image = _atp_tour_player_image(name, 2.5)
            if official_image:
                return official_image
        try:
            payload = _get_json(
                "https://en.wikipedia.org/api/rest_v1/page/summary/"
                f"{quote(name.replace(' ', '_'))}",
                headers={"User-Agent": "MatchRadar/1.0 (tennis match-card player portraits)"},
                timeout=2.5,
            ) or {}
            thumbnail = (payload.get("thumbnail") or {}).get("source")
            original = (payload.get("originalimage") or {}).get("source")
            if thumbnail or original:
                return _wikipedia_player_thumbnail(str(thumbnail or original))
        except Exception:
            pass
        return None

    live_profile = {}
    live_lookup = globals().get("_live_tennis_player_details")
    if callable(live_lookup):
        try:
            live_profile = live_lookup(name, player_id) or {}
        except Exception:
            live_profile = {}
    live_image = str(live_profile.get("image") or "").strip()
    if live_image:
        return live_image
    player_id = live_profile.get("id") or player_id

    candidates = []
    canonical_name = str(live_profile.get("name") or "").strip()
    if canonical_name and canonical_name.casefold() != name.casefold():
        candidates.append(canonical_name)
    has_initial = bool(re.search(r"(?:^|\s)[A-Z]\.?(?:\s|$)", name, re.IGNORECASE))
    if player_id is not None and _live_tennis_key() and has_initial:
        try:
            profile = _live_tennis_get(f"/players/{quote(str(player_id))}") or {}
            profile = profile.get("data") if isinstance(profile, dict) and isinstance(profile.get("data"), dict) else profile
            canonical_name = str(profile.get("name") or "").strip() if isinstance(profile, dict) else ""
            if canonical_name:
                candidates.append(canonical_name)
        except Exception:
            pass

    candidates.append(name)
    parts = name.split()
    if len(parts) >= 3:
        short_name = f"{parts[0]} {parts[-1]}"
        if short_name.casefold() != name.casefold():
            candidates.append(short_name)
    candidates = list(dict.fromkeys(x for x in candidates if x))

    if str(tour or "").strip().lower() == "atp":
        for candidate in candidates:
            atp_image = _atp_tour_player_image(candidate)
            if atp_image:
                return atp_image

    headers = {"User-Agent": "MatchRadar/1.0 (tennis match-card player portraits)"}
    for candidate in candidates:
        try:
            url = (
                "https://en.wikipedia.org/api/rest_v1/page/summary/"
                f"{quote(candidate.replace(' ', '_'))}"
            )
            payload = _get_json(url, headers=headers)
        except Exception:
            payload = {}

        thumb = (payload.get("thumbnail") or {}).get("source") if isinstance(payload, dict) else None
        original = (payload.get("originalimage") or {}).get("source") if isinstance(payload, dict) else None
        if thumb or original:
            return _wikipedia_player_thumbnail(str(thumb or original))

    # One indexed lookup handles alternate article titles without repeating
    # the same search for every candidate spelling.
    search_candidate = candidates[0]
    try:
        search = _get_json(
            "https://en.wikipedia.org/w/api.php",
            headers=headers,
            params={
                "action": "query",
                "generator": "search",
                "gsrsearch": f'"{search_candidate}" tennis player',
                "gsrnamespace": 0,
                "gsrlimit": 5,
                "prop": "pageimages",
                "piprop": "thumbnail",
                "pithumbsize": 480,
                "format": "json",
            },
        ) or {}
        pages = (search.get("query") or {}).get("pages") or {}
        pages = pages.values() if isinstance(pages, dict) else pages
        for page in pages:
            title = str(page.get("title") or "")
            image = (page.get("thumbnail") or {}).get("source")
            if image and title and _tennis_names_match(search_candidate, title):
                return _wikipedia_player_thumbnail(str(image))
    except Exception:
        pass

    # Lower-ranked players may not have a Wikipedia article but can still have
    # an identified photo on Wikimedia Commons. Require the file title itself
    # to match the player before using it.
    try:
        commons = _get_json(
            "https://commons.wikimedia.org/w/api.php",
            headers=headers,
            params={
                "action": "query",
                "generator": "search",
                "gsrsearch": f'"{search_candidate}" tennis',
                "gsrnamespace": 6,
                "gsrlimit": 5,
                "prop": "imageinfo",
                "iiprop": "url",
                "iiurlwidth": 480,
                "format": "json",
            },
        ) or {}
        pages = (commons.get("query") or {}).get("pages") or {}
        pages = pages.values() if isinstance(pages, dict) else pages
        for page in pages:
            title = str(page.get("title") or "")
            photo_title = re.sub(r"(?i)^file:", "", title)
            photo_title = re.sub(r"\.[a-z0-9]{2,5}$", "", photo_title, flags=re.IGNORECASE)
            image_info = page.get("imageinfo") or []
            image = (image_info[0].get("thumburl") or image_info[0].get("url")) if image_info else None
            if image and photo_title and _tennis_names_match(search_candidate, photo_title):
                return _wikipedia_player_thumbnail(str(image))
    except Exception:
        pass

    if str(tour or "").strip().lower() == "wta":
        for candidate in candidates:
            official_image = _wta_tour_player_image(candidate)
            if official_image:
                return official_image
    return None


def _tennis_image(player: dict, tour: str) -> str | None:
    player_name = str(
        player.get("name") or player.get("playerName") or player.get("fullName") or ""
    ).strip()
    tour_key = str(tour or "").strip().lower()
    if tour_key == "atp" and player_name:
        initials, surname = _tennis_name_identity(player_name)
        if (
            _tennis_name_key(player_name) == "alexander zverev"
            or (initials == "a" and surname == "zverev")
        ):
            # Prefer the confirmed ATP headshot for Zverev over an incomplete
            # provider image URL or a mismatched numeric upload id.
            official_image = _atp_tour_player_image(player_name)
            if official_image:
                return official_image

    # Prefer the exact player photo returned by the Tennis API. Different
    # versions of the API have used slightly different field names.
    candidates = [
        player.get("image"),
        player.get("image_p_name"),
        player.get("photo"),
        player.get("photoUrl"),
        player.get("photo_url"),
        player.get("playerPhoto"),
        player.get("playerPhotoUrl"),
        player.get("avatar"),
        player.get("avatarUrl"),
        player.get("headshot"),
        player.get("headshotUrl"),
        player.get("imageUrl"),
    ]

    for image in candidates:
        if isinstance(image, dict):
            image = image.get("url") or image.get("src") or image.get("source")
        if not image:
            continue
        image = str(image).strip()
        if image.startswith("http://") or image.startswith("https://"):
            return image
        if image.startswith("/"):
            return f"{TENNIS_BASE_URL}{image}"

    player_id = _tennis_player_id(player)
    if player_id is not None:
        return (
            f"{TENNIS_BASE_URL}/tennis/v2/ms-api/uploads/Photo/"
            f"{tour.lower()}/{player_id:05d}.jpg"
        )

    player_name = str(player.get("name") or player.get("playerName") or "").strip()
    return _wikipedia_player_image(player_name) if player_name else None


def _tennis_rows_for_tour(tour: str, start: date, end: date):
    # Keep the request to the documented core query parameters. The previous
    # version added `include=tournament,round,odds`, which is not required by
    # the results/fixtures examples and can be rejected as a plan-restricted
    # resource by RapidAPI.
    # Tournament metadata is required for tournament name/rank based Top-10
    # selection. The API docs support fixtures by date and by date range.
    params = {
        "pageNo": 1,
        "pageSize": 500,
        "filter": "PlayerGroup:singles",
        "include": "tournament,round,tournament.rank,tournament.country",
    }

    today = datetime.now(YEREVAN_TZ).date()

    # Historical window: use the results date-range endpoint.
    if end < today:
        payload = _tennis_get(
            f"/tennis/v2/{tour}/results/{start.isoformat()}/{end.isoformat()}",
            params,
        )
        return payload.get("data", [])

    # Any window containing future dates must use the fixtures date-range
    # endpoint. The old implementation only requested today's fixtures when
    # start == today, which made Week/Month silently return only today's
    # matches. For a single day the documented dated-fixtures endpoint is used.
    if start >= today:
        if start == end:
            payload = _tennis_get(
                f"/tennis/v2/{tour}/fixtures/{start.isoformat()}", params
            )
        else:
            payload = _tennis_get(
                f"/tennis/v2/{tour}/fixtures/{start.isoformat()}/{end.isoformat()}",
                params,
            )
        return payload.get("data", [])

    # Mixed historical + current/future window: fetch the historical part and
    # the current/future part separately, then merge them.
    rows = []
    yesterday = today - timedelta(days=1)
    result_end = min(yesterday, end)
    if start <= result_end:
        payload = _tennis_get(
            f"/tennis/v2/{tour}/results/{start.isoformat()}/{result_end.isoformat()}",
            params,
        )
        rows.extend(payload.get("data", []))

    if today <= end:
        if today == end:
            payload = _tennis_get(
                f"/tennis/v2/{tour}/fixtures/{today.isoformat()}", params
            )
        else:
            payload = _tennis_get(
                f"/tennis/v2/{tour}/fixtures/{today.isoformat()}/{end.isoformat()}",
                params,
            )
        rows.extend(payload.get("data", []))
    return rows


def _odds_api_tennis_events(
    date_from: date, date_to: date, tour: str | None = None
):
    """Return current tennis events across the active ATP/WTA competitions.

    The previous implementation stopped after the first three competitions
    that happened to return events.  That could leave us with only one Odds API
    event even when the Live Tennis API had many ATP/WTA matches.  We now scan
    the active top-level tennis competitions (with a request cap) and use
    UK/EU bookmaker regions for broader coverage.
    """
    if not THE_ODDS_API_KEY:
        return []

    try:
        sports = _odds_api_get(
            "/sports", {"all": "true"}, timeout=min(TIMEOUT, 8)
        ) or []
    except Exception as exc:
        print(f"[TENNIS] The Odds API sports discovery failed: {exc}")
        return []

    def tournament_rank(row: dict) -> int:
        title = str(row.get("title") or row.get("description") or "").lower()
        key = str(row.get("key") or "").lower()
        if "grand slam" in title or any(x in key for x in (
            "aus_open", "french_open", "wimbledon", "us_open"
        )):
            return 100
        if "1000" in title or "masters" in title or "wta 1000" in title:
            return 90
        if "500" in title:
            return 80
        if "250" in title:
            return 70
        return 60

    candidates = []
    tour_key = str(tour or "").strip().lower()
    for row in sports:
        key = str(row.get("key") or "").strip()
        title = str(row.get("title") or key).strip()
        if not key.startswith("tennis_"):
            continue
        low_key = key.lower()
        if tour_key in {"atp", "wta"} and not re.search(
            rf"(?:^|_){re.escape(tour_key)}(?:_|$)", low_key
        ):
            continue
        # Skip futures/winner outright competitions.
        if "winner" in low_key or "championship" in low_key:
            continue
        if not bool(row.get("active")):
            continue
        if not ("atp" in low_key or "wta" in low_key):
            continue
        candidates.append((tournament_rank(row), key, title))

    candidates.sort(key=lambda x: (-x[0], x[1]))

    # Cap requests so a free/low-credit Odds API account is not exhausted by
    # scanning every historical tennis competition returned by /sports.
    max_competitions = max(1, int(os.getenv("TENNIS_ODDS_MAX_COMPETITIONS", "16")))
    candidates = candidates[:max_competitions]

    events = []
    successful_keys = 0
    for rank, sport_key, title in candidates:
        params = {
            "regions": TENNIS_ODDS_REGION,
            "markets": "h2h",
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        }
        if TENNIS_ODDS_BOOKMAKERS:
            params["bookmakers"] = TENNIS_ODDS_BOOKMAKERS
            params.pop("regions", None)

        try:
            raw_rows = _odds_api_get(
                f"/sports/{sport_key}/odds",
                params,
                timeout=min(TIMEOUT, 8),
            ) or []
            successful_keys += 1
        except Exception as exc:
            print(f"[TENNIS] The Odds API {sport_key} failed: {exc}")
            continue

        for event in raw_rows:
            dt = _parse_dt(event.get("commence_time"))
            if not dt or not (date_from <= dt.date() <= date_to):
                continue
            event["_tournament_title"] = title
            event["_tournament_rank"] = rank
            events.append(event)

    # Remove duplicate events returned through overlapping competition feeds.
    unique = {}
    for event in events:
        key = str(event.get("id") or "")
        if key:
            unique[key] = event

    result = list(unique.values())
    result.sort(key=lambda e: (_parse_dt(e.get("commence_time")) or datetime.max.replace(tzinfo=UTC_TZ)))
    print(
        f"[TENNIS] The Odds API competitions tried={len(candidates)}; "
        f"successful={successful_keys}; events in range={len(result)}; "
        f"regions={TENNIS_ODDS_REGION}; bookmakers={TENNIS_ODDS_BOOKMAKERS or 'all supported'}"
    )
    return result


def _oddspapi_extract_tennis_h2h(event: dict, left: str, right: str) -> dict | None:
    """Extract Tennis match-winner odds from OddsPapi.

    OddsPapi's Tennis winner market is normally market 121, with outcome 121
    for participant 1 and 122 for participant 2. Some responses can expose
    equivalent labels such as home/away or 1/2, so this parser handles both
    the documented numeric IDs and the text labels.
    """
    if not isinstance(event, dict):
        return None

    bookmakers = event.get("bookmakerOdds") or {}
    if not isinstance(bookmakers, dict):
        return None

    wanted = {
        x.strip().lower()
        for x in (ODDSPAPI_TENNIS_BOOKMAKERS or "bet365").split(",")
        if x.strip()
    }
    ordered = list(bookmakers.items())
    if wanted:
        ordered = (
            [(k, v) for k, v in ordered if str(k).lower() in wanted]
            + [(k, v) for k, v in ordered if str(k).lower() not in wanted]
        )

    for bookmaker_key, bookmaker in ordered:
        if not isinstance(bookmaker, dict):
            continue
        if bookmaker.get("suspended") is True or bookmaker.get("bookmakerIsActive") is False:
            continue

        markets = bookmaker.get("markets") or {}
        if not isinstance(markets, dict):
            continue

        for market_id, market in markets.items():
            if not isinstance(market, dict) or market.get("marketActive") is False:
                continue

            # Tennis match winner is market 121 in OddsPapi's current catalog.
            # Keep 1 as a compatibility fallback because the provider's public
            # sport page has shown both compact and long-form IDs in examples.
            market_id_text = str(market_id).strip().lower()
            if market_id_text not in {"121", "1"}:
                continue

            outcomes = market.get("outcomes") or {}
            if not isinstance(outcomes, dict):
                continue

            values: dict[str, float] = {}
            indexed_prices: list[tuple[str, float, str]] = []

            for outcome_id, outcome in outcomes.items():
                if not isinstance(outcome, dict):
                    continue
                players = outcome.get("players") or {}
                if not isinstance(players, dict):
                    continue

                # /v4/odds uses players["0"] as a single price object for the
                # match market. Tolerate a list just in case a provider variant
                # wraps it differently.
                player_items = list(players.values())
                for player in player_items:
                    if isinstance(player, list):
                        player_items.extend(player)
                        continue
                    if not isinstance(player, dict) or player.get("active") is False:
                        continue
                    price = _decimal(player.get("price"))
                    if price is None:
                        continue

                    outcome_id_text = str(outcome_id or "").strip().lower()
                    bookmaker_outcome = str(player.get("bookmakerOutcomeId") or "").strip().lower()
                    player_name = str(player.get("playerName") or "").strip()
                    label = f"{outcome_id_text} {bookmaker_outcome} {player_name}".strip()
                    indexed_prices.append((outcome_id_text, price, label))

                    if _tennis_names_match(player_name, left):
                        values["left"] = price
                    elif _tennis_names_match(player_name, right):
                        values["right"] = price
                    elif outcome_id_text in {"121", "1", "home", "participant1", "player1", "p1"}:
                        values["left"] = price
                    elif outcome_id_text in {"122", "2", "away", "participant2", "player2", "p2"}:
                        values["right"] = price
                    elif bookmaker_outcome in {"121", "1", "home", "participant1", "player1", "p1"}:
                        values["left"] = price
                    elif bookmaker_outcome in {"122", "2", "away", "participant2", "player2", "p2"}:
                        values["right"] = price

            # If the feed used opaque outcome labels, the documented winner
            # market still arrives in participant order. Use the first two
            # active prices as a final compatibility fallback.
            if "left" not in values or "right" not in values:
                if len(indexed_prices) >= 2:
                    values.setdefault("left", indexed_prices[0][1])
                    values.setdefault("right", indexed_prices[1][1])

            if "left" in values and "right" in values:
                return {
                    "left": values["left"],
                    "right": values["right"],
                    "draw": None,
                    "bookmaker": str(
                        bookmaker.get("bookmakerName") or bookmaker_key or "OddsPapi"
                    ),
                }

    return None


def _oddsapi_event_to_normalized(event: dict) -> dict:
    """Turn an OddsPapi fixture/odds row into the same shape used by matcher."""
    return {
        "home_team": str(event.get("participant1Name") or "").strip(),
        "away_team": str(event.get("participant2Name") or "").strip(),
        "hasOdds": bool(event.get("hasOdds")),
        "bookmakerOdds": event.get("bookmakerOdds") or {},
        "_oddspapi_fixture_id": event.get("fixtureId") or event.get("id"),
    }


_ODDSPAPI_TENNIS_CACHE: dict[tuple[str, str, str], tuple[float, list[dict]]] = {}
_ODDSPAPI_TENNIS_CACHE_TTL = 300.0
_ODDSPAPI_TENNIS_TOURNAMENTS_CACHE: tuple[float, list[dict]] | None = None
_ODDSPAPI_TENNIS_TOURNAMENTS_TTL = 21600.0  # 6 hours


def _oddspapi_tennis_tournament_score(provider_name: str, target_names: set[str]) -> float:
    """Score an OddsPapi tournament name against Live Tennis competition names."""
    provider = _tennis_name_key(provider_name)
    if not provider:
        return 0.0
    pt = set(provider.split())
    best = 0.0
    for target_name in target_names:
        target = _tennis_name_key(target_name)
        if not target:
            continue
        tt = set(target.split())
        if provider == target:
            best = max(best, 1.0)
            continue
        overlap = len(pt & tt) / max(1, min(len(pt), len(tt)))
        seq = difflib.SequenceMatcher(None, provider, target).ratio()
        score = 0.65 * overlap + 0.35 * seq
        best = max(best, score)
    return best


def _oddspapi_tennis_tournaments() -> list[dict]:
    """Get the Tennis tournament board with a long cache.

    The tournament catalogue changes slowly compared with match odds. Caching
    it avoids spending one billable request every time the user opens Today,
    Tomorrow, Week, or Month.
    """
    global _ODDSPAPI_TENNIS_TOURNAMENTS_CACHE
    now_ts = time.time()
    if _ODDSPAPI_TENNIS_TOURNAMENTS_CACHE:
        cached_at, cached_rows = _ODDSPAPI_TENNIS_TOURNAMENTS_CACHE
        if now_ts - cached_at < _ODDSPAPI_TENNIS_TOURNAMENTS_TTL:
            return [dict(x) for x in cached_rows]

    try:
        payload = _oddspapi_get(
            "/tournaments",
            {"sportId": ODDSPAPI_TENNIS_SPORT_ID, "language": "en"},
        )
        if isinstance(payload, dict):
            payload = payload.get("data") or payload.get("results") or payload.get("items") or []
        rows = [x for x in (payload or []) if isinstance(x, dict)]
        _ODDSPAPI_TENNIS_TOURNAMENTS_CACHE = (now_ts, [dict(x) for x in rows])
        return rows
    except Exception as exc:
        print(f"[TENNIS] OddsPapi tournament discovery failed: {exc}")
        return []


def _oddspapi_tennis_account_debug() -> None:
    """Log quota information without consuming a billable request."""
    if not ODDSPAPI_API_KEY:
        return
    try:
        payload = _oddspapi_get("/account")
        if isinstance(payload, dict):
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            plan = data.get("plan") or data.get("subscription") or data.get("name") or "?"
            request_count = data.get("request_count")
            request_limit = data.get("request_limit")
            print(
                f"[TENNIS] OddsPapi account: plan={plan}; "
                f"requests={request_count}/{request_limit}"
            )
    except Exception as exc:
        print(f"[TENNIS] OddsPapi account check failed: {exc}")


def _odds_papi_tennis_events(
    date_from: date,
    date_to: date,
    target_rows: list[dict] | None = None,
) -> list[dict]:
    """Fetch Tennis winner odds from OddsPapi using a very small request budget.

    Important: OddsPapi documents /odds-by-tournaments as accepting a comma-
    separated list, but some accounts/feeds can return 400 for mixed tournament
    batches. To make the bot robust, this implementation uses the exact current
    tournaments that match the Live Tennis rows and requests them one at a time.
    It also omits the bookmaker filter on this endpoint and extracts bet365 from
    the returned bookmakerOdds object. That removes one common source of 400s
    while still preferring real bet365 prices.
    """
    if not ODDSPAPI_API_KEY:
        return []

    wanted_bookmaker = (ODDSPAPI_TENNIS_BOOKMAKERS or "bet365").strip().lower()
    cache_key = (date_from.isoformat(), date_to.isoformat(), wanted_bookmaker)
    now_ts = time.time()
    cached = _ODDSPAPI_TENNIS_CACHE.get(cache_key)
    if cached and (now_ts - cached[0]) < _ODDSPAPI_TENNIS_CACHE_TTL:
        print(
            f"[TENNIS] OddsPapi cache hit: events={len(cached[1])}; "
            f"bookmaker={wanted_bookmaker}"
        )
        return [dict(x) for x in cached[1]]

    _oddspapi_tennis_account_debug()

    tournaments = _oddspapi_tennis_tournaments()
    target_names = {
        str(item.get("competition") or "").strip()
        for item in (target_rows or [])
        if str(item.get("competition") or "").strip()
    }

    scored: list[tuple[float, int, int, str]] = []
    for row in tournaments:
        tid = row.get("tournamentId") or row.get("id")
        name = str(row.get("tournamentName") or row.get("name") or "").strip()
        if tid is None or not name:
            continue
        try:
            future = int(row.get("futureFixtures") or row.get("upcomingFixtures") or 0)
        except (TypeError, ValueError):
            future = 0
        if future <= 0:
            continue
        try:
            tid_int = int(tid)
        except (TypeError, ValueError):
            continue
        score = _oddspapi_tennis_tournament_score(name, target_names)
        scored.append((score, future, tid_int, name))

    # Only query the few tournaments most likely to contain the exact matches
    # already returned by Live Tennis API. This keeps the request budget small.
    scored.sort(key=lambda x: (-x[0], -x[1], x[3]))
    selected: list[tuple[int, str]] = []
    seen_ids: set[int] = set()
    for score, future, tid, name in scored:
        if score < 0.30:
            continue
        if tid in seen_ids:
            continue
        selected.append((tid, name))
        seen_ids.add(tid)
        if len(selected) >= 4:
            break

    if not selected:
        for score, future, tid, name in sorted(scored, key=lambda x: (-x[1], -x[0], x[3])):
            if tid in seen_ids:
                continue
            selected.append((tid, name))
            seen_ids.add(tid)
            if len(selected) >= 2:
                break

    if not selected:
        print("[TENNIS] OddsPapi found no active Tennis tournaments")
        return []

    all_events: list[dict] = []
    successful_tournaments = 0

    for index, (tid, tournament_name) in enumerate(selected, start=1):
        # Do not send bookmakers=bet365 here. We will extract the real bet365
        # board from bookmakerOdds after the response arrives.
        params = {
            "tournamentIds": str(tid),
            "language": "en",
            "verbosity": 3,
            "oddsFormat": "decimal",
        }
        try:
            payload = _oddspapi_get("/odds-by-tournaments", params)
            if isinstance(payload, dict):
                payload = payload.get("data") or payload.get("results") or payload.get("items") or []
            rows = [x for x in (payload or []) if isinstance(x, dict)]
            all_events.extend(rows)
            successful_tournaments += 1
            print(
                f"[TENNIS] OddsPapi tournament {tid} ({tournament_name}) "
                f"returned {len(rows)} fixture rows"
            )
        except requests.exceptions.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            body = ""
            try:
                body = (exc.response.text or "").strip().replace("\\n", " ")[:300]
            except Exception:
                pass
            print(
                f"[TENNIS] OddsPapi tournament {tid} HTTP {status}: {body or exc}"
            )
            # Do not hammer the API on rate limiting. The account check above
            # tells us whether the quota is exhausted.
            if status == 429:
                break
        except Exception as exc:
            print(f"[TENNIS] OddsPapi tournament {tid} failed: {exc}")

        if index < len(selected):
            time.sleep(1.2)

    result: list[dict] = []
    seen_fixtures: set[str] = set()
    for event in all_events:
        if not bool(event.get("hasOdds")):
            continue
        fixture_id = str(event.get("fixtureId") or event.get("id") or "").strip()
        if not fixture_id or fixture_id in seen_fixtures:
            continue

        start_time = event.get("startTime") or event.get("trueStartTime")
        dt = _parse_dt(start_time)
        if dt and not (date_from <= dt.date() <= date_to):
            continue

        bookmaker_odds = event.get("bookmakerOdds") or {}
        if not isinstance(bookmaker_odds, dict) or not bookmaker_odds:
            continue

        # Keep only fixtures that really have the requested bookmaker. If the
        # provider does not expose Bet365 for this fixture, do not invent a
        # coefficient and do not silently substitute another bookmaker.
        has_requested_book = any(
            str(key).strip().lower() == wanted_bookmaker
            for key in bookmaker_odds
        )
        if not has_requested_book:
            continue

        home = str(event.get("participant1Name") or "").strip()
        away = str(event.get("participant2Name") or "").strip()
        if not home or not away:
            continue

        normalized = _oddsapi_event_to_normalized(event)
        normalized["startTime"] = start_time
        normalized["tournamentName"] = event.get("tournamentName") or "Tennis"
        normalized["_has_requested_book"] = True
        result.append(normalized)
        seen_fixtures.add(fixture_id)

    result.sort(
        key=lambda e: _parse_dt(e.get("startTime")) or datetime.max.replace(tzinfo=UTC_TZ)
    )

    _ODDSPAPI_TENNIS_CACHE[cache_key] = (time.time(), [dict(x) for x in result])
    print(
        f"[TENNIS] OddsPapi tournaments selected={len(selected)}; "
        f"successful={successful_tournaments}; events with {wanted_bookmaker} odds={len(result)}"
    )
    return result


def _tennis_name_key(name: str) -> str:
    """Normalize tennis names for matching across providers."""
    text = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode("ascii")
    text = text.lower().replace("-", " ").replace("'", " ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    tokens = [x for x in text.split() if x]
    # Ignore one-letter initials, because providers often format the same
    # player as 'Zizou Bergs', 'Bergs, Zizou', or 'Z. Bergs'.
    tokens = [x for x in tokens if len(x) > 1]
    return " ".join(tokens)


def _tennis_name_identity(name: str) -> tuple[str, str]:
    """Return given-name initials and the final family-name token."""
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    if "," in text:
        family_text, given_text = text.split(",", 1)
        family_tokens = re.findall(r"[a-z0-9]+", family_text)
        given_tokens = re.findall(r"[a-z0-9]+", given_text)
    else:
        tokens = re.findall(r"[a-z0-9]+", text)
        given_tokens = tokens[:-1]
        family_tokens = tokens[-1:]

    particles = {"da", "de", "del", "der", "di", "la", "le", "van", "von"}
    family_name = next((token for token in reversed(family_tokens) if token not in particles), "")
    given_initials = "".join(token[:1] for token in given_tokens if token not in particles)
    return (given_initials, family_name)


def _tennis_names_match(a: str, b: str) -> bool:
    ka = _tennis_name_key(a)
    kb = _tennis_name_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True

    at = ka.split()
    bt = kb.split()
    aset = set(at)
    bset = set(bt)
    if aset == bset:
        return True

    # Live score feeds commonly abbreviate players as "J. Munar" while odds
    # feeds use "Jaume Munar". Keep initials for this comparison instead of
    # discarding them during the general normalization above.
    initials_a, surname_a = _tennis_name_identity(a)
    initials_b, surname_b = _tennis_name_identity(b)
    if surname_a and surname_a == surname_b and initials_a and initials_b:
        # Feeds use both "C. H. Tseng" and "Chun-Hsin Tseng". Permit a
        # shorter initial sequence only when it is a prefix of the full name.
        if initials_a.startswith(initials_b) or initials_b.startswith(initials_a):
            return True

    # Common provider formats: "Bergs, Zizou" vs "Zizou Bergs".
    if len(at) >= 2 and len(bt) >= 2 and at[-1] == bt[-1]:
        first_a = at[0]
        first_b = bt[0]
        if first_a == first_b:
            return True

    # Initial form: "Z Bergs" vs "Zizou Bergs".
    if len(at) >= 2 and len(bt) >= 2 and at[-1] == bt[-1]:
        if at[0][0] == bt[0][0]:
            return True

    return _similar(a, b) >= 0.84


def _tennis_names_match_precise(a: str, b: str) -> bool:
    """Match the same player without the fuzzy fallback used for discovery."""
    ka = _tennis_name_key(a)
    kb = _tennis_name_key(b)
    if not ka or not kb:
        return False
    if ka == kb or set(ka.split()) == set(kb.split()):
        return True

    tokens_a = re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKD", str(a or "")).encode("ascii", "ignore").decode("ascii").lower())
    tokens_b = re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKD", str(b or "")).encode("ascii", "ignore").decode("ascii").lower())
    particles = {"da", "de", "del", "der", "di", "la", "le", "van", "von"}
    # Some feeds reverse given and family names ("Sun Xinran"), while other
    # feeds abbreviate the given name ("X. Sun"). Match on the shared family
    # token plus the given-name initial, independent of display order.
    for shared in set(tokens_a) & set(tokens_b):
        if shared in particles or len(shared) < 3:
            continue
        given_a = [token for token in tokens_a if token != shared and token not in particles]
        given_b = [token for token in tokens_b if token != shared and token not in particles]
        if not given_a or not given_b:
            continue
        if [token for token in given_a if len(token) > 1] == [token for token in given_b if len(token) > 1]:
            if "".join(token[0] for token in given_a) == "".join(token[0] for token in given_b):
                return True
        initials_a = "".join(token[0] for token in given_a)
        initials_b = "".join(token[0] for token in given_b)
        has_initial = any(len(token) == 1 for token in given_a + given_b)
        if has_initial and (initials_a.startswith(initials_b) or initials_b.startswith(initials_a)):
            return True

    initials_a, surname_a = _tennis_name_identity(a)
    initials_b, surname_b = _tennis_name_identity(b)
    return bool(
        surname_a
        and surname_a == surname_b
        and initials_a
        and initials_b
        and (initials_a.startswith(initials_b) or initials_b.startswith(initials_a))
    )


def _tennis_preferred_display_name(current: str, incoming: str) -> str:
    """Prefer the provider's full player name over a first-initial form."""
    current = " ".join(str(current or "").split()).strip()
    incoming = " ".join(str(incoming or "").split()).strip()
    current_tokens = re.findall(r"[a-z0-9]+", current.lower())
    incoming_tokens = re.findall(r"[a-z0-9]+", incoming.lower())
    current_quality = (sum(len(token) for token in current_tokens), len(current_tokens))
    incoming_quality = (sum(len(token) for token in incoming_tokens), len(incoming_tokens))
    return incoming if incoming and incoming_quality > current_quality else current


def _tennis_same_match(a: dict, b: dict, *, require_same_date: bool = True) -> tuple[bool, bool]:
    """Return whether two provider rows describe the same singles match."""
    if str(a.get("tour") or "").upper() != str(b.get("tour") or "").upper():
        return False, True
    date_a = str(a.get("date") or "").casefold()
    date_b = str(b.get("date") or "").casefold()
    if require_same_date and date_a and date_b and date_a != date_b:
        return False, True
    a_home, a_away = str(a.get("home_name") or ""), str(a.get("away_name") or "")
    b_home, b_away = str(b.get("home_name") or ""), str(b.get("away_name") or "")
    if (
        _tennis_names_match_precise(a_home, b_home)
        and _tennis_names_match_precise(a_away, b_away)
    ):
        return True, True
    if (
        _tennis_names_match_precise(a_home, b_away)
        and _tennis_names_match_precise(a_away, b_home)
    ):
        return True, False
    return False, True


def _tennis_score_for_order(score_text: str, same_order: bool) -> str:
    """Orient every set/game score to the displayed home/away player order."""
    value = str(score_text or "")
    if same_order or not value:
        return value
    return re.sub(
        r"(?<![A-Za-z0-9])(AD|\d+)\s*-\s*(AD|\d+)(?![A-Za-z0-9])",
        lambda match: f"{match.group(2)}-{match.group(1)}",
        value,
        flags=re.IGNORECASE,
    )


def _merge_tennis_fixture(existing: dict, incoming: dict, same_order: bool = True) -> None:
    """Fold duplicate provider rows into one card without losing useful data."""
    home_side, away_side = ("home", "away") if same_order else ("away", "home")
    existing["home_name"] = _tennis_preferred_display_name(
        str(existing.get("home_name") or ""), str(incoming.get(f"{home_side}_name") or "")
    )
    existing["away_name"] = _tennis_preferred_display_name(
        str(existing.get("away_name") or ""), str(incoming.get(f"{away_side}_name") or "")
    )
    for field in ("player_id", "profile_name", "country", "logo"):
        for target_side, source_side in (("home", home_side), ("away", away_side)):
            key = f"{target_side}_{field}"
            value = incoming.get(f"{source_side}_{field}")
            if not existing.get(key) and value:
                existing[key] = value

    current_odds = existing.get("odds") or {}
    incoming_odds = dict(incoming.get("odds") or {})
    if not same_order:
        incoming_odds["left"], incoming_odds["right"] = incoming_odds.get("right"), incoming_odds.get("left")
    if (
        (current_odds.get("left") is None or current_odds.get("right") is None)
        and incoming_odds.get("left") is not None
        and incoming_odds.get("right") is not None
    ):
        existing["odds"] = incoming_odds

    old_status = str(existing.get("status") or "NS").upper()
    new_status = str(incoming.get("status") or "NS").upper()
    if new_status == "FT" or (new_status == "LIVE" and old_status != "FT"):
        existing["status"] = new_status
        for key in ("live_text", "score_text", "winner"):
            if incoming.get(key):
                existing[key] = (
                    _tennis_score_for_order(incoming[key], same_order)
                    if key in {"live_text", "score_text"} else incoming[key]
                )
        winner = str(incoming.get("winner") or "")
        if winner and not same_order:
            incoming_home = str(incoming.get("home_name") or "")
            incoming_away = str(incoming.get("away_name") or "")
            if _tennis_names_match_precise(winner, incoming_home):
                existing["winner"] = str(existing.get("away_name") or "")
            elif _tennis_names_match_precise(winner, incoming_away):
                existing["winner"] = str(existing.get("home_name") or "")
    for key in ("date", "time", "match_id"):
        if not existing.get(key) and incoming.get(key):
            existing[key] = incoming[key]
    if (not existing.get("competition") or existing.get("competition") == "Tennis") and incoming.get("competition"):
        existing["competition"] = incoming["competition"]
    existing["tournament_rank"] = max(
        int(existing.get("tournament_rank") or 0), int(incoming.get("tournament_rank") or 0)
    )


def _deduplicate_tennis_matches(items: list[dict]) -> list[dict]:
    """Return one merged row per tour/date/opponent pair across all feeds."""
    unique = []
    for item in items:
        duplicate = None
        same_order = True
        for existing in unique:
            is_same, incoming_same_order = _tennis_same_match(existing, item)
            if is_same:
                duplicate = existing
                same_order = incoming_same_order
                break
        if duplicate is None:
            unique.append(item)
        else:
            _merge_tennis_fixture(duplicate, item, same_order)
    return unique


def _extract_tennis_h2h_strong(event: dict, left: str, right: str) -> dict | None:
    """Extract tennis h2h using tennis-specific name matching.

    The generic _extract_h2h() uses a strict numeric similarity score.
    Tennis providers frequently format names as `Lastname, Firstname`,
    `Firstname Lastname`, or `F. Lastname`, so we use _tennis_names_match()
    for the outcome labels as well.
    """
    bookmakers = event.get("bookmakers") or []
    if not bookmakers:
        return None

    preferred = []
    wanted_bookmaker = THE_ODDS_BOOKMAKER or API_TENNIS_BOOKMAKER
    if wanted_bookmaker:
        wanted = wanted_bookmaker.lower()
        preferred = [
            b for b in bookmakers
            if str(b.get("key") or b.get("title") or "").lower() == wanted
        ]
    ordered = preferred + [b for b in bookmakers if b not in preferred]

    for bookmaker in ordered:
        for market in bookmaker.get("markets") or []:
            if str(market.get("key") or "").lower() != "h2h":
                continue
            values = {}
            for outcome in market.get("outcomes") or []:
                name = str(outcome.get("name") or "").strip()
                price = _decimal(outcome.get("price"))
                if price is None:
                    continue

                if _tennis_names_match(name, left):
                    values["left"] = price
                elif _tennis_names_match(name, right):
                    values["right"] = price
                elif name.lower() in {"draw", "tie"}:
                    values["draw"] = price

            if "left" in values and "right" in values:
                return {
                    "left": values["left"],
                    "right": values["right"],
                    "draw": values.get("draw"),
                    "bookmaker": bookmaker.get("title") or bookmaker.get("key") or "Odds",
                }
    return None


def _api_tennis_get(params: dict | None = None, *, timeout: float | None = None):
    """Call API Tennis with its documented APIkey query parameter."""
    if not API_TENNIS_KEY:
        return {}
    query = dict(params or {})
    query["APIkey"] = API_TENNIS_KEY
    return _get_json(
        API_TENNIS_BASE_URL,
        params=query,
        timeout=min(TIMEOUT, 8) if timeout is None else timeout,
    )


def _api_tennis_singles_event_type_keys() -> dict[str, str]:
    """Resolve ATP/WTA singles keys from the provider, with documented fallbacks."""
    global _API_TENNIS_EVENT_TYPE_KEYS_CACHE
    now_ts = time.monotonic()
    cached = _API_TENNIS_EVENT_TYPE_KEYS_CACHE
    if cached and now_ts - cached[0] < 6 * 60 * 60:
        return dict(cached[1])

    resolved = dict(API_TENNIS_SINGLES_EVENT_TYPE_KEYS)
    try:
        payload = _api_tennis_get({"method": "get_events"}) or {}
        event_types = payload.get("result") if isinstance(payload, dict) else []
        if isinstance(event_types, list):
            for event_type in event_types:
                if not isinstance(event_type, dict):
                    continue
                label = re.sub(
                    r"[^a-z]+", " ",
                    str(event_type.get("event_type_type") or "").casefold(),
                ).strip()
                key = str(event_type.get("event_type_key") or "").strip()
                if key and label in {"atp singles", "wta singles"}:
                    resolved[label.split()[0]] = key
    except Exception as exc:
        print(f"[TENNIS] API Tennis event type lookup failed; using documented keys: {exc}")

    _API_TENNIS_EVENT_TYPE_KEYS_CACHE = (now_ts, resolved)
    return dict(resolved)


def _api_tennis_price(value):
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return round(price, 2) if price >= 1 else None


def _api_tennis_profile_rows(payload) -> tuple[dict, ...]:
    result = payload.get("result") if isinstance(payload, dict) else payload
    if isinstance(result, dict):
        result = list(result.values())
    if not isinstance(result, list):
        return ()
    return tuple(row for row in result if isinstance(row, dict))


@lru_cache(maxsize=128)
def _api_tennis_players_for_tournament(
    tournament_key: str, timeout: float = 3
) -> tuple[dict, ...]:
    """Fetch one tournament roster so every match can use real flags/photos."""
    key = str(tournament_key or "").strip()
    if not key or not API_TENNIS_KEY:
        return ()
    try:
        return _api_tennis_profile_rows(_api_tennis_get(
            {"method": "get_players", "tournament_key": key},
            timeout=min(TIMEOUT, timeout),
        ))
    except Exception as exc:
        print(f"[TENNIS] API Tennis player roster lookup failed for tournament {key}: {exc}")
        return ()


@lru_cache(maxsize=2048)
def _api_tennis_player_by_key(player_key: str) -> dict:
    """Fallback profile lookup for players absent from a tournament roster."""
    key = str(player_key or "").strip()
    if not key or not API_TENNIS_KEY:
        return {}
    try:
        rows = _api_tennis_profile_rows(_api_tennis_get({
            "method": "get_players", "player_key": key,
        }))
        return rows[0] if rows else {}
    except Exception:
        return {}


def _api_tennis_fixture_commence(fixture: dict) -> str:
    """Keep API Tennis fixture times in the timezone requested from that API."""
    raw_date = str(fixture.get("event_date") or "").strip()
    raw_time = str(fixture.get("event_time") or "00:00").strip()
    try:
        value = datetime.fromisoformat(f"{raw_date}T{raw_time}")
        if value.tzinfo is None:
            value = value.replace(tzinfo=YEREVAN_TZ)
        return value.isoformat()
    except ValueError:
        return f"{raw_date}T{raw_time}:00"


def _api_tennis_fixture_player_meta(fixture: dict, side: str) -> dict:
    """Read optional player country/photo fields exposed by fixture feeds."""
    prefix = "event_first_player" if side == "home" else "event_second_player"

    def first_value(keys):
        for key in keys:
            value = fixture.get(key)
            if isinstance(value, dict):
                value = (
                    value.get("code") or value.get("iocCode") or value.get("ioc_code")
                    or value.get("countryAcr") or value.get("country_acr")
                    or value.get("countryCode") or value.get("country_code")
                    or value.get("alpha3") or value.get("alpha2")
                    or value.get("iso3") or value.get("iso2") or value.get("name")
                    or value.get("url") or value.get("src")
                )
            if value:
                return str(value).strip()
        return ""

    logo = first_value((
        f"{prefix}_photo_url", f"{prefix}_photo", f"{prefix}_image_url",
        f"{prefix}_image", f"{prefix}_logo",
    ))
    if logo.startswith("/"):
        logo = f"{TENNIS_BASE_URL}{logo}"
    player_key_fields = (
        ("first_player_key", "event_first_player_key", "event_first_player_id")
        if side == "home" else
        ("second_player_key", "event_second_player_key", "event_second_player_id")
    )
    player_key = next((str(fixture.get(key) or "").strip() for key in player_key_fields if fixture.get(key)), "")
    country = first_value((
        f"{prefix}_country_code", f"{prefix}_country_acr", f"{prefix}_country_acronym",
        f"{prefix}_country_name", f"{prefix}_country", f"{prefix}_nationality",
    ))
    profile = {}
    tournament_key = str(fixture.get("tournament_key") or "").strip()
    # Tournament rosters can enrich missing photo/flag data, but do not issue
    # a profile request for every participant when the fixture already has it.
    if tournament_key and player_key and (not country or not logo):
        profile = next((
            row for row in _api_tennis_players_for_tournament(tournament_key, 3)
            if str(row.get("player_key") or row.get("playerKey") or row.get("id") or "").strip() == player_key
        ), {})

    profile_logo = ""
    for key in (
        "image", "photo", "photo_url", "photoUrl", "playerPhoto", "playerPhotoUrl",
        "profileImage", "profile_image", "picture", "avatar", "avatar_url", "avatarUrl",
        "headshot", "headshot_url", "headshotUrl", "player_logo",
    ):
        value = profile.get(key)
        if isinstance(value, dict):
            value = value.get("url") or value.get("src") or value.get("source")
        if value:
            profile_logo = str(value).strip()
            break
    if profile_logo.startswith("/"):
        profile_logo = f"{TENNIS_BASE_URL}{profile_logo}"
    elif profile_logo and not profile_logo.startswith(("https://", "http://")):
        profile_logo = ""

    country = country or _tennis_profile_country(profile)
    return {
        "country": country,
        "logo": logo or profile_logo or _tennis_profile_image(profile) or None,
        "name": str(profile.get("player_name") or profile.get("playerName") or profile.get("name") or "").strip(),
        "id": player_key or str(profile.get("player_key") or "").strip(),
    }


def _api_tennis_fixture_state(fixture: dict, home: str, away: str) -> dict:
    """Normalize API Tennis finished/live fields and actual set scores."""
    status_text = str(fixture.get("event_status") or "").strip().lower()
    live_raw = str(fixture.get("event_live") or "").strip().lower()
    final_result = str(fixture.get("event_final_result") or "").strip()
    game_result = str(fixture.get("event_game_result") or "").strip()
    winner_raw = str(fixture.get("event_winner") or "").strip().lower()
    placeholder = {"", "-", "--", "0-0", "0 - 0", "null", "none"}
    terminal_status = status_text in {"final", "ft", "result"} or bool(re.search(
        r"\b(?:finished|completed|complete|ended|retired|walkover)\b", status_text
    ))
    live = live_raw in {"1", "true", "yes"} or bool(re.search(
        r"\b(?:live|in progress|\d+(?:st|nd|rd|th) set|set|tie.?break|serving)\b",
        status_text,
    ))
    has_final_result = final_result.lower() not in placeholder and bool(
        re.search(r"\d\s*[-:]\s*\d", final_result)
    )
    finished = (
        terminal_status
        or winner_raw in {"first player", "second player"}
        or (has_final_result and not live)
    )
    if finished:
        status = "FT"
    elif live:
        status = "LIVE"
    else:
        status = "NS"

    scores = fixture.get("scores") or []
    if isinstance(scores, list):
        scores = sorted(
            (row for row in scores if isinstance(row, dict)),
            key=lambda row: int(row.get("score_set") or 0) if str(row.get("score_set") or "0").isdigit() else 0,
        )
    else:
        scores = []
    set_scores = [
        f"{row.get('score_first')}-{row.get('score_second')}"
        for row in scores
        if str(row.get("score_first") or "").strip() not in placeholder
        and str(row.get("score_second") or "").strip() not in placeholder
    ]
    score_parts = set_scores if set_scores else (
        [final_result] if final_result.lower() not in placeholder else []
    )
    if status == "LIVE" and game_result.lower() not in placeholder:
        score_parts.append(game_result)
    winner = None
    if winner_raw == "first player":
        winner = home
    elif winner_raw == "second player":
        winner = away
    elif winner_raw and winner_raw not in {"null", "none", "-"}:
        winner = str(fixture.get("event_winner") or "").strip()
    return {
        "status": status,
        "score_text": " | ".join(score_parts),
        "live_text": (game_result or str(fixture.get("event_status") or "").strip()) if status == "LIVE" else "",
        "winner": winner,
    }


def _api_tennis_odds_events(start: date, end: date, tour: str | None = None) -> list[dict]:
    """Fetch tour fixtures independently from best-effort API Tennis prices."""
    if not API_TENNIS_KEY:
        print("[TENNIS] API Tennis odds key missing: add API_TENNIS_API_KEY to .env")
        return []

    tour_key = str(tour or "").strip().lower()
    cache_key = (start.isoformat(), end.isoformat(), tour_key)
    now_ts = time.time()
    cached = _API_TENNIS_ODDS_CACHE.get(cache_key)
    if cached and now_ts - cached[0] < _API_TENNIS_ODDS_CACHE_TTL:
        return [dict(x) for x in cached[1]]

    try:
        event_type_key = _api_tennis_singles_event_type_keys().get(tour_key)
        fixture_params = {
            "method": "get_fixtures",
            "date_start": start.isoformat(),
            "date_stop": end.isoformat(),
            "timezone": TIMEZONE,
        }
        if event_type_key:
            fixture_params["event_type_key"] = event_type_key
        fixtures_payload = _api_tennis_get({
            **fixture_params,
        }) or {}
        fixtures = fixtures_payload.get("result") or [] if isinstance(fixtures_payload, dict) else []
        if not isinstance(fixtures, list):
            fixtures = []
        print(
            f"[TENNIS] API Tennis {tour_key or 'all'} fixtures source returned "
            f"{len(fixtures)} rows for {start.isoformat()}..{end.isoformat()} "
            f"(event_type_key={event_type_key or 'all'})"
        )

        fixture_map: dict[str, dict] = {}
        for fx in fixtures:
            if not isinstance(fx, dict):
                continue
            event_type = str(fx.get("event_type_type") or "").lower()
            if "doubles" in event_type or "junior" in event_type or "wheelchair" in event_type:
                continue
            if "singles" not in event_type:
                continue
            if tour_key == "atp" and "atp" not in event_type:
                continue
            if tour_key == "wta" and "wta" not in event_type:
                continue
            event_key = str(fx.get("event_key") or "").strip()
            if event_key:
                fixture_map[event_key] = fx

        if not fixture_map:
            print(f"[TENNIS] API Tennis fixtures=0 for tour={tour_key or 'both'}")
            return []

        # First try the documented date-range odds endpoint. If it returns no
        # usable prices for the fixture keys, fall back to the documented
        # per-match query (`match_key`). This is important because some plans
        # return the date-range fixture feed correctly but expose pre-match
        # odds only when a specific match is requested.
        odds_result = {}
        try:
            odds_params = {
                "method": "get_odds",
                "date_start": start.isoformat(),
                "date_stop": end.isoformat(),
            }
            if event_type_key:
                odds_params["event_type_key"] = event_type_key
            odds_payload = _api_tennis_get(odds_params) or {}
            odds_result = odds_payload.get("result") if isinstance(odds_payload, dict) else {}
            if not isinstance(odds_result, dict):
                odds_result = {}
        except Exception as exc:
            # Odds are optional enrichment; never discard a valid schedule when
            # a provider rejects a wide historical/month odds query.
            print(
                f"[TENNIS] API Tennis date-range odds failed for "
                f"{start.isoformat()}..{end.isoformat()}; keeping fixtures: {exc}"
            )

        def bookmaker_price(prices: dict, bookmaker: str):
            wanted = str(bookmaker or "").lower().strip()
            for k, value in prices.items():
                if str(k).lower().strip() == wanted:
                    return _api_tennis_price(value)
            return None

        def extract_prices(raw: dict, bookmaker: str):
            """Extract Home/Away winner prices and the bookmaker actually used.

            When API_TENNIS_BOOKMAKER=auto, choose any bookmaker that has both
            Home and Away prices. If a specific bookmaker is configured, prefer
            that bookmaker and only fall back to any complete bookmaker when the
            configured one is unavailable. This keeps the match visible with real
            odds even when bet365 is not covered for a particular tennis event.
            """
            if not isinstance(raw, dict):
                return None, None, None
            market = raw.get("Home/Away") or raw.get("Home / Away")
            if not isinstance(market, dict):
                market = next(
                    (v for k, v in raw.items()
                     if str(k).strip().lower() in {"home/away", "home / away"}),
                    None,
                )
            if not isinstance(market, dict):
                return None, None, None

            home_prices = market.get("Home") or market.get("home") or {}
            away_prices = market.get("Away") or market.get("away") or {}
            if not isinstance(home_prices, dict) or not isinstance(away_prices, dict):
                return None, None, None

            preferred = str(bookmaker or "auto").lower().strip()
            if preferred and preferred != "auto":
                left = bookmaker_price(home_prices, preferred)
                right = bookmaker_price(away_prices, preferred)
                if left is not None and right is not None:
                    return left, right, preferred

            # Automatic mode, or fallback from a preferred bookmaker: find the
            # first bookmaker that exists with a valid price on BOTH sides.
            home_map = {str(k).lower().strip(): v for k, v in home_prices.items()}
            away_map = {str(k).lower().strip(): v for k, v in away_prices.items()}
            candidates = []
            for key in home_map:
                if key not in away_map:
                    continue
                left = _api_tennis_price(home_map[key])
                right = _api_tennis_price(away_map[key])
                if left is not None and right is not None:
                    candidates.append((key, left, right))

            if not candidates:
                return None, None, None

            # Stable provider preference without requiring it: bet365 first,
            # then common mainstream books, then the first available one.
            preference_order = [
                "bet365", "bwin", "betsson", "william hill", "unibet",
                "1xbet", "888sport", "sportingbet", "betcris", "pinnacle",
            ]
            # extract_prices() returns (P1 price, P2 price, bookmaker). Keep
            # this order consistent with the preferred-bookmaker branch above.
            by_name = {name: (left, right, name) for name, left, right in candidates}
            for name in preference_order:
                if name in by_name:
                    return by_name[name]
            name, left, right = candidates[0]
            return left, right, name

        events: list[dict] = []
        missing_fixture_odds: list[tuple[str, dict]] = []
        for event_key, fx in fixture_map.items():
            raw = odds_result.get(event_key)
            # Be tolerant if the provider serializes numeric keys differently.
            if raw is None:
                raw = odds_result.get(str(event_key))
            left_price, right_price, used_bookmaker = extract_prices(raw, API_TENNIS_BOOKMAKER)
            home = str(fx.get("event_first_player") or "").strip()
            away = str(fx.get("event_second_player") or "").strip()
            home_meta = _api_tennis_fixture_player_meta(fx, "home")
            away_meta = _api_tennis_fixture_player_meta(fx, "away")
            if not home or not away:
                continue
            fixture_state = _api_tennis_fixture_state(fx, home, away)
            if left_price is None or right_price is None:
                missing_fixture_odds.append((event_key, fx))
                # Keep the fixture even if this provider has not published
                # prices yet; it still improves the ATP/WTA match count.
                events.append({
                    "id": event_key,
                    "home_team": home,
                    "away_team": away,
                    "commence_time": _api_tennis_fixture_commence(fx),
                    "_api_prices": None,
                    "_home_country": home_meta["country"],
                    "_away_country": away_meta["country"],
                    "_home_logo": home_meta["logo"],
                    "_away_logo": away_meta["logo"],
                    "_home_player_id": home_meta["id"],
                    "_away_player_id": away_meta["id"],
                    "_home_profile_name": home_meta["name"],
                    "_away_profile_name": away_meta["name"],
                    "_status": fixture_state["status"],
                    "_score_text": fixture_state["score_text"],
                    "_live_text": fixture_state["live_text"],
                    "_winner": fixture_state["winner"],
                    "bookmakers": [],
                    "_api_tennis": True,
                    "_fixture_only": True,
                    "_tournament_title": str(fx.get("tournament_name") or "Tennis").strip(),
                    "_event_type": str(fx.get("event_type_type") or ""),
                })
                continue

            events.append({
                "id": event_key,
                "home_team": home,
                "away_team": away,
                "commence_time": _api_tennis_fixture_commence(fx),
                "_api_prices": {
                    "left": left_price,
                    "right": right_price,
                    "bookmaker": used_bookmaker or API_TENNIS_BOOKMAKER,
                },
                "_home_country": home_meta["country"],
                "_away_country": away_meta["country"],
                "_home_logo": home_meta["logo"],
                "_away_logo": away_meta["logo"],
                "_home_player_id": home_meta["id"],
                "_away_player_id": away_meta["id"],
                "_home_profile_name": home_meta["name"],
                "_away_profile_name": away_meta["name"],
                "_status": fixture_state["status"],
                "_score_text": fixture_state["score_text"],
                "_live_text": fixture_state["live_text"],
                "_winner": fixture_state["winner"],
                "bookmakers": [{
                    "key": used_bookmaker or API_TENNIS_BOOKMAKER,
                    "title": used_bookmaker or API_TENNIS_BOOKMAKER,
                    "markets": [{
                        "key": "h2h",
                        "outcomes": [
                            {"name": home, "price": left_price},
                            {"name": away, "price": right_price},
                        ],
                    }],
                }],
                "_api_tennis": True,
                "_tournament_title": str(fx.get("tournament_name") or "Tennis").strip(),
                "_event_type": str(fx.get("event_type_type") or ""),
                "_odds_bookmaker": used_bookmaker,
            })

        # If the date-range response did not contain usable prices, query the
        # specific matches one-by-one. The official API explicitly documents
        # `match_key` for get_odds, and this is much more reliable for the small
        # number of matches that the bot displays.
        per_match_checked = 0
        if missing_fixture_odds:
            today = datetime.now(YEREVAN_TZ).date()
            upcoming_missing_odds = []
            for event_key, fx in missing_fixture_odds:
                try:
                    fixture_day = date.fromisoformat(str(fx.get("event_date") or "")[:10])
                except ValueError:
                    fixture_day = today
                if fixture_day >= today:
                    upcoming_missing_odds.append((event_key, fx))
            print(
                f"[TENNIS] API Tennis match_key odds: "
                f"{len(upcoming_missing_odds)} upcoming fixtures to check; "
                f"skipped {len(missing_fixture_odds) - len(upcoming_missing_odds)} finished fixtures"
            )
            if API_TENNIS_BOOKMAKER == "auto":
                print(f"[TENNIS] API Tennis: no complete Home/Away prices found in date-range odds; trying match_key with ANY bookmaker")
            else:
                print(f"[TENNIS] API Tennis: no complete {API_TENNIS_BOOKMAKER} Home/Away prices found; match_key fallback will also allow any bookmaker")
            for event_key, fx in upcoming_missing_odds[:12]:
                try:
                    per_match_checked += 1
                    one_payload = _api_tennis_get({
                        "method": "get_odds",
                        "match_key": event_key,
                    }) or {}
                    one_result = one_payload.get("result") if isinstance(one_payload, dict) else {}
                    if not isinstance(one_result, dict):
                        continue

                    raw = one_result.get(event_key) or one_result.get(str(event_key))
                    if raw is None and len(one_result) == 1:
                        raw = next(iter(one_result.values()))
                    left_price, right_price, used_bookmaker = extract_prices(raw, API_TENNIS_BOOKMAKER)
                    if left_price is None or right_price is None:
                        continue

                    home = str(fx.get("event_first_player") or "").strip()
                    away = str(fx.get("event_second_player") or "").strip()
                    home_meta = _api_tennis_fixture_player_meta(fx, "home")
                    away_meta = _api_tennis_fixture_player_meta(fx, "away")
                    if not home or not away:
                        continue
                    fixture_state = _api_tennis_fixture_state(fx, home, away)
                    events.append({
                        "id": event_key,
                        "home_team": home,
                        "away_team": away,
                        "commence_time": _api_tennis_fixture_commence(fx),
                        "_api_prices": {
                            "left": left_price,
                            "right": right_price,
                            "bookmaker": used_bookmaker or API_TENNIS_BOOKMAKER,
                        },
                        "_home_country": home_meta["country"],
                        "_away_country": away_meta["country"],
                        "_home_logo": home_meta["logo"],
                        "_away_logo": away_meta["logo"],
                        "_home_player_id": home_meta["id"],
                        "_away_player_id": away_meta["id"],
                        "_home_profile_name": home_meta["name"],
                        "_away_profile_name": away_meta["name"],
                        "_status": fixture_state["status"],
                        "_score_text": fixture_state["score_text"],
                        "_live_text": fixture_state["live_text"],
                        "_winner": fixture_state["winner"],
                        "bookmakers": [{
                            "key": used_bookmaker or API_TENNIS_BOOKMAKER,
                            "title": used_bookmaker or API_TENNIS_BOOKMAKER,
                            "markets": [{
                                "key": "h2h",
                                "outcomes": [
                                    {"name": home, "price": left_price},
                                    {"name": away, "price": right_price},
                                ],
                            }],
                        }],
                        "_api_tennis": True,
                        "_tournament_title": str(fx.get("tournament_name") or "Tennis").strip(),
                        "_event_type": str(fx.get("event_type_type") or ""),
                        "_odds_bookmaker": used_bookmaker,
                    })
                    print(
                        f"[TENNIS ODDS] API Tennis match_key={event_key}: "
                        f"{home} vs {away} -> {used_bookmaker} {left_price}/{right_price}"
                    )
                except Exception as exc:
                    print(f"[TENNIS] API Tennis match_key={event_key} odds failed: {exc}")

        # De-duplicate by match id because a price can come from either the
        # date-range response or the per-match fallback.
        unique_events = {}
        for event in events:
            unique_events[str(event.get("id") or "")] = event
        events = list(unique_events.values())
        events.sort(key=lambda e: str(e.get("commence_time") or ""))
        _API_TENNIS_ODDS_CACHE[cache_key] = (now_ts, events)
        priced_count = sum(1 for event in events if event.get("bookmakers"))
        print(
            f"[TENNIS] API Tennis fixtures={len(fixture_map)}; "
            f"events={len(events)}; priced={priced_count}; "
            f"without odds={len(events) - priced_count}; "
            f"preferred bookmaker={API_TENNIS_BOOKMAKER}; "
            f"per-match checked={per_match_checked}"
        )
        return [dict(x) for x in events]
    except Exception as exc:
        print(f"[TENNIS] API Tennis odds failed ({type(exc).__name__}): {exc!r}")
        return []


def _tennis_odds_for_names(events: list[dict], left: str, right: str):
    """Match a fixture to an odds event and return winner prices."""
    for event in events:
        home = str(event.get("home_team") or "")
        away = str(event.get("away_team") or "")
        if not (
            (_tennis_names_match(home, left) and _tennis_names_match(away, right))
            or (_tennis_names_match(home, right) and _tennis_names_match(away, left))
        ):
            continue

        if event.get("_api_tennis"):
            odds = _extract_tennis_h2h_strong(event, left, right)
            if odds:
                print(
                    f"[TENNIS ODDS] MATCHED API Tennis: {left} vs {right} "
                    f"-> {odds.get('bookmaker')} {odds.get('left')}/{odds.get('right')}"
                )
                return odds
        else:
            odds = _extract_tennis_h2h_strong(event, left, right)
            if odds:
                print(
                    f"[TENNIS ODDS] MATCHED The Odds API: {left} vs {right} "
                    f"-> {odds.get('bookmaker')} {odds.get('left')}/{odds.get('right')}"
                )
                return odds
    return None


def _live_tennis_key() -> str:
    return (os.getenv("LIVE_TENNIS_API_KEY") or "").strip()


_LIVE_TENNIS_RATE_LIMIT_UNTIL = 0.0
_LIVE_TENNIS_RATE_LIMIT_LOCK = threading.Lock()


def _live_tennis_rate_limited() -> bool:
    with _LIVE_TENNIS_RATE_LIMIT_LOCK:
        return time.monotonic() < _LIVE_TENNIS_RATE_LIMIT_UNTIL


def _live_tennis_get(
    path: str, params: dict | None = None, *, timeout: float | None = None
):
    global _LIVE_TENNIS_RATE_LIMIT_UNTIL
    key = _live_tennis_key()
    if not key:
        raise RuntimeError("LIVE_TENNIS_API_KEY is missing from .env")
    with _LIVE_TENNIS_RATE_LIMIT_LOCK:
        remaining = _LIVE_TENNIS_RATE_LIMIT_UNTIL - time.monotonic()
    if remaining > 0:
        raise RuntimeError(
            f"Live Tennis API paused after HTTP 429; retry in {int(remaining) + 1}s"
        )
    # Live Tennis API documents Bearer authentication. X-API-Key is also
    # accepted by the public API, so send both for compatibility with keys
    # issued in either form.
    response = requests.get(
        f"{LIVE_TENNIS_BASE_URL}{path}",
        headers={
            "Authorization": f"Bearer {key}",
            "X-API-Key": key,
            "Accept": "application/json",
        },
        params=params,
        timeout=min(TIMEOUT, 5) if timeout is None else timeout,
    )
    if response.status_code == 429:
        try:
            retry_after = max(30, int(response.headers.get("Retry-After") or 60))
        except (TypeError, ValueError):
            retry_after = 60
        with _LIVE_TENNIS_RATE_LIMIT_LOCK:
            _LIVE_TENNIS_RATE_LIMIT_UNTIL = max(
                _LIVE_TENNIS_RATE_LIMIT_UNTIL, time.monotonic() + retry_after
            )
        raise RuntimeError(
            f"Live Tennis API HTTP 429; pausing requests for {retry_after}s"
        )
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict) and (payload.get("error") or payload.get("err")):
        raise RuntimeError(str(payload.get("error") or payload.get("err")))
    return payload


def _live_tennis_rank(tournament: str) -> int:
    """Assign a display priority to main-tour tournament levels."""
    text = str(tournament or "").lower()
    if any(x in text for x in ("grand slam", "australian open", "roland garros", "french open", "wimbledon", "us open")):
        return 100
    if "masters 1000" in text or "wta 1000" in text or "1000" in text or "masters" in text:
        return 90
    if "atp 500" in text or "wta 500" in text or " 500" in text:
        return 80
    if "atp 250" in text or "wta 250" in text or " 250" in text:
        return 70
    # Main-tour matches whose catalogue name does not expose the tier.
    return 60


def _live_tennis_player(player: dict | None) -> dict:
    player = player if isinstance(player, dict) else {}
    image = next((
        player.get(key) for key in (
            "image", "photo", "photo_url", "photoUrl", "avatar", "avatar_url",
            "avatarUrl", "headshot", "headshot_url", "headshotUrl", "playerPhoto",
            "playerPhotoUrl", "image_url", "imageUrl",
        ) if player.get(key)
    ), None)
    if isinstance(image, dict):
        image = image.get("url") or image.get("src") or image.get("source")
    image = _tennis_profile_image(player) or (str(image).strip() if image else None)
    if image and image.startswith("/"):
        image = f"{LIVE_TENNIS_BASE_URL}{image}"
    return {
        "name": str(player.get("name") or player.get("fullName") or "Player").strip(),
        "id": player.get("id") or player.get("playerId") or player.get("player_id"),
        "country": _tennis_profile_country(player),
        "image": str(image).strip() if image else None,
    }


def _tennis_profile_country(player: dict) -> str:
    for key in (
        "country", "countryCode", "country_code", "countryAcr", "countryAcronym",
        "country_acr", "country_acronym",
        "countryName", "country_name", "nation", "nationality", "nationalityName",
        "nationality_name", "nationalityCode", "nationality_code", "countryIso",
        "countryISO", "countryIso2", "countryIso3", "country_iso", "country_iso2",
        "country_iso3", "countryAbbreviation", "country_abbreviation", "iocCode",
        "ioc_code", "alpha2", "alpha3", "isoCode", "iso_code", "player_country",
    ):
        value = player.get(key)
        if isinstance(value, dict):
            value = (
                value.get("ioc") or value.get("iocCode") or value.get("ioc_code") or value.get("code")
                or value.get("alpha3") or value.get("alpha2") or value.get("iso3")
                or value.get("iso2") or value.get("abbreviation") or value.get("name")
                or value.get("countryAcr") or value.get("country_acr")
                or value.get("countryCode") or value.get("country_code")
                or value.get("countryName") or value.get("country_name")
            )
        if value:
            return str(value).strip()
    return ""


def _tennis_country_from_profile_html(source: str) -> str:
    """Read nationality from official ATP/WTA profile markup."""
    if not source:
        return ""
    source = html.unescape(str(source))
    structured = re.search(
        r"[\"'](?:countryName|country|nationalityName|nationality|countryCode|countryAcr)[\"']\s*:\s*[\"']([^\"']+)[\"']",
        source,
        re.IGNORECASE,
    )
    if structured:
        return structured.group(1).strip()

    visible = re.sub(
        r"<(script|style)\b[^>]*>.*?</\1>", " ", source,
        flags=re.IGNORECASE | re.DOTALL,
    )
    visible = " ".join(re.sub(r"<[^>]+>", " ", visible).split())
    country_match = re.search(
        r"\bCountry\s+([A-Za-z][A-Za-z .'-]*?)(?=\s+(?:Birthplace|Plays|Coach|Age|Weight|Height|Turned pro)\b)",
        visible,
        re.IGNORECASE,
    )
    return country_match.group(1).strip(" .") if country_match else ""


def _tennis_profile_image(player: dict) -> str:
    for key in (
        "image", "photo", "photo_url", "photoUrl", "playerPhoto", "playerPhotoUrl",
        "profileImage", "profile_image", "picture", "avatar", "avatar_url", "avatarUrl",
        "headshot", "headshot_url", "headshotUrl", "player_logo",
    ):
        value = player.get(key)
        if isinstance(value, dict):
            value = value.get("url") or value.get("src") or value.get("source")
        if value:
            value = str(value).strip()
            if value.startswith("https://") or value.startswith("http://"):
                return value
            if value.startswith("/"):
                return f"{LIVE_TENNIS_BASE_URL}{value}"
    return ""


@lru_cache(maxsize=512)
def _live_tennis_player_details(
    player_name: str,
    player_id: str | int | None = None,
    timeout: float | None = None,
) -> dict:
    """Resolve a canonical player name, nationality, and portrait once per player."""
    name = " ".join(str(player_name or "").split()).strip()
    if not name or not _live_tennis_key():
        return {}

    def as_profiles(payload):
        if not isinstance(payload, dict):
            return payload if isinstance(payload, list) else []
        value = payload.get("data") or payload
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for key in ("players", "results", "items", "content"):
                children = value.get(key)
                if isinstance(children, (list, dict)):
                    return as_profiles(children)
            for key in ("player", "profile", "result", "item"):
                child = value.get(key)
                if isinstance(child, (list, dict)):
                    return as_profiles(child)
            return [value]
        return []

    profiles = []
    id_only_profile = None
    best_profile = {}
    deadline = time.monotonic() + timeout if timeout is not None else None

    def fetch_profile(path: str, params: dict | None = None):
        request_timeout = None
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            request_timeout = max(0.25, remaining)
        return _live_tennis_get(path, params, timeout=request_timeout)

    if player_id is not None:
        try:
            payload = fetch_profile(f"/players/{quote(str(player_id))}") or {}
            profiles.extend(as_profiles(payload))
        except Exception:
            pass
    search_terms = [name]
    initials, surname = _tennis_name_identity(name)
    if surname and initials and re.match(r"^(?:[A-Z]\.?(?:\s|$))+", name, re.IGNORECASE):
        # The roster search often indexes full names only; searching the
        # surname lets us resolve score/odds feeds such as "A. Galarneau".
        search_terms.append(surname)
    for search_term in dict.fromkeys(search_terms):
        try:
            payload = fetch_profile(
                "/players", {"search": search_term, "limit": 20}
            ) or {}
            profiles.extend(as_profiles(payload))
        except Exception:
            pass
        if deadline is not None and time.monotonic() >= deadline:
            break

    for player in profiles:
        if not isinstance(player, dict):
            continue
        candidate = str(
            player.get("name") or player.get("fullName") or player.get("playerName") or ""
        ).strip()
        if not candidate:
            candidate = " ".join(
                str(player.get(key) or "").strip() for key in ("firstName", "lastName")
            ).strip()
        profile_id = (
            player.get("id") or player.get("playerId") or player.get("player_id")
            or player.get("idPlayer") or player_id
        )
        # An id-specific response is authoritative even if it omits the name.
        if candidate and not _tennis_names_match(name, candidate):
            continue
        if not candidate and player_id is None:
            continue
        country = _tennis_profile_country(player)
        image = _tennis_profile_image(player)
        resolved = {
            "name": candidate or name,
            "id": profile_id,
            "country": country,
            "image": image,
        }
        if country or image or candidate:
            best_profile = {
                "name": candidate or best_profile.get("name") or name,
                "id": profile_id or best_profile.get("id"),
                "country": country or best_profile.get("country") or "",
                "image": image or best_profile.get("image") or "",
            }
            if best_profile.get("country") and best_profile.get("image"):
                return best_profile
        if profile_id and id_only_profile is None:
            id_only_profile = resolved
    return best_profile or id_only_profile or {}


@lru_cache(maxsize=512)
def _live_tennis_player_country(
    player_name: str,
    tour: str | None = None,
    timeout: float | None = None,
) -> str:
    """Resolve a missing nationality for odds-only fixtures, when available."""
    normalized_name = " ".join(
        re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKD", str(player_name or "")).encode("ascii", "ignore").decode("ascii").lower())
    )
    # These two players are in the current sample cards; this exact-name map
    # keeps their flags visible even if a provider omits nationality fields.
    known_countries = {
        "c gauff": "USA", "coco gauff": "USA",
        "x sun": "CHN", "sun xinran": "CHN",
        "c wong": "HKG", "coleman wong": "HKG",
        "c hewitt": "AUS", "cruz hewitt": "AUS",
    }
    if normalized_name in known_countries:
        return known_countries[normalized_name]

    country = str(
        _live_tennis_player_details(player_name, timeout=timeout).get("country") or ""
    ).strip()
    if country:
        return country
    tour_key = str(tour or "").strip().lower()
    if tour_key == "atp":
        return _atp_tour_player_country(player_name, timeout)
    if tour_key == "wta":
        return _wta_tour_player_country(player_name, timeout)
    return (
        _atp_tour_player_country(player_name, timeout)
        or _wta_tour_player_country(player_name, timeout)
    )


def _live_tennis_score_text(match: dict) -> tuple[str, str]:
    score = match.get("score") or {}
    if not isinstance(score, dict):
        return "", ""
    sets = score.get("sets") or []
    games = score.get("games") or []
    points = score.get("points") or []
    if isinstance(sets, list) and len(sets) >= 2:
        try:
            set_text = f"Sets {sets[0]}-{sets[1]}"
        except Exception:
            set_text = ""
    else:
        set_text = ""
    game_text = ""
    if isinstance(games, list) and len(games) >= 2 and all(isinstance(x, list) for x in games[:2]):
        current = []
        for a, b in zip(games[0], games[1]):
            current.append(f"{a}-{b}")
        game_text = " ".join(current[-2:]) if current else ""
    point_text = ""
    if isinstance(points, list) and len(points) >= 2 and points[0] is not None and points[1] is not None:
        point_text = f"{points[0]}-{points[1]}"
    parts = [x for x in (set_text, game_text, point_text) if x]
    return " | ".join(parts), point_text


def _live_tennis_normalize(row: dict, tour: str) -> dict:
    p1 = _live_tennis_player((row.get("players") or {}).get("p1"))
    p2 = _live_tennis_player((row.get("players") or {}).get("p2"))
    status_raw = str(row.get("status") or "upcoming").lower()
    status = "LIVE" if status_raw == "live" else "FT" if status_raw == "completed" else "NS"
    score_text, live_text = _live_tennis_score_text(row)
    winner_id = row.get("winner")
    winner = None
    if winner_id is not None:
        try:
            winner = p1["name"] if int(winner_id) == int(p1.get("id")) else p2["name"] if int(winner_id) == int(p2.get("id")) else None
        except (TypeError, ValueError):
            winner = None
    tournament = str(row.get("tournament") or "Tennis").strip()
    scheduled = (row.get("live_at") or row.get("scheduled_time")) if status == "LIVE" else (row.get("scheduled_time") or row.get("live_at"))
    date_text, time_text = _date_time(scheduled)
    rank = _live_tennis_rank(tournament)
    return {
        "sport": "tennis",
        "type": "player_match",
        "sport_icon": "🎾",
        "competition": tournament,
        "tour": str(row.get("tour") or tour).upper(),
        "home_name": p1["name"],
        "away_name": p2["name"],
        # Preserve a portrait when the schedule provider included one. Other
        # sources are resolved lazily when the card is rendered.
        "home_logo": p1.get("image"),
        "away_logo": p2.get("image"),
        "home_player_id": p1.get("id"),
        "away_player_id": p2.get("id"),
        "home_country": p1["country"],
        "away_country": p2["country"],
        "status": status,
        "live_text": live_text if status == "LIVE" else "",
        "score_text": score_text,
        "round": row.get("round") or "",
        "winner": winner,
        "odds": {"left": None, "right": None, "bookmaker": ""},
        "date": date_text,
        "time": time_text,
        "match_id": str(row.get("id") or ""),
        "tournament_rank": rank,
    }


def _live_tennis_rows_for_tour(tour: str, start: date, end: date) -> list[dict]:
    """Fetch scheduled, upcoming and live singles matches for one ATP/WTA tour."""
    if not _live_tennis_key() or _live_tennis_rate_limited():
        return []

    rows = []

    today = datetime.now(YEREVAN_TZ).date()

    def append_in_window(payload, *, include_live_today: bool = False):
        data = payload.get("data") if isinstance(payload, dict) else []
        for row in data or []:
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or "").lower()
            if include_live_today and status == "live" and start <= today <= end:
                rows.append(row)
                continue
            scheduled = row.get("live_at") if status == "live" else row.get("scheduled_time")
            scheduled = scheduled or row.get("scheduled_time") or row.get("live_at")
            local_dt = _parse_dt(scheduled)
            if local_dt and start <= local_dt.date() <= end:
                rows.append(row)

    # /fixtures is the provider's dedicated scheduled-fixture feed. Unlike
    # /matches?status=upcoming, it explicitly contains upcoming draws and can
    # be paged. Omit is_qualifying so qualifying and main-draw matches are both
    # included; this matters for thin WTA slates and lower-ranked entrants.
    page_size = 200
    offset = 0
    max_pages = 8
    for _ in range(max_pages):
        try:
            fixture_payload = _live_tennis_get(
                "/fixtures",
                {
                    "tour": tour,
                    "draw": "singles",
                    "limit": page_size,
                    "offset": offset,
                },
            ) or {}
        except Exception as exc:
            print(f"[TENNIS] Live Tennis API fixtures query failed ({tour}): {exc}")
            break
        if _live_tennis_rate_limited():
            return []
        page = list(fixture_payload.get("data") or []) if isinstance(fixture_payload, dict) else []
        if not page:
            break
        append_in_window({"data": page})
        dated = [_parse_dt(row.get("scheduled_time")) for row in page if isinstance(row, dict)]
        dated = [dt for dt in dated if dt]
        if dated and min(dt.date() for dt in dated) > end:
            break
        offset += len(page)
        if len(page) < page_size:
            break

    # Keep the score endpoint as a second source: it can contain recent
    # schedule corrections that have not propagated to /fixtures yet.
    try:
        upcoming_payload = _live_tennis_get(
            "/matches",
            {
                "status": "upcoming",
                "tour": tour,
                "draw": "singles",
                "from": start.isoformat(),
                "to": end.isoformat(),
                "limit": 200,
                "offset": 0,
            },
        ) or {}
        append_in_window(upcoming_payload)
    except Exception as exc:
        print(f"[TENNIS] Live Tennis API upcoming query failed ({tour}): {exc}")
    if _live_tennis_rate_limited():
        return []

    # Also include currently live matches when the selected window contains today.
    if start <= today <= end:
        try:
            live_payload = _live_tennis_get(
                "/matches",
                {
                    "status": "live",
                    "tour": tour,
                    "draw": "singles",
                    "limit": 200,
                    "offset": 0,
                },
            ) or {}
            append_in_window(live_payload, include_live_today=True)
        except Exception as exc:
            print(f"[TENNIS] Live Tennis API live query failed ({tour}): {exc}")
    # De-duplicate by provider match id.
    unique = {}
    for row in rows:
        if isinstance(row, dict) and row.get("id") is not None:
            unique[str(row.get("id"))] = row
    return list(unique.values())


def _sofascore_tennis_tour(event: dict) -> str | None:
    """Best-effort ATP/WTA classification for SofaScore tennis events."""
    blobs = []
    tournament = event.get("tournament") or {}
    unique = tournament.get("uniqueTournament") or {}
    season = event.get("season") or {}
    for obj in (tournament, unique, season, event.get("homeTeam") or {}, event.get("awayTeam") or {}):
        if isinstance(obj, dict):
            for key in ("name", "slug", "gender", "genderType", "category"):
                value = obj.get(key)
                if isinstance(value, dict):
                    blobs.extend(str(v) for v in value.values())
                elif value is not None:
                    blobs.append(str(value))
    text = " ".join(blobs).lower()
    if any(token in text for token in ("itf", "challenger", "futures", "junior")):
        return None
    if any(token in text for token in ("wta", "women", "woman", "female")):
        return "wta"
    if any(token in text for token in ("atp", "men", "man", "male")):
        return "atp"

    # Common current tour-level tournament names where the provider metadata
    # may omit an explicit gender label.
    name = str(unique.get("name") or tournament.get("name") or "").lower()
    wta_names = {
        "china open", "wuhan open", "singapore open", "guadalajara open",
        "monterrey open", "jingshan tennis open", "seoul open",
        "korea open", "japan open",
    }
    atp_names = {
        "chengdu open", "hangzhou open", "japan open tennis championships",
        "kinoshita group japan open tennis championships", "laver cup",
        "china open", "tokyo open",
    }
    # If the event exposes player gender, use it before ambiguous tournament names.
    for team_key in ("homeTeam", "awayTeam"):
        team = event.get(team_key) or {}
        gender = str(team.get("gender") or team.get("genderType") or "").lower()
        if gender in {"f", "female", "women", "wta"}:
            return "wta"
        if gender in {"m", "male", "men", "atp"}:
            return "atp"

    if name in wta_names and name not in {"china open", "japan open"}:
        return "wta"
    if name in atp_names and name not in {"china open", "japan open"}:
        return "atp"
    return None


_SOFASCORE_TENNIS_DAY_CACHE: dict[str, tuple[float, dict]] = {}
_SOFASCORE_TENNIS_CACHE_LOCK = threading.Lock()
_SOFASCORE_TENNIS_PROVIDER_BLOCKED_UNTIL = 0.0


def _sofascore_tennis_day(date_text: str) -> dict:
    """Fetch one SofaScore schedule day with a short, refreshable cache."""
    global _SOFASCORE_TENNIS_PROVIDER_BLOCKED_UNTIL
    today_text = datetime.now(YEREVAN_TZ).date().isoformat()
    ttl = 60 if date_text == today_text else 900
    now = time.monotonic()
    with _SOFASCORE_TENNIS_CACHE_LOCK:
        if now < _SOFASCORE_TENNIS_PROVIDER_BLOCKED_UNTIL:
            return {}
        cached = _SOFASCORE_TENNIS_DAY_CACHE.get(date_text)
        if cached and now - cached[0] < ttl:
            return cached[1]

    try:
        payload = _get_json(
            f"https://api.sofascore.com/api/v1/sport/tennis/scheduled-events/{date_text}",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 Chrome/142.0 Safari/537.36",
                "Accept": "application/json,text/plain,*/*",
                "Referer": "https://www.sofascore.com/",
            },
            timeout=min(TIMEOUT, 4),
        ) or {}
    except Exception as exc:
        print(f"[TENNIS] SofaScore fallback failed for {date_text}: {type(exc).__name__}")
        payload = {}
        # A blocked provider should not receive 31 more month-range requests
        # (or be queried again immediately for the second tour).
        cooldown = 3600 if "403" in str(exc) or "forbidden" in str(exc).lower() else 60
        with _SOFASCORE_TENNIS_CACHE_LOCK:
            _SOFASCORE_TENNIS_PROVIDER_BLOCKED_UNTIL = max(
                _SOFASCORE_TENNIS_PROVIDER_BLOCKED_UNTIL,
                time.monotonic() + cooldown,
            )

    if not isinstance(payload, dict):
        payload = {}
    with _SOFASCORE_TENNIS_CACHE_LOCK:
        _SOFASCORE_TENNIS_DAY_CACHE[date_text] = (time.monotonic(), payload)
    return payload


def _sofascore_tennis_country(team: dict) -> str:
    for key in ("countryCode", "countryAcr", "nationalityCode", "nationality"):
        value = team.get(key)
        if value:
            return str(value).strip()
    country = team.get("country")
    if isinstance(country, dict):
        for key in ("alpha3", "alpha2", "code", "countryCode", "name"):
            if country.get(key):
                return str(country[key]).strip()
    elif country:
        return str(country).strip()
    return ""


def _sofascore_tennis_rows(start: date, end: date, tour: str) -> list[dict]:
    """Fallback tennis fixtures source when RapidAPI Tennis API is unavailable.

    SofaScore exposes daily tennis schedules publicly. We deliberately use it
    only for fixture discovery; bookmaker odds still come from The Odds API.
    """
    rows = []
    days = []
    current = start
    while current <= end:
        days.append(current.isoformat())
        current += timedelta(days=1)

    # A month can span 31 daily schedule calls. Fetch a small batch in
    # parallel so a slow day does not keep the whole WTA/ATP list waiting.
    with ThreadPoolExecutor(max_workers=min(2, max(1, len(days)))) as executor:
        daily_payloads = list(executor.map(_sofascore_tennis_day, days))

    for date_text, payload in zip(days, daily_payloads):
        for event in (payload or {}).get("events", []) or []:
            if not isinstance(event, dict):
                continue
            if _sofascore_tennis_tour(event) != tour:
                continue

            tournament = event.get("tournament") or {}
            unique = tournament.get("uniqueTournament") or {}
            event_title = " ".join(str(value or "") for value in (
                tournament.get("name"), unique.get("name"), event.get("slug")
            )).lower()
            if "double" in event_title:
                continue

            home = event.get("homeTeam") or {}
            away = event.get("awayTeam") or {}
            home_id = home.get("id")
            away_id = away.get("id")
            start_ts = event.get("startTimestamp")
            try:
                dt = datetime.fromtimestamp(float(start_ts), tz=UTC_TZ).astimezone(YEREVAN_TZ)
            except (TypeError, ValueError, OSError):
                dt = None
            if dt and not (start <= dt.date() <= end):
                continue

            status_obj = event.get("status") or {}
            status_type = str(status_obj.get("type") or "").lower()
            status = "LIVE" if status_type in {"inprogress", "inprogress"} else "NS"
            if status_type in {"finished", "afterpenalties", "ended"}:
                status = "FT"

            home_score = event.get("homeScore") or {}
            away_score = event.get("awayScore") or {}
            score_parts = []
            for key in ("period1", "period2", "period3", "period4", "period5"):
                if key in home_score and key in away_score:
                    score_parts.append(f"{home_score[key]}-{away_score[key]}")

            tournament_name = str(unique.get("name") or tournament.get("name") or "Tennis")
            rank = 0
            level_text = str(tournament.get("name") or unique.get("name") or "").lower()
            if "grand slam" in level_text:
                rank = 100
            elif "1000" in level_text or "masters" in level_text:
                rank = 90
            elif "500" in level_text:
                rank = 80
            elif "250" in level_text:
                rank = 70
            else:
                rank = 50

            try:
                winner_code = int(event.get("winnerCode") or 0)
            except (TypeError, ValueError):
                winner_code = 0
            rows.append({
                "player1": {
                    "name": str(home.get("name") or "Player 1"),
                    "id": home_id,
                    "image": (
                        f"https://api.sofascore.com/api/v1/player/{home_id}/image"
                        if home_id is not None else None
                    ),
                    "countryAcr": _sofascore_tennis_country(home),
                },
                "player2": {
                    "name": str(away.get("name") or "Player 2"),
                    "id": away_id,
                    "image": (
                        f"https://api.sofascore.com/api/v1/player/{away_id}/image"
                        if away_id is not None else None
                    ),
                    "countryAcr": _sofascore_tennis_country(away),
                },
                "tournament": {"name": tournament_name},
                "round": {"name": str((event.get("roundInfo") or {}).get("name") or "")},
                "date": dt.isoformat() if dt else date_text,
                "id": event.get("id"),
                "_status": status,
                "_winner": (
                    str(home.get("name") or "Player 1")
                    if winner_code == 1 else
                    str(away.get("name") or "Player 2")
                    if winner_code == 2 else None
                ),
                "result": " ".join(score_parts) if status == "FT" else "",
                "live": "LIVE" if status == "LIVE" else "",
                "_sofascore_rank": rank,
            })
    return rows

def _tennis_normalize(row: dict, tour: str) -> dict:
    p1 = row.get("player1") or {}
    p2 = row.get("player2") or {}
    p1_name = str(p1.get("name") or row.get("player1Name") or "Player 1").strip()
    p2_name = str(p2.get("name") or row.get("player2Name") or "Player 2").strip()
    result = str(row.get("result") or "").strip()
    live = str(row.get("live") or "").strip()

    source_status = str(row.get("_status") or "").strip().upper()
    if source_status in {"FT", "LIVE", "NS"}:
        status = source_status
    elif result:
        status = "FT"
    elif live:
        status = "LIVE"
    else:
        status = "NS"
    winner = row.get("_winner") or row.get("winner")

    odd1 = row.get("odd1")
    odd2 = row.get("odd2")
    odds = row.get("odds")
    if isinstance(odds, dict):
        odd1 = odds.get("odd1") if odd1 is None else odd1
        odd2 = odds.get("odd2") if odd2 is None else odd2
    p1_odd = p1.get("odd")
    p2_odd = p2.get("odd")
    if odd1 is None:
        odd1 = p1_odd
    if odd2 is None:
        odd2 = p2_odd

    round_info = row.get("round") or {}
    round_name = round_info.get("name") if isinstance(round_info, dict) else None
    date_text, time_text = _date_time(row.get("date"))

    return {
        "sport": "tennis",
        "type": "player_match",
        "sport_icon": "🎾",
        "competition": _tennis_tournament_name(row),
        "tour": tour.upper(),
        "home_name": p1_name,
        "away_name": p2_name,
        "home_logo": _tennis_image(p1, tour),
        "away_logo": _tennis_image(p2, tour),
        "home_country": str(p1.get("countryAcr") or "").strip(),
        "away_country": str(p2.get("countryAcr") or "").strip(),
        "status": status,
        "live_text": live,
        "score_text": result,
        "round": round_name,
        "winner": winner,
        "odds": {"left": _decimal(odd1), "right": _decimal(odd2), "bookmaker": "Tennis API"},
        "date": date_text,
        "time": time_text,
        "match_id": str(row.get("id") or row.get("matchId") or ""),
        "tournament_rank": max(
            _tennis_rank(row), int(row.get("_sofascore_rank") or 0)
        ),
    }


def get_tennis_messages(
    date_from: str | None = None,
    date_to: str | None = None,
    tour: str | None = None,
):
    start = date.fromisoformat(date_from) if date_from else datetime.now(YEREVAN_TZ).date()
    end = date.fromisoformat(date_to) if date_to else start

    selected_tour = str(tour or "").strip().lower()
    tours = (selected_tour,) if selected_tour in {"atp", "wta"} else ("atp", "wta")

    rows = []
    rapidapi_failed = False
    for tour in tours:
        try:
            for row in _tennis_rows_for_tour(tour, start, end):
                rows.append(_tennis_normalize(row, tour))
        except requests.exceptions.HTTPError as exc:
            rapidapi_failed = True
            status = getattr(exc.response, "status_code", None)
            if status == 403:
                print(
                    "[TENNIS] RapidAPI returned 403. Check that the key is subscribed "
                    "to Tennis API - ATP WTA ITF and uses the correct host."
                )
            else:
                print(f"[TENNIS] request failed: {exc}")
        except Exception as exc:
            rapidapi_failed = True
            print(f"[TENNIS] request failed: {exc}")

    # Always supplement RapidAPI with the dedicated fixture feed when the
    # selected window includes today or a future date. RapidAPI can return a
    # partial draw (especially WTA qualifying); using it only when RapidAPI is
    # completely empty hid the rest of that tour's schedule.
    today = datetime.now(YEREVAN_TZ).date()
    if _live_tennis_key() and end >= today:
        fallback_tours = (selected_tour,) if selected_tour in {"atp", "wta"} else ("atp", "wta")
        for fallback_tour in fallback_tours:
            try:
                fallback_rows = _live_tennis_rows_for_tour(fallback_tour, start, end)
                added = 0
                for raw in fallback_rows:
                    incoming = _live_tennis_normalize(raw, fallback_tour)
                    duplicate = None
                    for existing in rows:
                        if str(existing.get("tour") or "").upper() != fallback_tour.upper():
                            continue
                        if existing.get("date") and incoming.get("date") and existing["date"] != incoming["date"]:
                            continue
                        existing_home = str(existing.get("home_name") or "")
                        existing_away = str(existing.get("away_name") or "")
                        incoming_home = str(incoming.get("home_name") or "")
                        incoming_away = str(incoming.get("away_name") or "")
                        if (
                            (_tennis_names_match(existing_home, incoming_home)
                             and _tennis_names_match(existing_away, incoming_away))
                            or (_tennis_names_match(existing_home, incoming_away)
                                and _tennis_names_match(existing_away, incoming_home))
                        ):
                            duplicate = existing
                            break

                    if duplicate is None:
                        rows.append(incoming)
                        added += 1
                        continue

                    # Keep the richer provider row while filling fields the
                    # primary source omitted from its schedule response.
                    for key in ("home_player_id", "away_player_id", "home_country", "away_country", "home_logo", "away_logo"):
                        if not duplicate.get(key) and incoming.get(key):
                            duplicate[key] = incoming[key]
                    incoming_status = str(incoming.get("status") or "NS").upper()
                    existing_status = str(duplicate.get("status") or "NS").upper()
                    if (
                        incoming_status == "FT" and existing_status != "FT"
                    ) or (
                        incoming_status == "LIVE" and existing_status not in {"LIVE", "FT"}
                    ):
                        duplicate["status"] = incoming_status
                        for key in ("status", "live_text", "score_text", "winner"):
                            if incoming.get(key):
                                duplicate[key] = incoming[key]
                print(
                    f"[TENNIS] Live Tennis API {fallback_tour.upper()} "
                    f"fixtures={len(fallback_rows)}; added={added}"
                )
            except requests.exceptions.HTTPError as exc:
                status = getattr(exc.response, "status_code", None)
                print(f"[TENNIS] Live Tennis API {fallback_tour.upper()} HTTP {status}: {exc}")
            except Exception as exc:
                print(f"[TENNIS] Live Tennis API {fallback_tour.upper()} failed: {exc}")
    elif not rows:
        print("[TENNIS] RapidAPI unavailable and LIVE_TENNIS_API_KEY is missing")

    # SofaScore is a schedule fallback for when the paid tennis feeds are
    # unavailable or only return part of the draw. Its daily payloads are
    # cached briefly and fetched concurrently, which keeps month views useful
    # without making every tap wait for 30 serial HTTP requests.
    if end >= today:
        for fallback_tour in tours:
            try:
                fallback_rows = _sofascore_tennis_rows(start, end, fallback_tour)
                added = 0
                for raw in fallback_rows:
                    incoming = _tennis_normalize(raw, fallback_tour)
                    duplicate = None
                    same_order = True
                    for existing in rows:
                        is_same, incoming_same_order = _tennis_same_match(
                            existing, incoming
                        )
                        if is_same:
                            duplicate = existing
                            same_order = incoming_same_order
                            break
                    if duplicate is None:
                        rows.append(incoming)
                        added += 1
                    else:
                        _merge_tennis_fixture(duplicate, incoming, same_order)
                print(
                    f"[TENNIS] SofaScore {fallback_tour.upper()} "
                    f"fixtures={len(fallback_rows)}; added={added}"
                )
            except Exception as exc:
                print(
                    f"[TENNIS] SofaScore {fallback_tour.upper()} "
                    f"fallback failed: {type(exc).__name__}"
                )

    # Dedicated tennis odds provider: API Tennis.
    # The Odds API stays as a secondary fallback, but no OddsPapi calls are made.
    api_tennis_events = _api_tennis_odds_events(start, end, selected_tour) if API_TENNIS_KEY else []
    the_odds_events = (
        _odds_api_tennis_events(start, end, selected_tour)
        if THE_ODDS_API_KEY else []
    )
    odds_events = the_odds_events
    print(
        f"[TENNIS] API rows={len(rows)}; Odds sources: API Tennis={len(api_tennis_events)}; "
        f"TheOdds={len(the_odds_events)}; bookmaker={API_TENNIS_BOOKMAKER}; "
        f"API Tennis key loaded={bool(API_TENNIS_KEY)}; "
        f"RapidAPI key loaded={bool(_tennis_key())}; Live Tennis key loaded={bool(_live_tennis_key())}"
    )

    if rows and (api_tennis_events or odds_events):
        for event in (api_tennis_events + odds_events)[:20]:
            h = str(event.get('home_team') or '')
            a = str(event.get('away_team') or '')
            source = 'API Tennis' if event.get('_api_tennis') else 'The Odds API'
            print(f"[TENNIS ODDS DEBUG] {source}: {h} vs {a}")

    matched_count = 0
    base_row_count = len(rows)

    # Merge API Tennis fixtures directly. Keeping this provider out of the
    # generic Odds API matcher prevents its tour/date metadata from dropping
    # otherwise valid fixtures and preserves prices in the provider's player
    # order. API Tennis already received the exact requested date range.
    api_rows_added = 0
    api_prices_attached = 0
    api_priced_rows_added = 0
    for event in api_tennis_events:
        home = str(event.get("home_team") or "").strip()
        away = str(event.get("away_team") or "").strip()
        if not home or not away:
            continue

        event_type = " ".join(str(event.get(key) or "") for key in (
            "_event_type", "_tournament_title"
        )).lower()
        if selected_tour in {"atp", "wta"}:
            event_tour = selected_tour.upper()
        elif re.search(r"(?<![a-z])wta(?![a-z])", event_type):
            event_tour = "WTA"
        elif re.search(r"(?<![a-z])atp(?![a-z])|challenger", event_type):
            event_tour = "ATP"
        else:
            continue

        commence = str(event.get("commence_time") or "")
        date_text, time_text = _date_time(commence)
        event_day = _parse_dt(commence)
        if not date_text:
            date_text = event_day.strftime("%d %b") if event_day else start.strftime("%d %b")
        competition = str(event.get("_tournament_title") or "Tennis").strip()
        price_data = event.get("_api_prices")
        price_data = price_data if isinstance(price_data, dict) else {}
        left_price = _decimal(
            price_data.get("left") or price_data.get("home")
            or price_data.get("p1") or price_data.get("first")
        )
        right_price = _decimal(
            price_data.get("right") or price_data.get("away")
            or price_data.get("p2") or price_data.get("second")
        )
        bookmaker = str(price_data.get("bookmaker") or "").strip()
        # Older cached/provider event shapes may have the prices only in the
        # normalized h2h market. Fall back whenever either side is incomplete,
        # rather than treating an empty _api_prices object as authoritative.
        if left_price is None or right_price is None:
            extracted_prices = _extract_tennis_h2h_strong(event, home, away) or {}
            extracted_left = _decimal(extracted_prices.get("left"))
            extracted_right = _decimal(extracted_prices.get("right"))
            if extracted_left is not None and extracted_right is not None:
                left_price, right_price = extracted_left, extracted_right
                bookmaker = str(extracted_prices.get("bookmaker") or bookmaker).strip()
        has_prices = left_price is not None and right_price is not None

        duplicate = None
        same_order = True
        incoming_identity = {
            "tour": event_tour,
            "date": date_text,
            "home_name": home,
            "away_name": away,
        }
        for item in rows:
            is_same, incoming_same_order = _tennis_same_match(item, incoming_identity)
            if is_same:
                duplicate = item
                same_order = incoming_same_order
                break

        if duplicate is not None:
            home_country = event.get("_home_country") if same_order else event.get("_away_country")
            away_country = event.get("_away_country") if same_order else event.get("_home_country")
            home_logo = event.get("_home_logo") if same_order else event.get("_away_logo")
            away_logo = event.get("_away_logo") if same_order else event.get("_home_logo")
            home_player_id = event.get("_home_player_id") if same_order else event.get("_away_player_id")
            away_player_id = event.get("_away_player_id") if same_order else event.get("_home_player_id")
            home_profile_name = event.get("_home_profile_name") if same_order else event.get("_away_profile_name")
            away_profile_name = event.get("_away_profile_name") if same_order else event.get("_home_profile_name")
            duplicate["home_name"] = _tennis_preferred_display_name(
                str(duplicate.get("home_name") or ""), home if same_order else away
            )
            duplicate["away_name"] = _tennis_preferred_display_name(
                str(duplicate.get("away_name") or ""), away if same_order else home
            )
            if not duplicate.get("home_country") and home_country:
                duplicate["home_country"] = home_country
            if not duplicate.get("away_country") and away_country:
                duplicate["away_country"] = away_country
            if not duplicate.get("home_logo") and home_logo:
                duplicate["home_logo"] = home_logo
            if not duplicate.get("away_logo") and away_logo:
                duplicate["away_logo"] = away_logo
            if not duplicate.get("home_player_id") and home_player_id:
                duplicate["home_player_id"] = home_player_id
            if not duplicate.get("away_player_id") and away_player_id:
                duplicate["away_player_id"] = away_player_id
            if not duplicate.get("home_profile_name") and home_profile_name:
                duplicate["home_profile_name"] = home_profile_name
            if not duplicate.get("away_profile_name") and away_profile_name:
                duplicate["away_profile_name"] = away_profile_name
            incoming_status = str(event.get("_status") or "NS").upper()
            existing_status = str(duplicate.get("status") or "NS").upper()
            if incoming_status == "FT" or (incoming_status == "LIVE" and existing_status != "FT"):
                duplicate["status"] = incoming_status
                for key, source_key in (
                    ("score_text", "_score_text"),
                    ("live_text", "_live_text"),
                    ("winner", "_winner"),
                ):
                    if event.get(source_key):
                        duplicate[key] = (
                            _tennis_score_for_order(event[source_key], same_order)
                            if key in {"score_text", "live_text"} else event[source_key]
                        )
                if not same_order and event.get("_winner"):
                    event_winner = str(event.get("_winner") or "")
                    if _tennis_names_match_precise(event_winner, home):
                        duplicate["winner"] = away
                    elif _tennis_names_match_precise(event_winner, away):
                        duplicate["winner"] = home
            if has_prices:
                if not same_order:
                    left_price, right_price = right_price, left_price
                duplicate["odds"] = {
                    "left": left_price,
                    "right": right_price,
                    "bookmaker": bookmaker or "API Tennis",
                }
                api_prices_attached += 1
            if not duplicate.get("competition") or duplicate.get("competition") == "Tennis":
                duplicate["competition"] = competition
            if not duplicate.get("time") and time_text:
                duplicate["time"] = time_text
            continue

        rows.append({
            "sport": "tennis",
            "type": "player_match",
            "sport_icon": "🎾",
            "competition": competition,
            "tour": event_tour,
            "home_name": home,
            "away_name": away,
            "home_logo": event.get("_home_logo") or None,
            "away_logo": event.get("_away_logo") or None,
            "home_player_id": event.get("_home_player_id"),
            "away_player_id": event.get("_away_player_id"),
            "home_profile_name": event.get("_home_profile_name") or None,
            "away_profile_name": event.get("_away_profile_name") or None,
            "home_country": str(event.get("_home_country") or "").strip(),
            "away_country": str(event.get("_away_country") or "").strip(),
            "status": str(event.get("_status") or "NS").upper(),
            "live_text": str(event.get("_live_text") or ""),
            "score_text": str(event.get("_score_text") or ""),
            "round": "",
            "winner": event.get("_winner"),
            "odds": {
                "left": left_price,
                "right": right_price,
                "bookmaker": bookmaker or ("API Tennis" if has_prices else ""),
            },
            "date": date_text,
            "time": time_text,
            "match_id": str(event.get("id") or ""),
            "tournament_rank": int(event.get("_tournament_rank") or _live_tennis_rank(competition)),
        })
        api_rows_added += 1
        if has_prices:
            api_priced_rows_added += 1

    print(
        f"[TENNIS MERGE DIRECT] tour={selected_tour or 'both'} "
        f"API Tennis fixtures={len(api_tennis_events)} added={api_rows_added} "
        f"odds_attached={api_prices_attached} priced_added={api_priced_rows_added} "
        f"total_rows={len(rows)}"
    )

    # Odds feeds are also a useful fixture source. Previously they were only
    # consulted when the schedule feed was entirely empty, so odds-backed ATP
    # matches disappeared whenever the live feed returned even a partial list.
    odds_rows_added = 0
    priced_rows_added = 0
    for event in odds_events:
        home = str(event.get("home_team") or "").strip()
        away = str(event.get("away_team") or "").strip()
        if not home or not away:
            continue

        event_type = " ".join(str(event.get(key) or "") for key in (
            "_event_type", "sport_key", "sport_title", "_tournament_title"
        )).lower()
        if event.get("_api_tennis") and selected_tour in {"atp", "wta"}:
            # The fixtures request was already filtered to this tour.
            event_tour = selected_tour.upper()
        elif re.search(r"(?<![a-z])wta(?![a-z])", event_type):
            event_tour = "WTA"
        elif re.search(r"(?<![a-z])atp(?![a-z])|challenger", event_type):
            event_tour = "ATP"
        else:
            # Do not guess the tour for an odds-only match.
            continue
        if selected_tour and event_tour.lower() != selected_tour:
            continue

        commence = str(event.get("commence_time") or "")
        if event.get("_api_tennis"):
            event_dt = _parse_dt(commence)
            try:
                event_day = event_dt.date() if event_dt else date.fromisoformat(commence[:10])
            except ValueError:
                event_day = start
        else:
            event_dt = _parse_dt(commence)
            event_day = event_dt.date() if event_dt else None
        event_date_text, event_time_text = _date_time(commence)
        # API Tennis fixtures were requested for this exact date range in the
        # configured timezone. Re-filtering their serialized date here dropped
        # matches at UTC/local-day boundaries, so only The Odds events need a
        # second local-window check.
        if event_day is None or (not event.get("_api_tennis") and not (start <= event_day <= end)):
            continue

        duplicate = None
        same_order = True
        incoming_identity = {
            "tour": event_tour,
            "date": event_date_text,
            "home_name": home,
            "away_name": away,
        }
        for item in rows:
            is_same, incoming_same_order = _tennis_same_match(item, incoming_identity)
            if is_same:
                duplicate = item
                same_order = incoming_same_order
                break

        if duplicate is not None:
            duplicate["home_name"] = _tennis_preferred_display_name(
                str(duplicate.get("home_name") or ""), home if same_order else away
            )
            duplicate["away_name"] = _tennis_preferred_display_name(
                str(duplicate.get("away_name") or ""), away if same_order else home
            )
            current_odds = duplicate.get("odds") or {}
            if current_odds.get("left") is None or current_odds.get("right") is None:
                duplicate_odds = _extract_tennis_h2h_strong(event, home, away) or {}
                if duplicate_odds.get("left") is not None and duplicate_odds.get("right") is not None:
                    left_price, right_price = duplicate_odds["left"], duplicate_odds["right"]
                    if not same_order:
                        left_price, right_price = right_price, left_price
                    duplicate["odds"] = {
                        "left": left_price,
                        "right": right_price,
                        "draw": duplicate_odds.get("draw"),
                        "bookmaker": duplicate_odds.get("bookmaker") or "",
                    }
                    matched_count += 1
            continue

        odds = _extract_tennis_h2h_strong(event, home, away) or {}
        has_prices = odds.get("left") is not None and odds.get("right") is not None
        if not has_prices and not event.get("_fixture_only"):
            continue
        event_dt = _parse_dt(commence)
        date_text, time_text = event_date_text, event_time_text
        competition = str(
            event.get("_tournament_title") or event.get("sport_title") or "Tennis"
        ).strip()
        rows.append({
            "sport": "tennis",
            "type": "player_match",
            "sport_icon": "🎾",
            "competition": competition,
            "tour": event_tour,
            "home_name": home,
            "away_name": away,
            "home_logo": None,
            "away_logo": None,
            "home_player_id": None,
            "away_player_id": None,
            "home_country": "",
            "away_country": "",
            "status": "NS",
            "live_text": "",
            "score_text": "",
            "round": "",
            "winner": None,
            "odds": {
                "left": odds.get("left"),
                "right": odds.get("right"),
                "bookmaker": odds.get("bookmaker") or "",
            },
            "date": date_text or (event_day.strftime("%d %b") if event_day else ""),
            "time": time_text,
            "match_id": str(event.get("id") or ""),
            "tournament_rank": int(event.get("_tournament_rank") or _live_tennis_rank(competition)),
        })
        odds_rows_added += 1
        if has_prices:
            priced_rows_added += 1

    print(
        f"[TENNIS ODDS] Matched odds for {matched_count}/{base_row_count} schedule matches; "
        f"added {odds_rows_added} matches missing from the schedule feed "
        f"({priced_rows_added} with odds)"
    )

    now = datetime.now(YEREVAN_TZ)
    fresh_rows = []
    stale_unstarted = 0
    for item in rows:
        if str(item.get("status") or "NS").upper() != "NS":
            fresh_rows.append(item)
            continue
        try:
            scheduled = datetime.strptime(
                f"{item.get('date', '')} {item.get('time', '')} {now.year}",
                "%d %b %H:%M %Y",
            ).replace(tzinfo=YEREVAN_TZ)
            if scheduled.date() - now.date() > timedelta(days=180):
                scheduled = scheduled.replace(year=scheduled.year - 1)
            elif now.date() - scheduled.date() > timedelta(days=180):
                scheduled = scheduled.replace(year=scheduled.year + 1)
        except (TypeError, ValueError):
            scheduled = None
        # A fixture-only odds source leaves status as NS forever. Don't show
        # yesterday's (or long-past) rows as "NOT STARTED" when the score feed
        # did not publish a live/final update for them.
        if scheduled and scheduled < now - timedelta(hours=2):
            stale_unstarted += 1
            continue
        fresh_rows.append(item)
    rows = fresh_rows
    if stale_unstarted:
        print(f"[TENNIS] Hidden stale NOT STARTED fixtures: {stale_unstarted}")

    # Keep ATP and WTA completely separate in the UI. When a tour is selected,
    # only that tour is considered here. Tournament rank is the primary
    # priority, so Grand Slams / Masters / WTA 1000 events naturally rise first.
    grouped = {}
    for item in rows:
        if selected_tour and str(item.get("tour", "")).lower() != selected_tour:
            continue
        key = (item.get("tour"), item.get("competition"))
        bucket = grouped.setdefault(
            key, {
                "name": item.get("competition"),
                "rank": int(item.get("tournament_rank") or 0),
                "items": [],
            },
        )
        bucket["items"].append(item)
        bucket["rank"] = max(bucket["rank"], int(item.get("tournament_rank") or 0))

    tournaments = sorted(
        grouped.values(),
        key=lambda x: (x["rank"], len(x["items"])),
        reverse=True,
    )

    # Provider ids and tournament labels differ between feeds, so collapse
    # equivalent pairings after sorting the full selected-tour fixture list.
    ordered_items = [
        item
        for tournament in tournaments
        for item in sorted(
            tournament["items"],
            key=lambda row: (row.get("date", ""), row.get("time", "")),
        )
    ]
    return [
        ("Tennis event", item)
        for item in _deduplicate_tennis_matches(ordered_items)
    ]


# ============================================================
# Formula 1
# ============================================================


def _f1_get_json(path: str):
    return _get_json(f"{F1_BASE_URL}{path}")


def _f1_standings() -> list[dict]:
    payload = _f1_get_json("/current/driverStandings.json")
    lists = payload.get("MRData", {}).get("StandingsTable", {}).get("StandingsLists", [])
    if not lists:
        return []
    result = []
    for row in (lists[0].get("DriverStandings") or [])[:10]:
        driver = row.get("Driver") or {}
        name = " ".join(
            x for x in (driver.get("givenName"), driver.get("familyName")) if x
        ).strip()
        result.append({
            "position": int(row.get("position") or 0),
            "name": name or str(driver.get("driverId") or "Driver"),
            "points": row.get("points"),
            "driver_id": driver.get("driverId"),
            "wikipedia_url": driver.get("url"),
        })
    return result


def _f1_races() -> list[dict]:
    payload = _f1_get_json("/current.json")
    return payload.get("MRData", {}).get("RaceTable", {}).get("Races", []) or []


def _f1_sessions(race: dict) -> list[dict]:
    sessions = []
    for key, label in (
        ("FirstPractice", "Practice 1"),
        ("SecondPractice", "Practice 2"),
        ("ThirdPractice", "Practice 3"),
        ("Sprint", "Sprint"),
        ("Qualifying", "Qualifying"),
    ):
        data = race.get(key)
        if not isinstance(data, dict) or not data.get("date"):
            continue
        raw = f"{data['date']}T{data.get('time', '00:00:00')}"
        dt = _parse_dt(raw)
        if dt:
            sessions.append({"label": label, "datetime": dt, "raw": raw})
    race_raw = f"{race.get('date')}T{race.get('time', '00:00:00')}"
    race_dt = _parse_dt(race_raw)
    if race_dt:
        sessions.append({"label": "Grand Prix", "datetime": race_dt, "raw": race_raw})
    sessions.sort(key=lambda x: x["datetime"])
    return sessions


def _f1_next_session_info(race: dict, now: datetime | None = None) -> dict:
    """Return the next session to be run, with its exact Yerevan date/time.

    The UI uses this as the single source for the prominent "NEXT UP" line.
    This means the bot says exactly which stage is next: Practice 1, Practice 2,
    Practice 3, Sprint, Qualifying, or Grand Prix.
    """
    now = now or datetime.now(YEREVAN_TZ)
    sessions = _f1_sessions(race)
    if not sessions:
        return {"label": "Schedule unavailable", "date": "", "time": "", "status": "unavailable"}

    upcoming = [session for session in sessions if session["datetime"] > now]
    if upcoming:
        nxt = upcoming[0]
        return {
            "label": nxt["label"],
            "date": nxt["datetime"].strftime("%d %b"),
            "time": nxt["datetime"].strftime("%H:%M"),
            "status": "next",
        }

    return {"label": "Weekend completed", "date": "", "time": "", "status": "completed"}


def _f1_current_stage_info(race: dict, now: datetime | None = None) -> dict:
    """Return the live weekend stage/status using the race schedule.

    The stage is intentionally derived from session times rather than guessed
    from standings/results. This lets the card say Practice/Qualifying/Pre-Race
    or Grand Prix In Progress/Completed as the clock moves through the weekend.
    """
    now = now or datetime.now(YEREVAN_TZ)
    sessions = _f1_sessions(race)
    if not sessions:
        return {
            "label": "Schedule unavailable",
            "status": "unavailable",
            "detail": "No session schedule available",
        }

    # If the race result already exists, the weekend is completed.
    try:
        round_no = str(race.get("round") or "").strip()
        if round_no:
            result_payload = _f1_get_json(f"/current/{round_no}/results/limit/1.json")
            races = result_payload.get("MRData", {}).get("RaceTable", {}).get("Races", []) or []
            results = (races[0].get("Results") or []) if races else []
            if results:
                return {
                    "label": "Grand Prix",
                    "status": "completed",
                    "detail": "Race completed",
                }
    except Exception:
        pass

    # Treat a session as live only for a plausible duration. Schedules provide
    # start times, not end times; without this bound a missing race result made
    # the Grand Prix appear to be in progress forever.
    for idx, session in enumerate(sessions):
        start = session["datetime"]
        next_start = sessions[idx + 1]["datetime"] if idx + 1 < len(sessions) else None
        max_duration = timedelta(hours=4 if session["label"] == "Grand Prix" else 2)
        if (
            start <= now < start + max_duration
            and (next_start is None or now < next_start)
        ):
            label = session["label"]
            if label == "Grand Prix":
                return {
                    "label": "Grand Prix",
                    "status": "in_progress",
                    "detail": "Race in progress",
                }
            return {
                "label": label,
                "status": "in_progress",
                "detail": f"{label} in progress",
            }

    race_session = next((x for x in sessions if x["label"] == "Grand Prix"), None)
    if race_session and now < race_session["datetime"]:
        completed_before = [x for x in sessions if x["datetime"] <= now]
        last = completed_before[-1]["label"] if completed_before else "Weekend"
        if last == "Qualifying":
            return {
                "label": "Pre-Race / Starting Grid",
                "status": "pre_race",
                "detail": "Qualifying complete • waiting for Grand Prix",
            }

    upcoming = [session for session in sessions if session["datetime"] > now]
    if now < sessions[0]["datetime"]:
        return {
            "label": "Weekend not started",
            "status": "upcoming",
            "detail": f"Next: {sessions[0]['label']}",
        }

    if race_session and now >= race_session["datetime"] + timedelta(hours=4):
        return {
            "label": "Race result pending",
            "status": "result_pending",
            "detail": "Scheduled race time passed • official result unavailable",
        }

    if upcoming:
        nxt = upcoming[0]
        return {
            "label": "Between sessions",
            "status": "between_sessions",
            "detail": f"Next: {nxt['label']} • {nxt['datetime'].strftime('%d %b %H:%M')}",
        }

    return {"label": "Weekend completed", "status": "completed", "detail": "Weekend completed"}


def _f1_current_stage(race: dict, now: datetime | None = None) -> str:
    return str(_f1_current_stage_info(race, now).get("label") or "Schedule unavailable")


def _f1_pit_stop_summary(race: dict) -> dict:
    """Fetch pit-stop data for the current race and return a compact summary.

    Jolpica provides pit stops by season/round. Before or during a race the
    endpoint may legitimately return no stops; in that case we report the
    actual data state instead of inventing a summary.
    """
    round_no = str(race.get("round") or "").strip()
    if not round_no:
        return {"status": "unavailable", "total_stops": 0, "drivers": 0, "fastest": None}

    try:
        payload = _f1_get_json(f"/current/{round_no}/pitstops.json")
        races = payload.get("MRData", {}).get("RaceTable", {}).get("Races", []) or []
        pitstops = (races[0].get("Pitstops") or []) if races else []
    except Exception:
        pitstops = []

    if not pitstops:
        return {
            "status": "no_data",
            "total_stops": 0,
            "drivers": 0,
            "fastest": None,
        }

    def duration_seconds(value):
        text = str(value or "").strip()
        if not text:
            return None
        try:
            if ":" in text:
                parts = text.split(":")
                if len(parts) == 2:
                    return float(parts[0]) * 60 + float(parts[1])
            return float(text)
        except Exception:
            return None

    enriched = []
    for stop in pitstops:
        sec = duration_seconds(stop.get("duration"))
        if sec is not None:
            enriched.append((sec, stop))

    fastest = None
    if enriched:
        sec, stop = min(enriched, key=lambda x: x[0])
        fastest = {
            "driver_id": str(stop.get("driverId") or ""),
            "lap": str(stop.get("lap") or ""),
            "duration": str(stop.get("duration") or ""),
            "seconds": round(sec, 3),
        }

    return {
        "status": "available",
        "total_stops": len(pitstops),
        "drivers": len({str(x.get("driverId") or "") for x in pitstops if x.get("driverId")}),
        "fastest": fastest,
    }


def _krok_get(path: str, params: dict | None = None):
    if not KROK_API_KEY:
        return None
    return _get_json(
        f"{KROK_BASE_URL}{path}",
        headers={"X-API-Key": KROK_API_KEY, "Accept": "application/json"},
        params=params or {},
    )


def _krok_f1_sport_keys() -> list[str]:
    """Resolve Formula 1 sport slugs from KrokOdds, with safe aliases as fallback."""
    keys = []
    override = (os.getenv("KROK_F1_SPORT_KEY") or "").strip()
    if override:
        keys.append(override)
    if KROK_API_KEY:
        try:
            payload = _krok_get("/sports") or {}
            rows = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    key = str(row.get("sport") or row.get("key") or row.get("sport_key") or "").strip()
                    label = str(row.get("label") or row.get("title") or row.get("name") or "").strip().lower()
                    category = str(row.get("category") or row.get("group") or "").strip().lower()
                    blob = f"{key} {label} {category}".lower()
                    if any(token in blob for token in ("formula 1", "formula1", "formula_1", "motorsport")) and key:
                        keys.append(key)
        except Exception:
            pass
    keys.extend(["f1", "formula1", "formula_1", "motorsport_formula1", "motorsport_f1"])
    return list(dict.fromkeys(k for k in keys if k))


def _krok_f1_sport_key() -> str | None:
    keys = _krok_f1_sport_keys()
    return keys[0] if keys else None


def _krok_f1_headshots(driver_names: list[str], sport_key: str | None = None) -> dict[str, str]:
    if not KROK_API_KEY or not driver_names:
        return {}
    keys = ([sport_key] if sport_key else []) + [k for k in _krok_f1_sport_keys() if k != sport_key]
    for key in keys:
        if not key:
            continue
        try:
            payload = _krok_get("/reference/headshots", {"sport_key": key, "limit": 200}) or {}
            rows = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                continue
            indexed = [(str(r.get("name") or "").strip(), str(r.get("photo_url") or "").strip())
                       for r in rows if isinstance(r, dict) and r.get("name") and r.get("photo_url")]
            if not indexed:
                continue
            result = {}
            for driver in driver_names:
                best_photo, best_score = None, 0.0
                for name, photo in indexed:
                    score = _similar(driver, name)
                    if score > best_score:
                        best_score, best_photo = score, photo
                if best_photo and best_score >= 0.80:
                    result[driver] = best_photo
            if result:
                return result
        except Exception:
            continue
    return {}


def _wikipedia_driver_photo(driver: dict) -> str | None:
    """Resolve a driver image through Wikimedia Commons search."""
    name = str(driver.get("name") or "").strip()
    if not name:
        return None
    try:
        response = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query", "generator": "search", "gsrsearch": f"{name} Formula 1",
                "gsrnamespace": 6, "gsrlimit": 5, "prop": "imageinfo", "iiprop": "url",
                "iiurlwidth": 300, "format": "json", "formatversion": 2,
            },
            headers={"User-Agent": "MySportInfoBot/1.0"}, timeout=10,
        )
        response.raise_for_status()
        for page in response.json().get("query", {}).get("pages", []):
            info = (page.get("imageinfo") or [{}])[0]
            if info.get("thumburl") or info.get("url"):
                return info.get("thumburl") or info.get("url")
    except Exception:
        pass
    return None


def _f1_driver_name_key(value: str | None) -> str:
    value = _strip_accents(str(value or "")).lower()
    value = re.sub(r"\b(to win|race winner|winner|outright)\b", " ", value)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value).split())


def _f1_odds_event_matches_race(event: dict, race: dict) -> bool:
    """Reject odds from a different GP or season-wide championship market."""
    event_text = " ".join(
        str(event.get(key) or "")
        for key in ("event", "name", "title", "competition", "league", "description")
    ).lower()
    event_key = _f1_driver_name_key(event_text)
    circuit = race.get("Circuit") or {}
    location = circuit.get("Location") or {}
    race_names = [
        race.get("raceName"), circuit.get("circuitName"),
        location.get("locality"), location.get("country"),
    ]
    for candidate in race_names:
        key = _f1_driver_name_key(candidate)
        if key and key in event_key:
            return True

    race_dt = _parse_dt(f"{race.get('date')}T{race.get('time', '00:00:00')}")
    event_dt = _parse_dt(
        event.get("start_time") or event.get("startTime")
        or event.get("commence_time") or event.get("kickoff_utc")
    )
    if race_dt and event_dt and abs((race_dt - event_dt).total_seconds()) <= 48 * 3600:
        return any(token in event_text for token in ("f1", "formula 1", "grand prix", "race"))
    return False


def _krok_f1_driver_odds(
    driver_names: list[str],
    sport_key: str | None = None,
    race: dict | None = None,
    now: datetime | None = None,
) -> dict[str, dict]:
    """Fetch race-winner prices for this GP and match driver aliases safely."""
    if not KROK_API_KEY or not driver_names or not race:
        return {}

    now = now or datetime.now(YEREVAN_TZ)
    race_dt = _parse_dt(f"{race.get('date')}T{race.get('time', '00:00:00')}")
    keys = ([sport_key] if sport_key else []) + [k for k in _krok_f1_sport_keys() if k != sport_key]

    names_by_key = {_f1_driver_name_key(name): name for name in driver_names}
    surname_map: dict[str, list[str]] = {}
    for driver in driver_names:
        surname_map.setdefault(_f1_driver_name_key(driver).split()[-1], []).append(driver)

    def resolve_driver(selection_name: str) -> str | None:
        clean_name = re.sub(r"\([^)]*\)", " ", str(selection_name or ""))
        key = _f1_driver_name_key(clean_name)
        if key in names_by_key:
            return names_by_key[key]
        candidate_tokens = set(key.split())
        surname_matches = {
            driver
            for surname, drivers in surname_map.items()
            if surname in candidate_tokens and len(drivers) == 1
            for driver in drivers
        }
        if len(surname_matches) == 1:
            return next(iter(surname_matches))
        best_name, best_score = None, 0.0
        for known_key, known_name in names_by_key.items():
            score = _similar(key, known_key)
            if score > best_score:
                best_name, best_score = known_name, score
        return best_name if best_score >= 0.84 else None

    result: dict[str, dict] = {}
    for key in keys:
        if not key:
            continue
        params = {"markets": "true", "limit": 100}
        if race_dt and race_dt > now:
            params["upcoming"] = "true"
        try:
            payload = _krok_get(f"/odds-feed/sports/{key}", params) or {}
            feed = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(feed, list):
                continue

            for event in feed:
                if not isinstance(event, dict) or not _f1_odds_event_matches_race(event, race):
                    continue

                bookmaker_rows = event.get("bookmakers")
                if isinstance(bookmaker_rows, list) and bookmaker_rows:
                    sources = bookmaker_rows
                else:
                    sources = [event]

                for source in sources:
                    if not isinstance(source, dict):
                        continue
                    bookmaker = str(
                        source.get("bookmaker_title") or source.get("bookmaker_name")
                        or source.get("title") or source.get("name")
                        or source.get("bookmaker_key") or event.get("bookmaker_title")
                        or event.get("bookmaker_key") or "KrokOdds"
                    ).strip()
                    markets = source.get("markets") or []
                    if isinstance(markets, dict):
                        markets = list(markets.values())
                    for market in markets:
                        if not isinstance(market, dict):
                            continue
                        market_key = str(
                            market.get("key") or market.get("market_key")
                            or market.get("name") or market.get("marketName")
                            or market.get("market_title") or market.get("market") or ""
                        ).lower()
                        market_text = " ".join(re.sub(r"[^a-z0-9]+", " ", market_key).split())
                        if any(token in market_text for token in (
                            "championship", "season", "constructor", "podium",
                            "top", "finish", "fastest", "qualifying", "sprint", "h2h",
                        )):
                            continue
                        if not re.search(r"\b(win|winner|outright)\b", market_text):
                            continue

                        selections = market.get("selections") or market.get("outcomes") or []
                        if isinstance(selections, dict):
                            selections = list(selections.values())
                        for selection in selections:
                            if not isinstance(selection, dict):
                                continue
                            selection_name = str(
                                selection.get("name") or selection.get("selection")
                                or selection.get("description") or selection.get("outcome") or ""
                            ).strip()
                            driver = resolve_driver(selection_name)
                            price = _decimal(
                                selection.get("price")
                                if selection.get("price") is not None
                                else selection.get("odds")
                            )
                            if not driver or price is None:
                                continue
                            current = result.get(driver)
                            if current is None or price > current["price"]:
                                result[driver] = {
                                    "price": price,
                                    "bookmaker": bookmaker,
                                    "provider": "KrokOdds",
                                    "market": market_key,
                                }
            if result:
                break
        except Exception as exc:
            print(f"[F1 ODDS] {key}: {exc}")
            continue
    return result


def _f1_select_current_race(races: list[dict], now: datetime) -> dict | None:
    """Select an active race weekend, otherwise the next scheduled Grand Prix.

    A fixed +/- 3 day window can keep yesterday's race selected after the
    weekend has ended. Use the scheduled first session through four hours after
    the race start as the active weekend window, then prefer the next race.
    """
    scheduled = []
    for race in races:
        if not race.get("date"):
            continue
        race_dt = _parse_dt(f"{race.get('date')}T{race.get('time', '00:00:00')}")
        if not race_dt:
            continue
        sessions = _f1_sessions(race)
        first_session = sessions[0]["datetime"] if sessions else race_dt
        scheduled.append((race, race_dt, first_session))
    if not scheduled:
        return None

    active_weekend = [
        row for row in scheduled
        if row[2] <= now <= row[1] + timedelta(hours=4)
    ]
    if active_weekend:
        return min(active_weekend, key=lambda row: abs((row[1] - now).total_seconds()))[0]

    upcoming = [row for row in scheduled if row[1] > now]
    if upcoming:
        return min(upcoming, key=lambda row: row[1])[0]

    return max(scheduled, key=lambda row: row[1])[0]


def get_f1_messages():
    races = _f1_races()
    if not races:
        return []

    now = datetime.now(YEREVAN_TZ)
    race = _f1_select_current_race(races, now)
    if not race:
        return []

    standings = _f1_standings()
    sport_key = _krok_f1_sport_key()
    driver_names = [row["name"] for row in standings]
    odds = _krok_f1_driver_odds(driver_names, sport_key, race, now)
    headshots = _krok_f1_headshots(driver_names, sport_key)

    for row in standings:
        if not row.get("photo_url"):
            row["photo_url"] = headshots.get(row["name"]) or _wikipedia_driver_photo(row)

    next_session = _f1_next_session_info(race, now)
    stage_info = _f1_current_stage_info(race, now)
    pit_stop_summary = _f1_pit_stop_summary(race)
    circuit = race.get("Circuit") or {}
    race_dt = _parse_dt(f"{race.get('date')}T{race.get('time', '00:00:00')}")
    return [("F1 event", {
        "sport": "f1",
        "type": "f1",
        "sport_icon": "🏎️",
        "competition": str(race.get("raceName") or "Formula 1 Grand Prix"),
        "round": race.get("round"),
        "circuit": str(circuit.get("circuitName") or ""),
        "locality": str((circuit.get("Location") or {}).get("locality") or ""),
        "country": str((circuit.get("Location") or {}).get("country") or ""),
        "race_date": race_dt.strftime("%d %b") if race_dt else "",
        "race_time": race_dt.strftime("%H:%M") if race_dt else "",
        "stage": _f1_current_stage(race, now),
        "stage_status": stage_info.get("status"),
        "stage_detail": stage_info.get("detail"),
        "next_session": next_session,
        "pit_stop_summary": pit_stop_summary,
        "odds_provider": "KrokOdds" if odds else None,
        "standings": [
            {
                "position": row["position"],
                "name": row["name"],
                "points": row["points"],
                "photo_url": row.get("photo_url"),
                "odds": odds.get(row["name"]),
            }
            for row in standings
        ],
    })]


def get_sport_messages(sport: str, date_from: str | None = None, date_to: str | None = None):
    sport = str(sport or "").strip().lower()
    if sport == "basketball":
        return get_nba_messages(date_from, date_to)
    if sport == "nba_preseason":
        return get_nba_preseason_messages(date_from, date_to)
    if sport == "euroleague":
        return get_euroleague_messages(date_from, date_to)
    if sport == "hockey":
        return get_nhl_messages(date_from, date_to)
    if sport == "nhl_preseason":
        return get_nhl_preseason_messages(date_from, date_to)
    if sport == "tennis":
        return get_tennis_messages(date_from, date_to)
    if sport == "f1":
        return get_f1_messages()
    raise ValueError(f"Unsupported sport: {sport}")
