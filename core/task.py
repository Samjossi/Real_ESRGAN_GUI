import collections
import subprocess
import io
import math
import os
import re
import shlex
import shutil
import tempfile
import time
import threading
import traceback
import typing
from PIL import Image
from PIL import ImageFilter
from PIL import ImageSequence

import define
import param

class TaskCancelled(Exception):
    """用户主动停止处理时抛出，不作为失败处理。"""
    pass

class AbstractTask:
    def __init__(
        self,
        outputCallback: typing.Callable[[str], None],
        itemId: int | None = None,
        progressCallback: typing.Callable[[int | None, float], None] | None = None,
        cancelEvent: threading.Event | None = None,
    ) -> None:
        self.outputCallback = outputCallback
        # 任务在 GUI 文件列表中对应的行号（None 表示不关联列表行）
        self.itemId = itemId
        # 进度上报回调 progressCallback(itemId, fraction)，fraction 为 0~1
        self.progressCallback = progressCallback
        # 置位表示用户要求停止处理
        self.cancelEvent = cancelEvent

    def reportProgress(self, fraction: float) -> None:
        if self.progressCallback is not None:
            self.progressCallback(self.itemId, fraction)

    def isCancelRequested(self) -> bool:
        return self.cancelEvent is not None and self.cancelEvent.is_set()

    def run(self) -> None:
        pass

    def cleanup(self) -> None:
        """停止处理时清理任务关联的临时文件（尽力而为，失败不阻断收尾）。"""
        pass

