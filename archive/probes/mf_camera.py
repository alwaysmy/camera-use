"""
mf_camera.py — 通过Media Foundation API访问摄像头
这是Windows Camera应用使用的底层API
"""
import ctypes
from ctypes import wintypes, Structure, POINTER, byref, c_void_p
import ctypes.wintypes
import time
import subprocess

# GUID定义
class GUID(Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_byte * 8),
    ]
    
    def __init__(self, l, w1, w2, b1, b2, b3, b4, b5, b6, b7, b8):
        super().__init__()
        self.Data1 = l
        self.Data2 = w1
        self.Data3 = w2
        for i, b in enumerate([b1, b2, b3, b4, b5, b6, b7, b8]):
            self.Data4[i] = b

# Media Foundation GUIDs
MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE = GUID(0x27679CB3, 0x02C0, 0x4491, 0x97, 0x70, 0x08, 0x4B, 0x28, 0x54, 0xFA, 0xBE)
MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_SYMBOLIC_LINK = GUID(0x58f0aac2, 0x31b0, 0x4bde, 0x84, 0x12, 0x9c, 0x89, 0x0e, 0x1a, 0xb4, 0x1e)
MF_DEVSOURCE_ATTRIBUTE_FRIENDLY_NAME = GUID(0x60d81288, 0x76aa, 0x4c63, 0x8b, 0x81, 0xd6, 0x42, 0xb2, 0x0d, 0x48, 0x6e)

# IMFDeviceSource
class IMFDeviceSource(c_void_p):
    pass

# IMFMediaSource
class IMFMediaSource(c_void_p):
    pass

# IMFActivate
class IMFActivate(c_void_p):
    pass

# IMFAttributes
class IMFAttributes(c_void_p):
    pass

# 加载DLL
ole32 = ctypes.windll.ole32
mfplat = ctypes.windll.mfplat
mf = ctypes.windll.mf
mfreadwrite = ctypes.windll.mfreadwrite

# 初始化COM
ole32.CoInitializeEx(None, 0x2)  # COINIT_MULTITHREADED

# 初始化Media Foundation
mfplat.MFStartup(0x02000000, 0)  # MF_VERSION

def enumerate_devices():
    """枚举视频捕获设备"""
    print("=== 枚举Media Foundation设备 ===\n")
    
    # 创建设备枚举器
    device_source_activate = POINTER(IMFActivate)()
    
    attributes = c_void_p()
    hr = mfplat.MFCreateAttributes(ctypes.byref(attributes), 2)
    print(f"MFCreateAttributes: 0x{hr:08X}")
    
    if hr != 0:
        return
    
    # 设置设备类型为视频捕获
    hr = attributes.PTR.QueryInterface(GUID(
        0x27679CB3, 0x02C0, 0x4491, 0x97, 0x70, 0x08, 0x4B, 0x28, 0x54, 0xFA, 0xBE
    ), None)
    
    # 使用MFEnumDeviceSources
    count = wintypes.UINT()
    
    # 重新尝试
    hr = mf.MFEnumDeviceSources(
        attributes,
        ctypes.byref(device_source_activate),
        ctypes.byref(count)
    )
    
    print(f"MFEnumDeviceSources: 0x{hr:08X}, count={count.value}")
    
    return count.value

def main():
    print("=" * 60)
    print("  Media Foundation 摄像头测试")
    print("=" * 60)
    
    # 检查MF DLL
    print("\n检查Media Foundation DLL...")
    try:
        mfplat.MFStartup(0x02000000, 0)
        print("  mfplat: OK")
    except Exception as e:
        print(f"  mfplat: {e}")
    
    # 枚举设备
    enumerate_devices()
    
    # 清理
    mfplat.MFShutdown()
    ole32.CoUninitialize()
    
    print("\n完成!")

if __name__ == "__main__":
    main()
