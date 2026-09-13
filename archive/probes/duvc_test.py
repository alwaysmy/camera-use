import duvc_ctl as duvc
import cv2
import time
import subprocess

def close_camera():
    subprocess.run(["taskkill", "/IM", "Windows.Camera.exe", "/F"], capture_output=True)
    time.sleep(1)

def main():
    close_camera()
    print("=" * 60)
    print("  duvc-ctl - correct UVC exposure control")
    print("=" * 60)

    cam = duvc.find_camera('HP')
    print(f"\n[1] Device: {cam.device_name}")

    # Read current exposure
    print("\n[2] Current exposure:")
    current = cam.get_exposure()
    print(f"  value={current}")

    # Test Auto mode
    print("\n[3] Test Auto mode...")
    cam.set_exposure(0, 'auto')
    time.sleep(1)
    current = cam.get_exposure()
    print(f"  After set(0, auto): value={current}")
    cap = cv2.VideoCapture(0)
    time.sleep(1)
    ret, frame = cap.read()
    if ret:
        cv2.imwrite('duvc_auto.jpg', frame)
        print(f"  mean: {frame.mean():.1f}")
    cap.release()

    # Test Manual mode (-5)
    print("\n[4] Test Manual mode (-5)...")
    cam.set_exposure(-5, 'manual')
    time.sleep(1)
    current = cam.get_exposure()
    print(f"  After set(-5, manual): value={current}")
    cap = cv2.VideoCapture(0)
    time.sleep(1)
    ret, frame = cap.read()
    if ret:
        cv2.imwrite('duvc_manual_m5.jpg', frame)
        print(f"  mean: {frame.mean():.1f}")
    cap.release()

    # Test Manual mode (-2)
    print("\n[5] Test Manual mode (-2)...")
    cam.set_exposure(-2, 'manual')
    time.sleep(1)
    current = cam.get_exposure()
    print(f"  After set(-2, manual): value={current}")
    cap = cv2.VideoCapture(0)
    time.sleep(1)
    ret, frame = cap.read()
    if ret:
        cv2.imwrite('duvc_manual_m2.jpg', frame)
        print(f"  mean: {frame.mean():.1f}")
    cap.release()

    # Test Manual mode (0)
    print("\n[6] Test Manual mode (0)...")
    cam.set_exposure(0, 'manual')
    time.sleep(1)
    current = cam.get_exposure()
    print(f"  After set(0, manual): value={current}")
    cap = cv2.VideoCapture(0)
    time.sleep(1)
    ret, frame = cap.read()
    if ret:
        cv2.imwrite('duvc_manual_0.jpg', frame)
        print(f"  mean: {frame.mean():.1f}")
    cap.release()

    # Restore Auto
    print("\n[7] Restore Auto mode...")
    cam.set_exposure(0, 'auto')
    time.sleep(1)
    current = cam.get_exposure()
    print(f"  After set(0, auto): value={current}")
    cap = cv2.VideoCapture(0)
    time.sleep(1)
    ret, frame = cap.read()
    if ret:
        cv2.imwrite('duvc_auto_restored.jpg', frame)
        print(f"  mean: {frame.mean():.1f}")
    cap.release()

    cam.close()
    print("\nDone!")

if __name__ == "__main__":
    main()
