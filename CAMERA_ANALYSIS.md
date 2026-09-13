# 摄像头问题根因分析报告

## 问题描述
- 摄像头(cam1)拍出来的图非常暗 (mean=15)
- 打开Windows相机后变正常 (mean=127+)
- 想找到根本原因

## 根本原因 (已确认)

### OpenCV不设置UVC Auto/Manual曝光标志

**关键代码差异:**

```cpp
// Windows相机 (IAMCameraControl接口)
pAMVCamControl->Set(CameraControl_Exposure, Val, CameraControl_Flags_Auto);

// OpenCV (只设置值，不设置标志)
cap.set(cv2.CAP_PROP_EXPOSURE, value);
```

**`IAMCameraControl::Set()` 有4个参数:**
1. `Property` - 属性类型 (EXPOSURE, FOCUS, etc.)
2. `Value` - 属性值
3. `Flags` - **关键!** 区分Auto/Manual模式
   - `CameraControl_Flags_Auto = 0x0001` - 自动模式
   - `CameraControl_Flags_Manual = 0x0002` - 手动模式

**OpenCV只传递Value，不传递Flags，导致:**
- 摄像头停留在Manual模式
- Manual模式下曝光范围有限 (-5 到 -2)
- 图像很暗

## 实验验证

### 测试结果
| 模式 | 设置值 | 实际亮度(mean) |
|------|--------|---------------|
| Manual | -5 | 22.2 |
| Manual | -4 | 39.1 |
| Manual | -3 | 48.9 |
| Manual | -2 | 56.8 |
| **Auto** | **0** | **123.0** |

### 结论
- Auto模式让图像亮度从56提升到123 (正常)
- duvc-ctl库可以正确设置Auto/Manual标志

## 解决方案

### 1. 使用duvc-ctl库
```python
import duvc_ctl as duvc

cam = duvc.find_camera('HP')
cam.set_exposure(0, 'auto')  # 设置自动曝光
```

### 2. 集成到camera_app.py
已添加:
- Auto/Manual模式切换按钮
- 启动预览时自动设置Auto模式
- 使用duvc-ctl控制曝光

### 3. CLI命令
```bash
python camera_tool.py auto-exposure auto      # 自动曝光
python camera_tool.py auto-exposure manual -2  # 手动曝光
```

## 技术细节

### 设备信息
- **HP Wide Vision FHD Camera** (cam1)
- USB: VID_04CA, PID_7086 (Lite-On Technology)
- 驱动: usbvideo.sys
- 设备栈: ksthunk → hrdevmon → usbvideo → usbccgp

### 曝光范围 (Manual模式)
- min: -5
- max: -2
- step: 1

### Auto模式行为
- 设置 `set_exposure(0, 'auto')` 后
- 摄像头内部ISP自动调整曝光
- `get_exposure()` 返回当前ISP计算的值
- 图像亮度正常 (mean=120+)

## 文件清单
- `camera_app.py` - GUI应用 (已集成duvc-ctl)
- `camera_tool.py` - CLI工具 (已添加auto-exposure命令)
- `duvc_test.py` - duvc-ctl测试脚本
- `CAMERA_ANALYSIS.md` - 本文档
