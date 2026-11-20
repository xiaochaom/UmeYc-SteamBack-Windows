"""夸克协议适配的模拟 HTTP 测试；不访问用户网盘。"""
import base64
import http.cookiejar
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from umeyc_steamback.protocol.quark import QuarkClient, QuarkError, LoginExpired


class Response(io.BytesIO):
    def __init__(self, value=b'', headers=None):
        super().__init__(value if isinstance(value, bytes) else json.dumps(value).encode())
        self.headers = headers or {}


class QuarkProtocol(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.client = QuarkClient(self.root)

    def cookie(self, name, value):
        self.client.jar.set_cookie(http.cookiejar.Cookie(
            0, name, value, None, False, '.quark.cn', True, True, '/', True, True,
            None, True, None, None, {}, False))

    def test_scan_login_persists_private_cookie_file_and_exposes_profile_without_secrets(self):
        def protocol(method, domain, path, params=None, payload=None, raw=False):
            if path.endswith('getTokenForQrcodeLogin'):
                return {'data': {'members': {'token': 'private-token'}}}
            if path.endswith('getServiceTicketByQrcodeToken'):
                return {'status': 2000000, 'data': {'members': {'service_ticket': 'private-ticket'}}}
            if path == '/account/info':
                if params:
                    self.assertEqual(params['st'], 'private-ticket')
                    self.cookie('__puus', 'secret-cookie'); self.cookie('__uid', 'user123')
                    return {'success': True, 'code': 'OK', 'data': {}}
                return {'success': True, 'code': 'OK', 'data': {'avatarUri': 'avatar', 'nickname': 'tester'}}
            raise AssertionError(path)
        with patch.object(self.client, '_json', side_effect=protocol):
            qr = self.client.begin_login()
            self.assertIn('private-token', qr['url'])
            result = self.client.poll_login(qr['session_id'])
            self.assertEqual(result, {'state': 'logged_in', 'logged_in': True, 'user_id': 'user123', 'nickname': 'tester'})
        if os.name == "nt":
            self.assertNotIn(b'secret-cookie', self.client.cookie_path.read_bytes())
        else:
            self.assertEqual(self.client.cookie_path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('secret-cookie', json.dumps(result))
        reloaded = QuarkClient(self.root)
        self.assertEqual(reloaded.user_id, 'user123')
        reloaded.logout()
        self.assertFalse(reloaded.cookie_path.exists())
        with self.assertRaises(LoginExpired):
            reloaded.account()

    def test_account_nickname_is_optional_and_does_not_change_identity(self):
        self.cookie('__uid', 'user123')
        for nickname, expected in [('  中文昵称  ', '中文昵称'), ('  ', None), (None, None), (123, None)]:
            with self.subTest(nickname=nickname), patch.object(self.client, '_json', return_value={
                    'data': {'nickname': nickname}}):
                self.assertEqual(self.client.account(), {
                    'logged_in': True, 'user_id': 'user123', 'nickname': expected})
        with patch.object(self.client, '_json', return_value={'data': {}}):
            self.assertIsNone(self.client.account()['nickname'])

    def test_expired_cancelled_and_replaced_qr_sessions(self):
        with patch.object(self.client, '_json', return_value={'data': {'members': {'token': 'test'}}}):
            old = self.client.begin_login(); new = self.client.begin_login()
        self.assertEqual(self.client.poll_login(old['session_id'])['state'], 'expired')
        with patch.object(self.client, '_json', return_value={'status': 50004001}):
            self.assertEqual(self.client.poll_login(new['session_id'])['state'], 'waiting')
        self.client.cancel_login(new['session_id'])
        self.assertEqual(self.client.poll_login(new['session_id'])['state'], 'expired')
        with patch.object(self.client, '_json', return_value={'data': {'members': {'token': 'test'}}}):
            new = self.client.begin_login()
        with patch.object(self.client, '_json', return_value={'status': 50004002}):
            self.assertEqual(self.client.poll_login(new['session_id'])['state'], 'expired')

    def test_http_errors_are_redacted_and_auth_expiry_is_distinct(self):
        with patch.object(self.client.opener, 'open', side_effect=HTTPError(
                'https://pan.quark.cn/?st=secret-ticket', 401, 'secret-cookie', {}, None)):
            with self.assertRaisesRegex(LoginExpired, '重新扫码') as error:
                self.client._json('GET', self.client.PAN, '/account/info')
            self.assertNotIn('secret', str(error.exception))
        with patch.object(self.client.opener, 'open', side_effect=URLError('cookie=secret')):
            with self.assertRaisesRegex(QuarkError, '检查网络') as error:
                self.client._json('GET', self.client.PAN, '/account/info')
            self.assertNotIn('secret', str(error.exception))
        with patch.object(self.client, '_open', return_value=Response({'status': 200, 'data': {'uid': 'x'}})):
            self.assertEqual(self.client._json('GET', self.client.PAN, '/account/info')['data']['uid'], 'x')

    def test_quark_account_success_envelope_accepts_ok_code(self):
        with patch.object(self.client, '_open', return_value=Response(
                {'success': True, 'data': {'uid': 'x'}, 'code': 'OK'})):
            self.assertEqual(self.client._json('GET', self.client.PAN, '/account/info')['data']['uid'], 'x')

    def test_download_domain_and_size_validation(self):
        for url in ['http://pan.quark.cn/file', 'https://quark.cn.evil.test/file', 'https://127.0.0.1/file',
                    'https://user:secret@pan.quark.cn/file', 'https://pan.quark.cn:8080/file']:
            with self.assertRaises(QuarkError): self.client.check_url(url)
        for url in ['https://drive-pc.quark.cn/a', 'https://ul-sz.pds.quark.cn/a', 'https://bucket.oss-cn.aliyuncs.com/a']:
            self.client.check_url(url)
        with patch.object(self.client, '_json', return_value={'data': [{'download_url': 'https://pan.quark.cn/file'}]}), \
                patch.object(self.client, '_open', return_value=Response(b'123456')):
            with self.assertRaisesRegex(QuarkError, '超过'):
                self.client.download('fid', self.root/'file', limit=3)

    def test_folder_listing_paginates_and_rejects_name_collision(self):
        pages = [{'data': {'list': [{'fid': str(i), 'file_name': str(i)} for i in range(100)]}},
                 {'data': {'list': [{'fid': 'next', 'file_name': 'next'}]}}]
        with patch.object(self.client, '_json', side_effect=pages) as api:
            self.assertEqual(len(self.client.list_files('parent')), 101)
            self.assertEqual(api.call_args_list[1].args[3]['_page'], 2)
        with patch.object(self.client, 'list_files', return_value=[{'file_name': 'UmeYc-SteamBack', 'file_type': 1, 'fid': 'x'}]):
            with self.assertRaisesRegex(QuarkError, '名称冲突'):
                self.client.folder('0', 'UmeYc-SteamBack', True)

    def test_upload_fast_path_still_requires_confirmed_listed_file(self):
        file = self.root/'save.zip'; file.write_bytes(b'snapshot')
        with patch.object(self.client, '_json', side_effect=[{'data': {'task_id': 't'}}, {'data': {'finish': True}}]), \
                patch.object(self.client, 'list_files', return_value=[{'fid': 'done', 'file_name': 'snapshot.zip', 'size': 8}]), \
                patch.object(self.client, '_multipart') as multipart:
            self.assertEqual(self.client.upload(file, 'folder', 'snapshot.zip')['fid'], 'done')
            multipart.assert_not_called()

    def test_multipart_signs_upload_parts_and_completion_without_sending_login_cookie(self):
        file = self.root/'data'; file.write_bytes(b'archive')
        info = {'bucket': 'ul-sz', 'obj_key': 'abc/object', 'upload_url': 'https://pds.quark.cn',
                'upload_id': 'upload1', 'task_id': 'task1', 'auth_info': 'auth', 'callback': {'callbackUrl': 'callback'}}
        requests = []
        def transport(method, url, body=None, headers=None, authenticated=True):
            requests.append((method, url, body, headers, authenticated))
            return Response(b'{}', {'ETag': '"checksum"'})
        with patch.object(self.client, '_json', return_value={'data': {'auth_key': 'OSS signature'}}) as api, \
                patch.object(self.client, '_open', side_effect=transport):
            self.client._multipart(file, info, {}, 'application/octet-stream')
        self.assertEqual([r[0] for r in requests], ['PUT', 'POST'])
        self.assertTrue(all(not r[4] for r in requests))
        self.assertTrue(all('Cookie' not in r[3] for r in requests))
        self.assertEqual(urlsplit(requests[0][1]).hostname, 'ul-sz.pds.quark.cn')
        self.assertIn('<ETag>checksum</ETag>', requests[1][2].decode())
        self.assertEqual(api.call_count, 2)
        self.assertIn('x-oss-callback:', api.call_args.kwargs['payload']['auth_meta'])

    def test_delete_only_explicit_ids_and_never_clear_recycle_bin(self):
        with patch.object(self.client, '_json', return_value={'status': 200}) as api:
            self.client.delete_files(['old-index', 'old-archive'])
            api.assert_called_once_with('POST', self.client.DRIVE, '/1/clouddrive/file/delete', payload={
                'action_type': 2, 'filelist': ['old-index', 'old-archive'], 'exclude_fids': []})
            with self.assertRaises(ValueError):
                self.client.delete_files([])
            with self.assertRaises(ValueError):
                self.client.delete_files(list(range(101)))
            self.assertEqual(api.call_count, 1)
