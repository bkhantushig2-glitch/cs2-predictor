#!/usr/bin/env python3
"""
HLTV multi-bookmaker match-winner odds scraper.

Fetches each upcoming match's odds widget and stores one row per bookmaker
in `betting_odds`. Works for Pinnacle, 1xbet, Melbet, GGBet, Thunderpick,
Stake, Betify, etc. — whichever HLTV's affiliate widget happens to render.

Bypasses Cloudflare via curl_cffi chrome124 fingerprint (same trick as hltv_fetcher.py).
"""

from __future__ import annotations
from curl_cffi import requests as cffi_requests
from bs4 import BeautifulSoup
from typing import Optional
import sqlite3
import re
import os
import time
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cs2_data.db')
IMPERSONATE = 'chrome124'
HLTV_BASE = 'https://www.hltv.org'
THROTTLE_SEC = 2.0  # be polite — 1 request per 2 seconds


# ---------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------

def init_schema():
    """Add match_url column to upcoming_matches if missing.
    `betting_odds` already exists with the right columns."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    cols = {row[1] for row in c.execute("PRAGMA table_info(upcoming_matches)").fetchall()}
    if 'match_url' not in cols:
        c.execute('ALTER TABLE upcoming_matches ADD COLUMN match_url TEXT')
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------
# Match-page DOM parser
# ---------------------------------------------------------------------

LOGO_RE = re.compile(r'^Logo for\s+(.+?)(?:\s+Row)?\s*$', re.I)


def _normalize_bookmaker(name: str) -> str:
    """Canonicalize bookmaker names (HLTV affiliate names → standard form)."""
    n = name.strip()
    aliases = {
        'ggbet': 'GG.bet',
        'gg.bet': 'GG.bet',
        '1xbet': '1xbet',
        'melbet': 'Melbet',
        'pinnacle': 'Pinnacle',
        'stake': 'Stake',
        'thunderpick': 'Thunderpick',
        'betway': 'Betway',
        'roobet': 'Roobet',
        'csgopositive': 'CSGOPositive',
        'thunderpick row': 'Thunderpick',
        'fezbet': 'Fezbet',
        'betify': 'Betify',
        'granawin': 'GranaWin',
        'betlabel': 'BetLabel',
        'vulkan': 'Vulkan',
        'bcgame': 'BC.Game',
        'bc.game': 'BC.Game',
    }
    return aliases.get(n.lower(), n)


def parse_match_odds_page(html: str) -> list[dict]:
    """Pull every .provider row's bookmaker + odds out of an HLTV match page.
    Returns list of {bookmaker, team1_odds, team2_odds}."""
    soup = BeautifulSoup(html, 'html.parser')
    rows = []
    for el in soup.select('.provider'):
        img = el.find('img')
        if not img:
            continue
        alt = img.get('alt', '')
        m = LOGO_RE.match(alt)
        if not m:
            continue
        book = _normalize_bookmaker(m.group(1))

        odds_cells = el.select('.odds-cell')
        if len(odds_cells) < 2:
            continue  # no live odds (promo-only row, e.g. csgopositive)

        try:
            t1 = float(odds_cells[0].get_text(strip=True))
            t2 = float(odds_cells[1].get_text(strip=True))
        except (ValueError, IndexError):
            continue

        # Sanity check: decimal odds should be in (1.01, 50)
        if not (1.01 <= t1 <= 50 and 1.01 <= t2 <= 50):
            continue

        rows.append({'bookmaker': book, 'team1_odds': t1, 'team2_odds': t2})
    return rows


def fetch_page(url: str) -> Optional[str]:
    try:
        r = cffi_requests.get(url, impersonate=IMPERSONATE, timeout=30)
        if r.status_code != 200 or 'Just a moment' in r.text:
            return None
        return r.text
    except Exception as e:
        print(f"[hltv_odds] fetch error {url}: {e}")
        return None


def scrape_match_odds(match_url: str) -> list[dict]:
    """High-level: fetch a match page and return parsed odds rows."""
    html = fetch_page(match_url)
    if not html:
        return []
    return parse_match_odds_page(html)


# ---------------------------------------------------------------------
# Upcoming-match URL discovery
# ---------------------------------------------------------------------

def harvest_upcoming_urls() -> int:
    """Re-scrape /matches and store the match_url for each upcoming row.
    Updates upcoming_matches in place."""
    html = fetch_page(f'{HLTV_BASE}/matches')
    if not html:
        print('[hltv_odds] could not fetch matches list')
        return 0
    soup = BeautifulSoup(html, 'html.parser')

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    matched = 0

    for el in soup.select('.match-wrapper'):
        names = el.select('.match-teamname')
        if len(names) < 2:
            continue
        team1 = names[0].get_text(strip=True)
        team2 = names[1].get_text(strip=True)
        link = el.select_one('a[href*="/matches/"]')
        if not link:
            continue
        href = link.get('href', '')
        m = re.search(r'(/matches/\d+/[a-z0-9-]+)', href)
        if not m:
            continue
        url = HLTV_BASE + m.group(1)

        c.execute(
            'UPDATE upcoming_matches SET match_url = ? WHERE team1 = ? AND team2 = ?',
            (url, team1, team2)
        )
        if c.rowcount > 0:
            matched += 1

    conn.commit()
    conn.close()
    return matched


# ---------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------

def save_quotes(team1: str, team2: str, match_date: str, quotes: list[dict]) -> int:
    """Replace existing quotes for this matchup and write new ones."""
    if not quotes:
        return 0
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Wipe stale quotes for this matchup so we don't accumulate forever
    c.execute(
        '''DELETE FROM betting_odds
           WHERE (team1 = ? AND team2 = ?) OR (team1 = ? AND team2 = ?)''',
        (team1, team2, team2, team1),
    )

    inserted = 0
    now = datetime.now().isoformat(timespec='seconds')
    for q in quotes:
        c.execute(
            '''INSERT INTO betting_odds
               (team1, team2, team1_odds, team2_odds, bookmaker, match_date, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (team1, team2, q['team1_odds'], q['team2_odds'],
             q['bookmaker'], match_date or '', now),
        )
        inserted += 1

    conn.commit()
    conn.close()
    return inserted


# ---------------------------------------------------------------------
# Top-level refresh
# ---------------------------------------------------------------------

def refresh_all(verbose: bool = True) -> dict:
    """Iterate every top-tier upcoming match and refresh its multi-book odds.
    Returns {matches: N, books: M, by_book: {...}}."""
    init_schema()
    harvest_upcoming_urls()

    # Lazy import to avoid circular deps with auto_updater
    try:
        from auto_updater import classify_event
    except ImportError:
        classify_event = lambda _: 'tier2'

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        'SELECT team1, team2, event, match_time, match_url FROM upcoming_matches '
        'WHERE match_url IS NOT NULL'
    ).fetchall()
    conn.close()

    target = []
    for r in rows:
        tier = classify_event(r['event'] or '')
        if tier in ('major', 'tier1', 'tier2'):
            target.append(r)

    matches_done = 0
    books_total = 0
    by_book: dict[str, int] = {}

    for r in target:
        if verbose:
            print(f"[hltv_odds] {r['team1']} vs {r['team2']} ...", end=' ', flush=True)
        quotes = scrape_match_odds(r['match_url'])
        if not quotes:
            if verbose:
                print('no odds')
            time.sleep(THROTTLE_SEC)
            continue

        save_quotes(r['team1'], r['team2'], r['match_time'] or '', quotes)
        matches_done += 1
        books_total += len(quotes)
        for q in quotes:
            by_book[q['bookmaker']] = by_book.get(q['bookmaker'], 0) + 1
        if verbose:
            print(f"{len(quotes)} books")
        time.sleep(THROTTLE_SEC)

    summary = {'matches': matches_done, 'books': books_total, 'by_book': by_book}
    if verbose:
        print(f"\n[hltv_odds] done: {matches_done} matches, {books_total} quotes "
              f"from {len(by_book)} unique books")
        for book, n in sorted(by_book.items(), key=lambda x: -x[1]):
            print(f"  {book:18} {n}")
    return summary


if __name__ == '__main__':
    refresh_all()
