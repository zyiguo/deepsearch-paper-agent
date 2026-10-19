from deepsearch_experiments.audits import audit_release as module


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


def test_audit_proxy_rejection_fixture_exception_is_exact():
    fixture = ("'http://" + "user:secret@localhost:7897'").encode()
    path = 'tests/test_multihop_upstream.py'
    assert not module.inspect(path, fixture)
    assert module.inspect('config.py', fixture)
    assert module.inspect(path, fixture.replace(b'localhost', b'example.com'))
    assert module.inspect(path, fixture.replace(b'secret', b'real-password'))
