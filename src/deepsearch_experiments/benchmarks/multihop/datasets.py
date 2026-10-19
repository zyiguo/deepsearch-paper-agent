"""Import author IRCoT question JSONL against a separately supplied corpus.

Sources: StonyBrookNLP/ircot processing_scripts/process_{musique,2wikimultihopqa}.py
and retriever_server/build_index.py. No network, corpus construction, or inference
runs during import. The caller supplies an immutable upstream Git revision.
"""

import hashlib
import json
import random
from pathlib import Path
from typing import Any, Iterator

from pydantic import Field

from .schema import (
    AgentCase, BenchmarkManifest, CorpusScope, DatasetName, Document, GoldCase,
    SourceFile, StrictModel, case_fingerprint, document_id,
)


class _Date(StrictModel):
    day: str
    month: str
    year: str


class _AnswerObject(StrictModel):
    number: str
    date: _Date
    spans: list[str]


class _Context(StrictModel):
    idx: int
    title: str
    paragraph_text: str
    is_supporting: bool


class _ProcessedQuestion(StrictModel):
    question_id: str
    question_text: str
    answers_objects: list[_AnswerObject] = Field(min_length=1)
    contexts: list[_Context] = Field(min_length=1)
    reasoning_steps: list[str] = Field(default_factory=list)


