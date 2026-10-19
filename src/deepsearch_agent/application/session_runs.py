"""Conversation use cases, ordered turns, cancellation and resumable background runs."""
import logging
from contextlib import contextmanager
from queue import Queue, Empty
from threading import Thread, Lock, Event, current_thread
from deepsearch_agent.core.errors import ApplicationError
from deepsearch_agent.core.cancellation import CancellationToken, ResearchCancelled, current_token
from deepsearch_agent.adapters.demo import DEMO_QUESTIONS
from deepsearch_agent.application.scope import resolve_sources
from deepsearch_agent.application.skills import validate_skills
from deepsearch_agent.workspace.memory import MemoryManager
from deepsearch_agent.application.snapshots import snapshot_answer_service
from deepsearch_agent.application.commands import TurnCommand
from deepsearch_agent.application.run_reliability import submission_id, context_identity, error_code
from deepsearch_agent.core.identity import fingerprint
from deepsearch_agent.core.tool_execution import ToolExecutionBlocked


class RunWorkers:
    """Own thread lifetimes and cooperative cancellation for a single API process."""
    def __init__(self, max_active=4):
        if not 1 <= max_active <= 32:
            raise ValueError('Invalid worker capacity')
        self.lock = Lock()
        self.active = {}
        self.closed = False
        self.max_active = max_active

    def start(self, work, token):
        finished = Event()
        def execute():
            try:
                work()
            finally:
                with self.lock:
                    self.active.pop(current_thread(), None)
                finished.set()
        with self.lock:
            if self.closed:
                raise RuntimeError('Run workers are closed')
            if len(self.active) >= self.max_active:
                raise ApplicationError(503, '研究任务容量已满或服务正在关闭，请稍后显式重试')
            thread = Thread(target=execute, daemon=True, name='research-run')
            self.active[thread] = (token, finished)
            try:
                thread.start()
            except BaseException:
                self.active.pop(thread, None)
                raise

    @contextmanager
    def track(self, token):
        """Synchronous API work shares the same capacity and shutdown cancellation."""
        thread = current_thread()
        with self.lock:
            owned = thread not in self.active
            if owned:
                if self.closed or len(self.active) >= self.max_active:
                    raise ApplicationError(503, '研究任务容量已满或服务正在关闭，请稍后显式重试')
                finished = Event()
                self.active[thread] = (token, finished)
        try:
            yield
        finally:
            if owned:
                with self.lock:
                    self.active.pop(thread, None)
                finished.set()

    def close(self):
        with self.lock:
            self.closed = True
            active = list(self.active.values())
            for token, _ in active:
                token.cancel()
        for _, finished in active:
            finished.wait()


def require_session(store, session_id):
    try:
        return store.get(session_id)
    except KeyError as error:
        raise ApplicationError(status_code=404, detail='会话不存在') from error

def create_session(body, state):
    with state.lock:
        return state.conversations.create(body.title)

def list_sessions(state):
    with state.lock:
        return state.conversations.list_sessions()

def get_session(session_id, state):
    with state.lock:
        session = require_session(state.conversations, session_id)
        return {**session, 'memory': MemoryManager(state.conversations).get(session_id)}

def set_memory_scope(session_id, body, state):
    with state.lock:
        require_session(state.conversations, session_id)
        if body.document_ids:
            resolve_sources(state, body.document_ids)
        return MemoryManager(state.conversations).set_scope(session_id, body.document_ids)

def get_memory(session_id, state):
    with state.lock:
        require_session(state.conversations, session_id)
        return MemoryManager(state.conversations).get(session_id)

def edit_memory(session_id, body, state):
    with state.lock:
        require_session(state.conversations, session_id)
        return MemoryManager(state.conversations).edit(session_id, **body)

def clear_memory(session_id, state):
    with state.lock:
        require_session(state.conversations, session_id)
        clear_working_memory_files(state, session_id)
        return MemoryManager(state.conversations).clear(session_id)

def clear_working_memory_files(state, session_id):
    if getattr(state, 'working_memory', None) is None:
        return
    try:
        state.working_memory.clear(session_id)
    except (OSError, ValueError):
        raise ApplicationError(status_code=503, detail='working_memory_unavailable') from None

