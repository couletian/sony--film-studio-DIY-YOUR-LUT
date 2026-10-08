#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cube2preset.py - convert standard .cube look LUTs into preset JSON that can be
imported into `assets/lut-designer.html` (or fed straight to `inject_lut.py`).

WHY THIS EXISTS
    The camera ISP cannot load a LUT file. It accepts only
        y = curve( clip( matrix . x , 0 , 1 ) )
    a 3x3 matrix (9 ints at x1024) plus ONE shared 1024-point curve. A 3D LUT
    generally cannot be expressed in that model, so conversion is a *fit*, and
    a fit has to be measured, not assumed.

    `inject_lut.py` can already take a `.cube` directly. This tool exists for
    the cases where you want to
      - inspect and keep the fitted numbers as a reusable JSON recipe,
      - load the result in the visual designer and tweak it by hand,
      - convert a whole folder in one pass and get a QC report per file,
      - see the fit error BEFORE anything is written into an APK.

WHAT IT GUARANTEES
    Every emitted file is validated against the exact schema `inject_lut.py`
    enforces, and every fit is scored with a pure-channel sanity test that
    catches an axis-order mistake - the one failure mode that leaves the
    numbers looking perfectly healthy.

USAGE
    # one file, JSON lands next to the cube
    python cube2preset.py path/to/look.cube

    # a whole folder -> one JSON per cube + a combined library
    python cube2preset.py --cubes path/to/cubes --out path/to/profiles

    # set the menu label explicitly
    python cube2preset.py look.cube --name "我的风格"

    # quiet: only the final table
    python cube2preset.py look.cube -q
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------- constants --

# The camera ISP's fixed-point domain, and the schema limits inject_lut.py
# enforces on load. Keep these in step with inject_lut.py:load_json_presets.
MATRIX_SCALE = 1024
MATRIX_MIN, MATRIX_MAX = -2048, 3072
GAMMA_LEN = 1024
GAMMA_MAX = 1023

# QC thresholds. These are judgement calls, stated so a reader can disagree.
#   FIT_*      : how far the model may stray from the source LUT
#   CHANNEL_*  : how pure a pure-channel response must stay
FIT_MEAN_WARN = 0.020      # mean |delta| in 0..1 units
FIT_MEAN_FAIL = 0.060
FIT_DE_WARN = 6.0          # mean delta-E76
FIT_DE_FAIL = 12.0
CHANNEL_PURITY = 0.25      # allowed leak into the "wrong" channels


# ------------------------------------------------------------------ parsing --

