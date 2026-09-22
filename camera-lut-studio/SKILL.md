---
name: camera-lut-studio
description: "Design a custom colour LUT and inject it into a Sony PMCA camera APK, producing a signed, installable APK. Use when the user wants to 自定义相机色彩、制作自己的 LUT、将这个 LUT 导入这个 APK、把 LUT 写入相机应用并导出，或为 a6000、a5100、a6300、a6500、a7 系列、RX100、RX10、HX90 等 PlayMemories Camera Apps 机型添加自定义滤镜，或需要修改 FilmStudio / 胶片工坊 / Ricoh mod 这类机内调色 APK 的滤镜列表。Also use when asked to add, replace or rename a preset inside an already-built Film Studio APK, or to fit a .cube look LUT onto the matrix + gamma model the camera ISP can execute."
agent_created: true
---

# Camera LUT Studio

Produce a signed APK whose in-camera filter list contains a colour look the user
designed. The camera ISP cannot load LUT files; it only accepts a 3x3 RGB matrix and
one shared 1024-point gamma curve, so the whole job is: author or fit those two
things, write them into an existing Film Studio APK, and prove the result before it
reaches the camera.

## Inputs

Required, from the user:

- **the LUT** - either preset JSON (what `assets/lut-designer.html` exports) or a
  standard `.cube` look LUT
- **the APK to modify** - a built Film Studio APK (package
  `com.yuki.imaging.app.pictureeffectplus`). If the user only has the upstream
  *release* APK, that is fine: it is already the patched app and can be appended to
  directly. The official Sony "Picture Effect+" base APK is **not** needed.

If the user has no LUT yet, or asks how to make one, do not just hand over the file -
open it for them and walk them through it. See **Guiding the user through the designer**

If the user does not have an APK, tell them that the skill's assets folder contains the installation package and the installer.

below.

## Guiding the user through the designer

Offer this whenever the user has no LUT yet, asks "怎么用", or is unsure what to change.
The designer is one offline HTML file; the fastest way to make it usable is to open it
in their preview panel.

1. **Open it for them.** Call `present_files` with `assets/lut-designer.html` so it
   renders in the built-in browser panel. If they would rather keep their own copy,
   copy the file somewhere in their workspace first and present that path - it is
   self-contained, so anywhere works, including a folder next to their photos.
2. **Walk through it in order**: load their own photo → set the matrix → shape the
   curve → check the strength preview → export. The full talk-track, including what to
   say about each control, is in `references/lut-designer.md` under *Explaining the
   tool to the user*. Read it before explaining, and keep the explanation concrete.
3. **Say the two things users get wrong.** The strength slider is a preview only
   (the export always writes the 100% values, and the camera generates 30/50/70/100
   itself), and a curve that hugs the diagonal produces a barely visible look - that,
   not a broken injection, is the usual reason a recipe seems to do nothing.
4. **Show them the new selection UI.** Export lists every saved recipe plus the one
   being edited, each with a checkbox; 仅当前 exports just the current look. This
   matters because a library of several looks no longer has to be exported as one
   merged file.
5. **Close the loop.** Offer to inject the export immediately: ask for the APK and run
   the injector. Do not make them come back with the file if the APK is already known.

## Workflow

1. **Check both inputs exist.** Confirm the APK path and the LUT path. If the LUT was
   exported by the designer, note that it may be a `{"presets": [...]}` wrapper - the
   script accepts that.

2. **Locate java and apktool.** apktool is a JAR and needs a Java runtime. The script
   resolves both itself:
   - java: `JAVA_HOME` → `PATH` → `~/.workbuddy/binaries/java/versions/current` →
     common install paths
   - apktool: `~/.workbuddy/binaries/apktool/versions/current` → `$APKTOOL_JAR` →
     `**/inputs/apktool.jar` under cwd

   If apktool.jar is missing, download Apktool 2.12.1 from
   `https://github.com/iBotPeaches/Apktool/releases` (~26 MB) and pass `--apktool`.

