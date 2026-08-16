"""模型选择指南功能的 offscreen 验证：按钮存在、Markdown 渲染、深浅主题可读性、文件缺失兜底。"""
import os
import sys

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, os.path.join(os.path.abspath('.'), 'core'))

from PySide6.QtWidgets import QApplication

import main as m

failures = []

def check(name, cond, detail=''):
    print(('PASS' if cond else 'FAIL'), name, detail)
    if not cond:
        failures.append(name)

qapp = QApplication(sys.argv)
config, models = m.init_config_and_model_paths()
app = m.REGUIApp(config, models)

check('按钮存在且文字为中文', app.buttonModelGuide.text() == '模型选择指南', app.buttonModelGuide.text())

# 捕获对话框但不阻塞事件循环
captured = {}
RealDialog = m.QDialog
class SpyDialog(RealDialog):
    def exec(self):
        captured['dialog'] = self

def openDialog():
    m.QDialog = SpyDialog
    try:
        app.buttonModelGuide_click()
    finally:
        m.QDialog = RealDialog
    return captured.pop('dialog')

def browserOf(dlg):
    return dlg.findChild(m.QTextBrowser)

def bgLuminance(browser):
    img = browser.grab().toImage()
    c = img.pixelColor(img.width() // 2, img.height() // 2)
    return 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()

# ---- 正常渲染 ----
dlg = openDialog()
browser = browserOf(dlg)
html = browser.toHtml()
check('Markdown 标题渲染', '<h1' in html.lower())
check('Markdown 表格渲染', '<table' in html.lower(), '')
check('文档内容完整', 'realesrgan-x4plus-anime' in browser.toPlainText())
check('对话框尺寸约为主窗口 80%', abs(dlg.width() - app.width() * 0.8) < 2 and abs(dlg.height() - app.height() * 0.8) < 2,
      f'{dlg.width()}x{dlg.height()} vs {app.width()}x{app.height()}')
check('有关闭按钮', bool(dlg.findChild(m.QPushButton)))

# ---- 主题可读性（浅色应亮、深色应暗） ----
app.applyTheme('Light')
light = bgLuminance(browserOf(openDialog()))
app.applyTheme('Dark')
dark = bgLuminance(browserOf(openDialog()))
check('浅色主题底色为亮色', light > 128, f'亮度 {light:.0f}')
check('深色主题底色为暗色', dark < 128, f'亮度 {dark:.0f}')

# ---- 文件缺失兜底 ----
origBase = m.define.BASE_PATH
m.define.BASE_PATH = '/nonexistent-path'
try:
    browser = browserOf(openDialog())
    check('文件缺失显示兜底提示', '未找到模型对比说明文件' in browser.toPlainText(), browser.toPlainText()[:30])
finally:
    m.define.BASE_PATH = origBase

print('ALL PASSED' if not failures else f'FAILED: {failures}')
sys.exit(1 if failures else 0)
