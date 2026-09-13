"""
uvc_probe.py — 用libusb直接与摄像头通信
"""
import usb.core
import usb.util
import time

# UVC请求
UVC_SET_CUR = 0x01
UVC_GET_CUR = 0x81
UVC_GET_MIN = 0x82
UVC_GET_MAX = 0x83
UVC_GET_DEF = 0x87  # Get Default

# Video Streaming Interface Control Selectors
UVC_VS_PROBE_CONTROL = 0x01
UVC_VS_COMMIT_CONTROL = 0x02

# Video Control Interface Selectors
UVC_VC_VIDEO_POWER_MODE = 0x01

def find_camera():
    dev = usb.core.find(idVendor=0x04CA, idProduct=0x7086)
    if dev is None:
        print("未找到摄像头")
        return None
    print(f"找到摄像头: VID:PID={dev.idVendor:04X}:{dev.idProduct:04X}")
    print(f"  Bus: {dev.bus}, Dev: {dev.address}")
    return dev

def list_interfaces(dev):
    print("\n接口列表:")
    for cfg in dev:
        print(f"  配置 {cfg.bConfigurationValue}:")
        for intf in cfg:
            cls_name = {
                0x0E: "Video", 0x01: "Audio", 0x03: "HID", 0x09: "Hub"
            }.get(intf.bInterfaceClass, f"0x{intf.bInterfaceClass:02X}")
            sub_name = {
                0x01: "Control", 0x02: "Streaming", 0x03: "AudioStreaming"
            }.get(intf.bInterfaceSubClass, f"0x{intf.bInterfaceSubClass:02X}")
            
            print(f"    接口 {intf.bInterfaceNumber}: {cls_name}/{sub_name}")
            for ep in intf:
                direction = "IN" if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN else "OUT"
                print(f"      端点 0x{ep.bEndpointAddress:02X} ({direction}) max={ep.wMaxPacketSize}")

def uvc_get(dev, interface_num, selector, length):
    """发送UVC GET请求"""
    wIndex = (interface_num << 8) | 0x00
    data = dev.ctrl_transfer(
        0xA1,  # bmRequestType: IN, Class, Interface
        UVC_GET_CUR,
        selector << 8,
        wIndex,
        length
    )
    return data

def uvc_get_default(dev, interface_num, selector, length):
    """发送UVC GET Default请求"""
    wIndex = (interface_num << 8) | 0x00
    data = dev.ctrl_transfer(
        0xA1,  # bmRequestType: IN, Class, Interface
        UVC_GET_DEF,
        selector << 8,
        wIndex,
        length
    )
    return data

def uvc_set(dev, interface_num, selector, data):
    """发送UVC SET请求"""
    wIndex = (interface_num << 8) | 0x00
    dev.ctrl_transfer(
        0x21,  # bmRequestType: OUT, Class, Interface
        UVC_SET_CUR,
        selector << 8,
        wIndex,
        data
    )

def parse_vs_probe(data):
    """解析VS Probe/Commit Control"""
    if len(data) < 26:
        return {"raw": data.hex(), "length": len(data)}
    
    return {
        "bmHint": data[0] | (data[1] << 8),
        "bFormatIndex": data[2],
        "bFrameIndex": data[3],
        "dwMaxVideoFrameSize": int.from_bytes(data[4:8], 'little'),
        "dwMaxPayloadTransferSize": int.from_bytes(data[8:12], 'little'),
        "dwClockFrequency": int.from_bytes(data[12:16], 'little'),
        "bmFramingInfo": data[16],
        "bPreferedVersion": data[17],
        "bMaxVersion": data[18],
        "bInterfaceNumber": data[19] if len(data) > 19 else None,
        "bmaControls": list(data[20:]) if len(data) > 20 else None,
        "raw_hex": data.hex(),
    }