3. **Run the injector.**

   ```sh
   python scripts/inject_lut.py --apk <built.apk> --lut <lut.json|look.cube> \
       --out <output.apk>
   ```

   The input may hold one preset or a whole library - every preset in the file is
   appended in the order given, one menu entry each. Add `--name "显示名"` to set the
   menu label (default `自定义 ` + the LUT's name) and `--id` to set the internal id;
   both only apply when the file holds a single preset. Ids containing whitespace are
   rewritten with `-`. The default output name is `<apk>-custom.apk`; use a
   descriptive name such as `FilmStudio-UserCustomize.apk` when one was requested.

   **Watch for duplicate menu labels.** When appending a library, check that the new
   labels do not collide with each other or with entries already in the APK. Two menu
   entries showing the same text are indistinguishable on the camera. Rename via
   `--name` (single preset) or by editing the JSON's `name` fields before injecting.

   **Append vs replace.** By default the new look is appended as one extra menu
   entry. If the user wants the menu to stay the same length, or a 16th entry does
   not survive on their camera, replace an existing slot instead:

   ```sh
   python scripts/inject_lut.py --apk <built.apk> --lut <lut.json> \
       --replace ricoh-cross --id mylook --name "我的风格" --out <output.apk>
   ```

   The replaced slot keeps its index, so the menu length is unchanged.

   **Reuse the signing key when continuing an existing build.** The default is
   `~/.workbuddy/camera-lut-studio/signing.pem`. If the APK the user already has
   installed was signed with a different key, the new build needs an uninstall first.
   Pass `--key <that-key.pem>` whenever you know which key produced the installed
   build - one `-r` install beats making the user uninstall and lose their settings.

   **Removing or reorganising presets.** There is no delete command, and none is
   needed: provide a base that lacks the preset and the injector rebuilds the list
   from it. To drop the only custom preset, point `--apk` at the upstream release
   again and append just what should remain. Check what is in the target first
   (`unzip -p <apk> assets/MenuData.xml`) - if it carries several presets the user
   wants to keep, extract those first rather than rebuilding from the release.
   Indices must stay contiguous, so deleting from the middle of a list is not a
   supported in-place operation.

4. **Read the verification output.** The script re-decompiles the product, compares
   every stored array against `blend_profile` for all four strengths, and executes
   the patched lookup methods with a small interpreter. It exits non-zero if any
   check fails and prints `FAIL` lines. **Never hand over an APK whose verification
   failed** - report the failing check instead.

5. **Report and hand over.** Give the output path, its SHA-256, the menu name, and
   the install command. Always include the uninstall-first warning: each locally
   signed build has its own key, so `adb install -r` fails against a build signed
   with a different key. See `references/install-to-camera.md`.

## Non-obvious rules that must not be broken

These produced silent, hard-to-diagnose failures in practice. The script enforces all
of them; do not hand-edit around them.

- New lookup branches go at the **head** of the chain, not before the null-return
  label. The last built-in preset jumps straight to that label and would skip them.
- Branch on the `equals()` result with `if-eqz` (zero means *not* equal). `if-nez`
  inverts the test and hijacks every other preset id.
- Serialise menu XML to **bytes** with LF endings. `ElementTree.write(path)` opens
  the file in text mode and rewrites every line as CRLF on Windows.
- Build new menu nodes with `copy.deepcopy(existing_sibling)`, never
  `ET.Element(...)`, so they serialise in the same form as their siblings.
- Sign with OpenSSL `smime -sign -noattr -binary`. Modern jarsigner adds CMS
  attributes that the camera's Android 4.1.2 runtime rejects.

Reasoning and failure modes: `references/color-model.md`.

## Files

- `scripts/inject_lut.py` - the whole pipeline: decompile, inject, validate,
  rebuild, sign, verify. Self-contained; depends only on java, apktool, OpenSSL and
  (for `.cube` input) numpy.
- `assets/lut-designer.html` - offline single-file visual designer. Exports the
  preset JSON this skill consumes, plus a 33³ `.cube`. Works on the user's own photo
  or its built-in test chart.
- `references/color-model.md` - the ISP model, the preset schema, and the traps in
  detail.
- `references/lut-designer.md` - how to drive the designer.
- `references/install-to-camera.md` - Wi-Fi ADB setup, install commands, and a
  troubleshooting table for `Communication error 100`, signature and dexopt errors.
- `assets/FilmStudio-UserCustomize.apk`, `assets/pmca-gui-v0.18-win.exe` - the bundled
  build and the USB installer. Deliberate, see above.

### Bundled binaries - intentional, do not remove

`assets/` also carries two large files the skill owner placed there on purpose, so the
toolkit stays self-contained and works on a fresh machine:

- `assets/FilmStudio-UserCustomize.apk` - a ready-made build to modify or install
- `assets/pmca-gui-v0.18-win.exe` - Sony-PMCA-RE / pmca-gui, needed once over USB to
  install OpenMemories: Tweak before Wi-Fi ADB can be enabled (see
  `references/install-to-camera.md`)

They raise the package to roughly 13 MB, which is expected. **Do not delete, move or
"tidy" them** because they look out of place for a skill - they are not duplicates to
be cleaned up. Treat anything already present in `assets/` as deliberate unless the
owner says otherwise.

---


