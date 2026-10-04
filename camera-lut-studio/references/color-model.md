# The colour model, and the traps that cost real debugging time

## What the camera actually runs

Sony PMCA cameras expose no LUT loader. The app in this skill writes to the image
signal processor through `com.sony.scalar.hardware.CameraEx`, and the ISP accepts
exactly two colour inputs:

```
sRGB / Rec.709 encoded pixel 0-255
        |
        +-- 3x3 RGB colour matrix, 9 integers at fixed point x1024
        |
        +-- one shared 1024-point gamma curve, integers 0-1023, non-decreasing
                    |
                    v
        ISP -> viewfinder and in-camera output
```

Lua-free restatement of the arithmetic, identical to `fit_luts.py:apply_model`:

```python
x = clip(rgb @ matrix.T, 0, 1)        # matrix acts in the encoded domain, no linearisation
y = interp(x, linspace(0, 1, 1024), curve)
```

Consequences worth stating up front:

- One common curve for all three channels is the hard limit. A LUT that bends only
  a green band, or only skin tones, cannot be reproduced. Fitting reports the mean
  error so the gap is visible.
- `matrix` rows summing to 1024 keep neutrals neutral. Rows that do not sum to 1024
  deliberately tint grey (the upstream Ricoh presets do this). Never normalise a
  preset that was authored with a tint.
- Bump the matrix by ~2-3% and the look is obvious. The shipped `test-1` LUT sits at
  midtone offset +0 and highlight 252, which is very mild next to the built-in
  Fujifilm fits (midtones +16 to +31). A weak result is often the parameters, not a
  failed injection.

## Preset schema

```json
{
  "id": "test",
  "name": "测试1",
  "matrix": [[1024, 5, -5], [0, 1024, 0], [-297, 486, 835]],
  "gamma": [0, 1, 2, "… exactly 1024 integers, 0-1023, non-decreasing …"]
}
```

Validation performed on load: gamma length 1024 and inside 0-1023 and
non-decreasing; matrix 3x3 and inside -2048..3072. The menu label defaults to
`自定义 ` plus `name`; override with `--name`, or set `value_name` in the JSON.

## Trap 1 - the injected branch must be reachable

The tail of every lookup method looks like this:

```smali
    :cond_3a
    sget-object v0, ...->sFujimatrix14_100:[I
    return-object v0
    # inserting here produces DEAD CODE:
    :cond_3b
    const/4 v0, 0x0
    return-object v0
.end method
```

The last built-in preset dispatches with `if-eqz v0, :cond_3b`, which jumps
**straight to the null-return label** and skips anything sitting before it. Inject
there and the lookup keeps returning null. Symptoms, both at once:

- the menu shows a fallback name (the camera treats the id as a native PictureEffect
  id and displays e.g. 流行色彩 / Pop Color)
- selecting it changes nothing, because `applyHook` fails closed on a null matrix

Inject at the **head of the chain instead** - after the prologue, before the first
built-in `const-string v0, "<id>"`. `validate_reachable()` enforces this.

## Trap 2 - drop the equals result before branching

```smali
    const-string v0, "test"
    invoke-virtual {v0, p0}, Ljava/lang/String;->equals(Ljava/lang/Object;)Z
    move-result v0
    if-eqz v0, :skip        # correct: equals()==0 means NOT equal, so skip
    ...
    :skip
```

Writing `if-nez` here inverts the test. The failure mode is nasty: **every built-in
id hits the new branch and returns the custom preset, while the custom preset itself
returns null.** Static structure checks pass; only execution catches it. The script
simulates the patched methods before signing for exactly this reason.

## Trap 3 - line endings and XML node form

Two deviations that are invisible in a structural diff:

- `ElementTree.write(path)` opens the target in **text mode**, so on Windows every
  `\n` becomes `\r\n` and the whole `assets/MenuData.xml` is rewritten (the shipped
  asset is LF). Always serialise to bytes: `ET.tostring(root, encoding='unicode')`
  then `path.write_bytes(...)`.
- Create new menu nodes with `copy.deepcopy(existing_sibling)`, never
  `ET.Element(tag, attrib)`. The latter leaves `.text` as `None`, so it serialises
  as a self-closing `<Layer2 .../>` while its siblings are
  `<Layer2 ...>` + newline + `</Layer2>`. Whether the camera parser cares is
  **unproven** (the 滤镜强度 submenu uses self-closing nodes and works), but there
  is no reason to introduce the difference.

## Verification strategy

Structure checks cannot prove behaviour. Use three layers:

1. read the product's arrays back out of a fresh decompile and compare against
   `blend_profile` for all four strengths (100/70/50/30);
2. execute the patched lookup methods with a small interpreter - `inject_lut.py`
   does this for `getRGBMatrix`, `getGammaBytes`, `getFilterName`;
3. confirm unknown ids still return null, so the built-in presets were not hijacked.

The script runs all three after signing and refuses to report success otherwise.
A build that fails verification must not be installed.
