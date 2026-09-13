# Camera Use — Windows 摄像头控制工具集

解决 **OpenCV 打开 USB 摄像头画面过暗**的问题，并把这台机器上摄像头的真实情况
（含被隐藏的 IR 红外相机）摸清楚。

---

## 零、主线：原生 Go 后端（零依赖、离线可构建）

`backend/` 是一套**纯 Go 标准库 + Windows syscall** 的实现，
**不依赖 OpenCV / DirectShow / Python / 外网**：Media Foundation 直接采 RGB 与 IR，
SetupAPI 直接找设备，`IAMCameraControl` 原生控曝光。

```powershell
cd backend
.\build.ps1                  # 离线构建（GOPROXY=off 证明不需要网络）
.\camera_backend.exe -open    # 控制台 http://127.0.0.1:8770/
```

> *[截图 原生 Go 控制台 —— 未随仓库发布（含个人影像，见 tempIMG/）]*

**实测性能**（640×480 RGB + 340×340 IR）：算子合计 **10.5 ms/帧 → 单核 95 fps**，
拼图 MJPEG 流 **24.7 fps**，RGB 14.9 fps(MJPG) + IR 29.8 fps(YUY2) 并发。

**前端性能**：只开 **一条 MJPEG 流**（主视图 + 三个缩略图拼成一张，浏览器每帧只解一张
JPEG）；切到后台自动断开流；状态轮询 1.2s 且仅在可见时进行。

详见 **[backend/README.md](backend/README.md)**（含码流选择、带宽规则、HTTP 接口表）。

> 下面的 Python 实现是**参考实现 / 调查过程的产物**（它记录了怎么一步步把 IR 挖出来、
> 怎么定位"OpenCV 只用 Manual 标志"这个根因）。功能仍然可用，但新功能只加在 Go 侧。

---

## 一、根本原因

OpenCV 的 DirectShow 后端调用 `IAMCameraControl::Set()` 时把 Flags 硬编码成
`CameraControl_Flags_Manual`，**没有** `Auto`。Windows 相机应用走的是
`CameraControl_Flags_Auto`，所以同一个摄像头在 Windows 相机里正常、在 OpenCV 里发暗。

解法是绕过 OpenCV，用 `duvc-ctl`（DirectShow COM）把曝光标志设成 Auto：

```python
from camera_core import CameraController

with CameraController("rgb") as cam:
    cam.set_exposure(auto=True)         # 根治：亮度 20 → 110
    cam.snapshot("photo.jpg")
```

## 二、这台机器上的设备真相（实测）

**索引是反的 —— 这是本项目最容易踩的坑，旧文档当年写错了。**

| 索引 | 设备 | 类型 | 实测分辨率 | 实测亮度 | 备注 |
|---|---|---|---|---|---|
| 0 | HP Wide Vision FHD Camera | **rgb** | 640×480 | **110+** | 真正能用的主相机（MI_00）|
| 1 | Nikon Webcam Utility | **virtual** | 1024×768 | **8.6 恒定** | 尼康虚拟相机；宿主没推流时是一张静态占位图 |
| — | HP IR Camera | **ir** | 340×340 | 80/38 交替 | **没有 DShow 索引**，要走 Media Foundation（MI_02）|

> 关键事实：`index 1` 不是红外相机，是**尼康虚拟相机**；
> 真正的红外相机（`HP IR Camera`）根本不在 DirectShow 枚举里。
> 早期文档写的 "cam0=IR、cam1=RGB" 与实际完全不符。

**索引不可信**：它来自驱动枚举顺序，会随拔插、禁用/启用设备、驱动重载、
别的程序占用而变。所以本项目的设备选取一律按 **名字/类型** 解析：

```bash
python camera_tool.py capture -i rgb     -o a.jpg    # 类型
python camera_tool.py capture -i "hp"    -o a.jpg    # 名字子串
python camera_tool.py capture -i 0       -o a.jpg    # 索引（兼容旧用法）
python camera_tool.py ir                 -o ir.jpg   # 红外（无需索引）
```

## 三、模块结构

