import cv2
import sys
import time

def test_parameter_adjustment():
    """测试参数调节功能"""
    print("=== 摄像头参数调节测试 ===")
    
    # 打开摄像头
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("无法打开摄像头")
        return
    
    # 获取当前参数
    print("当前摄像头参数:")
    print(f"  分辨率: {int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}")
    print(f"  亮度: {cap.get(cv2.CAP_PROP_BRIGHTNESS)}")
    print(f"  对比度: {cap.get(cv2.CAP_PROP_CONTRAST)}")
    print(f"  饱和度: {cap.get(cv2.CAP_PROP_SATURATION)}")
    print(f"  曝光: {cap.get(cv2.CAP_PROP_EXPOSURE)}")
    
    # 测试调节参数
    print("\n测试调节参数...")
    
    # 1. 调节亮度
    original_brightness = cap.get(cv2.CAP_PROP_BRIGHTNESS)
    cap.set(cv2.CAP_PROP_BRIGHTNESS, 10)
    new_brightness = cap.get(cv2.CAP_PROP_BRIGHTNESS)
    print(f"亮度调节: {original_brightness} -> {new_brightness}")
    
    # 2. 调节对比度
    original_contrast = cap.get(cv2.CAP_PROP_CONTRAST)
    cap.set(cv2.CAP_PROP_CONTRAST, 60)
    new_contrast = cap.get(cv2.CAP_PROP_CONTRAST)
    print(f"对比度调节: {original_contrast} -> {new_contrast}")
    
    # 3. 调节饱和度
    original_saturation = cap.get(cv2.CAP_PROP_SATURATION)
    cap.set(cv2.CAP_PROP_SATURATION, 80)
    new_saturation = cap.get(cv2.CAP_PROP_SATURATION)
    print(f"饱和度调节: {original_saturation} -> {new_saturation}")
    
    # 4. 调节曝光
    original_exposure = cap.get(cv2.CAP_PROP_EXPOSURE)
    cap.set(cv2.CAP_PROP_EXPOSURE, -3)
    new_exposure = cap.get(cv2.CAP_PROP_EXPOSURE)
    print(f"曝光调节: {original_exposure} -> {new_exposure}")
    
    # 5. 调节分辨率
    original_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    original_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)
    new_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    new_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"分辨率调节: {original_width}x{original_height} -> {new_width}x{new_height}")
    
    # 拍照保存调节后的效果
    ret, frame = cap.read()
    if ret:
        filename = f"adjusted_{int(time.time())}.jpg"
        cv2.imwrite(filename, frame)
        print(f"\n调节后拍照保存为: {filename}")
    
    # 恢复原始参数
    cap.set(cv2.CAP_PROP_BRIGHTNESS, original_brightness)
    cap.set(cv2.CAP_PROP_CONTRAST, original_contrast)
    cap.set(cv2.CAP_PROP_SATURATION, original_saturation)
    cap.set(cv2.CAP_PROP_EXPOSURE, original_exposure)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, original_width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, original_height)
    
    cap.release()
    print("\n参数已恢复原始值")

def test_multiple_cameras():
    """测试多个摄像头"""
    print("\n=== 多摄像头测试 ===")
    
    cameras = []
    for i in range(5):
        cap = cv2.VideoCapture(i)
        if cap.isOpened():
            ret, frame = cap.read()
            if ret:
                width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                cameras.append({
                    'index': i,
                    'width': width,
                    'height': height,
                    'frame': frame
                })
            cap.release()
    
    print(f"找到 {len(cameras)} 个可用摄像头:")
    for cam in cameras:
        print(f"  摄像头 {cam['index']}: {cam['width']}x{cam['height']}")
    
    # 为每个摄像头拍照
    for cam in cameras:
        cap = cv2.VideoCapture(cam['index'])
        if cap.isOpened():
            ret, frame = cap.read()
            if ret:
                filename = f"camera_{cam['index']}_{int(time.time())}.jpg"
                cv2.imwrite(filename, frame)
                print(f"摄像头 {cam['index']} 拍照保存为: {filename}")
            cap.release()

def main():
    print("摄像头高级功能测试")
    print("=" * 50)
    
    # 测试参数调节
    test_parameter_adjustment()
    
    # 测试多摄像头
    test_multiple_cameras()
    
    print("\n" + "=" * 50)
    print("高级测试完成！")

if __name__ == "__main__":
    main()