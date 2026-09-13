"""
camera_state_check.py — 检查摄像头当前状态
"""
import cv2
import time
import subprocess

def main():
    print("=" * 60)
    print("  摄像头状态检查")
    print("=" * 60)
    
    # 检查设备状态
    print("\n[1] 设备状态:")
    result = subprocess.run(
        ["powershell", "-Command", 
         "Get-PnpDevice -FriendlyName '*Camera*' | Select-Object FriendlyName, Status | Format-Table -AutoSize"],
        capture_output=True, text=True, encoding="utf-8"
    )
    print(result.stdout)
    
    # 尝试打开摄像头
    print("[2] OpenCV测试:")
    cap = cv2.VideoCapture(0)
    time.sleep(1)
    
    if cap.isOpened():
        print("  摄像头已打开")
        print(f"  EXPOSURE = {cap.get(cv2.CAP_PROP_EXPOSURE)}")
        
        # 读取5帧
        for i in range(5):
            ret, frame = cap.read()
            if ret:
                print(f"  帧 {i+1}: mean={frame.mean():.1f}")
            else:
                print(f"  帧 {i+1}: 读取失败")
        
        cap.release()
    else:
        print("  无法打开摄像头")
    
    # 检查是否被占用
    print("\n[3] 检查占用进程:")
    result = subprocess.run(
        ["powershell", "-Command",
         "Get-Process | Where-Object { $_.MainWindowTitle -match 'Camera|camera|摄像' } | Select-Object Name, Id, MainWindowTitle"],
        capture_output=True, text=True, encoding="utf-8"
    )
    if result.stdout.strip():
        print(result.stdout)
    else:
        print("  无占用进程")

if __name__ == "__main__":
    main()
