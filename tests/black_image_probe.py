#!/usr/bin/env python3
# tests/black_image_probe.py
# 纯黑输出缺陷视觉验证探针（修复前后同脚本复测）
# 用法：uv run tests/black_image_probe.py [--tag pre|post]
# 产物：tmp/probe_input*.png、tmp/probe_L{0,1}_<tag>.png、tmp/probe_log_<tag>.txt
import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'core'))

import define
import param
import task
from PIL import Image, ImageStat

TMP = os.path.join(ROOT, 'tmp')
SIZE = 64
MODEL = 'realesrgan-x4plus'
MODEL_FACTOR = 4
SCALE = 4

parser = argparse.ArgumentParser()
parser.add_argument('--tag', default='pre', help='产物后缀（pre=修前，post=修后）')
args = parser.parse_args()
TAG = args.tag

logLines: list[str] = []


def log(msg: str) -> None:
    print(msg, end='' if msg.endswith('\n') else '\n')
    logLines.append(msg if msg.endswith('\n') else msg + '\n')


def makeTestImage(rgba: bool) -> str:
    """生成 64x64 确定性测试图：左 3/4 红->蓝渐变，右 1/4 灰阶条。"""
    img = Image.new('RGBA' if rgba else 'RGB', (SIZE, SIZE))
    px = img.load()
    for y in range(SIZE):
        for x in range(SIZE):
            if x < SIZE * 3 // 4:
                t = x / (SIZE - 1)
                r, g, b = round(255 * (1 - t)), round(255 * (y / (SIZE - 1))), round(255 * t)
            else:
                r = g = b = round(255 * (y / (SIZE - 1)))
            if rgba:
                a = round(255 * (y / (SIZE - 1)))  # alpha 随 y 渐变，非全 0 非全 255
                px[x, y] = (r, g, b, a)
            else:
                px[x, y] = (r, g, b)
    path = os.path.join(TMP, 'probe_input_rgba.png' if rgba else 'probe_input.png')
    img.save(path)
    return path


def pixelStats(path: str) -> dict:
    """像素扫描断言：mean / stddev / 非零像素占比（转灰度后统计）。"""
    with Image.open(path) as img:
        g = img.convert('L')
        st = ImageStat.Stat(g)
        hist = g.histogram()
        total = g.size[0] * g.size[1]
        nonzero = sum(hist[1:]) / total
    return {'size': g.size, 'mean': st.mean[0], 'stddev': st.stddev[0], 'nonzero': nonzero}


def judge(stats: dict) -> str:
    if stats['mean'] < 5 and stats['nonzero'] < 0.01:
        return '纯黑 🔴'
    if stats['stddev'] < 1:
        return '纯色/近纯色（可疑）🟡'
    return '正常 🟢'


def report(label: str, path: str) -> str:
    if not os.path.exists(path):
        verdict = '文件缺失 🔴'
        log(f'[{label}] {path} -> {verdict}')
        return verdict
    stats = pixelStats(path)
    verdict = judge(stats)
    log(f'[{label}] {path}\n  size={stats["size"]} mean={stats["mean"]:.2f} stddev={stats["stddev"]:.2f} nonzero={stats["nonzero"]:.2%} -> {verdict}')
    return verdict


def runEngine(inputPath: str, outputPath: str) -> int:
    """L0：裸跑引擎，命令与 core/task.py 的 realesrgan 分支逐字一致（含不带 -m）。"""
    cmd = (
        define.RE_PATH,
        '-v',
        '-i', inputPath,
        '-o', outputPath,
        '-s', str(SCALE),
        '-t', '0',
        '-n', MODEL,
        '-g', 'auto',
    )
    log(f'[L0] cmd: {" ".join(cmd)}')
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    for line in (p.stderr or '').splitlines(keepends=True):
        log(f'[L0 stderr] {line}')
    if p.stdout:
        for line in p.stdout.splitlines(keepends=True):
            log(f'[L0 stdout] {line}')
    log(f'[L0] returncode={p.returncode}')
    return p.returncode


def makeConfig(scale: int = SCALE) -> param.REConfigParams:
    return param.REConfigParams(
        model=MODEL,
        modelFactor=MODEL_FACTOR,
        modelDir=os.path.join(ROOT, 'models'),
        resizeMode=param.ResizeMode.RATIO,
        resizeModeValue=scale,
        downsample=Image.Resampling.LANCZOS,
        tileSize=0,
        gpuID=-1,
        useTTA=False,
        preupscale=False,
        customCommand='',
    )


def runTaskChain(inputPath: str, outputPath: str, scale: int = SCALE) -> None:
    """L1：实例化生产类 RESpawnTask 走完整任务链路。"""
    progressValue = [0, 0, 1]
    t = task.RESpawnTask(log, progressValue, inputPath, outputPath, makeConfig(scale))
    t.run()


def runGIFChain(inputPath: str, outputPath: str) -> None:
    """L2：GIF 帧链路（SplitGIFTask + taskRunner 生产组合）。"""
    import collections
    import threading
    queue: collections.deque = collections.deque()
    progressValue = [0, 0, 1]
    queue.append(task.SplitGIFTask(
        log, progressValue, inputPath, outputPath, makeConfig(), queue,
        optimizeTransparency=False,
    ))
    pauseEvent = threading.Event()
    pauseEvent.set()
    cancelEvent = threading.Event()
    task.taskRunner(
        queue, pauseEvent, cancelEvent, log,
        completeCallback=lambda withError: log(f'[L2] complete withError={withError}'),
        failCallback=lambda ex, itemId: log(f'[L2] fail: {ex!r}'),
        finallyCallback=lambda: log('[L2] finally'),
        ignoreError=False,
    )