def read_cube(path: Path):
    """Parse a .cube file.

    Returns (title, size, cube, domain) where cube is (n, n, n, 3) in 0..1 and
    axes are [blue][green][red] - i.e. red varies fastest, as the spec says.

    Anything that would silently corrupt the fit is a hard error here rather
    than a warning, because a mis-parsed LUT still produces plausible numbers.
    """
    size = None
    title = None
    dmin = dmax = None
    rows: list[list[float]] = []
    bad: list[str] = []

    for lineno, raw in enumerate(path.read_text(encoding='utf-8',
                                                errors='replace').splitlines(), 1):
        s = raw.strip()
        if not s or s.startswith('#'):
            continue
        head = s.split()[0].upper()
        if head == 'TITLE':
            m = re.search(r'"([^"]*)"', s)
            title = m.group(1) if m else s.split(None, 1)[1].strip()
            continue
        if head == 'LUT_3D_SIZE':
            try:
                size = int(s.split()[1])
            except (IndexError, ValueError):
                raise SystemExit('%s:%d: malformed LUT_3D_SIZE' % (path, lineno))
            continue
        if head == 'LUT_1D_SIZE':
            raise SystemExit('%s: 1D LUTs are not supported (need LUT_3D_SIZE)' % path)
        if head in ('DOMAIN_MIN', 'DOMAIN_MAX'):
            vals = [float(x) for x in s.split()[1:4]]
            if len(vals) != 3:
                raise SystemExit('%s:%d: %s needs 3 numbers' % (path, lineno, head))
            if head == 'DOMAIN_MIN':
                dmin = vals
            else:
                dmax = vals
            continue
        if head in ('LUT_3D_INPUT_RANGE',):
            vals = [float(x) for x in s.split()[1:3]]
            dmin, dmax = (vals + [None, None])[:2], None
            continue
        # anything else must be three numbers
        parts = s.split()
        if len(parts) != 3:
            bad.append('%d' % lineno)
            continue
        try:
            rows.append([float(x) for x in parts])
        except ValueError:
            bad.append('%d' % lineno)

    if bad:
        raise SystemExit('%s: %d line(s) are not 3 numbers (e.g. line %s)'
                         % (path, len(bad), bad[0]))
    if size is None:
        raise SystemExit('%s: no LUT_3D_SIZE' % path)
    if size < 2:
        raise SystemExit('%s: LUT_3D_SIZE %d is too small' % (path, size))
    if len(rows) != size ** 3:
        raise SystemExit('%s: LUT_3D_SIZE %d needs %d rows, found %d'
                         % (path, size, size ** 3, len(rows)))

    if dmin is not None or dmax is not None:
        for v in (dmin or [0, 0, 0]) + (dmax or [1, 1, 1]):
            if abs(v - 0.0) > 1e-9 and abs(v - 1.0) > 1e-9:
                raise SystemExit('%s: only unit-domain cubes are supported '
                                 '(DOMAIN_MIN=0, DOMAIN_MAX=1)' % path)

    flat = np.asarray(rows, dtype=np.float64)
    # spec: red varies fastest -> last axis is red
    return title, size, flat.reshape(size, size, size, 3), (dmin, dmax)


# -------------------------------------------------------------------- model --

def sample_cube(cube: np.ndarray, rgb) -> np.ndarray:
    """Trilinear interpolation of the LUT at rgb (N,3), model-space.

    cube is indexed [blue][green][red]; rgb is (r, g, b).
    """
    n = cube.shape[0]
    p = np.clip(np.asarray(rgb, dtype=np.float64), 0.0, 1.0) * (n - 1)
    lo = np.minimum(p.astype(int), n - 2)
    f = p - lo
    out = np.zeros((p.shape[0], 3), dtype=np.float64)
    for dr in (0, 1):
        for dg in (0, 1):
            for db in (0, 1):
                w = ((f[:, 0] if dr else 1.0 - f[:, 0]) *
                     (f[:, 1] if dg else 1.0 - f[:, 1]) *
                     (f[:, 2] if db else 1.0 - f[:, 2]))
                out += w[:, None] * cube[lo[:, 2] + db, lo[:, 1] + dg, lo[:, 0] + dr]
    return out


def build_curve(cube: np.ndarray) -> np.ndarray:
    """Shared curve, taken from the LUT's neutral diagonal and monotonised.

    The mean over channels (rather than one channel) is what preserves a look
    whose neutrals are deliberately tinted.
    """
    t = np.linspace(0.0, 1.0, GAMMA_LEN)
    gray = sample_cube(cube, np.repeat(t[:, None], 3, axis=1)).mean(axis=1)
    return np.maximum.accumulate(np.clip(gray, 0.0, 1.0))