def get_working_memory(session_id, state):
    with state.lock:
        require_session(state.conversations, session_id)
        try:
            memory = state.working_memory.bind(session_id)
            return {name: memory.read(name) for name in ('plan', 'history')}
        except (OSError, ValueError):
            raise ApplicationError(status_code=503, detail='working_memory_unavailable') from None

def clear_working_memory(session_id, state):
    with state.lock:
        require_session(state.conversations, session_id)
        clear_working_memory_files(state, session_id)
    return {'cleared': True}

def delete_session(session_id, state):
    with state.lock:
        try:
            require_session(state.conversations, session_id)
            runs = state.research_runs.list(session_id) if hasattr(state, 'research_runs') else []
            if any((run['status'] in {'queued', 'running', 'cancel_requested'} for run in runs)):
                raise ApplicationError(409, '请先取消正在运行的研究任务')
            clear_working_memory_files(state, session_id)
            state.conversations.delete(session_id)
            if runs:
                state.research_runs.delete_session(session_id)
                if getattr(state, 'tool_ledger', None) is not None:
                    for run in runs:
                        state.tool_ledger.delete_run(run['id'])
                if hasattr(state, 'research_checkpointer'):
                    for run in runs:
                        state.research_checkpointer.delete_thread(run['id'])
            if getattr(state, 'attachments', None) is not None:
                try:
                    state.attachments.delete_session(session_id)
                except (OSError, ValueError):
                    logging.getLogger(__name__).warning('attachment_cleanup_failed')
        except KeyError:
            raise ApplicationError(status_code=404, detail='会话不存在') from None
    return {'message': '会话及全部问答记录已删除'}

def send_turn(session_id, body, state, idempotency_key=None):
    run_id, existing = prepare_run(session_id, body, state, idempotency_key)
    if existing is not None:
        return existing
    return execute_run(session_id, body, state, run_id)

def turn_inputs(state, session_id, body, document_ids):
    validate_skills(state, body.skill_ids)
    attachments, evidence = ([], [])
    if body.attachment_ids:
        if not getattr(state, 'vision_enabled', False):
            raise ApplicationError(409, '请先配置视觉模型后带图提问')
        try:
            attachments, evidence = state.attachments.evidence(session_id, body.attachment_ids)
        except KeyError:
            raise ApplicationError(404, '图片附件不存在或不属于当前会话') from None
        settings = getattr(getattr(state.answer_service, 'visual_evidence', None), 'settings', None)
        if settings is not None and len(evidence) > settings.max_images:
            raise ApplicationError(422, '附件数量超过当前视觉模型的图片上限')
    sources = set() if evidence and document_ids == [] else resolve_sources(state, document_ids)
    return (sources, attachments, evidence)

def generate_turn(session_id, body, state, on_progress=None, run_id=None, resume=False):
    with state.lock:
        if not hasattr(state, 'turn_locks'):
            from weakref import WeakValueDictionary
            state.turn_locks = WeakValueDictionary()
        turn_lock = state.turn_locks.setdefault(session_id, Lock())
    with turn_lock:
        return _generate_turn(session_id, body, state, on_progress, run_id=run_id, resume=resume)

