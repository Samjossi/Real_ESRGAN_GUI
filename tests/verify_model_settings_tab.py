"""「模型设定」标签页与一键下载全部模型验证（离线直跑，秒级，产物落 tmp/verify_model_settings_tab/）。

覆盖：
1. download_models 批量下载：zip 缓存复用与清理、直链 + zip 成员混合
2. model_verified 跳过判定：完好 / 缺失 / 损坏
3. download_models 错误收集：单个失败不阻断其余
4. download_models 取消语义：中断剩余下载，不残留 .part
5. GUI 标签结构：「模型设定」在第 2 位，模型目录控件已从高级设定移入
6. GUI 一键下载：monkeypatch 清单为 file:// 夹具，走完整流程（跳过→下载→自动重扫）
7. 互斥：处理中/下载中目录控件与下载按钮禁用，进度控件仅下载中可见
"""
import hashlib
import os
import shutil
import sys
import threading
import time
import zipfile

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, os.path.join(os.path.abspath('.'), 'core'))

from PySide6.QtWidgets import QApplication

import define
import download
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

WORK = os.path.abspath('tmp/verify_model_settings_tab')
SRC = os.path.join(WORK, 'src')
DEST = os.path.join(WORK, 'dest')
shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(SRC)
os.makedirs(DEST)

# ---- 造夹具：一个直链模型 + 一个 zip 内模型（与 verify_model_download 同款） ----
def make_file(path: str, content: bytes) -> str:
    with open(path, 'wb') as f:
        f.write(content)
    return hashlib.sha256(content).hexdigest()

directHash = make_file(os.path.join(SRC, 'fake-x4.bin'), b'direct-bin' * 4096)
directParamHash = make_file(os.path.join(SRC, 'fake-x4.param'), b'direct-param')
zipPath = os.path.join(SRC, 'pkg.zip')
zipBin, zipParam = b'zip-bin' * 4096, b'zip-param'
with zipfile.ZipFile(zipPath, 'w') as z:
    z.writestr('models/zipmodel-x2.bin', zipBin)
    z.writestr('models/zipmodel-x2.param', zipParam)
zipBinHash = hashlib.sha256(zipBin).hexdigest()
zipParamHash = hashlib.sha256(zipParam).hexdigest()

entryDirect = {'name': 'fake-x4', 'files': (
    {'filename': 'fake-x4.bin', 'url': 'file://' + os.path.join(SRC, 'fake-x4.bin'), 'sha256': directHash},
    {'filename': 'fake-x4.param', 'url': 'file://' + os.path.join(SRC, 'fake-x4.param'), 'sha256': directParamHash},
)}
entryZip = {'name': 'zipmodel-x2', 'files': (
    {'filename': 'zipmodel-x2.bin', 'url': 'file://' + zipPath, 'zipMember': 'models/zipmodel-x2.bin', 'sha256': zipBinHash},
    {'filename': 'zipmodel-x2.param', 'url': 'file://' + zipPath, 'zipMember': 'models/zipmodel-x2.param', 'sha256': zipParamHash},
)}
# zip 来源拆成两个条目，验证同一 zip 在一次批量下载中只拉取一次
entryZip2 = {'name': 'zipmodel2-x3', 'files': (
    {'filename': 'zipmodel2-x3.bin', 'url': 'file://' + zipPath, 'zipMember': 'models/zipmodel-x2.bin', 'sha256': zipBinHash},
    {'filename': 'zipmodel2-x3.param', 'url': 'file://' + zipPath, 'zipMember': 'models/zipmodel-x2.param', 'sha256': zipParamHash},
)}
fakeManifest = (entryDirect, entryZip, entryZip2)

# ---- 1. download_models 批量：混合来源 + zip 缓存清理 ----
errors = download.download_models(fakeManifest, DEST)
check('T1 批量下载无错误', errors == [], str(errors))
check('T1b 全部产物落盘', all(os.path.exists(os.path.join(DEST, f)) for f in (
    'fake-x4.bin', 'fake-x4.param', 'zipmodel-x2.bin', 'zipmodel-x2.param', 'zipmodel2-x3.bin', 'zipmodel2-x3.param')))
check('T1c zip 缓存已清理', not any(x.endswith('.download') for x in os.listdir(DEST)), str(os.listdir(DEST)))

# ---- 2. model_verified 跳过判定 ----
check('T2 完好模型判定已安装', download.model_verified(entryDirect, DEST))
check('T2b 缺失判定未安装', not download.model_verified(
    {'name': 'ghost', 'files': ({'filename': 'ghost.bin', 'url': '', 'sha256': '0' * 64},)}, DEST))
corruptPath = os.path.join(DEST, 'fake-x4.param')
with open(corruptPath, 'ab') as f:
    f.write(b'corrupt')
check('T2c 损坏判定未安装', not download.model_verified(entryDirect, DEST))

