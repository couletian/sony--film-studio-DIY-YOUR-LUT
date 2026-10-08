# Turning a `.cube` look LUT into designer JSON

Everything here is about one job: take a normal 3D LUT that you got from anywhere
(a camera vendor app, a colourist, a film-simulation pack) and turn it into the
preset JSON that `assets/lut-designer.html` imports and `scripts/inject_lut.py`
accepts.

The tool is `scripts/cube2preset.py`. Run it **instead of** hand-fitting, because
a 3D LUT almost never maps exactly onto the camera's model, and the whole point
of this workflow is to *measure* how far off the fit is before anything reaches
an APK.

## Why a separate step exists

`inject_lut.py` can already take a `.cube` directly:

```sh
python scripts/inject_lut.py --apk <built.apk> --lut look.cube --out out.apk
```

That is fine for a quick one-off. Add `cube2preset.py` when you want to:

- **see the fitted numbers** as a reusable JSON recipe you can re-inject later;
- **open the result in the visual designer** and nudge it by hand;
- **batch a whole folder** and get a QC verdict per file;
- **know the fit error before** writing anything into an APK.

The two tools share the same model and produce the same shape of output, so a
`cube2preset.py` result can be fed straight to `inject_lut.py`.

## When the fit is good enough

The camera runs `y = curve(clip(matrix · x, 0, 1))` - a 3x3 matrix acting in the
encoded domain plus **one shared curve for all three channels**. A 3D LUT can
express per-hue changes that this model simply cannot. Some looks fit almost
perfectly; some cannot be represented at all, no matter how much you tune.

Rough guide, from LUTs actually processed:

| Look type | Typical dE76 | Verdict |
|---|---|---|
| Near-identity / toned mono (Sepia, Silver, Chrome) | 1.5-5 | fits well |
| Ordinary film emulation (Classic, Cine, Teal) | 7-10 | approximate but usable |
| Log-conversion LUTs (`LLog_*`) | 20-24 | **cannot** be represented |

Measured over 22 Leica looks, the pipeline lands 6 PASS / 12 WARN / 4 FAIL. The four
failures are all `LLog_*` Log-conversion LUTs, which are the textbook impossible
case: mostly a tone curve plus hue work that a single shared curve cannot carry,
over a deliberately compressed input range. Do not fight them.

If your numbers come out noticeably worse than this table, check the row-sum mode
before blaming the LUT - see *Fitting notes*.

## The workflow

```sh
# one file, JSON written next to the cube
python scripts/cube2preset.py path/to/look.cube

# a whole folder -> one JSON per cube + a combined library
python scripts/cube2preset.py --cubes path/to/cubes --out path/to/profiles \
    --strip-suffix _17 --strip-suffix _33 --strip-suffix _65

# set the menu label and id explicitly
python scripts/cube2preset.py look.cube --name "我的风格" --id my-look
```

Useful flags:

| Flag | Purpose |
|---|---|
| `--cubes <dir>` | convert every `.cube` in a folder (non-recursive) |
| `--out <dir>` | where the JSON goes (default: beside the input) |
| `--id`, `--name` | single-file mode; override the derived id / menu label |
| `--strip-suffix _33` | trim a trailing token from the derived name; repeatable |
| `--samples N` | regression grid per axis (default 48) |
| `--qc-samples N` | QC grid per axis (default 32) |
| `--tie-rows` | tie each matrix row to sum to 1, forcing greys to stay grey. Off by default; see *Fitting notes* for why that is the wrong trade most of the time |
| `--no-combined` | folder mode: skip the merged library file |
| `--allow-fail` | write outputs even when QC failed (see below) |
| `-q` | print only the final table |

Folder mode writes one JSON per input plus `luts_all_profiles.json`, a library you
can inject in one pass. **Failed files are skipped by default** - a FAILED look is
not written unless you pass `--allow-fail`, so a batch cannot silently poison a
library with a broken preset.

## Reading the QC report

Folder mode ends with a table, then explains every non-PASS result:

```
source .cube                      size    mean|d|   p95|d|    dE76  grade
----------------------------------------------------------------------------
Leica_Eternal_17.cube             17^3     0.0149   0.0459    3.38  PASS
Leica_Sepia_Standard_17.cube      17^3     0.0103   0.0289    1.48  PASS
Leica_Classic_Standard_17.cube    17^3     0.0422   0.1513    9.53  WARN
Leica_Blue_Standard_17.cube       17^3     0.0336   0.0852    3.70  WARN
LLog_cine_33.cube                 33^3     0.0929   0.3476   20.33  FAIL
```

