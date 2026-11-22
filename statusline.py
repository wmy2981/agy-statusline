#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Antigravity CLI (agy) status line - ported from wmy2981/cc-statusline
# Shows: session | path · branch · repo | progress bar · model | diff | quota (5h/7d)
from __future__ import annotations

import io
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# ── ANSI helpers ──────────────────────────────────────────────────────────────
RST = '\033[0m'
BOLD = '\033[1m'

CYAN   = '\033[96m'        # path
BLUE   = '\033[94m'        # git branch
ORANGE = '\033[38;5;214m'  # remote repo
GRAY   = '\033[90m'        # session name, diff/quota labels, reset time, peak/offpeak
WHITE  = '\033[97m'        # deepseek balance (distinct from all other colours)
GREEN  = '\033[92m'        # low usage (< 50%)
YELLOW = '\033[93m'        # medium usage (50% - 80%)
RED    = '\033[91m'        # high usage (>= 80%)
MAG    = '\033[95m'        # model
ADD    = '\033[38;5;76m'   # diff added (distinct from GREEN)
DEL    = '\033[38;5;160m'  # diff deleted (distinct from RED)

_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
_MAX_RESET_SECONDS = 366 * 86400

# ── git helpers ───────────────────────────────────────────────────────────────

def _run_git(args: list[str], cwd: str | None) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ['git', *args],
            cwd=cwd or None,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=1.5,
            creationflags=_NO_WINDOW,
        )
        return proc.returncode == 0, proc.stdout.strip()
    except Exception:
        return False, ''

def _git_branch(cwd: str | None) -> tuple[bool, str]:
    """Return (is_git_repo, branch_or_short_sha)."""
    ok, branch = _run_git(['branch', '--show-current'], cwd)
    if not ok:
        return False, ''
    if branch:
        return True, branch
    ok_sha, sha = _run_git(['rev-parse', '--short', 'HEAD'], cwd)
    return True, (sha if ok_sha else '')

def _short_host(host: str) -> str:
    """Abbreviate common git hosts."""
    h = host.lower().removesuffix('.com').removesuffix('.org')
    return {'github': 'gh', 'gitlab': 'gl', 'bitbucket': 'bb'}.get(h, h)

def _parse_remote_url(url: str) -> tuple[str, str, str]:
    """Parse git remote URL into (host, owner, name)."""
    url = url.strip()
    if not url:
        return '', '', ''

    scp_match = re.match(r'^(?:[^@]+@)?([^:/]+):(.+)$', url)
    if '://' not in url and scp_match:
        host = scp_match.group(1)
        path = scp_match.group(2)
    else:
        parsed = urlparse(url)
        host = parsed.hostname or ''
        path = parsed.path or ''

    path = path.strip('/')
    if path.endswith('.git'):
        path = path[:-4]

    if '/' not in path:
        return host, '', path

    owner, name = path.rsplit('/', 1)
    return host, owner, name

def _git_repo_info(ws: dict, cwd: str | None, is_git: bool) -> tuple[str, str, str]:
    repo = ws.get('repo') or {}
    if isinstance(repo, dict):
        host = str(repo.get('host') or '')
        owner = str(repo.get('owner') or '')
        name = str(repo.get('name') or '')
        if owner and name:
            return host, owner, name

    if not is_git:
        return '', '', ''

    ok, remote_url = _run_git(['config', '--get', 'remote.origin.url'], cwd)
    if not ok or not remote_url:
        return '', '', ''
    return _parse_remote_url(remote_url)

def _parse_numstat(output: str) -> tuple[int, int]:
    added = 0
    removed = 0
    for line in output.splitlines():
        parts = line.split('\t')
        if len(parts) < 2:
            continue
        if parts[0].isdigit():
            added += int(parts[0])
        if parts[1].isdigit():
            removed += int(parts[1])
    return added, removed

def _git_diff_stats(cwd: str | None) -> tuple[int, int]:
    ok, out = _run_git(['diff', 'HEAD', '--numstat'], cwd)
    if ok:
        return _parse_numstat(out)
    added = 0
    removed = 0
    for extra_args in (['diff', '--cached', '--numstat'], ['diff', '--numstat']):
        ok_sub, out_sub = _run_git(extra_args, cwd)
        if ok_sub and out_sub:
            a, r = _parse_numstat(out_sub)
            added += a
            removed += r
    return added, removed

# ── formatting helpers ────────────────────────────────────────────────────────

