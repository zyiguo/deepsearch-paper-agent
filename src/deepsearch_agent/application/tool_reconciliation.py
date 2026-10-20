"""Audited operator reconciliation; never dispatches an uncertain tool again."""
import hashlib
import json

from deepsearch_agent.core.errors import ApplicationError
from deepsearch_agent.core.tool_execution import ToolExecutionBlocked


LOCAL_TOOLS = frozenset({'list_documents', 'search_evidence', 'read_page', 'ocr_page',
                        'get_evidence', 'search_wiki', 'read_wiki', 'working_memory'})


def operation_scope(state, session_id, run_id, operation_id):
    try:
        state.conversations.get(session_id)
    except KeyError:
        raise ApplicationError(404, '研究任务不存在') from None
    if state.research_runs.get(run_id, session_id) is None:
        raise ApplicationError(404, '研究任务不存在')
    ledger = getattr(state, 'tool_ledger', None)
    if ledger is None:
        raise ApplicationError(503, 'tool_ledger_unavailable')
    try:
        row = ledger.get(operation_id)
    except ToolExecutionBlocked:
        raise ApplicationError(404, '工具操作不存在') from None
    except Exception:
        raise ToolExecutionBlocked('tool_ledger_unavailable', operation_id) from None
    if row['run_id'] != run_id:
        raise ApplicationError(404, '工具操作不存在')
    return ledger, row


def audit_operation(state, session_id, run_id, operation_id):
    ledger, _ = operation_scope(state, session_id, run_id, operation_id)
    try:
        return ledger.audit(operation_id)
    except Exception:
        raise ToolExecutionBlocked('tool_ledger_unavailable', operation_id) from None


def reconcile_operation(state, session_id, run_id, operation_id, *, actor, proof_reference,
                        confirmed, external_result=None):
    """Operator attests a provider receipt, or local readback proves a memory write.

    External results remain untrusted text, as on the normal MCP path. Local
    evidence/planner results cannot be fabricated through this endpoint.
    """
    if not confirmed or not actor or not proof_reference.strip():
        raise ApplicationError(422, '请提供已核实的结果和证明引用')
    with state.lock:
        ledger, row = operation_scope(state, session_id, run_id, operation_id)
        run = state.research_runs.get(run_id, session_id)
        if run['status'] != 'interrupted' or row['status'] != 'unknown':
            raise ApplicationError(409, '仅可核对中断任务中结果未知的工具操作')
        if row['tool'] == 'working_memory':
            if not row.get('arguments_json'):
                raise ApplicationError(409, '旧操作缺少可核对的写入意图')
            args = json.loads(row['arguments_json'])
            if args.get('action') != 'replace':
                raise ApplicationError(409, '此操作不能通过写入回读核对')
            try:
                current = state.working_memory.bind(session_id).read(args['document'])
            except (OSError, ValueError):
                raise ApplicationError(503, 'working_memory_unavailable') from None
            expected = hashlib.sha256(args['content'].encode('utf-8')).hexdigest()
            if current['revision'] != expected:
                raise ApplicationError(409, '当前内容不能确认原写入结果；操作仍为未知')
            data = {'document': args['document'], 'updated': True, 'revision': expected,
                    'trust': 'working_notes_not_evidence'}
            result = {'content': json.dumps(data, ensure_ascii=False), 'data': data, 'chunks': []}
        elif row['tool'] in LOCAL_TOOLS or row['tool'].startswith('__'):
            raise ApplicationError(409, '本地证据或规划结果需要可信服务查询适配器，不能人工注入')
        else:
            if not isinstance(external_result, str):
                raise ApplicationError(422, '外部工具需要提供核实后的原始文本结果')
            result = external_result[:state.answer_service.agent_settings.observation_chars]
        try:
            ledger.resolve(operation_id, result, actor=actor, proof_reference=proof_reference.strip())
            unresolved = [r['id'] for r in ledger.list_run(run_id)
                          if r['status'] == 'unknown' and r['kind'] != 'read_only']
        except ToolExecutionBlocked:
            raise
        except Exception:
            raise ToolExecutionBlocked('tool_reconciliation_unavailable', operation_id) from None
        return {'operation_id': operation_id, 'status': 'succeeded', 'unresolved_operations': unresolved,
                'ready_to_resume': not unresolved}
