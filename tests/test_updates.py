import io
import json
from urllib.error import HTTPError, URLError

import pytest
from umeyc_steamback import updates


def release(tag='v0.1.2', **fields):
    return {'tag_name': tag, 'draft': False, 'prerelease': False,
            'assets': [{'name': 'UmeYc-SteamBack-Windows.zip', 'size': 100, 'state': 'uploaded'}],
            **fields}


@pytest.mark.parametrize('tag,current,available', [('v0.1.2', '0.1.1', True), ('v0.1.10', '0.1.9', True),
    ('v0.2.0', '0.1.99', True), ('v0.1.1', '0.1.1', False), ('v0.1.0', '0.1.1', False)])
def test_compares_numeric_versions_without_downgrading(tag, current, available):
    result = updates.parse_release(release(tag, html_url='https://untrusted.invalid/installer'), current)
    assert result.available is available
    assert result.url == updates.REPOSITORY_URL + '/releases/tag/' + tag


@pytest.mark.parametrize('data', [release(prerelease=True), release(draft=True), release('v0.2.0-rc.1'),
    release('unrecognized'), release(assets=[]), release(assets=[{'name': 'other-platform.zip'}]), [], {}])
def test_rejects_unreleased_invalid_or_unavailable_builds(data):
    with pytest.raises(ValueError):
        updates.parse_release(data, '0.1.1')


def test_public_request_has_timeout_and_no_quark_or_github_credentials(monkeypatch):
    requests = []
    def get(request, timeout):
        requests.append((request, timeout))
        return io.BytesIO(json.dumps(release()).encode())
    monkeypatch.setattr(updates, 'urlopen', get)
    assert updates.check_updates('0.1.1').available
    request, timeout = requests[0]
    assert request.full_url == updates.LATEST_API
    assert 0 < timeout <= 10
    assert not request.has_header('Authorization')
    assert not request.has_header('Cookie')


@pytest.mark.parametrize('error,expected', [
    (URLError('offline'), '检查网络'), (TimeoutError('timeout'), '检查网络'),
    (HTTPError(updates.LATEST_API, 403, '', {}, None), '频繁'),
    (HTTPError(updates.LATEST_API, 404, '', {}, None), '暂无'),
    (HTTPError(updates.LATEST_API, 500, '', {}, None), '暂不可用')])
def test_network_failures_are_clear_and_do_not_expose_response_details(monkeypatch, error, expected):
    def get(*args, **kwargs):
        raise error
    monkeypatch.setattr(updates, 'urlopen', get)
    with pytest.raises(ValueError, match=expected):
        updates.check_updates('0.1.1')


@pytest.mark.parametrize('payload', [b'<html>error</html>', b'x' * (updates.MAX_RESPONSE + 1)])
def test_limits_response_and_handles_invalid_json(monkeypatch, payload):
    monkeypatch.setattr(updates, 'urlopen', lambda *a, **kw: io.BytesIO(payload))
    with pytest.raises(ValueError):
        updates.check_updates('0.1.1')