class RESpawnTask(AbstractTask):
    def __init__(
        self,
        outputCallback: typing.Callable[[str], None],
        progressValue: list[int | float],
        inputPath: str, outputPath: str,
        config: param.REConfigParams,
        removeInput: bool = False,
        itemId: int | None = None,
        progressCallback: typing.Callable[[int | None, float], None] | None = None,
        cancelEvent: threading.Event | None = None,
    ) -> None:
        super().__init__(outputCallback, itemId, progressCallback, cancelEvent)
        self.progressValue = progressValue
        self.inputPath = inputPath
        self.outputPath = outputPath
        self.config = config
        self.removeInput = removeInput
        self.process: subprocess.Popen = None

    def cleanup(self) -> None:
        if self.removeInput and os.path.exists(self.inputPath):
            os.remove(self.inputPath)

    def run(self) -> None:
        self.outputCallback(f'Using executable: {define.RE_PATH}\n')
        self.progressValue[0] = 0

        with Image.open(self.inputPath) as img:
            srcWidth, srcHeight = img.size
            srcRatio = srcWidth / srcHeight
            if img.mode == 'P':
                self.inputPath = tempfile.mktemp('.png')
                img.convert('RGBA').save(self.inputPath)
                self.removeInput = True
        resizeMode = self.config.resizeMode
        if (
            (resizeMode == param.ResizeMode.LONGEST_SIDE and srcWidth >= srcHeight)
            or (resizeMode == param.ResizeMode.SHORTEST_SIDE and srcWidth <= srcHeight)
        ):
            resizeMode = param.ResizeMode.WIDTH
        elif (
            (resizeMode == param.ResizeMode.LONGEST_SIDE and srcHeight >= srcWidth)
            or (resizeMode == param.ResizeMode.SHORTEST_SIDE and srcHeight <= srcWidth)
        ):
            resizeMode = param.ResizeMode.HEIGHT
        match resizeMode:
            case param.ResizeMode.RATIO:
                dstWidth = srcWidth * self.config.resizeModeValue
                dstHeight = srcHeight * self.config.resizeModeValue
            case param.ResizeMode.WIDTH:
                dstWidth = self.config.resizeModeValue
                dstHeight = round(dstWidth / srcRatio)
            case param.ResizeMode.HEIGHT:
                dstHeight = self.config.resizeModeValue
                dstWidth = round(dstHeight * srcRatio)
        inputPathPreupscaled: str = None
        if self.config.preupscale:
            match resizeMode:
                case param.ResizeMode.RATIO:
                    scaleRatio = self.config.resizeModeValue
                case param.ResizeMode.WIDTH:
                    scaleRatio = self.config.resizeModeValue / srcWidth
                case param.ResizeMode.HEIGHT:
                    scaleRatio = self.config.resizeModeValue / srcHeight
            frac, intg = math.modf(math.log(scaleRatio, self.config.modelFactor))
            preWidth = math.ceil(dstWidth / (self.config.modelFactor ** intg))
            preHeight = math.ceil(dstHeight / (self.config.modelFactor ** intg))
            if frac < .5 and (srcWidth != preWidth or srcHeight != preHeight):
                self.outputCallback(f'Pre-upscale from {srcWidth}x{srcHeight} to {preWidth}x{preHeight}.\n')
                inputPathPreupscaled = tempfile.mktemp('.webp' if os.path.splitext(self.inputPath)[1] == '.webp' else '.png')
                with Image.open(self.inputPath) as img:
                    resized = img.resize((preWidth, preHeight), Image.LANCZOS)
                    resized.save(inputPathPreupscaled, lossless=True)
                    resized.close()
                srcWidth, srcHeight = preWidth, preHeight
        scalePass = 0
        while srcWidth < dstWidth and srcHeight < dstHeight:
            scalePass += 1
            srcWidth *= self.config.modelFactor
            srcHeight *= self.config.modelFactor

        # input -> output
        # input -> temp0 -> output
        # input -> temp0 -> temp1 -> output
        outputExt = os.path.splitext(self.outputPath)[1]
        files = (inputPathPreupscaled or self.inputPath, *(tempfile.mktemp(outputExt) for _ in range(scalePass)))
        try:
            for i in range(len(files) - 1):
                inputPath, outputPath = files[i:(i + 2)]
                # tileSize 降级梯队：GPU 推理设备丢失（vkQueueSubmit failed 等 Vulkan 错误）时
                # 逐级缩小拆分尺寸重试，并兜底切换到引擎启动日志里识别到的 CPU 设备（llvmpipe/lavapipe）。
                # 引擎对 Vulkan 错误不做传播（照样退出码 0 报 done），只信退出码会把纯黑图当成功。
                tileSizeLadder = [self.config.tileSize] + [
                    t for t in (512, 256, 128, 64, 32)
                    if t != self.config.tileSize and (not self.config.tileSize or t < self.config.tileSize)
                ]
                cpuGpuID: int | None = None
                passSucceeded = False
                for tileSize in tileSizeLadder:
                    gpuIDCandidates = [self.config.gpuID]
                    if cpuGpuID is not None and cpuGpuID != self.config.gpuID:
                        gpuIDCandidates.append(cpuGpuID)
                    for gpuID in gpuIDCandidates:
                        alphaOverridePath = None
                        if os.path.splitext(os.path.split(define.RE_PATH)[1])[0] == 'realcugan-ncnn-vulkan':
                            model, modelFilename = self.config.model.split('#', 1)
                            denoiseLevel = {
                                'conservative': -1,
                                'no-denoise': 0,
                                **{f'denoise{i}x': i for i in range(1, 4)},
                            }[modelFilename.split('-', 1)[1]]
                            cmd = (
                                define.RE_PATH,
                                '-v',
                                '-i', inputPath,
                                '-o', outputPath,
                                '-s', str(self.config.modelFactor),
                                '-t', str(tileSize),
                                '-m', os.path.join(self.config.modelDir, model),
                                '-n', str(denoiseLevel),
                                '-g', 'auto' if gpuID < 0 else str(gpuID),
                                '-c', '1', # accurate sync
                                *(('-x', ) if self.config.useTTA else ()),
                            )
                        else:
                            cmd = (
                                define.RE_PATH,
                                '-v',
                                '-i', inputPath,
                                '-o', outputPath,
                                '-s', str(self.config.modelFactor),
                                *(('-z', str(self.config.modelFactor)) if os.path.splitext(os.path.split(define.RE_PATH)[1])[0] == 'upscayl-bin' else ()),
                                '-t', str(tileSize),
                                '-n', self.config.model,
                                '-g', 'auto' if gpuID < 0 else str(gpuID),
                                *(('-x', ) if self.config.useTTA else ()),
                            )
                        # 重试前把进度重置回本趟起点
                        self.progressValue[0] = i / (len(files) - 1)
                        self.reportProgress(self.progressValue[0])
                        vkFailed = False
                        with subprocess.Popen(
                            cmd,
                            stderr=subprocess.PIPE,
                            universal_newlines=True,
                            encoding='utf-8' if os.path.splitext(os.path.split(define.RE_PATH)[1])[0] == 'upscayl-bin' else None,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                        ) as p:
                            self.process = p
                            for line in p.stderr:
                                if self.isCancelRequested():
                                    p.terminate()
                                    try:
                                        p.wait(5)
                                    except subprocess.TimeoutExpired:
                                        p.kill()
                                    raise TaskCancelled()
                                # 如果输入文件是有alpha通道的图片，但是输出扩展名又是JPG
                                # Real-ESRGAN会强行给输出的文件名加上PNG的扩展名，导致后续处理找不到文件
                                # 这里额外加了一个重命名为原来的输出文件名的操作
                                # https://github.com/xinntao/Real-ESRGAN-ncnn-vulkan/blob/37026f49824c5cf84062e7c6a5dd71445dcf610f/src/main.cpp#L283
                                if m := re.search(r'^image .+? has alpha channel ! .+? will output (.+?)$', line, re.M):
                                    alphaOverridePath = m.group(1)
                                elif m := re.search(r'^\[(\d+)\s+(?:llvmpipe|lavapipe)\b', line, re.I):
                                    cpuGpuID = int(m.group(1))
                                elif re.search(r'vk\w+ failed', line):
                                    vkFailed = True
                                elif m := re.search(r'(\d+[.,]\d+)%', line):
                                    self.progressValue[0] = (i + float(m.group(1).replace(',', '.')) / 100) / (len(files) - 1)
                                    self.reportProgress(self.progressValue[0])
                                elif m := re.search(r'^.+? -> .+? done$', line, re.M):
                                    self.progressValue[0] = (i + 1) / (len(files) - 1)
                                    self.reportProgress(self.progressValue[0])
                                self.outputCallback(line)
                        self.process = None
                        # GUI 侧（停止按钮/关窗）可能已直接 terminate 引擎导致 stderr 读到 EOF，
                        # 循环内取消检查没机会触发——这里补判，避免把主动停止误报为失败
                        if self.isCancelRequested():
                            raise TaskCancelled()
                        if p.returncode == 0 and not vkFailed:
                            passSucceeded = True
                            break
                        if p.returncode != 0 and not vkFailed:
                            raise subprocess.CalledProcessError(p.returncode, cmd)
                        self.outputCallback(f'GPU inference failed (Vulkan error), retrying with next tileSize/gpuID...\n')
                    if passSucceeded:
                        break
                if not passSucceeded:
                    raise RuntimeError('Real-ESRGAN inference failed: GPU device lost, and all fallback attempts (smaller tileSize and CPU device) were exhausted')
                if i > 0 or inputPath == inputPathPreupscaled or self.removeInput:
                    os.remove(inputPath)
                if alphaOverridePath:
                    shutil.move(alphaOverridePath, outputPath)
                    self.outputCallback(f'Rename {alphaOverridePath} to {outputPath}\n')
        except TaskCancelled:
            # 停止处理时尽力清理半成品输出与临时文件，清理失败只写日志不阻断收尾
            for f in (*files[1:], inputPathPreupscaled, self.inputPath if self.removeInput else None):
                if f and os.path.exists(f):
                    try:
                        os.remove(f)
                    except OSError:
                        self.outputCallback(traceback.format_exc())
            raise

        os.makedirs(os.path.split(self.outputPath)[0], exist_ok=True)
        if srcWidth == dstWidth and srcHeight == dstHeight:
            if os.path.exists(self.outputPath):
                os.remove(self.outputPath)
            shutil.move(files[-1], self.outputPath)
        else:
            with Image.open(files[-1]) as img:
                self.outputCallback(f'Downsample from {img.size[0]}x{img.size[1]} to {dstWidth}x{dstHeight}.\n')
                resized = img.resize((dstWidth, dstHeight), self.config.downsample)
                resized.save(self.outputPath)
                resized.close()
            if scalePass:
                os.remove(files[-1])

        self.reportProgress(1)
        self.progressValue[0] = 0
        self.progressValue[1] += 1

