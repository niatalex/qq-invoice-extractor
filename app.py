import json
import sys
import os
import queue
import threading
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
import credentials
from version import PRODUCT_NAME, WINDOW_TITLE, BRANDING
from core import Query, Store, TZ, connect, date_bounds, run_query, safe_error


class App:
    def __init__(self, window):
        self.window = window
        window.title(WINDOW_TITLE)
        window.geometry('1040x760')
        window.minsize(880, 650)
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.current_directory = ''
        self.config = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'LocalInvoiceExtractor' / 'settings.json'
        try:
            settings = json.loads(self.config.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            settings = {}
        if not isinstance(settings, dict):
            settings = {}
        self.account = tk.StringVar(value=settings.get('account', ''))
        self.secret = tk.StringVar()
        self.keep = tk.BooleanVar(value=settings.get('keep', False))
        self.directory = tk.StringVar(value=settings.get('directory', str(Path.home() / 'Documents' / '发票报销')))
        today = datetime.now(TZ).date()
        self.start = tk.StringVar(value=today.replace(day=1).isoformat())
        self.end = tk.StringVar(value=today.isoformat())
        self.status = tk.StringVar(value='填写 QQ 邮箱和授权码，或载入已保存的授权码。')
        frame = ttk.Frame(window, padding=18)
        frame.pack(fill='both', expand=True)
        frame.columnconfigure(1, weight=1)
        header = ttk.Frame(frame)
        header.grid(row=0, column=0, columnspan=3, sticky='ew', pady=(0, 12))
        ttk.Label(header, text=PRODUCT_NAME, font=('Microsoft YaHei UI', 18, 'bold')).pack(side='left')
        self.branding = ttk.Label(header, text=BRANDING, font=('Microsoft YaHei UI', 10))
        self.branding.pack(side='right')
        self.editable = []
        def entry(row, label, variable, show=None):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky='w', padx=(0, 12), pady=5)
            widget = ttk.Entry(frame, textvariable=variable, show=show)
            widget.grid(row=row, column=1, sticky='ew', pady=5)
            self.editable.append(widget)
            return widget
        account_entry = entry(1, 'QQ 邮箱', self.account)
        account_entry.bind('<FocusOut>', lambda _: self.load_secret())
        entry(2, '邮箱授权码', self.secret, '•')
        self.keep_button = ttk.Checkbutton(frame, text='用 Windows 凭据管理器保存授权码', variable=self.keep)
        self.keep_button.grid(row=3, column=1, sticky='w')
        self.editable.append(self.keep_button)
        self.test_button = ttk.Button(frame, text='测试连接', command=self.test)
        self.test_button.grid(row=2, column=2, padx=8)
        self.editable.append(self.test_button)
        entry(4, '开始日期 YYYY-MM-DD', self.start)
        entry(5, '结束日期 YYYY-MM-DD', self.end)
        ttk.Label(frame, text='发件人（可留空）\n每行一个邮箱或名称').grid(row=6, column=0, sticky='w')
        self.senders = tk.Text(frame, height=3, width=50, font=('Microsoft YaHei UI', 10))
        self.senders.grid(row=6, column=1, columnspan=2, sticky='ew', pady=5)
        ttk.Label(frame, text='标题关键字（可留空）\n每行一个，例如：发票').grid(row=7, column=0, sticky='w')
        self.keywords = tk.Text(frame, height=3, width=50, font=('Microsoft YaHei UI', 10))
        self.keywords.grid(row=7, column=1, columnspan=2, sticky='ew', pady=5)
        self.keywords.insert('1.0', '发票')
        self.editable.extend([self.senders, self.keywords])
        ttk.Label(frame, text='同时填写时须同时匹配；每组内任意一项匹配即可。仅查询收件箱，包含起止日期全天。').grid(row=8, column=0, columnspan=3, sticky='w', pady=5)
        entry(9, '保存目录', self.directory)
        choose = ttk.Button(frame, text='选择目录', command=self.choose)
        choose.grid(row=9, column=2, padx=8)
        self.editable.append(choose)
        actions = ttk.Frame(frame)
        actions.grid(row=10, column=0, columnspan=3, sticky='ew', pady=10)
        self.start_button = ttk.Button(actions, text='开始提取', command=self.start_query)
        self.start_button.pack(side='left')
        self.cancel_button = ttk.Button(actions, text='取消', command=self.stop, state='disabled')
        self.cancel_button.pack(side='left', padx=8)
        ttk.Button(actions, text='打开保存文件夹', command=self.open_folder).pack(side='left', padx=8)
        ttk.Button(actions, text='预览选中 PDF', command=self.preview).pack(side='left', padx=8)
        self.remove_button = ttk.Button(actions, text='移除选中 PDF', command=self.remove)
        self.remove_button.pack(side='left', padx=8)
        ttk.Label(frame, textvariable=self.status, wraplength=980).grid(row=11, column=0, columnspan=3, sticky='w', pady=5)
        self.progress = ttk.Progressbar(frame, mode='indeterminate')
        self.progress.grid(row=12, column=0, columnspan=3, sticky='ew')
        self.results = ttk.Treeview(frame, columns=('status','subject','attachment','detail'), show='headings', selectmode='browse')
        for key, title, width in [('status','状态',60),('subject','邮件标题',240),('attachment','附件',240),('detail','位置 / 说明',350)]:
            self.results.heading(key, text=title)
            self.results.column(key, width=width)
        self.results.grid(row=13, column=0, columnspan=3, sticky='nsew', pady=8)
        scrollbar = ttk.Scrollbar(frame, orient='vertical', command=self.results.yview)
        scrollbar.grid(row=13, column=3, sticky='ns')
        self.results.configure(yscrollcommand=scrollbar.set)
        self.results.bind('<Double-1>', lambda _: self.preview())
        frame.rowconfigure(13, weight=1)
        self.paths = {}
        self.load_secret()
        window.protocol('WM_DELETE_WINDOW', self.close)
        window.after(100, self.poll)

    def load_secret(self):
        if self.busy:
            return
        try:
            self.secret.set(credentials.read(self.account.get()))
        except RuntimeError:
            self.secret.set('')

    def choose(self):
        directory = filedialog.askdirectory()
        if directory:
            self.directory.set(directory)

    def validate(self):
        account = self.account.get().strip()
        secret = self.secret.get().strip()
        if '@' not in account or not secret:
            raise ValueError('请填写完整邮箱地址和邮箱授权码')
        date_bounds(self.start.get().strip(), self.end.get().strip())
        if not self.directory.get().strip():
            raise ValueError('请选择保存目录')
        return Query(account, secret, self.start.get().strip(), self.end.get().strip(), self.senders.get('1.0', 'end').strip(), self.keywords.get('1.0', 'end').strip(), self.directory.get().strip())

    def settings(self, query):
        if self.keep.get():
            credentials.save(query.account, query.secret)
        else:
            credentials.delete(query.account)
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(json.dumps({'account': query.account, 'directory': query.directory, 'keep': self.keep.get()}, ensure_ascii=False), encoding='utf-8')

    def set_busy(self, value):
        self.busy = value
        for widget in self.editable:
            widget.configure(state='disabled' if value else 'normal')
        self.start_button.configure(state='disabled' if value else 'normal')
        self.remove_button.configure(state='disabled' if value else 'normal')
        self.cancel_button.configure(state='normal' if value else 'disabled')
        if value:
            self.cancel.clear()
            self.progress.start(15)
        else:
            self.progress.stop()

    def emit(self, kind, value):
        self.events.put((kind, value))

    def ask_password(self, name):
        response = queue.Queue(maxsize=1)
        self.events.put(('password', (name, response)))
        while not self.cancel.is_set():
            try:
                return response.get(timeout=.1)
            except queue.Empty:
                pass
        return None

    def test(self):
        try:
            query = self.validate()
            self.settings(query)
        except Exception as error:
            messagebox.showerror('设置错误', safe_error(error))
            return
        self.set_busy(True)
        self.status.set('正在测试连接…')
        def work():
            try:
                client = connect(query.account, query.secret)
                client.logout()
                self.emit('tested', '连接成功，可开始提取。')
            except Exception as error:
                self.emit('tested', safe_error(error))
        threading.Thread(target=work, daemon=True).start()

    def start_query(self):
        try:
            query = self.validate()
            self.settings(query)
        except Exception as error:
            messagebox.showerror('设置错误', safe_error(error))
            return
        if not query.senders and not query.keywords:
            messagebox.showinfo('查询范围', '将提取该时间段收件箱中的全部 PDF 附件。')
        self.results.delete(*self.results.get_children())
        self.paths.clear()
        self.current_directory = query.directory
        self.set_busy(True)
        def work():
            try:
                run_query(query, self.cancel, self.emit, self.ask_password)
            except Exception as error:
                self.emit('fatal', safe_error(error))
        threading.Thread(target=work, daemon=True).start()

    def stop(self):
        self.cancel.set()
        self.status.set('正在取消，保留已提取文件；等待当前网络操作结束…')

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'progress':
                    self.status.set(value)
                elif kind == 'result':
                    identifier = self.results.insert('', 'end', values=(value['status'], value['subject'], value['attachment'], value['detail'] or value['path']))
                    self.paths[identifier] = value['path']
                elif kind == 'password':
                    name, response = value
                    answer = None if self.cancel.is_set() else simpledialog.askstring('压缩包密码', f'请输入 {name} 的密码（取消则跳过）：', show='•', parent=self.window)
                    response.put(answer)
                elif kind == 'done':
                    counts = value['counts']
                    self.status.set(f"{value['state']}：新增 {counts['新增']}，重复 {counts['重复']}，失败 {counts['失败']}。清单：{value['report']}")
                    self.set_busy(False)
                elif kind in ('tested', 'fatal'):
                    self.status.set(value)
                    self.set_busy(False)
        except queue.Empty:
            pass
        self.window.after(100, self.poll)

    def selected_path(self):
        selection = self.results.selection()
        return self.paths.get(selection[0], '') if selection else ''

    def preview(self):
        path = self.selected_path()
        if path and Path(path).is_file():
            try:
                os.startfile(path)
            except OSError:
                messagebox.showerror('预览失败', '请安装 PDF 阅读器并关联 PDF 文件。')

    def open_folder(self):
        path = Path(self.current_directory or self.directory.get()) / '发票'
        if path.is_dir():
            os.startfile(str(path))

    def remove(self):
        path = self.selected_path()
        if not path or not self.current_directory:
            return
        if not messagebox.askyesno('移除 PDF', f'从磁盘删除这份工具保存的 PDF？\n{path}\n以后再次查询可重新提取。'):
            return
        store = None
        try:
            store = Store(self.current_directory)
            store.remove(path)
            for identifier, saved in self.paths.items():
                if saved == path:
                    self.results.set(identifier, 'status', '已移除')
                    self.paths[identifier] = ''
        except Exception as error:
            messagebox.showerror('移除失败', safe_error(error))
        finally:
            if store:
                store.close()

    def close(self):
        if self.busy:
            self.stop()
            messagebox.showinfo('正在停止', '请等待任务停止后关闭窗口，以确保清单保存。')
            return
        self.window.destroy()


if __name__ == '__main__':
    window = tk.Tk()
    if '--smoke-test' in sys.argv:
        window.withdraw()
        App(window)
        window.update()
        # Exercise bundled PDF, SQLite and 7-Zip without connecting to a mailbox.
        import io
        import tempfile
        import zipfile
        from pypdf import PdfWriter
        from core import Unpacker
        buffer = io.BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        writer.write(buffer)
        payload = buffer.getvalue()
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as pack:
            pack.writestr('smoke.pdf', payload)
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                unpacked = list(Unpacker(threading.Event(), lambda _: None).pdfs(archive.getvalue(), 'smoke.zip'))
                assert unpacked == [('smoke.pdf', payload)]
                assert store.save(payload, 'smoke.pdf', 'smoke', 'smoke')[0] == '新增'
                assert store.save(payload, 'smoke.pdf', 'smoke', 'smoke')[0] == '重复'
            finally:
                store.close()
        window.destroy()
    else:
        App(window)
        window.mainloop()