def _pct(val: object) -> float:
    if val is None:
        return 0.0
    try:
        v = float(str(val).rstrip('%'))
        return v if math.isfinite(v) else 0.0
    except (ValueError, TypeError):
        return 0.0

def _usage_colour(pct: float) -> str:
    if pct < 50:
        return GREEN
    if pct < 80:
        return YELLOW
    return RED

def _progress_bar(pct: float, width: int = 20) -> str:
    clamped = max(0.0, min(100.0, pct))
    filled = round(clamped / (100 / width))
    bar = '█' * filled + '░' * (width - filled)
    colour = _usage_colour(clamped)
    return f'{colour}[{bar}]{RST}{colour} {clamped:.0f}%{RST}'

# ── agy subscription quota helpers ────────────────────────────────────────────

_AGY_DIR = Path.home() / '.gemini' / 'antigravity-cli'
_AGY_SETTINGS_PATH = _AGY_DIR / 'settings.json'

def _unescape_pbtxt_string(s: str) -> str:
    """Decode protobuf text-format string escapes (including octal UTF-8)."""
    out = bytearray()
    i = 0
    n = len(s)
    simple = {'n': 10, 'r': 13, 't': 9, '\\': 92, '"': 34, "'": 39}
    while i < n:
        ch = s[i]
        if ch == '\\' and i + 1 < n:
            nxt = s[i + 1]
            if nxt in simple:
                out.append(simple[nxt])
                i += 2
            elif nxt in '01234567':
                m = re.match(r'[0-7]{1,3}', s[i + 1:i + 4])
                if m:
                    out.append(int(m.group(0), 8) & 0xFF)
                    i += 1 + len(m.group(0))
                else:
                    out.extend(ch.encode('utf-8'))
                    i += 1
            elif nxt in ('x', 'X') and i + 3 < n:
                try:
                    out.append(int(s[i + 2:i + 4], 16))
                    i += 4
                except ValueError:
                    out.extend(ch.encode('utf-8'))
                    i += 1
            else:
                out.extend(nxt.encode('utf-8'))
                i += 2
        else:
            out.extend(ch.encode('utf-8'))
            i += 1
    return out.decode('utf-8', errors='replace')

def _read_title_from_annotations(conv_id: str) -> str:
    if not re.fullmatch(r'[0-9A-Za-z_-]+', conv_id):
        return ''
    pbtxt = _AGY_DIR / 'annotations' / f'{conv_id}.pbtxt'
    try:
        text = pbtxt.read_text(encoding='utf-8', errors='replace')
    except Exception:
        return ''
    m = re.search(r'(?m)^\s*title\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    if not m:
        return ''
    return _unescape_pbtxt_string(m.group(1)).strip()

def _read_title_from_metadata(conv_id: str) -> str:
    meta_path = _AGY_DIR / 'cache' / 'conversation_metadata.json'
    try:
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
        entry = (meta.get('conversations') or {}).get(conv_id) or {}
        title = ((entry.get('summary') or {}).get('Title')) or ''
        return str(title).strip()
    except Exception:
        return ''

def _read_title_from_summaries_db(conv_id: str) -> str:
    db_path = _AGY_DIR / 'conversation_summaries.db'
    if not db_path.is_file():
        return ''
    try:
        uri = f'file:{db_path.as_posix()}?mode=ro'
        with sqlite3.connect(uri, uri=True, timeout=0.3) as conn:
            row = conn.execute(
                'SELECT title FROM conversation_summaries WHERE conversation_id = ?',
                (conv_id,),
            ).fetchone()
            if row and row[0]:
                return str(row[0]).strip()
    except Exception:
        pass
    return ''

def _resolve_session_label(data: dict, conv_id: str) -> str:
    """Resolve conversation title from payload, local agy stores, or short ID."""
    for key in ('conversation_title', 'session_name'):
        val = str(data.get(key) or '').strip()
        if val:
            return ' '.join(val.split())

    if conv_id:
        for reader in (
            _read_title_from_annotations,
            _read_title_from_summaries_db,
            _read_title_from_metadata,
        ):
            title = reader(conv_id)
            if title:
                return ' '.join(title.split())

    return conv_id[:8]

def _load_agy_settings() -> dict:
    try:
        raw = _AGY_SETTINGS_PATH.read_text(encoding='utf-8')
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}