class MergeGIFTask(AbstractTask):
    def __init__(
        self,
        outputCallback: typing.Callable[[str], None],
        outputPath: str,
        frames: tuple[str, ...],
        durations: tuple[int, ...],
        optimizeTransparency: bool,
        itemId: int | None = None,
        progressCallback: typing.Callable[[int | None, float], None] | None = None,
        cancelEvent: threading.Event | None = None,
    ) -> None:
        super().__init__(outputCallback, itemId, progressCallback, cancelEvent)
        self.outputPath = outputPath
        self.frames = frames
        self.durations = durations
        self.optimizeTransparency = optimizeTransparency

    def cleanup(self) -> None:
        for f in self.frames:
            if os.path.exists(f):
                os.remove(f)

    def run(self) -> None:
        self.outputCallback(f'Merging {len(self.frames)} frames to {self.outputPath}\n')
        frameImgs: list[Image.Image] = []
        for f in self.frames:
            b = io.BytesIO()
            with Image.open(f) as img:
                if self.optimizeTransparency:
                    # LUT from Photoshop curve: (209, 182) (237, 245)
                    img.putalpha(img.split()[-1].filter(ImageFilter.GaussianBlur(3)).point((
                        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
                        0x00, 0x00, 0x00, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
                        0x01, 0x01, 0x01, 0x02, 0x02, 0x02, 0x02, 0x02, 0x02, 0x02, 0x02, 0x02, 0x03, 0x03, 0x03, 0x03,
                        0x03, 0x03, 0x03, 0x04, 0x04, 0x04, 0x04, 0x04, 0x05, 0x05, 0x05, 0x05, 0x05, 0x06, 0x06, 0x06,
                        0x06, 0x07, 0x07, 0x07, 0x07, 0x08, 0x08, 0x08, 0x09, 0x09, 0x09, 0x0A, 0x0A, 0x0A, 0x0B, 0x0B,
                        0x0C, 0x0C, 0x0C, 0x0D, 0x0D, 0x0E, 0x0E, 0x0F, 0x0F, 0x10, 0x10, 0x10, 0x11, 0x12, 0x12, 0x13,
                        0x13, 0x14, 0x14, 0x15, 0x15, 0x16, 0x17, 0x17, 0x18, 0x19, 0x19, 0x1A, 0x1B, 0x1B, 0x1C, 0x1D,
                        0x1E, 0x1E, 0x1F, 0x20, 0x21, 0x22, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x29, 0x29, 0x2A,
                        0x2B, 0x2C, 0x2D, 0x2E, 0x2F, 0x30, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3B, 0x3C,
                        0x3D, 0x3E, 0x40, 0x41, 0x42, 0x43, 0x45, 0x46, 0x47, 0x49, 0x4A, 0x4C, 0x4D, 0x4F, 0x50, 0x51,
                        0x53, 0x55, 0x56, 0x58, 0x59, 0x5B, 0x5C, 0x5E, 0x60, 0x61, 0x63, 0x65, 0x67, 0x68, 0x6A, 0x6C,
                        0x6E, 0x70, 0x71, 0x73, 0x75, 0x77, 0x79, 0x7B, 0x7D, 0x7F, 0x81, 0x83, 0x85, 0x87, 0x89, 0x8C,
                        0x8E, 0x90, 0x92, 0x94, 0x97, 0x99, 0x9B, 0x9D, 0xA0, 0xA2, 0xA5, 0xA7, 0xA9, 0xAC, 0xAE, 0xB1,
                        0xB3, 0xB6, 0xB9, 0xBB, 0xBE, 0xC0, 0xC3, 0xC6, 0xC8, 0xCB, 0xCE, 0xD0, 0xD3, 0xD5, 0xD8, 0xDA,
                        0xDC, 0xDF, 0xE1, 0xE3, 0xE5, 0xE8, 0xEA, 0xEB, 0xED, 0xEF, 0xF1, 0xF2, 0xF4, 0xF5, 0xF6, 0xF7,
                        0xF8, 0xF9, 0xFA, 0xFB, 0xFB, 0xFC, 0xFC, 0xFD, 0xFD, 0xFE, 0xFE, 0xFE, 0xFE, 0xFF, 0xFF, 0xFF,
                    )).convert('1'))
                img.save(b, 'gif')
            os.remove(f)
            img = Image.open(b)
            if 'transparency' in img.info:
                paletteMap = list(range(256))
                paletteMap[0], paletteMap[img.info['transparency']] = paletteMap[img.info['transparency']], paletteMap[0]
                img = img.remap_palette(paletteMap)
                img.info['transparency'] = 0
            frameImgs.append(img)
        os.makedirs(os.path.split(self.outputPath)[0], exist_ok=True)
        frameImgs[0].save(self.outputPath, save_all=True, optimize=True, loop=0, duration=self.durations, append_images=frameImgs[1:], disposal=2)
        self.reportProgress(1)

