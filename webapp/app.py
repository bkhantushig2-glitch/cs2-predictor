#!/usr/bin/env python3
"""CS2 Match Predictor Web App"""

from flask import Flask, render_template, request, jsonify
import sqlite3
import json
import os
import sys
import math
import pickle

# Add parent dir so we can import predictor
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from advanced_predictor import (
    get_all_team_data, get_head_to_head, calculate_map_advantage,
    get_format_adjustment, EloSystem, init_advanced_db, get_map_pool
)
from auto_updater import classify_event
app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'cs2_data.db')

# Top-tier event filter — keep only majors, tier1, tier2 (excludes qualifier-grade noise)
TOP_TIERS = ('major', 'tier1', 'tier2')
TOP_TIER_SQL = "event_tier IN ('major', 'tier1', 'tier2')"


def is_top_tier_event(event_name: str) -> bool:
    """Check whether a free-text event name classifies into a top tier."""
    return classify_event(event_name or '') in TOP_TIERS


def get_top30_teams(db):
    """Return a set of lowercase team names ranked in the world top 30."""
    rows = db.execute(
        'SELECT team FROM team_rankings WHERE world_rank IS NOT NULL AND world_rank <= 30'
    ).fetchall()
    return {r['team'].lower() for r in rows}


def has_top30(team1: str, team2: str, top30: set) -> bool:
    """True if either team is exactly a top-30 ranked team (case-insensitive).
    Uses exact match — substring matching wrongly catches things like
    'ASTRAL' against 'Astralis' or 'HEROIC Academy' against 'HEROIC'."""
    return (team1 or '').lower() in top30 or (team2 or '').lower() in top30


def tier_badge_class(tier: str) -> str:
    """Map tier name to CSS badge class."""
    return {
        'major': 'badge-major',
        'tier1': 'badge-tier1',
        'tier2': 'badge-tier2',
    }.get(tier, 'badge-quali')

# Load calibrator if available
CALIBRATOR_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'calibrator.pkl')
_calibrator = None
if os.path.exists(CALIBRATOR_PATH):
    with open(CALIBRATOR_PATH, 'rb') as _f:
        _calibrator = pickle.load(_f)
    print(f"Loaded probability calibrator from {CALIBRATOR_PATH}")


def calibrate(prob1):
    """Map raw probability through isotonic calibrator. Returns (prob1, prob2) on 0-100 scale."""
    if _calibrator is None:
        return prob1, 100 - prob1
    cal = _calibrator.predict([prob1 / 100])[0] * 100
    cal = max(min(cal, 95), 5)
    return cal, 100 - cal


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ============================================================
# ROUTES
# ============================================================

@app.route('/')
def dashboard():
    db = get_db()

    # Top-tier upcoming matches involving at least one top-30 ranked team
    top30 = get_top30_teams(db)
    upcoming_raw = db.execute(
        'SELECT * FROM upcoming_matches ORDER BY status DESC, match_time'
    ).fetchall()
    upcoming = []
    for m in upcoming_raw:
        tier = classify_event(m['event'] or '')
        if tier not in TOP_TIERS:
            continue
        if not has_top30(m['team1'], m['team2'], top30):
            continue
        upcoming.append({**dict(m), 'tier': tier, 'tier_class': tier_badge_class(tier)})
        if len(upcoming) >= 12:
            break

    # Top-tier results
    results_raw = db.execute(
        f'SELECT * FROM matches WHERE {TOP_TIER_SQL} ORDER BY id DESC LIMIT 15'
    ).fetchall()
    results = [{**dict(r), 'tier_class': tier_badge_class(r['event_tier'])} for r in results_raw]

    # Top value bets: upcoming top-tier with bookmaker odds, model disagrees > 10%
    value_bets = []
    for m in upcoming[:10]:
        odds = db.execute('''
            SELECT team1_odds, team2_odds FROM betting_odds
            WHERE (team1 LIKE ? AND team2 LIKE ?) OR (team1 LIKE ? AND team2 LIKE ?)
            ORDER BY fetched_at DESC LIMIT 1
        ''', (f"%{m['team1']}%", f"%{m['team2']}%",
              f"%{m['team2']}%", f"%{m['team1']}%")).fetchone()
        if not odds or not odds['team1_odds'] or not odds['team2_odds']:
            continue
        try:
            pred = run_prediction(m['team1'], m['team2'], 'bo3')
            imp1 = (1 / odds['team1_odds']) * 100
            diff = pred['team1']['prob'] - imp1
            if abs(diff) >= 10:
                pick = m['team1'] if diff > 0 else m['team2']
                value_bets.append({
                    'team1': m['team1'], 'team2': m['team2'],
                    'event': m['event'], 'tier': m['tier'],
                    'tier_class': m['tier_class'],
                    'pick': pick, 'edge': abs(diff),
                    'model_prob': pred['team1']['prob'] if diff > 0 else pred['team2']['prob'],
                    'book_prob': imp1 if diff > 0 else 100 - imp1,
                })
            if len(value_bets) >= 5:
                break
        except Exception:
            continue

    team_count = db.execute('SELECT COUNT(*) FROM team_rankings').fetchone()[0]
    player_count = db.execute('SELECT COUNT(*) FROM players').fetchone()[0]
    top_tier_count = db.execute(
        f'SELECT COUNT(*) FROM matches WHERE {TOP_TIER_SQL}'
    ).fetchone()[0]

    db.close()
    return render_template('dashboard.html',
                           upcoming=upcoming, results=results,
                           value_bets=value_bets,
                           team_count=team_count, player_count=player_count,
                           match_count=top_tier_count)


@app.route('/teams')
def teams():
    db = get_db()
    teams = db.execute('''
        SELECT tr.*, te.elo_rating, tf.win_rate, tf.wins_last_10, tf.losses_last_10
        FROM team_rankings tr
        LEFT JOIN team_elo te ON tr.team = te.team
        LEFT JOIN team_form tf ON tr.team = tf.team
        ORDER BY tr.world_rank
    ''').fetchall()
    db.close()
    return render_template('teams.html', teams=teams)


