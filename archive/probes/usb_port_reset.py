"""
usb_port_reset.py — 通过Windows API做真正的USB端口重置
"""
import ctypes
from ctypes import wintypes
import time
import subprocess

# SetupAPI
setupapi = ctypes.windll.setupapi
cfgmgr32 = ctypes.windll.cfgmgr32

# 常量
DIGCF_PRESENT = 0x02
CR_SUCCESS = 0
CM_LOCATE_DEVNODE_PHANTOM = 0x00000001
CM_REMOVE_UI_NOT_OK = 0x00000002

class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_byte * 8),
    ]

class SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("ClassGuid", GUID),
        ("DevInst", wintypes.DWORD),
        ("Reserved", ctypes.POINTER(ctypes.c_ulong)),
    ]

def find_camera_devinst():
    """找到摄像头的DevInst"""
    GUID_DEVCLASS_CAMERA = GUID()
    GUID_DEVCLASS_CAMERA.Data1 = 0xca3e7ab9
    GUID_DEVCLASS_CAMERA.Data2 = 0xb4c3
    GUID_DEVCLASS_CAMERA.Data3 = 0x4ae6
    for i, b in enumerate([0x82, 0x51, 0x57, 0x9e, 0xf9, 0x33, 0x89, 0x0f]):
        GUID_DEVCLASS_CAMERA.Data4[i] = b

    dev_info = setupapi.SetupDiGetClassDevsW(
        ctypes.byref(GUID_DEVCLASS_CAMERA), None, None, DIGCF_PRESENT
    )
    
    dev_data = SP_DEVINFO_DATA()
    dev_data.cbSize = ctypes.sizeof(SP_DEVINFO_DATA)
    
    cameras = []
    idx = 0
    while setupapi.SetupDiEnumDeviceInfo(dev_info, idx, ctypes.byref(dev_data)):
        idx += 1
        
        buf = ctypes.create_unicode_buffer(512)
        setupapi.SetupDiGetDeviceRegistryPropertyW(
            dev_info, ctypes.byref(dev_data), 0x0C, None, buf, 512, None
        )
        
        cameras.append({
            "name": buf.value,
            "devinst": dev_data.DevInst,
        })
        
        dev_data = SP_DEVINFO_DATA()
        dev_data.cbSize = ctypes.sizeof(SP_DEVINFO_DATA)
    
    setupapi.SetupDiDestroyDeviceInfoList(dev_info)
    return cameras

def reset_camera():
    """尝试重置摄像头"""
    cameras = find_camera_devinst()
    print(f"找到 {len(cameras)} 个摄像头:")
    for cam in cameras:
        print(f"  {cam['name']} (DevInst={cam['devinst']})")
    
    if not cameras:
        return False
    
    devinst = cameras[0]["devinst"]
    
    # 方法1: SetupDiCallClassInstaller with DIF_PROPERTYCHANGE
    print("\n尝试方法1: SetupDiCallClassInstaller...")
    # 需要更多设置，先跳过
    
    # 方法2: 用cfgmgr32
    print("\n尝试方法2: cfgmgr32 DevNode状态变更...")
    
    # CM_Set_DevNode_Problem
    ret = cfgmgr32.CM_Set_DevNode_Problem(devinst, 0x18, 0)  # CM_PROB_DISABLED
    print(f"  CM_Set_DevNode_Problem(Disable): 返回={ret}")
    
    time.sleep(2)
    
    ret = cfgmgr32.CM_Set_DevNode_Problem(devinst, 0, 0)  # 清除问题
    print(f"  CM_Set_DevNode_Problem(Clear): 返回={ret}")
    
    time.sleep(5)
    
    return True

def main():
    print("=" * 60)
    print("  USB端口重置测试")
    print("=" * 60)
    
    reset_camera()
    
    # 测试
    print("\n测试摄像头...")
    import cv2
    cap = cv2.VideoCapture(0)
    time.sleep(1)
    
    for i in range(5):
        ret, frame = cap.read()
        if ret:
            print(f"  帧 {i+1}: mean={frame.mean():.1f}")
        else:
            print(f"  帧 {i+1}: 失败")
    
    cap.release()

if __name__ == "__main__":
    main()
