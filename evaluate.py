#!/usr/bin/env python3
"""
Honest accuracy evaluation for the CS2 predictor.

Performs:
  1. K-fold cross-validation: train calibrator on K-1 folds, test on the held-out fold.
     This is the proper out-of-sample number.
  2. Confusion matrix on held-out predictions.
  3. Brier score (probability quality) and reliability table.
  4. Comparison to baselines:
       - always pick higher-ranked team
       - always pick higher ELO
       - always pick higher recent form

Run: python3 evaluate.py [--folds N] [--save out.json]
"""
from __future__ import annotations
import sqlite3
import sys
import os
import argparse
import json
import math
import pickle
import random
from collections import Counter
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'webapp'))

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cs2_data.db')


def load_test_matches():
    """Pull all top-tier ranked-vs-ranked matches with a known winner."""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    ranked = {r['team'].lower() for r in db.execute('SELECT team FROM team_rankings').fetchall()}
    rows = db.execute(
        "SELECT id, team1, team2, score1, score2, winner, event_tier, match_date "
        "FROM matches "
        "WHERE winner IS NOT NULL AND event_tier IN ('major','tier1','tier2') "
        "ORDER BY id"
    ).fetchall()
    db.close()

    matches = []
    for r in rows:
        t1, t2 = r['team1'], r['team2']
        if not t1 or not t2:
            continue
        if t1.lower() not in ranked or t2.lower() not in ranked:
            continue
        matches.append(dict(r))
    return matches


def baseline_higher_rank(t1, t2, db_lookup):
    r1 = db_lookup.get(t1.lower())
    r2 = db_lookup.get(t2.lower())
    if r1 is None or r2 is None:
        return None
    return t1 if r1 < r2 else t2  # lower rank number = better team


def baseline_higher_elo(t1, t2, elo_lookup):
    e1 = elo_lookup.get(t1.lower(), 1500)
    e2 = elo_lookup.get(t2.lower(), 1500)
    return t1 if e1 >= e2 else t2


