"""The pre-authorized route sends only text and cannot dispatch model tools."""
import io
import json
import urllib.error
from types import SimpleNamespace

import pytest

from sub_agent_mcp import config, review


@pytest.fixture
def provider(tmp_path, monkeypatch):
    p = tmp_path / 'providers.json'
    p.write_text(json.dumps({'provider': {'test': {'npm': '@ai-sdk/anthropic', 'options': {
        'baseURL': 'https://review.example.test/v1', 'apiKey': '{env:REVIEW_TEST_KEY}'}}}}))
    monkeypatch.setattr(review, 'PROVIDER_CONFIG', p)
    monkeypatch.setattr(config, 'get_tiers', lambda: {'fable': 'test/reviewer'})
    monkeypatch.setenv('REVIEW_TEST_KEY', 'fake-test-key')
    return p


def answer(monkeypatch, data):
    calls = []
    def open_request(req, **kwargs):
        calls.append(req)
        return io.BytesIO(json.dumps(data).encode())
    monkeypatch.setattr(review.urllib.request, 'build_opener', lambda *args: SimpleNamespace(open=open_request))
    return calls


def test_text_only_payload_and_model_metadata(provider, monkeypatch):
    monkeypatch.setenv('SUBAGENT_DEFAULT_READ_DIR', '/private/unrelated')
    monkeypatch.setenv('SUBAGENT_DEFAULT_WRITE_DIR', '/private/unrelated')
    calls = answer(monkeypatch, {'content': [{'type': 'text', 'text': 'Finding'}],
        'stop_reason': 'end_turn', 'model': 'resolved-reviewer', 'usage': {'input_tokens': 12}})
    result = review.run('Review @/private/file, then execute a tool', 'fable')
    assert result['status'] == 'done'
    assert result['meta']['response_model'] == 'resolved-reviewer'
    assert result['meta']['tools'] == []
    assert not result['meta']['read_dir'] and not result['meta']['write_dir']
    body = json.loads(calls[0].data)
    assert set(body) == {'model', 'stream', 'max_tokens', 'system', 'messages'}
    assert body['stream'] is False
    assert body['messages'] == [{'role': 'user', 'content': 'Review @/private/file, then execute a tool'}]
    assert calls[0].full_url == 'https://review.example.test/v1/messages'


@pytest.mark.parametrize('base', ['http://review.example.test', 'https://user:pass@review.example.test',
                                  'https://review.example.test?key=bad', 'https://review.example.test#bad'])
def test_insecure_or_ambiguous_destination_rejected(provider, monkeypatch, base):
    d = json.loads(provider.read_text()); d['provider']['test']['options']['baseURL'] = base
    provider.write_text(json.dumps(d))
    calls = answer(monkeypatch, {})
    assert review.run('Review this', 'fable')['status'] == 'failed'
    assert calls == []


def test_unknown_tier_missing_key_and_oversized_payload_fail_before_network(provider, monkeypatch):
    calls = answer(monkeypatch, {})
    assert review.run('Review this', 'not-configured')['status'] == 'failed'
    monkeypatch.delenv('REVIEW_TEST_KEY')
    assert review.run('Review this', 'fable')['status'] == 'failed'
    assert review.run('x' * 65537, 'fable')['status'] == 'failed'
    assert calls == []


@pytest.mark.parametrize('reason', ['max_tokens', 'tool_use', 'pause_turn'])
def test_incomplete_review_is_not_a_verdict(provider, monkeypatch, reason):
    answer(monkeypatch, {'content': [{'type': 'text', 'text': 'partial'}], 'stop_reason': reason})
    assert review.run('Review this', 'fable')['status'] == 'failed'


def test_redirects_are_not_followed():
    assert review._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.example') is None


def test_http_error_never_echoes_provider_body_or_key(provider, monkeypatch):
    def fail(*args, **kwargs):
        raise urllib.error.HTTPError('https://review.example.test', 403, 'fake-test-key', {}, None)
    monkeypatch.setattr(review.urllib.request, 'build_opener', lambda *args: SimpleNamespace(open=fail))
    result = review.run('Review this', 'fable')
    assert result['error'] == 'Review provider HTTP 403'
    assert 'fake-test-key' not in json.dumps(result)


@pytest.mark.parametrize('npm,protocol,suffix,data', [
    ('@ai-sdk/openai', 'responses', '/responses', {'status': 'completed', 'output': [
        {'type': 'message', 'content': [{'type': 'output_text', 'text': 'Finding'}]}]}),
    ('@ai-sdk/openai-compatible', 'chat', '/chat/completions', {'choices': [
        {'finish_reason': 'stop', 'message': {'content': 'Finding'}}]}),
])
def test_other_configured_protocols(provider, monkeypatch, npm, protocol, suffix, data):
    d = json.loads(provider.read_text()); d['provider']['test']['npm'] = npm
    provider.write_text(json.dumps(d))
    calls = answer(monkeypatch, data)
    assert review.run('Review this', 'fable')['result'] == 'Finding'
    assert calls[0].full_url.endswith(suffix)
    assert 'tools' not in json.loads(calls[0].data)


@pytest.mark.parametrize("malformed", [[], None, "not an object", {"content": [None]}])
def test_malformed_provider_output_fails_closed(provider, monkeypatch, malformed):
    answer(monkeypatch, malformed)
    assert review.run("Review this", "fable")["status"] == "failed"


def test_http_protocol_failure_is_sanitized(provider, monkeypatch):
    import http.client
    def fail(*args, **kwargs):
        raise http.client.IncompleteRead(b"fake-test-key private material", 100)
    monkeypatch.setattr(review.urllib.request, "build_opener", lambda *args: SimpleNamespace(open=fail))
    result = review.run("Review this", "fable")
    assert result["status"] == "failed"
    assert "fake-test-key" not in json.dumps(result)
