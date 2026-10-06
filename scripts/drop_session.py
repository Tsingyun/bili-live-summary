#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""drop_session.py —— 把某个直播日「整场重置」，用于从录播站补档后重跑。

场景：本地录播只落了一部分（比如录播软件 22:26 才启动），管线把残缺场当整场
处理并写进 state 与月度文档；之后从 rec.darkpy.cn 补齐了完整场次，就必须把
该日**整场清干净**再让 daily_pipeline 重跑，否则：
  · state.done_dates 门禁会让新落位的完整场被跳过（live_date 不大于已处理最晚日）
  · 月度文档会出现同一天两个章节

本脚本做三件事（都默认可逆）：
  1. 删除该日在月度文档里的所有 level-2 章节（先自动备份 .bak_drop_<date>）
  2. --reset-state：state.done_dates 去掉该日；processed/uploads 里该日记录移除
  3. --quarantine：该日的 MP3 / *_compressed.mp4 **重命名**加 .partial_<date> 后缀
     （不删除！上传器按 *_compressed.mp4 匹配，改名后既不会被当待传稿件，
       也随时能改回来）

用法：
  python drop_session.py --config config.json --date 2026-10-05 \
      --reset-state --quarantine
  加 --dry-run 只打印计划
"""
from __future__ import print_function

import argparse
import datetime
import glob
import io
import json
import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from common import load_config, Logger          # noqa: E402
import summarize_gemini as SG                    # noqa: E402
from redo_session import remove_section          # noqa: E402


def _date_tokens(d):
    """标题里可能出现的日期写法：2026年10月05日 / 2026年10月5日 / 2026-10-05 / 20261005。

    ★ 必须同时给「补零」和「不补零」两种：文档里的场次标题走的是
    `YYYY年MM月DD日`（补零），而按 strftime('%d') 生成的是不补零形式，
    只写一种会 0 命中且不报错（实测踩过：--dry-run 报「章节命中 0 个」）。
    """
    y, m, dd = d.year, d.month, d.day
    return ['%d年%02d月%02d日' % (y, m, dd),
            '%d年%d月%d日' % (y, m, dd),
            d.strftime('%Y-%m-%d'),
            d.strftime('%Y%m%d')]


def _match_heading(text, tokens):
    t = (text or '').strip()
    return any(tok in t for tok in tokens)


def main():
    ap = argparse.ArgumentParser(description='重置某直播日（补档后整场重跑用）')
    ap.add_argument('--config', default=None)
    ap.add_argument('--date', required=True, help='直播日期 YYYY-MM-DD')
    ap.add_argument('--reset-state', action='store_true', help='清 state 门禁与记录')
    ap.add_argument('--quarantine', action='store_true', help='改名隔离该日 MP3/压缩视频')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()

    cfg = load_config(a.config)
    log = Logger(cfg, 'drop_session')
    d = datetime.date.fromisoformat(a.date)
    iso = d.isoformat()
    compact = d.strftime('%Y%m%d')
    tokens = _date_tokens(d)

    doc_path = SG.doc_path_for(cfg, d)
    font = cfg['output'].get('font', '思源宋体CN Light')
    base = cfg['_dirs']['root']
    print('日期      :', iso)
    print('月度文档  :', doc_path)
    print('state     :', os.path.join(base, 'state.json'))

    # ---------- ① 文档章节 ----------
    heads = []
    if os.path.exists(doc_path):
        doc, _ = SG.open_or_create(doc_path, font)
        heads = [p.text.strip() for p in doc.paragraphs
                 if SG.heading_level(p) == 2 and _match_heading(p.text, tokens)]
    print('\n[1] 文档章节命中 %d 个：' % len(heads))
    for h in heads:
        print('    ·', h)
    if not heads:
        print('    （无）')

    # ---------- ② state ----------
    st_path = os.path.join(base, 'state.json')
    st = json.load(io.open(st_path, encoding='utf-8')) if os.path.exists(st_path) else {}
    in_done = iso in st.get('done_dates', [])
    proc_keys = [k for k, v in (st.get('processed') or {}).items()
                 if iso in (str(v.get('live_date', '')), k.replace('LOCAL-', ''))]
    up_keys = [k for k in (st.get('uploads') or {}) if k == iso]
    print('\n[2] state：done_dates 含 %s = %s；processed 命中 %s；uploads 命中 %s'
          % (iso, in_done, proc_keys or '无', up_keys or '无'))

    # ---------- ③ 待隔离产物 ----------
    # MP3 真实落点是 config.mp3_export.out_dir（不是 _dirs.mp3，那个目录常年是空的）
    mp3_dir = (cfg.get('mp3_export') or {}).get('out_dir') or cfg['_dirs'].get('mp3') or ''
    rec_root = (cfg.get('local') or {}).get('root', '')
    q = []

    def _want(p):
        """只收真正的产物：MP3 与 *_compressed.mp4。

        ★ 必须按后缀白名单过滤：早期用 `*<compact>*` 裸 glob 会把
        `.docx.bak_drop_<date>` 这类**备份文件**也扫进来（实测误伤）。
        同时排除已隔离过的（.partial_），保证重复执行幂等。
        """
        b = os.path.basename(p)
        if '.partial_' in b:
            return False
        lb = b.lower()
        return lb.endswith('.mp3') or lb.endswith('_compressed.mp4')

    for pat in (os.path.join(mp3_dir, '*%s*.mp3' % compact) if mp3_dir else '',
                os.path.join(rec_root, '*', '*%s*_compressed.mp4' % compact),
                os.path.join(rec_root, '*%s*_compressed.mp4' % compact)):
        if not pat:
            continue
        for p in glob.glob(pat):
            if _want(p) and p not in q:
                q.append(p)
    print('\n[3] 待隔离产物 %d 个（MP3 目录：%s）：' % (len(q), mp3_dir or '未配置'))
    for p in q:
        print('    · %s (%.2f GB)' % (p, os.path.getsize(p) / 1024.0 ** 3
                                       if os.path.exists(p) else 0))

    if a.dry_run:
        print('\n（--dry-run，未做任何改动）')
        return 0

    # ---------- 执行 ----------
    print('\n=== 执行 ===')
    if heads:
        bak = '%s.bak_drop_%s' % (doc_path, compact)
        shutil.copy2(doc_path, bak)
        doc, _ = SG.open_or_create(doc_path, font)
        for h in heads:
            n = remove_section(doc, h)
            print('  已删章节「%s」：%d 段' % (h[:40], n))
        SG.save_doc_atomic(doc, doc_path, log)
        print('  备份：%s' % os.path.basename(bak))
        # 不在这里调 ensure_front_matter：它要传 doc 对象而不是路径（传路径会
        # AttributeError），且目录/大纲级别由随后那次 daily_pipeline 自己重整。

    if a.reset_state:
        bak = st_path + '.bak_drop_%s' % compact
        shutil.copy2(st_path, bak)
        st = json.load(io.open(st_path, encoding='utf-8'))
        if iso in st.get('done_dates', []):
            st['done_dates'].remove(iso)
        for k in proc_keys:
            (st.get('processed') or {}).pop(k, None)
        for k in up_keys:
            (st.get('uploads') or {}).pop(k, None)
        st['updated_at'] = datetime.datetime.now().isoformat(timespec='seconds')
        st['note'] = ('%s 整场重置（补档后重跑，drop_session）' % iso)
        tmp = st_path + '.part'
        json.dump(st, io.open(tmp, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
        os.replace(tmp, st_path)
        print('  state 已重置，备份：%s' % os.path.basename(bak))

    if a.quarantine:
        for p in q:
            if not os.path.exists(p):
                continue
            dst = p + '.partial_%s' % compact
            if os.path.exists(dst):
                print('  已存在隔离文件，跳过：%s' % os.path.basename(dst))
                continue
            os.replace(p, dst)
            print('  已隔离：%s' % os.path.basename(dst))

    print('\n完成。现在可跑：daily_pipeline.py --date %s' % iso)
    return 0


if __name__ == '__main__':
    sys.exit(main())