def _is_custom_or_api_key_model(model: dict) -> bool:
    """Return True if agy is configured to use a Gemini API key or custom model."""
    settings = _load_agy_settings()
    if str(settings.get('modelProvider') or '').strip().lower() == 'gemini':
        return True

    custom_cfg = settings.get('customModelsConfig')
    if isinstance(custom_cfg, dict) and custom_cfg:
        model_ids = {
            str(model.get('id') or '').strip().lower(),
            str(model.get('display_name') or '').strip().lower(),
        }
        model_ids.discard('')
        models_list = custom_cfg.get('models')
        if isinstance(models_list, list):
            for entry in models_list:
                if not isinstance(entry, dict):
                    continue
                for k in ('id', 'model', 'name', 'displayName', 'display_name'):
                    val = str(entry.get(k) or '').strip().lower()
                    if val and val in model_ids:
                        return True
    return False

def _finite_float(val: object) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, str) and not val.strip():
        return None
    try:
        f = float(val)
        return f if math.isfinite(f) else None
    except (ValueError, TypeError):
        return None

def _reset_seconds(bucket: dict) -> int | None:
    sec = _finite_float(bucket.get('reset_in_seconds'))
    if sec is not None:
        s = int(round(sec))
        return s if 0 < s <= _MAX_RESET_SECONDS else None

    reset_time = bucket.get('reset_time')
    if isinstance(reset_time, str) and reset_time.strip():
        try:
            iso = reset_time.strip().replace('Z', '+00:00')
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            s = int(round((dt - datetime.now(timezone.utc)).total_seconds()))
            return s if 0 < s <= _MAX_RESET_SECONDS else None
        except Exception:
            return None
    return None

def _format_reset(seconds: int | None) -> str:
    if seconds is None or seconds <= 0:
        return ''
    if seconds >= 86400:
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        return f'{days}d{hours}h' if hours else f'{days}d'
    if seconds >= 3600:
        hours = seconds // 3600
        mins = (seconds % 3600) // 60
        return f'{hours}h{mins}m' if mins else f'{hours}h'
    if seconds >= 60:
        return f'{seconds // 60}m'
    return '<1m'

def _bucket_window(key: str) -> str:
    k = key.lower()
    if k.endswith('-5h') or '-5h-' in k or 'five_hour' in k or 'fivehour' in k:
        return '5h'
    if k.endswith('-weekly') or '-weekly-' in k or 'weekly' in k or k.endswith('-7d'):
        return '7d'
    return ''

def _usable_quota_buckets(raw_quota: object) -> dict[str, dict]:
    if not isinstance(raw_quota, dict):
        return {}
    usable: dict[str, dict] = {}
    for key, val in raw_quota.items():
        if not isinstance(key, str) or not isinstance(val, dict):
            continue
        if val.get('disabled') is True:
            continue
        frac = _finite_float(val.get('remaining_fraction'))
        if frac is None:
            continue
        usable[key] = {
            'remaining_fraction': max(0.0, min(1.0, frac)),
            'reset_seconds': _reset_seconds(val),
            'window': _bucket_window(key),
        }
    return usable

