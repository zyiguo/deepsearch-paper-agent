"""Checkpoint serializer with the minimal historical evidence class aliases."""
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

EVIDENCE_TYPES = [
    ('deepsearch_agent.chunking', 'TextChunk'),
    ('deepsearch_agent.core.evidence', 'TextChunk'),
]


def checkpoint_saver(connection):
    serializer = JsonPlusSerializer(allowed_msgpack_modules=EVIDENCE_TYPES,
                                    allowed_json_modules=EVIDENCE_TYPES)
    return SqliteSaver(connection, serde=serializer)
