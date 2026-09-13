"""
directshow_test.py — 直接调用DirectShow接口
"""
import win32com.client
import pythoncom
import time

# 初始化COM
pythoncom.CoInitialize()

try:
    # 创建 Filter Graph Manager
    print("=== 创建 DirectShow Filter Graph ===")
    fg = win32com.client.Dispatch("DShow.FilterGraph.1")
    print(f"FilterGraph: {fg}")
    
    # 创建系统设备枚举器
    print("\n=== 枚举视频设备 ===")
    dev_enum = win32com.client.Dispatch("DShow.SystemDeviceEnum.1")
    
    # 视频输入设备类别 GUID
    # CLSID_VideoInputDeviceCategory = {860BB310-5D31-11D2-9AFA-0060979423D0}
    cat_guid = "{860BB310-5D31-11D2-9AFA-0060979423D0}"
    
    try:
        cat_enum = dev_enum.CreateClassEnumerator(cat_guid, 0)
        print(f"ClassEnumerator: {cat_enum}")
        
        if cat_enum:
            idx = 0
            while True:
                try:
                    moniker = cat_enum.Next()
                    if moniker is None:
                        break
                    
                    # 获取设备名称
                    try:
                        name = moniker.GetDisplayName(0, 0)
                        print(f"\n设备 {idx}: {name}")
                    except:
                        print(f"\n设备 {idx}: (无法获取名称)")
                    
                    # 绑定到Filter
                    try:
                        filter_obj = moniker.BindToObject(0, 0, "DShow.IBaseFilter.1")
                        print(f"  Filter: {filter_obj}")
                        
                        # 查询接口
                        try:
                            proc_amp = filter_obj.QueryInterface("DShow.IAMVideoProcAmp.1")
                            print(f"  VideoProcAmp: {proc_amp}")
                            
                            # 读取属性
                            for prop_id in range(0, 20):
                                try:
                                    val, flags = proc_amp.Get(prop_id, 0)
                                    print(f"    Property {prop_id}: value={val}, flags={flags}")
                                except:
                                    pass
                        except Exception as e:
                            print(f"  VideoProcAmp 查询失败: {e}")
                    
                    except Exception as e:
                        print(f"  绑定失败: {e}")
                    
                    idx += 1
                except Exception as e:
                    print(f"  枚举错误: {e}")
                    break
        else:
            print("未找到视频设备枚举器")
    
    except Exception as e:
        print(f"创建枚举器失败: {e}")

finally:
    pythoncom.CoUninitialize()
    print("\n=== 完成 ===")