class SplitGIFTask(AbstractTask):
    def __init__(
        self,
        outputCallback: typing.Callable[[str], None],
        progressValue: list[int | float],
        inputPath: str, outputPath: str,
        config: param.REConfigParams,
        queue: collections.deque[AbstractTask],
        optimizeTransparency: bool,
        itemId: int | None = None,
        progressCallback: typing.Callable[[int | None, float], None] | None = None,
        cancelEvent: threading.Event | None = None,
    ) -> None:
        super().__init__(outputCallback, itemId, progressCallback, cancelEvent)
        self.progressValue = progressValue
        self.inputPath = inputPath
        self.outputPath = outputPath
        self.config = config
        self.queue = queue
        self.optimizeTransparency = optimizeTransparency

    def run(self) -> None:
        frames = []
        durations = []
        tasks = []
        # 帧级任务的进度按（已完成帧数+当前帧进度）/总帧数聚合到 GIF 整体的 0~1
        frameCount = [0]
        with Image.open(self.inputPath) as img:
            for f in ImageSequence.Iterator(img):
                f: Image.Image
                frameSrcPath = tempfile.mktemp('.png' if self.optimizeTransparency else '.webp')
                frameDstPath = tempfile.mktemp('.png' if self.optimizeTransparency else '.webp')
                d = f.info.get('duration', 0)
                if self.optimizeTransparency:
                    f = f.convert('RGBA')
                    with Image.new('RGBA', img.size, (255, 255, 255, 255)) as g:
                        g.alpha_composite(f)
                        g.putalpha(f.split()[-1])
                        g.save(frameSrcPath, lossless=True)
                else:
                    f.save(frameSrcPath, lossless=True)
                self.outputCallback(f'Frame #{len(frames)}: {frameSrcPath} -> {frameDstPath} Duration: {d}\n')
                frameIndex = len(frames)
                def frameProgress(itemId: int | None, fraction: float, frameIndex: int = frameIndex) -> None:
                    if self.progressCallback is not None and frameCount[0]:
                        self.progressCallback(self.itemId, (frameIndex + fraction) / frameCount[0])
                frames.append(frameDstPath)
                durations.append(d)
                tasks.append(RESpawnTask(self.outputCallback, self.progressValue, frameSrcPath, frameDstPath, self.config, True, self.itemId, frameProgress, self.cancelEvent))
                self.progressValue[2] += 1
        frameCount[0] = len(frames)
        self.progressValue[2] -= 1
        if self.config.customCommand:
            t = tempfile.mktemp('.gif')
            tasks.append(MergeGIFTask(self.outputCallback, t, frames, durations, self.optimizeTransparency, cancelEvent=self.cancelEvent))
            tasks.append(CustomCompressTask(self.outputCallback, t, self.outputPath, self.config.customCommand, True, self.itemId, self.progressCallback, self.cancelEvent))
        else:
            tasks.append(MergeGIFTask(self.outputCallback, self.outputPath, frames, durations, self.optimizeTransparency, self.itemId, self.progressCallback, self.cancelEvent))
        tasks.reverse()
        for t in tasks:
            self.queue.appendleft(t)