def _generate_turn(session_id, body, state, on_progress=None, *, run_id=None, resume=False):
    with state.lock:
        session = require_session(state.conversations, session_id)
        manager = MemoryManager(state.conversations)
        if run_id is not None:
            request = state.research_runs.get(run_id, session_id)['request']
            if request.get('_runtime_identity') != context_identity(state, manager, session_id):
                raise ApplicationError(409, '模型、预算、技能或会话上下文已变化，请新建研究任务')
        saved_memory = manager.get(session_id)
        ids = body.document_ids if body.document_ids is not None else saved_memory.document_ids
        sources, attachments, input_evidence = turn_inputs(state, session_id, body, ids)
        if run_id is not None and (sorted(sources or []) != request.get('_source_ids')
                or scoped_chunk_ids(state.kb, sources) != request.get('_chunk_ids')):
            raise ApplicationError(409, '排队期间文献范围或索引已变化，请重新确认后创建任务')
        if state.demo_retrieval and (not input_evidence) and (body.question not in DEMO_QUESTIONS):
            raise ApplicationError(status_code=400, detail='演示模式仅支持：' + '、'.join(DEMO_QUESTIONS))
        manager.set_scope(session_id, sorted({s.split('/')[2] for s in sources}) if sources is not None else None)
        memory_context, history = manager.context(session_id)
        progress = {'on_progress': on_progress} if on_progress is not None else {}
        if getattr(state, 'agentic_rag_enabled', False):
            progress['working_memory'] = state.working_memory.bind(session_id)
        if getattr(state.answer_service, 'run_recorder', None) is not None:
            request = state.research_runs.get(run_id, session_id)['request'] if run_id else {}
            progress['trace_context'] = {'conversation_id': session_id,
                'runtime_identity': request.get('_runtime_identity'),
                'corpus_identity': fingerprint(request.get('_chunk_ids', []))}
        if sources is not None:
            progress['allowed_sources'] = sources
        if input_evidence:
            progress['input_evidence'] = input_evidence
        if body.skill_ids is not None:
            progress['skill_ids'] = body.skill_ids
        if run_id is not None:
            progress['run_id'] = run_id
            progress['resume'] = resume
        service = snapshot_answer_service(state)
    answer = service.answer(question=body.question, top_k=body.top_k, history=history, memory_context=memory_context, **progress)
    token = current_token.get()
    if token is not None:
        token.check()
    with state.lock:
        turn = state.conversations.append_turn(session_id=session_id, question=body.question, answer=answer, **{'attachments': attachments} if attachments else {}, run_id=run_id)
    try:
        client = getattr(state.answer_service, 'client', None)
        if client is not None and (not state.demo_retrieval):
            manager.compress(session_id, client, on_progress)
    except Exception:
        logging.getLogger(__name__).warning('memory_update_failed')
    return turn

def prepare_run(session_id, body, state, idempotency_key=None):
    with state.lock:
        require_session(state.conversations, session_id)
        ensure_run_state(state)
        ident = submission_id(session_id, idempotency_key)
        body_identity = fingerprint(body.to_dict())
        previous = state.research_runs.get(ident, session_id) if ident else None
        if previous is not None:
            if previous['request'].get('_submission_identity') != body_identity:
                raise ApplicationError(409, '同一幂等键不能用于不同请求')
            turn = reconcile_saved_turn(session_id, ident, state)
            if turn is not None:
                return ident, turn
            raise ApplicationError(409, '该请求已被接纳，请查看任务状态或显式恢复，不要重复发送')
        manager = MemoryManager(state.conversations)
        saved = manager.get(session_id)
        sources, _, _ = turn_inputs(state, session_id, body, body.document_ids if body.document_ids is not None else saved.document_ids)
        manager.set_scope(session_id, sorted({s.split('/')[2] for s in sources}) if sources is not None else None)
        record = {**body.to_dict(), '_source_ids': sorted(sources or []),
                  '_chunk_ids': scoped_chunk_ids(state.kb, sources),
                  '_runtime_identity': context_identity(state, manager, session_id),
                  '_submission_identity': body_identity, '_execution': {'attempts': 0}}
        run_id = state.research_runs.create(session_id, record, run_id=ident)
        if getattr(state, 'tool_ledger', None) is not None:
            state.tool_ledger.begin_run(run_id)
    return run_id, None


def stream_turn(session_id, body, state, idempotency_key=None):
    run_id, existing = prepare_run(session_id, body, state, idempotency_key)
    if existing is not None:
        return iter([('status', {'stage': 'queued', 'run_id': run_id, 'replayed': True}), ('done', existing)])
    return run_stream(session_id, body, state, run_id)

