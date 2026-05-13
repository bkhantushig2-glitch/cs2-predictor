#!/usr/bin/env python3
"""
PandaScore API integration for CS2 betting odds
Free tier: 1000 requests/hour
Set your API key: export PANDASCORE_API_KEY="your_key_here"
"""

import requests
import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cs2_data.db')
API_KEY = os.environ.get('PANDASCORE_API_KEY', '')
BASE_URL = 'https://api.pandascore.co'


def fetch_upcoming_odds():
    """Get odds for upcoming CS2 matches from PandaScore"""
    if not API_KEY:
        return []

    headers = {'Authorization': f'Bearer {API_KEY}'}
    url = f'{BASE_URL}/csgo/matches/upcoming'
    params = {'per_page': 20, 'sort': 'begin_at'}

    try:
        response = requests.get(url, headers=headers, params=params, timeout=15)
        if response.status_code != 200:
            print(f"PandaScore error: {response.status_code}")
            return []

        matches = response.json()
        odds_data = []

        for match in matches:
            opponents = match.get('opponents', [])
            if len(opponents) < 2:
                continue

            team1 = opponents[0].get('opponent', {}).get('name', '')
            team2 = opponents[1].get('opponent', {}).get('name', '')
            match_date = match.get('begin_at', '')
            event = match.get('league', {}).get('name', '')

            # Get odds from games or streams_list
            team1_odds = 0
            team2_odds = 0

            # PandaScore sometimes includes odds in match data
            if match.get('odds') and len(match['odds']) >= 2:
                team1_odds = match['odds'][0].get('value', 0)
                team2_odds = match['odds'][1].get('value', 0)

            odds_data.append({
                'team1': team1,
                'team2': team2,
                'team1_odds': team1_odds,
                'team2_odds': team2_odds,
                'match_date': match_date,
                'event': event
            })

        return odds_data

    except Exception as e:
        print(f"PandaScore fetch error: {e}")
        return []


def save_odds(odds_data):
    """Save odds to database"""
    if not odds_data:
        return 0

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    count = 0

    for odds in odds_data:
        try:
            c.execute('''
                INSERT OR REPLACE INTO betting_odds
                (team1, team2, team1_odds, team2_odds, bookmaker, match_date, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            ''', (odds['team1'], odds['team2'], odds['team1_odds'],
                  odds['team2_odds'], 'pandascore', odds['match_date']))
            count += 1
        except:
            pass

    conn.commit()
    conn.close()
    return count


def fetch_and_save():
    """Fetch odds and save to DB"""
    if not API_KEY:
        return 0
    odds = fetch_upcoming_odds()
    return save_odds(odds)


def get_match_odds(team1, team2):
    """Get stored odds for a matchup"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT team1, team2, team1_odds, team2_odds FROM betting_odds
        WHERE (team1 LIKE ? AND team2 LIKE ?)
           OR (team1 LIKE ? AND team2 LIKE ?)
        ORDER BY fetched_at DESC LIMIT 1
    ''', (f'%{team1}%', f'%{team2}%', f'%{team2}%', f'%{team1}%'))
    row = c.fetchone()
    conn.close()
    return row


if __name__ == "__main__":
    if not API_KEY:
        print("Set your API key first:")
        print("  export PANDASCORE_API_KEY=\"your_key_here\"")
        print("\nSign up free at: https://pandascore.co")
    else:
        print("Fetching CS2 odds from PandaScore...")
        odds = fetch_upcoming_odds()
        if odds:
            print(f"\nFound {len(odds)} upcoming matches:")
            for o in odds:
                print(f"  {o['team1']} vs {o['team2']} ({o['event']})")
                if o['team1_odds'] > 0:
                    print(f"    Odds: {o['team1_odds']:.2f} / {o['team2_odds']:.2f}")
            saved = save_odds(odds)
            print(f"\nSaved {saved} odds to database")
        else:
            print("No upcoming matches found")
