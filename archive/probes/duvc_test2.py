import duvc_ctl as duvc
import cv2
import time
import subprocess

def close_camera():
    subprocess.run(["taskkill", "/IM", "Windows.Camera.exe", "/F"], capture_output=True)
    time.sleep(1)

def capture(label):
    cap = cv2.VideoCapture(0)
    time.sleep(1)
    ret, frame = cap.read()
    if ret:
        filename = f'test_{label}.jpg'
        cv2.imwrite(filename, frame)
        print(f"  {label}: mean={frame.mean():.1f}, saved={filename}")
    cap.release()

def main():
    close_camera()
    cam = duvc.find_camera('HP')
    print(f"Device: {cam.device_name}")

    # Get property range
    print("\n--- Property Range ---")
    try:
        r = cam.get_property_range(duvc.CamProp.Exposure)
        print(f"  Range: min={r.min}, max={r.max}, step={r.step}, default={r.default_value}")
        print(f"  Auto supported: {r.auto_supported}")
        print(f"  Manual supported: {r.manual_supported}")
    except Exception as e:
        print(f"  Range failed: {e}")

    # Test sequence
    print("\n--- Test Sequence ---")
    
    # Start with manual
    cam.set_exposure(-5, 'manual')
    time.sleep(1)
    print(f"After set(-5, manual): get={cam.get_exposure()}")
    capture('manual_m5')

    cam.set_exposure(-4, 'manual')
    time.sleep(1)
    print(f"After set(-4, manual): get={cam.get_exposure()}")
    capture('manual_m4')

    cam.set_exposure(-3, 'manual')
    time.sleep(1)
    print(f"After set(-3, manual): get={cam.get_exposure()}")
    capture('manual_m3')

    cam.set_exposure(-2, 'manual')
    time.sleep(1)
    print(f"After set(-2, manual): get={cam.get_exposure()}")
    capture('manual_m2')

    cam.set_exposure(-1, 'manual')
    time.sleep(1)
    print(f"After set(-1, manual): get={cam.get_exposure()}")
    capture('manual_m1')

    cam.set_exposure(0, 'manual')
    time.sleep(1)
    print(f"After set(0, manual): get={cam.get_exposure()}")
    capture('manual_0')

    # Switch to auto
    cam.set_exposure(0, 'auto')
    time.sleep(1)
    print(f"After set(0, auto): get={cam.get_exposure()}")
    capture('auto_0')

    # Try auto then manual
    cam.set_exposure(-5, 'manual')
    time.sleep(1)
    print(f"After set(-5, manual): get={cam.get_exposure()}")
    capture('manual_m5_after_auto')

    cam.set_exposure(0, 'auto')
    time.sleep(2)
    print(f"After set(0, auto): get={cam.get_exposure()}")
    capture('auto_0_final')

    cam.close()
    print("\nDone!")

if __name__ == "__main__":
    main()
