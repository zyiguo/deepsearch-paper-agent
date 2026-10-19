"""Answer metrics plus paragraph-retrieval diagnostics, never a full leaderboard.

Normalization follows MuSiQue metrics/answer.py and 2Wiki's v1.1 evaluator.
2Wiki's sentence support, evidence triples, and joint score are not implemented.
MuSiQueFull group sufficiency metrics are not implemented.
"""

import re
import string
from collections import Counter
from collections.abc import Mapping

from .schema import BenchmarkManifest, DatasetName, Prediction, manifest_sha256


def normalize_answer(answer: str) -> str:
    answer = ''.join(character for character in answer.lower() if character not in string.punctuation)
    return ' '.join(re.sub(r'\b(a|an|the)\b', ' ', answer).split())


def answer_scores(prediction: str, answers: list[str], dataset: DatasetName) -> dict[str, float]:
    if dataset not in {'musique', '2wikimultihopqa'}:
        raise ValueError('Unsupported answer metric dataset')
    if not answers or any(not answer.strip() for answer in answers):
        raise ValueError('Cannot score an unlabelled answer')
    if not prediction.strip():
        return {'answer_em': 0.0, 'answer_f1': 0.0}
    predicted = normalize_answer(prediction)
    best_em, best_f1 = 0.0, 0.0
    for answer in answers:
        gold = normalize_answer(answer)
        best_em = max(best_em, float(predicted == gold))
        predicted_tokens, gold_tokens = predicted.split(), gold.split()
        if dataset == '2wikimultihopqa' and (
            predicted in {'yes', 'no', 'noanswer'} or gold in {'yes', 'no', 'noanswer'}
        ) and predicted != gold:
            continue
        common = sum((Counter(predicted_tokens) & Counter(gold_tokens)).values())
        if not predicted_tokens or not gold_tokens:
            f1 = float(predicted_tokens == gold_tokens) if dataset == 'musique' else 0.0
        elif common:
            precision, recall = common / len(predicted_tokens), common / len(gold_tokens)
            f1 = 2 * precision * recall / (precision + recall)
        else:
            f1 = 0.0
        best_f1 = max(best_f1, f1)
    return {'answer_em': best_em, 'answer_f1': best_f1}


def evaluate_predictions(
    manifest: BenchmarkManifest,
    predictions: Mapping[str, Prediction | dict],
) -> dict:
    """All labelled questions count, including absent and failed predictions.

    Retrieval recall is document-level and measures only whether a supporting
    paragraph was retrieved, not whether the method used or understood it.
    """
    gold_by_id = {gold.id: gold for gold in manifest.gold}
    if set(predictions) - set(gold_by_id):
        raise ValueError('Predictions contain unknown question IDs')
    document_ids = {doc.id for doc in manifest.documents}
    per_case = []
    for case in manifest.cases:
        missing = case.id not in predictions
        raw = predictions.get(case.id, {})
        prediction = raw if isinstance(raw, Prediction) else Prediction.model_validate(raw)
        if not set(prediction.retrieved_document_ids) <= document_ids:
            raise ValueError(f'Predictions contain unknown document IDs for {case.id}')
        gold = gold_by_id[case.id]
        scores = answer_scores(prediction.answer, gold.answers, manifest.dataset)
        if missing or prediction.status != 'ok':
            scores = {'answer_em': 0.0, 'answer_f1': 0.0}
        support_recall = len(set(prediction.retrieved_document_ids) & set(gold.support_document_ids)) / len(gold.support_document_ids)
        if missing:
            diagnostic = 'missing_prediction'
        elif prediction.status != 'ok':
            diagnostic = prediction.status
        elif scores['answer_em'] == 1:
            diagnostic = 'correct_with_annotated_support' if support_recall == 1 else 'correct_with_support_gap'
        else:
            diagnostic = 'retrieved_support_answer_incorrect' if support_recall == 1 else 'annotated_support_gap'
        per_case.append({
            'id': case.id, 'case_sha256': gold.case_sha256,
            'status': 'missing' if missing else prediction.status,
            **scores, 'document_support_recall_proxy': support_recall,
            'retrieved_documents': len(prediction.retrieved_document_ids),
            'diagnostic_tag': diagnostic,
        })
    count = len(per_case)
    return {
        'schema_version': 1,
        'dataset': manifest.dataset,
        'split': manifest.split,
        'manifest_sha256': manifest_sha256(manifest),
        'score_protocol': 'normalized-answer-em-f1-v1',
        'data_protocol': manifest.protocol,
        'alias_policy': ('released-musique-aliases' if manifest.protocol == 'logicrag-released-v1' and manifest.dataset == 'musique'
                         else 'supplied-question-aliases' if any(source.kind == 'aliases' for source in manifest.sources) else 'processed-spans-only'),
        'not_implemented': ['official_sentence_support', 'evidence_triple_and_joint', 'musique_full_group_sufficiency'],
        'summary': {
            'count': count,
            'missing_predictions': sum(row['status'] == 'missing' for row in per_case),
            'failed_predictions': sum(row['status'] not in {'ok', 'missing'} for row in per_case),
            **{key: sum(row[key] for row in per_case) / count for key in (
                'answer_em', 'answer_f1', 'document_support_recall_proxy',
            )},
        },
        'per_case': per_case,
        'diagnostic_counts': dict(Counter(row['diagnostic_tag'] for row in per_case)),
        'diagnostic_limit': 'Document presence is not proof of use; tags are observations, not causal attribution.',
    }
