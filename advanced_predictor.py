#!/usr/bin/env python3
"""
Advanced CS2 Match Predictor v3
Includes: ELO ratings, Map pools, Match format, Weighted recency, Roster stability
Dynamic weight redistribution for missing data
"""

import sqlite3
import json
import os
import math
from datetime import datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cs2_data.db')

# Top-tier event filter — keeps majors, tier1 (IEM/PGL/ESL Pro/BLAST), tier2 (CCT/BetBoom/etc)
# Excludes ~73% of the data (qualifier-tier garbage between unranked teams)
TOP_TIERS = ('major', 'tier1', 'tier2')
TOP_TIER_SQL = "event_tier IN ('major', 'tier1', 'tier2')"

# ============================================================
# DATABASE SETUP
# ============================================================

def init_advanced_db():
    """Create all tables for advanced prediction"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute('''
        CREATE TABLE IF NOT EXISTS team_elo (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team TEXT UNIQUE,
            elo_rating REAL DEFAULT 1500,
            peak_elo REAL DEFAULT 1500,
            matches_played INTEGER DEFAULT 0,
            last_updated TEXT
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS map_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team TEXT,
            map_name TEXT,
            times_played INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            win_rate REAL DEFAULT 0.5,
            ct_rounds_won INTEGER DEFAULT 0,
            t_rounds_won INTEGER DEFAULT 0,
            updated_at TEXT,
            UNIQUE(team, map_name)
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS roster_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team TEXT,
            player_in TEXT,
            player_out TEXT,
            change_date TEXT,
            change_type TEXT
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS match_extended (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team1 TEXT,
            team2 TEXT,
            team1_score INTEGER,
            team2_score INTEGER,
            winner TEXT,
            match_format TEXT,
            event TEXT,
            event_type TEXT,
            maps_played TEXT,
            match_date TEXT,
            UNIQUE(team1, team2, match_date, event)
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS betting_odds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team1 TEXT,
            team2 TEXT,
            team1_odds REAL,
            team2_odds REAL,
            bookmaker TEXT,
            match_date TEXT,
            fetched_at TEXT
        )
    ''')

    conn.commit()
    conn.close()
    print("Advanced database tables initialized")

# ============================================================
# ELO RATING SYSTEM (Step 4: seeded + tier-weighted)
# ============================================================

# Tier multipliers for K factor
TIER_K_MULTIPLIERS = {
    'major': 1.5,
    'tier1': 1.2,
    'tier2': 1.0,
    'qualifier': 0.7,
    None: 0.8,
}

class EloSystem:
    """ELO rating system for CS2 teams"""

    K_FACTOR = 32

    @staticmethod
    def expected_score(rating_a, rating_b):
        return 1 / (1 + 10 ** ((rating_b - rating_a) / 400))

    @staticmethod
    def update_ratings(winner_elo, loser_elo, score_diff=1, event_tier=None):
        """Update ELO ratings after a match, weighted by event tier"""
        expected_winner = EloSystem.expected_score(winner_elo, loser_elo)
        expected_loser = EloSystem.expected_score(loser_elo, winner_elo)

        tier_mult = TIER_K_MULTIPLIERS.get(event_tier, 0.8)
        k = EloSystem.K_FACTOR * (1 + score_diff * 0.1) * tier_mult

        new_winner_elo = winner_elo + k * (1 - expected_winner)
        new_loser_elo = loser_elo + k * (0 - expected_loser)

        return new_winner_elo, new_loser_elo

    @staticmethod
    def get_team_elo(team_name):
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('SELECT elo_rating FROM team_elo WHERE team LIKE ?', (f'%{team_name}%',))
        row = c.fetchone()
        conn.close()
        return row[0] if row else 1500

    @staticmethod
    def set_team_elo(team_name, elo):
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''
            INSERT OR REPLACE INTO team_elo (team, elo_rating, last_updated)
            VALUES (?, ?, datetime('now'))
        ''', (team_name, elo))
        conn.commit()
        conn.close()


def calculate_all_elo():
    """Calculate ELO ratings from match history, seeded from world rankings"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute('DELETE FROM team_elo')

    # Step 4: Seed ELO from world rankings
    # #1 starts ~1800, #30 starts ~1510, unranked = 1500
    seed_elos = {}
    c.execute('SELECT team, world_rank FROM team_rankings WHERE world_rank IS NOT NULL')
    for team, rank in c.fetchall():
        if rank <= 30:
            seed_elos[team] = 1500 + (30 - rank) * 10  # #1=1790, #30=1500
        else:
            seed_elos[team] = 1500

    # Get top-tier matches ordered by date
    c.execute(f'''
        SELECT team1, team2, score1, score2, winner, match_date, event_tier
        FROM matches
        WHERE {TOP_TIER_SQL}
        ORDER BY match_date ASC, id ASC
    ''')
    matches = c.fetchall()

    elo_ratings = {}

    for team1, team2, s1, s2, winner, date, event_tier in matches:
        # Initialize with seed if available, else 1500
        if team1 not in elo_ratings:
            elo_ratings[team1] = seed_elos.get(team1, 1500)
        if team2 not in elo_ratings:
            elo_ratings[team2] = seed_elos.get(team2, 1500)

        if s1 is None or s2 is None:
            continue

        if s1 > s2:
            winner_team, loser_team = team1, team2
            score_diff = s1 - s2
        elif s2 > s1:
            winner_team, loser_team = team2, team1
            score_diff = s2 - s1
        else:
            continue  # skip draws

        new_winner, new_loser = EloSystem.update_ratings(
            elo_ratings[winner_team],
            elo_ratings[loser_team],
            score_diff,
            event_tier
        )

        elo_ratings[winner_team] = new_winner
        elo_ratings[loser_team] = new_loser

    # Save to database
    for team, elo in elo_ratings.items():
        matches_played = sum(1 for m in matches if team in (m[0], m[1]))
        c.execute('''
            INSERT OR REPLACE INTO team_elo (team, elo_rating, matches_played, last_updated)
            VALUES (?, ?, ?, datetime('now'))
        ''', (team, elo, matches_played))

    conn.commit()
    conn.close()

    print(f"Calculated ELO for {len(elo_ratings)} teams (seeded from world rankings)")
    return elo_ratings

