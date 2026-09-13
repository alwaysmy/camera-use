"""
winrt_camera4.py — WinRT摄像头（修复版）
"""
import asyncio
import os
import subprocess
import time

def close_camera():
    subprocess.run(["taskkill", "/IM", "Windows.Camera.exe", "/F"], capture_output=True)
    time.sleep(1)

async def main():
    close_camera()
    
    print("=== WinRT Camera ===\n")
    
    # 导入
    from winrt.windows.media.capture import MediaCapture, MediaCaptureInitializationSettings
    from winrt.windows.media.mediaproperties import ImageEncodingProperties
    import winrt.windows.storage as storage
    import winrt.windows.storage.streams as streams
    
    # 初始化
    print("1. 初始化...")
    settings = MediaCaptureInitializationSettings()
    settings.streaming_capture_mode = 1  # VideoOnly
    
    # 不带参数初始化
    capture = MediaCapture()
    await capture.initialize_async(settings)
    print("   成功!")
    
    # 拍照
    print("\n2. 拍照...")
    encoding = ImageEncodingProperties.create_jpeg()
    
    temp_dir = os.path.join(os.environ["TEMP"], "cam_test")
    os.makedirs(temp_dir, exist_ok=True)
    
    folder = await storage.StorageFolder.get_folder_from_path_async(os.environ["TEMP"])
    file = await folder.create_file_async("cam_test.jpg", 1)
    
    stream = streams.InMemoryRandomAccessStream()
    await capture.capture_photo_to_stream_async(stream, encoding)
    stream.seek(0)
    fs = await file.open_async(2)
    await stream.copy_to_async(fs)
    await fs.flush_async()
    
    filepath = os.path.join(temp_dir, "cam_test.jpg")
    print(f"   保存: {filepath}")
    
    # 分析
    import cv2
    img = cv2.imread(filepath)
    if img is not None:
        print(f"   尺寸: {img.shape}")
        print(f"   均值: {img.mean():.1f}")
        
        # 保存到当前目录
        outpath = "winrt_result.jpg"
        cv2.imwrite(outpath, img)
        print(f"   已保存: {outpath}")
        
        # 对比测试：现在用OpenCV打开同一个摄像头
        print("\n3. 对比：用OpenCV打开...")
        cap = cv2.VideoCapture(0)
        time.sleep(1)
        ret, frame = cap.read()
        if ret:
            print(f"   OpenCV均值: {frame.mean():.1f}")
        else:
            print("   OpenCV读取失败")
        cap.release()
    
    # 清理
    capture.dispose()
    print("\n完成!")

asyncio.run(main())
