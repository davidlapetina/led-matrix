"""Live scores and team logos from ESPN's public scoreboard API.

The API needs no key but is unofficial (undocumented), so ESPN may change it.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

from . import http

BASE = "https://site.api.espn.com/apis/site/v2/sports"

# key -> (ESPN path, label, home team shown first)
LEAGUES = {
    "nba": ("basketball/nba", "NBA", False),
    "epl": ("soccer/eng.1", "Premier League", True),
    "seriea": ("soccer/ita.1", "Serie A", True),
    "ligue1": ("soccer/fra.1", "Ligue 1", True),
    "laliga": ("soccer/esp.1", "LaLiga", True),
    "bundesliga": ("soccer/ger.1", "Bundesliga", True),
    "ucl": ("soccer/uefa.champions", "Champions League", True),
}


@dataclass(frozen=True)
class Team:
    name: str
    abbr: str
    logo: str  # URL of a transparent PNG (the dark-background variant when ESPN has one)
    score: str


@dataclass(frozen=True)
class Game:
    id: str
    league: str
    left: Team    # home first for soccer, away first for the NBA (each sport's convention)
    right: Team
    state: str    # "pre", "in" or "post"
    detail: str   # e.g. "Final", "FT", "Q3 4:12", "67'"
    start: _dt.datetime  # UTC

    @property
    def started(self) -> bool:
        return self.state != "pre"


def parse_scoreboard(data: dict, league: str) -> list[Game]:
    home_first = LEAGUES[league][2]
    games = []
    for event in data.get("events", []):
        competitors = event["competitions"][0]["competitors"]
        side = {c["homeAway"]: c for c in competitors}
        teams = {k: Team(c["team"].get("displayName", c["team"]["abbreviation"]),
                         c["team"]["abbreviation"],
                         c["team"].get("logoDark") or c["team"].get("logo", ""),
                         str(c.get("score", "")))
                 for k, c in side.items()}
        left, right = ("home", "away") if home_first else ("away", "home")
        status = event["status"]["type"]
        games.append(Game(
            id=event["id"], league=league, left=teams[left], right=teams[right],
            state=status["state"], detail=status.get("shortDetail", ""),
            start=_dt.datetime.fromisoformat(event["date"].replace("Z", "+00:00")),
        ))
    return games


async def games(league: str, date: _dt.date | None = None) -> list[Game]:
    """Games of a league on a date (ESPN's current day when None)."""
    path = LEAGUES[league][0]
    url = f"{BASE}/{path}/scoreboard"
    if date:
        url += f"?dates={date:%Y%m%d}"
    data = await http.get_json(url)
    return parse_scoreboard(data, league)


async def game(league: str, game_id: str, date: _dt.date | None = None) -> Game:
    for g in await games(league, date):
        if g.id == game_id:
            return g
    raise KeyError(f"game {game_id} not found in {league}")


_logos: dict[str, bytes] = {}


async def logo(url: str) -> bytes:
    """Team logo PNG, cached for the life of the process."""
    if url not in _logos:
        _logos[url] = await http.get(url)
    return _logos[url]
