#!/usr/bin/env python3
"""
Update team rosters, match history, and player data from PandaScore API
Bypasses HLTV Cloudflare blocking by using PandaScore instead
"""

import requests
import sqlite3
import json
import os
import time
from datetime import datetime, timedelta

API_KEY = os.environ.get('PANDASCORE_API_KEY', 'HOcgZB4p46e3hS5TPwlG32c-QcdPT3OcDlYUWCVOn5TdP62vooc')
BASE_URL = 'https://api.pandascore.co'
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cs2_data.db')
headers = {'Authorization': f'Bearer {API_KEY}'}

# Known PandaScore team IDs for top teams (to avoid picking academy teams)
TEAM_IDS = {
    'Vitality': 3455,
    'Spirit': 124523,
    'Natus Vincere': 3216,
    'FaZe': 3212,
    'MOUZ': 3240,
    'FURIA': 124530,
    'G2': 3210,
    'Liquid': 3213,
    'Astralis': 3209,
    'Falcons': 130564,
    'The MongolZ': 3272,
    'HEROIC': 3246,
    'BIG': 3249,
    'Complexity': 3211,
    'paiN': 125751,
    'GamerLegion': 125847,
    'NIP': 3218,
    'Fnatic': 3217,
    'ENCE': 3251,
    'SAW': 126738,
    '3DMAX': 3318,
    'Aurora': 131505,
    'PARIVISION': 133719,
    'NRG': 3256,
    'M80': 133435,
    'MIBR': 3250,
    'Imperial': 126377,
    'Eternal Fire': 129413,
    'Apeks': 130552,
}


def _classify_event(event_name):
    """Local event classifier (avoids circular import from auto_updater)"""
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


def _detect_lan(event_name):
    """Local LAN detector (avoids circular import from auto_updater)"""
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


def _extract_match_info(match):
    """Extract team names, scores, winner, event, date from a PandaScore match object"""
    opponents = match.get('opponents', [])
    if len(opponents) < 2:
        return None

    team1 = opponents[0].get('opponent', {}).get('name', '')
    team2 = opponents[1].get('opponent', {}).get('name', '')

    results = match.get('results', [])
    if len(results) < 2:
        return None

    score1 = results[0].get('score', 0)
    score2 = results[1].get('score', 0)

    winner = match.get('winner', {})
    winner_name = winner.get('name', '') if winner else ''

    event_name = ''
    league = match.get('league', {})
    serie = match.get('serie', {})
    if league:
        event_name = league.get('name', '')
    if serie and serie.get('full_name'):
        event_name = serie['full_name']

    match_date = match.get('end_at', '')[:10] if match.get('end_at') else ''

    if not (team1 and team2 and match_date):
        return None

    return {
        'team1': team1, 'team2': team2,
        'score1': score1, 'score2': score2,
        'winner': winner_name, 'event': event_name,
        'match_date': match_date,
        'event_tier': _classify_event(event_name),
        'is_lan': 1 if _detect_lan(event_name) else 0,
    }


# ============================================================
# STEP 1: BACKFILL MATCH HISTORY
# ============================================================

def backfill_matches(days=90, max_pages=25):
    """
    Page through PandaScore past matches going back `days` days.
    Dedupes against existing matches table.
    """
    print(f"\nBackfilling matches (last {days} days, up to {max_pages} pages)...\n")

    since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%dT00:00:00Z')

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    new_matches = 0
    total_fetched = 0

    for page in range(1, max_pages + 1):
        try:
            r = requests.get(f'{BASE_URL}/csgo/matches/past', headers=headers,
                             params={
                                 'per_page': 100,
                                 'page': page,
                                 'sort': '-end_at',
                                 'range[end_at]': f'{since},',
                             },
                             timeout=15)
            if r.status_code != 200:
                print(f"  Page {page}: HTTP {r.status_code}, stopping")
                break

            matches = r.json()
            if not matches:
                print(f"  Page {page}: no more matches")
                break

            total_fetched += len(matches)

            for match in matches:
                info = _extract_match_info(match)
                if not info:
                    continue

                c.execute('''INSERT OR IGNORE INTO matches
                    (team1, team2, score1, score2, winner, event, match_date, fetched_at, event_tier, is_lan)
                    VALUES (?, ?, ?, ?, ?, ?, ?, datetime("now"), ?, ?)''',
                    (info['team1'], info['team2'], info['score1'], info['score2'],
                     info['winner'], info['event'], info['match_date'],
                     info['event_tier'], info['is_lan']))
                if c.rowcount > 0:
                    new_matches += 1

            print(f"  Page {page}: {len(matches)} matches fetched, {new_matches} new so far")
            time.sleep(0.5)

        except Exception as e:
            print(f"  Page {page}: error - {e}")
            break

    conn.commit()
    conn.close()

    print(f"\nBackfill done: {total_fetched} fetched, {new_matches} new matches added")
    return new_matches