def main() -> int:
    log(f'=== black_image_probe tag={TAG} ===')

    # ---- 第 0 步：环境与路径验证（H0/H1） ----
    log(f'[step0] define.RE_PATH = {define.RE_PATH}')
    exists = os.path.exists(define.RE_PATH)
    executable = os.access(define.RE_PATH, os.X_OK)
    log(f'[step0] exists={exists} executable={executable}')
    if not exists or not executable:
        log('[step0] 🔴 引擎二进制缺失或不可执行，命中 H0，终止。')
        flushLog()
        return 2
    h = subprocess.run([define.RE_PATH, '-h'], capture_output=True, text=True, cwd=ROOT)
    log(f'[step0] -h returncode={h.returncode}, stderr 前 3 行:')
    for line in (h.stderr or '').splitlines()[:3]:
        log(f'  {line}')
    log(f'[step0] vulkaninfo: {shutil.which("vulkaninfo") or "未安装"}')
    ldd = subprocess.run(['ldd', define.RE_PATH], capture_output=True, text=True)
    vulkanLibs = [l.strip() for l in ldd.stdout.splitlines() if 'vulkan' in l.lower()]
    log(f'[step0] ldd 中 vulkan 相关: {vulkanLibs or "无（可能运行时 dlopen）"}')

    # ---- 生成测试图 ----
    inputRGB = makeTestImage(rgba=False)
    inputRGBA = makeTestImage(rgba=True)
    report('input RGB', inputRGB)
    report('input RGBA', inputRGBA)

    # ---- L0 裸引擎 ----
    verdicts = {}
    for suffix, src in (('', inputRGB), ('_rgba', inputRGBA)):
        out = os.path.join(TMP, f'probe_L0{suffix}_{TAG}.png')
        if os.path.exists(out):
            os.remove(out)
        try:
            runEngine(src, out)
        except Exception:
            import traceback
            log(traceback.format_exc())
        verdicts[f'L0{suffix}'] = report(f'L0{suffix}', out)

    # ---- L1 任务链路 ----
    for suffix, src in (('', inputRGB), ('_rgba', inputRGBA)):
        out = os.path.join(TMP, f'probe_L1{suffix}_{TAG}.png')
        if os.path.exists(out):
            os.remove(out)
        try:
            runTaskChain(src, out)
        except Exception:
            import traceback
            log(f'[L1{suffix}] 异常:\n{traceback.format_exc()}')
        verdicts[f'L1{suffix}'] = report(f'L1{suffix}', out)

    # ---- L2 GIF 帧链路（用 tmp/c.gif 真实样本） ----
    gifIn = os.path.join(TMP, 'c.gif')
    if os.path.exists(gifIn):
        out = os.path.join(TMP, f'probe_L2_gif_{TAG}.gif')
        if os.path.exists(out):
            os.remove(out)
        try:
            runGIFChain(gifIn, out)
        except Exception:
            import traceback
            log(f'[L2] 异常:\n{traceback.format_exc()}')
        verdicts['L2_gif'] = report('L2_gif', out)
    else:
        log('[L2] 跳过：tmp/c.gif 不存在')

    # ---- L3 多趟链路（8 倍 → 两个 scalePass，经中间临时文件） ----
    out = os.path.join(TMP, f'probe_L3_8x_{TAG}.png')
    if os.path.exists(out):
        os.remove(out)
    try:
        runTaskChain(inputRGB, out, scale=8)
    except Exception:
        import traceback
        log(f'[L3] 异常:\n{traceback.format_exc()}')
    verdicts['L3_8x'] = report('L3_8x', out)

    # ---- L4 有损输出变体（jpg / webp 扩展名直出） ----
    for ext in ('jpg', 'webp'):
        out = os.path.join(TMP, f'probe_L4_{ext}_{TAG}.{ext}')
        if os.path.exists(out):
            os.remove(out)
        try:
            runTaskChain(inputRGB, out)
        except Exception:
            import traceback
            log(f'[L4_{ext}] 异常:\n{traceback.format_exc()}')
        verdicts[f'L4_{ext}'] = report(f'L4_{ext}', out)

    # ---- L5 大图输入（512x512 噪声图 big.png，2048x2048 输出，压 GPU） ----
    bigIn = os.path.join(TMP, 'big.png')
    if os.path.exists(bigIn):
        out = os.path.join(TMP, f'probe_L5_big_{TAG}.png')
        if os.path.exists(out):
            os.remove(out)
        try:
            runTaskChain(bigIn, out)
        except Exception:
            import traceback
            log(f'[L5] 异常:\n{traceback.format_exc()}')
        verdicts['L5_big'] = report('L5_big', out)
    else:
        log('[L5] 跳过：tmp/big.png 不存在')

    # ---- L6 用户真实样本（大图 + tileSize=0，复现/验证黑图缺陷的核心场景） ----
    userIn = os.path.join(TMP, '给你分析的图片', '生成清晰图片111.png')
    if os.path.exists(userIn):
        out = os.path.join(TMP, f'probe_L6_user_{TAG}.png')
        if os.path.exists(out):
            os.remove(out)
        try:
            runTaskChain(userIn, out)
        except Exception:
            import traceback
            log(f'[L6] 异常:\n{traceback.format_exc()}')
        verdicts['L6_user'] = report('L6_user', out)
    else:
        log('[L6] 跳过：用户样本不存在')

    # ---- 汇总 ----
    log('=== 汇总 ===')
    for k, v in verdicts.items():
        log(f'  {k}: {v}')
    flushLog()
    return 0


def flushLog() -> None:
    path = os.path.join(TMP, f'probe_log_{TAG}.txt')
    with open(path, 'w', encoding='utf-8') as f:
        f.writelines(logLines)
    print(f'[probe] 日志已存 {path}')


if __name__ == '__main__':
    sys.exit(main())