@app.route('/team/<name>')
def team_detail(name):
    db = get_db()

    team = db.execute('SELECT * FROM team_rankings WHERE team = ?', (name,)).fetchone()
    if not team:
        return "Team not found", 404

    elo = db.execute('SELECT * FROM team_elo WHERE team = ?', (name,)).fetchone()
    form = db.execute('SELECT * FROM team_form WHERE team = ?', (name,)).fetchone()

    roster = json.loads(team['roster']) if team['roster'] else []
    players = []
    for p in roster:
        player = db.execute('SELECT * FROM players WHERE name LIKE ?', (f'%{p}%',)).fetchone()
        if player:
            players.append(player)

    maps = db.execute('SELECT * FROM team_maps WHERE team = ? ORDER BY win_rate DESC', (name,)).fetchall()

    matches = db.execute('''
        SELECT * FROM matches
        WHERE team1 LIKE ? OR team2 LIKE ?
        ORDER BY id DESC LIMIT 20
    ''', (f'%{name}%', f'%{name}%')).fetchall()

    changes = db.execute('''
        SELECT * FROM roster_changes
        WHERE title LIKE ? OR to_team LIKE ? OR from_team LIKE ?
        ORDER BY fetched_at DESC LIMIT 5
    ''', (f'%{name}%', f'%{name}%', f'%{name}%')).fetchall()

    db.close()
    return render_template('team_detail.html',
                           team=team, elo=elo, form=form, players=players,
                           maps=maps, matches=matches, changes=changes, roster=roster)


@app.route('/players')
def players():
    db = get_db()
    sort = request.args.get('sort', 'rating')
    valid_sorts = {'rating': 'rating DESC', 'adr': 'adr DESC', 'kast': 'kast DESC',
                   'name': 'name ASC', 'team': 'team ASC', 'maps': 'maps_played DESC'}
    order = valid_sorts.get(sort, 'rating DESC')

    players = db.execute(f'SELECT * FROM players ORDER BY {order} LIMIT 100').fetchall()
    db.close()
    return render_template('players.html', players=players, sort=sort)


@app.route('/matches')
def matches():
    db = get_db()
    page = int(request.args.get('page', 1))
    show_all = request.args.get('all') == '1'
    per_page = 30
    offset = (page - 1) * per_page

    where = '' if show_all else f'WHERE {TOP_TIER_SQL}'
    matches = db.execute(f'''
        SELECT * FROM matches {where} ORDER BY id DESC LIMIT ? OFFSET ?
    ''', (per_page, offset)).fetchall()

    total = db.execute(f'SELECT COUNT(*) FROM matches {where}').fetchone()[0]

    db.close()
    return render_template('matches.html', matches=matches, page=page,
                           total=total, per_page=per_page, show_all=show_all)


@app.route('/rankings')
def rankings():
    db = get_db()
    world = db.execute('SELECT * FROM team_rankings ORDER BY world_rank').fetchall()
    elo = db.execute('SELECT * FROM team_elo ORDER BY elo_rating DESC LIMIT 30').fetchall()
    db.close()
    return render_template('rankings.html', world=world, elo=elo)


@app.route('/predict', methods=['GET', 'POST'])
def predict():
    db = get_db()
    teams = db.execute('SELECT team FROM team_rankings ORDER BY world_rank').fetchall()
    team_list = [t['team'] for t in teams]
    db.close()

    result = None
    if request.method == 'POST':
        team1 = request.form.get('team1', '')
        team2 = request.form.get('team2', '')
        match_format = request.form.get('format', 'bo3')

        if team1 and team2 and team1 != team2:
            result = run_prediction(team1, team2, match_format)
    elif request.args.get('team1') and request.args.get('team2'):
        team1 = request.args.get('team1', '')
        team2 = request.args.get('team2', '')
        match_format = request.args.get('format', 'bo3')
        if team1 != team2:
            result = run_prediction(team1, team2, match_format)

    return render_template('predict.html', teams=team_list, result=result)


@app.route('/api/predict')
def api_predict():
    team1 = request.args.get('team1', '')
    team2 = request.args.get('team2', '')
    fmt = request.args.get('format', 'bo3')

    if not team1 or not team2:
        return jsonify({'error': 'Need team1 and team2'}), 400

    result = run_prediction(team1, team2, fmt)
    return jsonify(result)


def calculate_score_probabilities(win_prob, match_format):
    """Calculate score line probabilities from overall win probability"""
    p = win_prob / 100  # convert to 0-1
    q = 1 - p
    fmt = match_format.lower()

    if fmt == 'bo1':
        return {'score_lines': {}, 'most_likely': None,
                'team1_win_1_map': None, 'team2_win_1_map': None}

    if fmt == 'bo3':
        raw = {
            '2-0': p ** 2,
            '2-1': 2 * p ** 2 * q,
            '1-2': 2 * q ** 2 * p,
            '0-2': q ** 2,
        }
    else:  # bo5
        raw = {
            '3-0': p ** 3,
            '3-1': 3 * p ** 3 * q,
            '3-2': 6 * p ** 3 * q ** 2,
            '2-3': 6 * q ** 3 * p ** 2,
            '1-3': 3 * q ** 3 * p,
            '0-3': q ** 3,
        }

    total = sum(raw.values())
    score_lines = {k: (v / total * 100) for k, v in raw.items()}
    most_likely = max(score_lines, key=score_lines.get)

    # "Win at least 1 map" probabilities
    if fmt == 'bo3':
        t1_win_1 = (1 - score_lines.get('0-2', 0) / 100) * 100
        t2_win_1 = (1 - score_lines.get('2-0', 0) / 100) * 100
    else:
        t1_win_1 = (1 - score_lines.get('0-3', 0) / 100) * 100
        t2_win_1 = (1 - score_lines.get('3-0', 0) / 100) * 100

    return {
        'score_lines': score_lines,
        'most_likely': most_likely,
        'team1_win_1_map': round(t1_win_1, 1),
        'team2_win_1_map': round(t2_win_1, 1),
    }


