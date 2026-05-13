#!/usr/bin/env python3
"""
Player-prop odds helper.

Scraping 1xbet / Melbet player kill props directly is fragile (anti-bot,
geo-restrictions, login walls) and would need proxies + constant maintenance.
For now we:

1. Maintain a `player_props` table so a future scraper can populate it.
2. Generate **deep-links** to 1xbet / Melbet / Stake / OddsPortal search pages
   so the user can verify the model's suggested line in one click.

When/if we ever integrate a real prop feed (e.g. paid API, Pinnacle JSON,
Bo3.gg widget), `refresh_all()` will be where it lives.
"""

from __future__ import annotations
import sqlite3
import os
import urllib.parse
from datetime import datetime
from typing import List, Dict

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cs2_data.db')


# ---------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------

def init_schema():
    """Create player_props table if missing."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS player_props (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player TEXT,
            match_team1 TEXT,
            match_team2 TEXT,
            market TEXT,
            line REAL,
            over_odds REAL,
            under_odds REAL,
            bookmaker TEXT,
            fetched_at TEXT,
            UNIQUE(player, match_team1, match_team2, market, line, bookmaker)
        )
    ''')
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------
# Deep-link generators (the realistic part — no scraping)
# ---------------------------------------------------------------------

def bookmaker_links(team1: str, team2: str) -> List[Dict]:
    """Return a list of {bookmaker, url, kind} where kind is 'search' or 'page'.
    Each link takes the user as close to the live market as possible."""
    q = urllib.parse.quote(f'{team1} {team2}')
    return [
        {
            'bookmaker': '1xbet',
            'url': f'https://1xbet.com/en/search/?q={q}',
            'kind': 'search',
            'note': 'Search 1xbet for this match — kills/headshot props live in "All bets"',
        },
        {
            'bookmaker': 'Melbet',
            'url': f'https://melbet.com/en/line/esports/?q={q}',
            'kind': 'search',
            'note': 'Melbet esports line — find the match, expand player props',
        },
        {
            'bookmaker': 'Stake',
            'url': f'https://stake.com/sports/esports/counter-strike?search={q}',
            'kind': 'search',
            'note': 'Stake CS2 — has player kills / map handicap markets',
        },
        {
            'bookmaker': 'OddsPortal',
            'url': f'https://www.oddsportal.com/search/{q}/',
            'kind': 'aggregator',
            'note': 'Compare match-winner odds across 50+ books',
        },
        {
            'bookmaker': 'Bo3.gg',
            'url': f'https://bo3.gg/?search={q}',
            'kind': 'aggregator',
            'note': 'Esports stats site — sometimes has prop odds',
        },
    ]


# ---------------------------------------------------------------------
# Stored-prop accessors (for future use)
# ---------------------------------------------------------------------

def get_props_for_match(team1: str, team2: str) -> List[Dict]:
    """Return any stored player props for this matchup. Currently usually empty —
    populated later when a real scraper is wired in. Safe if the table doesn't
    exist yet (returns []).
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            '''SELECT player, market, line, over_odds, under_odds, bookmaker, fetched_at
               FROM player_props
               WHERE (match_team1 = ? AND match_team2 = ?)
                  OR (match_team1 = ? AND match_team2 = ?)
               ORDER BY fetched_at DESC, player''',
            (team1, team2, team2, team1),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------
# Stub refresh — no-op until a real prop feed is wired in
# ---------------------------------------------------------------------

def refresh_all(verbose: bool = True) -> Dict:
    """Placeholder — kept to keep auto_updater integration uniform with hltv_odds.
    When we get a real prop feed (paid API, Pinnacle markets, etc), populate it here."""
    init_schema()
    if verbose:
        print('[prop_odds] no live prop feed configured — relying on deep-links in UI')
    return {'players': 0, 'lines': 0}


if __name__ == '__main__':
    init_schema()
    print('player_props table ready.')
    for link in bookmaker_links('Spirit', 'MOUZ'):
        print(f"  {link['bookmaker']:12} -> {link['url']}")
