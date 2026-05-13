#!/usr/bin/env python3
"""
CS2 News Auto-Updater v3
Uses Playwright for match results (bypasses HLTV blocking)
"""

import sqlite3
import feedparser
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from datetime import datetime
import json
import os
import time
import signal
import sys
import re
try:
    from pandascore_api import fetch_and_save as fetch_odds
except ImportError:
    fetch_odds = None

try:
    from update_from_pandascore import fetch_player_performance, detect_roster_changes
except ImportError:
    fetch_player_performance = None
    detect_roster_changes = None

try:
    from advanced_predictor import calculate_all_elo
except ImportError:
    calculate_all_elo = None

try:
    from hltv_fetcher import scrape_rankings as hltv_rankings, scrape_results as hltv_results, scrape_upcoming as hltv_upcoming, scrape_player_stats as hltv_player_stats
    HAS_CFFI = True
except ImportError:
    HAS_CFFI = False

try:
    from hltv_odds import refresh_all as refresh_match_odds
except ImportError:
    refresh_match_odds = None

try:
    from prop_odds import refresh_all as refresh_props
except ImportError:
    refresh_props = None

# Config
UPDATE_INTERVAL = 1800  # 30 minutes
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cs2_data.db')
JSON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'predictor_data.json')
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'updater.log')

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
}

# ============================================================
# EVENT CLASSIFICATION & LAN DETECTION
# ============================================================

def classify_event(event_name):
    """Classify event tier: major, tier1, tier2, qualifier"""
    name = event_name.lower()
    if 'major' in name:
        return 'major'
    tier1 = ['esl pro league', 'blast premier', 'iem ', 'iem katowice', 'iem cologne',
             'pgl ', 'blast world final', 'blast spring final', 'blast fall final']
    if any(kw in name for kw in tier1):
        return 'tier1'
    tier2 = ['cct', 'betboom', 'esea', 'roobet', 'dreamhack', 'elisa', 'yalla']
    if any(kw in name for kw in tier2):
        return 'tier2'
    return 'qualifier'

def detect_lan(event_name):
    """Detect if event is LAN based on name"""
    name = event_name.lower()
    online = ['qualifier', 'open qual', 'closed qual', 'online', 'stage 1', 'group stage']
    if any(kw in name for kw in online):
        return False
    lan = ['major', 'iem ', 'esl pro league', 'blast premier final',
           'blast world final', 'blast spring final', 'blast fall final',
           'pgl ', 'dreamhack masters', 'blast open']
    if any(kw in name for kw in lan):
        return True
    return False

TOP_TEAMS = [
    'Natus Vincere', 'NaVi', 'G2', 'FaZe', 'Vitality', 'Spirit', 'MOUZ', 'mouz',
    'Liquid', 'FURIA', 'Heroic', 'HEROIC', 'Complexity', 'Cloud9', 'Astralis',
    'NIP', 'Ninjas in Pyjamas', 'BIG', 'Eternal Fire', 'ENCE', 'Monte', 'GamerLegion',
    '3DMAX', 'paiN', 'MIBR', 'Imperial', 'Apeks', 'Falcons', 'SAW', 'fnatic',
    'OG', 'BetBoom', 'Virtus.pro', 'Aurora', 'ECSTATIC', 'Into the Breach',
    'TheMongolz', 'Lynn Vision', 'FlyQuest', 'NRG', 'Wildcard', 'TYLOO',
    'Passion UA', '9INE', 'Zero Tenacity', 'Sangal', 'ALTERNATE aTTaX',
    'Johnny Speeds', 'FOKUS', 'FUT', 'Sharks', 'RED Canids', 'Case', 'KOI',
    'M80', 'Legacy', 'Gaimin Gladiators', 'PARIVISION'
]

def log(message):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_line = f"[{timestamp}] {message}"
    print(log_line)
    with open(LOG_PATH, 'a') as f:
        f.write(log_line + '\n')

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute('''
        CREATE TABLE IF NOT EXISTS news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT UNIQUE,
            url TEXT,
            published TEXT,
            category TEXT,
            content TEXT,
            teams_mentioned TEXT,
            players_mentioned TEXT,
            fetched_at TEXT
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS roster_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT UNIQUE,
            url TEXT,
            content TEXT,
            player TEXT,
            from_team TEXT,
            to_team TEXT,
            change_type TEXT,
            published TEXT,
            fetched_at TEXT
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team1 TEXT,
            team2 TEXT,
            score1 INTEGER,
            score2 INTEGER,
            winner TEXT,
            event TEXT,
            maps TEXT,
            match_date TEXT,
            fetched_at TEXT,
            UNIQUE(team1, team2, match_date, event)
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS team_form (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team TEXT,
            wins_last_10 INTEGER,
            losses_last_10 INTEGER,
            win_rate REAL,
            recent_changes TEXT,
            updated_at TEXT,
            UNIQUE(team)
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS upcoming_matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team1 TEXT,
            team2 TEXT,
            match_time TEXT,
            event TEXT,
            status TEXT,
            fetched_at TEXT,
            UNIQUE(team1, team2, event, match_time)
        )
    ''')

    # Add new columns if they don't exist
    for col, coltype in [('event_tier', 'TEXT'), ('is_lan', 'INTEGER DEFAULT 0')]:
        try:
            c.execute(f'ALTER TABLE matches ADD COLUMN {col} {coltype}')
        except:
            pass

    conn.commit()
    conn.close()

