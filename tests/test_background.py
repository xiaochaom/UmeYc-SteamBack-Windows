from umeyc_steamback.background import UploadQueue


GAME = {"game_id": 42, "game_name": "测试游戏"}


def test_exit_queues_once_waits_for_flush_and_persists(tmp_path):
    queue = UploadQueue(tmp_path)
    queue.observe("uid", 123, [GAME], {42}, now=0)
    assert not queue.pending
    queue.observe("uid", 123, [GAME], set(), now=10)
    assert queue.next("uid", 123, set(), now=15) is None
    item = queue.next("uid", 123, set(), now=16)
    assert item["game"] == GAME
    restarted = UploadQueue(tmp_path)
    assert restarted.next("uid", 123, set(), now=16)
    restarted.finish(item, now=20)
    assert not restarted.pending


def test_relaunch_cancels_immediate_upload_and_delays_until_next_exit(tmp_path):
    queue = UploadQueue(tmp_path)
    queue.observe("uid", 123, [GAME], {42}, now=0)
    queue.observe("uid", 123, [GAME], set(), now=10)
    queue.observe("uid", 123, [GAME], {42}, now=20)
    assert queue.next("uid", 123, {42}, now=100) is None
    queue.observe("uid", 123, [GAME], set(), now=100)
    assert queue.next("uid", 123, set(), now=105) is None
    assert queue.next("uid", 123, set(), now=106)


def test_failure_retries_without_dropping_work_and_is_account_scoped(tmp_path):
    queue = UploadQueue(tmp_path)
    queue.observe("uid", 123, [GAME], {42}, now=0)
    queue.observe("uid", 123, [GAME], set(), now=1)
    item = queue.next("uid", 123, set(), now=10)
    queue.finish(item, ValueError("网络断开"), now=10)
    assert queue.next("uid", 123, set(), now=69) is None
    assert queue.next("uid", 123, set(), now=70)
    assert queue.next("other", 123, set(), now=100) is None
    assert queue.next("uid", 124, set(), now=100) is None
    queue.clear("uid", 123, 42)
    assert not queue.pending


def test_multiple_games_wait_independently_and_disabled_games_do_not_queue(tmp_path):
    other = {"game_id": 43, "game_name": "other"}
    queue = UploadQueue(tmp_path)
    queue.observe("uid", 123, [GAME, other], {42, 43}, now=0)
    queue.observe("uid", 123, [GAME, other], {43}, now=10)
    first = queue.next("uid", 123, {43}, now=16)
    assert first["game"]["game_id"] == 42
    queue.finish(first, now=16)
    assert queue.next("uid", 123, {43}, now=30) is None
    queue.observe("uid", 123, [GAME, other], set(), now=40)
    assert queue.next("uid", 123, set(), now=46)["game"]["game_id"] == 43
    queue.observe("uid", 123, [GAME], set(), now=47)
    assert not queue.pending
