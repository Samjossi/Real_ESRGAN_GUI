AGENTS

- 仅操作项目目录内文件，禁止访问外部路径
- 强制使用项目 `.venv` 的 Python，禁止系统全局 Python
- 如需生成临时文件、缓存或测试数据，必须存放在项目根目录内（如 `./.temp/`、`./tmp/` 或 `./cache/`），严禁使用系统临时目录（如 `/tmp`、`/var/tmp`、`C:\Windows\Temp`、`$TMPDIR` 等）
- 有时候使用绝对路径的时候就会被系统认为是目录外的文件而被要求权限，这个时候就尝试一下相对路径
- Always think and respond in Chinese (中文). 所有思考过程和输出必须使用中文。
- commit message 使用中文

目录说明：

- `core/`：Python 源码（`main.py` 主入口、`task.py`、`define.py`、`param.py`），启动命令 `uv run core/main.py`；根目录另有 `main.py` 启动器（仅转发到 `core/main.py`），也可 `uv run main.py`
- `bin/`：推理引擎二进制（`realesrgan-ncnn-vulkan`，git 忽略不入库），`define.py` 优先在 `bin/` 探测、兼容程序同级旧布局
- `asset/icons/`：图标资源（png/ico/icns/psd），`core/main.py` 与打包配置均从此处引用
- `asset/packaging/`：打包构建配置（`Info.plist`、两个 build 脚本、两个 `.spec`）
- `tests/`：测试脚本（冒烟、纯黑探针、主题/迁移验证），从项目根 `uv run tests/xxx.py` 直跑，产物落 `tmp/`，详见 `tests/README.md`
- 打包时从项目根目录执行，并显式传入 `.spec` 完整路径，例如 `pyinstaller asset/packaging/realesrgan-gui.spec`