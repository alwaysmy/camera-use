import cv2
import time
import subprocess

print('=== 测试：重启摄像头后立即拍照 ===')

# 重启设备
subprocess.run(['powershell', '-Command',
    'Get-PnpDevice -FriendlyName "*Camera*" | Disable-PnpDevice -Confirm:0 -ErrorAction SilentlyContinue'],
    capture_output=True)
time.sleep(0.5)
subprocess.run(['powershell', '-Command',
    'Get-PnpDevice -FriendlyName "*Camera*" | Enable-PnpDevice -Confirm:0 -ErrorAction SilentlyContinue'],
    capture_output=True)
time.sleep(2)  # 等待设备重启

# 打开摄像头
cap = cv2.VideoCapture(0)
time.sleep(1)

# 读取参数
print('重启后参数:')
print(f'  EXPOSURE = {cap.get(cv2.CAP_PROP_EXPOSURE)}')
print(f'  GAIN = {cap.get(cv2.CAP_PROP_GAIN)}')
print(f'  BRIGHTNESS = {cap.get(cv2.CAP_PROP_BRIGHTNESS)}')

# 读取10帧稳定
for i in range(10):
    cap.read()

# 拍照
ret, frame = cap.read()
if ret:
    print(f'  均值 = {frame.mean():.1f}')
    cv2.imwrite('cam0_restart_quick.jpg', frame)

cap.release()
