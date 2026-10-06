"""Dataset-specific metrics; no model-based answer guessing."""
import math


def normalized_text(value):
    return ' '.join(str(value).strip().lower().split())


def relaxed_correctness(target: str, prediction: str, tolerance=.05) -> bool:
    """Pix2Struct/ChartQA semantics, including exact string matching at zero."""
    def number(value):
        try:
            n = float(value[:-1]) / 100 if value.endswith('%') else float(value)
            return n if math.isfinite(n) else None
        except (ValueError, OverflowError):
            return None
    target, prediction = str(target), str(prediction)
    gold, actual = number(target), number(prediction)
    if actual is not None and gold:
        return abs(actual - gold) / abs(gold) <= tolerance
    return prediction.lower() == target.lower()


def element_scores(gold, predicted):
    gold, predicted = set(gold), set(predicted)
    overlap = len(gold & predicted)
    return {'element_exact_match': float(gold == predicted),
            'element_f1': 2 * overlap / (len(gold) + len(predicted)) if gold or predicted else 1.}


def ranking_scores(ranking, relevant, k):
    """Page-level ranks; multiple representations of one page count once."""
    if k < 1 or not relevant:
        raise ValueError('Ranking metrics require positive k and relevant pages')
    gold = set(relevant)
    hits = list(dict.fromkeys(ranking))[:k]
    matches = [i for i, page in enumerate(hits, 1) if page in gold]
    dcg = sum(1 / math.log2(rank + 1) for rank in matches)
    ideal = sum(1 / math.log2(rank + 1) for rank in range(1, min(k, len(gold)) + 1))
    return {'recall': len(matches) / len(gold), 'mrr': 1 / matches[0] if matches else 0.,
            'ndcg': dcg / ideal}


def answer_scores(case, prediction):
    if case['task'] == 'chartqa':
        actual = prediction.get('answer', '').strip()
        gold = case['answers']
        return {'relaxed_accuracy': float(any(relaxed_correctness(g, actual) for g in gold)),
                'exact_match': float(any(normalized_text(g) == normalized_text(actual) for g in gold))}
    if case['task'] == 'A':
        return {'accuracy': float(any(normalized_text(a) == normalized_text(prediction.get('answer', ''))
                                      for a in case['answers']))}
    return element_scores(case['gold_element_ids'], prediction.get('element_ids', []))
