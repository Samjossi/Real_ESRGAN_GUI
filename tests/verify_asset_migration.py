"""asset 目录迁移后的图标加载冒烟验证（offscreen 平台，不跑真实超分）。"""
import os
import sys

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, os.path.join(os.path.abspath('.'), 'core'))

from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QApplication

import main as m

failures = []

def check(name, cond, detail=''):
    print(('PASS' if cond else 'FAIL'), name, detail)
    if not cond:
        failures.append(name)

# ---- 迁移后文件落位 ----
for f in (
    'asset/icons/icon-1024px.png',
    'asset/icons/icon-128px.png',
    'asset/icons/icon-256px.ico',
    'asset/icons/icon.icns',
    'asset/icons/icon.psd',
    'asset/packaging/Info.plist',
    'asset/packaging/build-macos-app.sh',
    'asset/packaging/macos-build-script.sh',
    'asset/packaging/realesrgan-gui.spec',
    'asset/packaging/realesrgan-gui-macos.spec',
):
    check(f'文件落位 {f}', os.path.exists(f))

# ---- 运行时图标加载 ----
qapp = QApplication(sys.argv)
config, models = m.init_config_and_model_paths()
app = m.REGUIApp(config, models)

pm = QPixmap(os.path.join(m.define.BASE_PATH, 'asset', 'icons', 'icon-128px.png'))
check('关于页/通知图标可加载（非空 pixmap）', not pm.isNull())
ic = QIcon(os.path.join(m.define.BASE_PATH, 'asset', 'icons', 'icon-256px.ico'))
check('窗口图标可加载（非空 icon）', not ic.isNull())
check('模型对比说明.md 仍在根目录', os.path.exists('模型对比说明.md'))

print()
if failures:
    print('FAILED:', failures)
    sys.exit(1)
print('ALL PASSED')
