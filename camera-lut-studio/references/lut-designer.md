> Provenance: `assets/lut-designer.html` is a copy bundled with this skill so it works
> anywhere. If the user maintains a newer version elsewhere, copy that one in and keep
> the export schema (`matrix` + 1024-point `gamma` per preset) unchanged.

# Driving the LUT designer

`assets/lut-designer.html` is a single offline file - open it in any browser. No
install, no network, the photo never leaves the machine.

## Loading an image

Use **选择照片** for the user's own picture, or **测试图** for the built-in chart
(primary colour bars, hue sweep, 21-step greyscale wedge, skin-tone patches, plus
highlight and shadow chips). Dragging an image onto the canvas also works.

## The three controls, in the order they apply

The pipeline matches the camera exactly: `rgb → 3x3 matrix → 1024-point curve`.

**色彩矩阵** - nine coefficients. Rows sum to 1024 by default, which keeps neutrals
neutral; unlocking the third column lets a row carry a deliberate tint. `通用 look
快捷` offers starting points (warm, cool, saturated, desaturated, monochrome,
teal-orange). The row sum is shown live.

**色调曲线** - drag control points; double-click empty space to add one, double-click
a point to delete it (endpoints stay). Interpolation is monotone cubic, so the curve
is always non-decreasing and therefore always acceptable to the camera. The
brightness-mapping panel plots identity (grey), the 100% curve (green) and the
current-strength curve (blue).

**滤镜强度** - previews what the camera will do at 30 / 50 / 70 / 100%. The camera
generates those four steps itself by interpolating the matrix toward identity and the
curve toward a straight ramp, so **the export always writes the 100% values** - never
bake a strength into the profile.

**白平衡** is a preview aid only and is never exported; white balance is set on the
camera in MENU page 4.

## Viewing modes

`结果` / `原图` / `分屏对比` (drag the divider). The RGB histogram draws the result as
solid lines and the original as dashed, so a change that is not doing what was
intended is easy to spot.

## Judge the look's strength before expecting an obvious result

Read the curve numbers the tool reports: a midtone offset near 0 with highlights at
255 is a very mild look. The built-in Fujifilm-style presets in this ecosystem
typically lift midtones by +16 to +31. If the camera result feels weak, the
parameters are the reason - strengthen the curve and the matrix rather than
suspecting the injection.

## Exporting - pick exactly which recipes to export

Both **导出 profile (JSON)** and **导出库** open the same drawer. It lists the whole
recipe library plus the recipe being edited right now (tagged `当前编辑` when its id
already exists in the library, `当前未保存` otherwise). Every row has a checkbox and
**all rows start checked**; the JSON on the right updates on every click.

Controls along the bottom:

| Button | Effect |
| --- | --- |
| 全选 / 全不选 | check or clear every row |
| 仅当前 | select only the recipe being edited - the quickest way to export one look |
| 切换为单条 preset 对象 | output the bare preset instead of a `presets[]` pack; only meaningful with exactly one row selected |
| 下载 | save under the name shown in the note (`custom_profiles.json`, or `<id>.json` in single-preset shape) |

Two guards the drawer surfaces as notes:

- **more than 30 selected** - each preset writes four strength variants of 9 matrix
  values plus 2048 gamma bytes into the APK's class initialiser, so very long lists
  bloat the DEX and may exceed the camera VM's loading capacity.
- **duplicate ids among the selected** - `scripts/inject_lut.py` rejects duplicate
  ids, so this note means the export would be rejected on injection. Rename one of
  them or select fewer.

Each exported recipe becomes one menu entry in the APK.

- **导出 .cube** - a 33³ LUT of the current look only, usable in Resolve / Lightroom /
  Photoshop. It is also valid input for the injector, which fits it back onto the
  matrix + curve model.

## Recipe library

Named recipes live in browser localStorage: save, load, delete. Because the export
drawer shows the library plus the current edit, there is no longer any need to export
the whole library just to get one recipe out of it.

## Explaining the tool to the user

When the user has never seen the designer, open `assets/lut-designer.html` for them
and walk through it in this order. Keep it concrete and short.

1. **Load a picture.** They should use a photo shot with the camera they are tuning,
   in the light they care about. The built-in 测试图 is only for sanity checks - it
   has colour bars, a hue sweep, a 21-step grey wedge and skin patches.
2. **Set the matrix** if they want a colour shift (warm/cool/saturated/mono). The
   panel shows the row sums; leaving them at 1024 keeps greys neutral.
3. **Shape the curve** for contrast. Drag points; more curve = more visible effect.
   Tell them plainly: a curve that stays near the diagonal produces a very subtle
   result, and that is usually the reason a look seems to do nothing.
4. **Preview the strengths.** The camera generates 30/50/70/100% itself by
   interpolating toward identity. Whatever the strength slider shows, **the export
   always writes the 100% values** - strength is a camera-side choice, not part of the
   recipe.
5. **Switch between 结果 / 原图 / 分屏对比** so they can judge honestly rather than
   trusting the effect panel alone.
6. **Export.** Use 仅当前 for one look, or tick the rows they want. Read the notes:
   duplicates and >30 selections are flagged there for good reason.
7. **Hand the file back** for injection. The file name does not matter; the ids and
   names inside it become the menu entries.

Point out that 白平衡 is a preview aid only and is never exported, and that the photo
never leaves the browser.
