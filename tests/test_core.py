import io
import tempfile
import threading
import unittest
import zipfile
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch
import core

PDF = b'%PDF-1.4\nfixture\n%%EOF'


def mail(subject='电子发票'):
    return BytesParser(policy=policy.default).parsebytes(f'From: 商家 <seller@qq.com>\r\nSubject: {subject}\r\n\r\n'.encode())


class FilterTests(unittest.TestCase):
    def test_optional_and_combined(self):
        for senders, keywords in [([], ['发票']), (['seller@qq.com'], []), (['商家'], ['发票']), ([], [])]:
            self.assertTrue(core.matches(mail(), senders, keywords))
        self.assertFalse(core.matches(mail(), ['other@qq.com'], ['发票']))
        self.assertFalse(core.matches(mail(), ['seller@qq.com'], ['合同']))
        self.assertFalse(core.matches(mail(), ['eller@qq.com'], []))

    def test_encoded_and_multiple(self):
        self.assertTrue(core.matches(mail('=?utf-8?b?5Y+R56Wo?='), [], core.terms('合同\n发票')))
        self.assertTrue(core.matches(mail('INVOICE April'), [], core.terms('invoice')))
        self.assertTrue(core.matches(mail(), core.terms('other@qq.com；SELLER@QQ.COM'), []))

    def test_date_boundaries(self):
        start, stop = core.date_bounds('2026-10-01', '2026-10-02')
        self.assertEqual(core.received_date(b'INTERNALDATE "30-Sep-2026 16:00:00 +0000"'), start)
        self.assertTrue(start <= core.received_date(b'INTERNALDATE "02-Oct-2026 15:59:59 +0000"') < stop)
        self.assertEqual(core.received_date(b'INTERNALDATE "02-Oct-2026 16:00:00 +0000"'), stop)
        with self.assertRaises(ValueError):
            core.date_bounds('2026-10-03', '2026-10-01')


class StoreTests(unittest.TestCase):
    def test_cross_run_and_changed_or_deleted_files(self):
        with tempfile.TemporaryDirectory(prefix='中文 空格 ') as directory:
            store = core.Store(directory)
            try:
                status, path = store.save(PDF, '发票.pdf', 'mail1', 'run1')
                self.assertEqual(status, '新增')
                self.assertEqual(store.save(PDF, 'different.pdf', 'mail2', 'run2'), ('重复', path))
                store.remove(path)
                self.assertEqual(store.save(PDF, '发票.pdf', 'mail3', 'run3')[0], '新增')
                Path(path).write_bytes(b'user changed this')
                with self.assertRaises(ValueError):
                    store.remove(path)
                status, second = store.save(PDF, '发票.pdf', 'mail4', 'run4')
                self.assertEqual(status, '新增')
                self.assertNotEqual(path, second)
                outside = Path(directory) / 'private.pdf'
                outside.write_bytes(PDF)
                with self.assertRaises(ValueError):
                    store.remove(str(outside))
            finally:
                store.close()

    def test_semantic_dedup_requires_same_key(self):
        with tempfile.TemporaryDirectory() as directory:
            store = core.Store(directory)
            try:
                with patch('core.invoice_key', return_value='12345678|tax|100.00'):
                    store.save(PDF, 'a.pdf', 'mail1', 'run')
                    self.assertEqual(store.save(PDF+b'other', 'b.pdf', 'mail2', 'run')[0], '重复')
                for key, data in [('12345678|tax|200.00', PDF+b'different'), (None, PDF+b'unknown')]:
                    with patch('core.invoice_key', return_value=key):
                        self.assertEqual(store.save(data, 'a.pdf', 'mail3', 'run')[0], '新增')
            finally:
                store.close()


class ArchiveTests(unittest.TestCase):
    def test_reject_traversal_and_size(self):
        for path, size in [('../outside.pdf', 10), ('a.pdf', core.MAX_FILE+1), ('C:\\a.pdf', 10)]:
            unpacker = core.Unpacker(threading.Event(), lambda _: None)
            listing = f'Path = {path}\nSize = {size}\nFolder = -\n'
            with patch('core.seven_path', return_value='7z'), patch('core.command', return_value=(0, listing)), patch.object(unpacker, 'extract') as extract:
                with self.assertRaises(ValueError):
                    list(unpacker.pdfs(b'archive', 'a.zip'))
                extract.assert_not_called()

    def test_password_skip_depth_and_cancel(self):
        event = threading.Event()
        unpacker = core.Unpacker(event, lambda _: None)
        with patch('core.seven_path', return_value='7z'), patch('core.command', return_value=(2, 'Wrong password')):
            with self.assertRaisesRegex(ValueError, '跳过'):
                list(unpacker.pdfs(b'archive', 'a.7z'))
        with self.assertRaisesRegex(ValueError, '嵌套'):
            list(unpacker.pdfs(b'archive', 'a.zip', 4))
        event.set()
        with self.assertRaises(core.Cancelled):
            list(unpacker.pdfs(PDF, 'a.pdf'))

    def test_real_nested_zip(self):
        try:
            core.seven_path()
        except RuntimeError:
            self.skipTest('完整 7-Zip 未安装')
        inner, outer = io.BytesIO(), io.BytesIO()
        with zipfile.ZipFile(inner, 'w') as archive:
            archive.writestr('目录/发票.pdf', PDF)
        with zipfile.ZipFile(outer, 'w') as archive:
            archive.writestr('inner.zip', inner.getvalue())
        unpacker = core.Unpacker(threading.Event(), lambda _: None)
        self.assertEqual(list(unpacker.pdfs(outer.getvalue(), 'outer.zip')), [('目录/发票.pdf', PDF)])


class MailTests(unittest.TestCase):
    def test_readonly_connect_and_sanitized_error(self):
        with patch('core.imaplib.IMAP4_SSL') as factory:
            factory.return_value.select.return_value = ('OK', [])
            core.connect('x@qq.com', 'secret')
            factory.return_value.select.assert_called_once_with('INBOX', readonly=True)
        self.assertNotIn('secret', core.safe_error(core.imaplib.IMAP4.error('secret')))

    def test_empty_mailbox_report(self):
        with tempfile.TemporaryDirectory() as directory, patch('core.connect') as connect:
            connect.return_value.uid.return_value = ('OK', [b''])
            events = []
            core.run_query(core.Query('x@qq.com', 'secret', '2026-10-01', '2026-10-02', '', '发票', directory), threading.Event(), lambda kind, value: events.append((kind, value)), lambda _: None)
            kind, done = events[-1]
            self.assertEqual(kind, 'done')
            self.assertEqual(done['counts'], {'新增': 0, '重复': 0, '失败': 0})
            self.assertIn('邮件标题', Path(done['report']).read_text(encoding='utf-8-sig'))
            self.assertNotIn('secret', Path(done['report']).read_text(encoding='utf-8-sig'))


if __name__ == '__main__':
    unittest.main()