def estimate_player_kills(players):
    """Estimate kills per map from player rating.
    Calibrated against typical CS2 pro lines on 1xbet/Melbet:
      ZywOo (rating ~1.20)  → ~22 kills/map  → over 19.5/21.5 lines
      avg pro (rating ~1.05) → ~17 kills/map → over 15.5/17.5 lines
      below avg (~0.95)     → ~15 kills/map → mostly UNDER 17.5 region
    """
    AVG_ROUNDS = 26  # CS2 maps average ~26 rounds (OT common in close pro games)
    result = []
    for p in players:
        rating = p.get('rating')
        if not rating:
            continue
        # KPR = 0.65 baseline (avg CS2 pro ~17 kills) + 0.7 per +0.1 rating
        kpr = 0.65 + (rating - 1.0) * 0.7
        kpr = max(0.45, min(kpr, 1.05))  # clamp to realistic range
        kills = kpr * AVG_ROUNDS
        result.append({
            'name': p['name'],
            'estimated_kpr': round(kpr, 2),
            'estimated_kills': round(kills, 1),
        })
    return sorted(result, key=lambda x: x['estimated_kills'], reverse=True)


def generate_safe_bets(team1, team2, prob1, prob2, score_probs, t1_kills, t2_kills, match_format):
    """Generate safe bet suggestions based on prediction data"""
    bets = []
    fmt = match_format.lower()
    fav = team1 if prob1 > prob2 else team2
    dog = team2 if prob1 > prob2 else team1
    fav_prob = max(prob1, prob2)
    dog_prob = min(prob1, prob2)
    lines = score_probs.get('score_lines', {})

    # 1. Match winner (when confident)
    if fav_prob >= 65:
        bets.append({
            'label': f'{fav} to win',
            'prob': round(fav_prob, 1),
            'tier': 'safe',
            'icon': 'trophy-fill',
            'reason': f'{fav} is a clear favorite at {fav_prob:.0f}%',
        })
    elif fav_prob >= 55:
        bets.append({
            'label': f'{fav} to win',
            'prob': round(fav_prob, 1),
            'tier': 'moderate',
            'icon': 'trophy',
            'reason': f'Slight edge — could go either way',
        })

    if fmt == 'bo3' and lines:
        # 2. Underdog +1.5 map handicap (win at least 1 map)
        dog_1map = score_probs.get('team2_win_1_map' if dog == team2 else 'team1_win_1_map', 0)
        if dog_1map and dog_1map >= 60:
            tier = 'safe' if dog_1map >= 75 else 'moderate'
            bets.append({
                'label': f'{dog} +1.5 maps (win at least 1)',
                'prob': round(dog_1map, 1),
                'tier': tier,
                'icon': 'shield-check',
                'reason': f'{dog} has {dog_1map:.0f}% chance to take at least 1 map',
            })

        # 3. Over/Under 2.5 maps (does it go to map 3?)
        over_25 = lines.get('2-1', 0) + lines.get('1-2', 0)
        under_25 = lines.get('2-0', 0) + lines.get('0-2', 0)
        if over_25 >= 55:
            tier = 'safe' if over_25 >= 65 else 'moderate'
            bets.append({
                'label': 'Over 2.5 maps (goes to map 3)',
                'prob': round(over_25, 1),
                'tier': tier,
                'icon': 'arrow-up-circle',
                'reason': f'Close matchup — {over_25:.0f}% chance of 3 maps',
            })
        elif under_25 >= 55:
            tier = 'safe' if under_25 >= 65 else 'moderate'
            bets.append({
                'label': 'Under 2.5 maps (2-0 finish)',
                'prob': round(under_25, 1),
                'tier': tier,
                'icon': 'arrow-down-circle',
                'reason': f'Skill gap suggests a 2-0 — {under_25:.0f}% likely',
            })

    elif fmt == 'bo5' and lines:
        # Underdog +2.5 map handicap
        if prob1 > prob2:
            dog_sweep = lines.get('3-0', 0)
        else:
            dog_sweep = lines.get('0-3', 0)
        dog_wins_map = 100 - dog_sweep
        if dog_wins_map >= 60:
            tier = 'safe' if dog_wins_map >= 80 else 'moderate'
            bets.append({
                'label': f'{dog} +2.5 maps (win at least 1)',
                'prob': round(dog_wins_map, 1),
                'tier': tier,
                'icon': 'shield-check',
                'reason': f'{dog} very likely to avoid a 0-3 sweep',
            })

        # Over/Under 4.5 maps
        over_45 = lines.get('3-2', 0) + lines.get('2-3', 0)
        if over_45 >= 40:
            tier = 'safe' if over_45 >= 55 else 'moderate'
            bets.append({
                'label': 'Over 4.5 maps (goes to map 5)',
                'prob': round(over_45, 1),
                'tier': tier,
                'icon': 'arrow-up-circle',
                'reason': f'{over_45:.0f}% chance of a full 5 maps',
            })

    # 4. Player kill prop — only suggest at REALISTIC bookmaker lines.
    # 1xbet/Melbet/most CS2 books offer player kill lines on a 0.5-step ladder
    # roughly between 15.5 and 27.5 per map. Never suggest a line outside that range.
    # Map-1 props for star players: ~21.5+. Tier-2 players: 15.5–19.5.
    MAP_KILL_LINES = [15.5, 17.5, 19.5, 21.5, 23.5, 25.5, 27.5]
    SIGMA = 4.5  # rough per-map kill stddev across CS2 pros

    def hit_over(estimated, line):
        z = (estimated - line) / SIGMA
        return 0.5 * (1 + math.erf(z / math.sqrt(2))) * 100

    all_kills = t1_kills + t2_kills
    if all_kills:
        star = all_kills[0]
        est = star['estimated_kills']
        # Closest market line at or just below the player's estimate
        below = [L for L in MAP_KILL_LINES if L <= est - 0.5]
        above = [L for L in MAP_KILL_LINES if L > est]

        if below:
            line = below[-1]  # nearest line below estimate → OVER
            prob = hit_over(est, line)
            if prob >= 60:  # only suggest if real edge
                tier = 'safe' if prob >= 75 else 'moderate'
                bets.append({
                    'label': f'{star["name"]} over {line} kills (per map)',
                    'prob': round(prob, 1),
                    'tier': tier,
                    'icon': 'crosshair',
                    'reason': f'Est ~{est:.0f} kills/map · {line} is the typical 1xbet/Melbet line below his expectation',
                })
        elif above:
            # Player's expectation is below all real market lines → suggest UNDER
            line = above[0]  # lowest line above estimate
            prob = 100 - hit_over(est, line)
            if prob >= 60:
                tier = 'safe' if prob >= 75 else 'moderate'
                bets.append({
                    'label': f'{star["name"]} under {line} kills (per map)',
                    'prob': round(prob, 1),
                    'tier': tier,
                    'icon': 'crosshair',
                    'reason': f'Est ~{est:.0f} kills/map · books typically post {line}+ for this role; UNDER has the edge',
                })
        # Otherwise (no realistic line near this player) → skip the kill prop entirely

    return bets


