import os
import unittest
import uuid
import credentials


@unittest.skipUnless(os.name == 'nt', 'Windows only')
class WindowsTests(unittest.TestCase):
    def test_credentials_roundtrip_and_delete(self):
        account = 'test-' + uuid.uuid4().hex + '@local.invalid'
        try:
            self.assertEqual(credentials.read(account), '')
            credentials.save(account, '临时-test-secret')
            self.assertEqual(credentials.read(account), '临时-test-secret')
            credentials.delete(account)
            self.assertEqual(credentials.read(account), '')
        finally:
            credentials.delete(account)

    def test_desktop_constructs_without_network(self):
        import tkinter as tk
        from app import App
        window = tk.Tk()
        window.withdraw()
        try:
            app = App(window)
            window.update()
            self.assertEqual(app.keywords.get('1.0', 'end').strip(), '发票')
            self.assertFalse(app.busy)
            self.assertEqual(window.title(), 'QQ 邮箱发票提取工具 · v1.0试用')
            self.assertEqual(app.branding.cget('text'), 'v1.0试用｜作者：阿俊工作室')
        finally:
            window.destroy()
