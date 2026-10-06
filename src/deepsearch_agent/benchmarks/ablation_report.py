"""Render a Chinese ablation report from immutable local run artifacts."""
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import html
import json
import os
from pathlib import Path

from deepsearch_agent.benchmarks.ablation_metrics import summarize_ablation
from deepsearch_agent.benchmarks.badcases import attribute_cases
from deepsearch_agent.local_files import atomic_write


ARMS = ('fixed_workflow', 'agent_full', 'agent_no_rerank', 'agent_no_ocr', 'agent_no_visual')
LABELS = {'fixed_workflow': '固定检索工作流', 'agent_full': '完整工具决策',
          'agent_no_rerank': '关闭重排', 'agent_no_ocr': '关闭在线 OCR 工具',
          'agent_no_visual': '关闭研究/回答/核验角色原图', 'oracle': '给定原图阅读参考'}


def _json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _rows(path):
    return [_json_line(line) for line in Path(path).read_text(encoding='utf-8').splitlines() if line.strip()]


def _json_line(line):
    return json.loads(line)


def _cell(value):
    return html.escape(str(value), quote=False).replace('|', '\\|').replace('\n', ' ').replace('\r', '')


def _table(headers, rows):
    return '\n'.join(['| ' + ' | '.join(map(_cell, headers)) + ' |',
                      '| ' + ' | '.join('---' for _ in headers) + ' |',
                      *['| ' + ' | '.join(map(_cell, row)) + ' |' for row in rows]])


def _link(path, destination, label=None, suffix=''):
    try:
        target = Path(os.path.relpath(Path(path).resolve(), destination.parent.resolve())).as_posix()
    except ValueError:
        target = Path(path).resolve().as_posix()
    return f'[{label or Path(path).name}](<{target}{suffix}>)'


def _number(value, precision=2):
    return '未上报' if value is None else f'{value:.{precision}f}'


def _metric(record, binary=False):
    value, count = record.get('value'), record.get('denominator', 0)
    if value is None:
        return f'未观测 (n={count})'
    if binary:
        total = record.get('numerator', value * count)
        return f'{value * 100:.1f}% ({total:g}/{count})'
    return f'{value:.4f} (n={count})'


def _answer(variant, task, metric):
    return _metric(variant.get('by_task', {}).get(task, {}).get('answer_metrics', {}).get(metric, {}),
                   binary=metric != 'element_f1')


def _usage(calls):
    requested = [call for call in calls if call.get('status') != 'budget_exceeded']
    values = [call['total_tokens'] for call in requested if type(call.get('total_tokens')) is int]
    return len(requested), sum(values) if values else None, len(values)


def _checked_inputs(prepared, output):
    run, summary = _json(output / 'run.json'), _json(output / 'summary.json')
    manifests, cases = {}, []
    for dataset in ('chartqa', 'pdfvqa'):
        path = prepared / dataset / 'manifest.json'
        if hashlib.sha256(path.read_bytes()).hexdigest() != run.get('manifests', {}).get(dataset):
            raise ValueError('Run manifest identity mismatch')
        manifests[dataset] = _json(path)
        cases.extend(manifests[dataset]['cases'])
    if [case['id'] for case in cases] != run.get('case_ids'):
        raise ValueError('Run case IDs mismatch')
    rows, badcases = _rows(output / 'predictions.jsonl'), _rows(output / 'badcases.jsonl')
    expected = summarize_ablation(cases, rows, summary['k'], variants=run['variants'])
    if any(summary.get(key) != expected[key] for key in ('variants', 'paired_vs_agent_full')):
        raise ValueError('Stored summary does not match predictions; rescore first')
    allowed = {(case['id'], variant) for case in cases for variant in run['variants']}
    keys = [(row['id'], row['variant']) for row in badcases]
    if len(set(keys)) != len(keys) or not set(keys) <= allowed:
        raise ValueError('Unknown or duplicate badcase annotations')
    expected_badcases = {(b['id'], b['variant']): b for b in attribute_cases(cases, rows,
        oracle_rows=[r for r in rows if r['variant'] == 'oracle'])}
    if set(keys) != set(expected_badcases) or any(b.get('automatic') != expected_badcases[(b['id'], b['variant'])]['automatic']
            or b.get('taxonomy_version') != expected_badcases[(b['id'], b['variant'])]['taxonomy_version'] for b in badcases):
        raise ValueError('Stored badcase attribution is stale; rescore first')
    return run, summary, manifests, cases, rows, badcases


