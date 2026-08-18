import os
import sys
from PyInstaller.utils.hooks import collect_data_files

# Windows 上 UPX 压缩的 exe 被杀毒软件误报率高，默认关闭；
# 确需压缩时设环境变量 REGUI_UPX=1 显式开启
USE_UPX = bool(os.environ.get('REGUI_UPX'))

a = Analysis(
    ['core/main.py'],
    datas=[
        ('asset/icons/icon-256px.ico', '.'),
        ('asset/icons/icon-128px.png', '.'),
        # 「关于」页「模型选择指南」按钮展示的说明文档
        ('模型对比说明.md', '.'),
        # macOS下通过app实现通知，打包时需要附带
        *collect_data_files('notifypy'),
        # qdarktheme 的 QSS/图标等资源
        *collect_data_files('qdarktheme'),
    ],
    excludes=[
        '_asyncio',
        '_bz2',
        '_decimal',
        '_hashlib',
        '_lzma',
        *(['_multiprocessing'] if sys.platform == 'win32' else []),
        '_queue',
        '_ssl',
        'tkinter',
        'unicodedata',
    ],
)

# Windows 10+已经自带UCRT了，打包时不需要附带
a.binaries = [
    x
    for x in a.binaries
    if not any(x[0].startswith(y) for y in {
        'api-ms-win-',
        'ucrtbase.dll',
    })
]

print('Binaries:')
for i in a.binaries:
    print(i)

print('Datas:')
for i in a.datas:
    print(i)

pyz = PYZ(a.pure, a.zipped_data)

if os.environ.get('REGUI_ONEFILE'):
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        name='realesrgan-gui',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=USE_UPX,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        icon='asset/icons/icon-256px.ico',
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name='realesrgan-gui',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=USE_UPX,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        icon='asset/icons/icon-256px.ico',
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        name='realesrgan-gui',
        strip=False,
        upx=USE_UPX,
    )