def _jsonl(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    with path.open(encoding='utf-8-sig') as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except (ValueError, TypeError) as exc:
                raise ValueError(f'Invalid JSON in {path.name}:{line_number}') from exc
            if not isinstance(row, dict):
                raise ValueError(f'Expected an object in {path.name}:{line_number}')
            yield line_number, row


def _source(kind: str, path: Path) -> SourceFile:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return SourceFile(kind=kind, filename=path.name, sha256=digest.hexdigest())


def load_corpus(path: str | Path) -> list[Document]:
    """Read corpus JSONL {title, paragraph_text|text}, assigning canonical IDs.

    An optional upstream id is checked for uniqueness but is not our content ID.
    Deduplicate complete author corpora before this step; repeated rows are errors.
    """
    documents = []
    ids: set[str] = set()
    upstream_ids: set[str] = set()
    for line_number, row in _jsonl(Path(path)):
        if 'paragraph_text' in row and 'text' in row and row['paragraph_text'] != row['text']:
            raise ValueError(f'Ambiguous paragraph text at corpus line {line_number}')
        title, text = row.get('title'), row.get('paragraph_text', row.get('text'))
        if not isinstance(title, str) or not isinstance(text, str):
            raise ValueError(f'Corpus line {line_number} needs string title and paragraph_text/text')
        upstream_id = row.get('id')
        if upstream_id is not None:
            if not isinstance(upstream_id, str) or not upstream_id.strip() or upstream_id in upstream_ids:
                raise ValueError(f'Invalid or duplicate upstream corpus ID at line {line_number}')
            upstream_ids.add(upstream_id)
        ident = document_id(title, text)
        if ident in ids:
            raise ValueError(f'Duplicate document content at corpus line {line_number}')
        ids.add(ident)
        documents.append(Document(id=ident, title=title.strip(), text=text.strip()))
    if not documents:
        raise ValueError('Corpus is empty')
    return sorted(documents, key=lambda doc: doc.id)


def _load_aliases(path: Path | None) -> dict[str, list[str]]:
    if path is None:
        return {}
    aliases = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(aliases, dict):
        raise ValueError('Aliases must be an explicit JSON mapping question_id -> list[str]')
    for key, values in aliases.items():
        if not isinstance(key, str) or not isinstance(values, list) or any(
            not isinstance(value, str) or not value.strip() for value in values
        ):
            raise ValueError('Aliases must map question IDs to nonblank string lists')
    return aliases


def import_ircot(
    processed_path: str | Path,
    corpus_path: str | Path,
    *,
    dataset: DatasetName,
    split: str,
    corpus_scope: CorpusScope,
    source_revision: str,
    seed: int = 0,
    limit: int | None = None,
    aliases_path: str | Path | None = None,
) -> BenchmarkManifest:
    """Import labelled author records, failing closed on unsupported schemas.

    Alias mapping is optional and evaluator-only. The processed author format
    drops official raw aliases; without enrichment scores cover supplied spans
    only, not the complete official MuSiQueFull/2Wiki scoring protocol.
    """
    if type(seed) is not int or (limit is not None and (type(limit) is not int or limit < 1)):
        raise ValueError('Seed must be an integer and limit must be positive')
    processed_path, corpus_path = Path(processed_path), Path(corpus_path)
    aliases_file = Path(aliases_path) if aliases_path is not None else None
    aliases = _load_aliases(aliases_file)
    documents = load_corpus(corpus_path)
    document_ids = {doc.id for doc in documents}
    records: list[tuple[AgentCase, GoldCase]] = []
    seen_ids: set[str] = set()
    for line_number, row in _jsonl(processed_path):
        try:
            raw = _ProcessedQuestion.model_validate(row)
        except ValueError as exc:
            raise ValueError(f'Unsupported IRCoT processed question at line {line_number}: {exc}') from exc
        case = AgentCase(id=raw.question_id, question=raw.question_text)
        if case.id in seen_ids:
            raise ValueError(f'Duplicate question ID: {case.id}')
        seen_ids.add(case.id)
        answers = []
        for answer in raw.answers_objects:
            if answer.number or any(answer.date.model_dump().values()) or len(answer.spans) != 1:
                raise ValueError(f'Expected one scalar answer span per answer object for {case.id}')
            if not answer.spans[0].strip():
                raise ValueError(f'Unlabelled question: {case.id}')
            answers.append(answer.spans[0].strip())
        answers = sorted(set(answers + aliases.get(case.id, [])))
        indices = [context.idx for context in raw.contexts]
        if len(set(indices)) != len(indices):
            raise ValueError(f'Duplicate context indices for {case.id}')
        support_ids = sorted({
            document_id(context.title, context.paragraph_text)
            for context in raw.contexts if context.is_supporting
        })
        if not support_ids:
            raise ValueError(f'No gold supporting documents for {case.id}')
        if not set(support_ids) <= document_ids:
            raise ValueError(f'Gold documents missing from corpus for {case.id}; use the matching author corpus')
        gold = GoldCase(
            id=case.id, answers=answers, support_document_ids=support_ids,
            case_sha256=case_fingerprint(dataset, split, case, answers, support_ids),
        )
        records.append((case, gold))
    if not records:
        raise ValueError('No labelled questions found')
    if set(aliases) - seen_ids:
        raise ValueError('Alias mapping contains unknown question IDs')
    records.sort(key=lambda pair: pair[0].id)
    available_cases = len(records)
    if limit is not None and limit < available_cases:
        records = sorted(random.Random(seed).sample(records, limit), key=lambda pair: pair[0].id)
    sources = [_source('questions', processed_path), _source('corpus', corpus_path)]
    if aliases_file is not None:
        sources.append(_source('aliases', aliases_file))
    return BenchmarkManifest(
        dataset=dataset, split=split, source_revision=source_revision, corpus_scope=corpus_scope,
        sources=sources, seed=seed, limit=limit, available_cases=available_cases,
        cases=[pair[0] for pair in records], gold=[pair[1] for pair in records], documents=documents,
    )


def write_manifest(manifest: BenchmarkManifest, path: str | Path) -> None:
    """Write a validated, self-contained evaluator manifest without overwriting."""
    validated = BenchmarkManifest.model_validate(manifest.model_dump())
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open('x', encoding='utf-8', newline='\n') as handle:
        handle.write(validated.model_dump_json(indent=2) + '\n')


def load_manifest(path: str | Path) -> BenchmarkManifest:
    return BenchmarkManifest.model_validate_json(Path(path).read_text(encoding='utf-8'))


def import_logicrag(questions_path, corpus_path, *, dataset, split='author-validation',
                    source_revision, seed=0, limit=None):
    """Import the author's published 1000-question candidate-pool protocol.

    Read supporting labels from original question records; never infer them
    from substring answer matches. Question decompositions never reach agents.
    """
    if dataset not in {'musique', '2wikimultihopqa'}:
        raise ValueError('Unsupported dataset')
    if type(seed) is not int or (limit is not None and (type(limit) is not int or limit < 1)):
        raise ValueError('Invalid sampling configuration')
    questions_path, corpus_path = Path(questions_path), Path(corpus_path)
    rows = json.loads(questions_path.read_text(encoding='utf-8-sig'))
    raw_corpus = json.loads(corpus_path.read_text(encoding='utf-8-sig'))
    if not isinstance(rows, list) or not rows or not isinstance(raw_corpus, list) or not raw_corpus:
        raise ValueError('Author questions/corpus must be nonempty arrays')
    documents = []
    for doc in raw_corpus:
        if not isinstance(doc, dict) or not isinstance(doc.get('title'), str) or not isinstance(doc.get('text'), str):
            raise ValueError('Author corpus requires title and text strings')
        documents.append(Document(id=document_id(doc['title'], doc['text']),
                                  title=doc['title'].strip(), text=doc['text'].strip()))
    if len({doc.id for doc in documents}) != len(documents):
        raise ValueError('Duplicate author corpus documents')
    documents.sort(key=lambda doc: doc.id)
    document_ids = {doc.id for doc in documents}
    records, seen, warnings = [], set(), []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Author question must be an object')
        case = AgentCase(id=row.get('id' if dataset == 'musique' else '_id'), question=row.get('question'))
        if case.id in seen:
            raise ValueError('Duplicate question ID')
        seen.add(case.id)
        answer = row.get('answer')
        aliases = row.get('answer_aliases', []) if dataset == 'musique' else []
        if not isinstance(answer, str) or not answer.strip() or not isinstance(aliases, list) or any(
            not isinstance(alias, str) or not alias.strip() for alias in aliases
        ):
            raise ValueError('Unlabelled question or invalid aliases')
        answers = sorted(set([answer.strip(), *aliases]))
        support = []
        if dataset == 'musique':
            if row.get('answerable') is not True:
                raise ValueError('This protocol supports MuSiQue-Ans only, not Full')
            paragraphs = row.get('paragraphs')
            if not isinstance(paragraphs, list) or not paragraphs:
                raise ValueError('Missing MuSiQue paragraphs')
            for paragraph in paragraphs:
                context = _Context.model_validate(paragraph)
                if context.is_supporting:
                    support.append(document_id(context.title, context.paragraph_text))
        else:
            context = row.get('context')
            if not isinstance(context, list) or not context:
                raise ValueError('Missing 2Wiki context')
            by_title = {}
            for entry in context:
                if (not isinstance(entry, list) or len(entry) != 2 or not isinstance(entry[0], str)
                        or not isinstance(entry[1], list) or any(not isinstance(s, str) for s in entry[1])):
                    raise ValueError('Invalid 2Wiki context')
                title, sentences = entry
                if title in by_title and by_title[title] != sentences:
                    raise ValueError('Ambiguous 2Wiki title')
                by_title[title] = sentences
            facts = row.get('supporting_facts')
            if not isinstance(facts, list):
                raise ValueError('Missing 2Wiki support labels')
            for fact in facts:
                if not isinstance(fact, list) or len(fact) != 2:
                    raise ValueError('Invalid supporting fact')
                title, sentence = fact
                if not isinstance(title, str) or title not in by_title or type(sentence) is not int or sentence < 0:
                    raise ValueError('Supporting sentence has an invalid title/index')
                if sentence >= len(by_title[title]):
                    warnings.append(f'{case.id}: support_sentence_out_of_range; document-level label retained')
                support.append(document_id(title, ' '.join(by_title[title])))
        support = sorted(set(support))
        if not support or not set(support) <= document_ids:
            raise ValueError(f'Gold documents missing from author corpus for {case.id}')
        gold = GoldCase(id=case.id, answers=answers, support_document_ids=support,
                        case_sha256=case_fingerprint(dataset, split, case, answers, support))
        records.append((case, gold))
    records.sort(key=lambda pair: pair[0].id)
    available = len(records)
    if limit is not None and limit < available:
        records = sorted(random.Random(seed).sample(records, limit), key=lambda pair: pair[0].id)
    return BenchmarkManifest(
        dataset=dataset, split=split, protocol='logicrag-released-v1', source_revision=source_revision,
        corpus_scope='sampled_pool', sources=[_source('questions', questions_path), _source('corpus', corpus_path)],
        seed=seed, limit=limit, available_cases=available, cases=[r[0] for r in records],
        gold=[r[1] for r in records], documents=documents, annotation_warnings=warnings,
    )
