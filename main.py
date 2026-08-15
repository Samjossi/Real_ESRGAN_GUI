"""启动器：仅转发到 core/main.py，逻辑全部在 core/ 内。"""
import os
import runpy
import sys

if __name__ == '__main__':
    coreDir = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'core')
    sys.path.insert(0, coreDir)
    runpy.run_path(os.path.join(coreDir, 'main.py'), run_name='__main__')
