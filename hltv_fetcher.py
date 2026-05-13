#!/usr/bin/env python3
"""
HLTV data fetcher using curl_cffi to bypass Cloudflare
Replaces Playwright for scraping HLTV pages
"""

from curl_cffi import requests as cffi_requests
from bs4 import BeautifulSoup
import sqlite3
import json
import re
import os
import time
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cs2_data.db')
IMPERSONATE = 'chrome124'


def fetch_page(url):
    """Fetch a page using curl_cffi with Chrome TLS fingerprint"""
    r = cffi_requests.get(url, impersonate=IMPERSONATE, timeout=30)
    if 'Just a moment' in r.text:
        return None
    return BeautifulSoup(r.text, 'html.parser')


def scrape_rankings():
    """Scrape HLTV world rankings with rosters"""
    print("[HLTV] Scraping rankings...")
    soup = fetch_page('https://www.hltv.org/ranking/teams')
    if not soup:
        print("[HLTV] Rankings page blocked")
        return 0

    teams = []
    for elem in soup.select('.ranked-team')[:30]:
        try:
            pos = elem.select_one('.position')
            name = elem.select_one('.name')
            points = elem.select_one('.points')

            if pos and name:
                rank = int(pos.get_text(strip=True).replace('#', ''))
                team_name = name.get_text(strip=True)
                pts_text = points.get_text(strip=True) if points else '0'
                pts = int(re.sub(r'[^\d]', '', pts_text))

                roster = [p.get_text(strip=True) for p in elem.select('.nick')]

                link = elem.select_one('a.moreLink')
                team_id = None
                if link and link.get('href'):
                    match = re.search(r'/team/(\d+)/', link['href'])
                    if match:
                        team_id = match.group(1)

                teams.append({
                    'rank': rank, 'name': team_name, 'points': pts,
                    'roster': roster, 'team_id': team_id
                })
        except:
            continue

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    for t in teams:
        c.execute('''INSERT OR REPLACE INTO team_rankings
                     (team, world_rank, points, roster, team_id, updated_at)
                     VALUES (?, ?, ?, ?, ?, datetime("now"))''',
                  (t['name'], t['rank'], t['points'], json.dumps(t['roster']), t['team_id']))

        # Also update players table with roster
        for player_name in t['roster']:
            c.execute('''INSERT OR IGNORE INTO players (name, team, updated_at)
                         VALUES (?, ?, datetime("now"))''', (player_name, t['name']))

    conn.commit()
    conn.close()
    print(f"[HLTV] Updated {len(teams)} team rankings")
    return len(teams)


def scrape_results():
    """Scrape recent match results"""
    from auto_updater import classify_event, detect_lan

    print("[HLTV] Scraping results...")
    soup = fetch_page('https://www.hltv.org/results')
    if not soup:
        print("[HLTV] Results page blocked")
        return 0

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    new_count = 0
    today = datetime.now().strftime('%Y-%m-%d')

    for elem in soup.select('.result-con')[:100]:
        try:
            teams = elem.select('.team')
            if len(teams) < 2:
                continue

            team1 = teams[0].get_text(strip=True)
            team2 = teams[1].get_text(strip=True)

            score_elem = elem.select_one('.result-score')
            if not score_elem:
                continue
            parts = score_elem.get_text(strip=True).split('-')
            score1, score2 = int(parts[0].strip()), int(parts[1].strip())

            winner = team1 if score1 > score2 else team2
            event_elem = elem.select_one('.event-name')
            event = event_elem.get_text(strip=True) if event_elem else ''

            event_tier = classify_event(event)
            is_lan = 1 if detect_lan(event) else 0

            # Check if this exact match already exists (regardless of date)
            c.execute('''SELECT id FROM matches
                WHERE team1 = ? AND team2 = ? AND score1 = ? AND score2 = ? AND event = ?''',
                (team1, team2, score1, score2, event))
            if c.fetchone():
                continue

            c.execute('''INSERT OR IGNORE INTO matches
                (team1, team2, score1, score2, winner, event, match_date, fetched_at, event_tier, is_lan)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime("now"), ?, ?)''',
                (team1, team2, score1, score2, winner, event, today, event_tier, is_lan))

            if c.rowcount > 0:
                new_count += 1
        except:
            continue

    conn.commit()
    conn.close()
    print(f"[HLTV] Added {new_count} new results")
    return new_count


