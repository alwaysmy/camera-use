"""
uvc_probe2.py — UVC探测（正确处理设备占用）
"""
import usb.core
import usb.util
import subprocess
import time

UVC_SET_CUR = 0x01
UVC_GET_CUR = 0x81
UVC_GET_DEF = 0x87
UVC_VS_PROBE_CONTROL = 0x01
UVC_VS_COMMIT_CONTROL = 0x02

def close_camera_app():
    """确保Windows相机关闭"""
    subprocess.run(["taskkill", "/IM", "Windows.Camera.exe", "/F"], capture_output=True)
    time.sleep(1)

def find_camera():
    dev = usb.core.find(idVendor=0x04CA, idProduct=0x7086)
    return dev

def uvc_get(dev, iface_num, selector, length):
    wIndex = (iface_num << 8) | 0x00
    return dev.ctrl_transfer(0xA1, UVC_GET_CUR, selector << 8, wIndex, length)

def uvc_set(dev, iface_num, selector, data):
    wIndex = (iface_num << 8) | 0x00
    dev.ctrl_transfer(0x21, UVC_SET_CUR, selector << 8, wIndex, data)

def parse_probe(d):
    if len(d) < 26:
        return d.hex()
    return {
        "FormatIndex": d[2],
        "FrameIndex": d[3],
        "MaxFrameSize": int.from_bytes(d[4:8], 'little'),
        "MaxPayload": int.from_bytes(d[8:12], 'little'),
        "raw": d.hex(),
    }

def main():
    print("=" * 60)
    print("  UVC Probe 测试")
    print("=" * 60)
    
    # 确保相机关闭
    print("\n[0] 关闭Windows相机...")
    close_camera_app()
    
    # 找设备
    dev = find_camera()
    if not dev:
        print("未找到摄像头")
        return
    
    print(f"  VID:PID = {dev.idVendor:04X}:{dev.idProduct:04X}")
    
    # 打开设备
    try:
        dev.set_configuration()
    except:
        pass
    
    # Video Streaming 接口
    vs_intf = 1
    
    # ── 阶段A: 没有Windows相机的基准值 ──
    print("\n[A] 关闭状态下 Probe GET_CUR:")
    try:
        data_a = uvc_get(dev, vs_intf, UVC_VS_PROBE_CONTROL, 26)
        print(f"  {parse_probe(data_a)}")
    except Exception as e:
        print(f"  失败: {e}")
        return
    
    # ── 阶段B: 打开Windows相机 ──
    print("\n[B] 打开Windows相机...")
    subprocess.Popen(["start", "microsoft.windows.camera:"], shell=True)
    time.sleep(4)
    
    # ── 阶段C: 相机运行时读取 ──
    print("[C] 相机运行时 Probe GET_CUR:")
    try:
        data_c = uvc_get(dev, vs_intf, UVC_VS_PROBE_CONTROL, 26)
        print(f"  {parse_probe(data_c)}")
    except Exception as e:
        print(f"  失败: {e}")
    
    # ── 阶段D: 关闭相机 ──
    print("\n[D] 关闭Windows相机...")
    close_camera_app()
    
    # ── 阶段E: 关闭后读取 ──
    print("[E] 关闭后 Probe GET_CUR:")
    try:
        data_e = uvc_get(dev, vs_intf, UVC_VS_PROBE_CONTROL, 26)
        print(f"  {parse_probe(data_e)}")
    except Exception as e:
        print(f"  失败: {e}")
    
    # ── 对比 ──
    print("\n" + "=" * 60)
    print("  差异分析")
    print("=" * 60)
    
    if data_a.hex() == data_c.hex():
        print("A == C: Probe在相机打开时没变化")
    else:
        print("A != C: Probe在相机打开时变化了!")
        for i in range(min(len(data_a), len(data_c))):
            if data_a[i] != data_c[i]:
                print(f"  byte[{i}]: 0x{data_a[i]:02X} -> 0x{data_c[i]:02X}")
    
    if data_a.hex() == data_e.hex():
        print("A == E: Probe在相机关闭后恢复")
    else:
        print("A != E: Probe在相机关闭后未恢复!")
        for i in range(min(len(data_a), len(data_e))):
            if data_a[i] != data_e[i]:
                print(f"  byte[{i}]: 0x{data_a[i]:02X} -> 0x{data_e[i]:02X}")

if __name__ == "__main__":
    main()