def ensure_run_state(state):
    if not hasattr(state, 'run_workers'):
        state.run_workers = RunWorkers()
    if hasattr(state, 'research_runs'):
        return
    from deepsearch_agent.adapters.persistence.research_runs import ResearchRunStore
    state.research_runs = ResearchRunStore(state.conversations.path.with_name('research_runs.sqlite3'))
    state.run_tokens = {}

def scoped_chunk_ids(kb, sources):
    images = getattr(kb, 'image_index', None)
    chunks = [*kb.chunks, *(images.chunks if images else [])]
    return sorted((c.chunk_id for c in chunks if sources is None or c.source_path in sources))

def list_runs(session_id, state):
    require_session(state.conversations, session_id)
    return [{key: value for key, value in run.items() if key != 'request_json'} for run in state.research_runs.list(session_id)]


def list_tool_operations(session_id, run_id, state):
    require_session(state.conversations, session_id)
    if state.research_runs.get(run_id, session_id) is None:
        raise ApplicationError(404, '研究任务不存在')
    ledger = getattr(state, 'tool_ledger', None)
    return ledger.list_run(run_id) if ledger is not None else []

def cancel_run(session_id, run_id, state):
    with state.lock:
        run = state.research_runs.get(run_id, session_id)
        if run is None:
            raise ApplicationError(404, '研究任务不存在')
        if run['status'] in {'completed', 'cancelled', 'failed'}:
            return {'status': run['status']}
        if reconcile_saved_turn(session_id, run_id, state) is not None:
            return {'status': 'completed'}
        state.research_runs.update(run_id, 'cancel_requested', expected={'queued', 'running', 'interrupted'})
        token = state.run_tokens.get(run_id)
        if token is not None:
            token.cancel()
        else:
            state.research_runs.update(run_id, 'cancelled', expected={'cancel_requested'})
    return {'status': 'cancel_requested'}

def resume_run(session_id, run_id, state):
    with state.lock:
        run = state.research_runs.get(run_id, session_id)
        if run is None:
            raise ApplicationError(404, '研究任务不存在')
        if run['status'] != 'interrupted':
            raise ApplicationError(409, '只有中断的任务可以恢复')
        saved_turn = reconcile_saved_turn(session_id, run_id, state)
        if saved_turn is not None:
            return iter([('done', saved_turn)])
        body = TurnCommand.from_record(run['request'])
        saved = MemoryManager(state.conversations).get(session_id)
        sources, _, _ = turn_inputs(state, session_id, body, body.document_ids if body.document_ids is not None else saved.document_ids)
        current_chunks = scoped_chunk_ids(state.kb, sources)
        if sorted(sources or []) != run['request'].get('_source_ids') or current_chunks != run['request'].get('_chunk_ids'):
            raise ApplicationError(409, '文献范围或索引已变化，不能恢复旧任务')
        if run['request'].get('_runtime_identity') != context_identity(state, MemoryManager(state.conversations), session_id):
            raise ApplicationError(409, '旧任务身份缺失，或模型、预算、技能、上下文已变化，不能自动恢复')
        checkpoint = state.research_checkpointer.get_tuple({'configurable': {'thread_id': run_id}})
        if checkpoint is None:
            raise ApplicationError(409, '任务没有可恢复的检查点；请明确发起新任务，系统不会自动重新调用模型')
        ledger = getattr(state, 'tool_ledger', None)
        if ledger is not None and not ledger.has_run(run_id):
            raise ApplicationError(409, '旧任务没有工具执行账本，无法确认工具结果；请核对后明确发起新任务')
        if not state.research_runs.claim_resume(run_id, session_id):
            raise ApplicationError(409, '只有中断的任务可以恢复')
        if ledger is not None:
            try:
                ledger.recover_interrupted(run_id)
            except Exception:
                state.research_runs.update(run_id, 'interrupted', expected={'queued'},
                    execution={'error_code': 'tool_ledger_unavailable'})
                raise ApplicationError(503, '工具执行账本暂不可用，未重新调用工具') from None
    return run_stream(session_id, body, state, run_id, resume=True)


def reconcile_saved_turn(session_id, run_id, state):
    turn = state.conversations.find_turn(run_id, session_id)
    if turn is not None:
        state.research_runs.update(run_id, 'completed', turn_id=turn['id'],
                                   execution={'error_code': None, 'result_reconciled': True})
    return turn


