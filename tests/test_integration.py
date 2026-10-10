import io
import tempfile
import threading
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch, MagicMock
import core


class IntegrationTests(unittest.TestCase):
    def test_real_rar_stream(self):
        try:
            executable = core.seven_path()
        except RuntimeError:
            self.skipTest('完整 7-Zip 未安装')
        fixture = Path(__file__).parent / 'fixtures' / 'rar3-subdirs.rar'
        if not fixture.exists():
            self.skipTest('RAR 测试样本不存在')
        unpacker = core.Unpacker(threading.Event(), lambda _: None)
        self.assertEqual(unpacker.extract(executable, fixture, 'sub\\dir1\\file1.txt', ''), b'file1\n')

    def test_mail_fetch_peek_subject_filter_dedup_and_failure(self):
        message = EmailMessage()
        message['From'] = '商家 <seller@qq.com>'
        message['Subject'] = '电子发票'
        message.set_content('附件请查收')
        message.add_attachment(b'%PDF-1.4\nfixture\n%%EOF', maintype='application', subtype='pdf', filename='发票.pdf')
        message.add_attachment(b'broken', maintype='application', subtype='pdf', filename='损坏.pdf')
        body = message.as_bytes()
        header = b'From: seller@qq.com\r\nSubject: =?utf-8?b?5Y+R56Wo?=\r\n\r\n'
        requests = []
        class Client:
            def uid(self, *args):
                requests.append(args)
                if args[0] == 'search':
                    return 'OK', [b'1']
                if 'HEADER.FIELDS' in args[2]:
                    return 'OK', [(b'1 (INTERNALDATE "01-Oct-2026 08:00:00 +0800" RFC822.SIZE 1000)', header)]
                return 'OK', [(b'1 (BODY[]', body)]
            def logout(self):
                pass
        with tempfile.TemporaryDirectory() as directory, patch('core.connect', return_value=Client()):
            query = core.Query('me@qq.com', 'never-log-this-secret', '2026-10-01', '2026-10-01', '', '发票', directory)
            for expected in ['新增', '重复']:
                events = []
                core.run_query(query, threading.Event(), lambda kind, value: events.append((kind, value)), lambda _: None)
                done = events[-1][1]
                self.assertEqual(done['counts'][expected], 1)
                self.assertEqual(done['counts']['失败'], 1)
                report = Path(done['report']).read_text(encoding='utf-8-sig')
                self.assertIn('发票', report)
                self.assertNotIn(query.secret, report)
            self.assertTrue(all('PEEK' in item[2] for item in requests if item[0] == 'fetch'))

    def test_invoice_parser_ambiguity(self):
        import pypdf
        base = '电子发票\n发票号码：12345678\n销售方信息\n纳税人识别号：123456789012345678\n项目名称\n价税合计（小写）￥100.00'
        for text, expected in [(base, '12345678|123456789012345678|100.00'), (base+'\n发票号码：87654321', None), (base.replace('销售方信息', '购买方信息'), None), (base.replace('100.00', '待确认'), None)]:
            reader = MagicMock()
            reader.is_encrypted = False
            page = MagicMock()
            page.extract_text.return_value = text
            reader.pages = [page]
            with patch.object(pypdf, 'PdfReader', return_value=reader):
                self.assertEqual(core.invoice_key(b'fixture'), expected)

    def test_real_7z_encrypted_and_corrupt(self):
        try:
            executable = core.seven_path()
        except RuntimeError:
            self.skipTest('完整 7-Zip 未安装')
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / '发票.pdf'
            payload = b'%PDF-1.4\nfixture\n%%EOF'
            source.write_bytes(payload)
            archive = Path(directory) / '密码.7z'
            cancel = threading.Event()
            code, _ = core.command([executable, 'a', '-t7z', '-ptest-password', '-mhe=on', str(archive), str(source)], cancel)
            self.assertEqual(code, 0)
            unpacker = core.Unpacker(cancel, lambda _: 'test-password')
            self.assertEqual(list(unpacker.pdfs(archive.read_bytes(), '密码.7z')), [('发票.pdf', payload)])
            with self.assertRaises(ValueError):
                list(unpacker.pdfs(b'corrupt', '损坏.rar'))


if __name__ == '__main__':
    unittest.main()
