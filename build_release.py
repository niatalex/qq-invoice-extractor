"""Reproducible branding resources and a distribution containing program files only."""
import argparse
import importlib.metadata
from pathlib import Path
import shutil
import sys
import zipfile
from version import PRODUCT_NAME, DISPLAY_VERSION, AUTHOR, WINDOWS_VERSION, PACKAGE_NAME

ROOT = Path(__file__).resolve().parent


def prepare():
    from PyInstaller.utils.win32.versioninfo import (
        VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable,
        StringStruct, VarFileInfo, VarStruct,
    )
    numeric = '.'.join(map(str, WINDOWS_VERSION))
    fields = {
        'CompanyName': AUTHOR,
        'FileDescription': f'{PRODUCT_NAME} · {DISPLAY_VERSION}',
        'FileVersion': numeric,
        'InternalName': 'InvoiceExtractor',
        'OriginalFilename': 'InvoiceExtractor.exe',
        'ProductName': PRODUCT_NAME,
        'ProductVersion': numeric,
        'Comments': f'{DISPLAY_VERSION}｜作者：{AUTHOR}',
    }
    info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=WINDOWS_VERSION, prodvers=WINDOWS_VERSION,
                          mask=0x3F, flags=0, OS=0x40004, fileType=1, subtype=0, date=(0, 0)),
        kids=[StringFileInfo([StringTable('080404B0', [StringStruct(k, v) for k, v in fields.items()])]),
              VarFileInfo([VarStruct('Translation', [2052, 1200])])],
    )
    (ROOT / 'version_info.txt').write_text(str(info), encoding='utf-8')


def package():
    folder = ROOT / 'dist' / 'InvoiceExtractor'
    if not (folder / 'InvoiceExtractor.exe').is_file():
        raise RuntimeError('Executable is missing')
    license_dir = folder / 'licenses'
    license_dir.mkdir(exist_ok=True)
    distribution = importlib.metadata.distribution('pypdf')
    license_file = next(p for p in distribution.files if str(p).endswith('licenses/LICENSE'))
    shutil.copyfile(distribution.locate_file(license_file), license_dir / 'pypdf-LICENSE.txt')
    shutil.copyfile(Path(sys.base_prefix) / 'LICENSE.txt', license_dir / 'Python-LICENSE.txt')
    shutil.copyfile(ROOT / 'README.md', folder / '使用说明.md')
    (folder / '开始使用.txt').write_text(
        f'{PRODUCT_NAME} {DISPLAY_VERSION}\n作者：{AUTHOR}\n\n'
        '1. 保留本文件夹完整内容，双击 InvoiceExtractor.exe。\n'
        '2. 填写自己的 QQ 邮箱和邮箱授权码，点击测试连接。\n'
        '3. 选择日期、保存目录，按发件人或标题关键字提取。\n'
        '发件人可留空，标题填“发票”即可匹配。\n\n'
        '无需安装 Python 或 7-Zip。试用为版本名称，无期限或功能限制。\n'
        '适用于 Windows 10/11 64 位。授权码不要发给其他人。\n', encoding='utf-8-sig')
    files = sorted(p for p in folder.rglob('*') if p.is_file())
    forbidden = {'.invoice-extractor', 'settings.json', 'records.sqlite3'}
    for path in files:
        relative = path.relative_to(folder)
        if path.is_symlink() or any(part in forbidden for part in relative.parts) or path.suffix.lower() in {'.pdf', '.sqlite3', '.csv'}:
            raise RuntimeError(f'Unexpected user data in package: {relative}')
    output = ROOT / 'dist' / PACKAGE_NAME
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, Path('InvoiceExtractor') / path.relative_to(folder))
    print(f'Package created: {output}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--package', action='store_true')
    options = parser.parse_args()
    if options.prepare:
        prepare()
    if options.package:
        package()
