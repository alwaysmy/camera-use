"""camera_pipeline.py — RGB + IR 采集 / 暗场校准 / 配准 / 融合 引擎
================================================================
GUI（ir_fusion_app）、WebUI（camera_webui）、CLI（camctl）三者共用这一份实现，
避免"三个前端三套逻辑"。

职责
----
* ``RgbWorker`` / ``IrWorker`` —— 两路采集团线程（RGB 走 OpenCV/DSHOW，IR 走 Media Foundation）
* ``CameraSession``  —— 会话：参数、最新画面、各视图合成（融合/差分/伪彩/并排…）
* 图像算子          —— ``stretch`` / ``warp_ir`` / ``fuse_lab`` / ``inject_detail`` / ``auto_align``
* ``DarkField``      —— 暗场（遮住镜头）校准：固定图案噪声、热噪点、偏置

关于码流（codec）的实测结论 —— 见 docs/08
-----------------------------------------
单路开流时两者都能上 30fps，YUY2 画质更好：

===================  ==========  ==========
指标                  YUY2        MJPG
===================  ==========  ==========
梯度能量(锐度)        192.4       165.0
色度细节              3.35        3.12
高频块噪声            0.497       0.565
===================  ==========  ==========

但**两路同开**时 USB 带宽打架：RGB 用 YUY2 会从 30fps 崩到 **1.3fps**（拿到的
基本是黑帧），换 MJPG 后 RGB 15fps + IR 31fps 稳定。所以：

* 只用 RGB（IR 关掉）→ ``codec="yuy2"``（画质优先）
* RGB + IR 同时 → ``codec="mjpg"``（否则不可用）
* ``codec="auto"`` → 按 IR 是否启用来决定
"""
from __future__ import annotations

import json
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

# 本机实测标定的默认配准（IR 340x340 → RGB 640x480）
DEFAULT_SCALE = 1.300
DEFAULT_TX = 98.0
DEFAULT_TY = 18.0

CALIB_DIR = Path(__file__).resolve().parent / "calib"


# ═══════════════════════════════════════════════════════════════
# 图像算子
# ═══════════════════════════════════════════════════════════════
def stretch(img: np.ndarray, lo_pct: float = 2.0, hi_pct: float = 98.0) -> np.ndarray:
    """百分位拉伸（比 min-max 抗噪）。"""
    f = img.astype(np.float32)
    lo, hi = np.percentile(f, (lo_pct, hi_pct))
    if hi - lo < 1e-3:
        return np.zeros_like(img)
    return np.clip((f - lo) / (hi - lo) * 255.0, 0, 255).astype(np.uint8)


def affine(scale: float, tx: float, ty: float, rot: float = 0.0) -> np.ndarray:
    c, s = np.cos(rot), np.sin(rot)
    return np.array([[scale * c, -scale * s, tx],
                     [scale * s, scale * c, ty]], np.float32)


def warp_ir(ir: np.ndarray, A: np.ndarray, size: tuple) -> tuple:
    """把 IR 拉到 RGB 视角，同时给出 0~1 的覆盖掩码（边缘羽化，避免硬边）。"""
    warped = cv2.warpAffine(ir, A, size, flags=cv2.INTER_LINEAR)
    ones = np.full(ir.shape, 255, np.uint8)
    mask = cv2.warpAffine(ones, A, size, flags=cv2.INTER_LINEAR)
    mask = cv2.GaussianBlur(mask, (0, 0), 5.0).astype(np.float32) / 255.0
    return warped, mask


