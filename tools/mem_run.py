# -*- coding: utf-8 -*-
"""内存跑批器：把真实用户配置复制到临时 APPDATA，跑一次 mem_probe 并落盘产物。

用法（仓库根，venv python）：

    .venv/Scripts/python.exe tools/mem_run.py --label base-overlay --seconds 200
    .venv/Scripts/python.exe tools/mem_run.py --label no-island --seconds 200 \
        --override dynamic_island.enabled=false
    .venv/Scripts/python.exe tools/mem_run.py --label legacy --seconds 200 \
        --env PET_RENDER_TOPOLOGY=legacy

产物目录 ``.scratch/mem-probe/<label>/``：

    ws.csv          每 2s 一行：WS / PrivateBytes / 句柄 / 线程 / 子进程 / tracemalloc
    summary.json    起跑/中期/稳态统计 + Qt 对象普查 + top-level widget 清单
    snap/*.txt      trace 模式的 tracemalloc 归因快照（按"最外层我方帧"聚合）
    child.log       子进程 stdout/stderr

隔离纪律：**绝不写真实配置目录**——APPDATA 指向
``.scratch/mem-probe/appdata-<label>/``，真实目录只被读取复制一次。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / '.scratch' / 'mem-probe'

SKIP_DIRS = {
    'sessions', 'sessions-slot-1', 'sessions-slot-2', 'sessions-slot-3',
    'slots', 'screenshots', 'perfstats', 'agent-events', 'lyrics_cache',
    'voice_chime_cache', 'sounds_cache', 'dsh-pet-standalone',
}
SKIP_SUFFIXES = ('.log', '.lock', '.tmp')
SKIP_NAMES = {
    'overlay-active-pets.json',   # 活跃宠清单：跑批一律单宠（主宠）
    'collision-debug.log',
    'fs_debug.log',
}


def _copy_config(source: Path, dest: Path) -> list[str]:
    copied: list[str] = []
    dest.mkdir(parents=True, exist_ok=True)
    for entry in sorted(source.iterdir()):
        if entry.is_dir():
            if entry.name in SKIP_DIRS:
                continue
            continue
        if entry.name in SKIP_NAMES:
            continue
        if entry.name.endswith(SKIP_SUFFIXES):
            continue
        shutil.copy2(entry, dest / entry.name)
        copied.append(entry.name)
    return copied


def _set_override(data: dict, dotted: str, raw: str) -> None:
    try:
        value = json.loads(raw)
    except ValueError:
        value = raw
    parts = dotted.split('.')
    node = data
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--label', required=True)
    parser.add_argument('--seconds', type=float, default=200.0)
    parser.add_argument('--mode', default='ws', choices=['ws', 'trace'])
    parser.add_argument('--env', action='append', default=[],
                        help='子进程环境变量 K=V，可重复')
    parser.add_argument('--override', action='append', default=[],
                        help='config.json 覆盖 KEY.PATH=JSON，可重复')
    parser.add_argument('--source', default=str(
        Path(os.environ.get('APPDATA', '')) / 'dsh-pet-standalone-webm-chat'))
    parser.add_argument('--sample-s', type=float, default=2.0)
    parser.add_argument('--snap-s', type=float, default=20.0)
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--code-root', default=str(ROOT),
                        help='被测代码根（默认本仓库；可用独立 worktree 隔离并发改动）')
    args = parser.parse_args()

    code_root = Path(args.code_root).resolve()
    probe = code_root / 'tools' / 'mem_probe.py'
    if not probe.is_file():
        print(f'探针不存在（{probe}）——先把它拷进该 worktree 的 tools/', file=sys.stderr)
        return 2

    source = Path(args.source)
    if not source.is_dir():
        print(f'源配置目录不存在: {source}', file=sys.stderr)
        return 2

    out_dir = SCRATCH / args.label
    appdata = SCRATCH / f'appdata-{args.label}'
    if appdata.exists():
        shutil.rmtree(appdata, ignore_errors=True)
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 源码运行的 APP_DIR_NAME 恒为 dsh-pet-standalone（无 build_variant）。
    config_dir = appdata / 'dsh-pet-standalone'
    copied = _copy_config(source, config_dir)

    if args.override:
        config_path = config_dir / 'config.json'
        data = json.loads(config_path.read_text(encoding='utf-8'))
        for item in args.override:
            dotted, _, raw = item.partition('=')
            _set_override(data, dotted.strip(), raw)
        config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                               encoding='utf-8')

    env = dict(os.environ)
    env['APPDATA'] = str(appdata)
    env['PET_MEM_TRACE_DIR'] = str(out_dir)
    env['PET_MEM_TRACE_MODE'] = args.mode
    env['PET_MEM_TRACE_LABEL'] = args.label
    env['PET_MEM_TRACE_SECONDS'] = str(args.seconds)
    env['PET_MEM_TRACE_SAMPLE_S'] = str(args.sample_s)
    env['PET_MEM_TRACE_SNAP_S'] = str(args.snap_s)
    env.pop('QT_QPA_PLATFORM', None)
    for item in args.env:
        key, _, value = item.partition('=')
        env[key.strip()] = value
    env['PYTHONUNBUFFERED'] = '1'

    meta = {
        'label': args.label,
        'seconds': args.seconds,
        'mode': args.mode,
        'code_root': str(code_root),
        'source_config': str(source),
        'appdata': str(appdata),
        'copied_files': copied,
        'env_extra': args.env,
        'overrides': args.override,
        'started': time.strftime('%Y-%m-%d %H:%M:%S'),
    }
    (out_dir / 'run.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                      encoding='utf-8')
    print(json.dumps(meta, ensure_ascii=False, indent=2))

    log_path = out_dir / 'child.log'
    with log_path.open('w', encoding='utf-8') as log:
        proc = subprocess.run([args.python, str(probe)], env=env, cwd=str(code_root),
                              stdout=log, stderr=subprocess.STDOUT)
    print(f'child exit code: {proc.returncode}')
    summary_path = out_dir / 'summary.json'
    if summary_path.is_file():
        print(summary_path.read_text(encoding='utf-8'))
    else:
        tail = log_path.read_text(encoding='utf-8', errors='replace')[-4000:]
        print('--- child.log tail ---')
        print(tail)
    return 0


if __name__ == '__main__':
    sys.exit(main())
