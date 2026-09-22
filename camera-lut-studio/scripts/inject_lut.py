#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Inject a custom colour LUT into a Sony PMCA "Film Studio" APK.

Self-contained on purpose: workspace directories rotate, so nothing here imports
from the source repository. Logic is a faithful consolidation of the verified
toolchain (add_presets.py / xml_lf.py / sign_apk.py / filter_strength.py).

Pipeline
    locate java + apktool.jar
    apktool d -r  <apk>
    append the preset: arrays, <clinit> payloads, five lookup tables, icon map, menu XML
    validate label definitions AND branch reachability
    apktool b  ->  sign v1 JAR (OpenSSL)  ->  verify

Accepts either the Film Studio preset JSON (what the visual designer exports) or a
standard .cube look LUT, which is fitted to the matrix + common-curve model.

Usage
    python inject_lut.py --apk FilmStudio.apk --lut my-lut.json --out FilmStudio-UserCustomize.apk
"""
import argparse
import base64
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

STRENGTHS = (30, 50, 70, 100)
TAG = 'fujicustom'
MENU_ICON = '0x7f020054'
HOOK_CLASS = 'Lcom/yuki/imaging/app/pictureeffectplus/shooting/camera/RicohHook;'
LAYOUT_NAME = 'PictureEffectPlusOptionMenuLayout.smali'
HOOK_NAME = 'RicohHook.smali'
MENU_PATH = 'assets/MenuData.xml'
MENU_PARENT = 'ApplicationTop'
PRESET_FIELD = re.compile(r'^\.field private static sFujimatrix(\d+)_(\d+):\[I$', re.M)


def die(msg):
    print('ERROR: ' + msg, file=sys.stderr)
    sys.exit(1)


def note(msg):
    print('  ' + msg)


def quote(s):
    return json.dumps(s, ensure_ascii=True)


# --------------------------------------------------------------------------- #
# colour model, copied verbatim from tools/filter_strength.py
# --------------------------------------------------------------------------- #

def blend_profile(profile, strength):
    """Interpolate matrix toward identity and gamma toward the identity ramp.

    Python's round() is ties-to-even; keep it, the shipped arrays depend on it.
    """
    if not 0 <= strength <= 100:
        raise ValueError('Strength outside 0-100')
    matrix = []
    for row, values in enumerate(profile['matrix']):
        blended = [round(((1024 if column == row else 0) * (100 - strength)
                          + value * strength) / 100)
                   for column, value in enumerate(values)]
        if sum(values) == 1024:
            blended[row] = 1024 - sum(v for column, v in enumerate(blended) if column != row)
        matrix.append(blended)
    gamma = [round((i * (100 - strength) + value * strength) / 100)
             for i, value in enumerate(profile['gamma'])]
    return dict(matrix=matrix, gamma=gamma)


# --------------------------------------------------------------------------- #
# LUT input: preset JSON, or a .cube fitted to the model
# --------------------------------------------------------------------------- #

def load_json_presets(path):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    items = data['presets'] if isinstance(data, dict) else data
    if not isinstance(items, list) or not items:
        die('LUT JSON must be a preset list, or an object with a "presets" list')
    out = []
    for p in items:
        for key in ('id', 'name', 'matrix', 'gamma'):
            if key not in p:
                die('preset %s is missing "%s"' % (p.get('id', '?'), key))
        if len(p['gamma']) != 1024:
            die('%s: gamma must have exactly 1024 points' % p['id'])
        if len(p['matrix']) != 3 or any(len(r) != 3 for r in p['matrix']):
            die('%s: matrix must be 3x3' % p['id'])
        if not all(0 <= v <= 1023 for v in p['gamma']):
            die('%s: gamma values must be within 0-1023' % p['id'])
        if any(a > b for a, b in zip(p['gamma'], p['gamma'][1:])):
            die('%s: gamma must be non-decreasing' % p['id'])
        if not all(-2048 <= v <= 3072 for row in p['matrix'] for v in row):
            die('%s: matrix values must be within -2048..3072' % p['id'])
        out.append(dict(p))
    return out


def read_cube(path):
    import numpy as np
    size, rows = None, []
    for line in Path(path).read_text(encoding='utf-8', errors='replace').splitlines():
        words = line.split()
        if not words or words[0].startswith('#'):
            continue
        if words[0] == 'LUT_3D_SIZE':
            size = int(words[1])
        elif words[0] in ('DOMAIN_MIN', 'DOMAIN_MAX'):
            expected = 0.0 if words[0] == 'DOMAIN_MIN' else 1.0
            if any(float(x) != expected for x in words[1:]):
                die('only unit-domain .cube files are supported (DOMAIN_MIN=0, DOMAIN_MAX=1)')
        elif words[0] != 'TITLE':
            rows.append([float(x) for x in words])
    values = np.asarray(rows, dtype=np.float64)
    if not size or values.shape != (size ** 3, 3):
        die('.cube is truncated or malformed')
    # .cube ordering: red varies fastest, then green, then blue
    return values.reshape(size, size, size, 3)


def fit_cube(cube, samples=48):
    """Fit 3x3 matrix + 1024-point common curve, the model the camera ISP runs."""
    import numpy as np

    def sample(rgb):
        n = cube.shape[0]
        p = np.clip(np.asarray(rgb, dtype=np.float64), 0, 1) * (n - 1)
        lo = np.minimum(p.astype(int), n - 2)
        f = p - lo
        out = np.zeros_like(p)
        for dr in (0, 1):
            for dg in (0, 1):
                for db in (0, 1):
                    w = ((f[:, 0] if dr else 1 - f[:, 0]) *
                         (f[:, 1] if dg else 1 - f[:, 1]) *
                         (f[:, 2] if db else 1 - f[:, 2]))
                    out += w[:, None] * cube[lo[:, 2] + db, lo[:, 1] + dg, lo[:, 0] + dr]
        return out

    t = np.linspace(0, 1, 1024)
    gray = sample(np.repeat(t[:, None], 3, axis=1)).mean(1)
    curve = np.clip(np.maximum.accumulate(np.clip(gray, 0, 1)), 0, 1)

    axis = np.linspace(0.02, 0.98, samples)
    grid = np.array([(r, g, b) for b in axis for g in axis for r in axis])
    target = sample(grid)
    cy, ids = np.unique(curve, return_index=True)
    linear = np.stack([np.interp(target[:, c], cy, t[ids]) for c in range(3)], axis=1)
    design = grid[:, :2] - grid[:, 2:3]
    matrix = np.empty((3, 3))
    for c in range(3):
        coef = np.linalg.lstsq(design, linear[:, c] - grid[:, 2], rcond=None)[0]
        matrix[c] = [coef[0], coef[1], 1 - coef.sum()]

    matrix_i = np.rint(matrix * 1024).astype(int)
    matrix_i[:, 2] = 1024 - matrix_i[:, :2].sum(1)
    curve_i = np.rint(curve * 1023).astype(int)
    curve_i = np.maximum.accumulate(np.clip(curve_i, 0, 1023))

    pred = np.interp(np.clip(grid @ (matrix_i / 1024).T, 0, 1),
                     np.linspace(0, 1, 1024), curve_i / 1023)
    error = float(np.abs(pred - target).mean())
    return dict(matrix=matrix_i.tolist(), gamma=curve_i.tolist()), error


def load_lut(path):
    """Return (presets, warnings)."""
    suffix = Path(path).suffix.lower()
    if suffix != '.cube':
        return load_json_presets(path), []
    cube = read_cube(path)
    fitted, error = fit_cube(cube)
    name = Path(path).stem
    preset = dict(id=re.sub(r'[^A-Za-z0-9_-]', '-', name.lower()) or 'custom',
                  name=name, matrix=fitted['matrix'], gamma=fitted['gamma'])
    warnings = []
    fitted_gray = np_is_neutral(fitted['matrix'])
    if not fitted_gray:
        warnings.append('fitted matrix rows do not sum to 1024, so neutrals may pick up a tint')
    warnings.append('%.4f mean RGB fit error - a 3x3 matrix plus one common curve cannot '
                    'reproduce every hue-dependent 3D LUT' % error)
    return [preset], warnings


def np_is_neutral(matrix):
    return all(sum(row) == 1024 for row in matrix)


# --------------------------------------------------------------------------- #
# XML: always LF, and new nodes copy an existing sibling so they serialise alike
# --------------------------------------------------------------------------- #

def write_xml_lf(root, path):
    text = ET.tostring(root, encoding='unicode')
    data = ("<?xml version='1.0' encoding='utf-8'?>\n" + text).encode('utf-8')
    original = Path(path).read_bytes() if Path(path).exists() else b''
    if original.startswith(b'<?xml'):
        if original.endswith(b'\n') and not data.endswith(b'\n'):
            data += b'\n'
        elif not original.endswith(b'\n') and data.endswith(b'\n'):
            data = data[:-1]
    Path(path).write_bytes(data)


def patch_menu(path, presets):
    tree = ET.parse(path)
    root = tree.getroot()
    top = next(e for e in root.iter() if e.get('ItemId') == MENU_PARENT)
    existing = [e.get('ItemId') for e in top]
    template = list(top)[-1]
    for preset in presets:
        if preset['id'] in existing:
            die('%s is already in the menu' % preset['id'])
        # deepcopy keeps .text/.tail, so the new entry serialises exactly like its
        # siblings. ET.Element() would emit a self-closing <Layer2 .../>.
        item = copy.deepcopy(template)
        for child in list(item):
            item.remove(child)
        item.attrib.update(ItemId=preset['id'], Value=preset['id'],
                           Title=preset['menu_name'], DisplayName=preset['menu_name'])
        top.append(item)
    write_xml_lf(root, path)
    return existing


# --------------------------------------------------------------------------- #
# smali injection
# --------------------------------------------------------------------------- #

def method_span(text, signature, modifier='public static'):
    pattern = (r'^\.method ' + modifier + r' [^\n]*' + re.escape(signature)
               + r'[\s\S]*?^\.end method')
    match = re.search(pattern, text, re.M)
    if not match:
        die('method not found: ' + signature)
    return match


def insert_before(text, anchor, addition):
    idx = text.rfind(anchor)
    if idx < 0:
        die('anchor not found: ' + anchor.strip()[:60])
    return text[:idx] + addition + text[idx:]


def array_data_block(label, values, width):
    out = ['    :%s' % label, '    .array-data %d' % width]
    for value in values:
        hexed = '-0x%x' % -value if value < 0 else '0x%x' % value
        out.append('        ' + hexed + ('t' if width == 1 else ''))
    out.append('    .end array-data')
    return out


def strength_chain(kind, index, ret):
    """Each block ends with its own label, so a mismatch falls through to the next."""
    out = []
    for strength in STRENGTHS[:-1]:
        out += ['    const/16 v0, 0x%x' % strength,
                '    if-ne v1, v0, :%s_s%d_%d' % (TAG, strength, index),
                '    sget-object v0, %s->sFuji%s%d_%d:%s' % (HOOK_CLASS, kind, index, strength, ret),
                '    return-object v0',
                '    :%s_s%d_%d' % (TAG, strength, index)]
    out += ['    sget-object v0, %s->sFuji%s%d_%d:%s' % (HOOK_CLASS, kind, index, STRENGTHS[-1], ret),
            '    return-object v0']
    return out


def inject_lookup(text, signature, index, preset, kind):
    """Insert the new branch at the head of the lookup chain.

    It must NOT go just before the null-return label: the last built-in preset
    dispatches with `if-eqz v0, :<null label>`, which jumps over anything sitting
    there, leaving the new branch unreachable. The lookup would then return null,
    the menu entry would show a fallback name, and no colour would be applied.
    """
    match = method_span(text, signature)
    body = text[match.start():match.end()]
    anchor_inline = body.index('const-string v0, "')
    anchor = body.rindex('\n', 0, anchor_inline) + 1
    if not body[anchor:anchor_inline].isspace():
        die('unexpected prologue layout in ' + signature)
    ret = {'matrix': '[I', 'gamma': '[B',
           'name': 'Ljava/lang/String;', 'guide': 'Ljava/lang/String;'}[kind]
    skip = '%s_skip_%d' % (TAG, index)

    block = ['    const-string v0, ' + quote(preset['id']),
             '    invoke-virtual {v0, p0}, Ljava/lang/String;->equals(Ljava/lang/Object;)Z',
             '    move-result v0',
             # if-eqz means "not equal" (equals() returns 0). Using if-nez here
             # inverts the test and hijacks every other preset id.
             '    if-eqz v0, :%s' % skip]
    if kind in ('matrix', 'gamma'):
        block += strength_chain(kind, index, ret)
    else:
        block += ['    const-string v0, ' + quote(preset['value_' + kind]),
                  '    return-object v0']
    block.append('    :%s' % skip)
    new_body = body[:anchor] + '\n'.join(block) + '\n' + body[anchor:]
    return text[:match.start()] + new_body + text[match.end():]


def validate_labels(text, name):
    """Every branch target must be defined in the same method."""
    for match in re.finditer(r'^\.method [^\n]*\n[\s\S]*?^\.end method', text, re.M):
        body = match.group()
        defined = set(re.findall(r'^[ ]+:(\w+)[ ]*$', body, re.M))
        referenced = set()
        for line in body.split('\n'):
            stripped = line.strip()
            if not stripped or stripped[0] in '.#:':
                continue
            found = re.search(r'(?<![\w;/]):(\w+)\s*$', stripped)
            if found:
                referenced.add(found.group(1))
        missing = referenced - defined
        if missing:
            die('%s: undefined label(s) %s in %s' % (name, sorted(missing), body.split('\n')[0]))


def null_label_of(body):
    const_pos = body.rfind('const/4 v0, 0x0')
    labels = list(re.finditer(r'^[ ]+:(\w+)[ ]*$', body[:const_pos], re.M))
    return labels[-1].group(1) if labels else None


def validate_reachable(text, name, presets):
    """No jump to the null-return label may sit before the injected branch."""
    signatures = ('getRGBMatrix(Ljava/lang/String;)[I',
                  'getGammaBytes(Ljava/lang/String;)[B',
                  'getFilterName(Ljava/lang/String;)Ljava/lang/String;',
                  'getFilterGuide(Ljava/lang/String;)Ljava/lang/String;')
    for preset in presets:
        needle = 'const-string v0, ' + quote(preset['id'])
        for signature in signatures:
            match = method_span(text, signature)
            body = text[match.start():match.end()]
            label = null_label_of(body)
            if not label:
                die('no null-return label in ' + signature)
            ours = body.index(needle)
            pattern = re.compile(r'if-\w+ v\d+(?:, v\d+)?, :' + label + r'\b')
            bypass = [m.group(0) for m in pattern.finditer(body) if m.start() < ours]
            if bypass:
                die('%s: %s bypasses the new preset via %s; the branch is unreachable'
                    % (name, signature, bypass[0]))


def patch_hook(text, base_index, presets):
    if '# static fields' not in text:
        die('RicohHook.smali has no "# static fields" section')
    fields = ['.field private static sFuji%s%d_%d:%s' % (kind, base_index + n, s, ret)
              for n in range(len(presets)) for s in STRENGTHS
              for kind, ret in (('matrix', '[I'), ('gamma', '[B'))]
    text = text.replace('# static fields', '# static fields\n' + '\n\n'.join(fields) + '\n', 1)

    clinit = method_span(text, 'constructor <clinit>()V', modifier='static')
    clinit_end = clinit.end()
    statements, payloads = [], []
    for n, preset in enumerate(presets):
        index = base_index + n
        for strength in STRENGTHS:
            blend = blend_profile(preset, strength)
            for kind, count, ret, values, width in [
                    ('matrix', 9, '[I', sum(blend['matrix'], []), 4),
                    ('gamma', 2048, '[B',
                     [b for v in blend['gamma'] for b in (v & 255, v >> 8)], 1)]:
                label = '%s_%s_%d_%d' % (TAG, kind, index, strength)
                statements += ['    const/16 v0, 0x%x' % count,
                               '    new-array v1, v0, %s' % ret,
                               '    fill-array-data v1, :%s' % label,
                               '    sput-object v1, %s->sFuji%s%d_%d:%s'
                               % (HOOK_CLASS, kind, index, strength, ret)]
                payloads += array_data_block(label, values, width)
    statements.append('')
    head = text[:clinit.start()]
    body = text[clinit.start():clinit_end]
    if body.count('    return-void') != 1:
        die('<clinit> does not have exactly one return-void')
    body = insert_before(body, '    return-void', '\n'.join(statements) + '\n')
    body = insert_before(body, '.end method', '\n'.join(payloads) + '\n')
    text = head + body + text[clinit_end:]

    for n, preset in enumerate(presets):
        index = base_index + n
        for signature, kind in (('getRGBMatrix(Ljava/lang/String;)[I', 'matrix'),
                                ('getGammaBytes(Ljava/lang/String;)[B', 'gamma'),
                                ('getFilterName(Ljava/lang/String;)Ljava/lang/String;', 'name'),
                                ('getFilterGuide(Ljava/lang/String;)Ljava/lang/String;', 'guide')):
            text = inject_lookup(text, signature, index, preset, kind)

    ids_method = method_span(text, 'getPresetIds()Ljava/util/List;')
    addition = '\n'.join(
        '    const-string v1, ' + quote(p['id']) + '\n'
        '    invoke-virtual {v0, v1}, Ljava/util/ArrayList;->add(Ljava/lang/Object;)Z'
        for p in presets) + '\n'
    body = text[ids_method.start():ids_method.end()]
    body = insert_before(body, '    return-object v0', addition + '\n')
    text = text[:ids_method.start()] + body + text[ids_method.end():]

    validate_labels(text, HOOK_NAME)
    validate_reachable(text, HOOK_NAME, presets)
    return text


def patch_icon_map(text, presets):
    match = method_span(text, 'initializeIconMap()V', modifier='private')
    addition = '\n'.join(
        '    const-string v1, ' + quote(p['id']) + '\n'
        '    const v2, ' + MENU_ICON + '\n'
        '    invoke-static {v2}, Ljava/lang/Integer;->valueOf(I)Ljava/lang/Integer;\n'
        '    move-result-object v2\n'
        '    invoke-virtual {v0, v1, v2}, Ljava/util/HashMap;->put('
        'Ljava/lang/Object;Ljava/lang/Object;)Ljava/lang/Object;'
        for p in presets) + '\n'
    body = text[match.start():match.end()]
    body = insert_before(body, '    return-void', addition + '\n')
    return text[:match.start()] + body + text[match.end():]


# --------------------------------------------------------------------------- #
# toolchain: java, apktool
# --------------------------------------------------------------------------- #

def managed_tool(tool, launcher):
    versions = Path.home() / '.workbuddy' / 'binaries' / tool / 'versions'
    pointer = versions / 'current'
    if not pointer.exists():
        return None
    name = pointer.read_text(encoding='utf-8').strip()
    for sub in ('bin', ''):
        candidate = versions / name / sub / launcher if sub else versions / name / launcher
        if candidate.exists():
            return str(candidate)
    return None


def find_java():
    env = os.environ.get('JAVA_HOME')
    if env:
        for name in ('java.exe', 'java'):
            candidate = Path(env) / 'bin' / name
            if candidate.exists():
                return str(candidate)
    found = shutil.which('java')
    if found:
        return found
    for tool, launcher in (('java', 'java.exe'),):
        got = managed_tool(tool, launcher)
        if got:
            return got
    bases = [Path.home() / '.workbuddy' / 'binaries',
             Path(r'C:/Program Files/Java'), Path(r'C:/Program Files/Eclipse Adoptium'),
             Path(r'C:/Program Files/Microsoft'), Path(r'C:/Program Files/Android/Android Studio/jbr'),
             Path('/Library/Java/JavaVirtualMachines')]
    for base in bases:
        if not base.exists():
            continue
        for name in ('java.exe', 'java'):
            for candidate in sorted(base.rglob(name)):
                if candidate.parent.name == 'bin':
                    return str(candidate)
    return None


def find_apktool(explicit=None):
    if explicit:
        if not Path(explicit).exists():
            die('apktool.jar not found: %s' % explicit)
        return str(explicit)
    for tool, launcher in (('apktool', 'apktool.jar'),):
        got = managed_tool(tool, launcher)
        if got:
            return got
    env = os.environ.get('APKTOOL_JAR')
    if env and Path(env).exists():
        return env
    # a source checkout keeps it under inputs/
    for base in (Path.cwd(), Path.cwd().parent, Path.home()):
        for candidate in list(base.glob('**/inputs/apktool.jar'))[:1]:
            return str(candidate)
    die('apktool.jar not found. Pass --apktool, set APKTOOL_JAR, or install it under\n'
        '     ~/.workbuddy/binaries/apktool/versions/<ver>/apktool.jar\n'
        '     Download: https://github.com/iBotPeaches/Apktool/releases '
        '(needs Java; apktool is a JAR)')


# --------------------------------------------------------------------------- #
# signing: legacy v1 JAR, compatible with the camera's Android 4.1.2 runtime
# --------------------------------------------------------------------------- #

def ensure_pem(pem_path):
    if pem_path.exists():
        return str(pem_path), False
    pem_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile('w', suffix='.pem', delete=False)
    tmp.close()
    res = subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048',
                          '-keyout', tmp.name, '-out', tmp.name, '-days', '10000', '-nodes',
                          '-subj', '/CN=SonyPMCADebug/O=Community/C=US'],
                         capture_output=True, text=True)
    if res.returncode != 0:
        die('failed to generate certificate: ' + res.stderr)
    shutil.move(tmp.name, pem_path)
    try:
        pem_path.chmod(0o600)
    except OSError:
        pass
    return str(pem_path), True


def jar_header(name, value):
    raw = (name + ': ' + value).encode('utf-8')
    lines = []
    while len(raw) > 70:
        lines.append(raw[:70] + b'\r\n')
        raw = b' ' + raw[70:]
    return b''.join(lines) + raw + b'\r\n'


def sign_apk(input_apk, output_apk, pem, note_out=print):
    """v1 JAR signing via OpenSSL smime -noattr -binary.

    Modern jarsigner adds CMS protected attributes that the camera's 2012 Harmony
    runtime cannot parse (INSTALL_PARSE_FAILED_NO_CERTIFICATES). -noattr avoids it.
    """
    entries = {}
    with zipfile.ZipFile(input_apk) as zin:
        for item in zin.infolist():
            if item.filename.startswith('META-INF/'):
                continue
            entries[item.filename] = (item, zin.read(item.filename))
    manifest = [b'Manifest-Version: 1.0\r\n', b'Created-By: 1.0 (Android Signer)\r\n', b'\r\n']
    sections = {}
    for name in sorted(entries):
        digest = base64.b64encode(hashlib.sha1(entries[name][1]).digest()).decode('ascii')
        section = jar_header('Name', name) + jar_header('SHA1-Digest', digest) + b'\r\n'
        sections[name] = section
        manifest.append(section)
    manifest_bytes = b''.join(manifest)
    sf = [b'Signature-Version: 1.0\r\n', b'Created-By: 1.0 (Android Signer)\r\n',
          ('SHA1-Digest-Manifest: %s\r\n'
           % base64.b64encode(hashlib.sha1(manifest_bytes).digest()).decode('ascii')).encode(),
          b'\r\n']
    for name in sorted(entries):
        sf.append(jar_header('Name', name) + jar_header(
            'SHA1-Digest', base64.b64encode(hashlib.sha1(sections[name]).digest()).decode('ascii'))
            + b'\r\n')
    sf_bytes = b''.join(sf)
    with tempfile.TemporaryDirectory() as tmp:
        sf_path = Path(tmp) / 'cert.sf'
        rsa_path = Path(tmp) / 'cert.rsa'
        sf_path.write_bytes(sf_bytes)
        res = subprocess.run(['openssl', 'smime', '-sign', '-in', str(sf_path), '-outform', 'DER',
                              '-inkey', pem, '-signer', pem, '-noattr', '-md', 'sha1',
                              '-binary', '-out', str(rsa_path)], capture_output=True, text=True)
        if res.returncode != 0:
            die('openssl signing failed: ' + res.stderr)
        rsa_bytes = rsa_path.read_bytes()
    Path(output_apk).parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_apk, 'w', compression=zipfile.ZIP_DEFLATED) as zout:
        zout.writestr('META-INF/MANIFEST.MF', manifest_bytes)
        zout.writestr('META-INF/CERT.SF', sf_bytes)
        zout.writestr('META-INF/CERT.RSA', rsa_bytes)
        for name in sorted(entries):
            zout.writestr(entries[name][0], entries[name][1])
    note_out('signed %d entries' % len(entries))
    return len(entries)


# --------------------------------------------------------------------------- #
# verification: array readback + actual lookup execution
# --------------------------------------------------------------------------- #

def read_array(text, label, width, count):
    pattern = (r'^\s*:' + re.escape(label) + r'\s*\n\s*\.array-data ' + str(width)
               + r'\s*\n(.*?)^\s*\.end array-data')
    found = re.findall(pattern, text, re.M | re.S)
    if len(found) != 1:
        die('expected exactly one array: ' + label)
    words = re.sub(r'#[^\n]*', '', found[0]).split()
    values = [int(w.removesuffix('t'), 16) for w in words]
    if len(values) != count:
        die('wrong array length: ' + label)
    return values


def read_fields(text):
    links = re.findall(
        r'fill-array-data v1, :(\w+)\s+sput-object v1, [^\n]+->(sFuji\w+):(\[[IB])', text)
    result = {}
    for label, name, kind in links:
        data = read_array(text, label, 1 if kind == '[B' else 4, 2048 if kind == '[B' else 9)
        # byte arrays store negatives as two's complement; mask before pairing them
        result[name] = [v & 255 for v in data] if kind == '[B' else data
    return result


def simulate_lookup(text, signature, pid, strength):
    """Execute the lookup with a tiny interpreter; returns the sget-object field name."""
    body = method_span(text, signature).group()
    ops, labels = [], {}
    for raw in body.split('\n'):
        s = raw.strip()
        if not s or s.startswith('.') or s.startswith('#'):
            continue
        if s.startswith(':'):
            labels[s[1:]] = len(ops)
            continue
        ops.append(s)
    regs = {'v0': 0, 'v1': 0, 'v2': 0, 'v3': 0, 'p0': pid}
    pending, pc = None, 0
    while pc < len(ops):
        ins = ops[pc]
        if ins == 'nop':
            pc += 1
            continue
        m = re.match(r'const-string ([vp]\d+), (".*")$', ins)
        if m:
            regs[m.group(1)] = json.loads(m.group(2))
            pc += 1
            continue
        m = re.match(r'const(?:/4|/16|) ([vp]\d+), (0x[0-9a-fA-F]+|-?\d+)$', ins)
        if m:
            regs[m.group(1)] = int(m.group(2), 16) if m.group(2).startswith('0x') else int(m.group(2))
            pc += 1
            continue
        if re.match(r'invoke-static \{\}, .*->getStrength\(\)I$', ins):
            pending = strength
            pc += 1
            continue
        if 'String;->equals(Ljava/lang/Object;)Z' in ins:
            mm = re.match(r'invoke-virtual \{([vp]\d+), ([vp]\d+)\}', ins)
            pending = 1 if regs[mm.group(1)] == regs[mm.group(2)] else 0
            pc += 1
            continue
        m = re.match(r'move-result(-object)? ([vp]\d+)$', ins)
        if m:
            regs[m.group(2)] = pending
            pc += 1
            continue
        m = re.match(r'sget-object ([vp]\d+), .*->(\w+):\[[IB]$', ins)
        if m:
            regs[m.group(1)] = ('field', m.group(2))
            pc += 1
            continue
        m = re.match(r'if-(eqz|nez) ([vp]\d+), :(\w+)$', ins)
        if m:
            zero = regs[m.group(2)] == 0
            take = zero if m.group(1) == 'eqz' else (not zero)
            pc = labels[m.group(3)] if take else pc + 1
            continue
        m = re.match(r'if-ne ([vp]\d+), ([vp]\d+), :(\w+)$', ins)
        if m:
            pc = labels[m.group(3)] if regs[m.group(1)] != regs[m.group(2)] else pc + 1
            continue
        m = re.match(r'return-object ([vp]\d+)$', ins)
        if m:
            return regs[m.group(1)]
        die('simulator cannot execute: ' + ins)
    return None


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--apk', type=Path, required=True,
                    help='a built Film Studio APK to append presets to')
    ap.add_argument('--lut', type=Path, required=True,
                    help='preset JSON (visual designer export) or a .cube look LUT')
    ap.add_argument('--out', type=Path, help='output APK (default: <apk>-custom.apk)')
    ap.add_argument('--id', help='id for the new preset (default: from the LUT)')
    ap.add_argument('--name', help='menu name (default: from the LUT, prefixed 自定义 )')
    ap.add_argument('--replace', metavar='EXISTING_ID',
                    help='replace this built-in slot instead of appending a new one')
    ap.add_argument('--work', type=Path, help='apktool work dir (must be empty or absent)')
    ap.add_argument('--key', type=Path, default=Path.home() / '.workbuddy' / 'camera-lut-studio' / 'signing.pem',
                    help='v1 signing key; reuse the same one to allow adb install -r')
    ap.add_argument('--java'), ap.add_argument('--apktool')
    args = ap.parse_args()

    if not args.apk.exists():
        die('APK not found: %s' % args.apk)
    if not args.lut.exists():
        die('LUT not found: %s' % args.lut)

    java = args.java or find_java()
    if not java:
        die('no Java runtime found. apktool is a JAR and needs one.\n'
            '     Install via the WorkBuddy runtime dir:\n'
            '       ~/.workbuddy/binaries/java/versions/<ver>/bin/java.exe + versions/current\n'
            '     or set JAVA_HOME.')
    apktool = find_apktool(args.apktool)

    presets, warnings = load_lut(args.lut)
    for p in presets:
        if 'id' not in p or not p['id']:
            die('preset needs an id')
        # ids end up in smali string literals and XML attributes; keep them token-safe
        clean = re.sub(r'\s+', '-', str(p['id']))
        if clean != p['id']:
            print('note: preset id %r contains whitespace; using %r' % (p['id'], clean))
            p['id'] = clean
    ids_seen = [p['id'] for p in presets]
    dupes = sorted({i for i in ids_seen if ids_seen.count(i) > 1})
    if dupes:
        die('duplicate preset ids in the input: %s' % dupes)
    if args.id and len(presets) == 1:
        presets[0]['id'] = args.id
    if args.name and len(presets) == 1:
        presets[0]['name'] = args.name
    for p in presets:
        p['menu_name'] = p.get('value_name') or ('自定义 ' + p['name'])
        p['value_name'] = p['menu_name']
        p['value_guide'] = p.get('guide') or '本地自定义配方；未做实拍色彩标定，仅作风格近似。'

    print('Input   : %s' % args.apk.name)
    print('LUT     : %s' % args.lut)
    print('Java    : %s' % java)
    print('apktool : %s' % apktool)
    for w in warnings:
        print('WARN    : %s' % w)

    work = args.work or Path(tempfile.mkdtemp(prefix='lut-inject-'))
    if work.exists() and any(work.iterdir()):
        die('work dir is not empty: %s' % work)
    out = args.out or args.apk.with_name(args.apk.stem + '-custom.apk')

    print('\n[1/5] decompiling')
    subprocess.run([java, '-jar', apktool, 'd', '-r', '-f', str(args.apk), '-o', str(work)],
                   check=True, stdout=subprocess.DEVNULL)

    hook_path = next(iter(work.rglob(HOOK_NAME)), None)
    layout_path = next(iter(work.rglob(LAYOUT_NAME)), None)
    if not hook_path or not layout_path:
        die('this does not look like a Film Studio APK (RicohHook/menu layout not found)')
    hook = hook_path.read_text(encoding='utf-8')
    indices = sorted({int(m.group(1)) for m in PRESET_FIELD.finditer(hook)})
    if not indices:
        die('no preset arrays found; is this a Film Studio build?')
    if indices != list(range(len(indices))):
        die('preset indices are not contiguous: %s' % indices)

    menu_path = work / MENU_PATH
    placed = []          # (index, preset) pairs, in the order they land in the menu
    if args.replace:
        if len(presets) != 1:
            die('--replace takes a single preset; %d given' % len(presets))
        ids_method = method_span(hook, 'getPresetIds()Ljava/util/List;').group()
        found = re.findall(r'const-string v\d+, "([^"]+)"', ids_method)
        if args.replace not in found:
            die('--replace id %s not present; available: %s' % (args.replace, found))
        target = found.index(args.replace)
        print('\n[2/5] replacing slot %d (%s) - total stays %d'
              % (target, args.replace, len(indices)))
        hook = replace_slot(hook, target, presets[0])
        placed = [(target, presets[0])]
        layout = layout_path.read_text(encoding='utf-8')
        if args.replace != presets[0]['id']:
            layout = layout.replace(quote(args.replace), quote(presets[0]['id']))
        layout_path.write_text(patch_icon_map(layout, presets), encoding='utf-8')
        rename_menu_item(menu_path, args.replace, presets[0])
    else:
        base = len(indices)
        print('\n[2/5] appending %d preset(s) at index %d..%d'
              % (len(presets), base, base + len(presets) - 1))
        hook = patch_hook(hook, base, presets)
        placed = [(base + n, p) for n, p in enumerate(presets)]
        layout = layout_path.read_text(encoding='utf-8')
        layout_path.write_text(patch_icon_map(layout, presets), encoding='utf-8')
        patch_menu(menu_path, presets)
    hook_path.write_text(hook, encoding='utf-8')

    unsigned = work.parent / (args.apk.stem + '-unsigned.apk')
    print('[3/5] rebuilding')
    subprocess.run([java, '-jar', apktool, 'b', str(work), '-o', str(unsigned)],
                   check=True, stdout=subprocess.DEVNULL)

    print('[4/5] signing')
    pem, generated = ensure_pem(args.key)
    if generated:
        print('  generated signing key: %s' % pem)
        print('  KEEP IT: reuse the same key so later builds install with -r')
    sign_apk(str(unsigned), str(out), pem, note_out=note)

    print('[5/5] verifying')
    verify(str(out), placed, java, apktool)

    size_delta = out.stat().st_size - args.apk.stat().st_size
    print('\nDONE  %s' % out)
    print('  %d bytes (%+d vs source)  sha256 %s'
          % (out.stat().st_size, size_delta,
             hashlib.sha256(out.read_bytes()).hexdigest()))
    for index, item in placed:
        print('  menu entry [%d] %s  (id %s)' % (index, item['menu_name'], item['id']))
    print('\nInstall (uninstall any previous build of the same package first,')
    print('a different signing key makes adb install -r fail):')
    print('  adb connect CAMERA_IP:5555')
    print('  adb -s CAMERA_IP:5555 install -r "%s"' % out)
    return 0


def rename_slot_strings(hook, old_id, preset):
    """Swap the name/guide strings a renamed slot returns.

    Renaming the id alone would leave getFilterName serving the old label, so the
    menu entry and the active-filter readout would disagree.
    """
    for signature, new_value in (
            ('getFilterName(Ljava/lang/String;)Ljava/lang/String;', preset['value_name']),
            ('getFilterGuide(Ljava/lang/String;)Ljava/lang/String;', preset['value_guide'])):
        match = method_span(hook, signature)
        body = match.group()
        idx = body.index('const-string v0, ' + quote(old_id))
        tail = body[idx:]
        found = re.search(r'if-\w+ [vp]\d+, :\w+\s*\n\s*const-string v0, ("(?:\\.|[^"\\])*")',
                          tail)
        if not found:
            die('cannot locate the value string for %s in %s' % (old_id, signature))
        new_body = (body[:idx] + tail[:found.start(1)] + quote(new_value)
                    + tail[found.end(1):])
        if not new_body.startswith('.method '):
            die('internal error: the method header was dropped for ' + signature)
        hook = hook[:match.start()] + new_body + hook[match.end():]
    return hook


def replace_slot(hook, target, preset):
    """Repoint an existing slot: swap its label strings, arrays and id."""
    ids_method = method_span(hook, 'getPresetIds()Ljava/util/List;').group()
    old_id = re.findall(r'const-string v\d+, "([^"]+)"', ids_method)[target]
    if old_id == preset['id']:
        die('--replace target already uses id %s' % preset['id'])
    hook = rename_slot_strings(hook, old_id, preset)
    # the id appears only in the five lookup tables and getPresetIds
    hook = hook.replace('"' + old_id + '"', '"' + preset['id'] + '"')
    for strength in STRENGTHS:
        blend = blend_profile(preset, strength)
        for kind, values, width in (
                ('matrix', sum(blend['matrix'], []), 4),
                ('gamma', [b for v in blend['gamma'] for b in (v & 255, v >> 8)], 1)):
            field = 'sFuji%s%d_%d' % (kind, target, strength)
            link = re.search(r'fill-array-data v1, :(\w+)\s+sput-object v1, [^\n]+->%s:' % field,
                             hook)
            if not link:
                die('cannot find array for ' + field)
            label = link.group(1)
            hook = re.sub(r'^[ ]+:%s\s*\n[ ]+\.array-data %d\s*\n.*?^[ ]+\.end array-data'
                          % (re.escape(label), width),
                          '\n'.join(array_data_block(label, values, width)), hook,
                          count=1, flags=re.M | re.S)
    validate_labels(hook, HOOK_NAME)
    validate_reachable(hook, HOOK_NAME, [preset])
    return hook


def rename_menu_item(path, old_id, preset):
    tree = ET.parse(path)
    root = tree.getroot()
    top = next(e for e in root.iter() if e.get('ItemId') == MENU_PARENT)
    hit = None
    for item in top:
        if item.get('ItemId') == old_id:
            hit = item
            break
    if hit is None:
        die('%s not found in %s' % (old_id, MENU_PATH))
    hit.attrib.update(ItemId=preset['id'], Value=preset['id'],
                      Title=preset['menu_name'], DisplayName=preset['menu_name'])
    write_xml_lf(root, path)


def decompiled_hook(apk, java, apktool):
    """Re-decompile the product APK and return its RicohHook.smali text."""
    work = Path(tempfile.mkdtemp(prefix='lut-verify-'))
    try:
        subprocess.run([java, '-jar', apktool, 'd', '-r', '-f', str(apk), '-o', str(work)],
                       check=True, stdout=subprocess.DEVNULL)
        hook = next(iter(work.rglob(HOOK_NAME)))
        return hook.read_text(encoding='utf-8')
    finally:
        shutil.rmtree(work, ignore_errors=True)


def verify(apk, placed, java, apktool):
    """Re-decompile the product, then check every placed preset's arrays and lookups."""
    with zipfile.ZipFile(apk) as z:
        menu = z.read(MENU_PATH).decode('utf-8')
    if '\r\n' in menu:
        die('MenuData.xml has CRLF line endings; the shipped asset uses LF')
    lines = menu.split('\n')
    text = decompiled_hook(apk, java, apktool)
    fields = read_fields(text)

    checks = []
    for index, preset in placed:
        menu_id = preset['id']
        hits = [i for i, l in enumerate(lines) if 'ItemId="%s"' % menu_id in l]
        checks.append(('[%d] menu XML lists the entry once' % index, len(hits) == 1))
        if hits:
            i = hits[0]
            checks.append(('[%d] menu entry is not self-closing' % index,
                           not lines[i].rstrip().endswith('/>')))
            checks.append(('[%d] menu entry has its own closing tag' % index,
                           i + 1 < len(lines) and lines[i + 1].strip() == '</Layer2>'))
        for strength in STRENGTHS:
            exp = blend_profile(preset, strength)
            raw = fields.get('sFujigamma%d_%d' % (index, strength))
            got_g = [raw[k] | raw[k + 1] << 8 for k in range(0, 2048, 2)] if raw else None
            checks.append(('[%d] matrix %d%%' % (index, strength),
                           fields.get('sFujimatrix%d_%d' % (index, strength))
                           == sum(exp['matrix'], [])))
            checks.append(('[%d] gamma  %d%%' % (index, strength),
                           got_g == list(exp['gamma'])))
        for signature, kind in (('getRGBMatrix(Ljava/lang/String;)[I', 'matrix'),
                                ('getGammaBytes(Ljava/lang/String;)[B', 'gamma')):
            got = simulate_lookup(text, signature, menu_id, 100)
            checks.append(('[%d] %s resolves' % (index, signature.split('(')[0]),
                           isinstance(got, tuple)
                           and got[1] == 'sFuji%s%d_100' % (kind, index)))
        got_name = simulate_lookup(text, 'getFilterName(Ljava/lang/String;)Ljava/lang/String;',
                                   menu_id, 100)
        checks.append(('[%d] getFilterName returns the menu name' % index,
                       got_name == preset['menu_name']))
    checks.append(('unknown id still returns null',
                   simulate_lookup(text, 'getRGBMatrix(Ljava/lang/String;)[I', 'no-such-id', 100)
                   in (None, 0)))

    for name, ok in checks:
        print('  %s %s' % ('OK  ' if ok else 'FAIL', name))
    bad = [name for name, ok in checks if not ok]
    if bad:
        die('verification failed: %s' % bad)


if __name__ == '__main__':
    sys.exit(main())
