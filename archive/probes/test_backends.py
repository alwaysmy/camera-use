import cv2
import time

print("=== 尝试不同后端 ===")

# 1. DSHOW
print("\n1. DSHOW后端:")
try:
    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    time.sleep(1)
    ret, frame = cap.read()
    if ret:
        print(f"   成功: {frame.shape} mean={frame.mean():.1f}")
    else:
        print("   读取失败")
    cap.release()
except Exception as e:
    print(f"   错误: {e}")

# 2. Media Foundation
print("\n2. Media Foundation:")
try:
    cap = cv2.VideoCapture(0, cv2.CAP_MSMF)
    time.sleep(1)
    ret, frame = cap.read()
    if ret:
        print(f"   成功: {frame.shape} mean={frame.mean():.1f}")
    else:
        print("   读取失败")
    cap.release()
except Exception as e:
    print(f"   错误: {e}")

# 3. 默认
print("\n3. 默认后端:")
try:
    cap = cv2.VideoCapture(0)
    time.sleep(1)
    ret, frame = cap.read()
    if ret:
        print(f"   成功: {frame.shape} mean={frame.mean():.1f}")
    else:
        print("   读取失败")
    cap.release()
except Exception as e:
    print(f"   错误: {e}")

# 4. 用字符串路径打开 (DirectShow)
print("\n4. DirectShow字符串路径:")
try:
    cap = cv2.VideoCapture("video=HP Wide Vision FHD Camera", cv2.CAP_DSHOW)
    time.sleep(1)
    ret, frame = cap.read()
    if ret:
        print(f"   成功: {frame.shape} mean={frame.mean():.1f}")
    else:
        print("   读取失败")
    cap.release()
except Exception as e:
    print(f"   错误: {e}")

# 5. 设置不同的分辨率/格式
print("\n5. 设置1920x1080:")
try:
    cap = cv2.VideoCapture(0)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
    time.sleep(1)
    w = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
    h = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    print(f"   请求1920x1080 -> 实际{int(w)}x{int(h)}")
    ret, frame = cap.read()
    if ret:
        print(f"   mean={frame.mean():.1f}")
    else:
        print("   读取失败")
    cap.release()
except Exception as e:
    print(f"   错误: {e}")

# 6. 设置640x480
print("\n6. 设置640x480:")
try:
    cap = cv2.VideoCapture(0)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    time.sleep(1)
    ret, frame = cap.read()
    if ret:
        print(f"   mean={frame.mean():.1f}")
    else:
        print("   读取失败")
    cap.release()
except Exception as e:
    print(f"   错误: {e}")

# 7. 设置FPS=30
print("\n7. 设置FPS=30:")
try:
    cap = cv2.VideoCapture(0)
    cap.set(cv2.CAP_PROP_FPS, 30)
    time.sleep(1)
    fps = cap.get(cv2.CAP_PROP_FPS)
    print(f"   FPS={fps}")
    ret, frame = cap.read()
    if ret:
        print(f"   mean={frame.mean():.1f}")
    else:
        print("   读取失败")
    cap.release()
except Exception as e:
    print(f"   错误: {e}")