def _coverage(cases, row_map, variant, field, gold_field='gold_pages'):
    values = []
    for case in cases:
        row = row_map.get((case['id'], variant), (None, {}))[1]
        gold = set(case[gold_field])
        if gold and isinstance(row.get(field), list):
            values.append(len(gold & set(row[field])) / len(gold))
    return _metric({'value': sum(values) / len(values) if values else None, 'denominator': len(values)})


def write_report(prepared: Path, output: Path, destination: Path, *, acceptance_output: Path | None = None):
    """Write only the report; reject stale scores or changed data identities."""
    prepared, output, destination = Path(prepared), Path(output), Path(destination)
    run, summary, manifests, cases, rows, badcases = _checked_inputs(prepared, output)
    variants, configuration = summary['variants'], run['configuration']
    arms = [name for name in ARMS if name in variants]
    case_map = {case['id']: case for case in cases}
    row_map = {(row['id'], row['variant']): (index, row) for index, row in enumerate(rows, 1)}
    annotations = {(row['id'], row['variant']): row for row in badcases}
    counts = Counter(case['task'] for case in cases)
    completed, planned = len(rows), len(cases) * len(run['variants'])
    date = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d')
    blocks = ['# Agentic RAG 小批量消融实验报告',
        f'生成日期：{date}（北京时间）。实际完成 **{completed}/{planned}** 个题目-配置组合，'
        + ('本轮已全部完成。' if completed == planned else '**部分完成**；缺失组合保留在评分分母中。'),
        f'固定样本：ChartQA {counts.get("chartqa", 0)} 题，PDFVQA A {counts.get("A", 0)} 题，'
        f'PDFVQA B {counts.get("B", 0)} 题。下面均由本轮原始记录离线计算；未合并不同任务的答案指标。',
        '## 数据与干预',
        _table(['项目', '本轮记录'], [
            ['模型', configuration.get('model', '未记录')],
            ['检索通道', configuration.get('retrieval', '未记录，不能推断')],
            ['检索 top-k', summary['k']],
            ['单题模型请求预算', configuration.get('max_model_calls', '未记录')],
            ['上下文 / 最大输出 Token', f'{configuration.get("context_window", "未记录")} / {configuration.get("max_output_tokens", "未记录")}'],
            ['Agent 预算', json.dumps(configuration.get('agent', {}), ensure_ascii=False)],
            ['关闭能力', ', '.join(configuration.get('disabled', [])) or '未记录'],
            ['运行期 OCR 缓存', configuration.get('ocr_cache', '未记录')],
            ['图像干预的配置原文', configuration.get('no_visual', '未记录')],
        ]),
        _table(['配置', '干预'], [[name, LABELS[name]] for name in arms]),
        '所有实验组共用同一固定语料和索引；关闭在线 OCR 工具并不删除预处理 OCR。'
        '关闭角色原图输入仍保留其余明确配置的能力，不能解释为整个系统没有视觉信息。'
        '固定工作流仍包含 Supervisor/Answerer/Verifier，仅研究阶段使用原有固定检索路径。',
    ]
    observed_main = [b for b in badcases if b.get('variant') in arms and b.get('primary_code') != 'missing_result']
    observed_codes = Counter(b['primary_code'] for b in observed_main)
    total_tools = sum(t['attempts'] for name in arms for t in variants[name]['tools'].values())
    successful_tools = sum(t['success_count'] for name in arms for t in variants[name]['tools'].values())
    highlights = ['## 本轮结论']
    if 'agent_full' in variants:
        highlights.append('完整 agent：ChartQA ' + _answer(variants['agent_full'], 'chartqa', 'relaxed_accuracy')
            + '，PDF A ' + _answer(variants['agent_full'], 'A', 'accuracy')
            + '，PDF B 集合 EM ' + _answer(variants['agent_full'], 'B', 'element_exact_match') + '。'
            + ('运行尚未结束，此处包括未完成项的零分，不宜比较优劣。' if completed < planned else
               '这里只描述这 12 道题的一次运行，不证明组件稳定优劣。'))
    highlights.append(f'已完成的 {len(observed_main)} 条 RAG 轨迹自动主标签包括：格式失败 {observed_codes["model_format_error"]}，'
        f'预算耗尽 {observed_codes["budget_exhausted"]}，弃答 {observed_codes["abstention"]}，'
        f'待细分的阅读/推理失败 {observed_codes["generation_reading_or_reasoning"]}。'
        '因此不能把全部错题都算作召回问题，也不能把“有页仍答错”自动解释成 OCR 错误。')
    highlights.append(f'自然工具调用中正常返回 {successful_tools}/{total_tools} 次。这是执行成功率，不是工具选择准确率或答案正确率；'
                      '后文分别检查新增证据、重复调用、金页命中和最终回答。')
    if configuration.get('retrieval_fallback_reason') == 'image_embedding_http_402':
        highlights.append('本轮图像 embedding 服务 HTTP 402，**统一采用 BM25＋预处理 OCR 召回**；多模态重排和原图阅读仍参与，直接图像向量检索未验收。')
    blocks[3:3] = highlights
    retrieval = str(configuration.get('retrieval', '')).lower()
    fallback = str(configuration.get('retrieval_fallback_reason', ''))
    if 'bm25' in retrieval and ('402' in fallback or '402' in retrieval or 'only' in retrieval or '仅' in retrieval or retrieval.strip() == 'bm25'):
        blocks.append('本轮使用 BM25 检索，**直接图像向量检索未评估**。'
            + ('图像嵌入接口返回 HTTP 402，统一索引构建后改用同一 BM25 配置完成各组。'
               if '402' in fallback or '402' in retrieval else '请以运行配置中的回退原因作为依据。'))
    elif 'image' in retrieval or '图像' in retrieval:
        blocks.append('运行配置声明使用图像向量通道；文本侧按配置为 BM25，不能据此宣称测试了文本嵌入检索。')
    blocks += [
        'PDFVQA A/B 是已给定页面的任务，页级召回只说明该页片段是否被返回，不能衡量多文档选页。'
        'ChartQA 的跨图检索包含依赖“这张图”等指代的问题，缺少原任务给定的目标图，因此属于受控检索诊断，不能直接比较官方给定图像成绩。'
        '样本很小且每个配置只运行一次；抽样来自本地验证子集，PDF 页面镜像的元素匹配规则也限制了推广范围。'
        '本轮 memory、skills、外部 MCP 按配置关闭，不代表这些能力已经通过端到端质量评测。',
        '## 答案效果',
        _table(['配置', 'ChartQA relaxed accuracy', 'PDF A accuracy', 'PDF B 集合 EM', 'PDF B 集合 F1', '错误 / 弃答 / 缺失'], [
            [name, _answer(variants[name], 'chartqa', 'relaxed_accuracy'), _answer(variants[name], 'A', 'accuracy'),
             _answer(variants[name], 'B', 'element_exact_match'), _answer(variants[name], 'B', 'element_f1'),
             f'{variants[name]["error_count"]} / {variants[name].get("abstained_count", 0)} / {variants[name]["missing_count"]}']
            for name in arms]),
        '错误包括服务失败与无效输出，弃答（abstention）单列；三者均保留在全题分母并计零。'
        'ChartQA relaxed accuracy 使用既有 5% 数值容差规则；PDF A 为固定答案准确率；PDF B 为元素 ID 集合匹配及逐题集合 F1。'
        'F1 表格中的 n 是题数，不是元素数。无效格式不会通过事后猜测或答案改写补分。',
        '### 给定图像阅读参考',
    ]
    if 'oracle' in variants:
        oracle = variants['oracle']
        blocks.append(_table(['配置', 'ChartQA relaxed accuracy', 'PDF A accuracy', 'PDF B 集合 EM', 'PDF B 集合 F1', '错误 / 缺失'], [[
            'oracle', _answer(oracle, 'chartqa', 'relaxed_accuracy'), _answer(oracle, 'A', 'accuracy'),
            _answer(oracle, 'B', 'element_exact_match'), _answer(oracle, 'B', 'element_f1'),
            f'{oracle["error_count"]} / {oracle["missing_count"]}']]))
    blocks.append('oracle 直接给定题目对应原图，并按任务提供输入元素目录，是独立的阅读参考，**不保证是上界**。'
        '其提示词与角色流程不同；给定页面也不是模型实际产生的引用。因此不将 oracle 的页命中、引用率或相对差值当作普通组件消融。')

    groups = ('chartqa/chartqa/human', 'chartqa/chartqa/augmented')
    blocks += ['### 题型与标签分布', _table(['配置', 'ChartQA human', 'ChartQA augmented'], [
        [name, *[_metric(variants[name].get('by_group', {}).get(group, {}).get('answer_metrics', {}).get('relaxed_accuracy', {}),
                        binary=True) for group in groups]] for name in [*arms, *(['oracle'] if 'oracle' in variants else [])]]),
        'human 子集中的泛指问题依赖给定原图，跨图检索任务本身不充分；augmented 子集中的实体和时间通常更明确。两组仍都保留，不按结果删题。']
    labels = Counter(c['answers'][0] for c in cases if c['task'] == 'A')
    if labels:
        label, count = labels.most_common(1)[0]
        blocks.append(f'PDFVQA A 金标分布：{json.dumps(dict(labels), ensure_ascii=False)}；'
                      f'本子集固定回答“{label}”可得 {count}/{sum(labels.values())}。这是标签分布的描述性基线，不是检索结果。')

    blocks += ['## 逐阶段评估', '### 检索与证据保留',
        _table(['配置', '全部候选召回', '首次 Recall@k', '首次 MRR@k', '工具后金页命中', '已收集金页命中', '最终引用页精确率', '最终引用页覆盖'], [
            [name, *[_metric(variants[name]['retrieval'][key], binary=key.endswith('gold_hit')) for key in (
                'candidate_recall', 'first_search_recall_at_k', 'first_search_mrr_at_k', 'post_tool_gold_hit',
                'collected_evidence_gold_hit', 'final_citation_page_precision', 'final_citation_page_coverage')]] for name in arms]),
        '这一表按有金标准页的题目平均，表内保留各自 n。全部候选召回使用各次成功检索的去重页并集；'
        '首次指标只取第一次检索。固定工作流没有工具节点，其“工具后”列不应解释为检索失败。'
        '最终引用页指标只是与金标准页的重合代理，不验证引用是否真正支持结论。',
        _table(['配置', 'PDF B 检索文本元素覆盖', 'PDF B 已收集文本元素覆盖'], [
            [name, *[_metric(variants[name].get('by_task', {}).get('B', {}).get('element_text_coverage', {}).get(key, {}))
                     for key in ('retrieved_recall', 'evidence_recall')]] for name in arms]),
        '元素覆盖只检查文本中记录的目录 ID，不证明图像可读、模型理解正确或元素完整进入截断后的实际提示。'
        '缺少该观测字段时不按零覆盖处理；已收集证据与模型实际上下文的差别需结合提示记录检查。',
        _table(['配置', '收集金页覆盖', '最后回答上下文金页覆盖', '最后回答原图金页覆盖', 'PDF B 上下文文本元素覆盖'], [
            [name, *[_coverage(cases, row_map, name, field) for field in ('evidence_ranking', 'context_ranking', 'original_image_pages')],
             _coverage([c for c in cases if c['task'] == 'B'], row_map, name, 'context_element_ids', 'gold_element_ids')] for name in arms]),
        '这些覆盖率只对相应字段有观测的固定题目计算，n 单列。上下文指最后准备给 Answerer 的输入；实际请求状态见 answerer_request_status。'
        '原图列仅统计对应模型请求中确实包含图片、且未被预算拦截的页面。no_visual 的原图覆盖为零是实验干预，不是未完成调用。',
        '### 重排',
        _table(['配置', '调用数', '前 Recall@k', '后 Recall@k', '前 MRR@k', '后 MRR@k', '状态计数'], [
            [name, variants[name]['rerank']['attempts'], *[_metric(variants[name]['rerank'][key]) for key in (
                'before_recall_at_k', 'after_recall_at_k', 'before_mrr_at_k', 'after_mrr_at_k')],
             json.dumps(variants[name]['rerank']['status_counts'], ensure_ascii=False)] for name in arms]),
        '重排表按实际调用加权，属于诊断；同一道题的多次重排会重复贡献，不能当作独立题目样本。'
        '关闭重排组在相同查询和 top-k 下保留相同候选获取规则；不同模型轨迹仍可能产生不同查询、top-k 和调用次数，不保证候选完全相同。单候选可能直接跳过重排。',
        '### 工具调用',
    ]
    tool_rows = []
    for name in arms:
        for tool, value in variants[name]['tools'].items():
            tool_rows.append([name, tool, value['attempts'], value['success_count'], value['error_count'],
                value['invalid_count'], value['repeated_identical_count'], value['new_evidence_calls'],
                _metric(value['gold_page_hit'], binary=True), json.dumps(value['error_code_counts'], ensure_ascii=False)])
    blocks.append(_table(['配置', '工具', '尝试', '成功', '失败', '参数无效', '相同调用重复', '新增证据调用', '金页命中', '错误码'], tool_rows)
                  if tool_rows else '本轮已记录预测中尚无工具调用。')
    blocks += [
        '工具成功表示调用正常返回，不等于答案正确。重复调用按单题内同工具及规范化参数判断；新增证据依赖新的 chunk_id。'
        '金页命中仅在工具有明确排名观测时计算，目录查询等无页排名工具不凭空记为零。',
        '### 生成与核验',
        _table(['配置', 'ChartQA 已获金页条件准确率', 'PDF A 已获金页条件准确率', 'PDF B 已获金页条件 EM', '核验接受 claim', '正确草稿→错误终答', '错误草稿→正确终答'], [
            [name, *[_metric(variants[name].get('by_task', {}).get(task, {}).get('answer_when_gold_available', {}).get(metric, {}), binary=True)
                for task, metric in (('chartqa', 'relaxed_accuracy'), ('A', 'accuracy'), ('B', 'element_exact_match'))],
             _metric(variants[name]['verifier']['accepted_claims'], binary=True),
             variants[name]['verifier']['correct_draft_final_incorrect'], variants[name]['verifier']['incorrect_draft_final_correct']]
            for name in arms]),
        '条件准确率分母只包含明确收集到金标准页的题目，不能代表全样本质量。核验接受率是模型自检结果，不是人工忠实性评价。'
        '草稿到终答变化仅覆盖可以解析为单一任务答案的最终草稿；这种变化记录不证明核验模块的因果贡献。',
        '## 调用、Token 与延迟',
        _table(['配置', '请求 / 预算拒绝', '成功模型调用', '已上报总 Token', 'Token 已上报 / 请求', '题目延迟均值 / P50 / P95 秒'], [
            [name, f'{variants[name]["costs"]["model_calls"]} / {variants[name]["costs"].get("budget_blocked_calls", 0)}',
             variants[name]['costs']['success_count'], _number(variants[name]['costs']['tokens']['total_tokens']['sum'], 0),
             f'{variants[name]["costs"]["tokens"]["total_tokens"]["reported_calls"]} / {variants[name]["costs"]["model_calls"]}',
             ' / '.join(_number(variants[name]['costs']['latency_ms'][key] / 1000 if variants[name]['costs']['latency_ms'][key] is not None else None)
                        for key in ('mean', 'p50', 'p95'))] for name in [*arms, *(['oracle'] if 'oracle' in variants else [])]]),
        '仅汇总服务端实际返回的 Token；“未上报”不是零费用，失败请求也可能产生费用。模型请求不包含发送前预算拒绝。'
        '这里不含图像嵌入接口的调用量或 Token 账单，也未换算货币成本。延迟是本机及本轮并发、网络条件下的实测，不能作为独立硬件性能结论。',
        _table(['配置', '阶段', '模型请求数', '已上报总 Token', 'Token 已上报请求数'], [
            [name, stage, value['model_calls'], _number(value['tokens']['total_tokens']['sum'], 0),
             value['tokens']['total_tokens']['reported_calls']]
            for name in arms for stage, value in variants[name]['costs']['by_stage'].items()]),
        '### 索引预处理成本',
    ]
    prep_rows = []
    for dataset in ('chartqa', 'pdfvqa'):
        path = prepared / dataset / 'preparation-trace.json'
        if path.exists():
            calls = _json(path).get('model_calls', [])
            count, tokens, covered = _usage(calls)
            prep_rows.append([dataset, count, _number(tokens, 0), f'{covered}/{count}', _link(path, destination)])
        else:
            prep_rows.append([dataset, '预处理调用未观测', '未上报', '未知', '无 preparation-trace.json'])
    blocks += [_table(['数据集', '模型请求', '已上报总 Token', '覆盖', '原始记录'], prep_rows),
        '索引预处理与在线题目运行分开统计。预处理记录可能包含失败索引尝试；不能把已缓存的 OCR 视为本轮在线免费产生，'
        '也不能将同一次共享预处理成本重复累加到每个消融组。',
    ]
    probe_path = prepared / 'tool-probes.jsonl'
    if probe_path.exists():
        probes = _rows(probe_path)
        probe_calls = [call for r in probes for call in r['trace']['model_calls']]
        count, tokens, covered = _usage(probe_calls)
        blocks += ['### 独立工具验收',
            _table(['数据集', '工具', '状态', '新增片段', '错误码'], [[r['dataset'], t['name'], t['status'],
                len(t.get('new_chunk_ids', [])), t.get('error_code') or '无'] for r in probes for t in r['trace']['tool_calls']]),
            f'人为指定页面的探针共 {len(probes)} 页，生成请求 {count} 次，上报 Token {_number(tokens, 0)}（{covered}/{count} 次有 usage）。'
            '这里只验证工具可执行和返回内容，不代表 agent 会自主选对工具，也不证明 OCR 识别准确率。'
            + _link(probe_path, destination)]
    if acceptance_output is not None:
        acceptance_path = Path(acceptance_output) / 'predictions.jsonl'
        acceptance_rows = _rows(acceptance_path)
        count, tokens, covered = _usage([c for r in acceptance_rows for c in r['trace']['model_calls']])
        blocks.append(f'另有 {len(acceptance_rows)} 条接入验收轨迹，生成请求 {count} 次，上报 Token {_number(tokens, 0)}（{covered}/{count}）。'
            '它们来自正式冻结前的验收版本，不并入本报告的效果分母。' + _link(acceptance_path, destination))
    blocks += [
        '## 配对差异',
        '差值方向为“当前配置 − agent_full”。每一行使用相同固定题目，失败和缺失仍记零；正值只表示本次样本得分较高。'
        'Bootstrap 区间是固定 seed 的小样本描述，不能支持稳定性或显著性结论；单次生成随机性与多重比较尚未控制。']
    paired = []
    for name in arms:
        if name == 'agent_full':
            continue
        tasks = summary.get('paired_vs_agent_full', {}).get(name, {}).get('by_task', {})
        for task, metric in (('chartqa', 'relaxed_accuracy'), ('A', 'accuracy'), ('B', 'element_exact_match'), ('B', 'element_f1')):
            value = tasks.get(task, {}).get(metric)
            if value:
                paired.append([name, f'{task}/{metric}', value['case_count'], value['observed_both_count'],
                    f'{value["difference"]:+.4f}', f'{value["improved"]}/{value["worse"]}/{value["tied"]}',
                    '[' + ', '.join(f'{v:+.4f}' for v in value['bootstrap95']) + ']'])
    blocks.append(_table(['配置', '任务 / 指标', '固定 n', '两组均有记录', '分数差', '升 / 降 / 平', 'Bootstrap 95%'], paired))

    main_annotations = [value for value in badcases if value.get('variant') in arms]
    codes = sorted({value.get('primary_code', 'unknown') for value in main_annotations})
    blocks += ['## 错误归因与逐题追踪',
        '以下是固定规则产生的自动归因，**人工复核待完成**；`confirmed` 只说明记录满足该规则，不代表人工确认完整原因。'
        '“generation_reading_or_reasoning”不能进一步区分 OCR、图像阅读和推理；`verifier_rejected_correct` 表示观察到正确结构化草稿被拒绝，也不是完整反事实实验。'
        f'本轮归因文件有 {len(badcases)} 条记录，带人工覆盖记录 {sum(value.get("expert_override") is not None for value in badcases)} 条。',
        '补充的助手日志/原图抽查见 [Badcase 抽查记录](AGENTIC_ABLATION_AUDIT.md)。它不替代人工专家审核，也不覆盖自动判断。'
        '例如基准答案正确但引用越界时，程序拒绝答案不等于核验模型误判；必须查看原始 supported 与程序最终 supported 的差别。',
        _table(['配置', *codes], [[name, *[sum(value.get('variant') == name and value.get('primary_code') == code
            for value in main_annotations) for code in codes]] for name in arms]),
        _table(['配置', '次标签', '题数'], [[name, code, count] for name in arms
            for code, count in sorted(Counter(code for b in main_annotations if b['variant'] == name
                                             for code in b.get('secondary_codes', [])).items())]),
        '### 固定题目',
        _table(['题目 ID', '任务 / 分组', '原问题', '金标准'], [[case['id'], f'{case["task"]}/{case["group"]}', case['question'],
            json.dumps(case['gold_element_ids'] if case['task'] in {'B', 'C'} else case['answers'], ensure_ascii=False)] for case in cases]),
        '### 每题每组结果',
    ]
    detail = []
    for case in cases:
        for name in run['variants']:
            index, row = row_map.get((case['id'], name), (None, {}))
            annotation = annotations.get((case['id'], name), {})
            answer = row.get('element_ids', []) if case['task'] in {'B', 'C'} else row.get('answer', '')
            pointer = annotation.get('evidence', ['/trace/'])
            detail.append([case['id'], name, row.get('status', '缺失'), json.dumps(answer, ensure_ascii=False),
                annotation.get('primary_code', 'missing_result' if index is None else '未归因'),
                _link(output / 'predictions.jsonl', destination, f'L{index}', f'#L{index}') if index else '无预测行',
                '; '.join(pointer)])
    blocks += [_table(['题目 ID', '配置', '状态', '预测', '自动主归因', '原始预测行', 'JSON 证据指针'], detail),
        '行链接指向 predictions.jsonl，路径通常相对于该行对象；带等号的记录是路径加触发条件注记，不是严格的 RFC JSON Pointer。完整 `/trace/model_calls`、`/trace/retrieval_calls`、'
        '`/trace/rerank_calls`、`/trace/tool_calls` 和 `/trace/events` 保留模型输出与阶段记录。'
        '归因中的 `/case/`、`/score/`、`/oracle_row` 为关联对象位置，分别查数据清单、归因记录与同题 oracle 行。',
        '## 证据与复现',
        '\n'.join('- ' + _link(output / filename, destination) for filename in ('run.json', 'predictions.jsonl', 'summary.json', 'badcases.jsonl')),
        '\n'.join('- ' + _link(prepared / dataset / 'manifest.json', destination, dataset + ' manifest')
                  + f'，SHA-256 `{run["manifests"][dataset]}`' for dataset in ('chartqa', 'pdfvqa')),
        '原运行代码 SHA-256：`' + str(run.get('code_sha256', '未记录')) + '`。',
        '本报告核对了数据清单身份、预测 ID 与重新计算的汇总分数；未重新调用模型、选择性重试失败题或覆盖原始预测。'
        '后续复验需保留同一清单、索引、模型、预算及代码身份；报告所列结果只对应这些本地记录。',
    ]
    verification_path = output / 'verification.json'
    if verification_path.exists():
        verification = _json(verification_path)
        suite = verification['final_suite']
        blocks += ['## 软件验证',
            f'最终整套回归：{suite["passed"]} passed、{suite["skipped"]} skipped、{suite["failed"]} failed。'
            '此前一轮空白 PDF 导入断言失败，单独复跑及随后完整复跑通过，根因未确认；本次未修改 PDF 生产代码。'
            + _link(verification_path, destination)]
    postprocessing_path = output / 'postprocessing.json'
    if postprocessing_path.exists():
        postprocessing = _json(postprocessing_path)
        if hashlib.sha256((output / 'predictions.jsonl').read_bytes()).hexdigest() != postprocessing['predictions_sha256']:
            raise ValueError('Predictions changed after recorded offline scoring revision')
        blocks.append('正式轨迹完成后修正了一处离线统计：目录工具 list_documents 的金页命中指标不适用，不将空排名记成失败。'
            '原始预测未改写，也没有新增云端调用；旧统计模块已保留，并验证可重构原运行代码指纹。'
            '因此当前工作区代码指纹与 run.json 不同，不应直接恢复旧实验；重新生成轨迹需使用新目录。'
            + _link(postprocessing_path, destination))
    text = '\n\n'.join(blocks) + '\n'
    atomic_write(destination, text.encode('utf-8'))
    return destination
