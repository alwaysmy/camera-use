"""
winrt_camera3.py — WinRT摄像头（最简版）
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
    
    from winrt.windows.media.capture import MediaCapture, MediaCaptureInitializationSettings
    from winrt.windows.media.mediaproperties import ImageEncodingProperties
    
    print("初始化...")
    capture = MediaCapture()
    await capture.initialize_with_settings_async(MediaCaptureInitializationSettings())
    print("成功!")
    
    print("拍照...")
    encoding = ImageEncodingProperties.create_jpeg()
    temp = os.path.join(os.environ["TEMP"], "winrt_cam.jpg")
    
    import winrt.windows.storage as storage
    import winrt.windows.storage.streams as streams
    
    folder = await storage.StorageFolder.get_folder_from_path_async(os.environ["TEMP"])
    file = await folder.create_file_async("winrt_cam.jpg", 1)
    
    stream = streams.InMemoryRandomAccessStream()
    await capture.capture_photo_to_stream_async(stream, encoding)
    stream.seek(0)
    fs = await file.open_async(2)
    await stream.copy_to_async(fs)
    await fs.flush_async()
    
    capture.dispose()
    
    import cv2
    img = cv2.imread(temp)
    if img is not None:
        cv2.imwrite("winrt_capture.jpg", img)
        print(f"均值: {img.mean():.1f}")
    else:
        print("读取失败")

asyncio.run(main())
