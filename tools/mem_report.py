# -*- coding: utf-8 -*-
"""把 .scratch/mem-probe/<label>/summary.json 汇总成一张 Markdown 对照表。

    .venv/Scripts/python.exe tools/mem_report.py p180-base p180-cut1 p480-base p480-cut1
    .venv/Scripts/python.exe tools/mem_report.py --all

口径列取自 run.json（kind/source/seconds/env_extra/overrides），数字列取自
summary.json（进程内 WS 或 ext_ws.csv 的进程外 WS）。同表并排即可做
「同配置同流程」的 before/after 比对。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / '.scratch' / 'mem-probe'


def _load(label: str) -> dict | None:
    run = SCRATCH / label / 'run.json'
    summary = SCRATCH / label / 'summary.json'
    if not run.is_file() or not summary.is_file():
        return None
    meta = json.loads(run.read_text(encoding='utf-8'))
    data = json.loads(summary.read_text(encoding='utf-8'))
    census = data.get('census') or {}
    return {
        'label': label,
        'kind': meta.get('kind', 'source'),
        'seconds': meta.get('seconds'),
        'topology': next((e for e in meta.get('env_extra', [])
                          if e.startswith('PET_RENDER_TOPOLOGY')), ''),
        'overrides': '; '.join(meta.get('overrides', [])) or '-',
        't15': data.get('ws_t15_mb'),
        'late_avg': data.get('ws_late_avg_mb') or data.get('ws_late_max_mb'),
        'late_max': data.get('ws_late_max_mb'),
        'peak': data.get('ws_peak_mb'),
        'private': data.get('private_late_avg_mb'),
        'children': data.get('children_max'),
        'threads': data.get('threads_late'),
        'clips': data.get('clip_late_n'),
        'qframes': census.get('clip_queue_frames'),
        'qmb': census.get('clip_queue_mb'),
        'first_mb': census.get('clip_first_frame_mb'),
        'qimage_mb': data.get('qimage_late_mb') or census.get('qimage_mb'),
        'qpixmap_n': data.get('qpixmap_late_n') or census.get('qpixmap_n'),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('labels', nargs='*')
    parser.add_argument('--all', action='store_true')
    args = parser.parse_args()

    labels = args.labels
    if args.all or not labels:
        labels = sorted(p.name for p in SCRATCH.iterdir()
                        if p.is_dir() and not p.name.startswith('appdata-'))
    rows = [row for row in (_load(label) for label in labels) if row]
    if not rows:
        print('没有可用产物：先跑 tools/mem_run.py / tools/ext_ws_probe.py')
        return 1

    head = ('| label | 口径 | 秒 | 拓扑/覆盖 | t=15s | 稳态均值 | 稳态峰值 | 总峰值 '
            '| 私有字节 | 子进程 | 线程 | clip | 残留帧 | 残留帧MB | 首帧MB '
            '| QImage MB | QPixmap |')
    print(head)
    print('|' + '---|' * 16)
    for row in rows:
        print('| {label} | {kind} | {seconds} | {topology}{overrides} | {t15} | '
              '{late_avg} | {late_max} | {peak} | {private} | {children} | {threads} | '
              '{clips} | {qframes} | {qmb} | {first_mb} | {qimage_mb} | {qpixmap_n} |'.format(
                  topology=row['topology'] or 'overlay(默认)',
                  overrides=row['overrides'],
                  **{k: ('' if row[k] is None else row[k]) for k in row
                     if k not in ('topology', 'overrides')}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