class LossyCompressTask(AbstractTask):
    def __init__(
        self,
        outputCallback: typing.Callable[[str], None],
        inputPath: str, outputPath: str,
        quality: int,
        removeInput: bool = False,
        itemId: int | None = None,
        progressCallback: typing.Callable[[int | None, float], None] | None = None,
        cancelEvent: threading.Event | None = None,
    ) -> None:
        super().__init__(outputCallback, itemId, progressCallback, cancelEvent)
        self.inputPath = inputPath
        self.outputPath = outputPath
        self.quality = quality
        self.removeInput = removeInput

    def cleanup(self) -> None:
        if self.removeInput and os.path.exists(self.inputPath):
            os.remove(self.inputPath)

    def run(self) -> None:
        self.outputCallback(f'Compressing {self.inputPath} to {self.outputPath} with quality {self.quality}\n')
        os.makedirs(os.path.split(self.outputPath)[0], exist_ok=True)
        with Image.open(self.inputPath) as img:
            match os.path.splitext(self.outputPath)[1].lower():
                case '.webp':
                    img.save(self.outputPath, quality=self.quality, method=6)
                case '.jpg' | '.jpeg':
                    if img.mode == 'RGBA':
                        img = img.convert('RGB')
                        self.outputCallback('Discarding alpha channel to compress the RGBA image to JPEG\n')
                    img.save(self.outputPath, quality=self.quality, optimize=True, progressive=True)
        if self.removeInput:
            os.remove(self.inputPath)
        self.reportProgress(1)