# ============================================================
# MAP POOL ANALYSIS
# ============================================================

DEFAULT_MAP_POOLS = {
    'Vitality': {'Nuke': 0.75, 'Inferno': 0.68, 'Mirage': 0.62, 'Anubis': 0.58, 'Ancient': 0.55, 'Dust2': 0.50, 'Overpass': 0.45},
    'Spirit': {'Inferno': 0.72, 'Mirage': 0.70, 'Nuke': 0.65, 'Anubis': 0.60, 'Ancient': 0.58, 'Dust2': 0.52, 'Overpass': 0.48},
    'FURIA': {'Mirage': 0.70, 'Inferno': 0.65, 'Overpass': 0.63, 'Nuke': 0.55, 'Anubis': 0.52, 'Ancient': 0.50, 'Dust2': 0.48},
    'MOUZ': {'Inferno': 0.68, 'Nuke': 0.65, 'Anubis': 0.62, 'Mirage': 0.58, 'Ancient': 0.55, 'Dust2': 0.50, 'Overpass': 0.48},
    'Falcons': {'Mirage': 0.72, 'Inferno': 0.68, 'Dust2': 0.65, 'Anubis': 0.58, 'Nuke': 0.52, 'Ancient': 0.50, 'Overpass': 0.45},
    'NaVi': {'Inferno': 0.65, 'Mirage': 0.63, 'Nuke': 0.60, 'Anubis': 0.58, 'Ancient': 0.55, 'Dust2': 0.52, 'Overpass': 0.48},
    'Natus Vincere': {'Inferno': 0.65, 'Mirage': 0.63, 'Nuke': 0.60, 'Anubis': 0.58, 'Ancient': 0.55, 'Dust2': 0.52, 'Overpass': 0.48},
    'FaZe': {'Mirage': 0.68, 'Inferno': 0.65, 'Nuke': 0.62, 'Anubis': 0.58, 'Ancient': 0.52, 'Dust2': 0.50, 'Overpass': 0.45},
    'G2': {'Inferno': 0.70, 'Mirage': 0.65, 'Anubis': 0.60, 'Nuke': 0.55, 'Ancient': 0.52, 'Dust2': 0.50, 'Overpass': 0.45},
    'Liquid': {'Mirage': 0.60, 'Inferno': 0.58, 'Nuke': 0.55, 'Overpass': 0.52, 'Anubis': 0.50, 'Ancient': 0.48, 'Dust2': 0.45},
    'Astralis': {'Nuke': 0.70, 'Inferno': 0.62, 'Mirage': 0.58, 'Anubis': 0.55, 'Overpass': 0.52, 'Ancient': 0.50, 'Dust2': 0.48},
    'The MongolZ': {'Mirage': 0.72, 'Inferno': 0.65, 'Anubis': 0.60, 'Dust2': 0.58, 'Nuke': 0.52, 'Ancient': 0.50, 'Overpass': 0.45},
}

# Generic fallback values — used to detect "no real data"
_GENERIC_MAP_POOL = {'Mirage': 0.50, 'Inferno': 0.50, 'Nuke': 0.50, 'Anubis': 0.50, 'Ancient': 0.50, 'Dust2': 0.50, 'Overpass': 0.50}