def run_prediction(team1, team2, match_format='bo3'):
    """Run prediction with dynamic weight redistribution (matches v3 predictor logic)"""
    t1 = get_all_team_data(team1)
    t2 = get_all_team_data(team2)
    h2h = get_head_to_head(team1, team2)
    map_adv, map_details, map_data_reliable = calculate_map_advantage(team1, team2)

    # Check which factors have real data
    has_ranking = bool(t1['world_rank'] and t2['world_rank'])
    has_player_data = (t1['avg_player_rating'] is not None and t2['avg_player_rating'] is not None)
    has_form = bool(t1['recent_form'] and t2['recent_form'])
    has_h2h = bool(h2h['matches'])
    has_lan = (t1.get('lan_wr') is not None and t2.get('lan_wr') is not None)

    # Check if ELO is real (team actually has matches) vs default 1500
    t1_has_elo = t1['elo'] != 1500
    t2_has_elo = t2['elo'] != 1500
    both_have_elo = t1_has_elo and t2_has_elo

    # Boost ranking weight when rank gap is large (top team vs low team)
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

    # Form confidence: scale weight by how many matches it's based on
    t1_conf = min(t1.get('form_match_count', 0) / 8, 1.0)
    t2_conf = min(t2.get('form_match_count', 0) / 8, 1.0)
    form_confidence = min(t1_conf, t2_conf)
    effective_form_weight = max(3, int(15 * (0.2 + 0.8 * form_confidence)))
    has_form = bool(t1['recent_form'] or t2['recent_form'])

    # Dynamic H2H weight by match count
    h2h_count = h2h['total']
    if h2h_count >= 4:
        h2h_base_weight = 15
    elif h2h_count >= 2:
        h2h_base_weight = 10
    else:
        h2h_base_weight = 5

    # Factor definitions: (name, base_weight, has_data)
    factor_defs = [
        ('elo', 25 if both_have_elo else 10, True),
        ('ranking', rank_weight, has_ranking),
        ('player_ratings', 20, has_player_data),
        ('map_pool', 15 if map_data_reliable else 5, True),
        ('form', effective_form_weight, has_form),
        ('h2h', h2h_base_weight, has_h2h),
        ('stability', 5, True),
        ('lan', 5, has_lan),
    ]

    total_active = sum(w for _, w, has in factor_defs if has)
    total_all = sum(w for _, w, _ in factor_defs)
    scale = total_all / total_active if total_active > 0 else 1.0

    weights = {}
    for name, base_w, has_data in factor_defs:
        weights[name] = (base_w * scale) if has_data else 0

    scores = {'team1': 50, 'team2': 50}
    factors = []

    # 1. ELO
    if weights['elo'] > 0:
        elo_diff = t1['elo'] - t2['elo']
        w = weights['elo'] / 25
        elo_f = max(min((elo_diff / 20) * w, 15), -15)
        scores['team1'] += elo_f
        scores['team2'] -= elo_f
        factors.append({
            'name': 'ELO Rating', 'icon': 'trophy',
            'detail': f"{t1['elo']:.0f} vs {t2['elo']:.0f}",
            'advantage': team1 if elo_diff > 0 else team2,
            'impact': abs(elo_f)
        })

    # 2. Ranking
    if weights['ranking'] > 0:
        rank_diff = t2['world_rank'] - t1['world_rank']
        w = weights['ranking'] / 15
        rank_f = max(min(rank_diff * 0.8 * w, rank_cap), -rank_cap)
        scores['team1'] += rank_f
        scores['team2'] -= rank_f
        factors.append({
            'name': 'World Ranking', 'icon': 'globe',
            'detail': f"#{t1['world_rank']} vs #{t2['world_rank']}",
            'advantage': team1 if rank_diff > 0 else team2,
            'impact': abs(rank_f)
        })

    # 3. Players
    if weights['player_ratings'] > 0:
        rating_diff = (t1['avg_player_rating'] - t2['avg_player_rating']) * 25
        w = weights['player_ratings'] / 20
        rating_f = max(min(rating_diff * w, 12), -12)
        scores['team1'] += rating_f
        scores['team2'] -= rating_f
        factors.append({
            'name': 'Player Quality', 'icon': 'users',
            'detail': f"{t1['avg_player_rating']:.2f} vs {t2['avg_player_rating']:.2f}",
            'advantage': team1 if rating_diff > 0 else team2,
            'impact': abs(rating_f)
        })

    # 4. Map pool
    if weights['map_pool'] > 0:
        w = weights['map_pool'] / 15
        map_f = max(min(map_adv * 20 * w, 10), -10)
        scores['team1'] += map_f
        scores['team2'] -= map_f
        reliability = "" if map_data_reliable else " [low confidence]"
        factors.append({
            'name': 'Map Pool', 'icon': 'map',
            'detail': f"{map_adv*100:+.1f}% advantage{reliability}",
            'advantage': team1 if map_adv > 0 else team2,
            'impact': abs(map_f)
        })

    # 5. Form
    if weights['form'] > 0:
        form_diff = (t1['weighted_form'] - t2['weighted_form']) * 20
        w = weights['form'] / 15
        form_f = max(min(form_diff * w, 10), -10)
        scores['team1'] += form_f
        scores['team2'] -= form_f
        factors.append({
            'name': 'Recent Form', 'icon': 'trending-up',
            'detail': f"{t1['weighted_form']*100:.0f}% vs {t2['weighted_form']*100:.0f}%",
            'advantage': team1 if form_diff > 0 else team2,
            'impact': abs(form_f)
        })

    # 6. H2H (with closeness dampening)
    if weights['h2h'] > 0:
        h2h_diff = (h2h['t1_wins'] - h2h['t2_wins']) * 3
        w = weights['h2h'] / h2h_base_weight
        h2h_f = h2h_diff * w
        # Dampen when matches are close (2-1 results)
        if h2h['avg_closeness'] > 0.3:
            dampen = 1.0 - (h2h['avg_closeness'] * 0.8)
            h2h_f *= dampen
        h2h_f = max(min(h2h_f, 8), -8)
        scores['team1'] += h2h_f
        scores['team2'] -= h2h_f
        factors.append({
            'name': 'Head-to-Head', 'icon': 'repeat',
            'detail': f"{h2h['t1_wins']}-{h2h['t2_wins']} (closeness: {h2h['avg_closeness']:.2f})",
            'advantage': team1 if h2h_diff > 0 else team2,
            'impact': abs(h2h_f)
        })

    # 7. Stability
    if weights['stability'] > 0:
        stab_diff = (t1['roster_stability'] - t2['roster_stability']) * 10
        w = weights['stability'] / 5
        stab_f = max(min(stab_diff * w, 5), -5)
        scores['team1'] += stab_f
        scores['team2'] -= stab_f
        factors.append({
            'name': 'Roster Stability', 'icon': 'shield',
            'detail': f"{t1['roster_stability']*100:.0f}% vs {t2['roster_stability']*100:.0f}%",
            'advantage': team1 if stab_diff > 0 else team2,
            'impact': abs(stab_f)
        })

    # 8. LAN
    if weights['lan'] > 0:
        lan_diff = (t1['lan_factor'] - t2['lan_factor']) * 8
        w = weights['lan'] / 5
        lan_f = max(min(lan_diff * w, 5), -5)
        scores['team1'] += lan_f
        scores['team2'] -= lan_f
        factors.append({
            'name': 'LAN Performance', 'icon': 'wifi',
            'detail': f"Factor: {t1['lan_factor']:.2f} vs {t2['lan_factor']:.2f}",
            'advantage': team1 if lan_diff > 0 else team2,
            'impact': abs(lan_f)
        })

    # H2H closeness regression: pull toward 50% when matches are competitive
    if h2h['total'] >= 2 and h2h['avg_closeness'] > 0:
        close_ratio = h2h['close_matches'] / h2h['total']
        pull_strength = close_ratio * h2h['avg_closeness'] * 0.5
        diff = scores['team1'] - scores['team2']
        scores['team1'] -= diff * pull_strength / 2
        scores['team2'] += diff * pull_strength / 2

    # Format adjustment
    fmt_adj = get_format_adjustment(match_format)
    if scores['team1'] > scores['team2']:
        diff = scores['team1'] - scores['team2']
        scores['team1'] = 50 + (diff * fmt_adj) / 2
        scores['team2'] = 50 - (diff * fmt_adj) / 2
    else:
        diff = scores['team2'] - scores['team1']
        scores['team2'] = 50 + (diff * fmt_adj) / 2
        scores['team1'] = 50 - (diff * fmt_adj) / 2

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

    total = scores['team1'] + scores['team2']
    prob1 = scores['team1'] / total * 100
    prob2 = scores['team2'] / total * 100

    # Cap max probability — in CS2 anyone can upset, no team is 90%+ safe
    prob1 = max(min(prob1, 85), 15)
    prob2 = max(min(prob2, 85), 15)
    # Re-normalize to 100%
    total_prob = prob1 + prob2
    prob1 = prob1 / total_prob * 100
    prob2 = prob2 / total_prob * 100

    # Apply probability calibration (isotonic regression)
    prob1, prob2 = calibrate(prob1)

    winner = team1 if prob1 > prob2 else team2
    confidence = max(prob1, prob2)

    if confidence >= 70:
        verdict = 'Strong favorite'
    elif confidence >= 60:
        verdict = 'Moderate favorite'
    elif confidence >= 55:
        verdict = 'Slight favorite'
    else:
        verdict = 'Coin flip'

    # Multi-book bookmaker odds + best-price detection
    bookmaker = None        # legacy single-book payload (kept for backwards compat in templates)
    books = []              # full list of all bookmakers with implied probs + edge
    db = get_db()
    odds_rows = db.execute('''
        SELECT team1, team2, team1_odds, team2_odds, bookmaker, fetched_at
        FROM betting_odds
        WHERE (team1 LIKE ? AND team2 LIKE ?)
           OR (team1 LIKE ? AND team2 LIKE ?)
        ORDER BY fetched_at DESC, bookmaker ASC
    ''', (f'%{team1}%', f'%{team2}%', f'%{team2}%', f'%{team1}%')).fetchall()
    db.close()

    for o in odds_rows:
        t1o, t2o = o['team1_odds'], o['team2_odds']
        if not (t1o and t2o and t1o > 1 and t2o > 1):
            continue
        # If row's team1 doesn't match our team1, swap odds
        if team1.lower() not in (o['team1'] or '').lower():
            t1o, t2o = t2o, t1o
        imp1 = (1 / t1o) * 100
        imp2 = (1 / t2o) * 100
        # Edge from our perspective: positive when model thinks team1 is more likely than book
        edge_t1 = prob1 - imp1
        edge_t2 = prob2 - imp2
        # EV when betting on each side at this book
        ev_t1 = (prob1 / 100) * t1o - 1
        ev_t2 = (prob2 / 100) * t2o - 1
        books.append({
            'bookmaker': o['bookmaker'] or 'Unknown',
            't1_odds': round(t1o, 2),
            't2_odds': round(t2o, 2),
            't1_implied': round(imp1, 1),
            't2_implied': round(imp2, 1),
            'edge_t1': round(edge_t1, 1),
            'edge_t2': round(edge_t2, 1),
            'ev_t1': round(ev_t1 * 100, 1),
            'ev_t2': round(ev_t2 * 100, 1),
            'fetched_at': o['fetched_at'],
        })

    if books:
        # Sort by best EV on the model's predicted winner side
        winner_is_t1 = prob1 > prob2
        books.sort(key=lambda b: b['ev_t1'] if winner_is_t1 else b['ev_t2'], reverse=True)
        # Tag the best price for each side independently
        best_t1_idx = max(range(len(books)), key=lambda i: books[i]['t1_odds'])
        best_t2_idx = max(range(len(books)), key=lambda i: books[i]['t2_odds'])
        for i, b in enumerate(books):
            b['best_t1'] = (i == best_t1_idx)
            b['best_t2'] = (i == best_t2_idx)
        # Legacy single-book payload — use the first (best EV) entry
        top = books[0]
        bookmaker = {'team1_prob': top['t1_implied'], 'team2_prob': top['t2_implied']}

    # Score line probabilities
    score_probs = calculate_score_probabilities(prob1, match_format)

    # Player kill estimates
    t1_kills = estimate_player_kills(t1['players'])
    t2_kills = estimate_player_kills(t2['players'])

    # Safe bets
    safe_bets = generate_safe_bets(
        team1, team2, prob1, prob2, score_probs,
        t1_kills, t2_kills, match_format
    )

    # Deep-links to bookmakers so user can verify player props manually
    try:
        from prop_odds import bookmaker_links, get_props_for_match
        deep_links = bookmaker_links(team1, team2)
        stored_props = get_props_for_match(team1, team2)
    except Exception:
        deep_links = []
        stored_props = []

    return {
        'team1': {'name': team1, 'data': t1, 'prob': prob1, 'kill_preds': t1_kills},
        'team2': {'name': team2, 'data': t2, 'prob': prob2, 'kill_preds': t2_kills},
        'winner': winner,
        'confidence': confidence,
        'verdict': verdict,
        'format': match_format.upper(),
        'factors': sorted(factors, key=lambda x: x['impact'], reverse=True),
        'h2h': h2h,
        'map_details': map_details,
        'bookmaker': bookmaker,
        'books': books,
        'deep_links': deep_links,
        'stored_props': stored_props,
        'score_probs': score_probs,
        'most_likely_score': score_probs['most_likely'],
        'map_insights': {
            'team1_win_1_map': score_probs['team1_win_1_map'],
            'team2_win_1_map': score_probs['team2_win_1_map'],
        },
        'safe_bets': safe_bets,
        'method': 'unified',
    }


