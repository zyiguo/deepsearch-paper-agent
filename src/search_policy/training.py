"""Reader boundary for a trainer, without importing Torch or tokenizer libraries."""
from pathlib import Path

from .dataset import read_jsonl, verify_dataset


def load_sft_steps(directory, split='train'):
    if split not in {'train', 'val'}:
        raise ValueError('Expected train or val split')
    verify_dataset(directory)
    return read_jsonl(Path(directory) / (split + '.jsonl'))


def encode_sft_steps(directory, split, tokenizer, max_length, encode_step):
    """Inject the training repo's encode_step; preserve its completion-only mask.

    No replay, re-retrieval or silent truncation takes place here. The trainer's
    encode_step must reject an unsupported chat template or overlong example.
    """
    for row in load_sft_steps(directory, split):
        encoded = encode_step(row['messages'], row['text'], tokenizer, max_length)
        yield {**encoded, 'sample_id': row['id'], 'question_id': row['group_id']}