- **mean|d|** - mean absolute error per channel, in 0-1 units, measured on the
  **quantised** integers (so it includes the error the camera will actually show).
- **p95|d|** - the 95th percentile; this is what tells you whether the error is
  spread out or concentrated on a few colours. A low mean with a high p95 means
  "fine except for a few saturated hues".
- **dE76** - mean CIELAB colour difference. The scale people actually reason in.
- **grade** - PASS / WARN / FAIL, from the thresholds below.

Thresholds live at the top of the script:

```python
FIT_MEAN_WARN = 0.020      FIT_MEAN_FAIL = 0.060
FIT_DE_WARN   = 6.0        FIT_DE_FAIL   = 12.0
CHANNEL_PURITY = 0.25
```

`FAIL` also reports the **curve-only baseline** - the error you would get with no
matrix at all. If the fitted matrix does not beat that baseline, the matrix is not
contributing and the script says so; the look is essentially a tone curve.

## The trap this whole tool exists to catch

**A `.cube` stores its data with red varying fastest.** After loading a
`LUT_3D_SIZE n` file into a `(n, n, n, 3)` array, the axes are:

```
axis 0 = blue   (slowest)
axis 1 = green
axis 2 = red    (fastest)
```

Get this backwards and you silently swap the red and blue channels. The output
still looks *plausible* - a warm look becomes a cool one - which is why it can go
unnoticed until someone looks at an actual photo.

The file is not necessarily wrong: plenty of real-world `.cube` exports are
written the other way round. The export tutorial that shipped with this project
produced red-slowest cubes. So the tool must **detect** the layout, not assume it.

### Why the obvious tests fail

Both of these seem reasonable and both are wrong:

1. *"Pure red in must come out red."* False for any monochrome or toned look.
   Leica Sepia sends pure red to `[0.358, 0.346, 0.307]` - a near-neutral warm
   grey, on purpose. Asserting the rule makes the guard reject exactly the looks
   it should accept.

2. *"Fit both readings and keep the one with less error."* This can never fire.
   The regression design matrix is **invariant under a red/blue swap**, so the
   two residuals come out **bit-identical**. Verified: `0.044797` for both
   readings of the same file. Looking for a *smaller error* is hopeless here -
   which is why the working test below looks at the *shape of the answer*
   instead of the size of the residual.

### What actually works

The **shape of the fitted matrix**. A correctly-read look has a positive diagonal
and each output row dominated by its own input channel. A swapped file cannot
satisfy that - fitting it drives the red row onto the blue column and usually
pushes the diagonal negative:

```
correct layout      diag = [ 901,  932,  565]   red row peaks on red
swapped layout      diag = [ -67,  932,  167]   red row peaks on BLUE
```

`check_axis_order()` scores both readings with a penalty for a negative diagonal,
an off-diagonal winner, and specific red/blue cross terms. It requires a real
relative margin before claiming a direction; a near-degenerate monochrome look
where both readings score badly and almost equally is reported as `ambiguous`
rather than guessed at.

The margin on a genuine error is large, not marginal. On the Steve McCurry cube
(truly red-slowest) the two readings score `3432` and `34.5` - a hundredfold
separation, with diagonals `[-28, 1066, -41]` against `[1106, 1066, 1090]`. That
is what a real detection looks like; anything close to a tie is a near-monochrome
look that genuinely does not care.

When the verdict is `red-slowest`, the array is transposed before fitting and the
report says so:

```
Leica_Steve_McCurry_33.cube                  33^3   PASS
    note   file is stored red-slowest; transposed to the red-fastest layout before fitting
```

This is not hypothetical. The Steve McCurry cube is genuinely stored red-slowest;
after the automatic correction it fits at **dE76 0.24** with a matrix within
quantisation of the value extracted independently from the APK smali.

## Fitting notes

- The shared curve is taken from the **channel mean of the LUT's neutral
  diagonal**, then monotonised. Using the mean rather than one channel is what
  preserves a look whose greys are deliberately tinted.