def get_best_bet(team1, team2, pred):
    """Pick the single highest-probability realistic bet for a BO3 match.
    Markets: match winner, map handicap +-1.5, over/under 2.5 maps, correct score."""
    p1 = pred['team1']['prob']
    p2 = pred['team2']['prob']
    fav = team1 if p1 > p2 else team2
    dog = team2 if p1 > p2 else team1
    fav_prob = max(p1, p2)
    dog_prob = min(p1, p2)

    score_probs = pred.get('score_probs', {})
    lines = score_probs.get('score_lines', {})

    bets = []

    # Match winner
    bets.append({
        'market': 'Match Winner',
        'pick': f'{fav} to win',
        'prob': round(fav_prob, 1),
    })

    if lines:
        # Underdog +1.5 maps (win at least 1 map) — very common market
        if p1 > p2:
            dog_1map = (1 - lines.get('2-0', 0) / 100) * 100
        else:
            dog_1map = (1 - lines.get('0-2', 0) / 100) * 100
        bets.append({
            'market': 'Map Handicap',
            'pick': f'{dog} +1.5',
            'prob': round(dog_1map, 1),
        })

        # Favorite -1.5 maps (2-0 sweep)
        if p1 > p2:
            fav_sweep = lines.get('2-0', 0)
        else:
            fav_sweep = lines.get('0-2', 0)
        bets.append({
            'market': 'Map Handicap',
            'pick': f'{fav} -1.5',
            'prob': round(fav_sweep, 1),
        })

        # Over 2.5 maps (goes to map 3)
        over = lines.get('2-1', 0) + lines.get('1-2', 0)
        bets.append({
            'market': 'Total Maps',
            'pick': 'Over 2.5 maps',
            'prob': round(over, 1),
        })

        # Under 2.5 maps (2-0 either way)
        under = lines.get('2-0', 0) + lines.get('0-2', 0)
        bets.append({
            'market': 'Total Maps',
            'pick': 'Under 2.5 maps',
            'prob': round(under, 1),
        })

    # Return the single highest probability bet
    return max(bets, key=lambda b: b['prob'])