def fit_preset(cube: np.ndarray, samples: int = 48, free_rows: bool = True):
    """Fit (matrix_int9, gamma_int1024) plus diagnostics.

    Two modes, differing only in how much freedom each matrix row gets. The
    default is `free_rows=True`; the other mode exists for the rare case where a
    guaranteed-neutral grey matters more than fidelity.

    * `free_rows=True` (default) - each output row is solved independently
      (full 9-DOF, no sum constraint). This is the correct default because the
      curve is the *channel mean* of the LUT's neutral diagonal: a row-sum tie
      would map a neutral input to exactly `curve(v)`, i.e. it would mathematically
      erase any deliberate neutral tint. Measured across 22 Leica looks, the free
      solve was better on **every single one** (Sepia dE76 8.33 -> 1.50,
      Blue 8.81 -> 3.76), and it is what reproduces a hand-made reference JSON.
      When the source really is neutral the solve converges near 1024 on its own.

    * `free_rows=False` - rows tied to sum to 1. Keeps a grey card exactly grey at
      the cost of accuracy, and destroys a deliberate tint. Only use it when
      neutral fidelity is a hard requirement.

    Validation: a reference JSON for `Leica_Eternal_17` (row sums [1025,999,1031],
    i.e. not tied) was matched to dE76 0.0003 by `free_rows=True` and only 2.06 by
    the tied solve - direct evidence the untied form is what a hand-built
    reference actually uses.
    """
    t = np.linspace(0.0, 1.0, GAMMA_LEN)
    curve = build_curve(cube)

    # grid to regress on; skip the extreme corners where quantisation dominates
    axis = np.linspace(0.02, 0.98, samples)
    grid = np.array([(r, g, b) for b in axis for g in axis for r in axis],
                    dtype=np.float64)
    target = sample_cube(cube, grid)

    # invert the curve so the matrix only has to explain the hue rotation
    cy, ids = np.unique(curve, return_index=True)
    if cy.size < 8:
        raise SystemExit('the neutral diagonal is nearly flat - this LUT has no '
                         'usable tone curve, so it cannot be fitted')
    linear = np.stack([np.interp(target[:, c], cy, t[ids]) for c in range(3)],
                      axis=1)

    matrix = np.empty((3, 3))
    if free_rows:
        # 9 dof: plain 3x3 least squares, nothing tied. Row sums land wherever
        # the data wants, which is what preserves a deliberate neutral tint.
        for c in range(3):
            matrix[c] = np.linalg.lstsq(grid, linear[:, c], rcond=None)[0]
    else:
        # 6 dof: the "deviation from the blue axis" form guarantees the row sums
        # to 1 algebraically, so greys stay put.
        design = grid[:, :2] - grid[:, 2:3]
        for c in range(3):
            coef = np.linalg.lstsq(design, linear[:, c] - grid[:, 2],
                                   rcond=None)[0]
            matrix[c] = [coef[0], coef[1], 1.0 - coef.sum()]

    matrix_i = np.rint(matrix * MATRIX_SCALE).astype(int)
    curve_i = np.maximum.accumulate(
        np.clip(np.rint(curve * GAMMA_MAX).astype(int), 0, GAMMA_MAX))

    stats = {
        'row_sums': matrix_i.sum(axis=1).tolist(),
        'free_rows': bool(free_rows),
        'neutrals_neutral': all(abs(s - MATRIX_SCALE) <= 2 for s in matrix_i.sum(axis=1)),
        'out_of_range': bool(matrix_i.min() < MATRIX_MIN
                             or matrix_i.max() > MATRIX_MAX),
    }
    return matrix_i, curve_i, stats


def apply_preset(matrix_i, curve_i, rgb) -> np.ndarray:
    """Run the fitted preset exactly as the camera will."""
    M = np.asarray(matrix_i, dtype=np.float64) / MATRIX_SCALE
    c = np.asarray(curve_i, dtype=np.float64) / GAMMA_MAX
    x = np.clip(np.asarray(rgb, dtype=np.float64) @ M.T, 0.0, 1.0)
    return np.interp(x, np.linspace(0.0, 1.0, len(c)), c)


# ---------------------------------------------------------------------- QC --

def srgb_to_lab(rgb01) -> np.ndarray:
    c = np.clip(np.asarray(rgb01, dtype=np.float64), 0.0, 1.0)
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    m = np.array([[0.4124, 0.3576, 0.1805],
                  [0.2126, 0.7152, 0.0722],
                  [0.0193, 0.1192, 0.9505]])
    xyz = lin @ m.T / np.array([0.95047, 1.0, 1.08883])
    e, k = 216 / 24389, 24389 / 27
    f = np.where(xyz > e, np.cbrt(np.maximum(xyz, 1e-12)), (k * xyz + 16) / 116)
    return np.stack([116 * f[..., 1] - 16,
                     500 * (f[..., 0] - f[..., 1]),
                     200 * (f[..., 1] - f[..., 2])], axis=-1)


