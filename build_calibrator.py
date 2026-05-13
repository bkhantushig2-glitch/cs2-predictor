#!/usr/bin/env python3
"""Train isotonic regression calibrator on historical match data."""

import sqlite3
import pickle
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sklearn.isotonic import IsotonicRegression

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cs2_data.db')
CALIBRATOR_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'calibrator.pkl')


def build():
    from webapp.app import run_prediction

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    ranked = db.execute('SELECT team FROM team_rankings').fetchall()
    ranked_names = {r['team'].lower() for r in ranked}

    # Train only on top-tier matches (majors, tier1, tier2) — excludes qualifier noise
    matches = db.execute(
        "SELECT team1, team2, winner FROM matches "
        "WHERE winner IS NOT NULL AND event_tier IN ('major', 'tier1', 'tier2')"
    ).fetchall()

    raw_probs = []
    actuals = []
    skipped = 0

    for m in matches:
        t1, t2, winner = m['team1'], m['team2'], m['winner']
        if t1.lower() not in ranked_names or t2.lower() not in ranked_names:
            skipped += 1
            continue
        try:
            pred = run_prediction(t1, t2, 'bo3')
            p1 = pred['team1']['prob'] / 100
            did_t1_win = 1 if winner.lower() == t1.lower() else 0
            raw_probs.append(p1)
            actuals.append(did_t1_win)
        except Exception as e:
            skipped += 1
            continue

    db.close()

    print(f"\nCollected {len(raw_probs)} ranked-vs-ranked matches (skipped {skipped})")

    if len(raw_probs) < 20:
        print("Not enough data to calibrate (need 20+). Exiting.")
        return

    iso = IsotonicRegression(out_of_bounds='clip')
    iso.fit(raw_probs, actuals)

    with open(CALIBRATOR_PATH, 'wb') as f:
        pickle.dump(iso, f)
    print(f"Saved calibrator to {CALIBRATOR_PATH}")

    # Print calibration table
    print(f"\n{'Raw Prob':>10} {'Calibrated':>12} {'Actual Win%':>12} {'Count':>6}")
    print("-" * 44)

    bins = {}
    for rp, actual in zip(raw_probs, actuals):
        bucket = round(rp * 10) * 10  # 0,10,20,...,100
        if bucket not in bins:
            bins[bucket] = {'wins': 0, 'total': 0, 'raw_sum': 0}
        bins[bucket]['wins'] += actual
        bins[bucket]['total'] += 1
        bins[bucket]['raw_sum'] += rp

    for bucket in sorted(bins.keys()):
        b = bins[bucket]
        avg_raw = b['raw_sum'] / b['total']
        actual_wr = b['wins'] / b['total'] * 100
        calibrated = iso.predict([avg_raw])[0] * 100
        print(f"{avg_raw*100:>9.1f}% {calibrated:>10.1f}% {actual_wr:>10.1f}% {b['total']:>6}")

    print("\nDone! Restart the webapp to use calibrated probabilities.")


if __name__ == '__main__':
    build()
