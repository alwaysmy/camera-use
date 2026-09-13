import cv2
import sys

def realtime_preview():
    """实时预览摄像头"""
    print("=== 摄像头实时预览 ===")
    print("按 'q' 退出")
    print("按 's' 拍照")
    print("按 'b' 增加亮度")
    print("按 'B' 减少亮度")
    print("按 'c' 增加对比度")
    print("按 'C' 减少对比度")
    
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("无法打开摄像头")
        return
    
    # 获取初始参数
    brightness = cap.get(cv2.CAP_PROP_BRIGHTNESS)
    contrast = cap.get(cv2.CAP_PROP_CONTRAST)
    
    while True:
        ret, frame = cap.read()
        if not ret:
            print("无法读取帧")
            break
        
        # 显示帧
        cv2.imshow('Camera Preview', frame)
        
        # 等待按键
        key = cv2.waitKey(1) & 0xFF
        
        # 退出
        if key == ord('q'):
            break
        # 拍照
        elif key == ord('s'):
            import time
            filename = f"photo_{int(time.time())}.jpg"
            cv2.imwrite(filename, frame)
            print(f"拍照保存为: {filename}")
        # 调节亮度
        elif key == ord('b'):
            brightness += 1
            cap.set(cv2.CAP_PROP_BRIGHTNESS, brightness)
            print(f"亮度: {brightness}")
        elif key == ord('B'):
            brightness -= 1
            cap.set(cv2.CAP_PROP_BRIGHTNESS, brightness)
            print(f"亮度: {brightness}")
        # 调节对比度
        elif key == ord('c'):
            contrast += 1
            cap.set(cv2.CAP_PROP_CONTRAST, contrast)
            print(f"对比度: {contrast}")
        elif key == ord('C'):
            contrast -= 1
            cap.set(cv2.CAP_PROP_CONTRAST, contrast)
            print(f"对比度: {contrast}")
    
    cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    realtime_preview()