def check_axis_order(cube: np.ndarray, name: str):
    """Decide whether this file is laid out red-fastest or red-slowest.

    Why not simpler tests:

    * "pure red in must come out red" is false for a monochrome or toned look
      (Leica Sepia sends pure red to a near-neutral warm grey on purpose), so it
      cries wolf on the looks it should accept.
    * Comparing fit residuals between the two readings does not work either -
      the regression design is invariant under a red/blue swap, so the two
      residuals come out BIT-IDENTICAL and the test can never fire.

    What does work is the SHAPE OF THE FITTED MATRIX. A well-formed look has a
    positive diagonal and each output row dominated by its own input channel. A
    swapped file cannot satisfy that: fitting it drives the red row onto the
    blue column (and vice versa) and typically pushes the diagonal negative.
    That signature is independent of how saturated the look is.
    """
    def matrix_of(c):
        try:
            m, _, _ = fit_preset(c, samples=24)
        except SystemExit:
            return None
        return np.asarray(m, dtype=np.float64)

    def score(c):
        """Lower is better: penalise a negative diagonal and off-diagonal
        dominance. A clean layout scores ~0, a swapped one scores high."""
        m = matrix_of(c)
        if m is None:
            return float('inf'), None
        diag = np.array([m[i, i] for i in range(3)])
        pen = float(np.sum(np.abs(np.minimum(diag, 0.0))))       # negative diag
        for i in range(3):
            if int(np.argmax(m[i])) != i:
                pen += float(np.max(m[i]) - m[i, i])             # wrong winner
        # a swap specifically swaps the red and blue diagonals
        pen += 0.5 * float(abs(m[0, 2]) + abs(m[2, 0]))
        return pen, m

    s_fast, m_fast = score(cube)
    s_slow, m_slow = score(cube.transpose(2, 1, 0, 3))
    detail = {'penalty_bgr': round(s_fast, 4),
              'penalty_rgb': round(s_slow, 4),
              'diag_bgr': [int(x) for x in np.diag(m_fast)] if m_fast is not None else None,
              'diag_rgb': [int(x) for x in np.diag(m_slow)] if m_slow is not None else None}
    # Require a real margin before claiming a direction. On a near-monochrome
    # look the matrix is close to degenerate and both layouts score badly but
    # almost equally; guessing there would be worse than admitting the tie.
    hi, lo = max(s_fast, s_slow), min(s_fast, s_slow)
    if hi - lo < 0.15 * hi:
        return 'ambiguous', detail
    if s_fast < s_slow:
        return 'red-fastest', detail
    return 'red-slowest', detail


def check_purity(cube, matrix_i, curve_i, samples=16):
    """Does the fitted preset reproduce the SOURCE LUT's channel behaviour?

    The reference is the source LUT, not an abstract "pure red stays red" rule,
    so a monochrome or tinted look passes and a genuine axis mistake does not.

    The comparison is deliberately NOT a bare argmax. On a desaturating look a
    pure input can come out nearly neutral (Leica Blue sends pure B to
    [0.16, 0.18, 0.21]), where blue wins by 0.026 and any rounding flips the
    winner - noise, not an error. Instead we only call a channel WRONG when the
    fit's winner disagrees with the reference AND the reference's own winner is
    clear (a real margin). A near-neutral reference is reported as 'soft' and is
    judged by absolute error alone.
    """
    MARGIN = 0.02          # reference must separate its winner by this much
    out = {}
    for lbl, v in (('R', [1, 0, 0]), ('G', [0, 1, 0]), ('B', [0, 0, 1])):
        ref = sample_cube(cube, np.array([v], dtype=np.float64))[0]
        got = apply_preset(matrix_i, curve_i, [v])[0]
        order = np.argsort(ref)[::-1]
        ref_margin = float(ref[order[0]] - ref[order[1]])
        soft = ref_margin < MARGIN
        agrees = int(np.argmax(got)) == int(np.argmax(ref))
        out[lbl] = {
            'ref': [round(float(x), 4) for x in ref],
            'out': [round(float(x), 4) for x in got],
            'ref_margin': round(ref_margin, 4),
            'soft': bool(soft),
            'argmax_ok': bool(agrees),
            'abs_err': round(float(np.mean(np.abs(got - ref))), 4),
        }
    return out


