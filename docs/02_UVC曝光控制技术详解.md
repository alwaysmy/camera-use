# UVC 曝光控制技术详解

## 1. UVC 协议基础

### 1.1 什么是 UVC

UVC (USB Video Class) 是 USB 设备类标准之一，定义了视频设备的接口规范。摄像头、摄像头、视频采集卡等设备都遵循此标准。

### 1.2 UVC 曝光控制

UVC 定义了两种曝光控制模式：

| 模式 | 说明 | 控制方 |
|------|------|--------|
| Auto | 摄像头内部 ISP 自动调整曝光时间 | 摄像头固件 |
| Manual | 主机指定固定曝光值 | 主机软件 |

### 1.3 关键数据结构

```c
// UVC Camera Control 请求
typedef struct {
    uint8_t bRequest;        // 请求类型
    uint16_t wValue;         // 属性类型 + 要求
    uint16_t wIndex;         // 接口号
    uint16_t wLength;        // 数据长度
    uint8_t *data;           // 数据
} UVC_Control_Request;

// 曝光属性的 wValue 组成
// 高8位: 属性类型 (0x00 = Exposure)
// 低8位: 要求 (0x01 = Current, 0x02 = Min, 0x03 = Max, ...)
```

## 2. DirectShow 中的实现

### 2.1 IAMCameraControl 接口

DirectShow 通过 `IAMCameraControl` 接口控制摄像头属性：

```cpp
MIDL_INTERFACE("C6E13370-36AC-11D2-B40B-00A0C90F2719")
IAMCameraControl : public IUnknown
{
public:
    // 获取属性范围
    virtual HRESULT GetRange(
        long Property,           // 属性类型
        long *pMin, long *pMax,  // 范围
        long *pSteppingDelta,    // 步长
        long *pDefault,          // 默认值
        long *pCapsFlag          // 能力标志 (Auto/Manual)
    ) = 0;

    // 设置属性
    virtual HRESULT Set(
        long Property,   // 属性类型
        long Value,      // 属性值
        long Flags       // 关键! Auto/Manual标志
    ) = 0;

    // 获取属性
    virtual HRESULT Get(
        long Property,   // 属性类型
        long *pValue,    // 当前值
        long *pFlags     // 当前模式
    ) = 0;
};
```

### 2.2 属性类型常量

```cpp
// CameraControl 属性
CameraControl_Exposure = 0      // 曝光
CameraControl_Focus = 2         // 对焦
CameraControl_Pan = 3           // 平移
CameraControl_Tilt = 4          // 倾斜
CameraControl_Roll = 5          // 旋转
CameraControl_Zoom = 6          // 变焦
CameraControl_Iris = 7          // 光圈

// 能力标志
CameraControl_Flags_Auto   = 0x0001  // 自动模式
CameraControl_Flags_Manual = 0x0002  // 手动模式
```

### 2.3 正确的调用方式

```cpp
// 设置自动曝光
pAMCameraControl->Set(
    CameraControl_Exposure,       // 属性
    0,                            // 值 (Auto模式下忽略)
    CameraControl_Flags_Auto      // 标志: 自动
);

// 设置手动曝光
pAMCameraControl->Set(
    CameraControl_Exposure,       // 属性
    -5,                           // 值 (曝光时间)
    CameraControl_Flags_Manual    // 标志: 手动
);
```

## 3. OpenCV 的问题

### 3.1 CAP_PROP_EXPOSURE 的实现

OpenCV 的 DirectShow 后端在设置 `CAP_PROP_EXPOSURE` 时：

```cpp
// OpenCV 源码 (modules/videoio/src/cap_dshow.cpp)
bool VideoCapture_DShow::setProperty(int propId, double value)
{
    switch (propId) {
        case CAP_PROP_EXPOSURE:
            // 只调用 Set(Value)，不传 Flags!
            return m_pAMCameraControl->Set(
                CameraControl_Exposure,
                (long)value,
                CameraControl_Flags_Manual  // 硬编码为 Manual!
            );
    }
}
```

**问题：OpenCV 硬编码为 Manual 模式！**

### 3.2 为什么这样设计

1. **历史兼容性**：DirectShow 时代，大多数应用需要精确控制
2. **参数可预测性**：Manual 模式下参数值固定，不会自动变化
3. **简化接口**：OpenCV 的 `CAP_PROP_*` 常量没有 Auto/Manual 的区分

## 4. Media Foundation 的实现

### 4.1 IMFCameraControl 接口

Windows 相机使用 Media Foundation，它有新的接口：

```cpp
// Media Foundation 曝光控制
IMFCameraControl::SetRange(
    CameraControl_Exposure,  // 属性
    min, max, step,          // 范围
    default,                 // 默认值
    flags                    // Auto/Manual 标志
);
```

### 4.2 Windows 相机的初始化流程

```
1. 枚举摄像头设备
2. 查询支持的媒体类型
3. 设置最优分辨率/帧率
4. 初始化摄像头控制接口
5. 设置 Auto 模式 (关键!)
6. 开始预览
```

## 5. duvc-ctl 的实现

### 5.1 库结构

```
duvc-ctl/
├── src/
│   ├── duvc_ctl.cpp      # 核心实现
│   ├── camera_control.cpp # IAMCameraControl 封装
│   └── device_enumerate.cpp # 设备枚举
├── include/
│   └── duvc_ctl.h
└── python/
    └── duvc_ctl.py       # Python 绑定
```

### 5.2 关键代码

```cpp
// duvc-ctl 设置曝光
bool set_exposure(Camera& cam, int value, CamMode mode) {
    IAMCameraControl* pCC = cam.getCameraControl();
    
    long flags = (mode == CamMode::Auto) 
        ? CameraControl_Flags_Auto 
        : CameraControl_Flags_Manual;
    
    HRESULT hr = pCC->Set(
        CameraControl_Exposure,
        value,
        flags
    );
    
    return SUCCEEDED(hr);
}
```

### 5.3 Python API

```python
import duvc_ctl as duvc

# 打开摄像头
cam = duvc.find_camera('HP')

# 设置自动曝光
cam.set_exposure(0, 'auto')

# 设置手动曝光
cam.set_exposure(-5, 'manual')

# 读取当前值
current = cam.get_exposure()  # 返回 int
```

## 6. 实际应用建议

### 6.1 何时用 Auto 模式

- 通用视频通话
- 光照环境变化大
- 不需要精确控制曝光

### 6.2 何时用 Manual 模式

- 工业视觉检测
- 恒定光照环境
- 需要固定曝光时间

### 6.3 混合策略

```python
# 初始化时设置 Auto
cam.set_exposure(0, 'auto')

# 稳定后切换到 Manual
current = cam.get_exposure()
cam.set_exposure(current, 'manual')
```

## 7. 参考资料

- [UVC 1.5 Class Specification](https://www.usb.org/document-library/video-class-v15-document-set)
- [IAMCameraControl Interface](https://learn.microsoft.com/en-us/windows/win32/directshow/iamcameracontrol)
- [Media Foundation Camera Control](https://learn.microsoft.com/en-us/windows/win32/medfound/camera-controls)
- [duvc-ctl GitHub](https://github.com/AlanBinu/duvc-ctl)