def predict_with_maps(team1, team2, maps, match_format='bo3'):
    """Run prediction with map-pick adjustments"""
    base = run_prediction(team1, team2, match_format)

    pool1, _ = get_map_pool(team1)
    pool2, _ = get_map_pool(team2)

    map_breakdown = []
    diffs = []
    for m in maps:
        wr1 = pool1.get(m, 0.5)
        wr2 = pool2.get(m, 0.5)
        diff = wr1 - wr2
        diffs.append(diff)
        map_breakdown.append({
            'map': m,
            'team1_wr': round(wr1 * 100, 1),
            'team2_wr': round(wr2 * 100, 1),
            'diff': round(diff * 100, 1),
        })

    avg_map_adv = sum(diffs) / len(diffs) if diffs else 0
    adjustment = avg_map_adv * 50
    adjustment = max(min(adjustment, 10), -10)

    adj_prob1 = base['team1']['prob'] + adjustment
    adj_prob2 = base['team2']['prob'] - adjustment
    adj_prob1 = max(min(adj_prob1, 85), 15)
    adj_prob2 = max(min(adj_prob2, 85), 15)
    total = adj_prob1 + adj_prob2
    adj_prob1 = adj_prob1 / total * 100
    adj_prob2 = adj_prob2 / total * 100

    winner = team1 if adj_prob1 > adj_prob2 else team2
    confidence = max(adj_prob1, adj_prob2)

    return {
        'team1': {'name': team1, 'prob': round(adj_prob1, 1)},
        'team2': {'name': team2, 'prob': round(adj_prob2, 1)},
        'winner': winner,
        'confidence': round(confidence, 1),
        'map_picks': maps,
        'map_breakdown': map_breakdown,
        'map_adjustment': round(adjustment, 1),
        'base_prob1': round(base['team1']['prob'], 1),
        'base_prob2': round(base['team2']['prob'], 1),
    }


