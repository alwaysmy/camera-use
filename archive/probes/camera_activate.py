"""
camera_activate.py — 激活摄像头（调用Windows相机）
"""
import subprocess
import time
import os

def activate_camera():
    """调用Windows相机应用来激活摄像头"""
    print("正在激活摄像头...")
    
    # 启动Windows相机
    try:
        subprocess.Popen(["start", "microsoft.windows.camera:"], shell=True)
        time.sleep(3)  # 等待相机打开并初始化摄像头
        
        # 关闭相机
        subprocess.run(["taskkill", "/IM", "Windows.Camera.exe", "/F"], 
                      capture_output=True)
        time.sleep(1)  # 等待关闭
        
        print("摄像头已激活")
        return True
    except Exception as e:
        print(f"激活失败: {e}")
        return False

if __name__ == "__main__":
    activate_camera()
