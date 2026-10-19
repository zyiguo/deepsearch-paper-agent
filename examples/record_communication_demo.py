"""Synthetic, offline handoff recording through the real application graph."""
import argparse
import json
from pathlib import Path

from deepsearch_agent.agents.agent_config import AgentSettings
from deepsearch_agent.core.evidence import TextChunk
from deepsearch_tracing.communication import handoffs, export_split
from deepsearch_agent.core.models import ModelServiceError
from deepsearch_agent.application.answer_service import AnswerService
from deepsearch_agent.adapters.tracing import RAGTraceRecorder
from deepsearch_tracing.store import TraceStore


class FixtureLibrary:
    image_index = None

    def __init__(self):
        self.chunks = [TextChunk('The reported value is 12.', 'fixture.pdf', 1, 0)]

    def search(self, query, top_k=5):
        return self.chunks[:top_k]


class FixtureModel:
    def __init__(self, library, mode):
        self.library, self.mode, self.calls = library, mode, 0

    def complete(self, prompt):
        data = json.loads(prompt.split('\ndata: ', 1)[1])
        if prompt.startswith('你是 Supervisor。'):
            action = 'research' if not data['research_rounds'] else 'draft' if not data['verified'] else 'finish'
            result = {'action': action, 'tasks': [], 'instruction': 'Find the reported value.'}
        elif prompt.startswith('你是 Researcher。'):
            if self.mode == 'model_error':
                raise ModelServiceError('Synthetic unavailable model', error_code='connection_error')
            self.calls += 1
            result = ({'tool': 'search_evidence', 'arguments': {'query': 'reported value'}}
                      if self.calls == 1 else {'tool': '', 'arguments': {}, 'findings': [
                          {'text': 'The reported value is 12.', 'evidence_ids': [self.library.chunks[0].chunk_id]}]})
        elif prompt.startswith('你是 Answerer。'):
            result = {'claims': [{'text': 'The reported value is 12.', 'citations': [1]}]}
        else:
            result = {'supported': [] if self.mode == 'rejected' else [0], 'queries': [],
                      'issues': ['Synthetic rejected claim'] if self.mode == 'rejected' else []}
        return json.dumps(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    database = args.output / 'traces.sqlite3'
    for mode in ('success', 'rejected', 'model_error'):
        library = FixtureLibrary()
        service = AnswerService(FixtureModel(library, mode), library, use_graph=True,
            agent_settings=AgentSettings(enabled=True),
            run_recorder=RAGTraceRecorder(database, metadata={'fixture': True, 'fixture_mode': mode}))
        try:
            service.answer('What value is reported?', trace_context={'conversation_id': f'fixture-{mode}'})
        except ModelServiceError:
            pass
    TraceStore(database).start({'fixture': True, 'fixture_mode': 'interrupted',
                                'question': 'Synthetic unfinished run'})
    runs = TraceStore(database, read_only=True).read_runs()
    manifest = export_split(handoffs(runs), args.output / 'split', runs)
    print(json.dumps({'fixture': True, 'database': str(database.resolve()),
                      'manifest': manifest}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
