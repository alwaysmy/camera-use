import cv2
import sys
import time

def check_cameras():
    """检测可用摄像头"""
    print("=== 摄像头检测 ===")
    
    # 尝试打开索引0-10的摄像头
    available_cameras = []
    for i in range(10):
        cap = cv2.VideoCapture(i)
        if cap.isOpened():
            ret, frame = cap.read()
            if ret:
                width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                fps = cap.get(cv2.CAP_PROP_FPS)
                print(f"摄像头 {i}: {width}x{height}, FPS={fps:.1f}")
                available_cameras.append(i)
            cap.release()
    
    if not available_cameras:
        print("未找到可用摄像头")
        return None
    
    return available_cameras

def test_capture(camera_index=0):
    """测试拍照功能"""
    print(f"\n=== 测试拍照 (摄像头 {camera_index}) ===")
    
    cap = cv2.VideoCapture(camera_index)
    if not cap.isOpened():
        print(f"无法打开摄像头 {camera_index}")
        return False
    
    # 读取一帧
    ret, frame = cap.read()
    if ret:
        filename = f"test_capture_{int(time.time())}.jpg"
        cv2.imwrite(filename, frame)
        print(f"拍照成功，保存为: {filename}")
        print(f"图片尺寸: {frame.shape[1]}x{frame.shape[0]}")
        cap.release()
        return True
    else:
        print("拍照失败")
        cap.release()
        return False

def check_parameters(camera_index=0):
    """检查可调节的参数"""
    print(f"\n=== 摄像头参数检查 (摄像头 {camera_index}) ===")
    
    cap = cv2.VideoCapture(camera_index)
    if not cap.isOpened():
        print(f"无法打开摄像头 {camera_index}")
        return
    
    # 常用参数列表
    params = {
        "CAP_PROP_FRAME_WIDTH": cv2.CAP_PROP_FRAME_WIDTH,
        "CAP_PROP_FRAME_HEIGHT": cv2.CAP_PROP_FRAME_HEIGHT,
        "CAP_PROP_FPS": cv2.CAP_PROP_FPS,
        "CAP_PROP_BRIGHTNESS": cv2.CAP_PROP_BRIGHTNESS,
        "CAP_PROP_CONTRAST": cv2.CAP_PROP_CONTRAST,
        "CAP_PROP_SATURATION": cv2.CAP_PROP_SATURATION,
        "CAP_PROP_HUE": cv2.CAP_PROP_HUE,
        "CAP_PROP_GAIN": cv2.CAP_PROP_GAIN,
        "CAP_PROP_EXPOSURE": cv2.CAP_PROP_EXPOSURE,
        "CAP_PROP_AUTO_EXPOSURE": cv2.CAP_PROP_AUTO_EXPOSURE,
        "CAP_PROP_AUTO_WB": cv2.CAP_PROP_AUTO_WB,
    }
    
    print("参数名称 | 当前值 | 是否可调")
    print("-" * 50)
    
    for name, prop_id in params.items():
        value = cap.get(prop_id)
        # 尝试设置值来检查是否可调
        current_value = value
        if value != -1:  # -1表示不支持
            # 尝试设置一个不同的值
            test_value = value + 1 if value < 100 else value - 1
            cap.set(prop_id, test_value)
            new_value = cap.get(prop_id)
            adjustable = "是" if new_value != current_value else "否"
            print(f"{name:25} | {current_value:8.2f} | {adjustable}")
        else:
            print(f"{name:25} | {'不支持':8} | -")
    
    cap.release()

def main():
    print("摄像头功能测试工具")
    print("=" * 50)
    
    # 检测摄像头
    cameras = check_cameras()
    if not cameras:
        print("\n未找到摄像头，请检查：")
        print("1. 摄像头是否正确连接")
        print("2. 摄像头驱动是否安装")
        print("3. 其他程序是否占用摄像头")
        return
    
    # 使用第一个摄像头进行测试
    camera_index = cameras[0]
    
    # 测试拍照
    test_capture(camera_index)
    
    # 检查参数
    check_parameters(camera_index)
    
    print("\n" + "=" * 50)
    print("测试完成！")

if __name__ == "__main__":
    main()