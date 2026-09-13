import cv2
import time

# 当前正常状态的参数
print('=== 当前正常参数 (来自Windows相机) ===')
cap = cv2.VideoCapture(0)
normal_params = {}
for name in ['AUTO_EXPOSURE', 'AUTO_WB', 'EXPOSURE', 'GAIN', 'BRIGHTNESS', 'CONTRAST', 'SATURATION', 'HUE', 'SHARPNESS']:
    prop_id = getattr(cv2, f'CAP_PROP_{name}', None)
    if prop_id is not None:
        val = cap.get(prop_id)
        normal_params[name] = val
        print(f'{name:25} = {val}')
cap.release()

# 关闭摄像头
time.sleep(1)

# 重新打开，然后设置这些参数
print('\n=== 重新打开并设置参数 ===')
cap = cv2.VideoCapture(0)
time.sleep(0.5)

# 先读取当前值
print('打开后默认值:')
for name in ['EXPOSURE', 'GAIN', 'BRIGHTNESS']:
    print(f'  {name}: {cap.get(getattr(cv2, f"CAP_PROP_{name}"))}')

# 逐个设置参数，观察变化
print('\n=== 逐个设置参数 ===')
for name, val in normal_params.items():
    prop_id = getattr(cv2, f'CAP_PROP_{name}', None)
    if prop_id is not None:
        cap.set(prop_id, val)
        actual = cap.get(prop_id)
        ret, frame = cap.read()
        if ret:
            print(f'{name:25} 设置={val:6.1f} 实际={actual:6.1f} 均值={frame.mean():6.1f}')

cap.release()