def _select_model_quota(
    data: dict,
    model: dict,
) -> tuple[str, list[tuple[str, dict]]]:
    """Select independent quota buckets for the active agy subscription model.

    Returns (pool_label, [(window_label, bucket_info), ...]).
    Returns ('', []) when not using an agy subscription model or no quota exists.
    """
    if str(data.get('product') or '').lower() != 'antigravity':
        return '', []
    if not model:
        return '', []
    if _is_custom_or_api_key_model(model):
        return '', []

    usable = _usable_quota_buckets(data.get('quota'))
    if not usable:
        return '', []

    model_text = ' '.join(
        str(model.get(k) or '') for k in ('display_name', 'id')
    ).lower().strip()
    if not model_text:
        return '', []

    def _pick(keys: list[str]) -> list[tuple[str, dict]]:
        by_win: dict[str, dict] = {}
        extras: list[tuple[str, dict]] = []
        for k in keys:
            info = usable[k]
            win = info['window']
            if win and win not in by_win:
                by_win[win] = info
            elif not win:
                extras.append((k, info))
        ordered: list[tuple[str, dict]] = []
        for w in ('5h', '7d'):
            if w in by_win:
                ordered.append((w, by_win[w]))
        ordered.extend(extras)
        return ordered

    # 1. Gemini 4 Argon (and Orchestrator Argon): dedicated Argon bucket on Pro,
    #    falls back to standard Gemini pool on Ultra where Argon shares Gemini buckets.
    if 'argon' in model_text:
        argon_keys = [k for k in usable if 'argon' in k.lower()]
        if argon_keys:
            return 'argon', _pick(argon_keys)
        gemini_keys = [
            k for k in usable
            if k.lower().startswith('gemini-') and 'argon' not in k.lower()
        ]
        if gemini_keys:
            return 'gemini', _pick(gemini_keys)
        return '', []

    # 2. Third-party partner models included in agy subscription (Claude / GPT-OSS)
    if re.search(r'\b(claude|sonnet|opus|haiku|gpt)\b', model_text):
        for specific in ('claude', 'opus', 'sonnet', 'haiku', 'gpt'):
            if specific in model_text:
                spec_keys = [k for k in usable if specific in k.lower()]
                if spec_keys:
                    return specific, _pick(spec_keys)
        tp_keys = [k for k in usable if k.lower().startswith('3p-')]
        if tp_keys:
            return '3p', _pick(tp_keys)
        return '', []

    # 3. Native Gemini models (Neon / Carbon / Hydrogen / Flash / Pro / Orchestrator)
    if re.search(r'\b(gemini|neon|carbon|hydrogen|orchestrator)\b', model_text):
        # Check for any dedicated model-specific bucket first (future-proof)
        for sub in ('neon', 'carbon', 'hydrogen', 'flash', 'pro'):
            if sub in model_text:
                sub_keys = [
                    k for k in usable
                    if k.lower().startswith(f'gemini-{sub}-')
                ]
                if sub_keys:
                    return sub, _pick(sub_keys)

        # Standard shared Gemini pool (exclude model-specific buckets like gemini-argon-*)
        standard_keys = [
            k for k in ('gemini-5h', 'gemini-weekly') if k in usable
        ]
        if not standard_keys:
            standard_keys = [
                k for k in usable
                if k.lower().startswith('gemini-') and 'argon' not in k.lower()
            ]
        if standard_keys:
            return 'gemini', _pick(standard_keys)
        return '', []

    # Non-subscription / unrecognized custom model -> do not display agy quota
    return '', []

def _format_quota_line(data: dict, model: dict) -> str:
    pool_label, items = _select_model_quota(data, model)
    if not items:
        return ''

    term_width = int(_finite_float(data.get('terminal_width')) or 0)
    compact = 0 < term_width < 75

    segments = [f'{GRAY}quota({pool_label}):{RST}']
    for win_label, info in items:
        used_pct = (1.0 - info['remaining_fraction']) * 100.0
        if compact:
            colour = _usage_colour(used_pct)
            meter = f'{colour}{used_pct:.0f}%{RST}'
        else:
            meter = _progress_bar(used_pct, width=8)

        reset_str = _format_reset(info['reset_seconds'])
        seg = f'{GRAY}{win_label}{RST} {meter}'
        if reset_str:
            seg += f' {GRAY}({reset_str}){RST}'
        segments.append(seg)

    return '  '.join(segments)

# ── deepseek extras (only for model names containing 'deepseek') ──────────────

DEEPSEEK_BALANCE_URL = 'https://api.deepseek.com/user/balance'
BALANCE_CACHE_TTL = 60  # seconds
BALANCE_CACHE_FILE = Path(tempfile.gettempdir()) / 'agy-statusline-deepseek-balance.json'

def _pricing_at(t: datetime) -> str:
    """peak/offpeak for a Beijing-local datetime: Mon-Fri 9:00-12:00, 14:00-18:00 = peak."""
    minutes = t.hour * 60 + t.minute
    peak = t.weekday() < 5 and ((9 * 60 <= minutes < 12 * 60) or (14 * 60 <= minutes < 18 * 60))
    return 'peak' if peak else 'offpeak'

def _deepseek_label() -> str:
    """Beijing time is UTC+8 with no DST, so a fixed offset from system UTC suffices."""
    return _pricing_at(datetime.now(timezone.utc) + timedelta(hours=8))

def _deepseek_key() -> str:
    key = os.environ.get('DEEPSEEK_API_KEY') or ''
    if key:
        return key
    try:
        raw = Path.home().joinpath('.claude', 'settings.json').read_text(encoding='utf-8')
        env = json.loads(raw).get('env', {})
        return env.get('ANTHROPIC_AUTH_TOKEN') or env.get('ANTHROPIC_API_KEY') or ''
    except Exception:
        return ''

def _read_balance_cache() -> str | None:
    try:
        ts, val = json.loads(BALANCE_CACHE_FILE.read_text(encoding='utf-8'))
        if datetime.now().timestamp() - ts < BALANCE_CACHE_TTL:
            return val
    except Exception:
        pass
    return None