```
camera_use/
├── camera_core.py      # ★ 统一门面：设备解析 / 曝光 / 采集 / 黑图哨兵
├── camera_ir.py        # ★ 红外相机：Media Foundation + 设备符号链接
├── ir_fusion_app.py    # ★ RGB+IR 实时融合演示台（tkinter GUI）
├── camera_tool.py      # CLI（detect/params/capture/record/auto-exposure/ir/...）
├── camera_app.py       # 普通预览 GUI（预览 + 曝光模式切换）
├── camera_native.py    # 多层信息采集（WMI / PnP / OpenCV / VID-PID），可导出 JSON
├── camera_preview.py   # 极简预览脚本
├── tests/              # 回归测试（默认离线跑，--hw 连硬件）
├── docs/               # 文档（01~07）
├── archive/probes/     # 排查期的实验脚本（mf_* / winrt_* / uvc_* …）
└── archive/test-output/# 排查期的测试图片（.gitignore，仅本地留存）
```

`camera_core` 是唯一真相来源，其它模块都走它：

| 能力 | 接口 |
|---|---|
| 设备表（含 IR / 虚拟） | `list_devices()` → `[CameraDevice]` |
| 按类型/名字/索引解析 | `resolve_device("rgb")` / `resolve_index("hp")` |
| 曝光 Auto/Manual + 范围 | `CameraController.set_exposure(auto=True)` / `.exposure_range()` |
| 采集（预热 + 亮度校验） | `.grab(warmup=15)` / `.snapshot("a.jpg")` |
| 黑图哨兵 | `check_brightness(frame, min_mean=40)` → 太暗抛 `BlackFrameError` |
| 红外拍摄 | `camera_ir.IrCamera().snapshot("ir.jpg")` |

## 四、快速开始

```bash
# 设备表（含 IR、虚拟相机，带实测分辨率和亮度）
python camera_tool.py detect

# 拍一张（自动挑 RGB 主相机，预热 10 帧 + 黑图哨兵）
python camera_tool.py capture -i rgb -o photo.jpg

# 根治画面过暗：切自动曝光
python camera_tool.py auto-exposure auto --range

# 红外照片（自动挑补光灯点亮的那帧，并做对比度拉伸）
python camera_tool.py ir -o ir.jpg --info

# 综合信息
python camera_tool.py info
```

依赖：

```bash
pip install opencv-python duvc-ctl pillow    # + numpy；WMI/pywin32 仅 camera_native 需要
```

## 五、实测数据（2026-09-13，本机 HP Wide Vision FHD）

曝光范围实测 `{min:-10, max:-2, step:1, default:0}`（早期文档写的 -5~-2 是错的）。

| 模式 | 设置 | mean | BGR |
|---|---|---|---|
| Manual | -10 | 7.3 | (10, 1, 11) |
| Manual | -6 | 21.5 | (23, 17, 25) |
| Manual | -2 | 67.6 | (61, 64, 78) |
| **Auto** | 0 | **107.1** | (95, 108, 118) |

要点：

* **Auto 模式下 `get_exposure()` 仍返回手动区间的值（如 -10）**，但画面是最亮的 ——
  说明 Auto 同时动了增益（gain）等 OpenCV 看不到的参数。所以"看曝光值判断模式"不可靠，
  `camera_core` 记录的是你最后一次设置的模式。
* 手动档不是单调的：-10 比 -6 更暗，个别档位会出现非线性（-6 与 -10 之间不严格递增）。
* 摄像头**自动曝光需要爬坡时间**：冷启动第一帧往往很暗，连续读 10~15 帧才稳定。
  这就是"开完流立刻抓一帧会得到暗图"的原因，`grab(warmup=)` 解决它。

## 六、红外相机（Windows Hello）

简短版：**能打开**，走 Media Foundation + 设备符号链接，绕过被隐私策略过滤的枚举。
原生格式 YUY2 340×340 @31fps，补光灯逐帧交替（这是 Hello 活体检测的原料）。

```python
from camera_ir import IrCamera

with IrCamera() as ir:
    ir.snapshot("ir.jpg")              # 亮帧 + 对比度拉伸
    lit, dark, delta = ir.read_pair()  # 活体检测原料
```

> *[截图 IR 红外成像 —— 未随仓库发布（含个人影像，见 tempIMG/）]*

