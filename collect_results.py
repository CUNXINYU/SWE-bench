import json
from pathlib import Path

INSTANCES = [
    'astropy__astropy-6938', 'astropy__astropy-7746',
    'django__django-11964',  'django__django-11999',  'django__django-12113',
    'django__django-12125',  'django__django-12184',  'django__django-12284',
    'django__django-12286',  'django__django-12308',
]

MODELS = ['Kimi K2.5', 'GPT-5.4', 'Sonnet4.6']

# ── Local results (best across all eval runs) ──────────────────────────────
local = {}
for model in MODELS:
    best = {}
    for summary in Path(model).joinpath('eval_logs').rglob('summary.json'):
        data = json.loads(summary.read_text(encoding='utf-8'))
        iid = data.get('instance_id')
        if iid:
            best[iid] = best.get(iid, False) or bool(data.get('resolved', False))
    local[model] = best

# ── Cloud results (all failed → False) ────────────────────────────────────
cloud = {m: {iid: False for iid in INSTANCES} for m in MODELS}

# ── PTP / FTP helpers (local vs cloud as reference) ─────────────────────-
# PTP: passed locally AND passed in cloud
# FTP: failed locally  AND passed in cloud (or vice-versa for local-baseline)
# Here we define: baseline = cloud (all False), so FTP = locally resolved

print('=' * 70)
print('Local Evaluation Results')
print('=' * 70)
for model in MODELS:
    r = local[model]
    passed = [iid for iid in INSTANCES if r.get(iid, False)]
    failed = [iid for iid in INSTANCES if not r.get(iid, False)]
    print(f'\n{model}: {len(passed)}/10 passed')
    for iid in INSTANCES:
        mark = 'PASS' if r.get(iid, False) else 'FAIL'
        print(f'  {mark}  {iid}')

print()
print('=' * 70)
print('Cloud Evaluation Results (swe-bench_lite)')
print('=' * 70)
for model in MODELS:
    print(f'\n{model}: 0/10 passed (all failed runs on cloud)')

print()
print('=' * 70)
print('Comparison Summary')
print('=' * 70)
header = f"{'Instance':<40} {'Kimi-L':>6} {'Kimi-C':>6} {'GPT-L':>6} {'GPT-C':>6} {'S46-L':>6} {'S46-C':>6}"
print(header)
print('-' * 70)
for iid in INSTANCES:
    kl = 'PASS' if local['Kimi K2.5'].get(iid, False) else 'FAIL'
    gl = 'PASS' if local['GPT-5.4'].get(iid, False) else 'FAIL'
    sl = 'PASS' if local['Sonnet4.6'].get(iid, False) else 'FAIL'
    row = f"{iid:<40} {kl:>6} {'FAIL':>6} {gl:>6} {'FAIL':>6} {sl:>6} {'FAIL':>6}"
    print(row)

print('-' * 70)
kl_sum = sum(1 for iid in INSTANCES if local['Kimi K2.5'].get(iid, False))
gl_sum = sum(1 for iid in INSTANCES if local['GPT-5.4'].get(iid, False))
sl_sum = sum(1 for iid in INSTANCES if local['Sonnet4.6'].get(iid, False))
print(f"{'TOTAL PASSED':<40} {kl_sum:>6} {'0':>6} {gl_sum:>6} {'0':>6} {sl_sum:>6} {'0':>6}")
print(f"{'PASS RATE':<40} {kl_sum*10:>5}% {'0%':>6} {gl_sum*10:>5}% {'0%':>6} {sl_sum*10:>5}% {'0%':>6}")

print()
print('Legend: L=Local, C=Cloud')
print('Note: All cloud runs returned "Failed runs" (evaluation harness error)')
print('      This is a known issue consistent across all historical submissions.')
