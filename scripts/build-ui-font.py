#!/usr/bin/env python3
"""Build the app's Microsoft YaHei UI subset and truthful fallback ranges.

Only characters are retained from the local business corpus; source prose is never
copied into the deployed font or report. The original full font stays available.
"""
from pathlib import Path
from collections import Counter
import hashlib, io, json, re, sys
from fontTools import subset
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path('/Library/Fonts/MicrosoftYaHei.ttf')
if not SOURCE.is_file():
    raise SystemExit('Microsoft YaHei source font unavailable; do not substitute another family.')
font = TTFont(SOURCE, recalcTimestamp=False)
font_family = font['name'].getDebugName(1)
if font_family != 'Microsoft YaHei':
    raise SystemExit('Source font family mismatch.')
supported = set(font.getBestCmap())
required_text = ''.join((ROOT / p).read_text() for p in [
    'public/index.html', 'public/admin.html', 'public/guest.js',
    'public/admin.js', 'server.py',
])
names = []
def material_names(value):
    if isinstance(value, dict):
        if isinstance(value.get('name'), str): names.append(value['name'])
        for v in value.values(): material_names(v)
    elif isinstance(value, list):
        for v in value: material_names(v)
for path in sorted((ROOT / 'output').glob('*.json')):
    if path.name.startswith(('import-library-', 'seed-demo-')):
        material_names(json.loads(path.read_text()))
required_text += ''.join(names)
initial_copy = ROOT / 'content/package-copy.json'
if initial_copy.is_file(): required_text += initial_copy.read_text()
required = {ord(c) for c in required_text} & supported
required |= set(range(32, 127)) & supported
required |= {0xa0, 0x2022, 0x25cf, 0x3000} & supported
frequencies = Counter()
for name in ['CONTEXT.md', 'LOG.md', 'TASKS.md']:
    path = ROOT.parent / name
    if path.is_file(): frequencies.update(re.findall('[\u3400-\u9fff]', path.read_text()))
for path in sorted((ROOT.parents[2] / '10-Context').glob('*.md')):
    frequencies.update(re.findall('[\u3400-\u9fff]', path.read_text()))
chosen = set(required)
for char, _ in frequencies.most_common():
    if len(chosen) >= 1800: break
    if ord(char) in supported: chosen.add(ord(char))
# Always retain actual app strings and names, even if they exceed the frequency target.
options = subset.Options()
options.flavor = 'woff2'; options.hinting = False
options.layout_features = ['kern']; options.drop_tables += ['DSIG', 'FFTM', 'MERG', 'meta']
subsetter = subset.Subsetter(options=options); subsetter.populate(unicodes=chosen)
subsetter.subset(font); font.flavor = 'woff2'
buffer = io.BytesIO(); font.save(buffer); data = buffer.getvalue()
sha = hashlib.sha256(data).hexdigest()[:12]
font_name = 'yahei-ui-' + sha + '.woff2'
(ROOT / 'public/fonts' / font_name).write_bytes(data)

def ranges(values):
    ordered = sorted(values)
    if not ordered: return ''
    groups = []; start = end = ordered[0]
    for value in ordered[1:]:
        if value == end + 1: end = value
        else: groups.append((start, end)); start = end = value
    groups.append((start, end))
    return ','.join(f'U+{a:X}' if a == b else f'U+{a:X}-{b:X}' for a, b in groups)

# A missing symbol must not trigger an 8.7 MB download which cannot supply it.
# Both declared faces are regular static fonts; browser weight synthesis uses
# these same Microsoft YaHei outlines for requested bold text.
ui_range = ranges(chosen)
full_range = ranges(supported - chosen)
faces = (
    "@font-face{font-family:Yahei;src:url('./fonts/" + font_name + "') format('woff2');font-weight:400;font-style:normal;font-display:swap;unicode-range:" + ui_range + "}"
    "@font-face{font-family:YaheiFull;src:url('./fonts/yahei.woff2') format('woff2');font-weight:400;font-style:normal;font-display:swap;unicode-range:" + full_range + "}"
)
css = (ROOT / 'public/app.css').read_text()
css = faces + '\n' + css.split('\n', 1)[1]
css = css.replace('font-synthesis:none', 'font-synthesis:weight')
(ROOT / 'public/app.css').write_text(css)
for page_name in ['index.html', 'admin.html']:
    html = (ROOT / 'public' / page_name).read_text()
    html = re.sub(r'\n  <link rel="preload"[^>]*yahei-ui[^>]*>', '', html)
    html = html.replace('  <link rel="stylesheet"', '  <link rel="preload" href="/aidog-photos/fonts/' + font_name + '" as="font" type="font/woff2" crossorigin>\n  <link rel="stylesheet"', 1)
    (ROOT / 'public' / page_name).write_text(html)
report = {
    'fontFamily': font_family, 'file': font_name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
    'codepoints': len(chosen), 'requiredCodepoints': len(required), 'materialNamesCovered': len(set(names)),
    'sourceIsRegular': True, 'uiDeclaredWeight': 400, 'bold': 'same-family weight synthesis',
    'fullFallbackRetained': True, 'fullFallbackExcludesUIAndUnsupportedCodepoints': True,
    'unsupportedSourceSymbols': sorted({c for c in required_text if ord(c) > 127 and ord(c) not in supported}),
}
(ROOT / 'output/字体性能优化.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
print(json.dumps(report, ensure_ascii=False))
