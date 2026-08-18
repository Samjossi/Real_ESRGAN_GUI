"""模型下载器：仅依赖标准库（urllib/zipfile/hashlib）。

- 清单 MODEL_MANIFEST 硬编码官方来源 URL 与 SHA-256，随版本发布更新（改动前需实下核对）；
- 分块下载写 .part 临时文件，完成后 os.replace 原子改名；
- 下载/解压后逐文件比对 SHA-256，不符即删除并报错；
- 支持直链文件与官方 zip 便携包内成员两种来源；
- 不做断点续传与自动重试，失败由用户重新勾选下载（首启引导）或再次一键下载（「模型设定」页）。
"""

import hashlib
import os
import typing
import urllib.error
import urllib.request
import zipfile

# 官方引擎便携包（含 5 个预置模型，各平台包内模型一致，取体积最小的 Windows 包）
ZIP_20220424_URL = 'https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-windows.zip'
# GUI 官方附加模型发布页（realesr-general-x4v3 的 ncnn 版本在此单独分发）
ADDITIONAL_MODELS_URL = 'https://github.com/TransparentLC/realesrgan-gui/releases/download/additional-models'
# 手动下载指引（网络失败时的错误文案引用）
MANUAL_DOWNLOAD_PAGE = 'https://github.com/xinntao/Real-ESRGAN/releases'


def _zip_member(filename: str, sha256: str) -> dict:
    return {
        'filename': filename,
        'url': ZIP_20220424_URL,
        'zipMember': f'models/{filename}',
        'sha256': sha256,
    }


def _direct_file(filename: str, sha256: str) -> dict:
    return {
        'filename': filename,
        'url': f'{ADDITIONAL_MODELS_URL}/{filename}',
        'sha256': sha256,
    }


# 推荐模型清单：名称 / 展示说明 / 需要落盘的 .bin 与 .param（含来源 URL 与 SHA-256）
MODEL_MANIFEST: tuple[dict, ...] = (
    {
        'name': 'realesrgan-x4plus',
        'description': '三次元通用，质量最高，速度慢（约 32 MB）',
        'files': (
            _zip_member('realesrgan-x4plus.bin', '713ee713b0353afaa27976f0563a64a5043bd70b9bd8936c2e26e25ebcdbcddf'),
            _zip_member('realesrgan-x4plus.param', '35330ececcea33b6c397a72548e788d5d53becee4734c50b7fada36e89f10a86'),
        ),
    },
    {
        'name': 'realesrgan-x4plus-anime',
        'description': '二次元图片，画质优先（约 9 MB）',
        'files': (
            _zip_member('realesrgan-x4plus-anime.bin', 'fe01c269cfd10cdef8e018ab66ebe750cf79c7af4d1f9c16c737e1295229bacc'),
            _zip_member('realesrgan-x4plus-anime.param', '2b8fb6e0ae4d2d85704ca08c119a2f5ea40add4f2ecd512eb7f4cd44b6127ed4'),
        ),
    },
    {
        'name': 'realesr-animevideov3-x2',
        'description': '二次元视频/图片，速度优先，2 倍放大（约 1.2 MB）',
        'files': (
            _zip_member('realesr-animevideov3-x2.bin', '548a36f9c3f4ab8da56cd3b13badf23968bee207b396dad14d04b830e5f2ab2d'),
            _zip_member('realesr-animevideov3-x2.param', 'b88ff4f00ebf019a7fdac17fdd45a7fd3665d37509efc5baf2e4da2e24420a04'),
        ),
    },
    {
        'name': 'realesr-animevideov3-x3',
        'description': '二次元视频/图片，速度优先，3 倍放大（约 1.2 MB）',
        'files': (
            _zip_member('realesr-animevideov3-x3.bin', '548a36f9c3f4ab8da56cd3b13badf23968bee207b396dad14d04b830e5f2ab2d'),
            _zip_member('realesr-animevideov3-x3.param', 'd1a5755008791d09b57e3425fc9dd0bd26b00fdf79c606210bc0e693f8230881'),
        ),
    },
    {
        'name': 'realesr-animevideov3-x4',
        'description': '二次元视频/图片，速度优先，4 倍放大（约 1.2 MB）',
        'files': (
            _zip_member('realesr-animevideov3-x4.bin', '548a36f9c3f4ab8da56cd3b13badf23968bee207b396dad14d04b830e5f2ab2d'),
            _zip_member('realesr-animevideov3-x4.param', '850a248e7c14c27e5bd8cf7265113a9441036a7db63963bb8aa5169d788a435e'),
        ),
    },
    {
        'name': 'realesr-general-x4v3',
        'description': '三次元通用，速度优先，CPU 环境日常首选（约 4.6 MB）',
        'files': (
            _direct_file('realesr-general-x4v3.bin', 'bfdfaebf0b26be9442faa30b8cec617fe04a808ae2aa3616ece85b92d81ae0ce'),
            _direct_file('realesr-general-x4v3.param', '33950f36822fd37786502db9bd6a3638f677c13b23814c8e5a6e8fd9bcea2163'),
        ),
    },
)

CHUNK_SIZE = 65536