class CustomCompressTask(AbstractTask):
    def __init__(
        self,
        outputCallback: typing.Callable[[str], None],
        inputPath: str, outputPath: str,
        commandTemplate: str,
        removeInput: bool = False,
        itemId: int | None = None,
        progressCallback: typing.Callable[[int | None, float], None] | None = None,
        cancelEvent: threading.Event | None = None,
    ) -> None:
        super().__init__(outputCallback, itemId, progressCallback, cancelEvent)
        self.inputPath = inputPath
        self.outputPath = outputPath
        self.commandTemplate = commandTemplate
        self.removeInput = removeInput
        self.process: subprocess.Popen = None

    def cleanup(self) -> None:
        if self.removeInput and os.path.exists(self.inputPath):
            os.remove(self.inputPath)

    def run(self) -> None:
        cmd = []
        for x in shlex.split(self.commandTemplate):
            if x == '{input}':
                cmd.append(self.inputPath)
            elif x == '{output}':
                cmd.append(self.outputPath)
            elif (m := re.search(r'^{output:(.+)}$', x)):
                cmd.append(f'{os.path.splitext(self.outputPath)[0]}.{m.group(1)}')
            else:
                cmd.append(x)
        self.outputCallback(f'Compressing {self.inputPath} with command: {shlex.join(cmd)}\n')
        os.makedirs(os.path.split(self.outputPath)[0], exist_ok=True)
        with subprocess.Popen(
            cmd,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            encoding='utf-8',
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        ) as p:
            self.process = p
            for line in p.stderr:
                if self.isCancelRequested():
                    p.terminate()
                    try:
                        p.wait(5)
                    except subprocess.TimeoutExpired:
                        p.kill()
                    raise TaskCancelled()
                self.outputCallback(line)
        self.process = None
        # 同 RESpawnTask：GUI 侧直接 terminate 时 stderr 读到 EOF，补判主动停止
        if self.isCancelRequested():
            raise TaskCancelled()
        if p.returncode:
            raise subprocess.CalledProcessError(p.returncode, cmd)
        if self.removeInput:
            os.remove(self.inputPath)
        self.reportProgress(1)

def taskRunner(
    queue: collections.deque[AbstractTask],
    pauseEvent: threading.Event,
    cancelEvent: threading.Event,
    outputCallback: typing.Callable[[str], None],
    completeCallback: typing.Callable[[bool], None],
    failCallback: typing.Callable[[Exception, int | None], None],
    finallyCallback: typing.Callable[[], None],
    ignoreError: bool,
    taskStartCallback: typing.Callable[[AbstractTask], None] | None = None,
) -> None:
    counter = 0
    withError = False
    cancelled = False
    while queue:
        pauseEvent.wait()
        if cancelEvent.is_set():
            cancelled = True
            break
        currentTask = queue.popleft()
        try:
            if taskStartCallback is not None:
                taskStartCallback(currentTask)
            ts = time.perf_counter()
            currentTask.run()
            te = time.perf_counter()
            outputCallback(f'Task #{counter} completed in {round((te - ts) * 1000)}ms.\n')
            counter += 1
        except TaskCancelled:
            cancelled = True
            break
        except Exception as ex:
            withError = True
            outputCallback(traceback.format_exc())
            failCallback(ex, getattr(currentTask, 'itemId', None))
            if not ignoreError:
                finallyCallback()
                return
    if cancelled:
        # 用户主动停止：清空队列并尽力清理剩余任务关联的临时文件，不当作失败
        for t in queue:
            try:
                t.cleanup()
            except Exception:
                outputCallback(traceback.format_exc())
        queue.clear()
        finallyCallback()
        return
    completeCallback(withError)
    finallyCallback()