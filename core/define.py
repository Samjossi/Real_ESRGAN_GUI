import os
import sys

# 项目根目录：源码收纳于 core/ 下，开发态需向上一级
_ROOT_PATH = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

if sys.platform != 'darwin':
	BASE_PATH: str = os.path.realpath(sys._MEIPASS) if hasattr(sys, '_MEIPASS') else _ROOT_PATH
	APP_PATH = os.path.dirname(os.path.realpath(sys.executable)) if hasattr(sys, '_MEIPASS') else _ROOT_PATH
elif hasattr(sys, '_MEIPASS'):
	BASE_PATH = os.path.dirname(os.path.realpath(__file__))
	APP_PATH = BASE_PATH
else:
	BASE_PATH = _ROOT_PATH
	APP_PATH = _ROOT_PATH

# 引擎二进制优先在 bin/ 子目录探测，兼容与程序同级的旧布局
for executableName in (
	'upscayl-bin',
	'realesrgan-ncnn-vulkan',
	'realcugan-ncnn-vulkan',
):
	for candidateDir in (os.path.join(APP_PATH, 'bin'), APP_PATH):
		if os.path.exists(RE_PATH := os.path.join(candidateDir, executableName + ('.exe' if os.name == 'nt' else ''))):
			break
	else:
		continue
	break
APP_TITLE = 'Real-ESRGAN GUI'
APP_CONFIG_PATH = os.path.join(APP_PATH, 'config.ini')
BUILD_TIME: int = None
