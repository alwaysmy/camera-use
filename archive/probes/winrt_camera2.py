"""
winrt_camera2.py — 用WinRT API打开摄像头（修正版）
"""
import asyncio
import time
import subprocess
import os

def close_camera():
    subprocess.run(["taskkill", "/IM", "Windows.Camera.exe", "/F"], capture_output=True)
    time.sleep(1)

async def main():
    close_camera()
    
    print("=== WinRT Camera API ===\n")
    
    from winrt.windows.media.capture import MediaCapture, MediaCaptureInitializationSettings
    from winrt.windows.media.mediaproperties import ImageEncodingProperties
    import winrt.windows.storage as storage
    import winrt.windows.storage.streams as streams
    
    # 初始化
    print("1. 初始化MediaCapture...")
    settings = MediaCaptureInitializationSettings()
    settings.streaming_capture_mode = 1  # Video only
    
    capture = MediaCapture()
    await capture.initialize_async(settings)
    print("   成功!")
    
    # 拍照
    print("\n2. 拍照...")
    encoding = ImageEncodingProperties.create_jpeg()
    
    temp_dir = os.path.join(os.environ["TEMP"], "camera_test")
    os.makedirs(temp_dir, exist_ok=True)
    
    folder = await storage.StorageFolder.get_folder_from_path_async(temp_dir)
    file = await folder.create_file_async("winrt.jpg", 1)
    
    stream = streams.InMemoryRandomAccessStream()
    await capture.capture_photo_to_stream_async(stream, encoding)
    
    stream.seek(0)
    file_stream = await file.open_async(2)
    await stream.copy_to_async(file_stream)
    await file_stream.flush_async()
    
    filepath = os.path.join(temp_dir, "winrt.jpg")
    print(f"   保存: {filepath}")
    
    # 分析
    import cv2
    img = cv2.imread(filepath)
    if img is not None:
        print(f"   尺寸: {img.shape}")
        print(f"   均值: {img.mean():.1f}")
        
        # 保存到当前目录
        cv2.imwrite("winrt_capture.jpg", img)
        print("   已复制到 winrt_capture.jpg")
    
    # 清理
    capture.dispose()
    print("\n完成!")

if __name__ == "__main__":
    asyncio.run(main())
