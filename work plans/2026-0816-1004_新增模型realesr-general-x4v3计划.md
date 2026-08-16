# 新增模型 realesr-general-x4v3 计划

> **状态**：已完成（2026-08-16 执行完毕，smoke 回归 36/36 通过；默认模型保持 realesrgan-x4plus 不变，经用户确认）
> **范围**：`models/`（新增 2 个文件）、`模型对比说明.md`、`tests/smoke_test.py`（回归）
> **时间**：2026-08-16 10:04 (UTC+8)（设计）
> **优先级**：中

---

## 1. 需求来源

依据调研报告 `2026-0816-0959_超分模型选型调研报告_CPU环境.md` 第 7 节建议路线第一步。

用户明确原则：**只增不换**。现有 5 个模型（realesrgan-x4plus、realesrgan-x4plus-anime、realesr-animevideov3-x2/x3/x4）全部保留不动，新模型作为增量加入；若新模型实测不好用，删除新文件即可完整回退，零风险。

## 2. 目标

`models/` 新增 `realesr-general-x4v3.param` / `realesr-general-x4v3.bin`（Real-ESRGAN 官方 SRVGG 轻量模型，~1.2M 参数，BSD-3 协议），作为通用写实照片的**快速档**模型。定位：

| 档位 | 模型 | 场景 |
|:--|:--|:--|
| 通用图 · 质量档 | realesrgan-x4plus（现有，不动） | 追求最佳质量、能接受慢 |
| 通用图 · 快速档（**新增**） | realesr-general-x4v3 | CPU 环境日常用，官方文档明示质量略低于 x4plus 但速度最快 |
| 动漫/视频 · 快速档 | realesr-animevideov3 系列（现有，不动） | 动漫、GIF/视频帧 |
| 动漫 · 修复档 | realesrgan-x4plus-anime（现有，不动） | 有压缩伪影的动漫图 |

## 3. 可行性预检（已确认，零代码改动）

- GUI 模型列表由 `core/main.py` `init_config_and_model_paths()` 自动扫描 `models/` 下 param/bin 文件对生成，新文件放入即出现在模型下拉框；
- 放大倍率从文件名正则解析（`(\d+)x|x(\d+)`），`realesr-general-x4v3` 命中 `x4` → 倍率 4，正确；
- `core/task.py` 通过 `-n 模型名` 传给引擎，引擎原生支持该模型名；
- `models/*.bin` 已被 `.gitignore` 忽略，不入库，无仓库体积问题。

## 4. 实施清单

| 序号 | 内容 | 验收 |
|:--|:--|:--|
| T1 | 从 Real-ESRGAN 官方 release 便携包（v0.2.5.0+）提取 `realesr-general-x4v3.param/.bin`，核对文件来源与完整性（大小、magic bytes） | 文件来自官方 GitHub release，非第三方转载 |
| T2 | 本机裸跑验证：用现有测试图分别跑 x4v3 与 x4plus，断言输出非纯黑、尺寸正确 | `tmp/` 下落测试产物，mean 像素统计正常 |
| T3 | 速度基准：同图同参数对比 x4v3 vs x4plus（llvmpipe 与 `-g=-1` 原生 CPU 各跑一次），数据写入 `模型对比说明.md` | 预期 x4v3 快一个数量级 |
| T4 | 视觉对比：x4v3 与 x4plus 输出同图并排，人工确认质量差异可接受（流程参照 `视觉验证闭环开发指南.md`） | 用户过目 |
| T5 | 更新 `模型对比说明.md`：新增 x4v3 条目与定位说明；GUI 关于页自动读取该文件，无需改代码 | 文档与实测数据一致 |
| T6 | 回归：`uv run tests/smoke_test.py` 全量通过（GUI 枚举到新模型不破坏现有场景） | 35/35 |

## 5. 回退预案

新增物仅 2 个模型文件 + 文档改动。回退 = 删除 `models/realesr-general-x4v3.{param,bin}` + 还原文档，不影响任何现有功能。

## 6. 明确不做（本期范围外）

- ❌ 不删除/替换任何现有模型；
- ❌ 不改 `core/` 任何代码（预检已确认零改动）；
- ❌ 不入库 SPAN 社区模型与 Real_SAFMN++（需 pnnx 转换链与新内核引擎，属调研报告第 7 节第二、三步，待本期验证通过后另行立项）；
- ❌ 不改默认模型（`config.ini` 里 `model = realesrgan-x4plus` 保持，是否改默认由用户实测后决定）。

## 7. 风险与未核实项（执行结论）

- ✅ 已命中：x4v3 的 param/bin 官方便携包（v0.2.5.0 ubuntu zip）实际未包含，走了回退方案：官方 `realesr-general-x4v3.pth`（SHA-256 `8dc7edb9…`，体积与 GitHub API 元数据一致）→ torchscript → pnnx 20260526 转 ncnn fp32；ncnn python 与 torch 逐层数值对比 max diff 4.4e-6（容差 1e-3），转换与验证脚本留档 `tmp/x4v3_probe/convert_x4v3.py`、`verify_numeric.py`
- ✅ 已确认：ncnn 版引擎不支持 `-dn` 去噪强度调节，x4v3 只能以默认去噪强度运行，已在 `模型对比说明.md` 如实说明
- ✅ 速度实测（1024×1024 → 4096×4096，单次）：llvmpipe 软渲染 x4v3 约 10.5 秒 vs x4plus 约 141.5 秒（快约 13.5 倍）；本引擎不支持 `-g -1` 纯 CPU 参数（报 invalid gpu device）

## 8. 执行记录（2026-08-16）

- **兼容性修复**：2022 版引擎硬编码输入/输出 blob 名 `data`/`output`，而新版 pnnx 产出 `in0`/`out0`，直接放入会输出纯黑；已对 param 做改名处理（`in0→data`、`out0→output`），并固化进转换脚本
- **T2 裸跑验证**：RADV 核显与 llvmpipe 两路径输出均 256×256、非纯黑，与 x4plus 输出平均像素差 1.13
- **附带发现**：本机 RADV 核显路径下 x4plus 处理 ≥512×512 输入输出纯黑（既有环境缺陷，与本次新增无关），已写入 `模型对比说明.md`
- **T4 视觉对比**：`tmp/x4v3_probe/对比_实拍整图.png`、`对比_浪花细节.png`，x4v3 略平滑、差异符合官方「质量略低」定位，用户已过目
- **T6 回归**：`uv run tests/smoke_test.py` 36/36 全部通过（计划编写时为 35 项，执行时测试已增至 36 项）
- **开发环境说明**：转换需在 `.venv` 安装 torch（CPU 版）/pnnx/ncnn/numpy，仅开发用，未写入 `requirements.txt`