def get_map_pool(team_name):
    """Get map pool stats for a team. Returns (pool_dict, is_real_data)"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute('SELECT map_name, win_rate FROM team_maps WHERE team LIKE ?', (f'%{team_name}%',))
    rows = c.fetchall()

    if not rows:
        c.execute('SELECT map_name, win_rate FROM map_stats WHERE team LIKE ?', (f'%{team_name}%',))
        rows = c.fetchall()

    conn.close()

    if rows:
        return {row[0]: row[1] for row in rows}, True

    # Fall back to hardcoded data
    for key in DEFAULT_MAP_POOLS:
        if team_name.lower() in key.lower() or key.lower() in team_name.lower():
            return DEFAULT_MAP_POOLS[key], True

    return dict(_GENERIC_MAP_POOL), False


def calculate_map_advantage(team1, team2):
    """Calculate map pool advantage. Returns (overall_diff, map_details, has_real_data)"""
    pool1, real1 = get_map_pool(team1)
    pool2, real2 = get_map_pool(team2)

    common_maps = set(pool1.keys()) & set(pool2.keys())
    if not common_maps:
        return 0, {}, False

    advantages = {}
    for map_name in common_maps:
        diff = pool1.get(map_name, 0.5) - pool2.get(map_name, 0.5)
        advantages[map_name] = diff

    t1_best = sorted(pool1.values(), reverse=True)[:3]
    t2_best = sorted(pool2.values(), reverse=True)[:3]
    overall = (sum(t1_best) / 3) - (sum(t2_best) / 3)

    # If one team has real data and the other doesn't, flag unreliable
    has_real_data = real1 and real2

    return overall, advantages, has_real_data

# ============================================================
# WEIGHTED RECENCY
# ============================================================

TIER_WEIGHTS = {'major': 1.5, 'tier1': 1.2, 'tier2': 1.0, 'qualifier': 0.7, None: 0.8}

def get_weighted_form(team_name, days=90):
    """Get recent form with recency + event tier weighting"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute(f'''
        SELECT team1, team2, score1, score2, winner, match_date, event_tier
        FROM matches
        WHERE (team1 LIKE ? OR team2 LIKE ?) AND {TOP_TIER_SQL}
        ORDER BY id DESC
        LIMIT 20
    ''', (f'%{team_name}%', f'%{team_name}%'))

    matches = c.fetchall()
    conn.close()

    if not matches:
        return 0.5, []

    weighted_wins = 0
    total_weight = 0
    form = []

    for i, row in enumerate(matches):
        t1, t2, s1, s2, winner, date = row[0], row[1], row[2], row[3], row[4], row[5]
        event_tier = row[6] if len(row) > 6 else None

        recency = 1.0 - (i * 0.025)
        recency = max(recency, 0.5)

        tier_w = TIER_WEIGHTS.get(event_tier, 0.8)

        weight = recency * tier_w
        is_win = team_name.lower() in winner.lower() if winner else False

        weighted_wins += weight if is_win else 0
        total_weight += weight
        form.append('W' if is_win else 'L')

    win_rate = weighted_wins / total_weight if total_weight > 0 else 0.5
    return win_rate, form[:10]


def get_form_with_opponent_strength(team_name, days=90):
    """Get recent form weighted by opponent strength (ELO-based)"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute(f'''
        SELECT team1, team2, score1, score2, winner, match_date, event_tier
        FROM matches
        WHERE (team1 LIKE ? OR team2 LIKE ?) AND {TOP_TIER_SQL}
        ORDER BY id DESC
        LIMIT 20
    ''', (f'%{team_name}%', f'%{team_name}%'))

    matches = c.fetchall()

    if not matches:
        conn.close()
        return 0.5, [], 0, 1500

    weighted_wins = 0
    total_weight = 0
    form = []
    opp_elos = []

    for i, row in enumerate(matches):
        t1, t2, s1, s2, winner, date = row[0], row[1], row[2], row[3], row[4], row[5]
        event_tier = row[6] if len(row) > 6 else None

        # Identify opponent
        if team_name.lower() in t1.lower():
            opponent = t2
        else:
            opponent = t1

        # Look up opponent ELO
        c.execute('SELECT elo_rating FROM team_elo WHERE team LIKE ?', (f'%{opponent}%',))
        opp_row = c.fetchone()
        opp_elo = opp_row[0] if opp_row else 1500
        opp_elos.append(opp_elo)

        # Strength multiplier: beating strong teams counts more
        strength_mult = max(0.7, min(opp_elo / 1500, 1.3))

        recency = max(1.0 - (i * 0.025), 0.5)
        tier_w = TIER_WEIGHTS.get(event_tier, 0.8)

        weight = recency * tier_w * strength_mult
        is_win = team_name.lower() in winner.lower() if winner else False

        weighted_wins += weight if is_win else 0
        total_weight += weight
        form.append('W' if is_win else 'L')

    conn.close()

    win_rate = weighted_wins / total_weight if total_weight > 0 else 0.5
    match_count = len(matches)
    avg_opp_elo = sum(opp_elos) / len(opp_elos) if opp_elos else 1500

    return win_rate, form[:10], match_count, avg_opp_elo


# ============================================================
# MATCH FORMAT ANALYSIS
# ============================================================

def get_format_adjustment(match_format):
    adjustments = {
        'bo1': 0.85,
        'bo3': 1.0,
        'bo5': 1.15,
    }
    return adjustments.get(match_format.lower(), 1.0)

# ============================================================
# ROSTER STABILITY (Step 5: uses roster_history table)
# ============================================================

def get_lan_factor(team_name):
    """Get team's LAN vs online performance difference"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(f'''
        SELECT is_lan, winner FROM matches
        WHERE (team1 LIKE ? OR team2 LIKE ?)
          AND is_lan IS NOT NULL AND {TOP_TIER_SQL}
    ''', (f'%{team_name}%', f'%{team_name}%'))

    lan_wins, lan_total, online_wins, online_total = 0, 0, 0, 0
    for is_lan, winner in c.fetchall():
        if is_lan:
            lan_total += 1
            if team_name.lower() in (winner or '').lower():
                lan_wins += 1
        else:
            online_total += 1
            if team_name.lower() in (winner or '').lower():
                online_wins += 1
    conn.close()

    if lan_total < 3 or online_total < 3:
        return 1.0, None, None

    lan_wr = lan_wins / lan_total
    online_wr = online_wins / online_total
    factor = 1.0 + (lan_wr - online_wr) * 0.5
    return factor, lan_wr, online_wr


def get_roster_stability(team_name):
    """
    Check roster_history table first (reliable, from roster diffs),
    then fall back to roster_changes news table.
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Primary: check roster_history (populated by detect_roster_changes)
    c.execute('''
        SELECT change_date FROM roster_history
        WHERE team LIKE ?
        ORDER BY change_date DESC
        LIMIT 1
    ''', (f'%{team_name}%',))
    row = c.fetchone()

    if not row:
        # Fallback: check roster_changes news table
        c.execute('''
            SELECT fetched_at FROM roster_changes
            WHERE title LIKE ? OR to_team LIKE ?
            ORDER BY fetched_at DESC
            LIMIT 1
        ''', (f'%{team_name}%', f'%{team_name}%'))
        row = c.fetchone()

    conn.close()

    if not row:
        return 1.0

    try:
        date_str = row[0]
        change_date = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        days_since = (datetime.now() - change_date.replace(tzinfo=None)).days

        if days_since < 14:
            return 0.7
        elif days_since < 30:
            return 0.85
        elif days_since < 60:
            return 0.95
        else:
            return 1.0
    except:
        return 0.9

# ============================================================
# MAIN PREDICTION ENGINE (Step 3: dynamic weight redistribution)
# ============================================================

def get_all_team_data(team_name):
    """Gather all data for a team"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    data = {
        'name': team_name,
        'elo': 1500,
        'world_rank': None,
        'points': 0,
        'roster': [],
        'players': [],
        'avg_player_rating': None,  # None = no data (was 1.0)
        'map_pool': {},
        'map_pool_is_real': False,
        'weighted_form': 0.5,
        'recent_form': [],
        'roster_stability': 1.0,
        'wins': 0,
        'losses': 0
    }

    # ELO
    c.execute('SELECT elo_rating FROM team_elo WHERE team LIKE ?', (f'%{team_name}%',))
    row = c.fetchone()
    if row:
        data['elo'] = row[0]

    # World ranking
    c.execute('SELECT world_rank, points, roster FROM team_rankings WHERE team LIKE ?', (f'%{team_name}%',))
    row = c.fetchone()
    if row:
        data['world_rank'] = row[0]
        data['points'] = row[1]
        data['roster'] = json.loads(row[2]) if row[2] else []

    # Player stats
    for player in data['roster'][:5]:
        c.execute('SELECT name, rating, role, adr, kast FROM players WHERE name LIKE ?', (f'%{player}%',))
        prow = c.fetchone()
        if prow:
            data['players'].append({
                'name': prow[0], 'rating': prow[1], 'role': prow[2],
                'adr': prow[3], 'kast': prow[4]
            })

    # Step 3 fix: compute avg rating, set to None if no players have ratings
    if data['players']:
        scores = []
        for p in data['players']:
            if p['rating'] is None:
                continue
            score = p['rating'] * 0.5
            if p.get('adr'):
                score += (p['adr'] / 100) * 0.3
            else:
                score += p['rating'] * 0.3
            if p.get('kast'):
                score += (p['kast'] / 100) * 0.2
            else:
                score += p['rating'] * 0.2
            scores.append(score)
        data['avg_player_rating'] = sum(scores) / len(scores) if scores else None

    # Map pool
    data['map_pool'], data['map_pool_is_real'] = get_map_pool(team_name)

    # Weighted form (opponent-strength adjusted)
    data['weighted_form'], data['recent_form'], data['form_match_count'], data['avg_opponent_elo'] = get_form_with_opponent_strength(team_name)

    # Win/loss (top-tier only)
    c.execute(f'''
        SELECT winner FROM matches
        WHERE (team1 LIKE ? OR team2 LIKE ?) AND {TOP_TIER_SQL}
    ''', (f'%{team_name}%', f'%{team_name}%'))
    for (winner,) in c.fetchall():
        if winner and team_name.lower() in winner.lower():
            data['wins'] += 1
        else:
            data['losses'] += 1

    # Roster stability
    data['roster_stability'] = get_roster_stability(team_name)

    # LAN performance
    data['lan_factor'], data['lan_wr'], data['online_wr'] = get_lan_factor(team_name)

    conn.close()
    return data


def get_head_to_head(team1, team2):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute(f'''
        SELECT team1, score1, score2, team2, winner, event
        FROM matches
        WHERE ((team1 LIKE ? AND team2 LIKE ?)
            OR (team1 LIKE ? AND team2 LIKE ?))
          AND {TOP_TIER_SQL}
        ORDER BY id DESC
    ''', (f'%{team1}%', f'%{team2}%', f'%{team2}%', f'%{team1}%'))

    matches = c.fetchall()
    conn.close()

    t1_wins = sum(1 for m in matches if team1.lower() in (m[4] or '').lower())
    t2_wins = len(matches) - t1_wins

    # Closeness from map scores
    closeness_values = []
    close_matches = 0
    for m in matches:
        s1, s2 = m[1], m[2]
        if s1 is not None and s2 is not None and (s1 + s2) > 0:
            winner_maps = max(s1, s2)
            loser_maps = min(s1, s2)
            c_val = loser_maps / winner_maps if winner_maps > 0 else 0.0
            closeness_values.append(c_val)
            if c_val > 0.3:
                close_matches += 1

    avg_closeness = sum(closeness_values) / len(closeness_values) if closeness_values else 0.0
    total = len(matches)

    return {
        'matches': matches,
        't1_wins': t1_wins,
        't2_wins': t2_wins,
        'total': total,
        'avg_closeness': avg_closeness,
        'close_matches': close_matches,
    }


def predict_match(team1, team2, match_format='bo3'):
    """
    Advanced match prediction with dynamic weight redistribution.
    Factors that lack data for either team get weight 0, redistributed to others.
    """
    print(f"\n{'='*70}")
    print(f"  ADVANCED MATCH PREDICTION v3")
    print(f"  {team1} vs {team2} ({match_format.upper()})")
    print(f"{'='*70}")

    t1 = get_all_team_data(team1)
    t2 = get_all_team_data(team2)
    h2h = get_head_to_head(team1, team2)
    map_adv, map_details, map_data_reliable = calculate_map_advantage(team1, team2)

    # ============================================================
    # Step 3: Dynamic weight redistribution
    # Define all factors with their base weights and whether data exists
    # ============================================================

    # Each factor: (name, base_weight, has_data)
    factor_defs = []

    # Check if ELO is real vs default 1500
    t1_has_elo = t1['elo'] != 1500
    t2_has_elo = t2['elo'] != 1500
    both_have_elo = t1_has_elo and t2_has_elo

    # 1. ELO - reduced weight if one team has no real ELO
    factor_defs.append(('elo', 25 if both_have_elo else 10, True))

    # 2. World Ranking - boosted weight for large rank gaps
    has_ranking = bool(t1['world_rank'] and t2['world_rank'])
    rank_weight = 15
    rank_cap = 12
    if has_ranking:
        rank_gap = abs(t1['world_rank'] - t2['world_rank'])
        if rank_gap >= 20:
            rank_weight = 35
            rank_cap = 25
        elif rank_gap >= 10:
            rank_weight = 25
            rank_cap = 18
    factor_defs.append(('ranking', rank_weight, has_ranking))

    # 3. Player Ratings (20%) - needs both teams to have real ratings
    has_player_data = (t1['avg_player_rating'] is not None and t2['avg_player_rating'] is not None)
    factor_defs.append(('player_ratings', 20, has_player_data))

    # 4. Map Pool (15%) - reduced if data unreliable
    factor_defs.append(('map_pool', 15 if map_data_reliable else 5, map_data_reliable or True))

    # 5. Form - weight scaled by confidence (how many matches it's based on)
    has_form = bool(t1['recent_form'] or t2['recent_form'])
    t1_conf = min(t1.get('form_match_count', 0) / 8, 1.0)
    t2_conf = min(t2.get('form_match_count', 0) / 8, 1.0)
    form_confidence = min(t1_conf, t2_conf)
    effective_form_weight = max(3, int(15 * (0.2 + 0.8 * form_confidence)))
    factor_defs.append(('form', effective_form_weight, has_form))

    # 6. H2H - dynamic weight by match count, needs previous matchups
    h2h_count = h2h['total']
    if h2h_count >= 4:
        h2h_base_weight = 15
    elif h2h_count >= 2:
        h2h_base_weight = 10
    else:
        h2h_base_weight = 5
    factor_defs.append(('h2h', h2h_base_weight, bool(h2h['matches'])))

    # 7. Roster Stability (5%)
    factor_defs.append(('stability', 5, True))

    # 8. LAN (5%) - needs LAN data for both
    has_lan = (t1['lan_wr'] is not None and t2['lan_wr'] is not None)
    factor_defs.append(('lan', 5, has_lan))

    # Redistribute weights from missing factors to present ones
    total_active_base = sum(w for _, w, has in factor_defs if has)
    total_all_base = sum(w for _, w, _ in factor_defs)

    if total_active_base > 0:
        scale = total_all_base / total_active_base
    else:
        scale = 1.0

    active_weights = {}
    for name, base_w, has_data in factor_defs:
        if has_data:
            active_weights[name] = base_w * scale
        else:
            active_weights[name] = 0

    # ============================================================
    # CALCULATE PREDICTION SCORES
    # ============================================================

    scores = {'team1': 50, 'team2': 50}
    factors = {}
    skipped = []

    # 1. ELO Rating
    if active_weights.get('elo', 0) > 0:
        elo_diff = t1['elo'] - t2['elo']
        w = active_weights['elo'] / 25  # normalize to base
        elo_factor = (elo_diff / 20) * w
        elo_factor = max(min(elo_factor, 15), -15)
        scores['team1'] += elo_factor
        scores['team2'] -= elo_factor
        factors['elo'] = {
            'description': f"ELO: {t1['elo']:.0f} vs {t2['elo']:.0f}",
            'advantage': team1 if elo_diff > 0 else team2,
            'impact': abs(elo_factor)
        }

    # 2. World Ranking
    if active_weights.get('ranking', 0) > 0:
        rank_diff = t2['world_rank'] - t1['world_rank']
        w = active_weights['ranking'] / 15
        rank_factor = rank_diff * 0.8 * w
        rank_factor = max(min(rank_factor, rank_cap), -rank_cap)
        scores['team1'] += rank_factor
        scores['team2'] -= rank_factor
        factors['ranking'] = {
            'description': f"World Rank: #{t1['world_rank']} vs #{t2['world_rank']}",
            'advantage': team1 if rank_diff > 0 else team2,
            'impact': abs(rank_factor)
        }
    else:
        skipped.append('World Ranking (missing for one/both teams)')

    # 3. Player Ratings
    if active_weights.get('player_ratings', 0) > 0:
        rating_diff = (t1['avg_player_rating'] - t2['avg_player_rating']) * 25
        w = active_weights['player_ratings'] / 20
        rating_factor = rating_diff * w
        rating_factor = max(min(rating_factor, 12), -12)
        scores['team1'] += rating_factor
        scores['team2'] -= rating_factor
        factors['player_ratings'] = {
            'description': f"Avg Rating: {t1['avg_player_rating']:.2f} vs {t2['avg_player_rating']:.2f}",
            'advantage': team1 if rating_diff > 0 else team2,
            'impact': abs(rating_factor)
        }
    else:
        skipped.append('Player Ratings (no stats for one/both teams)')

    # 4. Map Pool
    if active_weights.get('map_pool', 0) > 0:
        w = active_weights['map_pool'] / 15
        map_factor = map_adv * 20 * w
        map_factor = max(min(map_factor, 10), -10)
        scores['team1'] += map_factor
        scores['team2'] -= map_factor
        reliability = "" if map_data_reliable else " [low confidence]"
        factors['map_pool'] = {
            'description': f"Map Pool: {map_adv*100:+.1f}%{reliability}",
            'advantage': team1 if map_adv > 0 else team2,
            'impact': abs(map_factor)
        }

    # 5. Recent Form
    if active_weights.get('form', 0) > 0:
        form_diff = (t1['weighted_form'] - t2['weighted_form']) * 20
        w = active_weights['form'] / 15
        form_factor = form_diff * w
        form_factor = max(min(form_factor, 10), -10)
        scores['team1'] += form_factor
        scores['team2'] -= form_factor
        factors['form'] = {
            'description': f"Form: {t1['weighted_form']*100:.0f}% vs {t2['weighted_form']*100:.0f}% (weighted)",
            'advantage': team1 if form_diff > 0 else team2,
            'impact': abs(form_factor)
        }
    else:
        skipped.append('Recent Form (no match data)')

    # 6. Head-to-Head (with closeness dampening)
    if active_weights.get('h2h', 0) > 0:
        h2h_diff = (h2h['t1_wins'] - h2h['t2_wins']) * 3
        w = active_weights['h2h'] / h2h_base_weight
        h2h_factor = h2h_diff * w
        # Dampen when matches are close (2-1 results)
        if h2h['avg_closeness'] > 0.3:
            dampen = 1.0 - (h2h['avg_closeness'] * 0.8)
            h2h_factor *= dampen
        h2h_factor = max(min(h2h_factor, 8), -8)
        scores['team1'] += h2h_factor
        scores['team2'] -= h2h_factor
        factors['h2h'] = {
            'description': f"H2H: {h2h['t1_wins']}-{h2h['t2_wins']} (closeness: {h2h['avg_closeness']:.2f})",
            'advantage': team1 if h2h_diff > 0 else team2,
            'impact': abs(h2h_factor)
        }

    # 7. Roster Stability
    if active_weights.get('stability', 0) > 0:
        stability_diff = (t1['roster_stability'] - t2['roster_stability']) * 10
        w = active_weights['stability'] / 5
        stability_factor = stability_diff * w
        stability_factor = max(min(stability_factor, 5), -5)
        scores['team1'] += stability_factor
        scores['team2'] -= stability_factor
        factors['stability'] = {
            'description': f"Roster Stability: {t1['roster_stability']*100:.0f}% vs {t2['roster_stability']*100:.0f}%",
            'advantage': team1 if stability_diff > 0 else team2,
            'impact': abs(stability_factor)
        }

    # 8. LAN Performance
    if active_weights.get('lan', 0) > 0:
        lan_diff = (t1['lan_factor'] - t2['lan_factor']) * 8
        w = active_weights['lan'] / 5
        lan_factor_val = lan_diff * w
        lan_factor_val = max(min(lan_factor_val, 5), -5)
        scores['team1'] += lan_factor_val
        scores['team2'] -= lan_factor_val
        lan_desc1 = f"LAN:{t1['lan_wr']*100:.0f}%/Online:{t1['online_wr']*100:.0f}%" if t1['lan_wr'] else "N/A"
        lan_desc2 = f"LAN:{t2['lan_wr']*100:.0f}%/Online:{t2['online_wr']*100:.0f}%" if t2['lan_wr'] else "N/A"
        factors['lan'] = {
            'description': f"LAN Factor: {lan_desc1} vs {lan_desc2}",
            'advantage': team1 if lan_diff > 0 else team2,
            'impact': abs(lan_factor_val)
        }
    else:
        skipped.append('LAN Performance (not enough LAN/online data)')

    # H2H closeness regression: pull toward 50% when matches are competitive
    if h2h['total'] >= 2 and h2h['avg_closeness'] > 0:
        close_ratio = h2h['close_matches'] / h2h['total']
        pull_strength = close_ratio * h2h['avg_closeness'] * 0.5
        diff = scores['team1'] - scores['team2']
        scores['team1'] -= diff * pull_strength / 2
        scores['team2'] += diff * pull_strength / 2

    # Apply format adjustment
    format_adj = get_format_adjustment(match_format)
    if scores['team1'] > scores['team2']:
        diff = scores['team1'] - scores['team2']
        scores['team1'] = 50 + (diff * format_adj) / 2
        scores['team2'] = 50 - (diff * format_adj) / 2
    else:
        diff = scores['team2'] - scores['team1']
        scores['team2'] = 50 + (diff * format_adj) / 2
        scores['team1'] = 50 - (diff * format_adj) / 2

    # Sigmoid compression: compress extreme score differences
    raw_diff = scores['team1'] - scores['team2']
    if abs(raw_diff) > 10:
        sign = 1 if raw_diff > 0 else -1
        compressed = sign * (10 + 12 * math.log(abs(raw_diff) / 10))
        midpoint = (scores['team1'] + scores['team2']) / 2
        scores['team1'] = midpoint + compressed / 2
        scores['team2'] = midpoint - compressed / 2

    # Clamp scores so neither goes below 5 (prevents >100% / negative %)
    scores['team1'] = max(scores['team1'], 5)
    scores['team2'] = max(scores['team2'], 5)

    # Normalize to percentages
    total = scores['team1'] + scores['team2']
    prob1 = scores['team1'] / total * 100
    prob2 = scores['team2'] / total * 100

    # Cap max probability — in CS2 anyone can upset, no team is 90%+ safe
    prob1 = max(min(prob1, 85), 15)
    prob2 = max(min(prob2, 85), 15)
    total_prob = prob1 + prob2
    prob1 = prob1 / total_prob * 100
    prob2 = prob2 / total_prob * 100

    # ============================================================
    # DISPLAY RESULTS
    # ============================================================

    def print_team_info(name, t):
        print(f"\n{name.upper()}")
        print("-" * 50)
        print(f"ELO: {t['elo']:.0f} | World Rank: #{t['world_rank'] or 'N/A'}")
        print(f"Roster: {', '.join(t['roster'][:5])}")
        rated_players = [p for p in t['players'] if p.get('rating')]
        if rated_players:
            print("Key Players:")
            for p in sorted(rated_players, key=lambda x: x['rating'], reverse=True)[:3]:
                adr_str = f" ADR:{p['adr']:.1f}" if p.get('adr') else ""
                kast_str = f" KAST:{p['kast']:.0f}%" if p.get('kast') else ""
                role_str = f" ({p['role']})" if p.get('role') else ""
                print(f"  * {p['name']}{role_str}: {p['rating']:.2f}{adr_str}{kast_str}")
        elif t['avg_player_rating'] is None:
            print("  (no player ratings available)")
        print(f"Form: {''.join(t['recent_form'][:8]) or 'N/A'} ({t['weighted_form']*100:.0f}% weighted)")
        print(f"Roster Stability: {t['roster_stability']*100:.0f}%")
        best_maps = sorted(t['map_pool'].items(), key=lambda x: x[1], reverse=True)[:3]
        real_tag = "" if t['map_pool_is_real'] else " [generic]"
        print(f"Best Maps: {', '.join([f'{m[0]}({m[1]*100:.0f}%)' for m in best_maps])}{real_tag}")

    print_team_info(team1, t1)
    print_team_info(team2, t2)

    # Head to head
    print(f"\nHEAD TO HEAD")
    print("-" * 50)
    if h2h['matches']:
        print(f"{team1}: {h2h['t1_wins']} wins | {team2}: {h2h['t2_wins']} wins")
        for m in h2h['matches'][:3]:
            print(f"  {m[0]} {m[1]}-{m[2]} {m[3]}")
    else:
        print("No previous matches")

    # Factors breakdown
    print(f"\nPREDICTION FACTORS ({len(factors)} active, {len(skipped)} skipped)")
    print("-" * 50)
    for name, f in sorted(factors.items(), key=lambda x: x[1]['impact'], reverse=True):
        arrow = "->" if f['advantage'] == team1 else "<-"
        print(f"  {f['description']}")
        print(f"    {arrow} {f['advantage']} (+{f['impact']:.1f})")

    if skipped:
        print(f"\n  Skipped (no data, weight redistributed):")
        for s in skipped:
            print(f"    - {s}")

    if match_format != 'bo3':
        print(f"\n  Format Adjustment ({match_format.upper()}): {'Increased' if format_adj > 1 else 'Reduced'} favorite advantage")

    # Final prediction
    winner = team1 if prob1 > prob2 else team2
    confidence = max(prob1, prob2)

    print(f"\n{'='*70}")
    print(f"  FINAL PREDICTION")
    print(f"{'='*70}")
    print(f"\n  {team1}: {prob1:.1f}%")
    print(f"  {team2}: {prob2:.1f}%")
    print(f"\n  >>> PREDICTED WINNER: {winner}")
    print(f"  >>> CONFIDENCE: {confidence:.0f}%")

    if confidence >= 70:
        print(f"  >>> VERDICT: Strong favorite")
    elif confidence >= 60:
        print(f"  >>> VERDICT: Moderate favorite")
    elif confidence >= 55:
        print(f"  >>> VERDICT: Slight favorite")
    else:
        print(f"  >>> VERDICT: Coin flip / High upset potential")

    # Score line probabilities
    if match_format.lower() != 'bo1':
        p = prob1 / 100
        q = 1 - p
        if match_format.lower() == 'bo3':
            raw = {'2-0': p**2, '2-1': 2*p**2*q, '1-2': 2*q**2*p, '0-2': q**2}
        else:
            raw = {'3-0': p**3, '3-1': 3*p**3*q, '3-2': 6*p**3*q**2,
                   '2-3': 6*q**3*p**2, '1-3': 3*q**3*p, '0-3': q**3}
        total_raw = sum(raw.values())
        lines = {k: v/total_raw*100 for k, v in raw.items()}
        best = max(lines, key=lines.get)

        print(f"\n  SCORE PREDICTIONS ({match_format.upper()}):")
        for score, pct in lines.items():
            marker = " <<<" if score == best else ""
            bar = "#" * int(pct / 2)
            print(f"    {score}: {pct:5.1f}% {bar}{marker}")

        if match_format.lower() == 'bo3':
            t1_1map = (1 - lines.get('0-2', 0)/100) * 100
            t2_1map = (1 - lines.get('2-0', 0)/100) * 100
        else:
            t1_1map = (1 - lines.get('0-3', 0)/100) * 100
            t2_1map = (1 - lines.get('3-0', 0)/100) * 100
        print(f"\n    {team1} wins at least 1 map: {t1_1map:.1f}%")
        print(f"    {team2} wins at least 1 map: {t2_1map:.1f}%")

    # Player kill estimates
    def _est_kills(players, name):
        rated = [p for p in players if p.get('rating')]
        if not rated:
            return
        print(f"\n  {name} KILL ESTIMATES (per map, ~24 rounds):")
        for p in sorted(rated, key=lambda x: x['rating'], reverse=True)[:3]:
            kpr = 0.5 + (p['rating'] - 1.0) * 0.4
            kills = kpr * 24
            print(f"    {p['name']}: ~{kills:.0f} kills/map (est. KPR: {kpr:.2f})")

    _est_kills(t1['players'], team1.upper())
    _est_kills(t2['players'], team2.upper())

    # Safe bet suggestions
    print(f"\n  SAFE BET SUGGESTIONS:")
    print("  " + "-" * 50)
    fav = team1 if prob1 > prob2 else team2
    dog = team2 if prob1 > prob2 else team1
    fav_prob = max(prob1, prob2)

    if fav_prob >= 65:
        print(f"  [SAFE]     {fav} to win ({fav_prob:.0f}%)")
    elif fav_prob >= 55:
        print(f"  [MODERATE] {fav} to win ({fav_prob:.0f}%)")

    if match_format.lower() == 'bo3' and match_format.lower() != 'bo1':
        dog_sweep = lines.get('0-2', 0) if prob1 > prob2 else lines.get('2-0', 0)
        dog_1map = 100 - dog_sweep
        if dog_1map >= 60:
            tag = "SAFE" if dog_1map >= 75 else "MODERATE"
            print(f"  [{tag:8}] {dog} +1.5 maps — win at least 1 ({dog_1map:.0f}%)")

        over_25 = lines.get('2-1', 0) + lines.get('1-2', 0)
        under_25 = lines.get('2-0', 0) + lines.get('0-2', 0)
        if over_25 >= 55:
            tag = "SAFE" if over_25 >= 65 else "MODERATE"
            print(f"  [{tag:8}] Over 2.5 maps — goes to map 3 ({over_25:.0f}%)")
        elif under_25 >= 55:
            tag = "SAFE" if under_25 >= 65 else "MODERATE"
            print(f"  [{tag:8}] Under 2.5 maps — 2-0 finish ({under_25:.0f}%)")

    elif match_format.lower() == 'bo5':
        if prob1 > prob2:
            dog_sweep = lines.get('3-0', 0)
        else:
            dog_sweep = lines.get('0-3', 0)
        dog_wins_map = 100 - dog_sweep
        if dog_wins_map >= 60:
            tag = "SAFE" if dog_wins_map >= 80 else "MODERATE"
            print(f"  [{tag:8}] {dog} +2.5 maps — win at least 1 ({dog_wins_map:.0f}%)")

    # Star player kills
    all_players = t1['players'] + t2['players']
    rated = [p for p in all_players if p.get('rating')]
    if rated:
        star = max(rated, key=lambda x: x['rating'])
        kpr = 0.5 + (star['rating'] - 1.0) * 0.4
        est = kpr * 24
        safe_line = int(est) - 3
        if safe_line > 0:
            print(f"  [MODERATE] {star['name']} over {safe_line}.5 kills (~{est:.0f} estimated)")

    print()

    # Bookmaker odds comparison
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT team1, team2, team1_odds, team2_odds FROM betting_odds
        WHERE (team1 LIKE ? AND team2 LIKE ?)
           OR (team1 LIKE ? AND team2 LIKE ?)
        ORDER BY fetched_at DESC LIMIT 1
    ''', (f'%{team1}%', f'%{team2}%', f'%{team2}%', f'%{team1}%'))
    odds = c.fetchone()
    conn.close()

    if odds and odds[2] and odds[3] and odds[2] > 0 and odds[3] > 0:
        implied1 = (1 / odds[2]) * 100
        implied2 = (1 / odds[3]) * 100
        if team1.lower() not in odds[0].lower():
            implied1, implied2 = implied2, implied1
        print(f"\n  BOOKMAKER COMPARISON:")
        print(f"  Our model:  {team1} {prob1:.0f}% | {team2} {prob2:.0f}%")
        print(f"  Bookmakers: {team1} {implied1:.0f}% | {team2} {implied2:.0f}%")
        diff = abs(prob1 - implied1)
        if diff > 10:
            print(f"  >>> VALUE BET: Model disagrees with bookmakers by {diff:.0f}%")

    print(f"{'='*70}\n")

    return {
        'winner': winner,
        'team1_prob': prob1,
        'team2_prob': prob2,
        'confidence': confidence,
        'factors': factors
    }


