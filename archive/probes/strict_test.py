"""
strict_test.py — 严格对照实验
每步都冷启动，确保对比公平
"""
import cv2
import time
import subprocess
import gc

def cold_open():
    """冷启动：释放所有资源，等待后重新打开"""
    gc.collect()
    time.sleep(2)
    cap = cv2.VideoCapture(0)
    time.sleep(0.5)
    return cap

def measure(cap, label):
    """读取10帧稳定后测量"""
    for _ in range(10):
        cap.read()
    ret, frame = cap.read()
    mean = frame.mean() if ret else -1
    props = {}
    for name in ['EXPOSURE', 'GAIN', 'BRIGHTNESS', 'CONTRAST', 'AUTO_EXPOSURE']:
        pid = getattr(cv2, f'CAP_PROP_{name}', None)
        if pid is not None:
            props[name] = cap.get(pid)
    cap.release()
    print(f"  {label:40s} mean={mean:6.1f}  {props}")
    return mean, props

def open_win_camera():
    subprocess.Popen(["start", "microsoft.windows.camera:"], shell=True)
    time.sleep(4)

def close_win_camera():
    subprocess.run(["taskkill", "/IM", "Windows.Camera.exe", "/F"], capture_output=True)
    time.sleep(1)

def restart_device():
    subprocess.run(['powershell', '-Command',
        'Get-PnpDevice -FriendlyName "*Camera*" | Disable-PnpDevice -Confirm:0 -ErrorAction SilentlyContinue'],
        capture_output=True)
    time.sleep(2)
    subprocess.run(['powershell', '-Command',
        'Get-PnpDevice -FriendlyName "*Camera*" | Enable-PnpDevice -Confirm:0 -ErrorAction SilentlyContinue'],
        capture_output=True)
    time.sleep(3)

def main():
    print("=" * 80)
    print("  严格对照实验")
    print("=" * 80)
    
    # 先确保干净状态
    close_win_camera()
    
    # 测试1: 直接冷启动
    print("\n[1] 直接冷启动:")
    cap = cold_open()
    m1, p1 = measure(cap, "冷启动")
    
    # 测试2: 再次冷启动
    print("\n[2] 再次冷启动:")
    cap = cold_open()
    m2, p2 = measure(cap, "再次冷启动")
    
    # 测试3: 打开Windows相机
    print("\n[3] 打开Windows相机...")
    open_win_camera()
    
    # 测试4: 相机运行时（用OpenCV读取）
    print("\n[4] 相机运行中尝试OpenCV:")
    try:
        cap = cv2.VideoCapture(0)
        time.sleep(0.5)
        m4, p4 = measure(cap, "相机运行中")
    except Exception as e:
        print(f"  失败: {e}")
    
    # 测试5: 关闭相机
    print("\n[5] 关闭Windows相机...")
    close_win_camera()
    
    # 测试6: 关闭后立即冷启动
    print("\n[6] 关闭后立即冷启动:")
    cap = cold_open()
    m6, p6 = measure(cap, "关闭后立即")
    
    # 测试7: 关闭后等5秒再冷启动
    print("\n[7] 关闭后等5秒冷启动:")
    time.sleep(5)
    cap = cold_open()
    m7, p7 = measure(cap, "关闭后5秒")
    
    # 测试8: 设备重启后冷启动
    print("\n[8] 设备重启...")
    restart_device()
    print("  设备重启后冷启动:")
    cap = cold_open()
    m8, p8 = measure(cap, "设备重启后")
    
    # 汇总
    print("\n" + "=" * 80)
    print("  汇总")
    print("=" * 80)
    results = [
        ("直接冷启动", m1, p1),
        ("再次冷启动", m2, p2),
        ("关闭后立即", m6, p6),
        ("关闭后5秒", m7, p7),
        ("设备重启后", m8, p8),
    ]
    for label, m, p in results:
        status = "正常" if m > 80 else "偏暗" if m > 30 else "很暗"
        print(f"  {label:20s}  mean={m:6.1f}  EXPOSURE={p.get('EXPOSURE','?'):6.1f}  [{status}]")

if __name__ == "__main__":
    main()
