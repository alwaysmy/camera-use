import cv2, time, subprocess, gc

subprocess.run(['powershell', '-Command',
    'Get-PnpDevice -FriendlyName "*Camera*" | Disable-PnpDevice -Confirm:0 -ErrorAction SilentlyContinue'],
    capture_output=True)
time.sleep(3)
subprocess.run(['powershell', '-Command',
    'Get-PnpDevice -FriendlyName "*Camera*" | Enable-PnpDevice -Confirm:0 -ErrorAction SilentlyContinue'],
    capture_output=True)
time.sleep(8)

gc.collect()
time.sleep(2)

cap = cv2.VideoCapture(0)
time.sleep(2)

print('参数:')
for name in ['EXPOSURE', 'GAIN', 'BRIGHTNESS', 'AUTO_EXPOSURE']:
    print(f'  {name} = {cap.get(getattr(cv2, f"CAP_PROP_{name}"))}')

for i in range(20):
    ret, frame = cap.read()
    if ret:
        print(f'帧 {i+1:2d}: mean={frame.mean():.1f} max={frame.max()}')
    else:
        print(f'帧 {i+1:2d}: 读取失败')

ret, frame = cap.read()
if ret:
    cv2.imwrite('cold_start.jpg', frame)
    print(f'最终: mean={frame.mean():.1f}')
else:
    print('最终: 读取失败')

cap.release()
