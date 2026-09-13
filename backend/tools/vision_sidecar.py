# -*- coding: utf-8 -*-
"""vision_sidecar.py — 旁路视觉推理（YOLO 人脸 + YOLOv8n-pose 骨架）

定位：这类 DNN 重活不塞进 Go 主程序（保持主程序零依赖、离线可构建）。
本进程只做三件事：从后端拿**干净帧** → onnxruntime 推理 → 把结构化结果 POST 回去。
后端负责画框与状态展示；本进程不跑也不影响其它功能（人脸自动回退到原生 Viola-Jones）。

用法
  python tools/vision_sidecar.py                       # 连 http://127.0.0.1:8770，5fps
  python tools/vision_sidecar.py --fps 3 --view fuse    # 换帧源与频率
  python tools/vision_sidecar.py --image x.jpg          # 单张图跑一次（联调/自检用）
  python tools/vision_sidecar.py --save-annotated out/  # 顺便存标注图

模型：models/yolov8n-face.onnx（12MB）与 models/yolov8n-pose.onnx（13MB），
一次性导出（tools/export_models.py），运行时不需要联网、不需要 ultralytics。
"""
from __future__ import annotations

import argparse
import io as _io
import json
import os
import sys
import time
import urllib.request

import cv2
import numpy as np
import onnxruntime as ort

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MODELS = os.path.join(ROOT, "models")

COCO17 = [(0, 1), (0, 2), (1, 3), (2, 4), (5, 7), (7, 9), (6, 8), (8, 10),
          (5, 6), (5, 11), (6, 12), (11, 12), (11, 13), (13, 15), (12, 14), (14, 16)]

# 手部 21 点骨架 + 五指判定（MediaPipe 关键点定义）
HAND_BONES = [(0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8),
              (5, 9), (9, 10), (10, 11), (11, 12), (9, 13), (13, 14), (14, 15), (15, 16),
              (13, 17), (17, 18), (18, 19), (19, 20), (0, 17)]
GESTURE_NAMES = {(1, 1, 1, 1, 1): "五指张开", (0, 0, 0, 0, 0): "握拳",
                 (0, 1, 0, 0, 0): "单指", (0, 1, 1, 0, 0): "剪刀手", (1, 0, 0, 0, 0): "竖大拇指",
                 (1, 1, 0, 0, 1): "六", (1, 0, 0, 0, 1): "Shaka", (0, 0, 0, 0, 1): "小指",
                 (0, 1, 1, 1, 0): "三", (0, 1, 1, 1, 1): "四", (1, 1, 1, 0, 0): "OK/捏",
                 (0, 0, 0, 1, 0): "无名指", (0, 1, 0, 0, 1): "摇滚"}


class Hands:
    """MediaPipe Hands：21 点 + 五指伸展判定（判据=指尖到腕距离 vs 中间关节到腕距离）。"""

    def __init__(self, model_path: str, num_hands: int = 2):
        import mediapipe as mp
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision
        self._mp = mp
        # VIDEO 模式 + 逐帧时间戳：MediaPipe 会把手部位置**跟踪**起来，
        # 比默认的逐帧检测稳得多（实测逐帧检测在 1080p 下会"有时有有时没有"地闪）。
        opts = vision.HandLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_path=model_path),
            running_mode=vision.RunningMode.VIDEO,
            num_hands=num_hands, min_hand_detection_confidence=0.3,
            min_hand_presence_confidence=0.3, min_tracking_confidence=0.3)
        self.det = vision.HandLandmarker.create_from_options(opts)
        self._t0 = time.time()

    @staticmethod
    def _d(lm, a, b):
        import math
        return math.hypot(lm[a].x - lm[b].x, lm[a].y - lm[b].y)

    def _fingers(self, lm):
        st = [self._d(lm, 4, 17) > self._d(lm, 2, 17) * 1.05]
        for pip, tip in ((6, 8), (10, 12), (14, 16), (18, 20)):
            st.append(self._d(lm, tip, 0) > self._d(lm, pip, 0) * 1.02)
        return st

    def run(self, bgr):
        import cv2
        mp = self._mp
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        t0 = time.perf_counter()
        ts_ms = int((time.time() - self._t0) * 1000)
        if ts_ms <= getattr(self, "_last_ts", -1):
            ts_ms = getattr(self, "_last_ts", -1) + 1
        self._last_ts = ts_ms
        res = self.det.detect_for_video(img, ts_ms)
        ms = (time.perf_counter() - t0) * 1000.0
        out = []
        for i, lm in enumerate(res.hand_landmarks):
            st = self._fingers(lm)
            name = GESTURE_NAMES.get(tuple(int(x) for x in st), "".join("1" if x else "0" for x in st))
            hand = "?"
            try:
                hand = res.handedness[i][0].category_name
            except Exception:
                pass
            out.append({"hand": hand, "gesture": name,
                        "fingers": [int(x) for x in st],
                        "kpts": [c for p in lm for c in (p.x, p.y, 1.0)]})
        return out, ms


