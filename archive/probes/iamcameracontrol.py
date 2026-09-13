"""
iamcameracontrol.py — 通过IAMCameraControl接口正确设置UVC曝光
根本原因: OpenCV只设置值，不设置Auto/Manual标志
"""
import win32com.client
import pythoncom
import time
import subprocess
import ctypes
from ctypes import wintypes, byref, c_void_p, POINTER

# DirectShow GUIDs
CLSID_SystemDeviceEnum = "{62BE5D10-60FE-11CF-AF3A-00AA0037B9DC}"
CLSID_VideoInputDeviceCategory = "{860BB310-5D31-11D2-9AFA-0060979423D0}"
IID_ICreateDevEnum = "{29840822-5B84-11D0-BD3B-00A0C911CE86}"
IID_IEnumMoniker = "{55272A00-42CB-11CE-8135-00AA004BB852}"
IID_IMoniker = "{00000002-0000-0000-C000-000000000046}"
IID_IBaseFilter = "{56A86895-0AD4-11CE-B03A-0020AF0BA770}"
IID_IAMCameraControl = "{C6E13370-36AC-11D2-B40B-00A0C90F2719}"

# CameraControl属性
CameraControl_Exposure = 0
CameraControl_Focus = 2
CameraControl_Pan = 3
CameraControl_Tilt = 4
CameraControl_Roll = 5
CameraControl_Zoom = 6
CameraControl_Iris = 7

# CameraControl标志
CameraControl_Flags_Auto = 1
CameraControl_Flags_Manual = 2

def close_camera():
    subprocess.run(["taskkill", "/IM", "Windows.Camera.exe", "/F"], capture_output=True)
    time.sleep(1)

