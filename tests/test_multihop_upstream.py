import pytest

from deepsearch_experiments.benchmarks.multihop.upstream import fetch_logicrag_dataset, fetch_reference_assets


def test_download_requires_opt_in(tmp_path):
    with pytest.raises(ValueError, match='allow-network'):
        fetch_reference_assets(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_pinned_bounded_download_and_hashes(tmp_path, monkeypatch):
    calls = []
    def fake_fetch(client, url, limit):
        calls.append((url, limit))
        return b'[]'
    monkeypatch.setattr('deepsearch_experiments.benchmarks.multihop.upstream.fetch', fake_fetch)
    result = fetch_logicrag_dataset(tmp_path, 'musique', allow_network=True)
    assert len(result['assets']) == 2
    assert result['executed_upstream_code'] is False
    assert all('/72a298c95ffd75890c7f9c28fd1c094201f669a7/' in url for url, _ in calls)
    assert all(limit <= 20 * 1024 * 1024 for _, limit in calls)
    assert all(len(row['sha256']) == 64 for row in result['assets'])
    target = tmp_path / result['assets'][0]['local_path']
    target.write_bytes(b'tampered')
    with pytest.raises(ValueError, match='differs'):
        fetch_logicrag_dataset(tmp_path, 'musique', allow_network=True)
    assert target.read_bytes() == b'tampered'


@pytest.mark.parametrize('proxy', ['http://user:secret@localhost:7897', 'file:///tmp', 'https://x/?token=secret'])
def test_reject_proxy_secrets(tmp_path, proxy):
    with pytest.raises(ValueError, match='Proxy'):
        fetch_reference_assets(tmp_path, allow_network=True, proxy=proxy)


def test_reject_dataset_path_traversal(tmp_path):
    with pytest.raises(ValueError, match='Unsupported'):
        fetch_logicrag_dataset(tmp_path, '../secret', allow_network=True)
