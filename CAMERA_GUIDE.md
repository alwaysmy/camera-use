# 摄像头使用指南

> ⚠️ **本文 2026-09-13 已按实测重写。**
> 旧版本写"索引 0 = IR、索引 1 = RGB 主力"是**错的**，按旧文档写代码会取到
> 尼康虚拟相机的静态黑图（mean≈8.6）。实测结果见下表。

## 这台机器上到底有几台"相机"

| 索引 | 设备 | 类型 | 实测分辨率 | 实测亮度 | 能不能直接用 |
|---|---|---|---|---|---|
| 0 | HP Wide Vision FHD Camera | **rgb** | 640×480 | 110+ | ✅ **主力，用它** |
| 1 | Nikon Webcam Utility | **virtual** | 1024×768 | 8.6（恒定） | ❌ 尼康相机没插时只有占位图 |
| — | HP IR Camera | **ir** | 340×340 | 80 / 38 逐帧交替 | ⚠️ 要单独走 Media Foundation |

两个 HP 是同一个 USB 复合设备（`VID_04CA:PID_7086`）的两个接口：
`MI_00` = RGB，`MI_02` = 红外。

> **索引不保证稳定** —— 它来自驱动枚举顺序，拔插、禁用/启用、驱动重载、
> 别的程序占用都会让它变。所以下面一律用**名字/类型**选设备，不要写死索引。

## 怎么选设备

所有工具都支持同一个"选择符"：

| 写法 | 含义 |
|---|---|
| `-i rgb` | 类型：挑第一台能直接开流的 RGB 相机（**推荐**）|
| `-i ir` | 类型：红外相机（没有 DShow 索引，自动走 Media Foundation）|
| `-i virtual` | 类型：虚拟相机 |
| `-i 0` | 直接给索引（兼容旧用法，不推荐）|
| `-i "hp"` / `-i nikon` | 名字子串匹配 |

## RGB 主相机（HP Wide Vision FHD）

```bash
# 拍照（预热 10 帧 + 黑图哨兵）
python camera_tool.py capture -i rgb -o photo.jpg

# 根治画面暗：切自动曝光
python camera_tool.py auto-exposure auto --range

# 实时预览（q 退出，s 拍照）
python camera_tool.py preview -i rgb
```

Python API：

```python
from camera_core import CameraController

with CameraController("rgb") as cam:        # 也可以用 None 让它自动挑
    print(cam.device)                       # HP Wide Vision FHD Camera [rgb] idx 0
    cam.set_exposure(auto=True)             # 解决过暗的关键
    frame = cam.grab(warmup=15)             # 预热 + 亮度校验（太暗直接抛错）
    cam.snapshot("photo.jpg")
```

### 关于曝光

* 曝光范围实测 `{min: -10, max: -2, step: 1, default: 0}`。
* **Auto 模式下 `get_exposure()` 仍会返回手动区间的值**（例如 -10），但画面最亮 ——
  因为 Auto 同时调整了增益等 OpenCV 看不到的参数。别用返回值判断模式。
* 摄像头自动曝光**需要爬坡时间**：冷启动第一帧往往很暗，连续读 10~15 帧才稳。
  所以 `grab()` 默认先丢掉 10 帧。

## 红外相机（HP IR Camera）

它**不在 DirectShow 枚举里**（被 Windows Hello 的隐私策略过滤掉了），
所以 `cv2.VideoCapture(1)` 之类永远打不开它。要用本项目提供的 MF 通道：

```bash
python camera_tool.py ir -o ir.jpg --info     # 自动挑补光灯点亮那一帧
python camera_tool.py ir --pair               # 同时存补光灯亮/灭两帧
```

```python
from camera_ir import IrCamera

with IrCamera() as ir:
    ir.snapshot("ir.jpg")                 # 亮帧 + 对比度拉伸
    lit, dark, delta = ir.read_pair()     # 活体检测原料
```

细节（为什么打不开、怎么打开的、补光灯交替帧、踩过的坑）见
[06_IR红外相机.md](docs/06_IR红外相机.md)。

## AI 视觉接入建议

```
┌────────────────────────────────────────────────────┐
│                   AI 视觉能力                       │
├────────────────────────────────────────────────────┤
│  rgb (HP FHD)  →  图像识别 / OCR / 场景理解   ← 主力 │
│  ir  (HP IR)   →  暗光人脸 / 活体检测               │
│  virtual       →  基本不用（没插相机时是黑图）        │
└────────────────────────────────────────────────────┘
```

主链路示例（带亮度防护）：

```python
from camera_core import CameraController, BlackFrameError
from camera_core import stretch_frame

with CameraController("rgb", warmup=15) as cam:
    cam.set_exposure(auto=True)
    try:
        frame = cam.grab()                     # mean < 40 会抛 BlackFrameError
    except BlackFrameError as e:
        # 取错设备 / 曝光没生效 / 镜头被遮挡 —— 让它显式失败，别把黑图喂给模型
        raise
    # 仍然偏暗时（暗光环境）再上预处理
    frame = stretch_frame(frame)
```

> **为什么要有黑图哨兵**：取错设备时 OpenCV 不报错 —— `ret=True`、帧也读到了，
> 只是内容是黑的。链路后面接 OCR/识别时会表现成"模型准确率莫名很低"，
> 排查方向很容易被带偏。哨兵把它变成一句立刻能看懂的报错。

## 快速命令速查

```bash
python camera_tool.py detect                 # 设备表（类型/分辨率/亮度）
python camera_tool.py info                   # 综合信息（设备+硬件+参数+可调性）
python camera_tool.py params -i rgb          # 读全部参数
python camera_tool.py test   -i rgb          # 逐个测参数可写性
python camera_tool.py capture -i rgb -o a.jpg
python camera_tool.py record -i rgb -t 5     # 录 5 秒
python camera_tool.py auto-exposure auto     # 自动曝光
python camera_tool.py auto-exposure manual -v -4
python camera_tool.py ir -o ir.jpg           # 红外
python camera_tool.py list-params            # 所有可用参数名

python camera_core.py --probe --json         # 门面自检（JSON 设备表）
```

## 文件说明

| 文件 | 用途 |
|---|---|
| `camera_core.py` | **统一门面** —— 设备解析 / 曝光 / 采集 / 黑图哨兵 |
| `camera_ir.py` | **红外相机** —— Media Foundation + 设备符号链接 |
| `camera_tool.py` | CLI 工具 |
| `camera_app.py` | tkinter GUI（预览 + 曝光模式切换）|
| `camera_native.py` | 多层信息采集（WMI / PnP / OpenCV / VID-PID），可导出 JSON |
| `camera_preview.py` | 极简预览脚本 |
| `tests/` | 回归测试 |

## 依赖

```bash
pip install opencv-python numpy duvc-ctl pillow
# camera_native 的 WMI 层还需要: pip install wmi pywin32
```
