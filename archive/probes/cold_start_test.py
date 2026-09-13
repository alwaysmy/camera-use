"""
cold_start_test.py — 从设备重启开始的严格冷启动测试
"""
import cv2
import time
import subprocess
import gc

def restart_device():
    subprocess.run(['powershell', '-Command',
        'Get-PnpDevice -FriendlyName "*Camera*" | Disable-PnpDevice -Confirm:0 -ErrorAction SilentlyContinue'],
        capture_output=True)
    time.sleep(2)
    subprocess.run(['powershell', '-Command',
        'Get-PnpDevice -FriendlyName "*Camera*" | Enable-PnpDevice -Confirm:0 -ErrorAction SilentlyContinue'],
        capture_output=True)
    time.sleep(5)

def main():
    print("步骤1: 重启摄像头设备...")
    restart_device()
    
    print("步骤2: 释放所有Python资源...")
    gc.collect()
    time.sleep(2)
    
    print("步骤3: 冷启动OpenCV...")
    cap = cv2.VideoCapture(0)
    time.sleep(1)
    
    print("步骤4: 读取参数...")
    for name in ['EXPOSURE', 'GAIN', 'BRIGHTNESS', 'CONTRAST', 'AUTO_EXPOSURE']:
        val = cap.get(getattr(cv2, f'CAP_PROP_{name}'))
        print(f"  {name:20s} = {val}")
    
    print("步骤5: 读取10帧预热...")
    for i in range(10):
        ret, frame = cap.read()
        if ret:
            print(f"  帧 {i+1:2d}: mean={frame.mean():.1f}")
    
    print("步骤6: 最终拍照...")
    ret, frame = cap.read()
    if ret:
        mean = frame.mean()
        print(f"  最终均值: {mean:.1f}")
        cv2.imwrite('cold_start_result.jpg', frame)
        print("  已保存: cold_start_result.jpg")
        
        if mean > 80:
            print("\n结论: 摄像头正常工作！")
        else:
            print(f"\n结论: 摄像头偏暗 (mean={mean:.1f})")
            print("可能原因:")
            print("  1. 环境光线不足")
            print("  2. 摄像头需要特殊初始化")
    
    cap.release()

if __name__ == "__main__":
    main()
