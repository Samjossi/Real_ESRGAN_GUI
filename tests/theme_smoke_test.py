"""主题手动切换开关的冒烟测试（offscreen 平台，不跑真实处理）。"""
import configparser
import os
import sys

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, os.path.join(os.path.abspath('.'), 'core'))

from PySide6.QtWidgets import QApplication

import main as m

# 无 dbus 的 headless 环境，打补丁跳过桌面通知
class DummyNotification:
    def __init__(self, **kw): pass
    def send(self, block=False): pass
    title = ''
    message = ''
m.notifypy.Notify = DummyNotification

failures = []

def check(name, cond, detail=''):
    print(('PASS' if cond else 'FAIL'), name, detail)
    if not cond:
        failures.append(name)

qapp = QApplication(sys.argv)
config, models = m.init_config_and_model_paths()

# ---- 场景 1：Theme 缺失（默认配置）→ 浅色，复选框未勾选 ----
config['Config'].pop('Theme', None)
app = m.REGUIApp(config, models)
check('T1 缺失 Theme 默认未勾选', not app.checkDarkMode.isChecked())

# ---- 场景 2：Theme = Dark → 启动即勾选 ----
config['Config']['Theme'] = 'Dark'
app2 = m.REGUIApp(config, models)
check('T2 Theme=Dark 启动勾选', app2.checkDarkMode.isChecked())
app2.close()
app2.deleteLater()

# ---- 场景 3：Theme 为非法值 → fallback 浅色 ----
config['Config']['Theme'] = 'xxx'
app3 = m.REGUIApp(config, models)
check('T3 非法值 fallback 未勾选', not app3.checkDarkMode.isChecked())
app3.close()
app3.deleteLater()

# ---- 场景 4：勾选/取消勾选即时切换主题（spy applyTheme + 样式表变化）----
config['Config']['Theme'] = 'Light'
app4 = m.REGUIApp(config, models)
calls = []
origApplyTheme = m.REGUIApp.applyTheme
def spy(self, theme):
    calls.append(theme)
    origApplyTheme(self, theme)
m.REGUIApp.applyTheme = spy

# 构造器本身不应用主题（真实启动流程由 main() 调用 applyTheme），先手动应用一次浅色作为基准
origApplyTheme(app4, 'Light')
ssLight = qapp.styleSheet()
app4.checkDarkMode.setChecked(True)
QApplication.processEvents()
ssDark = qapp.styleSheet()
check('T4 勾选触发 applyTheme(Dark)', calls == ['Dark'], str(calls))
check('T4 深色样式表已应用', ssDark != ssLight and len(ssDark) > 0)
app4.checkDarkMode.setChecked(False)
QApplication.processEvents()
check('T4 取消勾选触发 applyTheme(Light)', calls == ['Dark', 'Light'], str(calls))
check('T4 样式表恢复浅色', qapp.styleSheet() == ssLight)
m.REGUIApp.applyTheme = origApplyTheme

# ---- 场景 5：saveConfig 持久化 Theme（写入临时路径，不动真实 config.ini）----
tmpConfigPath = os.path.abspath('tmp/test_theme_config.ini')
origPath = m.define.APP_CONFIG_PATH
m.define.APP_CONFIG_PATH = tmpConfigPath
try:
    app4.checkDarkMode.setChecked(True)
    app4.saveConfig()
    saved = configparser.ConfigParser()
    saved.read(tmpConfigPath, encoding='utf-8')
    check('T5 勾选后写入 theme = Dark', saved['Config'].get('theme') == 'Dark',
          str(saved['Config'].get('theme')))
    app4.checkDarkMode.setChecked(False)
    app4.saveConfig()
    saved = configparser.ConfigParser()
    saved.read(tmpConfigPath, encoding='utf-8')
    check('T5 取消后写入 theme = Light', saved['Config'].get('theme') == 'Light',
          str(saved['Config'].get('theme')))
finally:
    m.define.APP_CONFIG_PATH = origPath
    if os.path.exists(tmpConfigPath):
        os.remove(tmpConfigPath)

app4.close()
app4.deleteLater()

print()
if failures:
    print('FAILED:', failures)
    sys.exit(1)
print('ALL PASSED')