def execute_run(session_id, body, state, run_id, on_progress=None, resume=False, token=None):
    """Claim once; never repeat a paid call to repair a result-commit ambiguity."""
    token = token or CancellationToken()
    ensure_run_state(state)
    try:
        with state.run_workers.track(token):
            return execute_claimed_run(session_id, body, state, run_id, on_progress, resume, token)
    except ApplicationError as error:
        if error.status_code == 503:
            try:
                state.research_runs.update(run_id, 'interrupted', expected={'queued'},
                    execution={'error_code': 'worker_unavailable'})
            except Exception:
                logging.getLogger(__name__).warning('run_terminal_persistence_failed')
        raise


def execute_claimed_run(session_id, body, state, run_id, on_progress, resume, token):
    with state.lock:
        if not state.research_runs.update(run_id, 'running', expected={'queued'},
                execution={'new_attempt': True, 'error_code': None}):
            raise ApplicationError(409, '任务已被执行或取消，请刷新状态')
        state.run_tokens[run_id] = token
    reset = current_token.set(token)
    try:
        token.check()
        turn = reconcile_saved_turn(session_id, run_id, state)
        if turn is None:
            turn = generate_turn(session_id, body, state, on_progress=on_progress, run_id=run_id, resume=resume)
        state.research_runs.update(run_id, 'completed', turn_id=turn['id'],
                                   execution={'error_code': None})
        return turn
    except Exception as error:
        try:
            turn = reconcile_saved_turn(session_id, run_id, state)
            if turn is not None:
                return turn
            state.research_runs.update(run_id, 'cancelled' if isinstance(error, ResearchCancelled) else 'interrupted',
                expected={'running', 'cancel_requested'}, execution={'error_code': 'cancelled' if isinstance(error, ResearchCancelled) else error_code(error)})
        except Exception:
            logging.getLogger(__name__).warning('run_terminal_persistence_failed')
        raise
    finally:
        current_token.reset(reset)
        with state.lock:
            state.run_tokens.pop(run_id, None)

def run_stream(session_id, body, state, run_id, resume=False):
    ensure_run_state(state)
    events = Queue()
    token = CancellationToken()

    def emit(stage):
        token = current_token.get()
        if token is not None:
            token.check()
        if not state.research_runs.update(run_id, 'running', stage=stage, expected={'running'}):
            if token is not None:
                token.cancel()
                token.check()
            return
        events.put(('status', {'stage': stage}))

    def work():
        try:
            turn = execute_run(session_id, body, state, run_id, on_progress=emit, resume=resume, token=token)
            for index, paragraph in enumerate(turn['answer']['text'].split('\n\n')):
                events.put(('answer', {'text': ('\n\n' if index else '') + paragraph, 'sources': turn['answer']['sources']}))
            events.put(('done', turn))
        except ResearchCancelled:
            events.put(('cancelled', {'message': '研究已取消，未保存本轮答案。'}))
        except ToolExecutionBlocked as error:
            events.put(('error', {'message': '工具执行结果未确认或恢复被阻止，请查看任务执行账本，勿自动重新提交。',
                                 'code': error.code, 'operation_id': error.operation_id}))
        except Exception:
            events.put(('error', {'message': '回答未完成，请刷新会话核对结果；不要自动重复发送。'}))

    def stream():
        yield ('status', {'stage': 'queued', 'run_id': run_id, 'resumed': resume})
        while True:
            try:
                event, data = events.get(timeout=10)
            except Empty:
                yield (None, None)
                continue
            yield (event, data)
            if event in {'done', 'error', 'cancelled'}:
                break
    # Start before producing the response body: a disconnect must not strand a queued run.
    try:
        state.run_workers.start(work, token)
    except Exception:
        state.research_runs.update(run_id, 'interrupted', expected={'queued'},
                                   execution={'error_code': 'worker_unavailable'})
        raise ApplicationError(503, '任务工作线程不可用，请稍后显式恢复') from None
    return stream()


