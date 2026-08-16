"""退出链路治理的端到端验证（offscreen 平台，真实跑 realesrgan-ncnn-vulkan）。

对应计划：work plans/2026-0816-0931_GUI关闭与停止时引擎进程残留治理计划.md

覆盖：
- E1 中途停止：引擎子进程被立即终止（不等 stderr 新行），无孤儿进程、无临时文件残留
- E2 日志行缓冲：处理中途 output.log 已实时落盘（修前为 0 字节）
- E3 处理中关窗（确认）：引擎被回收、工作线程退出、日志正常关闭、无孤儿进程
- E4 处理中关窗（取消）：关窗被拦截，处理继续
"""
import glob
import os
import sys
import time

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, os.path.join(os.path.abspath('.'), 'core'))

from PIL import Image
from PySide6.QtWidgets import QApplication
from PySide6.QtWidgets import QMessageBox

import main as m

# 无 dbus 的 headless 环境，打补丁跳过桌面通知
class DummyNotification:
    def __init__(self, **kw): pass
    def send(self, block=False): pass
    title = ''
    message = ''
m.notifypy.Notify = DummyNotification

# offscreen 下 QMessageBox 无人可点会阻塞，question 改为可配置的应答记录
questionAnswer = [QMessageBox.StandardButton.Yes]
questionTexts = []
m.QMessageBox.question = lambda *a, **kw: (questionTexts.append(a[2] if len(a) > 2 else ''), questionAnswer[0])[1]
m.QMessageBox.warning = lambda *a, **kw: None

TMP = os.path.abspath('tmp')
failures = []

def check(name, cond, detail=''):
    print(('PASS' if cond else 'FAIL'), name, detail)
    if not cond:
        failures.append(name)

def pumpUntil(cond, timeout=60, interval=0.05):
    """泵事件循环直到 cond() 为真或超时，返回是否等到。"""
    t0 = time.time()
    while not cond() and time.time() - t0 < timeout:
        QApplication.processEvents()
        time.sleep(interval)
    QApplication.processEvents()
    return cond()

def enginePid(app):
    """当前任务的引擎子进程 pid（未启动则为 None）。"""
    p = getattr(app.currentTask, 'process', None)
    return p.pid if p is not None and p.poll() is None else None

def pidAlive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False

def tmpSnapshot():
    return set(glob.glob('/tmp/tmp*'))

# ---- 造测试图：512x512 噪声图，llvmpipe 软渲染足够慢，保证停止/关窗窗口期 ----
os.makedirs(TMP, exist_ok=True)
Image.effect_noise((512, 512), 64).convert('RGB').save(f'{TMP}/big.png')

qapp = QApplication(sys.argv)
config, models = m.init_config_and_model_paths()
app = m.REGUIApp(config, models)
app.spinResizeRatio.setValue(4)
app.checkUseWebP.setChecked(False)
app.checkLossyMode.setChecked(False)
app.entryCustomCommand.setText('')

# ============ E1：中途停止——引擎立即终止、无孤儿、无残留 ============
app.setInputPath((f'{TMP}/big.png',))
snap = tmpSnapshot()
app.buttonProcess_click()
check('E1 开始处理', app.processing)
check('E1 引擎子进程已启动', pumpUntil(lambda: enginePid(app) is not None, 120))
pid = enginePid(app)
app.buttonStop_click()
t0 = time.time()
dead = pumpUntil(lambda: not pidAlive(pid), 15)
stopLatency = time.time() - t0
check('E1 停止后引擎进程被终止', dead, f'pid={pid} 耗时 {stopLatency:.1f}s')
check('E1 终止延迟 < 10s（不等 stderr 新行）', stopLatency < 10, f'{stopLatency:.1f}s')
pumpUntil(lambda: not app.processing, 60)
check('E1 处理已结束（主动停止不报错）', not app.processing)
check('E1 currentTask 已清空', app.currentTask is None)
leftover = tmpSnapshot() - snap
check('E1 无临时文件残留', not leftover, str(leftover))

# ============ E2：日志行缓冲——处理中途 output.log 实时落盘 ============
app.setInputPath((f'{TMP}/big.png',))
app.buttonProcess_click()
check('E2 开始处理', pumpUntil(lambda: enginePid(app) is not None, 120))
logSize = os.path.getsize(app.logPath) if os.path.exists(app.logPath) else 0
check('E2 处理中途 output.log 已有内容（行缓冲）', logSize > 0, f'{logSize} bytes')
app.buttonStop_click()
pumpUntil(lambda: not app.processing, 60)

# ============ E4：处理中关窗（取消）——关窗被拦截 ============
app.setInputPath((f'{TMP}/big.png',))
app.buttonProcess_click()
check('E4 开始处理', pumpUntil(lambda: enginePid(app) is not None, 120))
questionAnswer[0] = QMessageBox.StandardButton.No
app.close()
check('E4 关窗弹出确认框', bool(questionTexts), str(questionTexts[-1] if questionTexts else ''))
check('E4 取消后处理仍在继续', app.processing)
check('E4 取消后引擎进程仍存活', enginePid(app) is not None)

# ============ E3：处理中关窗（确认）——完整停机 ============
pid = enginePid(app)
questionAnswer[0] = QMessageBox.StandardButton.Yes
app.close()
check('E3 确认后引擎进程被回收', pumpUntil(lambda: not pidAlive(pid), 15), f'pid={pid}')
check('E3 工作线程已退出', app.workerThread is None or not app.workerThread.is_alive())
check('E3 日志文件已关闭置空', app.logFile is None)
QApplication.processEvents()
time.sleep(0.3)
QApplication.processEvents()
leftover = tmpSnapshot() - snap
check('E3 无临时文件残留', not leftover, str(leftover))

print()
if failures:
    print('FAILED:', failures)
    sys.exit(1)
print('ALL PASSED')