# ============================================================
# STEP 2: PLAYER STATS FROM MATCH DATA
# ============================================================

def fetch_player_performance():
    """
    Fetch per-match player stats from PandaScore for each known team.
    Computes KD ratio and approximate rating, then updates players table.
    """
    print("\nFetching player performance stats...\n")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Collect stats per player: {name: {'kills': [], 'deaths': [], 'assists': [], 'team': str}}
    player_stats = {}

    for team_name, team_id in TEAM_IDS.items():
        try:
            r = requests.get(f'{BASE_URL}/csgo/matches/past', headers=headers,
                             params={
                                 'filter[opponent_id]': team_id,
                                 'per_page': 10,
                                 'sort': '-end_at',
                             },
                             timeout=15)
            if r.status_code != 200:
                print(f"  {team_name:20} HTTP {r.status_code}")
                time.sleep(0.5)
                continue

            matches = r.json()
            match_count = 0

            for match in matches:
                games = match.get('games', [])
                for game in games:
                    players = game.get('players', [])
                    for p in players:
                        name = p.get('name', '')
                        if not name:
                            continue

                        kills = p.get('kills', 0) or 0
                        deaths = p.get('deaths', 0) or 0
                        assists = p.get('assists', 0) or 0

                        if name not in player_stats:
                            player_stats[name] = {
                                'kills': [], 'deaths': [], 'assists': [],
                                'team': p.get('team', {}).get('name', team_name) if isinstance(p.get('team'), dict) else team_name
                            }

                        player_stats[name]['kills'].append(kills)
                        player_stats[name]['deaths'].append(deaths)
                        player_stats[name]['assists'].append(assists)
                        match_count += 1

            print(f"  {team_name:20} {len(matches)} matches, {match_count} player entries")
            time.sleep(0.5)

        except Exception as e:
            print(f"  {team_name:20} error - {e}")
            continue

    # Calculate ratings and update DB
    updated = 0
    for name, stats in player_stats.items():
        if not stats['kills']:
            continue

        avg_kills = sum(stats['kills']) / len(stats['kills'])
        avg_deaths = sum(stats['deaths']) / len(stats['deaths'])
        avg_assists = sum(stats['assists']) / len(stats['assists'])

        kd_ratio = avg_kills / max(avg_deaths, 1)

        # Approximate HLTV-style rating: based on KD + impact
        # Real HLTV rating uses KAST, ADR, etc. but this is a reasonable proxy
        rating = 0.5 + (kd_ratio - 1.0) * 0.5 + (avg_kills / 25) * 0.3
        rating = max(0.5, min(rating, 2.0))

        # ADR proxy: avg_kills * ~75 damage per kill / ~24 rounds
        adr_proxy = (avg_kills * 75) / 24
        adr_proxy = max(40, min(adr_proxy, 120))

        # KAST proxy: based on KD and assists
        kast_proxy = min(85, 50 + kd_ratio * 15 + (avg_assists / max(avg_kills, 1)) * 10)

        maps_played = len(stats['kills'])

        c.execute('''UPDATE players SET rating = ?, adr = ?, kast = ?,
                     maps_played = ?, updated_at = datetime("now")
                     WHERE name = ?''',
                  (round(rating, 2), round(adr_proxy, 1), round(kast_proxy, 1),
                   maps_played, name))

        if c.rowcount == 0:
            # Player not in DB yet, insert
            c.execute('''INSERT OR IGNORE INTO players (name, team, rating, adr, kast, maps_played, updated_at)
                         VALUES (?, ?, ?, ?, ?, ?, datetime("now"))''',
                      (name, stats['team'], round(rating, 2), round(adr_proxy, 1),
                       round(kast_proxy, 1), maps_played))

        if c.rowcount > 0:
            updated += 1

    conn.commit()
    conn.close()
    print(f"\nUpdated stats for {updated} players")
    return updated


# ============================================================
# STEP 5: ROSTER CHANGE TRACKING
# ============================================================