def fetch_article_content(url):
    try:
        time.sleep(1)
        response = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(response.text, 'html.parser')
        article = soup.select_one('.newstext-con')
        if article:
            paragraphs = article.find_all('p')
            return '\n'.join([p.get_text(strip=True) for p in paragraphs])[:5000]
        return ""
    except:
        return ""

def extract_teams(text):
    found = []
    text_lower = text.lower()
    for team in TOP_TEAMS:
        if team.lower() in text_lower:
            found.append(team)
    return list(set(found))

def extract_players(text):
    players = []
    quoted = re.findall(r'["\']([a-zA-Z0-9_-]{2,15})["\']', text)
    players.extend(quoted)
    return list(set(players))[:10]

def fetch_news():
    """Fetch news from RSS"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    try:
        feed = feedparser.parse("https://www.hltv.org/rss/news")
        now = datetime.now().isoformat()
        new_count = 0

        for entry in feed.entries[:15]:
            c.execute('SELECT id FROM news WHERE title = ?', (entry.title,))
            if c.fetchone():
                continue

            log(f"Fetching: {entry.title}")
            content = fetch_article_content(entry.link)
            full_text = f"{entry.title} {content}"
            teams = extract_teams(full_text)
            players = extract_players(full_text)

            try:
                c.execute('''
                    INSERT OR IGNORE INTO news
                    (title, url, published, category, content, teams_mentioned, players_mentioned, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (entry.title, entry.link, entry.get('published', ''), 'general',
                      content, json.dumps(teams), json.dumps(players), now))
                if c.rowcount > 0:
                    new_count += 1
                    log(f"  -> {len(content)} chars, Teams: {teams[:3]}")
            except:
                pass

        conn.commit()
        return new_count
    except Exception as e:
        log(f"Error fetching news: {e}")
        return 0
    finally:
        conn.close()

def fetch_results_playwright():
    """Fetch match results using Playwright"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    new_count = 0

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
            )
            page = context.new_page()

            log("Fetching results with Playwright...")
            page.goto('https://www.hltv.org/results', wait_until='networkidle', timeout=30000)
            page.wait_for_selector('.result-con', timeout=10000)

            result_elements = page.query_selector_all('.result-con')
            log(f"Found {len(result_elements)} matches")

            now = datetime.now().isoformat()
            today = datetime.now().strftime('%Y-%m-%d')

            for elem in result_elements[:50]:
                try:
                    teams = elem.query_selector_all('.team')
                    if len(teams) < 2:
                        continue

                    team1 = teams[0].inner_text().strip()
                    team2 = teams[1].inner_text().strip()

                    score_elem = elem.query_selector('.result-score')
                    if score_elem:
                        parts = score_elem.inner_text().strip().split('-')
                        try:
                            score1, score2 = int(parts[0].strip()), int(parts[1].strip())
                        except:
                            score1, score2 = 0, 0
                    else:
                        score1, score2 = 0, 0

                    winner = team1 if score1 > score2 else team2 if score2 > score1 else 'draw'

                    event_elem = elem.query_selector('.event-name')
                    event = event_elem.inner_text().strip() if event_elem else ''

                    event_tier = classify_event(event)
                    is_lan = 1 if detect_lan(event) else 0

                    # Skip if this exact match already exists
                    c.execute('SELECT id FROM matches WHERE team1=? AND team2=? AND score1=? AND score2=? AND event=?',
                              (team1, team2, score1, score2, event))
                    if c.fetchone():
                        continue

                    c.execute('''
                        INSERT OR IGNORE INTO matches
                        (team1, team2, score1, score2, winner, event, match_date, fetched_at, event_tier, is_lan)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (team1, team2, score1, score2, winner, event, today, now, event_tier, is_lan))

                    if c.rowcount > 0:
                        new_count += 1
                        log(f"  NEW: {team1} {score1}-{score2} {team2}")

                except:
                    continue

            browser.close()

        conn.commit()
        return new_count

    except Exception as e:
        log(f"Playwright error: {e}")
        return 0
    finally:
        conn.close()

def _event_from_href(href, team2=''):
    """Extract event name from match URL slug"""
    parts = href.rstrip('/').split('/')
    if len(parts) < 2:
        return ''
    slug = parts[-1]
    vs_idx = slug.find('-vs-')
    if vs_idx < 0:
        return ''
    after_vs = slug[vs_idx + 4:]
    if team2:
        team2_slug = team2.lower().replace(' ', '-').replace('.', '')
        if after_vs.startswith(team2_slug + '-'):
            after_vs = after_vs[len(team2_slug) + 1:]
        elif after_vs.startswith(team2_slug):
            after_vs = after_vs[len(team2_slug):]
    if not after_vs:
        return ''
    return after_vs.replace('-', ' ').title()


def fetch_upcoming_playwright():
    """Fetch upcoming/live matches (updated for 2026 HLTV layout)"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    new_count = 0

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            log("Fetching upcoming matches...")
            page.goto('https://www.hltv.org/matches', wait_until='networkidle', timeout=30000)
            time.sleep(2)

            now = datetime.now().isoformat()
            c.execute('DELETE FROM upcoming_matches')

            elements = page.query_selector_all('.match-wrapper')
            for elem in elements:
                try:
                    teams = elem.query_selector_all('.match-teamname')
                    if len(teams) < 2:
                        continue
                    team1 = teams[0].inner_text().strip()
                    team2 = teams[1].inner_text().strip()
                    if not team1 or not team2:
                        continue

                    time_el = elem.query_selector('.match-time')
                    match_time = time_el.inner_text().strip() if time_el else 'TBD'

                    link = elem.query_selector('a[href*="/matches/"]')
                    event = _event_from_href(link.get_attribute('href') or '', team2) if link else ''

                    is_live = elem.get_attribute('live') == 'true'
                    status = 'LIVE' if is_live else 'upcoming'
                    if is_live:
                        match_time = 'NOW'
                        log(f"  LIVE: {team1} vs {team2}")

                    c.execute('''
                        INSERT OR IGNORE INTO upcoming_matches
                        (team1, team2, match_time, event, status, fetched_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (team1, team2, match_time, event, status, now))
                    new_count += 1
                except:
                    continue

            browser.close()

        conn.commit()
        log(f"Found {new_count} upcoming/live matches")
        return new_count

    except Exception as e:
        log(f"Error fetching upcoming: {e}")
        return 0
    finally:
        conn.close()

def update_team_form():
    """Calculate team form"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    try:
        c.execute('''
            SELECT DISTINCT team FROM (
                SELECT team1 as team FROM matches
                UNION
                SELECT team2 as team FROM matches
            )
        ''')
        teams = [row[0] for row in c.fetchall()]
        now = datetime.now().isoformat()

        for team in teams:
            c.execute('''
                SELECT winner FROM matches
                WHERE team1 = ? OR team2 = ?
                ORDER BY match_date DESC, id DESC
                LIMIT 10
            ''', (team, team))

            results = c.fetchall()
            wins = sum(1 for r in results if r[0] == team)
            losses = len(results) - wins
            win_rate = wins / len(results) if results else 0

            c.execute('''
                SELECT title FROM roster_changes
                WHERE to_team LIKE ? OR from_team LIKE ?
                ORDER BY fetched_at DESC LIMIT 3
            ''', (f'%{team}%', f'%{team}%'))
            changes = [r[0] for r in c.fetchall()]

            c.execute('''
                INSERT OR REPLACE INTO team_form
                (team, wins_last_10, losses_last_10, win_rate, recent_changes, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (team, wins, losses, win_rate, json.dumps(changes), now))

        conn.commit()
        return len(teams)
    except Exception as e:
        log(f"Error updating form: {e}")
        return 0
    finally:
        conn.close()

def export_json():
    """Export to JSON"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute('SELECT * FROM news ORDER BY fetched_at DESC LIMIT 100')
    news = [dict(row) for row in c.fetchall()]

    c.execute('SELECT * FROM roster_changes ORDER BY fetched_at DESC LIMIT 50')
    roster = [dict(row) for row in c.fetchall()]

    c.execute('SELECT * FROM matches ORDER BY match_date DESC, id DESC LIMIT 200')
    matches = [dict(row) for row in c.fetchall()]

    c.execute('SELECT * FROM team_form ORDER BY win_rate DESC')
    form = [dict(row) for row in c.fetchall()]

    c.execute('SELECT * FROM upcoming_matches ORDER BY status DESC, match_time')
    upcoming = [dict(row) for row in c.fetchall()]

    conn.close()

    data = {
        'news': news,
        'roster_changes': roster,
        'matches': matches,
        'team_form': form,
        'upcoming_matches': upcoming,
        'updated': datetime.now().isoformat()
    }

    with open(JSON_PATH, 'w') as f:
        json.dump(data, f, indent=2)

    return len(news), len(roster), len(matches), len(form), len(upcoming)

def backfill_event_data():
    """Backfill event_tier and is_lan for existing matches"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT id, event FROM matches WHERE event_tier IS NULL OR is_lan IS NULL')
    rows = c.fetchall()
    if rows:
        for match_id, event in rows:
            tier = classify_event(event or '')
            lan = 1 if detect_lan(event or '') else 0
            c.execute('UPDATE matches SET event_tier = ?, is_lan = ? WHERE id = ?', (tier, lan, match_id))
        conn.commit()
        log(f"Backfilled event data for {len(rows)} matches")
    conn.close()

_update_cycle = 0

def run_update():
    """Run full update cycle"""
    global _update_cycle
    _update_cycle += 1

    log("="*50)
    log(f"STARTING UPDATE (v4 - cycle #{_update_cycle})")
    log("="*50)

    init_db()

    # Backfill event classification for old matches
    backfill_event_data()

    # News (RSS + article content)
    news_count = fetch_news()

    # Results + Upcoming (try curl_cffi first, fall back to Playwright)
    if HAS_CFFI:
        log("Using curl_cffi fetcher (bypasses Cloudflare)")
        results_count = hltv_results()
        upcoming_count = hltv_upcoming()
        hltv_rankings()  # Also refresh rankings/rosters
    else:
        results_count = fetch_results_playwright()
        upcoming_count = fetch_upcoming_playwright()

    # Team form
    teams_count = update_team_form()

    # Recalculate ELO after new matches
    if calculate_all_elo:
        try:
            calculate_all_elo()
            log("ELO ratings recalculated")
        except Exception as e:
            log(f"ELO recalc error: {e}")

    # Roster diff detection (every cycle)
    if detect_roster_changes:
        try:
            changes = detect_roster_changes()
            if changes:
                log(f"Detected {changes} roster changes")
        except Exception as e:
            log(f"Roster diff error: {e}")

    # Player stats refresh (every 12 cycles = ~6 hours at 30min interval)
    if _update_cycle % 12 == 1:
        hltv_players = 0
        if HAS_CFFI:
            try:
                hltv_players = hltv_player_stats()
                log(f"HLTV player stats: {hltv_players} players updated")
            except Exception as e:
                log(f"HLTV player stats error: {e}")
        if hltv_players == 0 and fetch_player_performance:
            try:
                updated = fetch_player_performance()
                log(f"PandaScore player stats fallback: {updated} players updated")
            except Exception as e:
                log(f"Player stats error: {e}")

    # Betting odds — multi-book scrape from HLTV (replaces PandaScore single-book)
    odds_count = 0
    if refresh_match_odds:
        try:
            summary = refresh_match_odds(verbose=False)
            odds_count = summary.get('books', 0)
            books_seen = len(summary.get('by_book', {}))
            log(f"[odds] hltv: {summary.get('matches', 0)} matches, "
                f"{odds_count} quotes from {books_seen} books")
        except Exception as e:
            log(f"[odds] hltv refresh FAILED: {e}")

    # PandaScore odds — kept as a secondary source (rarely returns anything on free tier)
    if fetch_odds:
        try:
            ps_count = fetch_odds() or 0
            if ps_count:
                log(f"[odds] pandascore: {ps_count} extra quotes")
        except Exception as e:
            log(f"[odds] pandascore FAILED: {e}")

    # Player props (currently no-op — relies on UI deep-links)
    if refresh_props:
        try:
            refresh_props(verbose=False)
        except Exception as e:
            log(f"[odds] props refresh FAILED: {e}")

    # Export
    n, r, m, f, u = export_json()

    log("-"*50)
    log(f"NEW: {news_count} news, {results_count} results, {upcoming_count} upcoming")
    log(f"TOTAL: {n} news, {r} roster, {m} matches, {f} teams, {u} upcoming")
    log("-"*50)

def signal_handler(sig, frame):
    log("Shutting down...")
    sys.exit(0)

def main():
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    log("")
    log("="*50)
    log("CS2 AUTO-UPDATER v3")
    log("Using Playwright for match results!")
    log(f"Update interval: {UPDATE_INTERVAL//60} minutes")
    log("="*50)

    run_update()

    while True:
        log(f"Next update in {UPDATE_INTERVAL//60} minutes...")
        time.sleep(UPDATE_INTERVAL)
        run_update()

if __name__ == "__main__":
    main()
