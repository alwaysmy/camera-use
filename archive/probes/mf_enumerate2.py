"""
mf_enumerate2.py — Media Foundation C API枚举（修复GUID）
"""
import ctypes
from ctypes import wintypes, byref, c_void_p, c_uint32, c_byte, POINTER
import time
import subprocess

class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", c_byte * 8),
    ]

def make_guid(d1, d2, d3, d4_bytes):
    g = GUID()
    g.Data1 = d1
    g.Data2 = d2
    g.Data3 = d3
    for i, b in enumerate(d4_bytes):
        g.Data4[i] = b
    return g

ole32 = ctypes.windll.ole32
mfplat = ctypes.windll.mfplat
mfreadwrite = ctypes.windll.mfreadwrite

ole32.CoInitializeEx(None, 0x2)
mfplat.MFStartup(0x02000000, 0)

def hr_s(hr):
    return f"0x{hr & 0xFFFFFFFF:08X}"

def main():
    print("=" * 60)
    print("  Media Foundation 设备枚举")
    print("=" * 60)
    
    # 创建属性存储
    ppAttrs = c_void_p()
    hr = mfplat.MFCreateAttributes(byref(ppAttrs), 2)
    print(f"\nMFCreateAttributes: {hr_s(hr)}")
    
    # MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE = {27679CB3-02C0-4491-9770-084B2854FABE}
    # MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_GUID = {A6D401E7-5C05-4DE2-9859-76C5F1024E01}
    guid_key = make_guid(0x27679CB3, 0x02C0, 0x4491, [0x97, 0x70, 0x08, 0x4B, 0x28, 0x54, 0xFA, 0xBE])
    guid_val = make_guid(0xA6D401E7, 0x5C05, 0x4DE2, [0x98, 0x59, 0x76, 0xC5, 0xF1, 0x02, 0x4E, 0x01])
    
    # 设置属性
    # IMFAttributes::SetGUID
    # vtable[3] = SetGUID(this, guidKey, guidValue)
    IUnknown_vtable = ctypes.cast(ppAttrs, POINTER(c_void_p)).contents.value
    SetGUID_ptr = ctypes.c_void_p.from_address(IUnknown_vtable + 3 * ctypes.sizeof(c_void_p))
    SetGUID = ctypes.CFUNCTYPE(ctypes.c_long, c_void_p, POINTER(GUID), POINTER(GUID))(SetGUID_ptr.value)
    
    hr = SetGUID(ppAttrs, byref(guid_key), byref(guid_val))
    print(f"SetGUID(SOURCE_TYPE, VIDCAP): {hr_s(hr)}")
    
    if hr != 0:
        print("SetGUID失败，尝试直接使用MFEnumDeviceSources")
    
    # 枚举设备
    ppActivate = POINTER(c_void_p)()
    numActivate = c_uint32(0)
    
    # MFEnumDeviceSources: mf.dll
    mf = ctypes.windll.mf
    hr = mf.MFEnumDeviceSources(ppAttrs, byref(ppActivate), byref(numActivate))
    print(f"MFEnumDeviceSources: {hr_s(hr)}, count={numActivate.value}")
    
    if hr == 0 and numActivate.value > 0:
        print(f"\n找到 {numActivate.value} 个视频设备:")
        for i in range(numActivate.value):
            activate = ppActivate[i]
            
            # 读取友好名称
            # IMFActivate::GetStringLength
            name_buf = ctypes.create_unicode_buffer(256)
            name_len = c_uint32(0)
            
            # 获取vtable
            vtbl = ctypes.cast(activate, POINTER(c_void_p)).contents.value
            # IMFAttributes继承: GetStringLength在vtable[8] (IUnknown(3) + IMFAttributes(8) = 11? 不对)
            # 实际上IMFActivate继承IMFAttributes
            # GetStringLength = IMFAttributes的方法 index 7
            
            # 读取友好名称
            guid_friendly = make_guid(0x60D81288, 0x76AA, 0x4C63, [0x8B, 0x81, 0xD6, 0x42, 0xB2, 0x0D, 0x48, 0x6E])
            
            GetStringLength_ptr = ctypes.c_void_p.from_address(vtbl + 8 * ctypes.sizeof(c_void_p))
            GetStringLength = ctypes.CFUNCTYPE(ctypes.c_long, c_void_p, POINTER(GUID), POINTER(c_uint32))(GetStringLength_ptr.value)
            
            hr2 = GetStringLength(activate, byref(guid_friendly), byref(name_len))
            if hr2 == 0 and name_len.value > 0:
                GetString_ptr = ctypes.c_void_p.from_address(vtbl + 9 * ctypes.sizeof(c_void_p))
                GetString = ctypes.CFUNCTYPE(ctypes.c_long, c_void_p, POINTER(GUID), c_uint32, POINTER(ctypes.c_wchar), POINTER(c_uint32))(GetString_ptr.value)
                hr3 = GetString(activate, byref(guid_friendly), name_len.value + 1, name_buf, byref(name_len))
                if hr3 == 0:
                    print(f"  [{i}] {name_buf.value}")
                else:
                    print(f"  [{i}] 无法读取名称 (GetString: {hr_s(hr3)})")
            else:
                print(f"  [{i}] 无法读取名称 (GetStringLength: {hr_s(hr2)})")
    else:
        print("未找到设备")
    
    # 清理
    mfplat.MFShutdown()
    ole32.CoUninitialize()
    print("\n完成!")

if __name__ == "__main__":
    main()
