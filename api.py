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

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import difflib
import os
import re
import time
import unicodedata

import requests
from dotenv import load_dotenv

from config import TIMEZONE


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
    raw_matches = _request_competition(league_id, date_from, date_to)
    return [_parse_fixture(match) for match in raw_matches]


def get_fixtures_all_leagues(date_from: str, date_to: str) -> list:
    all_fixtures = []
    for league in LEAGUES.values():
        all_fixtures.extend(get_fixtures_by_date_range(league["id"], date_from, date_to))
    return all_fixtures


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

    competition_priority = {"CL": 100, "PL": 90, "PD": 85, "SA": 80, "BL1": 75, "FL1": 70}

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
    os.getenv("LIVE_SCORE_REFRESH_SECONDS", os.getenv("LIVE_REFRESH_SECONDS", "120"))
)

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


def _normalize_odds_team_name(team_name: str | None) -> str:
    if not team_name:
        return ""
    # IMPORTANT: strip accents BEFORE the alnum regex. Without this,
    # "Málaga" -> "m laga" (the "á" is dropped as non-alnum, splitting one
    # word into two bogus tokens), which no longer matches the odds
    # provider's "Malaga" and silently loses that fixture's odds — this is
    # exactly what happened to Málaga FC vs Villarreal FC.
    value = _strip_accents(str(team_name)).lower()
    value = re.sub(r"[^a-z0-9]+", " ", value).strip()
    tokens = [
        token for token in value.split()
        if token not in {
            "fc", "cf", "afc", "sc", "ac", "cd", "rcd", "de", "del",
            "da", "dos", "do", "the", "club",
        }
    ]
    return " ".join(tokens)


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


def enrich_matches_with_odds(matches: list) -> list:
    """Attach real Bet365 1X2 odds without touching the football-data match source.

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
        date_from = today_local
        date_to = today_local + timedelta(days=30)
    else:
        raise ValueError(f"Unknown period: {period}")

    return date_from.strftime("%Y-%m-%d"), date_to.strftime("%Y-%m-%d")