def _event_from_href(href, team2=''):
    """Extract event name from match URL slug like /matches/123/team1-vs-team2-event-name"""
    parts = href.rstrip('/').split('/')
    if len(parts) < 2:
        return ''
    slug = parts[-1]
    vs_idx = slug.find('-vs-')
    if vs_idx < 0:
        return ''
    after_vs = slug[vs_idx + 4:]
    # Strip team2 name from the start of after_vs
    if team2:
        team2_slug = team2.lower().replace(' ', '-').replace('.', '')
        if after_vs.startswith(team2_slug + '-'):
            after_vs = after_vs[len(team2_slug) + 1:]
        elif after_vs.startswith(team2_slug):
            after_vs = after_vs[len(team2_slug):]
    if not after_vs:
        return ''
    return after_vs.replace('-', ' ').title()


def scrape_upcoming():
    """Scrape upcoming/live matches (updated for 2026 HLTV layout)"""
    print("[HLTV] Scraping upcoming matches...")
    soup = fetch_page('https://www.hltv.org/matches')
    if not soup:
        print("[HLTV] Matches page blocked")
        return 0

    # Parse all matches first, only touch DB if we got data
    parsed = []
    now = datetime.now().isoformat()

    for elem in soup.select('.match-wrapper'):
        try:
            teams = elem.select('.match-teamname')
            if len(teams) < 2:
                continue
            team1 = teams[0].get_text(strip=True)
            team2 = teams[1].get_text(strip=True)
            if not team1 or not team2:
                continue

            time_el = elem.select_one('.match-time')
            match_time = time_el.get_text(strip=True) if time_el else 'TBD'

            link = elem.select_one('a[href*="/matches/"]')
            href = link.get('href', '') if link else ''
            event = _event_from_href(href, team2) if href else ''
            match_url = ('https://www.hltv.org' + href) if href.startswith('/matches/') else ''

            is_live = elem.get('live', 'false') == 'true'
            status = 'LIVE' if is_live else 'upcoming'
            if is_live:
                match_time = 'NOW'

            parsed.append((team1, team2, match_time, event, status, now, match_url))
        except:
            continue

    if not parsed:
        print("[HLTV] No matches parsed, keeping existing data")
        return 0

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Make sure the match_url column exists (legacy DBs won't have it)
    cols = {r[1] for r in c.execute("PRAGMA table_info(upcoming_matches)").fetchall()}
    if 'match_url' not in cols:
        c.execute('ALTER TABLE upcoming_matches ADD COLUMN match_url TEXT')
    c.execute('DELETE FROM upcoming_matches')
    for row in parsed:
        c.execute('''INSERT OR IGNORE INTO upcoming_matches
            (team1, team2, match_time, event, status, fetched_at, match_url)
            VALUES (?, ?, ?, ?, ?, ?, ?)''', row)
    conn.commit()
    count = len(parsed)
    conn.close()
    print(f"[HLTV] Found {count} upcoming/live matches")
    return count


def _get_player_ids_from_team(team_id, team_name):
    """Get player HLTV IDs from a team page"""
    slug = team_name.lower().replace(' ', '-').replace('.', '')
    soup = fetch_page(f'https://www.hltv.org/team/{team_id}/{slug}')
    if not soup:
        return {}
    players = {}
    for link in soup.select('a[href^="/player/"]'):
        href = link.get('href', '')
        m = re.match(r'/player/(\d+)/(\S+)', href)
        if m:
            name = link.get_text(strip=True)
            if name and len(name) < 20 and '"' not in name and name not in players:
                players[name] = m.group(1)
    return players


def _scrape_player_page(player_id, player_slug):
    """Scrape rating and stats from individual player page"""
    soup = fetch_page(f'https://www.hltv.org/player/{player_id}/{player_slug}')
    if not soup:
        return None

    stats = {}

    # Rating 3.0 from .player-stat
    for ps in soup.select('.player-stat'):
        label_elem = ps.select_one('.player-stat-label')
        val_elem = ps.select_one('.statsVal')
        if not label_elem or not val_elem:
            # Fallback: check text content
            text = ps.get_text(' ', strip=True)
            if 'Rating' in text:
                val = ps.select_one('.statsVal')
                if val:
                    try:
                        stats['rating'] = float(val.get_text(strip=True))
                    except ValueError:
                        pass

    # If we got rating from labeled approach
    for ps in soup.select('.player-stat'):
        children = [c for c in ps.children if hasattr(c, 'get_text')]
        if len(children) >= 2:
            label = children[0].get_text(strip=True)
            value = children[1].get_text(strip=True)
            if 'Rating' in label:
                try:
                    stats['rating'] = float(value)
                except ValueError:
                    pass

    # Career stats: KDR, win rate, matches
    for stat_el in soup.select('.stat'):
        parent = stat_el.parent
        if parent:
            full = parent.get_text(' ', strip=True)
            val = stat_el.get_text(strip=True)
            if 'Average KDR' in full:
                try:
                    stats['kdr'] = float(val)
                except ValueError:
                    pass
            elif 'Matches' in full and 'Matches' == full.replace(val, '').strip():
                try:
                    stats['maps'] = int(val.replace(',', ''))
                except ValueError:
                    pass

    return stats if stats else None


def scrape_player_stats():
    """Scrape player ratings from HLTV player pages via team rosters"""
    print("[HLTV] Scraping player stats...")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Get teams with HLTV IDs from rankings
    c.execute('SELECT team, team_id, roster FROM team_rankings WHERE team_id IS NOT NULL ORDER BY world_rank')
    teams = c.fetchall()
    if not teams:
        print("[HLTV] No teams with IDs in database")
        conn.close()
        return 0

    # Step 1: Get player HLTV IDs from team pages
    player_ids = {}  # name -> (hltv_id, team_name)
    for team_name, team_id, roster_json in teams:
        time.sleep(1)
        roster = json.loads(roster_json) if roster_json else []
        ids = _get_player_ids_from_team(team_id, team_name)
        for pname in roster:
            if pname in ids:
                player_ids[pname] = (ids[pname], team_name)
        print(f"[HLTV] {team_name}: found IDs for {sum(1 for p in roster if p in ids)}/{len(roster)} players")

    print(f"[HLTV] Total: {len(player_ids)} player IDs collected")

    # Step 2: Scrape individual player pages for stats
    updated = 0
    for name, (pid, team_name) in player_ids.items():
        time.sleep(1)
        slug = name.lower().replace(' ', '-')
        stats = _scrape_player_page(pid, slug)
        if not stats or 'rating' not in stats:
            continue

        rating = stats['rating']
        maps_played = stats.get('maps')

        c.execute('''UPDATE players SET rating = ?, maps_played = ?, updated_at = datetime("now")
                     WHERE name = ?''', (rating, maps_played, name))
        if c.rowcount == 0:
            c.execute('''INSERT INTO players (name, team, rating, maps_played, updated_at)
                         VALUES (?, ?, ?, ?, datetime("now"))''',
                      (name, team_name, rating, maps_played))
        updated += 1

        if updated % 10 == 0:
            conn.commit()
            print(f"[HLTV] Progress: {updated}/{len(player_ids)} players updated")

    conn.commit()
    conn.close()

    print(f"[HLTV] Updated {updated} players with ratings")
    return updated


if __name__ == '__main__':
    print("="*50)
    print("HLTV Data Fetcher (curl_cffi)")
    print("="*50 + "\n")

    scrape_rankings()
    print()
    scrape_results()
    print()
    scrape_upcoming()
    print()
    scrape_player_stats()

    # Summary
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    teams = c.execute('SELECT COUNT(*) FROM team_rankings').fetchone()[0]
    players = c.execute('SELECT COUNT(*) FROM players').fetchone()[0]
    matches = c.execute('SELECT COUNT(*) FROM matches').fetchone()[0]
    upcoming = c.execute('SELECT COUNT(*) FROM upcoming_matches').fetchone()[0]
    conn.close()

    print(f"\nTOTALS: {teams} teams, {players} players, {matches} matches, {upcoming} upcoming")
