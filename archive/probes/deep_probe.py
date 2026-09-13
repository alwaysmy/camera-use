"""
deep_probe.py — 深度探测摄像头初始化过程
目标：找到Windows相机激活摄像头的根本原因
"""
import cv2
import time
import subprocess
import json

def get_all_cv2_props(cap):
    """读取所有OpenCV支持的摄像头属性"""
    props = {}
    prop_names = [
        "POS_MSEC", "POS_FRAMES", "POS_AVI_RATIO",
        "FRAME_WIDTH", "FRAME_HEIGHT", "FPS",
        "FOURCC", "FRAME_COUNT",
        "BRIGHTNESS", "CONTRAST", "SATURATION", "HUE",
        "GAIN", "EXPOSURE", "FOCUS", "ZOOM",
        "PAN", "TILT", "ROLL", "BACKLIGHT",
        "SHARPNESS", "AUTO_EXPOSURE", "AUTO_WB",
        "WHITE_BALANCE_TEMPERATURE",
    ]
    for name in prop_names:
        prop_id = getattr(cv2, f"CAP_PROP_{name}", None)
        if prop_id is not None:
            try:
                val = cap.get(prop_id)
                props[name] = val
            except:
                pass
    return props

def test_capture(label):
    """打开摄像头，读取参数，拍照"""
    cap = cv2.VideoCapture(0)
    time.sleep(0.3)
    
    props = get_all_cv2_props(cap)
    
    # 读几帧稳定
    for _ in range(5):
        cap.read()
    
    ret, frame = cap.read()
    mean_val = frame.mean() if ret else -1
    
    cap.release()
    
    return {
        "label": label,
        "mean": round(mean_val, 1),
        "props": props,
    }

def main():
    results = []
    
    # 阶段1: 冷启动
    print("[1/6] 冷启动（等待3秒）...")
    import gc; gc.collect(); time.sleep(3)
    results.append(test_capture("冷启动"))
    
    # 阶段2: 快速重开
    print("[2/6] 快速重开...")
    time.sleep(0.5)
    results.append(test_capture("快速重开"))
    
    # 阶段3: 启动Windows相机
    print("[3/6] 启动Windows相机...")
    subprocess.Popen(["start", "microsoft.windows.camera:"], shell=True)
    time.sleep(3)
    results.append(test_capture("Windows相机运行中"))
    
    # 阶段4: 关闭Windows相机
    print("[4/6] 关闭Windows相机...")
    subprocess.run(["taskkill", "/IM", "Windows.Camera.exe", "/F"], capture_output=True)
    time.sleep(1)
    results.append(test_capture("刚关闭Windows相机"))
    
    # 阶段5: 等待5秒
    print("[5/6] 等待5秒...")
    time.sleep(5)
    results.append(test_capture("关闭5秒后"))
    
    # 阶段6: 重启设备
    print("[6/6] 重启摄像头设备...")
    subprocess.run(['powershell', '-Command',
        'Get-PnpDevice -FriendlyName "*Camera*" | Disable-PnpDevice -Confirm:0 -ErrorAction SilentlyContinue'],
        capture_output=True)
    time.sleep(1)
    subprocess.run(['powershell', '-Command',
        'Get-PnpDevice -FriendlyName "*Camera*" | Enable-PnpDevice -Confirm:0 -ErrorAction SilentlyContinue'],
        capture_output=True)
    time.sleep(3)
    results.append(test_capture("设备重启后"))
    
    # 输出对比
    print("\n" + "=" * 70)
    print("  均值亮度对比")
    print("=" * 70)
    for r in results:
        marker = " <-- 正常" if r["mean"] > 100 else ""
        print(f"  {r['label']:25s}  均值={r['mean']:6.1f}{marker}")
    
    # 输出关键参数差异
    print("\n" + "=" * 70)
    print("  关键参数对比")
    print("=" * 70)
    key_props = ["EXPOSURE", "GAIN", "BRIGHTNESS", "CONTRAST", "AUTO_EXPOSURE"]
    header = f"{'阶段':25s}"
    for p in key_props:
        header += f"  {p:>12s}"
    print(header)
    print("-" * 70)
    for r in results:
        line = f"{r['label']:25s}"
        for p in key_props:
            val = r["props"].get(p, "N/A")
            line += f"  {val:>12}"
        print(line)
    
    # 保存完整结果
    with open("deep_probe_result.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print("\n完整结果已保存: deep_probe_result.json")

if __name__ == "__main__":
    main()
