"""
media_types.py — 列出摄像头支持的所有媒体类型
"""
import subprocess
import time

def close_camera():
    subprocess.run(["taskkill", "/IM", "Windows.Camera.exe", "/F"], capture_output=True)
    time.sleep(1)

def main():
    close_camera()
    
    print("=== 用 ffmpeg 列出摄像头支持的格式 ===\n")
    
    # 列出设备支持的所有格式
    result = subprocess.run(
        ["ffmpeg", "-f", "dshow", "-list_formats", "true", "-i", "video=HP Wide Vision FHD Camera"],
        capture_output=True, text=True, timeout=10
    )
    
    print("stdout:")
    print(result.stdout)
    print("stderr:")
    print(result.stderr)
    
    # 也试试IR Camera
    print("\n\n=== IR Camera ===")
    result2 = subprocess.run(
        ["ffmpeg", "-f", "dshow", "-list_formats", "true", "-i", "video=HP IR Camera"],
        capture_output=True, text=True, timeout=10
    )
    print("stdout:")
    print(result2.stdout)
    print("stderr:")
    print(result2.stderr)

if __name__ == "__main__":
    main()