def letterbox(img, new=640, color=114):
    h, w = img.shape[:2]
    r = min(new / h, new / w)
    nh, nw = int(round(h * r)), int(round(w * r))
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((new, new, 3), color, dtype=np.uint8)
    top, left = (new - nh) // 2, (new - nw) // 2
    canvas[top:top + nh, left:left + nw] = resized
    return canvas, r, left, top


def nms(boxes, scores, thr=0.45):
    if len(boxes) == 0:
        return []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
        order = order[1:][iou <= thr]
    return keep


def pick_providers(prefer: str = "auto"):
    """挑最快的执行后端：DirectML(GPU) > CUDA > CPU。
    实测（RTX 5070 Ti，640 输入）：face+pose 从 58ms(CPU 16T) 降到 3.1ms，约 19x。"""
    avail = ort.get_available_providers()
    if prefer and prefer != "auto":
        want = [p for p in prefer.split(",") if p]
        if all(p in avail for p in want):
            return want + ["CPUExecutionProvider"]
    for p in ("DmlExecutionProvider", "CUDAExecutionProvider", "CoreMLExecutionProvider"):
        if p in avail:
            return [p, "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]


class Yolo:
    def __init__(self, path: str, is_pose: bool, threads: int = 4, providers=None):
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        so.log_severity_level = 3
        self.providers = providers or ["CPUExecutionProvider"]
        self.sess = ort.InferenceSession(path, so, providers=self.providers)
        self.inp = self.sess.get_inputs()[0].name
        self.out = self.sess.get_outputs()[0].name
        self.is_pose = is_pose

    def run(self, bgr, conf=0.25):
        img, r, dx, dy = letterbox(bgr)
        x = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        x = np.transpose(x, (2, 0, 1))[None]
        t0 = time.perf_counter()
        y = self.sess.run([self.out], {self.inp: x})[0]
        ms = (time.perf_counter() - t0) * 1000.0
        y = y[0].T
        scores = y[:, 4]
        m = scores > conf
        if not m.any():
            return [], ms
        y, scores = y[m], scores[m]
        cx, cy, bw, bh = y[:, 0], y[:, 1], y[:, 2], y[:, 3]
        boxes = np.stack([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], 1)
        out = []
        for i in nms(boxes, scores):
            b = boxes[i]
            x1, y1 = (b[0] - dx) / r, (b[1] - dy) / r
            x2, y2 = (b[2] - dx) / r, (b[3] - dy) / r
            if self.is_pose:
                kp = y[i][5:].reshape(-1, 3)
                out.append({"box": [float(x1), float(y1), float(x2), float(y2)], "score": float(scores[i]),
                            "kpts": [[float((kp[k, 0] - dx) / r), float((kp[k, 1] - dy) / r), float(kp[k, 2])]
                                     for k in range(kp.shape[0])]})
            else:
                out.append({"box": [float(x1), float(y1), float(x2), float(y2)], "score": float(scores[i])})
        return out, ms


def annotate(bgr, faces, poses):
    vis = bgr.copy()
    for f in faces:
        x1, y1, x2, y2 = [int(v) for v in f["box"]]
        cv2.rectangle(vis, (x1, y1), (x2, y2), (0, 255, 0), 2)
        cv2.putText(vis, "face %.2f" % f["score"], (x1, max(12, y1 - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
    for p in poses:
        x1, y1, x2, y2 = [int(v) for v in p["box"]]
        cv2.rectangle(vis, (x1, y1), (x2, y2), (255, 160, 0), 2)
        kp = np.array(p["kpts"]).reshape(-1, 3)
        for a, b in COCO17:
            if kp[a, 2] > 0.5 and kp[b, 2] > 0.5:
                cv2.line(vis, (int(kp[a, 0]), int(kp[a, 1])), (int(kp[b, 0]), int(kp[b, 1])), (0, 255, 255), 2)
        for k in kp:
            if k[2] > 0.5:
                cv2.circle(vis, (int(k[0]), int(k[1])), 3, (255, 0, 255), -1)
    return vis


def infer(face_model, pose_model, hand_model, bgr):
    faces, ms_f = face_model.run(bgr)
    poses, ms_p = pose_model.run(bgr)
    hands, ms_h = (hand_model.run(bgr) if hand_model else ([], 0.0))
    # 人脸框统一成 x,y,w,h；姿态的 box/kpts 原样（后端按 [x1,y1,x2,y2] 画）
    f_out = [{"x": f["box"][0], "y": f["box"][1],
              "w": f["box"][2] - f["box"][0], "h": f["box"][3] - f["box"][1],
              "score": f["score"]} for f in faces]
    p_out = [{"box": p["box"], "score": p["score"],
              "kpts": [c for pt in p["kpts"] for c in pt]} for p in poses]
    return f_out, p_out, hands, ms_f, ms_p, ms_h


def post_result(api: str, faces, poses, hands, w, h, ms_f, ms_p, ms_h):
    body = json.dumps({"faces": faces, "poses": poses, "hands": hands,
                       "model": "yolov8n-face+yolov8n-pose+mediapipe-hands",
                       "ms_face": ms_f, "ms_pose": ms_p, "ms_hand": ms_h,
                       "frame_w": w, "frame_h": h}).encode()
    req = urllib.request.Request(api.rstrip("/") + "/api/vision", data=body,
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def get_frame(api: str, view: str, q: int):
    url = f"{api.rstrip('/')}/frame.jpg?view={view}&q={q}"
    data = urllib.request.urlopen(url, timeout=10).read()
    arr = np.frombuffer(data, np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://127.0.0.1:8770")
    ap.add_argument("--fps", type=float, default=0.0, help="0=不限速（默认，跑满为止）；>0 为节流上限")
    ap.add_argument("--view", default="fuse", help="取哪一路画面做推理：fuse(默认,更亮)/rgb/ir")
    ap.add_argument("--q", type=int, default=85, help="取帧 JPEG 质量")
    ap.add_argument("--conf", type=float, default=0.25)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--no-hands", dest="hands", action="store_false", help="关掉手部检测")
    ap.add_argument("--providers", default="auto", help="auto|DmlExecutionProvider|CUDAExecutionProvider|CPUExecutionProvider")
    ap.add_argument("--image", default="", help="单张图模式（联调/自检），跑一次就退出")
    ap.add_argument("--save-annotated", default="", help="顺便把标注图存到这个目录")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    fpath = os.path.join(MODELS, "yolov8n-face.onnx")
    ppath = os.path.join(MODELS, "yolov8n-pose.onnx")
    for p in (fpath, ppath):
        if not os.path.exists(p):
            print(f"[sidecar] 缺模型 {p}，先跑 python tools/export_models.py", file=sys.stderr)
            return 2
    provs = pick_providers(args.providers)
    face_model = Yolo(fpath, False, args.threads, provs)
    pose_model = Yolo(ppath, True, args.threads, provs)
    hand_model = None
    hpath = os.path.join(MODELS, "mediapipe", "hand_landmarker.task")
    if args.hands and os.path.exists(hpath):
        try:
            hand_model = Hands(hpath)
            if not args.quiet:
                print(f"[sidecar] 手部模型: MediaPipe Hands ({os.path.getsize(hpath)/1e6:.1f}MB)，21 点 + 五指判定")
        except Exception as e:  # noqa: BLE001
            print("[sidecar] 手部模型加载失败（跳过）:", e, file=sys.stderr)
    if not args.quiet:
        print(f"[sidecar] 模型就绪 (face {os.path.getsize(fpath)/1e6:.1f}MB, "
              f"pose {os.path.getsize(ppath)/1e6:.1f}MB)")
        print(f"[sidecar] 推理后端: {provs[0]}（可用: {', '.join(ort.get_available_providers())}）"
              f" api={args.api} view={args.view} fps={args.fps}")
        sys.stdout.flush()

    if args.image:
        bgr = cv2.imread(args.image)
        if bgr is None:
            print("[sidecar] 读不到图", args.image, file=sys.stderr)
            return 2
        h, w = bgr.shape[:2]
        faces, poses, hands, ms_f, ms_p, ms_h = infer(face_model, pose_model, hand_model, bgr)
        print(f"[sidecar] {os.path.basename(args.image)} {w}x{h} → 人脸 {len(faces)} 骨架 {len(poses)} 手 {len(hands)}"
              f"（face {ms_f:.0f}ms pose {ms_p:.0f}ms hand {ms_h:.0f}ms）")
        for h in hands:
            print(f"    hand {h['hand']} 五指={h['fingers']} → {h['gesture']}")
        for f in faces[:4]:
            print(f"    face [{f['x']:.0f},{f['y']:.0f} {f['w']:.0f}x{f['h']:.0f} {f['score']:.2f}]")
        for p in poses[:4]:
            n = sum(1 for i in range(17) if p["kpts"][i * 3 + 2] > 0.5)
            print(f"    pose [{p['box'][0]:.0f},{p['box'][1]:.0f} {p['box'][2]-p['box'][0]:.0f}x"
                  f"{p['box'][3]-p['box'][1]:.0f} {p['score']:.2f} kpts={n}]")
        if args.save_annotated:
            os.makedirs(args.save_annotated, exist_ok=True)
            cv2.imwrite(os.path.join(args.save_annotated, "ann_" + os.path.basename(args.image)),
                        annotate(bgr, [{"box": [f["x"], f["y"], f["x"] + f["w"], f["y"] + f["h"]], "score": f["score"]} for f in faces],
                                 poses))
            print("[sidecar] 标注图:", os.path.join(args.save_annotated, "ann_" + os.path.basename(args.image)))
        try:
            r = post_result(args.api, faces, poses, hands, w, h, ms_f, ms_p, ms_h)
            print("[sidecar] 已回传:", r)
        except Exception as e:  # noqa: BLE001
            print("[sidecar] 回传失败:", e, file=sys.stderr)
        return 0

    # fps<=0 表示**不限制**：跑完一帧立刻跑下一帧（速度上限由取帧+推理决定）
    unlimited = args.fps <= 0
    period = 0.0 if unlimited else 1.0 / max(0.2, args.fps)
    fails = 0
    while True:
        t0 = time.perf_counter()
        try:
            bgr = get_frame(args.api, args.view, args.q)
            if bgr is None:
                raise RuntimeError("取帧解码失败")
            h, w = bgr.shape[:2]
            faces, poses, hands, ms_f, ms_p, ms_h = infer(face_model, pose_model, hand_model, bgr)
            post_result(args.api, faces, poses, hands, w, h, ms_f, ms_p, ms_h)
            fails = 0
            if not args.quiet:
                gs = " ".join(h["gesture"] for h in hands) if hands else "-"
            print(f"[sidecar] {w}x{h} 人脸 {len(faces)} 骨架 {len(poses)} 手 {len(hands)}[{gs}] "
                      f"(face {ms_f:.0f} pose {ms_p:.0f} hand {ms_h:.0f}ms 总 {((time.perf_counter()-t0)*1000):.0f}ms)")
        except Exception as e:  # noqa: BLE001
            fails += 1
            if fails in (1, 10) or fails % 60 == 0:
                print(f"[sidecar] 第 {fails} 次失败: {e}", file=sys.stderr)
        if not unlimited:
            dt = period - (time.perf_counter() - t0)
            if dt > 0:
                time.sleep(dt)


if __name__ == "__main__":
    sys.exit(main())