def measure_fit(cube, matrix_i, curve_i, samples=32):
    """Score the fitted preset against the source LUT, using the QUANTISED
    integers (quantisation error is part of what the camera will show)."""
    axis = np.linspace(0.0, 1.0, samples)
    grid = np.array([(r, g, b) for b in axis for g in axis for r in axis])
    ref = sample_cube(cube, grid)
    got = apply_preset(matrix_i, curve_i, grid)
    d = np.abs(got - ref)
    de = float(np.mean(np.linalg.norm(
        srgb_to_lab(got) - srgb_to_lab(ref), axis=1)))

    # a no-matrix baseline, so the reader can see what the matrix is contributing
    ident = np.rint(np.eye(3) * MATRIX_SCALE).astype(int).tolist()
    base = apply_preset(ident, curve_i, grid)
    de_base = float(np.mean(np.linalg.norm(
        srgb_to_lab(base) - srgb_to_lab(ref), axis=1)))

    return {
        'mean_abs': float(d.mean()),
        'p95_abs': float(np.percentile(d, 95)),
        'max_abs': float(d.max()),
        'de76': de,
        'de76_curve_only': de_base,
    }


def fit_grade(fit, purity) -> tuple[str, list[str]]:
    """Turn numbers into PASS / WARN / FAIL plus human-readable reasons."""
    notes: list[str] = []
    grade = 'PASS'

    def bump(level, msg):
        nonlocal grade
        order = {'PASS': 0, 'WARN': 1, 'FAIL': 2}
        if order[level] > order[grade]:
            grade = level
        notes.append('%s: %s' % (level, msg))

    hard_bad = [k for k, p in purity.items()
                if not p['argmax_ok'] and not p['soft']]
    soft_bad = [k for k, p in purity.items()
                if not p['argmax_ok'] and p['soft']]
    if hard_bad:
        bump('FAIL', 'the fit sends pure %s to the wrong dominant channel '
                     '(source LUT argues otherwise) - the matrix is wrong, '
                     'most likely an axis-order problem'
             % '/'.join(sorted(hard_bad)))
    elif soft_bad:
        bump('WARN', 'pure %s comes out nearly neutral in the source LUT, so the '
                     'dominant-channel check is unreliable there - judge this one '
                     'by the fit error instead'
             % '/'.join(sorted(soft_bad)))
    else:
        worst = max(p['abs_err'] for p in purity.values())
        if worst > CHANNEL_PURITY:
            bump('WARN', 'pure-channel error %.3f exceeds %.2f - the fitted look '
                         'deviates on saturated colours'
                 % (worst, CHANNEL_PURITY))

    if fit['de76'] > FIT_DE_FAIL or fit['mean_abs'] > FIT_MEAN_FAIL:
        bump('FAIL', 'fit error too large (dE76 %.2f, mean |d| %.4f); this look '
                     'depends on per-hue changes a 3x3 matrix cannot express'
             % (fit['de76'], fit['mean_abs']))
    elif fit['de76'] > FIT_DE_WARN or fit['mean_abs'] > FIT_MEAN_WARN:
        bump('WARN', 'approximate fit (dE76 %.2f, mean |d| %.4f) - usable, but '
                     'expect visible deviation on some colours'
             % (fit['de76'], fit['mean_abs']))

    if fit['de76'] >= fit['de76_curve_only']:
        bump('WARN', 'the fitted matrix does not beat a curve-only fit '
                     '(dE76 %.2f vs %.2f); this LUT is close to a tone curve'
             % (fit['de76'], fit['de76_curve_only']))

    return grade, notes


