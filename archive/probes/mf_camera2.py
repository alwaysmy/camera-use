"""
mf_camera2.py — 用comtypes调用Media Foundation API
"""
import comtypes
from comtypes import GUID as COM_GUID
import time
import subprocess

def close_camera():
    subprocess.run(["taskkill", "/IM", "Windows.Camera.exe", "/F"], capture_output=True)
    time.sleep(1)

def main():
    close_camera()
    
    print("=== Media Foundation 设备枚举 ===\n")
    
    # 初始化COM
    import comtypes.client
    comtypes.client.CoInitialize()
    
    try:
        # 加载Media Foundation
        import ctypes
        mf = ctypes.windll.mf
        mfplat = ctypes.windll.mfplat
        
        # 初始化MF
        hr = mfplat.MFStartup(0x02000000, 0)
        print(f"MFStartup: 0x{hr:08X}")
        
        # 创建属性存储
        from ctypes import c_void_p, pointer, byref, c_uint32
        
        ppAttributes = c_void_p()
        hr = mfplat.MFCreateAttributes(byref(ppAttributes), 2)
        print(f"MFCreateAttributes: 0x{hr:08X}")
        
        if hr != 0:
            print("无法创建属性存储")
            return
        
        # 设置设备类型为视频捕获
        # MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE = GUID
        SOURCE_TYPE_VIDCAP = COM_GUID('{27679CB3-02C0-4491-9770-084B2854FABE}')
        
        # 使用IMFAttributes接口
        from comtypes import IUnknown
        import comtypes.client
        
        # 使用win32com来访问
        import win32com.client
        
        # 创建MFAttributes
        attrs = win32com.client.Dispatch("MFAttributes.1")
        print(f"MFAttributes创建成功")
        
        # 设置源类型
        # 使用MFCreateDeviceSource
        
        # 尝试直接用MFCreateDeviceSources
        print("\n尝试MFCreateDeviceSources...")
        
        # 需要先设置属性
        # MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE
        attrs.SetGUID(
            '{27679CB3-02C0-4491-9770-084B2854FABE}',  # MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE
            '{A6D401E7-5C05-4DE2-9859-76C5F1024E01}'   # MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_GUID
        )
        
        print("属性设置成功")
        
    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()
    finally:
        comtypes.client.CoUninitialize()

if __name__ == "__main__":
    main()
