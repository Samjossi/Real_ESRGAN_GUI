"""core/bin 目录迁移验证：路径基准、引擎探测、模型加载、图标与配置路径。"""
import os
import sys

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'core'))

import define
import param
import task

failures = []

def check(name, cond, detail=''):
    print(('PASS' if cond else 'FAIL'), name, detail)
    if not cond:
        failures.append(name)

check('BASE_PATH 指向项目根', define.BASE_PATH == ROOT, define.BASE_PATH)
check('APP_PATH 指向项目根', define.APP_PATH == ROOT, define.APP_PATH)
check('RE_PATH 命中 bin/', define.RE_PATH == os.path.join(ROOT, 'bin', 'realesrgan-ncnn-vulkan'), define.RE_PATH)
check('引擎二进制存在且可执行', os.path.isfile(define.RE_PATH) and os.access(define.RE_PATH, os.X_OK))
check('config.ini 路径指向项目根且存在', define.APP_CONFIG_PATH == os.path.join(ROOT, 'config.ini') and os.path.isfile(define.APP_CONFIG_PATH), define.APP_CONFIG_PATH)

import main as m
config, models = m.init_config_and_model_paths()
check('模型列表非空', len(models) > 0, f'{len(models)} 个模型')
check('图标 icon-128px.png 存在', os.path.isfile(os.path.join(define.BASE_PATH, 'asset', 'icons', 'icon-128px.png')))
check('图标 icon-256px.ico 存在', os.path.isfile(os.path.join(define.BASE_PATH, 'asset', 'icons', 'icon-256px.ico')))
check('模型对比说明.md 存在', os.path.isfile(os.path.join(define.BASE_PATH, '模型对比说明.md')))
check('task 模块可用', hasattr(task, 'AbstractTask'))
check('param 模块可用', hasattr(param, 'REConfigParams'))

print()
if failures:
    print('FAILED:', failures)
    sys.exit(1)
print(f'ALL PASSED ({11 - 0}/11)')
