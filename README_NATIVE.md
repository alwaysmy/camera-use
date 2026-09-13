# camera_native.py — 摄像头原生接口工具

> **注（2026-09-13）**：本文件描述的是**信息采集**用的老模块。
> 日常采集/控制请用 `camera_core.py`（统一门面：名称解析 + 曝光 + 黑图哨兵）；
> 红外相机见 `camera_ir.py`。`camera_native.py` 保留是因为它额外提供
> VID/PID 解析与 JSON 导出。另外，它的 `-i` 参数现在同样接受
> 类型/名字选择符（`rgb` / `hp` / `0`），不要再依赖裸索引。

## 功能概述

通过多层原生接口获取摄像头完整信息：

| 层级 | 接口 | 获取内容 |
|------|------|----------|
| 1 | **WMI** | 设备名、设备ID、VID/PID、驱动、状态 |
| 2 | **PowerShell** | PnP设备属性、枚举器、类信息 |
| 3 | **OpenCV+DirectShow** | 分辨率、FPS、参数值、可调性测试 |
| 4 | **USB VID/PID解析** | 厂商识别、硬件ID解析 |

## 快速使用

```bash
# 综合报告（推荐）
python camera_native.py

# JSON格式输出
python camera_native.py json

# 保存到文件
python camera_native.py json -o report.json

# 指定摄像头拍照
python camera_native.py snapshot -i 1 -d ./photos
```

## 命令详解

### `full` — 综合报告（默认）
采集所有层级信息并格式化打印：
```
[WMI] 设备信息
  名称:     HP Wide Vision FHD Camera
  设备ID:   USB\VID_04CA&PID_7086&MI_00\...
  状态:     OK
  制造商:   Microsoft
  驱动:     usbvideo
  USB:      VID_04CA&PID_7086  (Lite-On Technology)

[OpenCV] DirectShow 摄像头参数
  索引 0: 640x480  FPS=30.0  后端=DSHOW
    可调参数:
      BRIGHTNESS    = 1.0
      CONTRAST      = 51.0
      EXPOSURE      = -4.0
    只读参数:
      AUTO_WB       = 0.0
    不支持:
      FOCUS, ZOOM, PAN, TILT
```

### `wmi` — WMI查询
仅返回WMI层数据（JSON）：
```json
[{
  "name": "HP Wide Vision FHD Camera",
  "device_id": "USB\\VID_04CA&PID_7086&MI_00\\...",
  "usb": {"vid": "04CA", "pid": "7086", "vendor": "Lite-On Technology"}
}]
```

### `ps` — PowerShell查询
仅返回PowerShell PnP数据（JSON）

### `opencv` — OpenCV查询
返回OpenCV检测到的摄像头及参数可调性：
```json
[{
  "index": 0,
  "width": 640, "height": 480,
  "params": {
    "BRIGHTNESS": {"value": 1.0, "writable": true},
    "EXPOSURE": {"value": -4.0, "writable": true}
  }
}]
```

### `snapshot` — 拍照
```bash
python camera_native.py snapshot -i 0          # 摄像头0拍照
python camera_native.py snapshot -i 1 -d photos # 摄像头1，保存到photos目录
```

## 参数说明

| 参数 | 说明 |
|------|------|
| `-i, --index` | 摄像头索引（0=第一个，1=第二个） |
| `-o, --output` | JSON输出文件路径 |
| `-d, --dir` | 拍照保存目录 |
| `--max-index` | OpenCV扫描最大索引（默认20） |

## 摄像头参数可调性

### 您的设备（HP Wide Vision FHD Camera）

| 参数 | 值 | 可调 | 说明 |
|------|-----|------|------|
| BRIGHTNESS | 1.0 | ✓ | 亮度 |
| CONTRAST | 51.0 | ✓ | 对比度 |
| SATURATION | 65.0 | ✓ | 饱和度 |
| HUE | 1.0 | ✓ | 色调 |
| GAIN | 1.0 | ✓ | 增益 |
| EXPOSURE | -4.0 | ✓ | 曝光 |
| SHARPNESS | 50.0 | ✓ | 锐度 |
| BACKLIGHT | 1.0 | ✗ | 背光补偿（只读） |
| AUTO_WB | 0.0 | ✗ | 自动白平衡（只读） |
| FOCUS/ZOOM/PAN/TILT | - | - | 不支持 |

## 作为AI视觉能力接入

### Python API调用
```python
from camera_native import wmi_get_cameras, opencv_capture, opencv_get_cameras

# 1. 获取设备列表
cameras = wmi_get_cameras()
for cam in cameras:
    print(f"{cam['name']}: {cam['usb']['vendor']}")

# 2. 获取参数
params = opencv_get_cameras()

# 3. 拍照
result = opencv_capture(camera_index=0, save_dir="./photos")
print(f"已保存: {result['file']}")

# 4. 调节参数
import cv2
cap = cv2.VideoCapture(0)
cap.set(cv2.CAP_PROP_BRIGHTNESS, 10)
cap.set(cv2.CAP_PROP_CONTRAST, 60)
ret, frame = cap.read()
cv2.imwrite("adjusted.jpg", frame)
cap.release()
```

## 依赖

- Python 3.8+
- OpenCV (`pip install opencv-python`)
- wmi (`pip install wmi`)
- pywin32 (Windows)

## 注意事项

1. **DirectShow COM未注册**: 部分系统可能需要安装DirectShow运行时
2. **参数支持**: 不同摄像头支持的参数不同，由硬件决定
3. **FPS=0**: 某些USB摄像头在未打开预览时FPS显示为0
4. **IR Camera**: 红外摄像头可能不支持标准参数调节