def show_elo_rankings():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    print(f"\n{'='*70}")
    print("  ELO RANKINGS")
    print(f"{'='*70}\n")

    c.execute('''
        SELECT te.team, te.elo_rating, te.matches_played, tr.world_rank
        FROM team_elo te
        LEFT JOIN team_rankings tr ON te.team = tr.team
        ORDER BY te.elo_rating DESC
        LIMIT 20
    ''')

    for i, row in enumerate(c.fetchall(), 1):
        team, elo, matches, world_rank = row
        rank_str = f"HLTV #{world_rank}" if world_rank else ""
        bar = "#" * int(max(0, (elo - 1400) / 20))
        print(f"{i:2}. {team:20} {elo:7.1f} ELO ({matches:3} matches) {rank_str:12} {bar}")

    conn.close()


def main():
    import sys

    init_advanced_db()

    if len(sys.argv) >= 3:
        team1 = sys.argv[1]
        team2 = sys.argv[2]
        fmt = sys.argv[3] if len(sys.argv) > 3 else 'bo3'
        predict_match(team1, team2, fmt)
    elif len(sys.argv) == 2:
        if sys.argv[1] == 'elo':
            calculate_all_elo()
            show_elo_rankings()
        elif sys.argv[1] == 'rankings':
            show_elo_rankings()
    else:
        print("\nCS2 UNIFIED PREDICTOR")
        print("="*50)
        print("\nUsage:")
        print("  python3 advanced_predictor.py <team1> <team2> [format]")
        print("  python3 advanced_predictor.py elo        # Calculate ELO ratings")
        print("  python3 advanced_predictor.py rankings   # Show ELO rankings")
        print("\nFormats: bo1, bo3, bo5")
        print("\nExamples:")
        print("  python3 advanced_predictor.py Vitality Spirit bo3")
        print("  python3 advanced_predictor.py MOUZ FaZe bo3")

if __name__ == "__main__":
    main()
