import re, sys
sys.stdout.reconfigure(encoding='utf-8')
with open('config.py', encoding='utf-8') as f:
    orig = f.read()
test = orig
test = re.sub(r'(STOP_LOSS\s*=\s*)[\d.]+', r'\g<1>0.25', test)
test = re.sub(r'(BASE_TARGET\s*=\s*)[\d.]+', r'\g<1>1.0', test)
test = re.sub(r'(TRAILING_ACTIVATION\s*=\s*)[\d.]+', r'\g<1>0.42', test)
lines_orig = orig.split('\n')
lines_new  = test.split('\n')
diffs = [(i+1, a, b) for i,(a,b) in enumerate(zip(lines_orig, lines_new)) if a!=b]
for ln, a, b in diffs:
    print(f'Line {ln}: {repr(a.strip()[:80])} => {repr(b.strip()[:80])}')