@app.route('/value-bets')
def value_bets():
    """Top-tier upcoming matches where the model disagrees with the bookmaker by 5%+."""
    db = get_db()
    upcoming_raw = db.execute(
        'SELECT * FROM upcoming_matches ORDER BY status DESC, match_time'
    ).fetchall()

    top30 = get_top30_teams(db)
    bets = []
    for m in upcoming_raw:
        tier = classify_event(m['event'] or '')
        if tier not in TOP_TIERS:
            continue
        if not has_top30(m['team1'], m['team2'], top30):
            continue
        odds = db.execute('''
            SELECT team1_odds, team2_odds, bookmaker FROM betting_odds
            WHERE (team1 LIKE ? AND team2 LIKE ?) OR (team1 LIKE ? AND team2 LIKE ?)
            ORDER BY fetched_at DESC LIMIT 1
        ''', (f"%{m['team1']}%", f"%{m['team2']}%",
              f"%{m['team2']}%", f"%{m['team1']}%")).fetchone()
        if not odds or not odds['team1_odds'] or not odds['team2_odds']:
            continue
        try:
            pred = run_prediction(m['team1'], m['team2'], 'bo3')
        except Exception:
            continue
        imp1 = (1 / odds['team1_odds']) * 100
        imp2 = (1 / odds['team2_odds']) * 100
        diff1 = pred['team1']['prob'] - imp1
        if abs(diff1) < 5:
            continue
        pick = m['team1'] if diff1 > 0 else m['team2']
        edge = abs(diff1)
        # Expected value: model_prob * decimal_odds - 1
        if diff1 > 0:
            ev = (pred['team1']['prob'] / 100) * odds['team1_odds'] - 1
            book_p = imp1
            model_p = pred['team1']['prob']
            decimal_odd = odds['team1_odds']
        else:
            ev = (pred['team2']['prob'] / 100) * odds['team2_odds'] - 1
            book_p = imp2
            model_p = pred['team2']['prob']
            decimal_odd = odds['team2_odds']
        bets.append({
            'team1': m['team1'], 'team2': m['team2'],
            'event': m['event'], 'tier': tier,
            'tier_class': tier_badge_class(tier),
            'match_time': m['match_time'], 'status': m['status'],
            'pick': pick, 'edge': edge,
            'model_prob': model_p, 'book_prob': book_p,
            'odds': decimal_odd, 'ev': ev * 100,
            'bookmaker': odds['bookmaker'] or 'Pinnacle',
        })

    bets.sort(key=lambda b: b['ev'], reverse=True)
    db.close()
    return render_template('value_bets.html', bets=bets)


