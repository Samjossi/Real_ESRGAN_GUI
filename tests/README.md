# tests/ 测试脚本

项目测试资产目录。不启用 pytest 框架，保持「脚本直跑」模式。

## 运行前提

- 在**项目根目录**下执行，统一用 `uv run tests/xxx.py`
- 使用项目 `.venv`（`uv run` 自动命中），GUI 相关脚本以 offscreen 平台运行，无需显示服务器
- 测试输入图（`a.png`/`b.png`/`c.gif`/`big.png` 等）由脚本自行生成到 `tmp/`，无需入库
- 所有产物（探针 PNG、日志、临时配置）落在 `tmp/`（git 忽略，随手可清）

## 常驻回归脚本

| 脚本 | 用途 | 运行命令 |
|:--|:---|:---|
| `smoke_test.py` | 端到端冒烟：offscreen 真实跑引擎，覆盖批处理/GIF/暂停继续/中途停止/扩展名规则/像素级非纯黑断言（30+ 项，需数分钟） | `uv run tests/smoke_test.py` |
| `exit_chain_test.py` | 退出链路验证：中途停止/处理中关窗时引擎子进程立即回收、无孤儿进程、日志行缓冲实时落盘（需数分钟） | `uv run tests/exit_chain_test.py` |
| `black_image_probe.py` | 纯黑缺陷视觉验证探针：L0 裸引擎 → L1 任务链 → L2 GIF 链 → L3 多趟 → L4 有损变体 → L5 大图 → L6 用户样本，逐档像素统计 | `uv run tests/black_image_probe.py --tag post` |
| `theme_smoke_test.py` | 主题手动切换开关冒烟（不跑真实处理，秒级） | `uv run tests/theme_smoke_test.py` |
| `verify_model_download.py` | 模型目录自选与下载功能验证：离线夹具覆盖直链/zip 下载、SHA-256 校验、取消、重扫、`ModelDir` 失效回退（秒级） | `uv run tests/verify_model_download.py` |

## 历史一次性验证脚本（留档）

以下脚本记录当时迁移/功能落地时的状态断言，入库仅作留档复用，不要求常驻可跑；当前状态下仍可通过，但相关结构再次变动时可能失效：

| 脚本 | 用途 |
|:--|:---|
| `verify_asset_migration.py` | asset 目录迁移验证（图标落位与加载） |
| `verify_core_bin_migration.py` | core/bin 模块化迁移验证（路径基准、引擎探测、模型加载） |
| `verify_model_guide.py` | 模型选择指南/关于页 Markdown 渲染验证 |

## 注意

- 脚本内 `sys.path` 假设「从项目根目录运行」，请勿在 `tests/` 内直接 `python xxx.py`
- 冒烟测试会真实调用 `bin/realesrgan-ncnn-vulkan`，无 Vulkan 环境时走 llvmpipe 软渲染，耗时更长属正常
