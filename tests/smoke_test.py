"""输出路径选择逻辑修复的端到端冒烟测试（offscreen 平台，真实跑 realesrgan-ncnn-vulkan）。

覆盖：
- 选图后输出框自动填输入文件所在目录；改参数后输出框（目录）不变
- 拖放已禁用；输出为空/非目录时开始会弹提示且不启动任务
- 单文件、多文件、GIF、暂停/继续、中途停止、暂停中停止
- 无损 WebP 与有损模式的输出扩展名规则
- 命令行传图（相对路径）回归
"""
import glob
import os
import re
import sys
import time

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, os.path.join(os.path.abspath('.'), 'core'))

from PIL import Image
from PIL import ImageStat
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

import main as m

# 无 dbus 的 headless 环境，打补丁跳过桌面通知
class DummyNotification:
    def __init__(self, **kw): pass
    def send(self, block=False): pass
    title = ''
    message = ''
m.notifypy.Notify = DummyNotification

# offscreen 下 QMessageBox 无人可点会阻塞，改为记录弹窗文本
warnings = []
m.QMessageBox.warning = lambda *a, **kw: warnings.append(a[-1])

TMP = os.path.abspath('tmp')
failures = []

def check(name, cond, detail=''):
    print(('PASS' if cond else 'FAIL'), name, detail)
    if not cond:
        failures.append(name)

def pump(app, timeout=600, after=None):
    """泵事件循环直到处理结束；after 为 (延迟秒, 回调)，用于中途暂停/停止。"""
    t0 = time.time()
    fired = False
    while app.processing and time.time() - t0 < timeout:
        QApplication.processEvents()
        if after and not fired and time.time() - t0 >= after[0]:
            fired = True
            after[1]()
        time.sleep(0.05)
    QApplication.processEvents()
    time.sleep(0.3)
    QApplication.processEvents()

def rowStates(app):
    return [app.treeFiles.topLevelItem(i).data(1, Qt.ItemDataRole.UserRole) for i in range(app.treeFiles.topLevelItemCount())]

def rowProgress(app):
    return [app.treeFiles.itemWidget(app.treeFiles.topLevelItem(i), 2).value() for i in range(app.treeFiles.topLevelItemCount())]

def tmpSnapshot():
    return set(glob.glob('/tmp/tmp*'))

def pixelNotBlack(path):
    """像素级断言：输出不得为纯黑/近纯黑（防「退出码 0 但内容全零」盲区，见纯黑缺陷修复计划）。"""
    with Image.open(path) as img:
        g = img.convert('L')
        st = ImageStat.Stat(g)
        hist = g.histogram()
        total = g.size[0] * g.size[1]
        return st.mean[0] > 5 and sum(hist[1:]) / total > 0.01

# ---- 造测试图片 ----
os.makedirs(TMP, exist_ok=True)
Image.new('RGB', (64, 64), (200, 30, 30)).save(f'{TMP}/a.png')
Image.new('RGB', (64, 64), (30, 30, 200)).save(f'{TMP}/b.png')
frames = [Image.new('RGB', (48, 48), (c, 100, 255 - c)) for c in (0, 80, 160)]
frames[0].save(f'{TMP}/c.gif', save_all=True, duration=100, loop=0, append_images=frames[1:])
# 大图用于停止测试（llvmpipe 软渲染较慢，保证有窗口期）
big = Image.effect_noise((512, 512), 64).convert('RGB')
big.save(f'{TMP}/big.png')

qapp = QApplication(sys.argv)
config, models = m.init_config_and_model_paths()
app = m.REGUIApp(config, models)
# 固定测试参数，避免受 config.ini 里用户配置影响
app.spinResizeRatio.setValue(4)
app.checkUseWebP.setChecked(False)
app.checkLossyMode.setChecked(False)
app.entryCustomCommand.setText('')
model = app.comboModel.currentText()

# ============ 场景 0：交互语义与校验（不启动引擎） ============
app.setInputPath((f'{TMP}/a.png', f'{TMP}/b.png'))
check('S0 输出框自动填输入文件所在目录', app.entryOutputPath.text() == TMP, app.entryOutputPath.text())
app.spinResizeRatio.setValue(2)
check('S0 改倍率后输出框（目录）不变', app.entryOutputPath.text() == TMP, app.entryOutputPath.text())
app.spinResizeRatio.setValue(4)
check('S0 主窗口不再接受拖放', not app.acceptDrops())

app.entryOutputPath.setText('')
app.buttonProcess_click()
check('S0 输出为空时提示且不启动', warnings and not app.processing, str(warnings[-1] if warnings else ''))
app.entryOutputPath.setText(f'{TMP}/不存在的目录_xyz')
app.buttonProcess_click()
check('S0 输出非目录时提示且不启动', len(warnings) >= 2 and not app.processing, str(warnings[-1]))
check('S0 文件列表未残留行', app.treeFiles.topLevelItemCount() == 0)

# ============ 场景 1：正常批处理（2 PNG + 1 GIF），中途暂停/继续一次 ============
snap0 = tmpSnapshot()
app.setInputPath((f'{TMP}/a.png', f'{TMP}/b.png', f'{TMP}/c.gif'))
paused = [False]
def pauseResume():
    app.buttonProcess_click()  # 暂停
    paused[0] = app.processingPaused
    time.sleep(0.5)
    app.buttonProcess_click()  # 继续
