# -*- coding: utf-8 -*-
"""重跑单场总结（只重做「模型总结 + 写 Word」，跳过转写 / MP3 / 压缩）。

用途：提示词或写入规则修订后，对某一场重跑总结以应用新规则。
      TXT / SRT 复用已有转写结果，**不重跑 ASR**（省 2 分钟转写 + 避免 GPU 占用）。

流程：备份 docx → 删除该场章节 → process_session 重总结 → reorder → 打印用量。

用法：
  python redo_session.py --date 2026-10-02 --txt "20261002-标题 2026年10月02日20点场.txt" \
      [--config D:/岁己直播自动总结/config.json] [--src <源视频路径>] [--dry-run]

说明：
  · --heading 默认由 --txt 推导（去掉 "YYYYMMDD-" 前缀与 ".txt" 后缀）。
  · 章节已不存在（上一轮删过但总结失败）时会直接重生成，不会重复追加。
  · 失败时文档可能缺该场章节：TXT 仍在，修好后重跑即可；备份在同目录 .bak_redo_YYYYMMDD。
"""
import os
import sys
import shutil
import argparse
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import load_config, Logger                      # noqa: E402
import summarize_gemini as SG                               # noqa: E402
import regen_gemini_v2 as R                                 # noqa: E402
import model_router as MR                                   # noqa: E402
import reorder_doc as RD                                    # noqa: E402


def remove_section(doc, heading):
    """删除指定 level-2 标题及其到下一 level-2 之间的所有段落（含分页符）。返回删除段数。"""
    ps = doc.paragraphs
    start = None
    for i, p in enumerate(ps):
        if SG.heading_level(p) == 2 and p.text.strip() == heading:
            start = i
            break
    if start is None:
        return 0
    end = len(ps)
    for j in range(start + 1, len(ps)):
        if SG.heading_level(ps[j]) == 2:
            end = j
            break
    for k in range(start, end):
        el = ps[k]._p
        el.getparent().remove(el)
    return end - start


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', default=None, help='config.json 路径')
    ap.add_argument('--date', required=True, help='直播日期 YYYY-MM-DD')
    ap.add_argument('--txt', required=True, help='TXT 文件名（TXT 目录下）')
    ap.add_argument('--heading', default=None, help='章节标题；默认由 --txt 推导')
    ap.add_argument('--src', default=None, help='源视频路径（写入「来源」元数据行）')
    ap.add_argument('--dry-run', action='store_true', help='只打印计划，不改动文档')
    a = ap.parse_args()

    cfg = load_config(a.config)
    log = Logger(cfg, 'redo_session')
    font = cfg['output'].get('font', '思源宋体CN Light')
    d = datetime.date.fromisoformat(a.date)
    doc_path = SG.doc_path_for(cfg, d)
    heading = a.heading or (a.txt[:-4].split('-', 1)[1] if '-' in a.txt else a.txt[:-4])
    txt_path = os.path.join(cfg['_dirs']['txt'], a.txt)

    print('文档 :', doc_path)
    print('TXT  :', txt_path)
    print('章节 :', heading)
    if not os.path.exists(txt_path):
        print('❌ TXT 不存在，不能重跑（先跑转写）')
        return 1
    if a.dry_run:
        print('（--dry-run，未做任何改动）')
        return 0

    bak = '%s.bak_redo_%s' % (doc_path, datetime.date.today().strftime('%Y%m%d'))
    shutil.copy2(doc_path, bak)
    print('已备份:', bak)

    doc, _ = SG.open_or_create(doc_path, font)
    before = len(doc.paragraphs)
    n = remove_section(doc, heading)
    if n == 0:
        if any(SG.heading_level(p) == 2 and p.text.strip() == heading for p in doc.paragraphs):
            print('⚠️ 章节存在却删不掉，终止（避免重复追加）')
            return 1
        print('（章节已不存在，直接重生成）')
    else:
        SG.save_doc_atomic(doc, doc_path, log)
        doc2, _ = SG.open_or_create(doc_path, font)
        print('已删除章节：%d 段（段落 %d → %d）' % (n, before, len(doc2.paragraphs)))

    sys_prompt = R.load_prompt(cfg)
    rot = MR.build_rotator(cfg, log)
    # progress 必须传 dict（None 会在 process_session 里 AttributeError）
    ok = R.process_session(rot, sys_prompt, cfg, a.txt, a.date, font, log, {},
                           src_path=a.src)
    print('process_session ->', ok)
    if not ok:
        print('❌ 总结失败；TXT 仍在，可修复后重跑。回滚备份：', bak)
        return 1
    RD.reorder(doc_path)
    print('已重排')
    try:
        print(R.format_usage_report())
    except Exception as e:
        print('用量汇报异常：', e)
    return 0


if __name__ == '__main__':
    sys.exit(main())