class DownloadError(Exception):
    """下载或校验失败，消息为面向用户的友好提示。"""
    pass


class DownloadCancelled(Exception):
    """用户取消下载，不作为失败处理。"""
    pass


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b''):
            h.update(chunk)
    return h.hexdigest()


def _check_cancel(cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled()


def download_file(
    url: str,
    dest: str,
    progress_cb: typing.Callable[[str, int, int], None] | None = None,
    cancel_event=None,
) -> None:
    """分块下载 url 到 dest（先写 dest.part，完成后原子改名）。

    progress_cb(display_name, done_bytes, total_bytes)，total 未知时为 0。
    """
    request = urllib.request.Request(url, headers={'User-Agent': 'realesrgan-gui'})
    partPath = dest + '.part'
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            total = int(response.headers.get('Content-Length') or 0)
            done = 0
            with open(partPath, 'wb') as f:
                while True:
                    _check_cancel(cancel_event)
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if progress_cb is not None:
                        progress_cb(os.path.basename(dest), done, total)
    except DownloadCancelled:
        if os.path.exists(partPath):
            os.remove(partPath)
        raise
    except (urllib.error.URLError, OSError) as ex:
        if os.path.exists(partPath):
            os.remove(partPath)
        raise DownloadError(
            f'下载失败：{os.path.basename(dest)}\n'
            f'原因：{ex}\n'
            f'可改用浏览器打开 {MANUAL_DOWNLOAD_PAGE} 手动下载，'
            '把 .bin/.param 文件放入所选模型目录后点「重新扫描」。'
        )
    os.replace(partPath, dest)


def _extract_zip_member(
    zipPath: str,
    member: str,
    dest: str,
    progress_cb: typing.Callable[[str, int, int], None] | None,
    cancel_event,
) -> None:
    _check_cancel(cancel_event)
    with zipfile.ZipFile(zipPath) as z:
        try:
            data = z.read(member)
        except KeyError:
            raise DownloadError(f'模型压缩包内未找到文件：{member}（官方包结构可能已变动，请反馈）')
    h = hashlib.sha256(data).hexdigest()
    partPath = dest + '.part'
    with open(partPath, 'wb') as f:
        f.write(data)
    os.replace(partPath, dest)
    if progress_cb is not None:
        progress_cb(os.path.basename(dest), len(data), len(data))
    # 把校验结果透传给调用方统一比对
    return h


def model_verified(entry: dict, dest_dir: str) -> bool:
    """清单条目是否已安装且完好：逐文件比对 SHA-256，任一缺失或不符即视为未安装。"""
    for fileEntry in entry['files']:
        path = os.path.join(dest_dir, fileEntry['filename'])
        if not os.path.isfile(path) or sha256_file(path) != fileEntry['sha256']:
            return False
    return True


def download_models(
    entries: typing.Iterable[dict],
    dest_dir: str,
    progress_cb: typing.Callable[[str, int, int], None] | None = None,
    cancel_event=None,
) -> list[str]:
    """批量下载清单条目到 dest_dir，返回错误信息列表（空列表 = 全部成功）。

    同一 zip 来源在一次批量下载中只拉取一次，结束后清理缓存。
    用户取消（DownloadCancelled）时中断剩余下载，已下载部分保留。
    """
    errors: list[str] = []
    zipCache: dict = {}
    try:
        for entry in entries:
            try:
                download_model(entry, dest_dir, progress_cb, cancel_event, zipCache)
            except DownloadCancelled:
                break
            except DownloadError as ex:
                errors.append(str(ex))
    finally:
        for p in zipCache.values():
            try:
                os.remove(p)
            except OSError:
                pass
    return errors


def download_model(
    entry: dict,
    dest_dir: str,
    progress_cb: typing.Callable[[str, int, int], None] | None = None,
    cancel_event=None,
    zip_cache: dict | None = None,
) -> None:
    """按清单条目下载一个模型到 dest_dir，逐文件校验 SHA-256。

    zip_cache：同一次批量下载中按 URL 复用已下载的 zip，避免重复拉取。
    """
    os.makedirs(dest_dir, exist_ok=True)
    for fileEntry in entry['files']:
        _check_cancel(cancel_event)
        dest = os.path.join(dest_dir, fileEntry['filename'])
        if 'zipMember' in fileEntry:
            url = fileEntry['url']
            if zip_cache is not None and url in zip_cache:
                zipPath = zip_cache[url]
            else:
                zipPath = os.path.join(dest_dir, f'.{os.path.basename(url)}.download')
                download_file(url, zipPath, progress_cb, cancel_event)
                if zip_cache is not None:
                    zip_cache[url] = zipPath
            actual = _extract_zip_member(zipPath, fileEntry['zipMember'], dest, progress_cb, cancel_event)
        else:
            download_file(fileEntry['url'], dest, progress_cb, cancel_event)
            actual = sha256_file(dest)
        if actual != fileEntry['sha256']:
            os.remove(dest)
            raise DownloadError(
                f'校验失败：{fileEntry["filename"]}\n'
                '文件 SHA-256 与清单不一致，已删除。可能是下载被篡改或官方包已更新，'
                '请重试；仍失败请反馈维护者更新模型清单。'
            )