def main():
    print("=" * 70)
    print("  UVC Probe / Commit 探测")
    print("=" * 70)
    
    dev = find_camera()
    if not dev:
        return
    
    list_interfaces(dev)
    
    # 尝试获取配置
    try:
        dev.set_configuration()
        print("\n配置成功")
    except Exception as e:
        print(f"\n配置失败: {e}")
    
    cfg = dev.get_active_configuration()
    
    # 找到Video Streaming接口 (class=0x0E, subclass=0x02)
    vs_intf_num = None
    vc_intf_num = None
    for intf in cfg:
        if intf.bInterfaceClass == 0x0E:
            if intf.bInterfaceSubClass == 0x01:  # Video Control
                vc_intf_num = intf.bInterfaceNumber
                print(f"\n找到 Video Control 接口: {intf.bInterfaceNumber}")
            elif intf.bInterfaceSubClass == 0x02:  # Video Streaming
                vs_intf_num = intf.bInterfaceNumber
                print(f"找到 Video Streaming 接口: {intf.bInterfaceNumber}")
    
    if vs_intf_num is None:
        print("\n未找到 Video Streaming 接口")
        return
    
    # ── Probe Control ──
    print("\n" + "=" * 50)
    print("  VS Probe Control")
    print("=" * 50)
    
    try:
        # GET_CUR
        probe_cur = uvc_get(dev, vs_intf_num, UVC_VS_PROBE_CONTROL, 26)
        print(f"\nGET_CUR (当前值):")
        parsed = parse_vs_probe(probe_cur)
        for k, v in parsed.items():
            if k != "raw_hex":
                print(f"  {k:30s}: {v}")
        
        # GET_MIN
        try:
            probe_min = uvc_get(dev, vs_intf_num, UVC_VS_PROBE_CONTROL, 26)
            # 先设置为MIN
            uvc_set(dev, vs_intf_num, UVC_VS_PROBE_CONTROL, probe_min)
            print(f"\n设置为MIN后 GET_CUR:")
            probe_after_min = uvc_get(dev, vs_intf_num, UVC_VS_PROBE_CONTROL, 26)
            parsed_min = parse_vs_probe(probe_after_min)
            for k, v in parsed_min.items():
                if k != "raw_hex":
                    print(f"  {k:30s}: {v}")
            
            # 恢复
            uvc_set(dev, vs_intf_num, UVC_VS_PROBE_CONTROL, probe_cur)
        except Exception as e:
            print(f"  GET_MIN 失败: {e}")
        
        # GET_MAX
        try:
            probe_max = uvc_get(dev, vs_intf_num, UVC_VS_PROBE_CONTROL, 26)
            print(f"\nGET_MAX:")
            parsed_max = parse_vs_probe(probe_max)
            for k, v in parsed_max.items():
                if k != "raw_hex":
                    print(f"  {k:30s}: {v}")
        except Exception as e:
            print(f"  GET_MAX 失败: {e}")
            
        # GET_DEF
        try:
            probe_def = uvc_get_default(dev, vs_intf_num, UVC_VS_PROBE_CONTROL, 26)
            print(f"\nGET_DEF (默认值):")
            parsed_def = parse_vs_probe(probe_def)
            for k, v in parsed_def.items():
                if k != "raw_hex":
                    print(f"  {k:30s}: {v}")
        except Exception as e:
            print(f"  GET_DEF 失败: {e}")
        
    except Exception as e:
        print(f"Probe Control 失败: {e}")
    
    # ── 保存结果 ──
    print("\n" + "=" * 70)
    print("  对比: Windows相机打开前后")
    print("=" * 70)
    print("请手动执行:")
    print("  1. 记录当前GET_CUR值")
    print("  2. 打开Windows相机")
    print("  3. 关闭Windows相机")
    print("  4. 再次GET_CUR")
    print("  5. 对比差异")
    
    # 自动测试: 读取 -> 打开相机 -> 读取
    print("\n\n自动测试流程:")
    
    print("\n[A] 当前 Probe GET_CUR:")
    data_a = uvc_get(dev, vs_intf_num, UVC_VS_PROBE_CONTROL, 26)
    print(f"  hex: {data_a.hex()}")
    
    print("\n[B] 启动Windows相机...")
    import subprocess
    subprocess.Popen(["start", "microsoft.windows.camera:"], shell=True)
    time.sleep(4)
    
    print("[C] 相机运行中 Probe GET_CUR:")
    try:
        data_c = uvc_get(dev, vs_intf_num, UVC_VS_PROBE_CONTROL, 26)
        print(f"  hex: {data_c.hex()}")
    except Exception as e:
        print(f"  失败: {e}")
    
    print("\n[D] 关闭Windows相机...")
    subprocess.run(["taskkill", "/IM", "Windows.Camera.exe", "/F"], capture_output=True)
    time.sleep(2)
    
    print("[E] 关闭后 Probe GET_CUR:")
    try:
        data_e = uvc_get(dev, vs_intf_num, UVC_VS_PROBE_CONTROL, 26)
        print(f"  hex: {data_e.hex()}")
    except Exception as e:
        print(f"  失败: {e}")
    
    # 对比
    print("\n[F] 差异分析:")
    if data_a.hex() == data_c.hex():
        print("  A == C: Probe值在相机打开时没有变化")
    else:
        print("  A != C: Probe值在相机打开时发生了变化!")
        for i in range(min(len(data_a), len(data_c))):
            if data_a[i] != data_c[i]:
                print(f"    字节[{i}]: 0x{data_a[i]:02X} -> 0x{data_c[i]:02X}")
    
    if data_a.hex() == data_e.hex():
        print("  A == E: Probe值在相机关闭后恢复")
    else:
        print("  A != E: Probe值在相机关闭后未恢复!")

if __name__ == "__main__":
    main()