def _write_balance_cache(val: str) -> None:
    try:
        BALANCE_CACHE_FILE.write_text(
            json.dumps([datetime.now().timestamp(), val]),
            encoding='utf-8',
        )
    except Exception:
        pass

def _deepseek_balance(key: str) -> str:
    """Formatted balance like '¥110.00'/'$12.34', or '' on any failure (not cached)."""
    if not key:
        return ''
    cached = _read_balance_cache()
    if cached is not None:
        return cached
    try:
        req = Request(
            DEEPSEEK_BALANCE_URL,
            headers={'Authorization': f'Bearer {key}', 'Accept': 'application/json'},
        )
        with urlopen(req, timeout=2) as resp:
            data = json.load(resp)
        infos = data.get('balance_infos') or []
        if not infos:
            return ''
        currency = str(infos[0].get('currency', ''))
        amount = str(infos[0].get('total_balance', '')).strip()
        if not amount:
            return ''
        symbol = {'CNY': '¥', 'USD': '$'}.get(currency, f'{currency} ')
        result = f'{symbol}{amount}'
    except Exception:
        return ''
    _write_balance_cache(result)
    return result

# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    raw = sys.stdin.buffer.read()
    if not raw or not raw.strip():
        return
    try:
        data = json.loads(raw)
    except Exception:
        return
    if not isinstance(data, dict):
        return

    ws    = data.get('workspace') or {}
    ctx   = data.get('context_window') or {}
    model = data.get('model') or {}
    cost  = data.get('cost') or {}

    lines: list[list[str]] = [[], [], [], [], []]  # up to 5 lines

    # ── Line 1: session / conversation name ───────────────────────────────────
    conv_id = str(data.get('conversation_id') or data.get('session_id') or '').strip()
    session_label = _resolve_session_label(data, conv_id)
    if session_label:
        lines[0].append(f'{GRAY}{BOLD}{session_label}{RST}')

    # ── Line 2: path  branch  repo ────────────────────────────────────────────
    native_dir = str(ws.get('current_dir') or data.get('cwd') or '')
    display_path = native_dir.replace('\\', '/') if native_dir else '?'
    lines[1].append(f"{CYAN}'{display_path}'{RST}")

    repo_dir = native_dir if (native_dir and os.path.isdir(native_dir)) else None
    is_git, branch = _git_branch(repo_dir)
    if branch:
        lines[1].append(f'{BLUE}[{branch}]{RST}')

    host, owner, name = _git_repo_info(ws, repo_dir, is_git)
    if host and owner and name:
        short = _short_host(host)
        lines[1].append(f'{ORANGE}{short}:{owner}/{name}{RST}')
    elif owner and name:
        lines[1].append(f'{ORANGE}{owner}/{name}{RST}')

    # ── Line 3: progress bar  model ───────────────────────────────────────────
    pct = _pct(ctx.get('used_percentage', 0))
    lines[2].append(_progress_bar(pct))

    model_name = str(model.get('display_name') or model.get('id') or '—')
    model_seg = f'{MAG}{model_name}{RST}'

    # deepseek extras: (peak|offpeak) + balance, only for deepseek models
    if 'deepseek' in model_name.lower():
        label = _deepseek_label()
        label_colour = RED if label == 'peak' else GRAY
        parts = [model_seg, f'{label_colour}({label}){RST}']
        amount = _deepseek_balance(_deepseek_key())
        if amount:
            parts.append(f'{WHITE}{amount}{RST}')
        lines[2].append(' '.join(parts))
    else:
        lines[2].append(model_seg)

    # ── Line 4: code changes ──────────────────────────────────────────────────
    added   = int(cost.get('total_lines_added') or 0)
    removed = int(cost.get('total_lines_removed') or 0)
    if not (added or removed) and is_git:
        added, removed = _git_diff_stats(repo_dir)

    if added or removed:
        diff_parts = [f'{GRAY}diff:{RST}']
        if added:
            diff_parts.append(f'{ADD}+{added}{RST}')
        if removed:
            diff_parts.append(f'{DEL}-{removed}{RST}')
        lines[3].append('  '.join(diff_parts))

    # ── Line 5: agy subscription model quota (5h & weekly) ────────────────────
    quota_line = _format_quota_line(data, model)
    if quota_line:
        lines[4].append(quota_line)

    # ── output ────────────────────────────────────────────────────────────────
    out = '\n'.join('  '.join(ln) for ln in lines if ln)
    if out:
        print(out)

if __name__ == '__main__':
    main()
