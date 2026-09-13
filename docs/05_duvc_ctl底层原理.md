# duvc-ctl 底层原理 — Auto vs Manual 的根本区别

## 1. 调用链路

```
Python: cam.set_exposure(0, 'auto')
    ↓
Python: PropSetting(0, CamMode.Auto)
    ↓
C++ Core: _core_camera.set(CamProp.Exposure, PropSetting)
    ↓
DirectShow: IAMCameraControl::Set(Property, Value, Flags)
    ↓
UVC: USB Control Transfer (SET_CUR)
    ↓
摄像头固件: 调整曝光策略
```

## 2. 关键区别：Flags 参数

### 2.1 IAMCameraControl::Set 原型

```cpp
HRESULT Set(
    long Property,   // 属性类型 (Exposure=0)
    long Value,      // 属性值
    long Flags       // 关键! 区分 Auto/Manual
);
```

### 2.2 Flags 值

```cpp
#define CameraControl_Flags_Manual  (0x0002)  // 手动模式
#define CameraControl_Flags_Auto    (0x0001)  // 自动模式
```

### 2.3 duvc-ctl 的实现

```python
# Python 层
cam.set_exposure(0, 'auto')
    ↓
# 构造 PropSetting
setting = PropSetting(value=0, mode=CamMode.Auto)
    ↓
# C++ 层调用
self._core_camera.set(CamProp.Exposure, setting)
    ↓
# 最终调用 DirectShow
pAMCameraControl->Set(
    CameraControl_Exposure,  // Property = 0
    0,                       // Value = 0
    CameraControl_Flags_Auto // Flags = 0x0001
)
```

## 3. Auto vs Manual 的本质区别

### 3.1 Auto 模式 (Flags = 0x0001)

```
主机设置: Set(Exposure, 0, Auto)
    ↓
摄像头固件:
    1. 读取当前环境亮度
    2. 计算最优曝光时间
    3. 实时调整曝光
    4. 返回当前曝光值 (get_exposure)
    ↓
效果: 图像亮度自动适应环境
```

**特点：**
- Value 参数被忽略 (设置为 0)
- 摄像头 ISP 自动计算曝光时间
- 实时调整，适应光照变化
- get_exposure() 返回 ISP 当前使用的值

### 3.2 Manual 模式 (Flags = 0x0002)

```
主机设置: Set(Exposure, -5, Manual)
    ↓
摄像头固件:
    1. 使用指定的曝光值 -5
    2. 固定曝光时间
    3. 不再自动调整
    ↓
效果: 图像亮度固定，可能偏暗或偏亮
```

**特点：**
- Value 参数被使用 (-5)
- 摄像头使用固定曝光时间
- 不会自动调整
- get_exposure() 返回设置的值

## 4. 为什么 OpenCV 图像暗

### 4.1 OpenCV 的调用方式

```cpp
// OpenCV DirectShow 后端
cap.set(cv2.CAP_PROP_EXPOSURE, value)
    ↓
// 内部调用
pAMCameraControl->Set(
    CameraControl_Exposure,
    (long)value,
    CameraControl_Flags_Manual  // 硬编码为 Manual!
);
```

**问题：OpenCV 硬编码为 Manual 模式！**

### 4.2 结果

| 设置 | 实际调用 | 效果 |
|------|---------|------|
| `cap.set(EXPOSURE, -5)` | `Set(Exposure, -5, Manual)` | 固定曝光，图像暗 |
| `cap.set(EXPOSURE, 0)` | `Set(Exposure, 0, Manual)` | 固定曝光，仍暗 |

## 5. duvc-ctl 的优势

### 5.1 正确设置 Auto 模式

```python
# duvc-ctl
cam.set_exposure(0, 'auto')
    ↓
pAMCameraControl->Set(Exposure, 0, CameraControl_Flags_Auto)
    ↓
摄像头自动调整曝光
    ↓
图像亮度正常
```

### 5.2 手动模式有更多控制

```python
# duvc-ctl 手动模式
cam.set_exposure(-5, 'manual')
    ↓
pAMCameraControl->Set(Exposure, -5, CameraControl_Flags_Manual)
    ↓
摄像头使用固定曝光值 -5
```

## 6. 手动模式的调整项

### 6.1 曝光范围

```python
# 查询范围
r = cam.get_property_range('exposure')
print(f"min={r.min}, max={r.max}, step={r.step}")
# 输出: min=-13, max=1, step=1
```

| 值 | 含义 |
|----|------|
| -13 | 最长曝光时间 (最亮) |
| ... | ... |
| -5 | 较长曝光 |
| -2 | 中等曝光 |
| 0 | 较短曝光 |
| 1 | 最短曝光时间 (最暗) |

**注意：** 不同摄像头的范围可能不同！

### 6.2 其他可调参数

duvc-ctl 支持更多参数的 Auto/Manual 控制：

| 参数 | Auto支持 | Manual支持 | 说明 |
|------|---------|-----------|------|
| Exposure | ✅ | ✅ | 曝光时间 |
| Focus | ✅ | ✅ | 对焦距离 |
| WhiteBalance | ✅ | ✅ | 白平衡 |
| Brightness | ❌ | ✅ | 亮度 |
| Contrast | ❌ | ✅ | 对比度 |
| Saturation | ❌ | ✅ | 饱和度 |
| Gain | ❌ | ✅ | 增益 |
| Pan/Tilt | ❌ | ✅ | 云台控制 |
| Zoom | ❌ | ✅ | 变焦 |

### 6.3 示例：对焦控制

```python
# 自动对焦
cam.set_focus(0, 'auto')

# 手动对焦
cam.set_focus(50, 'manual')  # 固定对焦距离
```

### 6.4 示例：白平衡

```python
# 自动白平衡
cam.set_white_balance(0, 'auto')

# 手动白平衡 (色温)
cam.set_white_balance(5500, 'manual')  # 日光
cam.set_white_balance(3200, 'manual')  # 白炽灯
```

## 7. 实际应用建议

### 7.1 何时用 Auto

- 通用视频通话
- 光照环境变化大
- 不需要精确控制

### 7.2 何时用 Manual

- 工业视觉检测
- 恒定光照环境
- 需要固定曝光时间
- 批量采集图像

### 7.3 混合策略

```python
# 1. 初始化时 Auto
cam.set_exposure(0, 'auto')
time.sleep(2)  # 等待稳定

# 2. 读取当前曝光值
current = cam.get_exposure()

# 3. 切换到 Manual，使用当前值
cam.set_exposure(current, 'manual')

# 4. 现在可以微调了
cam.set_exposure(current - 1, 'manual')
```

## 8. 总结

| 对比项 | OpenCV | duvc-ctl |
|--------|--------|----------|
| Auto模式 | ❌ 不支持 | ✅ 支持 |
| Manual模式 | ⚠️ 只有值 | ✅ 值+标志 |
| 对焦控制 | ❌ 无 | ✅ Auto/Manual |
| 白平衡 | ❌ 无 | ✅ Auto/Manual |
| 图像亮度 | 暗 (mean=56) | 正常 (mean=123) |

**核心结论：** duvc-ctl 通过正确设置 DirectShow 的 Flags 参数，实现了 Auto/Manual 模式的切换，解决了 OpenCV 图像过暗的问题。