def detect_roster_changes():
    """
    Compare current PandaScore rosters against stored rosters.
    If players differ, log changes to roster_history table.
    """
    print("\nChecking for roster changes...\n")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    changes_found = 0

    for team_name, team_id in TEAM_IDS.items():
        data = fetch_team(team_id)
        if not data:
            continue

        current_players = set(p['name'] for p in data.get('players', []))

        # Get stored roster
        c.execute('SELECT roster FROM team_rankings WHERE team LIKE ?', (f'%{team_name}%',))
        row = c.fetchone()
        if not row or not row[0]:
            continue

        stored_players = set(json.loads(row[0]))

        # Find diffs
        players_in = current_players - stored_players
        players_out = stored_players - current_players

        for p_in in players_in:
            c.execute('''INSERT INTO roster_history (team, player_in, player_out, change_date, change_type)
                         VALUES (?, ?, NULL, date("now"), 'joined')''',
                      (team_name, p_in))
            changes_found += 1
            print(f"  {team_name}: +{p_in}")

        for p_out in players_out:
            c.execute('''INSERT INTO roster_history (team, player_in, player_out, change_date, change_type)
                         VALUES (?, NULL, ?, date("now"), 'left')''',
                      (team_name, p_out))
            changes_found += 1
            print(f"  {team_name}: -{p_out}")

        # If we have matched pairs (same count in/out), link them
        if len(players_in) == len(players_out) and players_in:
            for p_in, p_out in zip(sorted(players_in), sorted(players_out)):
                c.execute('''INSERT INTO roster_history (team, player_in, player_out, change_date, change_type)
                             VALUES (?, ?, ?, date("now"), 'swap')''',
                          (team_name, p_in, p_out))

        time.sleep(0.3)

    conn.commit()
    conn.close()

    if changes_found:
        print(f"\nDetected {changes_found} roster changes")
    else:
        print("  No roster changes detected")
    return changes_found


# ============================================================
# ORIGINAL FUNCTIONS (updated)
# ============================================================

def fetch_team(team_id):
    """Fetch team data by ID"""
    r = requests.get(f'{BASE_URL}/teams/{team_id}', headers=headers, timeout=15)
    if r.status_code == 200:
        return r.json()
    return None


def fetch_all_teams():
    """Fetch rosters for all known teams"""
    print("Fetching team rosters from PandaScore...\n")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    updated = 0
    for team_name, team_id in TEAM_IDS.items():
        data = fetch_team(team_id)
        if not data:
            print(f"  {team_name:20} FAILED")
            continue

        players = data.get('players', [])
        player_names = [p['name'] for p in players]

        actual_name = data.get('name', team_name)

        print(f"  {actual_name:20} -> {', '.join(player_names[:5])}")

        # Update team_rankings roster
        c.execute('SELECT team FROM team_rankings WHERE team LIKE ?', (f'%{team_name}%',))
        row = c.fetchone()
        if row:
            c.execute('UPDATE team_rankings SET roster = ?, updated_at = datetime("now") WHERE team = ?',
                      (json.dumps(player_names), row[0]))
        else:
            c.execute('''INSERT OR REPLACE INTO team_rankings (team, world_rank, points, roster, updated_at)
                         VALUES (?, 99, 0, ?, datetime("now"))''',
                      (actual_name, json.dumps(player_names)))

        # Update players table
        for p in players:
            c.execute('''INSERT OR REPLACE INTO players (name, team, updated_at)
                         VALUES (?, ?, datetime("now"))
                         ''', (p['name'], actual_name))

        updated += 1
        time.sleep(0.5)

    conn.commit()
    conn.close()
    print(f"\nUpdated {updated} teams")
    return updated


def verify_data():
    """Show current data state"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    print("\n" + "="*60)
    print("CURRENT DATA STATE")
    print("="*60)

    print("\nTOP 10 TEAMS (with updated rosters):")
    c.execute('SELECT team, world_rank, roster FROM team_rankings ORDER BY world_rank LIMIT 10')
    for team, rank, roster in c.fetchall():
        players = json.loads(roster) if roster else []
        print(f"  #{rank:2} {team:20} -> {', '.join(players[:5])}")

    c.execute('SELECT COUNT(*) FROM team_rankings')
    teams = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM players')
    players = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM players WHERE rating IS NOT NULL')
    rated = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM matches')
    matches = c.fetchone()[0]

    print(f"\nTOTALS: {teams} teams, {players} players ({rated} with ratings), {matches} matches")

    # Show top rated players
    c.execute('SELECT name, team, rating, adr, kast FROM players WHERE rating IS NOT NULL ORDER BY rating DESC LIMIT 10')
    rows = c.fetchall()
    if rows:
        print("\nTOP 10 RATED PLAYERS:")
        for name, team, rating, adr, kast in rows:
            adr_s = f" ADR:{adr:.0f}" if adr else ""
            kast_s = f" KAST:{kast:.0f}%" if kast else ""
            print(f"  {name:15} ({team:15}) rating:{rating:.2f}{adr_s}{kast_s}")

    conn.close()


if __name__ == '__main__':
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == 'backfill':
        backfill_matches()
    elif len(sys.argv) > 1 and sys.argv[1] == 'players':
        fetch_player_performance()
    elif len(sys.argv) > 1 and sys.argv[1] == 'roster-diff':
        detect_roster_changes()
    else:
        # Full update
        fetch_all_teams()
        detect_roster_changes()
        backfill_matches()
        fetch_player_performance()

    verify_data()