def validate_preset(preset) -> list[str]:
    """Re-check the emitted object against the schema inject_lut.py enforces."""
    errs = []
    for key in ('id', 'name', 'matrix', 'gamma'):
        if key not in preset:
            errs.append('missing "%s"' % key)
    if errs:
        return errs
    m, g = preset['matrix'], preset['gamma']
    if len(g) != GAMMA_LEN:
        errs.append('gamma has %d points, needs %d' % (len(g), GAMMA_LEN))
    if any(not (0 <= v <= GAMMA_MAX) for v in g):
        errs.append('gamma values outside 0..%d' % GAMMA_MAX)
    if any(a > b for a, b in zip(g, g[1:])):
        errs.append('gamma is not non-decreasing')
    if len(m) != 3 or any(len(r) != 3 for r in m):
        errs.append('matrix is not 3x3')
    else:
        flat = [v for row in m for v in row]
        if any(not (MATRIX_MIN <= v <= MATRIX_MAX) for v in flat):
            errs.append('matrix values outside %d..%d' % (MATRIX_MIN, MATRIX_MAX))
    if not isinstance(preset['id'], str) or not preset['id']:
        errs.append('id must be a non-empty string')
    return errs


# ------------------------------------------------------------------ naming --

def slug(s: str) -> str:
    """ASCII-safe, unique-ish id derived from the file name."""
    out = re.sub(r'[^A-Za-z0-9]+', '-', s).strip('-').lower()
    return out or 'custom'


def default_name(title, stem, strip_suffixes) -> str:
    """Menu label: prefer the cube's own TITLE, minus noise words."""
    base = title or stem
    for suf in strip_suffixes:
        base = re.sub(re.escape(suf) + r'$', '', base, flags=re.I)
    base = re.sub(r'[_\s]+', ' ', base).strip()
    return base or stem


# ------------------------------------------------------------------- driver --

def convert_one(path: Path, args) -> dict:
    """Convert a single cube. Returns a result record (never raises for QC
    findings - those are recorded so the caller can report all of them)."""
    rec = {'file': path.name, 'path': str(path), 'grade': 'FAIL', 'errors': []}
    try:
        title, size, cube, domain = read_cube(path)
    except SystemExit as exc:
        rec['errors'].append(str(exc))
        return rec

    rec['size'] = size
    rec['title'] = title
    rec['domain'] = [domain[0] or [0, 0, 0], domain[1] or [1, 1, 1]]

    # --- QC 1: axis order ---------------------------------------------------
    verdict, detail = check_axis_order(cube, path.name)
    rec['axis'] = verdict
    rec['axis_detail'] = detail
    if verdict == 'red-slowest':
        # the file is written the other way round; transpose so the rest of the
        # pipeline sees the spec layout, and record that we did.
        cube = cube.transpose(2, 1, 0, 3)
        rec['axis_fixed'] = True
    else:
        rec['axis_fixed'] = False

    # --- fit ----------------------------------------------------------------
    matrix_i, curve_i, stats = fit_preset(cube, samples=args.samples,
                                          free_rows=not args.tie_rows)

    # --- QC 2: fit error ----------------------------------------------------
    fit = measure_fit(cube, matrix_i, curve_i, samples=args.qc_samples)
    rec['fit'] = fit

    # --- QC 3: pure-channel sanity ------------------------------------------
    purity = check_purity(cube, matrix_i, curve_i)
    rec['purity'] = purity

    # --- QC 4: schema -------------------------------------------------------
    preset_id = args.id or slug(Path(path).stem)
    name = args.name or default_name(title, path.stem, args.strip_suffix)
    preset = {
        'id': preset_id,
        'name': name,
        'family': 'custom',
        'guide': ('Fitted from the 3D LUT "%s" onto the camera model '
                  '(3x3 matrix + one shared curve). This is a close approximation, '
                  'not a point-for-point reproduction.'
                  % (title or path.name)),
        'source_file': path.name,
        'source_size': size,
        'matrix': matrix_i.tolist(),
        'gamma': curve_i.tolist(),
    }
    schema_errs = validate_preset(preset)
    rec['schema_errors'] = schema_errs

    grade, notes = fit_grade(fit, purity)
    if schema_errs:
        grade = 'FAIL'
        notes += ['FAIL: schema: %s' % e for e in schema_errs]
    if stats['out_of_range']:
        grade = 'FAIL'
        notes.append('FAIL: matrix values outside the accepted %d..%d range'
                     % (MATRIX_MIN, MATRIX_MAX))
    rec['grade'] = grade
    rec['notes'] = notes
    rec['preset'] = preset
    return rec