完整排查过程、vtable 槽位表、以及 6 个"别再踩"的坑（缓冲区泄漏导致 `ReadSample`
永久阻塞、`MFShutdown` 卡死、符号链接少 `\GLOBAL`、强杀后设备残留 …）
见 **[docs/06_IR红外相机.md](docs/06_IR红外相机.md)**。

## 六之二、RGB + IR 融合（`ir_fusion_app.py`）

暗光下用 **IR 的亮度 + RGB 的色度** 合成彩色夜视图：

> *[截图 融合对比 —— 未随仓库发布（含个人影像，见 tempIMG/）]*

```bash
python ir_fusion_app.py --auto-align        # 融合演示台
```

要点：

* **配准**：IR(340×340) 与 RGB(640×480) 的总缩放实测 **1.30**、平移 (98, 18)
  （IR 视场更窄，落在 RGB 中央）。用**梯度图多尺度模板匹配**标定 ——
  跨模态下 ORB/SIFT 会匹配到错误的背景纹理。
* **带宽**：两路同时开流时 RGB 若用未压缩 YUY2 会从 30fps 崩到 **1.3fps**；
  改用 **MJPG** 后 RGB 15fps + IR 31fps 稳定并发。
* **补光差分**：IR 补光灯逐帧亮灭，`|亮帧 − 灭帧|` 直接得到**纯补光照明分量**
  （环境红外被抵消），是最直接的"强化对象"手段。

波段（850nm / 940nm）**软件查不到** —— 驱动是微软通用 `usbvideo.inf`，
UVC/MF/KS 都不报波长；给了三个自测判别法。补光灯**不可控**（固件自动频闪），
三条证据链和由此衍生的应用见 **[docs/07_IR波段与可见光融合.md](docs/07_IR波段与可见光融合.md)**。

## 七、测试

```bash
python -m unittest discover -s tests          # 31 项，纯逻辑，秒级
python tests/test_camera_core.py --hw         # 额外跑真实摄像头
```

覆盖：设备分类（IR/虚拟/RGB）、名字匹配打分、选择符解析、黑图哨兵（含边界与
虚拟相机的额外提示）、符号链接推导格式、辅助函数。

## 八、已知限制

* **MSMF 后端不可用**：这套 `opencv-python` 构建 `hasBackend(MSMF) == False`，
  只能走 DSHOW。好处是 MF 那条路我们自己在 `camera_ir.py` 里实现了。
* **IR 无法手动控曝光**：IR 相机不暴露 `IAMCameraControl`（`E_NOINTERFACE`），
  只能靠 warmup 等它自己收敛。补光灯也不能主动点（固件逐帧驱动）。
* **虚拟摄像头会静默给黑图**：Nikon Webcam Utility 在宿主未推流时输出静态占位图
  （字节级完全相同的 JPEG）。黑图哨兵能拦住它，但哨兵阈值（40）对极暗场景可能误报，
  可用 `--no-check` 或 `min_mean=None` 关掉。
* `camera_native.py` 的 WMI/PowerShell 层与 `camera_core` 有部分功能重叠，
  保留了它是因为它额外提供 VID/PID 解析和 JSON 导出。

## 九、文档

| 文档 | 内容 |
|---|---|
| [01_问题排查记录](docs/01_问题排查记录.md) | 从"画面暗"到定位 Auto 标志的完整过程 |
| [02_UVC曝光控制技术详解](docs/02_UVC曝光控制技术详解.md) | UVC / IAMCameraControl 原理 |
| [03_解决方案实现指南](docs/03_解决方案实现指南.md) | 方案落地 |
| [04_工具使用指南](docs/04_工具使用指南.md) | 各命令用法 |
| [05_duvc_ctl底层原理](docs/05_duvc_ctl底层原理.md) | duvc-ctl 内部机制 |
| **[06_IR红外相机](docs/06_IR红外相机.md)** | **打开 Windows Hello IR 相机（新增）** |
| **[07_IR波段与可见光融合](docs/07_IR波段与可见光融合.md)** | **波段判别 / 融合原理与标定 / 补光灯可控性 / 融合应用（新增）** |
| [CAMERA_GUIDE.md](CAMERA_GUIDE.md) | 面向使用者的设备说明与 AI 视觉接入建议 |

## 许可证

MIT License
