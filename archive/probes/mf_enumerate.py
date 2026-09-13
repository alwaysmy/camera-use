"""
mf_enumerate.py — 用ctypes直接调用Media Foundation C API枚举摄像头
参考: https://learn.microsoft.com/en-us/windows/win32/medfound/enumerating-devices
"""
import ctypes
from ctypes import wintypes, byref, c_void_p, c_uint32, POINTER
import time
import subprocess

# ── 初始化COM和MF ──
ole32 = ctypes.windll.ole32
mfplat = ctypes.windll.mfplat
mfreadwrite = ctypes.windll.mfreadwrite

ole32.CoInitializeEx(None, 0x2)  # COINIT_MULTITHREADED
mfplat.MFStartup(0x02000000, 0)  # MF_VERSION

def hr_str(hr):
    return f"0x{hr & 0xFFFFFFFF:08X}"

def main():
    print("=" * 60)
    print("  Media Foundation 设备枚举 (C API)")
    print("=" * 60)
    
    # ── 1. 创建属性存储 ──
    print("\n[1] 创建MFAttributes...")
    ppAttributes = c_void_p()
    hr = mfplat.MFCreateAttributes(byref(ppAttributes), 2)
    print(f"  MFCreateAttributes: {hr_str(hr)}")
    if hr != 0:
        return
    
    attrs = ppAttributes
    
    # ── 2. 设置源类型为视频捕获 ──
    print("[2] 设置设备类型...")
    
    # MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE = {27679CB3-02C0-4491-9770-084B2854FABE}
    # MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_GUID = {A6D401E7-5C05-4DE2-9859-76C5F1024E01}
    import ctypes.wintypes as wt
    
    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD), ("Data4", ctypes.c_byte * 8)]
    
    guid_source_type = GUID(0x27679CB3, 0x02C0, 0x4491, 0x97, 0x70, 0x08, 0x4B, 0x28, 0x54, 0xFA, 0xBE)
    guid_vidcap = GUID(0xA6D401E7, 0x5C05, 0x4DE2, 0x98, 0x59, 0x76, 0xC5, 0xF1, 0x02, 0x4E, 0x01)
    
    # 调用 IMFAttributes::SetGUID
    vtable = ctypes.cast(attrs, POINTER(ctypes.c_void_p)).contents.value
    # IMFAttributes虚表: 3=SetGUID
    SetGUID_func = ctypes.CFUNCTYPE(ctypes.c_long, c_void_p, POINTER(GUID), POINTER(GUID))
    
    # 直接用MF函数
    hr = mfplat.MFAllocateSetGUID(attrs, byref(guid_source_type), byref(guid_vidcap))
    print(f"  设置源类型: {hr_str(hr)}")
    
    # ── 3. 枚举设备 ──
    print("[3] 枚举视频捕获设备...")
    
    ppActivate = POINTER(c_void_p)()
    pnumActivate = c_uint32(0)
    
    hr = mfreadwrite.MFEnumDeviceSources(attrs, byref(ppActivate), byref(pnumActivate))
    print(f"  MFEnumDeviceSources: {hr_str(hr)}, 数量: {pnumActivate.value}")
    
    if hr != 0 or pnumActivate.value == 0:
        print("  未找到设备，尝试其他方法...")
        
        # 尝试用MFCreateDeviceSource
        print("\n[3b] 尝试MFCreateDeviceSources...")
        ppSources = POINTER(c_void_p)()
        pnumSources = c_uint32(0)
        
        hr = mfreadwrite.MFEnumDeviceSources(attrs, byref(ppSources), byref(pnumSources))
        print(f"  结果: {hr_str(hr)}, 数量: {pnumSources.value}")
    
    # ── 4. 如果有设备，读取属性 ──
    if pnumActivate.value > 0:
        print(f"\n[4] 找到 {pnumActivate.value} 个设备:")
        for i in range(pnumActivate.value):
            activate = ppActivate[i]
            print(f"  设备 {i}: {activate}")
    
    # ── 5. 清理 ──
    print("\n[5] 清理...")
    ctypes.windll.ole32.CoTaskMemFree(ppAttributes)
    mfplat.MFShutdown()
    ole32.CoUninitialize()
    
    print("\n完成!")

if __name__ == "__main__":
    main()
