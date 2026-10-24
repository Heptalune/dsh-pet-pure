# -*- coding: utf-8 -*-
"""打包产物外部采样器：给部署版 exe 同口径的 WS 曲线（进程外，零注入）。

为什么需要它：用户口径的 +170MB 来自**部署版**（onedir/pyinstaller）单宠稳态。
源码跑批能给出同口径的相对归因，但绝对值与打包版不一致；要"拆解 +170MB"就必须
对同一份 exe 做 before/after。打包版无法注入 tracemalloc（内嵌解释器 + pythonw），
故本脚本只做进程外采样：WS / PrivateBytes / 子进程数 / 句柄，2s 一行。

    .venv/Scripts/python.exe tools/ext_ws_probe.py --label pkg-overlay \
        --exe dist-onedir/dsh-pet-standalone-webm-chat/dsh-pet-standalone-webm-chat.exe \
        --seconds 180

产物落在 ``.scratch/mem-probe/<label>/``（ext_ws.csv / summary.json），与
``tools/mem_run.py`` 同一目录约定，便于并排比较。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / '.scratch' / 'mem-probe'

SKIP_DIRS = {
    'sessions', 'sessions-slot-1', 'sessions-slot-2', 'sessions-slot-3',
    'slots', 'screenshots', 'perfstats', 'agent-events', 'lyrics_cache',
    'voice_chime_cache', 'sounds_cache',
}
SKIP_SUFFIXES = ('.log', '.lock', '.tmp')
SKIP_NAMES = {'overlay-active-pets.json', 'collision-debug.log', 'fs_debug.log'}


def _prepare_appdata(source: Path, dest: Path, *, keep_active_pets: bool = False) -> None:
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    for entry in sorted(source.iterdir()):
        if entry.is_dir() or entry.name.endswith(SKIP_SUFFIXES):
            continue
        if entry.name in SKIP_NAMES:
            if keep_active_pets and entry.name == 'overlay-active-pets.json':
                shutil.copy2(entry, dest / entry.name)
            continue
        shutil.copy2(entry, dest / entry.name)


def _tree(proc: psutil.Process) -> list[psutil.Process]:
    try:
        return [proc, *proc.children(recursive=True)]
    except Exception:
        return [proc]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--label', required=True)
    parser.add_argument('--exe', required=True)
    parser.add_argument('--seconds', type=float, default=180.0)
    parser.add_argument('--sample-s', type=float, default=2.0)
    parser.add_argument('--env', action='append', default=[])
    parser.add_argument('--source', default=str(
        Path(os.environ.get('APPDATA', '')) / 'dsh-pet-standalone-webm-chat'))
    parser.add_argument('--app-dir-name', default='dsh-pet-standalone-webm-chat')
    parser.add_argument('--keep-active-pets', action='store_true',
                        help='保留真实 active-pets 清单（多宠复现用；默认跑单宠）')
    args = parser.parse_args()

    exe = Path(args.exe).resolve()
    if not exe.is_file():
        print(f'可执行文件不存在: {exe}', file=sys.stderr)
        return 2

    out_dir = SCRATCH / args.label
    appdata = SCRATCH / f'appdata-{args.label}'
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    _prepare_appdata(Path(args.source), appdata / args.app_dir_name,
                     keep_active_pets=args.keep_active_pets)

    env = dict(os.environ)
    env['APPDATA'] = str(appdata)
    env.pop('QT_QPA_PLATFORM', None)
    for item in args.env:
        key, _, value = item.partition('=')
        env[key.strip()] = value

    meta = {
        'label': args.label, 'kind': 'packaged', 'exe': str(exe),
        'seconds': args.seconds, 'appdata': str(appdata),
        'env_extra': args.env, 'exe_mtime': time.strftime(
            '%Y-%m-%d %H:%M:%S', time.localtime(exe.stat().st_mtime)),
        'started': time.strftime('%Y-%m-%d %H:%M:%S'),
    }
    (out_dir / 'run.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                      encoding='utf-8')

    creation = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    proc = subprocess.Popen([str(exe)], env=env, cwd=str(exe.parent),
                            creationflags=creation)
    ps = psutil.Process(proc.pid)
    csv_path = out_dir / 'ext_ws.csv'
    started = time.monotonic()
    rows = []
    try:
        with csv_path.open('w', newline='', encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                't', 'ws_mb', 'private_mb', 'peak_ws_mb', 'handles', 'threads', 'children'])
            writer.writeheader()
            handle.flush()
            while time.monotonic() - started < args.seconds:
                row = {'t': round(time.monotonic() - started, 2)}
                try:
                    info = ps.memory_info()
                    row['ws_mb'] = round(info.rss / 1048576.0, 2)
                    row['private_mb'] = round(getattr(info, 'private', 0) / 1048576.0, 2)
                    row['peak_ws_mb'] = round(
                        getattr(info, 'peak_wset', info.rss) / 1048576.0, 2)
                    row['handles'] = ps.num_handles() if hasattr(ps, 'num_handles') else 0
                    row['threads'] = ps.num_threads()
                    row['children'] = len(ps.children(recursive=True))
                except Exception:
                    pass
                writer.writerow(row)
                handle.flush()
                rows.append(row)
                time.sleep(args.sample_s)
    finally:
        try:
            subprocess.run(['taskkill', '/PID', str(proc.pid)],
                           capture_output=True, timeout=20)
        except Exception:
            pass
        try:
            proc.wait(timeout=12)
        except Exception:
            try:
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                               capture_output=True, timeout=20)
            except Exception:
                pass
        try:
            proc.wait(timeout=20)
        except Exception:
            pass

    def _f(row, key):
        try:
            return float(row.get(key) or 0.0)
        except Exception:
            return 0.0

    mark = max(0.0, args.seconds - 60.0)
    late = [r for r in rows if _f(r, 't') >= mark]
    early = [r for r in rows if _f(r, 't') <= 15.0]
    summary = dict(meta)
    if rows:
        peak = max(rows, key=lambda r: _f(r, 'ws_mb'))
        summary.update({
            'ws_start_mb': _f(rows[0], 'ws_mb'),
            'ws_t15_mb': _f(early[-1], 'ws_mb') if early else None,
            'ws_late_avg_mb': round(sum(_f(r, 'ws_mb') for r in late) / len(late), 2) if late else None,
            'ws_late_max_mb': round(max(_f(r, 'ws_mb') for r in late), 2) if late else None,
            'ws_peak_mb': _f(peak, 'ws_mb'),
            'ws_peak_t': _f(peak, 't'),
            'private_late_avg_mb': round(
                sum(_f(r, 'private_mb') for r in late) / len(late), 2) if late else None,
            'children_max': max(int(_f(r, 'children')) for r in rows),
            'threads_late': int(_f(rows[-1], 'threads')),
            'samples': len(rows),
        })
    (out_dir / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                          encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
