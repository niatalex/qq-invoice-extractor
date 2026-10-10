"""Read-only QQ mailbox extraction. No credentials are written to disk."""
from __future__ import annotations
import csv
import hashlib
import imaplib
import io
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from pathlib import Path, PureWindowsPath

TZ = timezone(timedelta(hours=8))
MAX_FILE = 100 * 1024 * 1024
MAX_TOTAL = 500 * 1024 * 1024
ARCHIVES = {'.zip', '.rar', '.7z'}


class Cancelled(Exception):
    pass


def check_cancel(event):
    if event.is_set():
        raise Cancelled()


def terms(text):
    return [v.strip().casefold() for v in re.split(r'[\r\n;；]+', text) if v.strip()]


def matches(message, senders, keywords):
    name, address = parseaddr(str(message.get('From', '')))
    sender_ok = not senders or any(
        address.casefold() == s if '@' in s else s in name.casefold() for s in senders
    )
    subject = str(message.get('Subject', '')).casefold()
    return sender_ok and (not keywords or any(k in subject for k in keywords))


def date_bounds(start, end):
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if last < first:
        raise ValueError('结束日期不能早于开始日期')
    return datetime.combine(first, datetime.min.time(), TZ), datetime.combine(last + timedelta(days=1), datetime.min.time(), TZ)


def received_date(metadata):
    match = re.search(rb'INTERNALDATE "([^"]+)"', metadata)
    if not match:
        raise ValueError('邮件缺少收信时间')
    # Month names are IMAP English tokens; avoid dependence on Windows locale.
    value = match[1].decode('ascii')
    found = re.fullmatch(r'\s*(\d{1,2})-([A-Za-z]{3})-(\d{4}) (\d{2}):(\d{2}):(\d{2}) ([+-])(\d{2})(\d{2})', value)
    if not found:
        raise ValueError('收信时间格式错误')
    day, month, year, hh, mm, ss, sign, zh, zm = found.groups()
    months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
    offset = timedelta(hours=int(zh), minutes=int(zm)) * (1 if sign == '+' else -1)
    return datetime(int(year), months.index(month.title()) + 1, int(day), int(hh), int(mm), int(ss), tzinfo=timezone(offset)).astimezone(TZ)


def safe_name(name):
    name = re.split(r'[/\\]', name)[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).rstrip(' .')
    if not name:
        name = '附件.pdf'
    if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', name, re.I):
        name = '_' + name
    suffix = Path(name).suffix
    return Path(name).stem[:120] + suffix