def fuse_lab(rgb: np.ndarray, ir_gray: np.ndarray, weight: float,
             mask: Optional[np.ndarray] = None, chroma: float = 1.0) -> np.ndarray:
    """RGB 出彩色、IR 出亮度 —— 暗光彩色化。

    chroma>1 放大色度；**必须先按增益比例模糊降噪再放大**，否则暗光下
    放大的是一屏彩噪而不是颜色。
    """
    lab = cv2.cvtColor(rgb, cv2.COLOR_BGR2LAB)
    L, A, B = cv2.split(lab)
    if chroma != 1.0:
        k = int(3 + 2 * chroma) | 1
        A = cv2.GaussianBlur(A, (k, k), 0)
        B = cv2.GaussianBlur(B, (k, k), 0)
        A = np.clip((A.astype(np.float32) - 128.0) * chroma + 128.0, 0, 255).astype(np.uint8)
        B = np.clip((B.astype(np.float32) - 128.0) * chroma + 128.0, 0, 255).astype(np.uint8)
    w = np.float32(weight) if mask is None else (np.float32(weight) * mask)
    Lf = L.astype(np.float32) * (1.0 - w) + ir_gray.astype(np.float32) * w
    return cv2.cvtColor(cv2.merge([np.clip(Lf, 0, 255).astype(np.uint8), A, B]),
                        cv2.COLOR_LAB2BGR)


def inject_detail(rgb: np.ndarray, ir_gray: np.ndarray, amount: float,
                  mask: Optional[np.ndarray] = None) -> np.ndarray:
    """把 IR 的高频纹理叠到 RGB 亮度上 —— 不动整体曝光，只加细节。"""
    lab = cv2.cvtColor(rgb, cv2.COLOR_BGR2LAB)
    L, A, B = cv2.split(lab)
    blur = cv2.GaussianBlur(ir_gray, (0, 0), 3.0)
    detail = ir_gray.astype(np.float32) - blur.astype(np.float32)
    if mask is not None:
        detail = detail * mask
    Lf = L.astype(np.float32) + detail * amount
    return cv2.cvtColor(cv2.merge([np.clip(Lf, 0, 255).astype(np.uint8), A, B]),
                        cv2.COLOR_LAB2BGR)


def grad_mag(img: np.ndarray) -> np.ndarray:
    g = cv2.GaussianBlur(img, (0, 0), 1.2)
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    return cv2.GaussianBlur(np.sqrt(gx * gx + gy * gy), (0, 0), 1.5)


