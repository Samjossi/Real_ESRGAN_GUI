# Windows 打包脚本：建环境 → 取 Windows 引擎 → PyInstaller onedir → 压 zip
# 用法：powershell -ExecutionPolicy Bypass -File build-windows.ps1
# 前置：Windows 10/11 x64，已安装 Python 3.12+ 与 uv（https://docs.astral.sh/uv/）
$ErrorActionPreference = 'Stop'

# 脚本位于项目根目录，统一切到脚本所在目录执行
Set-Location $PSScriptRoot

# --- 1. 准备 Windows 版引擎（不入库，.gitignore 已忽略） ---
$engineExe = 'bin\realesrgan-ncnn-vulkan.exe'
if (-not (Test-Path $engineExe)) {
    Write-Host '下载 Real-ESRGAN 官方 Windows 便携包...'
    $workDir = 'tmp\windows_engine'
    $zipPath = Join-Path $workDir 'realesrgan-windows.zip'
    New-Item -ItemType Directory -Force $workDir | Out-Null
    Invoke-WebRequest -Uri 'https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-windows.zip' -OutFile $zipPath
    Expand-Archive -Force $zipPath $workDir
    New-Item -ItemType Directory -Force 'bin' | Out-Null
    # vcomp140.dll 是引擎依赖的 OpenMP 运行库，须与 exe 同目录
    Move-Item -Force (Join-Path $workDir 'realesrgan-ncnn-vulkan.exe') $engineExe
    Move-Item -Force (Join-Path $workDir 'vcomp140.dll') 'bin\vcomp140.dll'
    Remove-Item -Recurse -Force $workDir
}

# 引擎裸跑冒烟：-h 应有输出（无 Vulkan 环境也会打印用法）
$engineHelp = & $engineExe -h 2>&1 | Out-String
if ([string]::IsNullOrWhiteSpace($engineHelp)) {
    throw "引擎冒烟失败：$engineExe -h 无输出"
}
Write-Host "引擎就绪：$engineExe"

# --- 2. 建虚拟环境并安装依赖 ---
if (-not (Test-Path '.venv\Scripts\python.exe')) {
    uv venv .venv
}
uv pip install --python .venv\Scripts\python.exe -r requirements.txt 'pyinstaller==5.*'

# --- 3. PyInstaller onedir 打包（UPX 默认关闭，设 REGUI_UPX=1 可开启） ---
& .venv\Scripts\pyinstaller.exe --noconfirm asset/packaging/realesrgan-gui.spec
$distDir = 'dist\realesrgan-gui'
if (-not (Test-Path (Join-Path $distDir 'realesrgan-gui.exe'))) {
    throw "打包失败：未产出 $distDir\realesrgan-gui.exe"
}

# --- 4. 引擎随包分发：复制到 exe 同级 bin/（define.py 的探测路径） ---
Copy-Item -Recurse -Force 'bin' (Join-Path $distDir 'bin')

# --- 5. 压便携 zip ---
$zipOut = 'dist\realesrgan-gui-windows-x64.zip'
if (Test-Path $zipOut) { Remove-Item $zipOut }
Compress-Archive -Path $distDir -DestinationPath $zipOut
Write-Host "完成：$zipOut（解压即用，请勿放入 Program Files 等只读目录）"
