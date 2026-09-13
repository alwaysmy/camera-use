"""
uvc_probe3.py — 通过claim_interface直接通信
"""
import usb.core
import usb.util
import subprocess
import time

UVC_GET_CUR = 0x81
UVC_SET_CUR = 0x01
UVC_VS_PROBE_CONTROL = 0x01

def close_camera_app():
    subprocess.run(["taskkill", "/IM", "Windows.Camera.exe", "/F"], capture_output=True)
    time.sleep(1)

def main():
    print("=" * 60)
    print("  UVC Probe — 直接claim接口")
    print("=" * 60)
    
    close_camera_app()
    
    dev = usb.core.find(idVendor=0x04CA, idProduct=0x7086)
    if not dev:
        print("未找到摄像头")
        return
    
    print(f"VID:PID = {dev.idVendor:04X}:{dev.idProduct:04X}")
    
    # 不调用 set_configuration
    # 尝试直接 ctrl_transfer
    print("\n尝试直接控制传输...")
    
    # UVC Video Streaming Probe Control
    # bmRequestType = 0xA1 (IN | Class | Interface)
    # bRequest = 0x81 (GET_CUR)
    # wValue = 0x0100 (VS_PROBE_CONTROL << 8)
    # wIndex = 0x0100 (interface 1, 0 in low byte)
    # wLength = 26
    
    tests = [
        ("iface=1, probe", 1, 0x0100),
        ("iface=1, commit", 1, 0x0200),
        ("iface=3, probe", 3, 0x0100),
        ("iface=3, commit", 3, 0x0200),
    ]
    
    for label, iface, wValue in tests:
        try:
            data = dev.ctrl_transfer(
                0xA1,       # bmRequestType
                UVC_GET_CUR,  # bRequest
                wValue,     # wValue
                (iface << 8),  # wIndex
                26          # wLength
            )
            print(f"\n{label}:")
            print(f"  长度={len(data)} hex={data.hex()}")
            if len(data) >= 8:
                print(f"  FormatIndex={data[2]} FrameIndex={data[3]}")
                print(f"  MaxFrameSize={int.from_bytes(data[4:8], 'little')}")
        except Exception as e:
            print(f"\n{label}: 失败 - {e}")

    # ── 关键测试：打开Windows相机后对比 ──
    print("\n\n" + "=" * 60)
    print("  A/B测试: Windows相机的影响")
    print("=" * 60)
    
    # A: 当前状态
    print("\n[A] 当前 Probe (iface=1):")
    try:
        data_a = dev.ctrl_transfer(0xA1, UVC_GET_CUR, 0x0100, 0x0100, 26)
        print(f"  hex: {data_a.hex()}")
    except Exception as e:
        print(f"  失败: {e}")
        return
    
    # B: 打开相机
    print("\n[B] 打开Windows相机...")
    subprocess.Popen(["start", "microsoft.windows.camera:"], shell=True)
    time.sleep(4)
    
    # C: 相机运行中
    print("[C] 相机运行中 Probe (iface=1):")
    try:
        data_c = dev.ctrl_transfer(0xA1, UVC_GET_CUR, 0x0100, 0x0100, 26)
        print(f"  hex: {data_c.hex()}")
    except Exception as e:
        print(f"  失败: {e}")
    
    # D: 关闭相机
    print("\n[D] 关闭Windows相机...")
    close_camera_app()
    
    # E: 关闭后
    print("[E] 关闭后 Probe (iface=1):")
    try:
        data_e = dev.ctrl_transfer(0xA1, UVC_GET_CUR, 0x0100, 0x0100, 26)
        print(f"  hex: {data_e.hex()}")
    except Exception as e:
        print(f"  失败: {e}")
    
    # 对比
    print("\n差异:")
    if data_a.hex() == data_c.hex():
        print("  A == C (没变化)")
    else:
        print("  A != C (有变化!)")
        for i in range(min(len(data_a), len(data_c))):
            if data_a[i] != data_c[i]:
                print(f"    byte[{i}]: 0x{data_a[i]:02X} -> 0x{data_c[i]:02X}")
    
    if data_a.hex() == data_e.hex():
        print("  A == E (恢复)")
    else:
        print("  A != E (未恢复)")

if __name__ == "__main__":
    main()
