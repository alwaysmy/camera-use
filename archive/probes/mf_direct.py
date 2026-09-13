"""
mf_direct.py — 直接用MFCreateDeviceSource打开摄像头
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
    print("=== MFCreateDeviceSource 直接打开 ===\n")
    
    # 创建属性存储
    ppAttrs = c_void_p()
    hr = mfplat.MFCreateAttributes(byref(ppAttrs), 4)
    print(f"MFCreateAttributes: {hr_s(hr)}")
    
    # 设置SOURCE_TYPE = VIDCAP
    vtbl = ctypes.cast(ppAttrs, POINTER(c_void_p)).contents.value
    
    SetGUID_addr = ctypes.c_void_p.from_address(vtbl + 22 * ctypes.sizeof(c_void_p))
    SetGUID = ctypes.CFUNCTYPE(ctypes.c_long, c_void_p, POINTER(GUID), POINTER(GUID))(SetGUID_addr.value)
    
    guid_key = make_guid(0x27679CB3, 0x02C0, 0x4491, [0x97, 0x70, 0x08, 0x4B, 0x28, 0x54, 0xFA, 0xBE])
    guid_val = make_guid(0xA6D401E7, 0x5C05, 0x4DE2, [0x98, 0x59, 0x76, 0xC5, 0xF1, 0x02, 0x4E, 0x01])
    
    hr = SetGUID(ppAttrs, byref(guid_key), byref(guid_val))
    print(f"SetGUID(SOURCE_TYPE, VIDCAP): {hr_s(hr)}")
    
    # 设置友好的名称属性
    # MF_DEVSOURCE_ATTRIBUTE_FRIENDLY_NAME
    guid_friendly = make_guid(0x60D81288, 0x76AA, 0x4C63, [0x8B, 0x81, 0xD6, 0x42, 0xB2, 0x0D, 0x48, 0x6E])
    
    # 尝试用MFCreateDeviceSource
    ppSource = c_void_p()
    hr = mf.MFCreateDeviceSource(ppAttrs, byref(ppSource))
    print(f"\nMFCreateDeviceSource: {hr_s(hr)}")
    
    if hr == 0:
        print("成功打开摄像头设备!")
        
        # 获取源的属性
        # IMFMediaSource::GetCharacteristics
        vtbl_src = ctypes.cast(ppSource, POINTER(c_void_p)).contents.value
        
        # 读取特性
        characteristics = c_uint32(0)
        GetChar_addr = ctypes.c_void_p.from_address(vtbl_src + 4 * ctypes.sizeof(c_void_p))
        GetChar = ctypes.CFUNCTYPE(ctypes.c_long, c_void_p, POINTER(c_uint32))(GetChar_addr.value)
        hr = GetChar(ppSource, byref(characteristics))
        print(f"GetCharacteristics: {hr_s(hr)}, 值={characteristics.value:#x}")
        
        # 创建演示描述符
        ppPD = c_void_p()
        CreatePD_addr = ctypes.c_void_p.from_address(vtbl_src + 5 * ctypes.sizeof(c_void_p))
        CreatePD = ctypes.CFUNCTYPE(ctypes.c_long, c_void_p, POINTER(c_void_p))(CreatePD_addr.value)
        hr = CreatePD(ppSource, byref(ppPD))
        print(f"CreatePresentationDescriptor: {hr_s(hr)}")
        
        if hr == 0:
            print("演示描述符创建成功!")
            
            # 获取流的数量
            vtbl_pd = ctypes.cast(ppPD, POINTER(c_void_p)).contents.value
            GetStreamCount_addr = ctypes.c_void_p.from_address(vtbl_pd + 3 * ctypes.sizeof(c_void_p))
            GetStreamCount = ctypes.CFUNCTYPE(ctypes.c_long, c_void_p, POINTER(c_uint32))(GetStreamCount_addr.value)
            
            streamCount = c_uint32(0)
            hr = GetStreamCount(ppPD, byref(streamCount))
            print(f"GetStreamDescriptionCount: {hr_s(hr)}, 流数量={streamCount.value}")
            
            # 获取每个流的描述
            for i in range(streamCount.value):
                # 获取流描述
                streamDesc = (c_byte * 256)()
                streamMediaType = (c_byte * 256)()
                
                GetStreamDesc_addr = ctypes.c_void_p.from_address(vtbl_pd + 4 * ctypes.sizeof(c_void_p))
                # 简化：只打印流数量
                print(f"  流 {i}: 可用")
        
        # 清理
        release_addr = ctypes.c_void_p.from_address(vtbl_src + 2 * ctypes.sizeof(c_void_p))
        Release = ctypes.CFUNCTYPE(ctypes.c_ulong, c_void_p)(release_addr.value)
        Release(ppSource)
    else:
        print("无法打开摄像头设备")
    
    mfplat.MFShutdown()
    ole32.CoUninitialize()
    print("\n完成!")

if __name__ == "__main__":
    main()
