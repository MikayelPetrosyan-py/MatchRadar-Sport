# 🏆 MatchRadar Sport
[Open MatchRadar Sport on Telegram]([https://t.me/My_Sport_Info_Bot](https://t.me/My_Sport_Info_Bot))

A Telegram bot that shows football fixtures (today, tomorrow, this week, or
this month) for the top European leagues, using live data from the
[API-Football](https://www.api-football.com/) REST API — no web scraping.

## Features

- `/start` flow with interactive buttons: choose a time period, then a league
- Supports Premier League, La Liga, Serie A, Bundesliga, Ligue 1,
  Champions League, or all of them at once
- Shows match date, local kickoff time, status, and score
- Correctly converts match times from UTC to a configured local timezone
  using Python's `zoneinfo`
- Handles all match states: not started, live (with current minute),
  halftime, finished, postponed, cancelled, suspended, abandoned
- Manual "🔄 Refresh" button to pull the latest live score without
  constant background polling (keeps API usage within free-tier limits)
- Clean error handling for API/network/Telegram failures

## Project structure

```
football_bot/
├── bot.py            # Telegram bot: commands, buttons, message flow
├── api.py            # API-Football client + response parsing + formatting
├── config.py         # Loads settings from .env
├── .env              # Secrets (not committed to git)
├── .gitignore
├── requirements.txt
└── README.md
```

## Setup

### 1. Clone and install dependencies

```bash
git clone <your-repo-url>
cd football_bot
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Get your API keys

- **Football data**: sign up free at [api-football.com](https://www.api-football.com/)
  and grab your API key from the dashboard.
- **Telegram bot token**: message [@BotFather](https://t.me/BotFather) on
  Telegram, run `/newbot`, and copy the token it gives you.

### 3. Configure environment variables

Create a `.env` file in the project root:

```
API_KEY=your_api_football_key_here
BOT_TOKEN=your_telegram_bot_token_here
TIMEZONE=Asia/Yerevan
LIVE_REFRESH_SECONDS=60
```

`TIMEZONE` must be a valid [IANA timezone name](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)
(e.g. `Asia/Yerevan`, `Europe/London`, `America/New_York`).

### 4. Run the bot

```bash
python bot.py
```

Then open your bot in Telegram and send `/start`.

## API notes

This project uses API-Football's **free plan**, which allows:

- 100 requests/day
- 10 requests/minute
- Live scores, match status, and elapsed minute — all included for free

Because the free tier has a low daily request cap, this bot fetches matches
on demand (when you pick a period/league, or press Refresh) instead of
polling automatically in the background. `LIVE_REFRESH_SECONDS` in `.env`
is kept as a configurable value for anyone who upgrades to a paid plan and
wants to wire up automatic polling later.

## Future improvements

- SQLite/PostgreSQL storage for:
  - Favorite teams and leagues per user
  - Match history and cached results
  - Push notifications for goals or kickoff
- Automatic background polling for live matches (requires a paid API plan)
- Pagination for long match lists instead of truncating
- Multi-language support

## Tech stack

Python 3 · python-telegram-bot · requests · python-dotenv · zoneinfo · API-Football REST API
