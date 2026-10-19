"""Offline dependency audit; reads source only and needs the standard library."""
import argparse
import ast
from importlib.util import resolve_name
import json
from pathlib import Path
import sys


def inspect(root):
    root = Path(root)
    modules = {'.'.join(p.relative_to(root).with_suffix('').parts).removesuffix('.__init__'): p
               for p in root.rglob('*.py')}
    edges = {name: set() for name in modules}
    violations = []
    for name, path in modules.items():
        package = name if path.name == '__init__.py' else name.rsplit('.', 1)[0]
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = (resolve_name('.' * node.level + (node.module or ''), package)
                        if node.level else node.module or '')
                names = [base, *(base + '.' + alias.name for alias in node.names)]
            else:
                continue
            edges[name].update(target for target in names if target in modules and target != name)
            for target in names:
                external = target.split('.')[0]
                rule = None
                if name.startswith(('deepsearch_agent.core.', 'deepsearch_tracing.')):
                    own = name.split('.')[0] + ('.core' if '.core.' in name else '')
                    if external not in sys.stdlib_module_names and not (target == own or target.startswith(own + '.')):
                        rule = 'stdlib_only'
                if name.startswith('deepsearch_agent.') and not name.startswith('deepsearch_agent.benchmarks.'):
                    # Explicit offline CLI shims are the only bridge to experiments.
                    if name not in {'deepsearch_agent.evaluation', 'deepsearch_agent.communication_traces'}:
                        if external in {'deepsearch_experiments', 'search_policy'}:
                            rule = 'production_imports_offline'
                if name.startswith(('deepsearch_agent.agents.', 'deepsearch_agent.knowledge.',
                                    'deepsearch_agent.workspace.', 'deepsearch_agent.application.')):
                    if target.startswith(('deepsearch_tracing.store', 'deepsearch_tracing.recorder',
                                          'deepsearch_tracing.communication', 'deepsearch_tracing.outcomes')):
                        rule = 'business_imports_trace_storage'
                if name.startswith('deepsearch_agent.application.') and '.bootstrap' not in name and '.mcp_cli' not in name:
                    if external in {'fastapi', 'starlette'} or target.startswith('deepsearch_agent.web.'):
                        rule = 'application_imports_http'
                if name == 'deepsearch_agent.agents.tool_service' and external in {'langgraph', 'langchain_core'}:
                    rule = 'tool_service_imports_runtime'
                if rule:
                    violations.append({'module': name, 'target': target, 'line': node.lineno, 'rule': rule})
    visited, stack, cycles = set(), [], []
    def visit(name):
        if name in stack:
            cycles.append(stack[stack.index(name):] + [name])
            return
        if name in visited:
            return
        stack.append(name)
        for target in sorted(edges[name]):
            visit(target)
        stack.pop()
        visited.add(name)
    for name in sorted(modules):
        visit(name)
    return {'modules': len(modules), 'edges': sum(map(len, edges.values())),
            'cycles': cycles, 'violations': violations}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--src', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    report = inspect(args.src)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return bool(report['cycles'] or report['violations'])


if __name__ == '__main__':
    raise SystemExit(main())