def invoice_key(data):
    """Conservative text-only recognition; ambiguous layouts never collapse files."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted or len(reader.pages) != 1:
            return None
        text = reader.pages[0].extract_text() or ''
        if '发票' not in text:
            return None
        numbers = re.findall(r'发票号码\s*[:：]?\s*(\d{20}|\d{8})(?!\d)', text)
        # Require an explicit seller block, never infer seller from tax-ID order.
        sellers = re.findall(r'销售方(?:信息)?\s*[:：]?([\s\S]{0,400}?)(?:购买方|项目名称|货物或应税|合\s*计|价税合计|备注|$)', text)
        taxes = []
        for block in sellers:
            taxes.extend(re.findall(r'(?:纳税人识别号|统一社会信用代码)\s*[:：]?\s*([A-Z0-9]{18}|[A-Z0-9]{15}|[A-Z0-9]{20})(?![A-Z0-9])', block))
        amounts = re.findall(r'价税合计[^\n]{0,100}?(?:小写)[）)]?\s*[:：]?\s*[¥￥]?\s*(\d+\.\d{2})(?!\d)', text)
        if len(numbers) == len(taxes) == len(amounts) == 1:
            return '|'.join([numbers[0], taxes[0], str(Decimal(amounts[0]))])
    except Exception:
        pass
    return None


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.output = self.root / '发票'
        self.state = self.root / '.invoice-extractor'
        self.output.mkdir(parents=True, exist_ok=True)
        self.state.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.state / 'records.sqlite3')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY, hash TEXT NOT NULL, invoice TEXT, path TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS hash_index ON files(hash);
            CREATE INDEX IF NOT EXISTS invoice_index ON files(invoice);
            CREATE TABLE IF NOT EXISTS sources(id INTEGER PRIMARY KEY, file_id INTEGER, run TEXT, source TEXT, status TEXT);
        ''')

    def managed(self, path):
        resolved = Path(path).resolve()
        return resolved.parent == self.output.resolve() and not Path(path).is_symlink()

    def valid(self, row):
        _, digest, _, path = row
        try:
            return self.managed(path) and Path(path).is_file() and hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
        except OSError:
            return False

    def save(self, data, name, source, run):
        if len(data) > MAX_FILE or not data.lstrip().startswith(b'%PDF-'):
            raise ValueError('PDF 格式无效或超过 100 MB')
        digest = hashlib.sha256(data).hexdigest()
        key = invoice_key(data)
        rows = self.db.execute('SELECT id,hash,invoice,path FROM files WHERE hash=? OR (invoice IS NOT NULL AND invoice=?)', (digest, key)).fetchall()
        for row in rows:
            if self.valid(row):
                self.db.execute('INSERT INTO sources(file_id,run,source,status) VALUES(?,?,?,?)', (row[0], run, source, '重复'))
                self.db.commit()
                return '重复', row[3]
        filename = safe_name(name)
        if not filename.lower().endswith('.pdf'):
            filename += '.pdf'
        target = self.output / filename
        counter = 0
        while True:
            try:
                with target.open('xb') as stream:
                    stream.write(data)
                break
            except FileExistsError:
                counter += 1
                target = self.output / f'{Path(filename).stem}_{digest[:10]}_{counter}.pdf'
        try:
            cursor = self.db.execute('INSERT INTO files(hash,invoice,path) VALUES(?,?,?)', (digest, key, str(target)))
            self.db.execute('INSERT INTO sources(file_id,run,source,status) VALUES(?,?,?,?)', (cursor.lastrowid, run, source, '新增'))
            self.db.commit()
        except Exception:
            self.db.rollback()
            target.unlink(missing_ok=True)
            raise
        return '新增', str(target)

    def remove(self, path):
        row = self.db.execute('SELECT id,hash,invoice,path FROM files WHERE path=?', (path,)).fetchone()
        if not row or not self.valid(row):
            raise ValueError('文件不属于当前工具，已移动或内容已改变，不能删除')
        Path(path).unlink()

    def close(self):
        self.db.close()


def seven_path():
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
    candidates = [base / 'vendor' / '7z.exe', Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / '7-Zip' / '7z.exe']
    for path in candidates:
        if path.is_file():
            return str(path)
    raise RuntimeError('未找到完整 7-Zip，请安装官方 7-Zip 或使用包含组件的打包程序')


def command(args, cancel, timeout=60):
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        started = time.monotonic()
        try:
            while process.poll() is None:
                check_cancel(cancel)
                if time.monotonic() - started > timeout:
                    raise ValueError('压缩包处理超时')
                if output.tell() > 4 * 1024 * 1024:
                    raise ValueError('压缩包列表过大')
                time.sleep(.05)
            output.seek(0)
            text = output.read(4 * 1024 * 1024 + 1)
            if len(text) > 4 * 1024 * 1024:
                raise ValueError('压缩包列表过大')
            return process.returncode, text.decode('utf-8', errors='replace')
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


class Unpacker:
    def __init__(self, cancel, password):
        self.cancel, self.password = cancel, password
        self.total = 0

    def pdfs(self, data, name, depth=0):
        check_cancel(self.cancel)
        if len(data) > MAX_FILE:
            raise ValueError('附件超过 100 MB')
        suffix = Path(name).suffix.lower()
        if suffix == '.pdf':
            yield name, data
            return
        if suffix not in ARCHIVES:
            return
        if depth >= 4:
            raise ValueError('压缩包嵌套超过 4 层')
        executable = seven_path()
        with tempfile.TemporaryDirectory(prefix='invoice-') as temp:
            archive = Path(temp) / ('archive' + suffix)
            archive.write_bytes(data)
            password = ''
            for attempt in range(4):
                code, listing = command([executable, 'l', '-slt', '-ba', '-sccUTF-8', '-p' + password, str(archive)], self.cancel)
                if code == 0:
                    break
                if 'password' not in listing.lower() and 'encrypted' not in listing.lower():
                    raise ValueError('压缩包损坏或格式不支持')
                password = self.password(name)
                if password is None:
                    raise ValueError('已跳过密码压缩包')
            else:
                raise ValueError('压缩包密码错误')
            records = []
            for block in re.split(r'\r?\n\s*\r?\n', listing.strip()):
                record = dict(line.split(' = ', 1) for line in block.splitlines() if ' = ' in line)
                if 'Path' in record:
                    records.append(record)
            if len(records) > 1000:
                raise ValueError('压缩包超过 1000 项')
            for item in records:
                path = item['Path']
                win = PureWindowsPath(path)
                if win.is_absolute() or win.drive or '..' in win.parts or ':' in path or path.startswith(('/', '\\')):
                    raise ValueError('压缩包包含不安全路径')
                if any('link' in k.lower() for k in item) or 'L' in item.get('Attributes', ''):
                    raise ValueError('压缩包包含链接')
                size = int(item.get('Size', '0'))
                if size < 0 or size > MAX_FILE:
                    raise ValueError('解压文件超过 100 MB')
                self.total += size
                if self.total > MAX_TOTAL:
                    raise ValueError('本次累计解压超过 500 MB')
            if any(item.get('Encrypted') == '+' for item in records) and not password:
                password = self.password(name)
                if password is None:
                    raise ValueError('已跳过密码压缩包')
            # Stream selected entries separately: never let archive paths write to disk.
            for item in records:
                check_cancel(self.cancel)
                path = item['Path']
                if Path(path).suffix.lower() not in ARCHIVES | {'.pdf'} or item.get('Folder') == '+':
                    continue
                payload = self.extract(executable, archive, path, password)
                if len(payload) != int(item.get('Size', '0')):
                    raise ValueError('压缩包实际大小与清单不符')
                yield from self.pdfs(payload, path.replace('\\', '/'), depth + 1)

    def extract(self, executable, archive, entry, password):
        for attempt in range(3):
            with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
                process = subprocess.Popen([executable, 'x', '-so', '-spd', '-y', '-p' + password, str(archive), '--', entry],
                    stdin=subprocess.DEVNULL, stdout=output, stderr=errors, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                started = time.monotonic()
                try:
                    while process.poll() is None:
                        check_cancel(self.cancel)
                        if output.tell() > MAX_FILE or time.monotonic() - started > 60:
                            raise ValueError('解压超过文件大小限制或超时')
                        time.sleep(.05)
                    output.seek(0)
                    data = output.read(MAX_FILE + 1)
                    if len(data) > MAX_FILE:
                        raise ValueError('解压超过文件大小限制')
                    if process.returncode == 0:
                        if self.total > MAX_TOTAL:
                            raise ValueError('解压总量超限')
                        return data
                    errors.seek(0)
                    error = errors.read(8192).decode('utf-8', errors='replace').lower()
                    if 'password' not in error:
                        raise ValueError('附件解压失败')
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait()
            password = self.password(entry)
            if password is None:
                raise ValueError('已跳过密码压缩包')
        raise ValueError('压缩包密码错误')


def connect(account, secret):
    client = imaplib.IMAP4_SSL('imap.qq.com', 993, timeout=30)
    try:
        client.login(account, secret)
        status, _ = client.select('INBOX', readonly=True)
        if status != 'OK':
            raise ValueError('无法只读打开收件箱')
        return client
    except Exception:
        try:
            client.logout()
        except Exception:
            pass
        raise


@dataclass
class Query:
    account: str
    secret: str
    start: str
    end: str
    senders: str
    keywords: str
    directory: str


def run_query(query, cancel, emit, password):
    first, stop = date_bounds(query.start, query.end)
    store = Store(query.directory)
    client = None
    run = datetime.now(TZ).strftime('%Y%m%d_%H%M%S_%f')
    report = store.state / f'查询_{run}.csv'
    counts = {'新增': 0, '重复': 0, '失败': 0}
    state = '完成'
    columns = ['状态','邮箱','开始日期','结束日期','发件人条件','标题条件','邮件UID','收信时间','发件人','邮件标题','附件','保存位置','说明']
    with report.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(columns)
        writer.writerow(['查询条件', query.account, query.start, query.end, query.senders, query.keywords, '', '', '', '', '', '', ''])
        def record(status, context, attachment='', path='', explanation=''):
            counts[status] += 1
            row = [status, query.account, query.start, query.end, query.senders, query.keywords, *context, attachment, path, explanation]
            # Prevent spreadsheet formula execution in email-controlled CSV cells.
            writer.writerow(["'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@', '\t', '\r')) else v for v in row])
            stream.flush()
            emit('result', {'status': status, 'subject': context[3], 'attachment': attachment, 'path': path, 'detail': explanation})
        try:
            emit('progress', '正在连接 QQ 邮箱…')
            client = connect(query.account, query.secret)
            check_cancel(cancel)
            months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
            def imap_date(value):
                return f'{value.day:02d}-{months[value.month-1]}-{value.year}'
            # Broad server-side dates, then exact INTERNALDATE filtering in Beijing.
            status, result = client.uid('search', None, 'SINCE', imap_date((first - timedelta(days=1)).date()), 'BEFORE', imap_date((stop + timedelta(days=1)).date()))
            if status != 'OK':
                raise ValueError('邮件查询失败')
            uids = result[0].split() if result and result[0] else []
            unpacker = Unpacker(cancel, password)
            sender_terms, keyword_terms = terms(query.senders), terms(query.keywords)
            for index, uid in enumerate(uids):
                check_cancel(cancel)
                emit('progress', f'正在检查邮件 {index+1}/{len(uids)}')
                context = [uid.decode(), '', '', '']
                try:
                    status, response = client.uid('fetch', uid, '(INTERNALDATE RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])')
                    part = next((p for p in response if isinstance(p, tuple)), None)
                    if status != 'OK' or part is None:
                        raise ValueError('读取邮件头失败')
                    received = received_date(part[0])
                    if not first <= received < stop:
                        continue
                    header = BytesParser(policy=policy.default).parsebytes(part[1])
                    context = [uid.decode(), received.isoformat(), str(header.get('From', '')), str(header.get('Subject', ''))]
                    if not matches(header, sender_terms, keyword_terms):
                        continue
                    size = re.search(rb'RFC822.SIZE (\d+)', part[0])
                    if not size or int(size[1]) > 150 * 1024 * 1024:
                        raise ValueError('邮件大小未知或超过 150 MB')
                    check_cancel(cancel)
                    status, response = client.uid('fetch', uid, '(BODY.PEEK[])')
                    body = next((p[1] for p in response if isinstance(p, tuple)), None)
                    if status != 'OK' or body is None:
                        raise ValueError('下载邮件失败')
                    message = BytesParser(policy=policy.default).parsebytes(body)
                    for attachment in message.walk():
                        check_cancel(cancel)
                        if attachment.is_multipart():
                            continue
                        name = attachment.get_filename()
                        if not name and attachment.get_content_type() == 'application/pdf':
                            name = '附件.pdf'
                        if not name or Path(name).suffix.lower() not in ARCHIVES | {'.pdf'}:
                            continue
                        try:
                            payload = attachment.get_payload(decode=True)
                            if payload is None:
                                raise ValueError('附件为空')
                            for inner_name, data in unpacker.pdfs(payload, name):
                                check_cancel(cancel)
                                status, path = store.save(data, inner_name, '|'.join(context + [name, inner_name]), run)
                                record(status, context, name + (' / ' + inner_name if name != inner_name else ''), path)
                        except Cancelled:
                            raise
                        except Exception as error:
                            record('失败', context, name, explanation=safe_error(error))
                except Cancelled:
                    raise
                except Exception as error:
                    record('失败', context, explanation=safe_error(error))
                    if isinstance(error, (imaplib.IMAP4.abort, OSError)):
                        if client:
                            try:
                                client.logout()
                            except Exception:
                                pass
                        client = connect(query.account, query.secret)
        except Cancelled:
            state = '已取消'
        except Exception as error:
            state = '未完成'
            record('失败', ['', '', '', ''], explanation=safe_error(error))
        finally:
            if client:
                try:
                    client.logout()
                except Exception:
                    pass
            store.close()
    emit('done', {'state': state, 'counts': counts, 'report': str(report)})


def safe_error(error):
    if isinstance(error, imaplib.IMAP4.error):
        return '邮箱连接或认证失败，请检查 IMAP 服务及授权码，随后重新查询'
    if isinstance(error, OSError):
        return '网络或文件操作失败，请检查连接、目录权限及磁盘空间'
    if isinstance(error, ValueError) or isinstance(error, RuntimeError):
        return str(error)
    return '处理失败，请检查附件格式并重试'