def run_predictions(matches):
    """Predict every match without applying the calibrator (raw model probability).
    Calibrator is applied during fold evaluation, not here."""
    # Patch the calibrator to a no-op so run_prediction returns RAW probability
    import importlib.util
    spec = importlib.util.spec_from_file_location('webapp_app', os.path.join('webapp','app.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Override calibrator
    orig_calibrate = mod.calibrate
    mod.calibrate = lambda p: (p, 100 - p)  # no-op pass-through

    raw = []
    for i, m in enumerate(matches, 1):
        if i % 25 == 0:
            print(f'  predicting {i}/{len(matches)} ...', flush=True)
        try:
            pred = mod.run_prediction(m['team1'], m['team2'], 'bo3')
            raw_p1 = pred['team1']['prob'] / 100
        except Exception as e:
            raw_p1 = None
        winner_t1 = 1 if (m['winner'] or '').lower() == m['team1'].lower() else 0
        raw.append({**m, 'raw_p1': raw_p1, 'winner_t1': winner_t1})

    mod.calibrate = orig_calibrate
    return raw


def kfold_indices(n, k, seed=7):
    idx = list(range(n))
    random.Random(seed).shuffle(idx)
    folds = [idx[i::k] for i in range(k)]
    return folds


def evaluate_fold(test_idx, train_idx, raw):
    from sklearn.isotonic import IsotonicRegression
    train = [raw[i] for i in train_idx if raw[i]['raw_p1'] is not None]
    test  = [raw[i] for i in test_idx  if raw[i]['raw_p1'] is not None]
    if len(train) < 10 or not test:
        return None

    iso = IsotonicRegression(out_of_bounds='clip')
    iso.fit([r['raw_p1'] for r in train], [r['winner_t1'] for r in train])

    correct = 0
    brier_sum = 0.0
    confusion = {'tp': 0, 'tn': 0, 'fp': 0, 'fn': 0}
    bins = {b: {'count': 0, 'wins': 0, 'sum_p': 0.0} for b in (10,20,30,40,50,60,70,80,90)}

    for r in test:
        cal = float(iso.predict([r['raw_p1']])[0])
        cal = max(min(cal, 0.95), 0.05)
        pred_t1 = cal > 0.5
        actual_t1 = bool(r['winner_t1'])
        if pred_t1 == actual_t1:
            correct += 1
        # Confusion: positive = "team1 wins"
        if pred_t1 and actual_t1: confusion['tp'] += 1
        elif pred_t1 and not actual_t1: confusion['fp'] += 1
        elif not pred_t1 and not actual_t1: confusion['tn'] += 1
        else: confusion['fn'] += 1
        brier_sum += (cal - r['winner_t1']) ** 2
        bucket = round(cal * 10) * 10
        if bucket in bins:
            bins[bucket]['count'] += 1
            bins[bucket]['wins'] += r['winner_t1']
            bins[bucket]['sum_p'] += cal

    return {
        'n': len(test),
        'correct': correct,
        'accuracy': correct / len(test),
        'brier': brier_sum / len(test),
        'confusion': confusion,
        'reliability': bins,
    }


def baseline_eval(matches, db_lookup, elo_lookup):
    rank_correct = elo_correct = always_t1 = 0
    n = 0
    for m in matches:
        if not m['winner']:
            continue
        n += 1
        winner_t1 = (m['winner'] or '').lower() == m['team1'].lower()
        rank_pick = baseline_higher_rank(m['team1'], m['team2'], db_lookup)
        elo_pick  = baseline_higher_elo(m['team1'], m['team2'], elo_lookup)
        if rank_pick and rank_pick.lower() == m['winner'].lower():
            rank_correct += 1
        if elo_pick and elo_pick.lower() == m['winner'].lower():
            elo_correct += 1
        if winner_t1:
            always_t1 += 1
    return {
        'n': n,
        'higher_rank_acc': rank_correct / n if n else 0,
        'higher_elo_acc':  elo_correct / n if n else 0,
        'always_team1':    always_t1 / n if n else 0,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--folds', type=int, default=5)
    ap.add_argument('--save', default=None, help='Save full results to JSON')
    args = ap.parse_args()

    print(f'\n{"="*70}\n  HONEST OUT-OF-SAMPLE EVALUATION\n{"="*70}\n')
    matches = load_test_matches()
    print(f'Test universe: {len(matches)} top-tier ranked-vs-ranked matches '
          f'(majors + tier1 + tier2, both teams in team_rankings)\n')

    if not matches:
        print('No eligible matches — stopping.')
        return

    print(f'1. Running raw model predictions on all {len(matches)} matches...')
    raw = run_predictions(matches)
    valid = [r for r in raw if r['raw_p1'] is not None]
    print(f'   {len(valid)}/{len(raw)} matches got a prediction\n')

    # Baselines
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    rank_lookup = {r['team'].lower(): r['world_rank'] for r in db.execute(
        'SELECT team, world_rank FROM team_rankings WHERE world_rank IS NOT NULL').fetchall()}
    elo_lookup = {r['team'].lower(): r['elo_rating'] for r in db.execute(
        'SELECT team, elo_rating FROM team_elo').fetchall()}
    db.close()

    base = baseline_eval(matches, rank_lookup, elo_lookup)
    print(f'2. Baselines on the same set:')
    print(f'   Always pick higher-ranked team: {base["higher_rank_acc"]*100:5.1f}%')
    print(f'   Always pick higher-ELO team:    {base["higher_elo_acc"]*100:5.1f}%')
    print(f'   Always pick team1 (control):    {base["always_team1"]*100:5.1f}%\n')

    # K-fold
    print(f'3. {args.folds}-fold cross-validation (train calibrator on K-1 folds, test on held-out fold):')
    folds = kfold_indices(len(valid), args.folds)
    fold_results = []
    for i, test_idx in enumerate(folds, 1):
        train_idx = [j for j in range(len(valid)) if j not in set(test_idx)]
        r = evaluate_fold(test_idx, train_idx, valid)
        if r:
            fold_results.append(r)
            print(f'   Fold {i}: n={r["n"]:4d}  acc={r["accuracy"]*100:5.1f}%  brier={r["brier"]:.4f}')

    if not fold_results:
        print('No usable folds — stopping.')
        return

    # Aggregate
    total_n = sum(r['n'] for r in fold_results)
    total_correct = sum(r['correct'] for r in fold_results)
    pooled_acc = total_correct / total_n
    pooled_brier = sum(r['brier'] * r['n'] for r in fold_results) / total_n
    pooled_conf = Counter()
    pooled_bins = {b: {'count': 0, 'wins': 0, 'sum_p': 0.0} for b in (10,20,30,40,50,60,70,80,90)}
    for r in fold_results:
        for k, v in r['confusion'].items():
            pooled_conf[k] += v
        for b, vals in r['reliability'].items():
            pooled_bins[b]['count'] += vals['count']
            pooled_bins[b]['wins']  += vals['wins']
            pooled_bins[b]['sum_p'] += vals['sum_p']

    print(f'\n   POOLED RESULT (across all {args.folds} folds):')
    print(f'     Accuracy: {total_correct}/{total_n} = {pooled_acc*100:.1f}%')
    print(f'     Brier:    {pooled_brier:.4f}  (random=0.25, perfect=0)')
    tp, tn, fp, fn = pooled_conf['tp'], pooled_conf['tn'], pooled_conf['fp'], pooled_conf['fn']
    print(f'     Confusion (positive = team1 wins):')
    print(f'                       predicted T1   predicted T2')
    print(f'       actual T1           {tp:4d}           {fn:4d}')
    print(f'       actual T2           {fp:4d}           {tn:4d}')
    print(f'     Precision: {tp/(tp+fp)*100:.1f}%  Recall: {tp/(tp+fn)*100:.1f}%')

    print(f'\n4. Reliability — predicted probability vs actual win rate (out-of-sample):')
    print(f'   {"Predicted":>10} {"Actual":>8} {"N":>6}  Calibration')
    print(f'   {"-"*38}')
    for b in sorted(pooled_bins.keys()):
        v = pooled_bins[b]
        if v['count'] < 3:
            continue
        avg_p = v['sum_p'] / v['count'] * 100
        actual = v['wins'] / v['count'] * 100
        gap = abs(avg_p - actual)
        marker = '✓' if gap < 5 else '~' if gap < 10 else '✗'
        print(f'   {avg_p:>9.1f}% {actual:>7.1f}% {v["count"]:>6d}  {marker}  '
              f'{"BAR " + "█" * int(actual/5):<22}')

    print(f'\n{"="*70}')
    print(f'HEADLINE: model accuracy {pooled_acc*100:.1f}%')
    print(f'   vs baseline (higher rank): {base["higher_rank_acc"]*100:.1f}%')
    print(f'   improvement: +{(pooled_acc - base["higher_rank_acc"])*100:.1f} pts')
    print(f'   Brier {pooled_brier:.3f} (lower=better; 0.25 = random guessing)')
    print(f'{"="*70}\n')

    if args.save:
        with open(args.save, 'w') as f:
            json.dump({
                'timestamp': datetime.now().isoformat(timespec='seconds'),
                'n_matches': total_n,
                'pooled_accuracy': pooled_acc,
                'pooled_brier': pooled_brier,
                'confusion': dict(pooled_conf),
                'baselines': base,
                'reliability': pooled_bins,
                'fold_results': [{k: v for k, v in r.items() if k != 'reliability'} for r in fold_results],
            }, f, indent=2)
        print(f'Saved JSON to {args.save}')


if __name__ == '__main__':
    main()
