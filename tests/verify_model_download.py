"""模型目录自选与模型下载功能验证（离线直跑，秒级，产物落 tmp/verify_model_download/）。

覆盖：
1. 直链下载（file://）+ SHA-256 校验通过
2. 篡改哈希 → DownloadError 且文件被删除
3. zip 成员提取 + 同一次批量下载中同 URL 缓存复用
4. 取消语义（取消后不残留 .part）
5. scan_models 识别下载产物（.bin/.param 成对）
6. ModelDir 失效回退（临时改写 config.ini，finally 恢复）
7. ModelDownloadDialog offscreen 实例化、重扫刷新下拉框
"""
import configparser
import hashlib
import os
import shutil
import sys
import threading
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

WORK = os.path.abspath('tmp/verify_model_download')
SRC = os.path.join(WORK, 'src')
DEST = os.path.join(WORK, 'dest')
shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(SRC)
os.makedirs(DEST)

def make_file(path: str, content: bytes) -> str:
    with open(path, 'wb') as f:
        f.write(content)
    return hashlib.sha256(content).hexdigest()

# ---- 造夹具：一个直链文件 + 一个内含模型的 zip ----
directHash = make_file(os.path.join(SRC, 'fake-x4.bin'), b'direct-bin' * 4096)
directParamHash = make_file(os.path.join(SRC, 'fake-x4.param'), b'direct-param')
zipPath = os.path.join(SRC, 'pkg.zip')
zipBin, zipParam = b'zip-bin' * 4096, b'zip-param'
with zipfile.ZipFile(zipPath, 'w') as z:
    z.writestr('models/zipmodel-x2.bin', zipBin)
    z.writestr('models/zipmodel-x2.param', zipParam)
zipBinHash = hashlib.sha256(zipBin).hexdigest()
zipParamHash = hashlib.sha256(zipParam).hexdigest()
directUrl = 'file://' + os.path.join(SRC, 'fake-x4.bin')
zipUrl = 'file://' + zipPath

# ---- 1. 直链下载 + 校验通过 ----
entryDirect = {'name': 'fake-x4', 'files': (
    {'filename': 'fake-x4.bin', 'url': directUrl, 'sha256': directHash},
    {'filename': 'fake-x4.param', 'url': 'file://' + os.path.join(SRC, 'fake-x4.param'), 'sha256': directParamHash},
)}
progressSeen = []
download.download_model(entryDirect, DEST, lambda n, d, t: progressSeen.append((n, d, t)))
check('T1 直链下载+校验通过',
      os.path.exists(os.path.join(DEST, 'fake-x4.bin')) and os.path.exists(os.path.join(DEST, 'fake-x4.param')))
check('T1b 进度回调被调用', len(progressSeen) > 0)

# ---- 2. 篡改哈希 → 报错并删除 ----
entryBad = {'name': 'fake-x4', 'files': (
    {'filename': 'fake-bad.bin', 'url': directUrl, 'sha256': '0' * 64},
)}
try:
    download.download_model(entryBad, DEST)
    check('T2 篡改哈希报错', False, '未抛 DownloadError')
except download.DownloadError:
    check('T2 篡改哈希报错', not os.path.exists(os.path.join(DEST, 'fake-bad.bin')), '文件应被删除')

# ---- 3. zip 成员提取 + 缓存复用 ----
cache = {}
for _ in range(2):  # 两个条目共享同一 zip URL，第二次应命中缓存
    entryZip = {'name': 'zipmodel-x2', 'files': (
        {'filename': 'zipmodel-x2.bin', 'url': zipUrl, 'zipMember': 'models/zipmodel-x2.bin', 'sha256': zipBinHash},
        {'filename': 'zipmodel-x2.param', 'url': zipUrl, 'zipMember': 'models/zipmodel-x2.param', 'sha256': zipParamHash},
    )}
    download.download_model(entryZip, DEST, None, None, cache)
check('T3 zip 成员提取通过', os.path.exists(os.path.join(DEST, 'zipmodel-x2.bin')))
check('T3b 同 URL 缓存复用', len(cache) == 1)

# ---- 4. 取消语义 ----
ev = threading.Event()
ev.set()
try:
    download.download_file(directUrl, os.path.join(DEST, 'cancelled.bin'), None, ev)
    check('T4 取消语义', False, '未抛 DownloadCancelled')
except download.DownloadCancelled:
    check('T4 取消语义', not os.path.exists(os.path.join(DEST, 'cancelled.bin.part')), '不应残留 .part')

# ---- 5. scan_models 识别下载产物 ----
check('T5 scan_models 识别产物', sorted(m.scan_models(DEST)) == ['fake-x4', 'zipmodel-x2'])
check('T5b 不存在目录返回空', m.scan_models(os.path.join(WORK, 'nonexistent')) == [])

# ---- 6. ModelDir 失效回退（临时改写 config.ini，finally 恢复） ----
configBak = os.path.join(WORK, 'config.ini.bak')
shutil.copy(define.APP_CONFIG_PATH, configBak)
try:
    with open(define.APP_CONFIG_PATH, 'w', encoding='utf-8') as f:
        f.write('[Config]\nmodeldir = /nonexistent/dir/不存在\n')
    config, models = m.init_config_and_model_paths()
    check('T6 失效 ModelDir 回退默认并清空', config['Config'].get('ModelDir') == '' and len(models) > 0,
          f'models={len(models)}')
finally:
    shutil.copy(configBak, define.APP_CONFIG_PATH)

# ---- 7. GUI：下载对话框与重扫（offscreen） ----
qapp = QApplication(sys.argv)
config, models = m.init_config_and_model_paths()
dlg = m.ModelDownloadDialog(config)
check('T7 对话框勾选项与清单一致', len(dlg.checks) == len(download.MODEL_MANIFEST))
check('T7b 对话框默认目录', dlg.entryDir.text() == os.path.join(define.APP_PATH, 'models'))

app = m.REGUIApp(config, models)
config['Config']['ModelDir'] = DEST
app.rescanModels()
check('T7c 重扫刷新下拉框', sorted(app.comboModel.itemText(i) for i in range(app.comboModel.count())) == ['fake-x4', 'zipmodel-x2'])
check('T7d 倍率表解析', app.modelFactors.get('fake-x4') == 4 and app.modelFactors.get('zipmodel-x2') == 2)
config['Config']['ModelDir'] = ''

appEmpty = m.REGUIApp(config, [])
check('T7e 空模型主窗口不崩', appEmpty.comboModel.count() == 0)

print()
if failures:
    print('失败项：', failures)
    sys.exit(1)
print(f'全部通过（{7} 组）。')
