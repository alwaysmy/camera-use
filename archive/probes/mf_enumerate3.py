"""
mf_enumerate3.py — 用MFCreateAttributes直接设置属性
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
mf = ctypes.windll.mf

ole32.CoInitializeEx(None, 0x2)
mfplat.MFStartup(0x02000000, 0)

def hr_s(hr):
    return f"0x{hr & 0xFFFFFFFF:08X}"

def main():
    print("=" * 60)
    print("  Media Foundation 设备枚举 (v3)")
    print("=" * 60)
    
    # 直接用MFCreateAttributes设置初始属性
    # MFCreateAttributes(ppAttributes, cInitialSize)
    ppAttrs = c_void_p()
    
    # 创建属性存储
    hr = mfplat.MFCreateAttributes(byref(ppAttrs), 2)
    print(f"\nMFCreateAttributes: {hr_s(hr)}")
    
    # 使用IMFAttributes接口
    # IMFAttributes继承自IUnknown
    # 方法顺序: IUnknown(3) + IMFAttributes methods
    # 0-2: QueryInterface, AddRef, Release
    # 3: GetItem
    # 4: GetItemType
    # 5: CompareItem
    # 6: Compare
    # 7: GetUINT32
    # 8: GetUINT64
    # 9: GetDouble
    # 10: GetGUID
    # 11: GetStringLength
    # 12: GetString
    # 13: GetAllocatedString
    # 14: GetBlobSize
    # 15: GetBlob
    # 16: GetAllocatedBlob
    # 17: GetUnknown
    # 18: SetItem (not what we want)
    # ...
    # Actually let's use the proper MF API functions
    
    # MFCreateAttributes已经创建了空属性存储
    # 我们需要通过vtable来设置属性
    
    # 获取vtable指针
    vtbl_ptr = ctypes.cast(ppAttrs, POINTER(c_void_p)).contents.value
    
    # IMFAttributes::SetGUID (vtable index 22)
    # https://learn.microsoft.com/en-us/windows/win32/api/mfobjects/nf-mfobjects-imfattributes-setguid
    SetGUID_addr = ctypes.c_void_p.from_address(vtbl_ptr + 22 * ctypes.sizeof(c_void_p))
    SetGUID = ctypes.CFUNCTYPE(
        ctypes.c_long,   # HRESULT
        c_void_p,        # this
        POINTER(GUID),   # guidKey
        POINTER(GUID),   # guidValue
    )(SetGUID_addr.value)
    
    # 设置SOURCE_TYPE = VIDCAP
    guid_key = make_guid(0x27679CB3, 0x02C0, 0x4491, [0x97, 0x70, 0x08, 0x4B, 0x28, 0x54, 0xFA, 0xBE])
    guid_val = make_guid(0xA6D401E7, 0x5C05, 0x4DE2, [0x98, 0x59, 0x76, 0xC5, 0xF1, 0x02, 0x4E, 0x01])
    
    hr = SetGUID(ppAttrs, byref(guid_key), byref(guid_val))
    print(f"SetGUID: {hr_s(hr)}")
    
    if hr != 0:
        # 尝试不同的vtable偏移
        for offset in [20, 21, 22, 23, 24]:
            addr = ctypes.c_void_p.from_address(vtbl_ptr + offset * ctypes.sizeof(c_void_p))
            try:
                fn = ctypes.CFUNCTYPE(ctypes.c_long, c_void_p, POINTER(GUID), POINTER(GUID))(addr.value)
                hr2 = fn(ppAttrs, byref(guid_key), byref(guid_val))
                print(f"  offset {offset}: {hr_s(hr2)}")
                if hr2 == 0:
                    hr = hr2
                    print(f"  找到正确的offset: {offset}")
                    break
            except:
                pass
    
    # 枚举设备
    ppActivate = POINTER(c_void_p)()
    numActivate = c_uint32(0)
    
    hr = mf.MFEnumDeviceSources(ppAttrs, byref(ppActivate), byref(numActivate))
    print(f"\nMFEnumDeviceSources: {hr_s(hr)}, count={numActivate.value}")
    
    if hr == 0 and numActivate.value > 0:
        print(f"\n找到 {numActivate.value} 个设备!")
        
        # 读取设备名称
        guid_friendly = make_guid(0x60D81288, 0x76AA, 0x4C63, [0x8B, 0x81, 0xD6, 0x42, 0xB2, 0x0D, 0x48, 0x6E])
        
        for i in range(numActivate.value):
            activate = ppActivate[i]
            vtbl = ctypes.cast(activate, POINTER(c_void_p)).contents.value
            
            # GetStringLength
            GetStringLength_addr = ctypes.c_void_p.from_address(vtbl + 11 * ctypes.sizeof(c_void_p))
            GetStringLength = ctypes.CFUNCTYPE(
                ctypes.c_long, c_void_p, POINTER(GUID), POINTER(c_uint32)
            )(GetStringLength_addr.value)
            
            name_len = c_uint32(0)
            hr2 = GetStringLength(activate, byref(guid_friendly), byref(name_len))
            
            if hr2 == 0:
                # GetString
                GetString_addr = ctypes.c_void_p.from_address(vtbl + 12 * ctypes.sizeof(c_void_p))
                GetString = ctypes.CFUNCTYPE(
                    ctypes.c_long, c_void_p, POINTER(GUID), c_uint32, POINTER(ctypes.c_wchar), POINTER(c_uint32)
                )(GetString_addr.value)
                
                name_buf = ctypes.create_unicode_buffer(name_len.value + 1)
                hr3 = GetString(activate, byref(guid_friendly), name_len.value + 1, name_buf, byref(name_len))
                
                if hr3 == 0:
                    print(f"  [{i}] {name_buf.value}")
                else:
                    print(f"  [{i}] GetString失败: {hr_s(hr3)}")
            else:
                print(f"  [{i}] GetStringLength失败: {hr_s(hr2)}")
    else:
        print("未找到设备")
    
    mfplat.MFShutdown()
    ole32.CoUninitialize()
    print("\n完成!")

if __name__ == "__main__":
    main()
