import importlib.util
from pathlib import Path


spec = importlib.util.spec_from_file_location('audit_release', Path(__file__).parents[1] / 'scripts/audit_release.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_audit_never_echoes_secret():
    secret = 'sk-' + 'Ab12' * 10
    findings = module.inspect('config.py', ('token=' + secret).encode())
    assert findings == [{'path': 'config.py', 'line': 1, 'rule': 'provider_token'}]
    assert secret not in str(findings)


def test_audit_blocks_memory_and_env_but_allows_template():
    assert module.inspect('data/conversations.working-memory/PLAN.md', b'')
    assert module.inspect('.env', b'')
    assert not module.inspect('.env.example', b'API_KEY=your-api-key')


def test_audit_fixture_exception_is_exact_and_path_scoped():
    fixture = ('"http://' + 'user:secret@localhost/mcp"').encode()
    path = 'tests/test_mcp_runtime.py'
    assert not module.inspect(path, fixture)
    assert module.inspect('config.py', fixture)
    assert module.inspect(path, fixture.replace(b'localhost', b'example.com'))
    assert module.inspect(path, fixture.replace(b'secret', b'real-password'))