app.buttonProcess_click()
check('S1 开始处理', app.processing)
pump(app, after=(1.0, pauseResume))
check('S1 暂停曾生效', paused[0])
outs = [f'{TMP}/a ({model} x4).png', f'{TMP}/b ({model} x4).png', f'{TMP}/c ({model} x4).gif']
check('S1 输出文件存在且命名正确', all(os.path.exists(o) for o in outs), str([o for o in outs if not os.path.exists(o)]))
check('S1 输出非纯黑（像素级）', all(os.path.exists(o) and pixelNotBlack(o) for o in outs))
check('S1 行状态全部 done', rowStates(app) == ['done'] * 3, str(rowStates(app)))
check('S1 行进度全部 100', rowProgress(app) == [100] * 3, str(rowProgress(app)))
gui_log = app.textOutput.toPlainText()
check('S1 GUI 日志无百分比刷屏', not re.search(r'\d+[.,]\d+%', gui_log))
with open(app.logPath, encoding='utf-8') as f:
    file_log = f.read()
check('S1 output.log 保留百分比行', re.search(r'\d+[.,]\d+%', file_log) is not None)
check('S1 GIF 输出为多帧', Image.open(outs[2]).n_frames == 3 if os.path.exists(outs[2]) else False)
leftover = tmpSnapshot() - snap0
check('S1 无临时文件残留', not leftover, str(leftover))

# ============ 场景 2：处理中途停止 ============
app.setInputPath((f'{TMP}/big.png',))
snap1 = tmpSnapshot()
app.buttonProcess_click()
check('S2 开始处理', app.processing)
check('S2 停止按钮可用', app.buttonStop.isEnabled())
def stop():
    app.buttonStop_click()
pump(app, after=(2.0, stop), timeout=300)
check('S2 处理已结束', not app.processing)
check('S2 行状态为已停止', rowStates(app) == ['stopped'], str(rowStates(app)))
check('S2 按钮复位为开始', app.buttonProcess.text() == '开始', app.buttonProcess.text())
check('S2 停止按钮禁用', not app.buttonStop.isEnabled())
check('S2 停止后日志有提示', '正在停止' in app.textOutput.toPlainText())
leftover = tmpSnapshot() - snap1
check('S2 无临时文件残留', not leftover, str(leftover))

# ============ 场景 3：批量停止（部分完成/等待中的行标记） ============
app.setInputPath((f'{TMP}/big.png', f'{TMP}/a.png', f'{TMP}/b.png'))
snap2 = tmpSnapshot()
app.buttonProcess_click()
pump(app, after=(2.0, lambda: app.buttonStop_click()), timeout=300)
states = rowStates(app)
check('S3 首行已停止', states[0] == 'stopped', str(states))
check('S3 后续行未处理', states[1:] == ['skipped', 'skipped'], str(states))
leftover = tmpSnapshot() - snap2
check('S3 无临时文件残留', not leftover, str(leftover))

# ============ 场景 4：暂停状态下停止 ============
app.setInputPath((f'{TMP}/big.png',))
snap3 = tmpSnapshot()
app.buttonProcess_click()
time.sleep(1.0)
QApplication.processEvents()
app.buttonProcess_click()  # 暂停
check('S4 已暂停', app.processingPaused)
app.buttonStop_click()     # 暂停中点停止
pump(app, timeout=60)
check('S4 暂停中停止生效', not app.processing)
check('S4 行状态为已停止', rowStates(app) == ['stopped'], str(rowStates(app)))
leftover = tmpSnapshot() - snap3
check('S4 无临时文件残留', not leftover, str(leftover))

# ============ 场景 5：无损 WebP 输出扩展名 ============
app.checkUseWebP.setChecked(True)
app.setInputPath((f'{TMP}/a.png',))
app.buttonProcess_click()
pump(app, timeout=120)
webpOut = f'{TMP}/a ({model} x4).webp'
check('S5 无损 WebP 输出 .webp', os.path.exists(webpOut) and rowStates(app) == ['done'], str(rowStates(app)))
check('S5 WebP 输出非纯黑（像素级）', os.path.exists(webpOut) and pixelNotBlack(webpOut))

# ============ 场景 6：有损模式输出扩展名 ============
app.checkUseWebP.setChecked(False)
app.checkLossyMode.setChecked(True)
app.setInputPath((f'{TMP}/b.png',))
app.buttonProcess_click()
pump(app, timeout=120)
lossyOut = f'{TMP}/b ({model} x4).jpg'
check('S6 有损模式输出 .jpg', os.path.exists(lossyOut) and rowStates(app) == ['done'], str(rowStates(app)))
check('S6 JPG 输出非纯黑（像素级）', os.path.exists(lossyOut) and pixelNotBlack(lossyOut))
app.checkLossyMode.setChecked(False)

# ============ 场景 7：命令行传图（相对路径）回归 ============
app.setInputPath(('tmp/a.png',))
check('S7 相对路径输入，输出目录为绝对路径', app.entryOutputPath.text() == TMP, app.entryOutputPath.text())

print()
if failures:
    print('FAILED:', failures)
    sys.exit(1)
print('ALL PASSED')
