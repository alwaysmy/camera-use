# -*- coding: utf-8 -*-
"""把 yolov8n-pose.pt / yolov8n-face.pt 导出为 ONNX（一次性，运行时不联网）。"""
import os
import shutil
import sys

os.environ.setdefault("YOLO_CONFIG_DIR", os.path.abspath("models/.ultralytics"))
os.makedirs(os.environ["YOLO_CONFIG_DIR"], exist_ok=True)

from ultralytics import YOLO  # noqa: E402

os.chdir(r"<REPO>\backend")

jobs = [("models/yolov8n-pose.pt", "models/yolov8n-pose.onnx"),
        ("models/yolov8n-face.pt", "models/yolov8n-face.onnx")]

for pt, onnx in jobs:
    if not os.path.exists(pt):
        print(f"[skip] {pt} 不存在")
        continue
    if os.path.exists(onnx):
        print(f"[skip] {onnx} 已存在 {os.path.getsize(onnx)/1e6:.1f} MB")
        continue
    print(f"[export] {pt} → {onnx}")
    m = YOLO(pt)
    out = m.export(format="onnx", imgsz=640, opset=12, simplify=False, dynamic=False)
    print("   导出结果:", out)
    # ultralytics 会输出到同目录同名 .onnx
    cand = pt.replace(".pt", ".onnx")
    if os.path.exists(cand) and cand != onnx:
        shutil.move(cand, onnx)
    if os.path.exists(onnx):
        print(f"   OK {os.path.getsize(onnx)/1e6:.1f} MB  → {onnx}")
    else:
        print("   失败：没找到导出的 onnx")

print("\nmodels/ 目录:")
for f in sorted(os.listdir("models")):
    p = os.path.join("models", f)
    if os.path.isfile(p):
        print(f"  {f:28s} {os.path.getsize(p)/1e6:8.2f} MB")