# ---- 3. 错误收集：损坏的 fake-x4 触发校验失败，其余不受影响 ----
entryBad = {'name': 'bad', 'files': (
    {'filename': 'bad.bin', 'url': 'file://' + os.path.join(SRC, 'fake-x4.bin'), 'sha256': '0' * 64},
)}
errors = download.download_models((entryBad, entryZip), DEST)
check('T3 失败被收集且不阻断', len(errors) == 1 and 'bad.bin' in errors[0], str(errors))
check('T3b 失败文件已删除', not os.path.exists(os.path.join(DEST, 'bad.bin')))

# ---- 4. 取消语义：预设取消事件，直接中断且不残留 .part ----
ev = threading.Event()
ev.set()
DEST2 = os.path.join(WORK, 'dest2')
errors = download.download_models(fakeManifest, DEST2, None, ev)
check('T4 取消即中断', errors == [] and not os.listdir(DEST2), str(os.listdir(DEST2)))

# ---- 5. GUI 标签结构（offscreen） ----
qapp = QApplication(sys.argv)
config, models = m.init_config_and_model_paths()
app = m.REGUIApp(config, models)
check('T5 标签顺序', [app.notebookConfig.tabText(i) for i in range(app.notebookConfig.count())]
      == ['基本设定', '模型设定', '高级设定', '关于'])
check('T5b 模型目录控件已移入模型设定页',
      app.labelModelDir.parentWidget() is app.frameModelConfig
      and app.entryModelDir.parentWidget() is app.frameModelConfig)
check('T5c 状态行初始文本', app.labelModelStatus.text()
      == f'已安装 {sum(1 for e in download.MODEL_MANIFEST if e["name"] in models)} / {len(download.MODEL_MANIFEST)} 个模型',
      app.labelModelStatus.text())
# offscreen 下主窗口未 show()，isVisible() 恒为 False，改用 isHidden() 判断显式可见性
check('T5d 进度控件平时隐藏',
      app.progressBarDownload.isHidden() and app.buttonCancelDownload.isHidden())

# ---- 6. GUI 一键下载完整流程（monkeypatch 清单为本地夹具） ----
realManifest = download.MODEL_MANIFEST
DEST3 = os.path.join(WORK, 'dest3')
try:
    download.MODEL_MANIFEST = fakeManifest
    config['Config']['ModelDir'] = DEST3
    app.rescanModels()
    check('T6 空目录状态行', app.labelModelStatus.text() == f'已安装 0 / {len(fakeManifest)} 个模型',
          app.labelModelStatus.text())

    app.buttonDownloadAllModels_click()
    deadline = time.time() + 30
    while app.downloadingModels and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.02)
    qapp.processEvents()
    check('T6b 一键下载完成', not app.downloadingModels and app.labelDownloadStatus.text() == '全部模型下载完成。',
          app.labelDownloadStatus.text())
    check('T6c 产物落盘且自动重扫进下拉框',
          sorted(app.comboModel.itemText(i) for i in range(app.comboModel.count()))
          == ['fake-x4', 'zipmodel-x2', 'zipmodel2-x3'])
    check('T6d 状态行刷新', app.labelModelStatus.text() == f'已安装 3 / {len(fakeManifest)} 个模型',
          app.labelModelStatus.text())

    # 再点一次：全部跳过，秒完
    app.buttonDownloadAllModels_click()
    deadline = time.time() + 30
    while app.downloadingModels and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.02)
    qapp.processEvents()
    check('T6e 重复点击全部跳过', app.labelDownloadStatus.text() == '全部模型已安装，无需下载。',
          app.labelDownloadStatus.text())
finally:
    download.MODEL_MANIFEST = realManifest
    config['Config']['ModelDir'] = ''

# ---- 7. 互斥：处理中/下载中禁用 ----
app.processing = True
app.updateProcessButton()
check('T7 处理中禁用下载与目录控件',
      not app.buttonDownloadAllModels.isEnabled() and not app.buttonBrowseModelDir.isEnabled()
      and not app.buttonRescanModels.isEnabled())
app.processing = False
app.setModelDownloading(True)
check('T7b 下载中禁用目录控件与下载按钮',
      not app.buttonDownloadAllModels.isEnabled() and not app.buttonBrowseModelDir.isEnabled())
check('T7c 下载中显示进度条与取消按钮',
      not app.progressBarDownload.isHidden() and not app.buttonCancelDownload.isHidden()
      and app.buttonCancelDownload.isEnabled())
app.setModelDownloading(False)
check('T7d 结束后恢复',
      app.buttonDownloadAllModels.isEnabled() and app.buttonBrowseModelDir.isEnabled()
      and app.progressBarDownload.isHidden())

print()
if failures:
    print('失败项：', failures)
    sys.exit(1)
print(f'全部通过（{7} 组）。')