def auto_align(rgb: np.ndarray, ir: np.ndarray, verbose: bool = False):
    """梯度图 + 多尺度模板匹配，估 (score, scale, tx, ty)。

    跨模态下 ORB/SIFT 会被背景纹理骗走（实测解是错的），梯度相关稳得多。
    """
    H, W = rgb.shape[:2]
    if rgb.mean() < 8:
        return None
    G_rgb = grad_mag(stretch(cv2.cvtColor(rgb, cv2.COLOR_BGR2GRAY)))
    G_ir = grad_mag(stretch(ir))
    best = None
    for s in np.arange(0.45, 2.31, 0.025):
        ws, hs = int(round(ir.shape[1] * s)), int(round(ir.shape[0] * s))
        if ws < 60 or hs < 60 or ws > 3 * W or hs > 3 * H:
            continue
        interp = cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC
        g_s = cv2.resize(G_ir, (ws, hs), interpolation=interp)
        tw, th = int(ws * 0.6), int(hs * 0.6)
        if tw < 40 or th < 40 or tw >= W or th >= H:
            continue
        tx0, ty0 = (ws - tw) // 2, (hs - th) // 2
        tmpl = g_s[ty0:ty0 + th, tx0:tx0 + tw].astype(np.float32)
        res = cv2.matchTemplate(G_rgb.astype(np.float32), tmpl, cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(res)
        if best is None or mx > best[0]:
            best = (float(mx), float(s), float(loc[0] - tx0), float(loc[1] - ty0))
    if best and verbose:
        print("[auto_align] score=%.3f scale=%.3f tx=%d ty=%d" % best)
    return best


def illum_diff(bright: np.ndarray, dark: np.ndarray) -> np.ndarray:
    """补光照明分量 = |亮帧 − 灭帧|（环境红外被抵消）。

    结果**是一张正常图**（补光灯照亮的东西），不是边缘图。
    如果看起来像边缘/满是噪点，说明两帧相位相同（都是亮帧或都是灭帧），
    或者两帧间隔太远（有运动/曝光漂移）。
    """
    d = cv2.absdiff(bright, dark).astype(np.float32)
    return cv2.normalize(d, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)


def measure_illuminator(ir_worker, roi: tuple | None = None, frames: int = 10,
                        sleep: float = 0.05) -> dict:
    """测「补光照明分量」的强度 —— **故意不做归一化**，保证多次测量之间可比。

    用于水吸收法判别波段：同一个容器，空着测一次、装满水测一次，比较 ROI 均值。

    :param roi: ``(x, y, w, h)``，IR 坐标；缺省取画面中心 20% 方框
    """
    vals, refs = [], []
    for _ in range(max(1, frames)):
        if ir_worker is None or ir_worker.bright is None or ir_worker.darkframe is None:
            time.sleep(sleep)
            continue
        raw = cv2.absdiff(ir_worker.bright, ir_worker.darkframe).astype(np.float32)
        if roi:
            x, y, w, h = roi
            vals.append(float(raw[y:y + h, x:x + w].mean()))
        else:
            h, w = raw.shape
            vals.append(float(raw[h // 2 - h // 10: h // 2 + h // 10,
                                  w // 2 - w // 10: w // 2 + w // 10].mean()))
        refs.append(float(raw.mean()))
        time.sleep(sleep)
    if not vals:
        raise DarkNotCovered("拿不到 IR 帧（相机没开或被占用）")
    return {"roi_mean": round(float(np.mean(vals)), 2),
            "frame_mean": round(float(np.mean(refs)), 2),
            "samples": len(vals),
            "led": ir_worker.led_stats()}


def wavelength_verdict(state: dict) -> dict:
    """由两次测量（空容器 / 装满水）给出波段倾向。"""
    if "empty" not in state or "filled" not in state:
        return {"ready": False, "have": list(state.keys()),
                "hint": "需要先测 empty（空容器）再测 filled（装满水）"}
    e, f = state["empty"], state["filled"]
    ratio = f["roi_mean"] / max(1e-6, e["roi_mean"])
    drift = f["frame_mean"] / max(1e-6, e["frame_mean"])
    reliable = 0.7 <= drift <= 1.4
    if ratio >= 0.45:
        verdict, why = "850nm 可能性大", "水对 850nm 吸收弱，光能穿过去"
    elif ratio <= 0.18:
        verdict, why = "940nm 可能性大", "水对 940nm 吸收强，光基本被吃掉"
    else:
        verdict, why = "不确定（过渡区）", "建议换更大的水层厚度（更粗的瓶子）重测"
    return {"ready": True, "ratio_filled_over_empty": round(ratio, 3),
            "brightness_drift": round(drift, 3), "reliable": reliable,
            "verdict": verdict, "reason": why,
            "hint": "" if reliable else
                    "两次测量间整体亮度变化 >30%（自动曝光在漂），建议重测",
            "raw": state}


# ═══════════════════════════════════════════════════════════════
# 暗场校准
# ═══════════════════════════════════════════════════════════════
class DarkNotCovered(RuntimeError):
    """镜头没遮住，暗场校准拿到的还是画面。"""


class DarkField:
    """暗场（遮住镜头拍的）—— 固定图案噪声 + 偏置 + 热噪点。

    原理：像素级 ``暗帧`` 包含读出偏置、放大器偏置、固定图案噪声(FPN)和热噪点，
    这些与场景无关，直接减掉即可提高暗部信噪比、消掉固定花纹。
    ``out = clip(frame - dark, 0, 255)``
    """

    HOT_SIGMA = 6.0        # 超过 均值+N*std 的像素判为热噪点

    def __init__(self, dark: np.ndarray | None = None, hot: np.ndarray | None = None,
                 meta: dict | None = None):
        self.dark = dark
        self.hot = hot
        self.meta = meta or {}

    # ---- 采集 ----
    @staticmethod
    def capture(frames: list, max_mean: float = 32.0, name: str = "camera") -> "DarkField":
        """用一组"遮住镜头"的帧生成暗场。

        :param max_mean: 平均亮度超过它就认为没遮住（默认 32）。
        """
        if not frames:
            raise DarkNotCovered(f"{name}: 没有取到帧")
        stack = np.stack([f.astype(np.float32) for f in frames])
        mean_all = float(stack.mean())
        if mean_all > max_mean:
            raise DarkNotCovered(
                f"{name}: 画面平均亮度 {mean_all:.1f} > {max_mean}，镜头似乎没遮住。\n"
                f"请用不透光的物体（手掌/纸/镜头盖）完全盖住镜头后再校准。")
        dark = np.median(stack, axis=0)          # 中值比均值更抗偶发亮帧
        dev = np.std(stack, axis=0)
        flat = dark if dark.ndim == 2 else dark.mean(axis=2)
        hot = (flat > flat.mean() + DarkField.HOT_SIGMA * max(1.0, flat.std())).astype(np.uint8) * 255
        meta = {"frames": len(frames), "mean": round(mean_all, 2),
                "temporal_std": round(float(dev.mean()), 3),
                "hot_pixels": int((hot > 0).sum()), "shape": list(dark.shape),
                "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
        return DarkField(dark, hot, meta)

    # ---- 应用 ----
    def apply(self, frame: np.ndarray, repair_hot: bool = True) -> np.ndarray:
        if self.dark is None or self.dark.shape != frame.shape:
            return frame
        out = frame.astype(np.float32) - self.dark
        out = np.clip(out, 0, 255).astype(np.uint8)
        if repair_hot and self.hot is not None and self.hot.shape == out.shape[:2]:
            if out.ndim == 2:
                med = cv2.medianBlur(out, 3)
                out = np.where(self.hot > 0, med, out)
            else:
                med = cv2.medianBlur(out, 3)
                m = (self.hot > 0)[:, :, None]
                out = np.where(m, med, out)
        return out

    @property
    def ready(self) -> bool:
        return self.dark is not None

    # ---- 存取 ----
    def save(self, tag: str) -> Path:
        CALIB_DIR.mkdir(parents=True, exist_ok=True)
        path = CALIB_DIR / f"dark_{tag}.npz"
        np.savez_compressed(path, dark=self.dark, hot=self.hot,
                            meta=json.dumps(self.meta, ensure_ascii=False))
        return path

    @staticmethod
    def load(tag: str) -> Optional["DarkField"]:
        path = CALIB_DIR / f"dark_{tag}.npz"
        if not path.exists():
            return None
        z = np.load(path, allow_pickle=True)
        meta = json.loads(str(z["meta"])) if "meta" in z else {}
        return DarkField(z["dark"], z["hot"], meta)

    def __repr__(self):
        if not self.ready:
            return "<DarkField empty>"
        return "<DarkField %s hot=%d mean=%.1f>" % (
            self.dark.shape, int((self.hot > 0).sum()) if self.hot is not None else 0,
            float(self.dark.mean()))


# ═══════════════════════════════════════════════════════════════
# 采集线程
# ═══════════════════════════════════════════════════════════════
def fourcc_name(cap) -> str:
    try:
        v = int(cap.get(cv2.CAP_PROP_FOURCC))
        return "".join(chr((v >> (8 * i)) & 0xFF) for i in range(4)).strip("\x00") or "?"
    except Exception:
        return "?"


class RgbWorker(threading.Thread):
    """OpenCV/DirectShow 采 RGB。

    ``codec``：``"yuy2"``（画质优先，仅单路可用）/ ``"mjpg"``（与 IR 同开时必需）。
    """

    def __init__(self, spec: str = "rgb", width: int = 640, height: int = 480,
                 codec: str = "mjpg", name: str = "rgb"):
        super().__init__(daemon=True)
        self.spec, self.width, self.height = spec, width, height
        self.codec, self.tag = codec, name
        self.frame: np.ndarray | None = None
        self.raw: np.ndarray | None = None
        self.seq = 0                      # 帧号：外部进程靠它判断"有没有新帧"
        self.error: str | None = None
        self.fps = 0.0
        self.fourcc = "?"
        self.device = None
        self.dark: DarkField | None = None
        self._halt = False
        self.cam = None

    def run(self):
        try:
            from camera_core import CameraController
            self.cam = CameraController(self.spec, warmup=5, min_mean=None)
            self.cam.open(width=self.width, height=self.height)
            self.device = self.cam.device
            if self.codec in ("mjpg", "yuy2"):
                cc = cv2.VideoWriter_fourcc(*self.codec.upper())
                # 顺序讲究：改分辨率会复位 FOURCC，所以 设→改分辨率→再设
                self.cam._cap.set(cv2.CAP_PROP_FOURCC, cc)
                self.cam._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
                self.cam._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
                self.cam._cap.set(cv2.CAP_PROP_FOURCC, cc)
            try:
                self.cam.set_exposure(auto=True)
            except Exception:
                pass
            time.sleep(0.4)
            self.fourcc = fourcc_name(self.cam._cap)
        except Exception as e:
            self.error = f"RGB 打开失败: {e}"
            return

        n, t0 = 0, time.time()
        while not self._halt:
            ok, f = self.cam._cap.read()
            if ok and f is not None:
                self.raw = f
                self.frame = self.dark.apply(f) if (self.dark and self.dark.ready) else f
                self.seq += 1
                n += 1
            dt = time.time() - t0
            if dt >= 0.5:
                self.fps, n, t0 = n / dt, 0, time.time()
        self.cam.close()

    def stop(self):
        # 注意：标志位千万不能叫 _stop —— 那是 threading.Thread 的内部方法，
        # 覆盖掉之后 join() 会抛 "'bool' object is not callable"。
        self._halt = True


class IrWorker(threading.Thread):
    """Media Foundation 采 IR；按亮度把最近几帧分成"补光/无补光"两组保存。

    这样 ``diff()`` 拿到的一定是**不同相位**的两帧 —— 用 ``prefer="bright"``
    连续取两张会得到两张都亮的帧，相减就是噪点/边缘，那是错的。
    """

    def __init__(self, warmup: int = 20, recent: int = 8, name: str = "ir"):
        super().__init__(daemon=True)
        self.warmup, self.recent, self.tag = warmup, recent, name
        self.bright: np.ndarray | None = None
        self.darkframe: np.ndarray | None = None
        self.seq = 0
        self.buffer: deque = deque(maxlen=recent)
        self.error: str | None = None
        self.fps = 0.0
        self.device_name = "?"
        self.dark: DarkField | None = None
        self._halt = False
        self.cam = None

    @property
    def frame(self):
        """给融合用的那一帧：补光灯点亮的（更亮更干净）。"""
        return self.bright

    def run(self):
        try:
            from camera_ir import IrCamera
            self.cam = IrCamera(warmup=self.warmup)
            self.cam.open()
            self.device_name = self.cam.device_name
        except Exception as e:
            self.error = f"IR 打开失败: {e}"
            return

        n, t0 = 0, time.time()
        while not self._halt:
            g = self.cam.read(warmup=1, prefer="any")   # 全速：相位由本线程自己分
            if g is not None:
                if self.dark and self.dark.ready:
                    g = self.dark.apply(g)
                self.buffer.append(g)
                b = max(self.buffer, key=lambda f: float(f.mean()))
                d = min(self.buffer, key=lambda f: float(f.mean()))
                self.bright, self.darkframe = b, d
                self.seq += 1
                n += 1
            dt = time.time() - t0
            if dt >= 0.5:
                self.fps, n, t0 = n / dt, 0, time.time()
        self.cam.close()

    def stop(self):
        # 注意：标志位千万不能叫 _stop —— 那是 threading.Thread 的内部方法，
        # 覆盖掉之后 join() 会抛 "'bool' object is not callable"。
        self._halt = True

    def diff(self) -> np.ndarray | None:
        """纯补光照明分量（正常图，不是边缘图）。"""
        if self.bright is None or self.darkframe is None:
            return None
        # 两帧必须真的分属不同相位
        if float(self.bright.mean()) - float(self.darkframe.mean()) < 5.0:
            return None
        return illum_diff(self.bright, self.darkframe)

    def led_stats(self) -> dict:
        if self.bright is None or self.darkframe is None:
            return {}
        return {"led_on_mean": round(float(self.bright.mean()), 1),
                "led_off_mean": round(float(self.darkframe.mean()), 1),
                "led_delta": round(float(self.bright.mean() - self.darkframe.mean()), 1)}


MODES = ("fuse", "detail", "rgb", "ir", "ircolor", "diff", "side")


# ═══════════════════════════════════════════════════════════════
# 会话
# ═══════════════════════════════════════════════════════════════
class CameraSession:
    """RGB + IR 会话：参数、采集线程、各视图合成。供 GUI / WebUI / CLI 共用。"""

    def __init__(self, rgb_spec: str = "rgb", width: int = 640, height: int = 480,
                 codec: str = "auto", use_ir: bool = True, ir_warmup: int = 20,
                 out_dir: str = "captures", auto_start: bool = True,
                 load_dark: bool = True):
        self.width, self.height = width, height
        self.out_dir = Path(out_dir)
        self.use_ir = use_ir
        self.rgb_spec = rgb_spec
        # 码流：两路同开必须 MJPG，否则 RGB 会被抢带宽抢到 1.3fps
        if codec == "auto":
            codec = "mjpg" if use_ir else "yuy2"
        self.codec = codec
        self.params = {
            "mode": "fuse", "weight": 0.62, "gain": 1.6, "chroma": 2.0,
            "scale": DEFAULT_SCALE, "tx": DEFAULT_TX, "ty": DEFAULT_TY, "rot": 0.0,
            "invert_ir": False, "dark_enable": load_dark, "ir_align": True,
        }
        self.rgb = RgbWorker(rgb_spec, width, height, codec)
        self.ir = IrWorker(warmup=ir_warmup) if use_ir else None
        self.locked = threading.Lock()
        self.align_info: dict = {}
        if load_dark:
            d1 = DarkField.load("rgb")
            d2 = DarkField.load("ir")
            if d1 and d1.ready:
                self.rgb.dark = d1
            if d2 and d2.ready and self.ir:
                self.ir.dark = d2
        if auto_start:
            self.start()

    # ---- 生命周期 ----
    def start(self):
        self.rgb.start()
        if self.ir:
            self.ir.start()
        return self

    def stop(self, timeout: float = 2.5):
        """停止采集。**必须 join** —— 否则采集线程可能还卡在 COM 调用里，
        导致进程退出时挂住（这是实测踩过的坑：JSON 已经打印了但进程不退出）。
        """
        self.rgb.stop()
        if self.ir:
            self.ir.stop()
        for th in (self.rgb, self.ir):
            if th is not None and th.is_alive():
                th.join(timeout=timeout)
        # join 不回来也不强求：采集线程是 daemon，CLI 侧会用 os._exit 兜底
        return all(th is None or not th.is_alive() for th in (self.rgb, self.ir))

    def wait_ready(self, timeout: float = 20.0, need_ir: bool = True,
                   require_mean: float = 5.0) -> bool:
        """等两路都出画面（并且不是黑帧）。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            ok_rgb = self.rgb.frame is not None and float(self.rgb.frame.mean()) >= require_mean
            ok_ir = (not need_ir) or (self.ir is not None and self.ir.bright is not None)
            if ok_rgb and ok_ir:
                return True
            if self.rgb.error or (self.ir and self.ir.error):
                return False
            time.sleep(0.15)
        return False

    # ---- 热切换（码流 / 分辨率 / IR 开关）----
    def restart_rgb(self, codec: str | None = None, width: int | None = None,
                    height: int | None = None) -> dict:
        """换码流或分辨率 —— 必须重开采集（改 FOURCC 不能在推流中途生效）。"""
        dark = self.rgb.dark if self.rgb else None
        self.rgb.stop()
        if self.rgb.is_alive():
            self.rgb.join(timeout=2.0)
        if codec:
            self.codec = codec
        if width:
            self.width = width
        if height:
            self.height = height
        self.rgb = RgbWorker(self.rgb_spec, self.width, self.height, self.codec)
        self.rgb.dark = dark
        self.rgb.start()
        return {"codec": self.codec, "width": self.width, "height": self.height}

    def set_ir_enabled(self, enabled: bool) -> dict:
        """开/关 IR —— 带宽不够时（未压缩码流）就该只留一路。"""
        if enabled and self.ir is None:
            self.ir = IrWorker(warmup=20)
            self.ir.dark = DarkField.load("ir")
            self.ir.start()
        elif not enabled and self.ir is not None:
            self.ir.stop()
            if self.ir.is_alive():
                self.ir.join(timeout=2.0)
            self.ir = None
        self.use_ir = enabled
        return {"ir": self.ir is not None}

    # ---- 参数 ----
    def set_params(self, **kw) -> dict:
        with self.locked:
            for k, v in kw.items():
                if k in self.params and v is not None:
                    self.params[k] = v
        return dict(self.params)

    def get_params(self) -> dict:
        return dict(self.params)

    # ---- 视图合成 ----
    def compose(self, mode: str | None = None) -> dict:
        """返回 {'rgb','ir','diff','fused','view','aligned','mask','stats'}。"""
        p = self.params
        mode = mode or p["mode"]
        rgb_raw = self.rgb.frame if self.rgb else None
        ir = self.ir.bright if self.ir else None
        diff = self.ir.diff() if self.ir else None

        ir_aligned = mask = None
        if ir is not None and rgb_raw is not None and p.get("ir_align", True):
            A = affine(float(p["scale"]), float(p["tx"]), float(p["ty"]), np.radians(float(p["rot"])))
            warped, mask = warp_ir(ir, A, (rgb_raw.shape[1], rgb_raw.shape[0]))
            ir_aligned = np.clip(warped.astype(np.float32) * float(p["gain"]), 0, 255).astype(np.uint8)
        elif ir is not None:
            ir_aligned = np.clip(ir.astype(np.float32) * float(p["gain"]), 0, 255).astype(np.uint8)

        view = None
        if mode == "rgb" or rgb_raw is None:
            view = rgb_raw
        elif mode == "ir":
            view = ir_aligned
        elif mode == "ircolor":
            view = cv2.applyColorMap(ir_aligned, cv2.COLORMAP_INFERNO) if ir_aligned is not None else None
        elif mode == "diff":
            view = diff if diff is not None else ir_aligned
        elif mode == "side":
            if ir is not None and rgb_raw is not None:
                h = rgb_raw.shape[0]
                ir_big = cv2.resize(stretch(ir), (int(ir.shape[1] * h / ir.shape[0]), h))
                view = np.hstack([rgb_raw, cv2.cvtColor(ir_big, cv2.COLOR_GRAY2BGR)])
        elif mode == "detail":
            if ir_aligned is not None and rgb_raw is not None:
                view = inject_detail(rgb_raw, ir_aligned, float(p["weight"]) * 2.0, mask)
        else:   # fuse
            if ir_aligned is not None and rgb_raw is not None:
                view = fuse_lab(rgb_raw, ir_aligned, float(p["weight"]), mask, float(p["chroma"]))

        if view is None:
            view = rgb_raw
        stats = {
            "rgb_fps": round(self.rgb.fps, 1) if self.rgb else 0,
            "ir_fps": round(self.ir.fps, 1) if self.ir else 0,
            "rgb_mean": round(float(rgb_raw.mean()), 1) if rgb_raw is not None else None,
            "ir_mean": round(float(ir.mean()), 1) if ir is not None else None,
            "codec": self.rgb.fourcc if self.rgb else "?",
            "mode": mode,
            "rgb_error": self.rgb.error if self.rgb else None,
            "ir_error": self.ir.error if self.ir else None,
            "dark_rgb": bool(self.rgb and self.rgb.dark and self.rgb.dark.ready),
            "dark_ir": bool(self.ir and self.ir.dark and self.ir.dark.ready),
        }
        if self.ir:
            stats.update(self.ir.led_stats())
        return {"rgb": rgb_raw, "ir": ir, "diff": diff, "view": view,
                "ir_aligned": ir_aligned, "mask": mask, "stats": stats}

    # ---- 校准 ----
    def do_align(self) -> dict:
        """用当前画面自动标定配准。"""
        if self.rgb.frame is None or self.ir is None or self.ir.bright is None:
            raise RuntimeError("需要 RGB 和 IR 都出画面才能标定")
        r = auto_align(self.rgb.frame, self.ir.bright, verbose=True)
        if not r:
            raise RuntimeError("自动标定失败（画面太暗或纹理不足）")
        score, s, tx, ty = r
        self.set_params(scale=round(s, 3), tx=round(tx, 1), ty=round(ty, 1), rot=0.0)
        self.align_info = {"score": round(score, 4), "scale": round(s, 3),
                           "tx": round(tx, 1), "ty": round(ty, 1)}
        return self.align_info

    def calibrate_dark(self, target: str = "both", frames: int = 30,
                       settle: float = 0.8) -> dict:
        """暗场校准：**请先完全遮住镜头**。

        :param target: ``rgb`` / ``ir`` / ``both``
        """
        out = {}
        if target in ("rgb", "both") and self.rgb:
            time.sleep(settle)
            got = []
            for _ in range(frames):
                if self.rgb.raw is not None:
                    got.append(self.rgb.raw.copy())
                time.sleep(1 / 30.0)
            df = DarkField.capture(got, name="RGB")
            path = df.save("rgb")
            self.rgb.dark = df
            out["rgb"] = {"meta": df.meta, "file": str(path)}
        if target in ("ir", "both") and self.ir:
            time.sleep(settle)
            got = []
            for _ in range(frames):
                if self.ir.bright is not None:
                    got.append(self.ir.bright.copy())
                time.sleep(1 / 30.0)
            df = DarkField.capture(got, name="IR", max_mean=45.0)   # IR 暗帧基线本来就高
            path = df.save("ir")
            self.ir.dark = df
            out["ir"] = {"meta": df.meta, "file": str(path)}
        if not out:
            raise RuntimeError("没有可校准的相机")
        return out

    def clear_dark(self, target: str = "both"):
        if target in ("rgb", "both") and self.rgb:
            self.rgb.dark = None
            for p in CALIB_DIR.glob("dark_rgb.npz"):
                p.unlink()
        if target in ("ir", "both") and self.ir:
            self.ir.dark = None
            for p in CALIB_DIR.glob("dark_ir.npz"):
                p.unlink()

    # ---- 保存 ----
    def save(self, prefix: str = "") -> dict:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        tag = (prefix + "_") if prefix else ""
        res = self.compose()
        saved = {}
        for key, img in (("rgb", res["rgb"]), ("ir", res["ir"]),
                         ("diff", res["diff"]), ("view", res["view"])):
            if img is None:
                continue
            show = stretch(img) if (img.ndim == 2 and key in ("ir", "diff")) else img
            p = self.out_dir / f"{tag}{key}_{ts}.jpg"
            cv2.imwrite(str(p), show)
            saved[key] = str(p)
        return saved