- **Matrix rows are deliberately NOT tied to sum to 1024** (the default). Each row
  is solved independently - a full 9-DOF fit - and this matters more than it
  sounds.

  The reason is a direct consequence of the curve definition above. Because the
  curve is the *mean* of the three neutral channels, it has already thrown away
  any per-channel tint in the greys. If the matrix rows were then tied to sum to
  1, a neutral input `v` would map to exactly `curve(v)` - i.e. the tie would
  **mathematically guarantee the tint is erased**, no matter what the source LUT
  does. The two choices are only consistent if the rows are free to put the tint
  back. A reference preset built by another tool shows this plainly: its row sums
  are `[1025, 999, 1031]`, not 1024.

  Measured over 22 Leica looks, the free solve was better on **every single one**:

  | Look | tied rows | free rows |
  |---|---|---|
  | Sepia | 8.33 | **1.50** |
  | Blue | 8.81 | **3.76** |
  | Selenium | 8.80 | **4.84** |
  | Greg Williams Mono | 6.28 | **4.03** |
  | Classic | 10.93 | **9.53** |
  | Eternal | 3.86 | **3.38** |

  On a look whose greys really are neutral, the free solve converges to row sums
  of ~1024 on its own (Chrome `[1045,1016,993]`, Silver `[1052,1044,1038]`), so
  nothing is lost by not forcing it. Use `--tie-rows` only when a guaranteed-grey
  grey is a hard requirement that outranks fidelity.

- Corner samples are skipped (`linspace(0.02, 0.98)`) because quantisation
  dominates there.
- Matrix values stay well inside the legal `-2048..3072` range in practice
  (measured extremes across the 22 looks: -102 and 1153), so the free solve does
  not produce wild matrices.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Every file says `ambiguous` for axis | The look is near-monochrome, so both layouts are near-equivalent | Usually harmless. If colours look inverted, force a check by fitting a strongly-coloured LUT through the same path to confirm the detector is alive. |
| `FAIL: fit error too large` | The LUT needs per-hue work the model cannot do | Use it anyway only if a visual check looks acceptable; `--allow-fail` to write it. Otherwise pick a different look. |
| `FAIL: ... wrong dominant channel` | Genuine axis problem that survived detection | Treat as a bug: check the cube's `LUT_3D_SIZE`/header and how it was exported. |
| `the neutral diagonal is nearly flat` | Not a look LUT - it has no usable tone curve | The file is probably a display/calibration profile, not a creative look. |
| Neutrals look wrong / grey cast invented | You passed `--tie-rows`, which forces greys to be exactly neutral | Drop the flag. It is off by default for a reason. |
| Fit much worse than the table above | Check the axis verdict first - a silently swapped file fits badly but plausibly | Compare `penalty_bgr` and `penalty_rgb` in the verbose output; a real swap is a large gap, not a tie. |
| Fit is fine but the camera shows nothing | Not a conversion problem | The curve hugs the diagonal, or the injection is unreachable - see `color-model.md`, Trap 1. |

## After converting

1. Sanity-check the numbers in `print_one`'s verbose block (grades above PASS/WARN
   also print `purity` and the matrix).
2. Import the JSON into `assets/lut-designer.html` to see it on a real photo.
3. Inject it:

```sh
python scripts/inject_lut.py --apk <built.apk> --lut profiles/look.json --out out.apk
```

The emitted JSON is validated against the exact schema `inject_lut.py` enforces
before it is written, so a successful conversion will always load.

## How the pipeline was validated

The conversion has been checked against an independently produced reference
preset for the same source LUT (`Leica_Eternal_17.cube` -> `Leica_Eternal_17.json`,
the latter built outside this tool with row sums `[1025, 999, 1031]`). Treat this
as the expected accuracy envelope when you convert a cube you already have a good
preset for:

| Check | Result |
|---|---|
| schema / field names | identical (`id`, `name`, `family`, `guide`, `matrix`, `gamma`) |
| matrix element-wise | max delta **6 / 1024 = 0.59%** of full scale |
| matrix row sums | `[1025, 999, 1033]` vs reference `[1025, 999, 1031]` |
| gamma, 1024 points | max delta **±2 / 1023**, mean 0.30, endpoints identical |
| end-to-end colour | mean **dE76 0.358**, p95 0.76, max 1.30 |
| faithfulness to source | reference 3.445, converted 3.478 (**+0.96%**) |
| mid-grey tint | both reproduce the source's slight magenta cast |
| injector acceptance | both load via `inject_lut.load_json_presets()` |

Two readers should take different things from this table:

- **dE76 0.358 between the two presets** is well below the ~1.0 just-noticeable
  threshold, so the conversion is perceptually equivalent to a hand-built
  reference - the pipeline is trustworthy.
- **Neither preset is "the" answer.** Both sit ~3.4 dE76 away from the source
  cube, because the 3x3-matrix-plus-one-curve model simply cannot represent a
  general 3D LUT exactly. Two correct implementations will always differ by
  roughly this much. Do not chase a byte-exact match - chase the QC grade.

Note the row sums above: the reference is *not* tied to 1024, and matching that
was most of the accuracy (the tied solve scored dE76 2.06 against the reference
where the free solve scored 0.358). That is the evidence behind the default
described under *Fitting notes*.