def print_one(rec: dict, verbose: bool):
    f = rec.get('fit') or {}
    print('%-44s %-6s %s' % (rec['file'],
                             '%d^3' % rec['size'] if rec.get('size') else '-',
                             rec['grade']))
    if rec['errors']:
        for e in rec['errors']:
            print('    ERROR  %s' % e)
        return
    if rec['axis_fixed']:
        print('    note   file is stored red-slowest; transposed to the '
              'red-fastest layout before fitting')
    elif rec['axis'] == 'ambiguous':
        print('    warn   axis order could not be determined (near-neutral LUT) '
              '- assumed red-fastest')
    if f:
        print('    fit    mean|d| %.4f  p95 %.4f  max %.4f   dE76 %.2f '
              '(curve-only baseline %.2f)'
              % (f['mean_abs'], f['p95_abs'], f['max_abs'], f['de76'],
                 f['de76_curve_only']))
    pur = rec.get('purity') or {}
    if pur:
        print('    purity R->%s  G->%s  B->%s'
              % (pur['R']['out'], pur['G']['out'], pur['B']['out']))
    if verbose:
        p = rec['preset']
        print('    id     %s' % p['id'])
        print('    name   %s' % p['name'])
        print('    matrix %s  (row sums %s)'
              % (p['matrix'], [sum(r) for r in p['matrix']]))
    for n in rec.get('notes') or []:
        print('    %s' % n)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('cube', nargs='?', type=Path,
                    help='a single .cube file')
    ap.add_argument('--cubes', type=Path,
                    help='a folder of .cube files (mutually exclusive with the '
                         'positional argument)')
    ap.add_argument('--out', type=Path,
                    help='output folder (default: next to the input)')
    ap.add_argument('--id', help='preset id (single-file mode only)')
    ap.add_argument('--name', help='menu label (single-file mode only)')
    ap.add_argument('--strip-suffix', action='append', default=[],
                    metavar='TEXT',
                    help='trim this trailing word from the derived name; '
                         'repeatable (e.g. --strip-suffix _33)')
    ap.add_argument('--samples', type=int, default=48,
                    help='regression grid resolution per axis (default 48)')
    ap.add_argument('--qc-samples', type=int, default=32,
                    help='QC grid resolution per axis (default 32)')
    ap.add_argument('--tie-rows', action='store_true',
                    help='tie each matrix row to sum to 1 (6-DOF) so neutrals stay '
                         'exactly neutral. Off by default: the free 9-DOF solve is '
                         'more accurate and is required to keep a deliberately '
                         'tinted neutral, which tying would erase. Use this only '
                         'when a guaranteed-grey grey is a hard requirement.')
    ap.add_argument('--no-combined', action='store_true',
                    help='in folder mode, skip the combined library JSON')
    ap.add_argument('--combined-name', default='luts_all_profiles.json',
                    help='file name for the combined library')
    ap.add_argument('--allow-fail', action='store_true',
                    help='write outputs even when a QC check failed')
    ap.add_argument('-q', '--quiet', action='store_true',
                    help='only print the summary table')
    args = ap.parse_args(argv)

    if bool(args.cube) == bool(args.cubes):
        ap.error('give exactly one of: a .cube file, or --cubes <folder>')

    if args.cubes:
        targets = sorted(p for p in args.cubes.glob('*.cube'))
        if not targets:
            ap.error('no .cube files under %s' % args.cubes)
        out_dir = args.out or args.cubes
        if args.name or args.id:
            ap.error('--name/--id only apply to a single file')
    else:
        targets = [args.cube]
        if not targets[0].is_file():
            ap.error('no such file: %s' % targets[0])
        out_dir = args.out or targets[0].parent
        # a single file with --name/--id may also carry a custom output name
    out_dir.mkdir(parents=True, exist_ok=True)

    print('=' * 78)
    print('cube -> preset conversion')
    print('  inputs : %s' % (args.cubes if args.cubes else args.cube))
    print('  output : %s' % out_dir)
    print('=' * 78)

    results = []
    for p in targets:
        rec = convert_one(p, args)
        results.append(rec)
        if not args.quiet:
            print_one(rec, verbose=True)
            print()

    # --- write --------------------------------------------------------------
    written = []
    skipped = []
    for rec in results:
        if rec['errors'] or rec['grade'] == 'FAIL':
            if not args.allow_fail:
                skipped.append(rec['file'])
                continue
        if 'preset' not in rec:
            skipped.append(rec['file'])
            continue
        payload = {
            'schema': 1,
            'version': 'camera-lut-studio-cube2preset',
            'note': ('Fitted from %s by cube2preset.py. The camera model is a 3x3 '
                     'matrix plus one shared 1024-point curve, so this reproduces '
                     'the look closely but not exactly.'
                     % rec['file']),
            'presets': [rec['preset']],
        }
        dest = out_dir / (Path(rec['file']).stem + '.json')
        dest.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                        encoding='utf-8')
        written.append(dest)

    combined = None
    if args.cubes and not args.no_combined:
        good = [r['preset'] for r in results
                if 'preset' in r and r['grade'] != 'FAIL']
        if good:
            combined = out_dir / args.combined_name
            combined.write_text(json.dumps(
                {'schema': 1, 'version': 'camera-lut-studio-cube2preset',
                 'note': ('Combined library of %d looks fitted by cube2preset.py.'
                          % len(good)),
                 'presets': good}, ensure_ascii=False, indent=1),
                encoding='utf-8')

    # --- report -------------------------------------------------------------
    print('=' * 78)
    print('QC summary')
    print('=' * 78)
    print('%-44s %-6s %8s %8s %7s  %s'
          % ('source .cube', 'size', 'mean|d|', 'p95|d|', 'dE76', 'grade'))
    print('-' * 78)
    for rec in results:
        f = rec.get('fit') or {}
        print('%-44s %-6s %8s %8s %7s  %s'
              % (rec['file'],
                 '%d^3' % rec['size'] if rec.get('size') else '-',
                 '%.4f' % f['mean_abs'] if f else '-',
                 '%.4f' % f['p95_abs'] if f else '-',
                 '%.2f' % f['de76'] if f else '-',
                 rec['grade']))
    print('-' * 78)

    n_fail = sum(1 for r in results if r['grade'] == 'FAIL')
    n_warn = sum(1 for r in results if r['grade'] == 'WARN')
    n_pass = sum(1 for r in results if r['grade'] == 'PASS')
    print('%d file(s): %d PASS, %d WARN, %d FAIL' % (len(results), n_pass,
                                                     n_warn, n_fail))
    print()
    for d in written:
        print('  wrote %s' % d)
    if combined:
        print('  wrote %s  (%d presets)' % (combined, len(json.loads(
            combined.read_text(encoding='utf-8'))['presets'])))
    for s in skipped:
        print('  SKIPPED %s (QC failed; re-run with --allow-fail to write it '
              'anyway)' % s)

    print()
    print('Next: import a JSON into assets/lut-designer.html, or inject it with')
    print('      python scripts/inject_lut.py --apk <apk> --lut <this.json> --out <out.apk>')
    print()
    if n_fail:
        print('At least one conversion FAILED QC. Do not inject a FAILED look '
              'without reading its notes:')
        for rec in results:
            if rec['grade'] == 'FAIL':
                print('  %s' % rec['file'])
                for n in rec.get('notes') or []:
                    print('      %s' % n)
    return 1 if n_fail and not args.allow_fail else 0


if __name__ == '__main__':
    sys.exit(main())
