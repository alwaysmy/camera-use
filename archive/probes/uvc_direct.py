"""
uvc_direct.py — 直接用pyusb发送UVC控制请求
目标：手动发送Windows相机发送的命令
"""
import usb.core
import usb.util
import time

# UVC请求代码
UVC_SET_CUR = 0x01
UVC_GET_CUR = 0x81
UVC_GET_MIN = 0x82
UVC_GET_MAX = 0x83
UVC_GET_RES = 0x84
UVC_GET_LEN = 0x85
UVC_GET_INFO = 0x86

# Video Streaming Interface Control Selectors
UVC_VS_PROBE_CONTROL = 0x01
UVC_VS_COMMIT_CONTROL = 0x02

# Video Control Interface Selectors  
UVC_VC_VIDEO_POWER_MODE = 0x01
UVC_VC_REQUEST_ERROR_CODE_CONTROL = 0x02

def find_camera():
    """查找摄像头设备"""
    dev = usb.core.find(idVendor=0x04CA, idProduct=0x7086)
    if dev is None:
        print("未找到摄像头")
        return None
    
    print(f"找到摄像头: VID:PID = {dev.idVendor:04X}:{dev.idProduct:04X}")
    print(f"  配置数: {dev.bNumConfigurations}")
    
    # 设置后端
    try:
        dev.set_configuration()
        print(f"  配置成功")
    except Exception as e:
        print(f"  配置失败: {e}")
    
    return dev

def list_interfaces(dev):
    """列出所有接口"""
    print("\n接口列表:")
    for cfg in dev:
        print(f"  配置 {cfg.bConfigurationValue}:")
        for intf in cfg:
            print(f"    接口 {intf.bInterfaceNumber}: "
                  f"类={intf.bInterfaceClass:02X} "
                  f"子类={intf.bInterfaceSubClass:02X} "
                  f"协议={intf.bInterfaceProtocol:02X}")
            
            # 列出端点
            for ep in intf:
                direction = "IN" if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN else "OUT"
                print(f"      端点 {ep.bEndpointAddress:02X} ({direction}) "
                      f"类型={ep.bmAttributes:02X} 最大包长={ep.wMaxPacketSize}")

def get_video_streaming_interface(dev):
    """获取Video Streaming接口"""
    for cfg in dev:
        for intf in cfg:
            # Video Streaming接口通常是 class=0x0E, subclass=0x02
            if intf.bInterfaceClass == 0x0E and intf.bInterfaceSubClass == 0x02:
                return intf
    return None

def probe_commit(dev, intf, selector):
    """发送Probe或Commit控制请求"""
    # UVC Video Streaming Interface Control Request
    # bmRequestType: 0xA1 (IN, Class, Interface)
    # bRequest: 0x01 (SET_CUR) or 0x81 (GET_CUR)
    # wValue: selector << 8
    # wIndex: interface number
    # wLength: 26 (Video Streaming Probe/Commit Control size)
    
    wIndex = intf.bInterfaceNumber << 8  # Interface number in high byte
    
    # 先GET_CUR获取当前值
    try:
        data = dev.ctrl_transfer(
            0xA1,  # bmRequestType: IN, Class, Interface
            UVC_GET_CUR,
            selector << 8,
            wIndex,
            26  # VS Probe/Commit Control size
        )
        print(f"\nGET_CUR (selector=0x{selector:02X}):")
        print(f"  原始数据: {data.hex()}")
        print(f"  解析: {parse_probe_control(data)}")
        return data
    except Exception as e:
        print(f"GET_CUR 失败: {e}")
        return None

def parse_probe_control(data):
    """解析UVC Probe/Commit Control数据"""
    if len(data) < 26:
        return "数据太短"
    
    # 按照UVC规范解析
    result = {
        "bmHint": int.from_bytes(data[0:2], 'little'),
        "bFormatIndex": data[2],
        "bFrameIndex": data[3],
        "dwMaxVideoFrameSize": int.from_bytes(data[4:8], 'little'),
        "dwMaxPayloadTransferSize": int.from_bytes(data[8:12], 'little'),
        "dwClockFrequency": int.from_bytes(data[12:16], 'little'),
        "bmFramingInfo": data[16],
        "bPreferedVersion": data[17],
        "bMaxVersion": data[18],
        "bInterfaceNumber": data[19],
    }
    return result

def set_probe_control(dev, intf, data):
    """设置Probe控制"""
    wIndex = intf.bInterfaceNumber << 8
    
    try:
        dev.ctrl_transfer(
            0x21,  # bmRequestType: OUT, Class, Interface
            UVC_SET_CUR,
            UVC_VS_PROBE_CONTROL << 8,
            wIndex,
            data
        )
        print("SET_CUR (Probe) 成功")
        return True
    except Exception as e:
        print(f"SET_CUR (Probe) 失败: {e}")
        return False

def main():
    print("=" * 70)
    print("  UVC直接控制测试")
    print("=" * 70)
    
    # 查找摄像头
    dev = find_camera()
    if not dev:
        return
    
    # 列出接口
    list_interfaces(dev)
    
    # 获取Video Streaming接口
    vs_intf = get_video_streaming_interface(dev)
    if not vs_intf:
        print("\n未找到Video Streaming接口，尝试接口0...")
        # 尝试直接用接口0
        dev.set_configuration()
        cfg = dev.get_active_configuration()
        vs_intf = cfg[(0, 0)]
    
    print(f"\n使用接口: {vs_intf.bInterfaceNumber}")
    
    # 获取当前Probe控制
    print("\n--- 测试Probe控制 ---")
    probe_data = probe_commit(dev, vs_intf, UVC_VS_PROBE_CONTROL)
    
    # 获取当前Commit控制
    print("\n--- 测试Commit控制 ---")
    commit_data = probe_commit(dev, vs_intf, UVC_VS_COMMIT_CONTROL)
    
    # 尝试修改Probe参数并发送
    if probe_data:
        print("\n--- 尝试修改Probe参数 ---")
        new_probe = bytearray(probe_data)
        
        # 修改一些参数试试
        # bFrameIndex: 帧索引
        # dwMaxVideoFrameSize: 最大帧大小
        print(f"原始: {new_probe.hex()}")
        
        # 尝试设置
        set_probe_control(dev, vs_intf, bytes(new_probe))
        
        # 再次读取
        print("\n修改后重新读取:")
        probe_commit(dev, vs_intf, UVC_VS_PROBE_CONTROL)
    
    # 释放接口
    try:
        usb.util.dispose_resources(dev)
    except:
        pass
    
    print("\n" + "=" * 70)
    print("  测试完成")
    print("=" * 70)

if __name__ == "__main__":
    main()