def main():
    close_camera()
    
    print("=" * 60)
    print("  IAMCameraControl — 正确的UVC曝光设置")
    print("=" * 60)
    
    # 初始化COM
    pythoncom.CoInitialize()
    
    try:
        # 创建设备枚举器
        print("\n[1] 枚举DirectShow设备...")
        dev_enum = win32com.client.Dispatch(CLSID_SystemDeviceEnum)
        cat_enum = dev_enum.CreateClassEnumerator(CLSID_VideoInputDeviceCategory, 0)
        
        if cat_enum is None:
            print("  未找到视频设备")
            return
        
        # 枚举设备
        cameras = []
        while True:
            moniker = cat_enum.Next()
            if moniker is None:
                break
            
            try:
                name = moniker.GetDisplayName(0, 0)
                cameras.append((name, moniker))
                print(f"  找到: {name[:60]}")
            except:
                pass
        
        if not cameras:
            print("  未找到摄像头")
            return
        
        # 选择第一个摄像头
        print(f"\n[2] 打开摄像头: {cameras[0][0][:50]}...")
        moniker = cameras[0][1]
        
        # 绑定到过滤器
        try:
            # 尝试获取IBaseFilter
            # 注意: 这里可能需要不同的方法
            print("  尝试绑定到IBaseFilter...")
            
            # 使用更直接的方法 - 通过pywin32的EnsureDispatch
            filter_obj = moniker.BindToObject(0, 0, "{56A86895-0AD4-11CE-B03A-0020AF0BA770}")
            print(f"  过滤器: {filter_obj}")
            
            # 查询IAMCameraControl接口
            # pywin32不支持直接QueryInterface，需要用ctypes
            print("\n[3] 使用ctypes调用IAMCameraControl...")
            
            # 获取过滤器的vtable
            filter_ptr = ctypes.cast(int(filter_obj), c_void_p).value
            
            # IUnknown::QueryInterface (vtable index 0)
            qi_vtable = ctypes.c_void_p.from_address(filter_ptr)
            qi_func = ctypes.CFUNCTYPE(
                ctypes.c_long, c_void_p, POINTER(wintypes.GUID), POINTER(c_void_p)
            )(qi_vtable.value)
            
            # IID_IAMCameraControl
            iid_cc = wintypes.GUID()
            iid_cc.Data1 = 0xC6E13370
            iid_cc.Data2 = 0x36AC
            iid_cc.Data3 = 0x11D2
            for i, b in enumerate([0xB4, 0x0B, 0x00, 0xA0, 0xC9, 0x0F, 0x27, 0x19]):
                iid_cc.Data4[i] = b
            
            cc_ptr = c_void_p()
            hr = qi_func(filter_ptr, byref(iid_cc), byref(cc_ptr))
            print(f"  QueryInterface(IAMCameraControl): 0x{hr & 0xFFFFFFFF:08X}")
            
            if hr == 0 and cc_ptr.value:
                print("  IAMCameraControl接口获取成功!")
                
                # IAMCameraControl vtable:
                # 0-2: IUnknown (QueryInterface, AddRef, Release)
                # 3: GetRange
                # 4: Set
                # 5: Get
                
                # 先GetRange
                GetRange_addr = ctypes.c_void_p.from_address(cc_ptr.value + 3 * ctypes.sizeof(c_void_p))
                GetRange = ctypes.CFUNCTYPE(
                    ctypes.c_long, c_void_p, ctypes.c_long,
                    POINTER(ctypes.c_long), POINTER(ctypes.c_long), 
                    POINTER(ctypes.c_long), POINTER(ctypes.c_long),
                    POINTER(ctypes.c_long)
                )(GetRange_addr.value)
                
                min_val = ctypes.c_long()
                max_val = ctypes.c_long()
                step = ctypes.c_long()
                default = ctypes.c_long()
                caps = ctypes.c_long()
                
                hr = GetRange(cc_ptr, CameraControl_Exposure, 
                            byref(min_val), byref(max_val), byref(step),
                            byref(default), byref(caps))
                
                if hr == 0:
                    print(f"\n  曝光范围: {min_val.value} ~ {max_val.value}, 步进={step.value}, 默认={default.value}")
                    print(f"  支持模式: {'Auto' if caps.value & CameraControl_Flags_Auto else ''} {'Manual' if caps.value & CameraControl_Flags_Manual else ''}")
                    
                    # Set方法
                    Set_addr = ctypes.c_void_p.from_address(cc_ptr.value + 4 * ctypes.sizeof(c_void_p))
                    Set_func = ctypes.CFUNCTYPE(
                        ctypes.c_long, c_void_p, ctypes.c_long, ctypes.c_long, ctypes.c_long
                    )(Set_addr.value)
                    
                    # Get方法
                    Get_addr = ctypes.c_void_p.from_address(cc_ptr.value + 5 * ctypes.sizeof(c_void_p))
                    Get_func = ctypes.CFUNCTYPE(
                        ctypes.c_long, c_void_p, ctypes.c_long,
                        POINTER(ctypes.c_long), POINTER(ctypes.c_long)
                    )(Get_addr.value)
                    
                    # 读取当前值
                    current_val = ctypes.c_long()
                    current_flags = ctypes.c_long()
                    hr = Get_func(cc_ptr, CameraControl_Exposure, byref(current_val), byref(current_flags))
                    if hr == 0:
                        flag_name = "Auto" if current_flags.value == CameraControl_Flags_Auto else "Manual"
                        print(f"\n  当前曝光: 值={current_val.value}, 模式={flag_name}")
                    
                    # ── 关键测试 ──
                    print("\n[4] 测试不同模式...")
                    
                    # 测试1: 设置Auto模式
                    print("\n  测试1: Auto模式")
                    hr = Set_func(cc_ptr, CameraControl_Exposure, 0, CameraControl_Flags_Auto)
                    print(f"    Set(Auto): 0x{hr & 0xFFFFFFFF:08X}")
                    time.sleep(1)
                    
                    # 读取并拍照
                    hr = Get_func(cc_ptr, CameraControl_Exposure, byref(current_val), byref(current_flags))
                    if hr == 0:
                        flag_name = "Auto" if current_flags.value == CameraControl_Flags_Auto else "Manual"
                        print(f"    当前: 值={current_val.value}, 模式={flag_name}")
                    
                    import cv2
                    cap = cv2.VideoCapture(0)
                    time.sleep(1)
                    ret, frame = cap.read()
                    if ret:
                        cv2.imwrite('exposure_auto.jpg', frame)
                        print(f"    均值: {frame.mean():.1f}")
                    cap.release()
                    
                    # 测试2: 设置Manual模式
                    print("\n  测试2: Manual模式 (-5)")
                    hr = Set_func(cc_ptr, CameraControl_Exposure, -5, CameraControl_Flags_Manual)
                    print(f"    Set(Manual, -5): 0x{hr & 0xFFFFFFFF:08X}")
                    time.sleep(1)
                    
                    hr = Get_func(cc_ptr, CameraControl_Exposure, byref(current_val), byref(current_flags))
                    if hr == 0:
                        flag_name = "Auto" if current_flags.value == CameraControl_Flags_Auto else "Manual"
                        print(f"    当前: 值={current_val.value}, 模式={flag_name}")
                    
                    cap = cv2.VideoCapture(0)
                    time.sleep(1)
                    ret, frame = cap.read()
                    if ret:
                        cv2.imwrite('exposure_manual.jpg', frame)
                        print(f"    均值: {frame.mean():.1f}")
                    cap.release()
                    
                    # 测试3: 设置Manual模式 (-2)
                    print("\n  测试3: Manual模式 (-2)")
                    hr = Set_func(cc_ptr, CameraControl_Exposure, -2, CameraControl_Flags_Manual)
                    print(f"    Set(Manual, -2): 0x{hr & 0xFFFFFFFF:08X}")
                    time.sleep(1)
                    
                    cap = cv2.VideoCapture(0)
                    time.sleep(1)
                    ret, frame = cap.read()
                    if ret:
                        cv2.imwrite('exposure_manual2.jpg', frame)
                        print(f"    均值: {frame.mean():.1f}")
                    cap.release()
                    
                    # 测试4: 恢复Auto
                    print("\n  测试4: 恢复Auto模式")
                    hr = Set_func(cc_ptr, CameraControl_Exposure, 0, CameraControl_Flags_Auto)
                    print(f"    Set(Auto): 0x{hr & 0xFFFFFFFF:08X}")
                    time.sleep(1)
                    
                    cap = cv2.VideoCapture(0)
                    time.sleep(1)
                    ret, frame = cap.read()
                    if ret:
                        cv2.imwrite('exposure_auto_restored.jpg', frame)
                        print(f"    均值: {frame.mean():.1f}")
                    cap.release()
                else:
                    print(f"  GetRange失败: 0x{hr & 0xFFFFFFFF:08X}")
            else:
                print(f"  获取IAMCameraControl失败")
        
        except Exception as e:
            print(f"  错误: {e}")
            import traceback
            traceback.print_exc()
    
    finally:
        pythoncom.CoUninitialize()
        print("\n完成!")

if __name__ == "__main__":
    main()