@app.route('/player/<name>')
def player_detail(name):
    """Player trends page: last 5/10/20 match performance + hit rate bars."""
    db = get_db()
    player = db.execute(
        'SELECT * FROM players WHERE name LIKE ? LIMIT 1', (f'%{name}%',)
    ).fetchone()
    if not player:
        db.close()
        return f"Player '{name}' not found", 404

    team = player['team']

    # Last 20 top-tier matches for this player's team
    recent = db.execute(f'''
        SELECT team1, team2, score1, score2, winner, event, event_tier, match_date, is_lan
        FROM matches
        WHERE (team1 LIKE ? OR team2 LIKE ?) AND {TOP_TIER_SQL}
        ORDER BY id DESC LIMIT 20
    ''', (f'%{team}%', f'%{team}%')).fetchall()

    # Compute team form for windows
    def form_window(matches, n):
        wins = 0
        for m in matches[:n]:
            w = (m['winner'] or '').lower()
            if team.lower() in w:
                wins += 1
        return wins, min(n, len(matches))

    w5, t5 = form_window(recent, 5)
    w10, t10 = form_window(recent, 10)
    w20, t20 = form_window(recent, 20)

    rating = player['rating'] or 1.0
    # Estimated kills per map based on player rating
    kpr = 0.5 + (rating - 1.0) * 0.4
    avg_kills = kpr * 24

    # Hit rate prop simulation: how often does player likely exceed kill thresholds?
    # Approximate by distributing around average with std=4 (rough)
    import math as _m

    def hit_rate(threshold):
        # Normal CDF approximation: P(kills > threshold)
        z = (avg_kills - threshold) / 4.0
        return 0.5 * (1 + _m.erf(z / _m.sqrt(2))) * 100

    prop_lines = []
    for delta in (-4, -2, 0, 2, 4):
        line = round(avg_kills) + delta - 0.5
        if line < 5:
            continue
        prop_lines.append({
            'line': line,
            'over_pct': hit_rate(line),
            'estimated': avg_kills,
        })

    # Recent matches with team result
    recent_list = []
    for m in recent[:10]:
        is_t1 = team.lower() in (m['team1'] or '').lower()
        opp = m['team2'] if is_t1 else m['team1']
        team_score = m['score1'] if is_t1 else m['score2']
        opp_score = m['score2'] if is_t1 else m['score1']
        won = team.lower() in (m['winner'] or '').lower()
        recent_list.append({
            'opponent': opp,
            'team_score': team_score,
            'opp_score': opp_score,
            'won': won,
            'event': m['event'],
            'tier': m['event_tier'],
            'tier_class': tier_badge_class(m['event_tier']),
            'is_lan': m['is_lan'],
            'date': m['match_date'],
            'est_kills': round(avg_kills, 1),
        })

    db.close()
    return render_template('player_trends.html',
                           player=player,
                           team=team,
                           form={'w5': w5, 't5': t5, 'w10': w10, 't10': t10, 'w20': w20, 't20': t20},
                           prop_lines=prop_lines,
                           recent=recent_list,
                           avg_kills=round(avg_kills, 1))


@app.route('/live')
def live():
    return render_template('live.html')


@app.route('/api/live-predictions')
def api_live_predictions():
    db = get_db()
    upcoming = db.execute('SELECT * FROM upcoming_matches ORDER BY status DESC, match_time').fetchall()

    ranked_teams = db.execute(
        'SELECT team FROM team_rankings WHERE world_rank <= 50'
    ).fetchall()
    ranked_names = [r['team'].lower() for r in ranked_teams]
    db.close()

    results = []
    for match in upcoming:
        event = match['event'] or ''
        tier = classify_event(event)
        if tier == 'qualifier' and 'blast' in event.lower():
            tier = 'tier1'

        t1 = match['team1'] or ''
        t2 = match['team2'] or ''
        t1_known = any(t1.lower() in rn or rn in t1.lower() for rn in ranked_names)
        t2_known = any(t2.lower() in rn or rn in t2.lower() for rn in ranked_names)

        if tier in ('major', 'tier1') and (t1_known or t2_known):
            pass
        elif t1_known and t2_known:
            pass
        else:
            continue

        try:
            pred = run_prediction(t1, t2, 'bo3')
            top_factors = pred['factors'][:3]
            best_bet = get_best_bet(t1, t2, pred)
            results.append({
                'team1': t1,
                'team2': t2,
                'event': event,
                'tier': tier,
                'status': match['status'] or '',
                'match_time': match['match_time'] or '',
                'prob1': round(pred['team1']['prob'], 1),
                'prob2': round(pred['team2']['prob'], 1),
                'winner': pred['winner'],
                'confidence': round(pred['confidence'], 1),
                'verdict': pred['verdict'],
                'factors': [{'name': f['name'], 'detail': f['detail'],
                             'advantage': f['advantage'], 'impact': round(f['impact'], 1)}
                            for f in top_factors],
                'best_bet': best_bet,
            })
        except Exception:
            continue

    return jsonify(results)


@app.route('/api/refresh-odds')
def api_refresh_odds():
    """Refresh multi-book odds for one match on demand. Slow (1-3s)."""
    team1 = request.args.get('team1', '')
    team2 = request.args.get('team2', '')
    if not team1 or not team2:
        return jsonify({'error': 'need team1 and team2'}), 400

    db = get_db()
    row = db.execute(
        'SELECT match_url FROM upcoming_matches WHERE team1 = ? AND team2 = ? LIMIT 1',
        (team1, team2),
    ).fetchone()
    db.close()

    if not row or not row['match_url']:
        return jsonify({'ok': False, 'reason': 'no match URL on file (not currently upcoming)'}), 200

    try:
        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from hltv_odds import scrape_match_odds, save_quotes
        quotes = scrape_match_odds(row['match_url'])
        if not quotes:
            return jsonify({'ok': False, 'reason': 'no odds posted on this match yet'}), 200
        save_quotes(team1, team2, '', quotes)
        return jsonify({
            'ok': True,
            'books': len(quotes),
            'fetched_at': __import__('datetime').datetime.now().isoformat(timespec='seconds'),
        })
    except Exception as e:
        return jsonify({'ok': False, 'reason': f'scrape failed: {e}'}), 200


@app.route('/api/predict-maps')
def api_predict_maps():
    team1 = request.args.get('team1', '')
    team2 = request.args.get('team2', '')
    maps_str = request.args.get('maps', '')
    fmt = request.args.get('format', 'bo3')

    if not team1 or not team2 or not maps_str:
        return jsonify({'error': 'Need team1, team2, and maps'}), 400

    maps = [m.strip() for m in maps_str.split(',') if m.strip()]
    result = predict_with_maps(team1, team2, maps, fmt)
    return jsonify(result)


# Run schema init at import time so gunicorn workers see the tables
init_advanced_db()
try:
    from prop_odds import init_schema as _props_init
    _props_init()
except Exception:
    pass


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    debug = os.environ.get('FLASK_DEBUG', '0') == '1'
    print(f"\n  CS2 Match Predictor Web App")
    print(f"  http://localhost:{port}\n")
    app.run(host='0.0.0.0', debug=debug, port=port)
