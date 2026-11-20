from unittest.mock import patch
import pytest
from tests.test_sync import setup
from umeyc_steamback.sync import WindowsCloud
from umeyc_steamback.protocol.cloud import CloudConflict
from umeyc_steamback.protocol.quark import QuarkError
from umeyc_steamback.protocol.custom import write_json


def fail_offline(setup):
    e, game, source, client, cloud = setup
    cloud.set_auto_sync(123, game, True)
    cloud.upload(123, game)
    base = cloud.latest(123, game)
    (source / 'slot.sav').write_bytes(b'offline progress')
    item = cloud.retry_queue.enqueue(client.user_id, 123, game, base['content_hash'], now=0)
    with patch.object(client, 'account', side_effect=QuarkError('offline')):
        with pytest.raises(QuarkError) as error:
            cloud.upload_after_exit(123, game, client.user_id, wait=lambda _: None, base_hash=item['base_hash'])
    cloud.retry_queue.finish(item, error.value, now=10)
    return base


def test_retry_uses_persisted_base_and_checks_cloud_before_upload(setup):
    e, game, source, client, cloud = setup
    fail_offline(setup)
    (source / 'slot.sav').write_bytes(b'other machine progress')
    other = WindowsCloud(e, client)
    # Simulate another device, sharing only the in-memory remote. Keep this
    # device's pending queue intact even while advancing mutable sync metadata.
    with patch.object(other.retry_queue, 'clear'):
        other.upload(123, game)
    remote = other.latest(123, game)
    (source / 'slot.sav').write_bytes(b'offline progress')
    resumed = WindowsCloud(e, client)
    item = resumed.retry_queue.next(client.user_id, 123, set(), now=70)
    with pytest.raises(CloudConflict) as error:
        resumed.upload_after_exit(123, game, client.user_id, wait=lambda _: None, base_hash=item['base_hash'])
    resumed.retry_queue.finish(item, error.value, conflict=True, now=70)
    assert resumed.retry_queue.next(client.user_id, 123, set(), now=10**12) is None
    assert resumed.latest(123, game)['revision'] == remote['revision']
    assert (source / 'slot.sav').read_bytes() == b'offline progress'
    resumed.download(123, game, remote['revision'], client.user_id)
    assert not WindowsCloud(e, client).retry_queue.pending
    assert (source / 'slot.sav').read_bytes() == b'other machine progress'


@pytest.mark.parametrize('same_remote', [False, True])
def test_retry_uploads_only_if_cloud_unchanged_or_already_same(setup, same_remote):
    e, game, source, client, cloud = setup
    fail_offline(setup)
    if same_remote:
        other = WindowsCloud(e, client)
        with patch.object(other.retry_queue, 'clear'):
            other.upload(123, game)
    remote = cloud.latest(123, game)
    resumed = WindowsCloud(e, client)
    item = next(iter(resumed.retry_queue.pending.values()))
    resumed.upload_after_exit(123, game, client.user_id, wait=lambda _: None, base_hash=item['base_hash'])
    resumed.retry_queue.finish(item)
    assert not WindowsCloud(e, client).retry_queue.pending
    assert (resumed.latest(123, game)['revision'] == remote['revision']) == same_remote


def test_retry_detects_cloud_update_during_archive_transfer(setup):
    e, game, source, client, cloud = setup
    base = fail_offline(setup)
    item = next(iter(cloud.retry_queue.pending.values()))
    original = cloud.latest
    calls = []
    def concurrent(*args):
        calls.append(1)
        remote = original(*args)
        return {**remote, 'revision': 'another-commit'} if len(calls) > 1 else remote
    with patch.object(cloud, 'latest', side_effect=concurrent), pytest.raises(CloudConflict):
        cloud.upload_after_exit(123, game, client.user_id, wait=lambda _: None, base_hash=item['base_hash'])
    assert original(123, game)['revision'] == base['revision']
    assert not any(f['file_name'].endswith('.json') and f['fid'] != base['revision'] for f in client.items.values())
