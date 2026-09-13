"""
winrt_camera.py — 用Windows Runtime API打开摄像头
这是Windows Camera应用使用的同一套API
"""
import asyncio
import time
import subprocess

def close_camera():
    subprocess.run(["taskkill", "/IM", "Windows.Camera.exe", "/F"], capture_output=True)
    time.sleep(1)

async def main():
    close_camera()
    
    print("=== WinRT Camera API ===\n")
    
    try:
        from winrt.windows.media.capture import MediaCapture, MediaCaptureInitializationSettings
        from winrt.windows.media.devices import MediaDevice
        from winrt.windows.media.mediaproperties import ImageEncodingProperties, VideoEncodingProperties
        import winrt.windows.storage as storage
        import winrt.windows.storage.streams as streams
    except ImportError as e:
        print(f"导入失败: {e}")
        print("需要安装: pip install winrt-Windows.Media.Capture winrt-Windows.Media.Devices winrt-Windows.Media.MediaProperties")
        return
    
    # 获取视频设备ID
    print("1. 获取摄像头设备ID...")
    device_id = MediaDevice.get_default_video_capture_device_id()
    print(f"   默认视频设备: {device_id[:80]}...")
    
    # 初始化设置
    print("\n2. 初始化MediaCapture...")
    settings = MediaCaptureInitializationSettings()
    settings.streaming_capture_mode = 1  # Video only
    settings.media_category = 0  # Other
    
    capture = MediaCapture()
    await capture.initialize_async(settings)
    print("   初始化成功!")
    
    # 列出视频设备
    print("\n3. 视频设备信息:")
    print(f"   设备名称: {device_id}")
    
    # 尝试拍照
    print("\n4. 拍照测试...")
    try:
        # 获取编码属性
        encoding = ImageEncodingProperties.create_jpeg()
        
        # 创建临时文件
        import tempfile
        import os
        
        temp_dir = tempfile.gettempdir()
        temp_file = os.path.join(temp_dir, "winrt_capture.jpg")
        
        # 创建文件
        folder = await storage.StorageFolder.get_folder_from_path_async(temp_dir)
        file = await folder.create_file_async("winrt_capture.jpg", 1)  # ReplaceExisting
        
        # 拍照
        stream = streams.InMemoryRandomAccessStream()
        await capture.capture_photo_to_stream_async(stream, encoding)
        
        # 保存到文件
        stream.seek(0)
        file_stream = await file.open_async(2)  # ReadWrite
        await stream.copy_to_async(file_stream)
        await file_stream.flush_async()
        
        print(f"   已保存: {temp_file}")
        
        # 读取并分析
        import cv2
        img = cv2.imread(temp_file)
        if img is not None:
            print(f"   尺寸: {img.shape}")
            print(f"   均值: {img.mean():.1f}")
        else:
            print("   无法读取图片")
            
    except Exception as e:
        print(f"   拍照失败: {e}")
    
    # 清理
    print("\n5. 清理...")
    capture.dispose()
    print("   完成!")

if __name__ == "__main__":
    asyncio.run(main())
