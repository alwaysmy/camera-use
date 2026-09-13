#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""camera_webui.py — RGB+IR 摄像头 Web 控制台

纯标准库实现（``http.server`` + MJPEG multipart 流），不依赖 Flask/前端 CDN
（这台机器没有外网）。

功能
----
* 实时画面：融合 / 细节注入 / RGB / IR / IR 伪彩 / **补光差分** / 并排
* 可调：融合权重、IR 增益、彩度、配准 scale/tx/ty/rot、码流(YUY2/MJPG)、IR 对齐开关
* 动作：自动标定、存图、**暗场校准**（遮住镜头）、暗场清除
* 工具：**水吸收法测 IR 波段**（空容器 → 装满水 → 出结论）

接口（也可被 agent 直接调用）
----------------------------
======================================  ===================================
``GET  /``                               控制台页面
``GET  /mjpg?view=fuse&q=80``            MJPEG 视频流
``GET  /snapshot.jpg?view=fuse``         单帧 JPEG
``GET  /api/state``                      参数 + 状态 JSON
``POST /api/set``                        改参数 ``{"weight":0.7,...}``
``POST /api/align``                      自动标定
``POST /api/snapshot``                   存图，返回文件路径
``POST /api/dark``                       暗场校准 ``{"target":"both"}``
``POST /api/dark/clear``                 清除暗场
``POST /api/wavelength``                 ``{"step":"empty|filled|status|reset"}``
======================================  ===================================

启动::

    python camctl.py serve --port 8765
    python camera_webui.py --port 8765 --open
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent))

from camera_pipeline import (CameraSession, DarkNotCovered, MODES,   # noqa: E402
                             measure_illuminator, stretch, wavelength_verdict)

WAVE_FILE = Path(__file__).resolve().parent / "calib" / "wavelength.json"

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>RGB + IR 摄像头控制台</title>
<style>
 :root{--bg:#15171c;--pane:#1e2129;--fg:#e6e8ee;--dim:#8b93a5;--acc:#4da3ff;--ok:#3ecf8e;--warn:#ffb454;--err:#ff6b6b}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--fg);font:13px/1.5 "Segoe UI",system-ui,sans-serif}
 header{padding:10px 16px;background:var(--pane);border-bottom:1px solid #2b3040;display:flex;gap:14px;align-items:baseline}
 header h1{font-size:15px;margin:0;font-weight:600}
 header .sub{color:var(--dim);font-size:12px}
 .wrap{display:flex;gap:14px;padding:14px;align-items:flex-start;flex-wrap:wrap}
 .viewer{flex:1 1 660px;min-width:520px}
 #main{width:100%;max-width:900px;background:#000;border-radius:8px;display:block}
 .thumbs{display:flex;gap:10px;margin-top:10px}
 .thumbs figure{margin:0}
 .thumbs img{width:200px;border-radius:6px;background:#000;display:block}
 .thumbs figcaption{color:var(--dim);font-size:11px;margin-top:4px}
 aside{flex:0 0 360px;background:var(--pane);border-radius:8px;padding:14px}
 fieldset{border:1px solid #2b3040;border-radius:6px;margin:0 0 12px;padding:10px}
 legend{color:var(--acc);font-size:12px;padding:0 6px}
 label{display:flex;align-items:center;gap:8px;margin:7px 0}
 label span.k{flex:0 0 96px;color:var(--dim)}
 input[type=range]{flex:1}
 output{flex:0 0 46px;text-align:right;color:var(--acc);font-variant-numeric:tabular-nums}
 .modes{display:flex;flex-wrap:wrap;gap:6px}
 .modes button,button.act{background:#262b36;color:var(--fg);border:1px solid #333a4a;border-radius:6px;
   padding:6px 11px;cursor:pointer;font-size:12px}
 .modes button.on{background:var(--acc);border-color:var(--acc);color:#04121f;font-weight:600}
 button.act:hover{background:#2f3644}
 button.act.primary{background:var(--acc);color:#04121f;border-color:var(--acc);font-weight:600}
 .row{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
 #status{font-family:ui-monospace,Consolas,monospace;font-size:11.5px;color:var(--dim);
   padding:8px;background:#12141a;border-radius:6px;white-space:pre-wrap;word-break:break-all}
 #msg{margin-top:8px;font-size:12px;min-height:18px}
 .ok{color:var(--ok)} .warn{color:var(--warn)} .err{color:var(--err)}
 #verdict{font-size:14px;font-weight:600}
</style></head><body>
<header>
  <h1>RGB + 红外(IR) 摄像头控制台</h1>
  <span class="sub">融合演示 · 补光差分 · 暗场校准 · 波段判别</span>
</header>
<div class="wrap">
  <div class="viewer">
    <img id="main" src="/mjpg?view=fuse">
    <div class="thumbs">
      <figure><img id="t-rgb" src="/mjpg?view=rgb&q=55"><figcaption>RGB</figcaption></figure>
      <figure><img id="t-ir" src="/mjpg?view=ir&q=55"><figcaption>IR</figcaption></figure>
      <figure><img id="t-diff" src="/mjpg?view=diff&q=55"><figcaption>补光差分（正常图，不是边缘）</figcaption></figure>
      <figure><label style="margin:0"><input type="checkbox" id="thumbsOn" checked> 缩略图</label></figure>
    </div>
  </div>
  <aside>
    <fieldset><legend>视图</legend><div class="modes" id="modes"></div></fieldset>

    <fieldset><legend>融合参数</legend>
      <label><span class="k">融合权重</span><input type="range" id="weight" min="0" max="1" step="0.01"><output id="o-weight"></output></label>
      <label><span class="k">IR 增益</span><input type="range" id="gain" min="0.5" max="4" step="0.05"><output id="o-gain"></output></label>
      <label><span class="k">彩度</span><input type="range" id="chroma" min="0.5" max="4" step="0.1"><output id="o-chroma"></output></label>
      <label><span class="k">码流</span>
        <select id="codec"><option value="mjpg">MJPG（与 IR 同开必须）</option><option value="yuy2">YUY2（画质更好，仅单路）</option></select>
        <span class="sub" style="font-size:11px">改码流需重启服务</span></label>
    </fieldset>

    <fieldset><legend>配准对齐</legend>
      <label><span class="k">scale</span><input type="range" id="scale" min="0.5" max="2.5" step="0.005"><output id="o-scale"></output></label>
      <label><span class="k">tx</span><input type="range" id="tx" min="-300" max="300" step="1"><output id="o-tx"></output></label>
      <label><span class="k">ty</span><input type="range" id="ty" min="-300" max="300" step="1"><output id="o-ty"></output></label>
      <label><span class="k">rot°</span><input type="range" id="rot" min="-10" max="10" step="0.1"><output id="o-rot"></output></label>
      <div class="row"><button class="act" onclick="doAlign()">自动标定</button>
        <button class="act" onclick="resetAlign()">重置对齐</button></div>
    </fieldset>

    <fieldset><legend>动作</legend>
      <div class="row">
        <button class="act primary" onclick="snap()">存图</button>
        <button class="act" onclick="darkCal()">暗场校准</button>
        <button class="act" onclick="darkClear()">清除暗场</button>
      </div>
      <div class="sub" style="font-size:11px;margin-top:8px">
        暗场校准前请用不透光物体把镜头和 IR 窗口<b>完全遮住</b>：
        会采集 30 帧取中值作为暗场，用于扣掉固定图案噪声、偏置和热噪点。
      </div>
    </fieldset>

    <fieldset><legend>水吸收法测 IR 波段</legend>
      <div class="row">
        <button class="act" onclick="wave('empty')">① 空容器（参照）</button>
        <button class="act" onclick="wave('filled')">② 装满水</button>
      </div>
      <div class="row">
        <button class="act" onclick="wave('status')">出结论</button>
        <button class="act" onclick="wave('reset')">重测</button>
      </div>
      <div id="verdict" style="margin-top:8px"></div>
      <div class="sub" style="font-size:11px;margin-top:6px">
        做法：把同一个透明容器放在 IR 镜头正前方（画面中央），后面贴一张白纸。
        先空着测①，再灌满水、位置不动测②。水对 940nm 吸收远强于 850nm，
        比值 &gt;0.45 偏 850nm、&lt;0.18 偏 940nm。
      </div>
    </fieldset>

    <div id="msg"></div>
    <div id="status">连接中…</div>
  </aside>
</div>
<script>
const MODES = [["fuse","融合"],["detail","细节注入"],["rgb","RGB"],["ir","IR"],
               ["ircolor","IR 伪彩"],["diff","补光差分"],["side","并排"]];
let mode = "fuse", dragging = null;
const $ = id => document.getElementById(id);

MODES.forEach(([v,t]) => {
  const b = document.createElement("button");
  b.textContent = t; b.dataset.v = v;
  b.onclick = () => setMode(v);
  $("modes").appendChild(b);
});
function setMode(v){
  mode = v;
  [...$("modes").children].forEach(b => b.classList.toggle("on", b.dataset.v === v));
  $("main").src = "/mjpg?view=" + v + "&t=" + Date.now();
  api("set", {mode: v});
}
const SLIDERS = {weight:"o-weight", gain:"o-gain", chroma:"o-chroma",
                 scale:"o-scale", tx:"o-tx", ty:"o-ty", rot:"o-rot"};
for (const [k, out] of Object.entries(SLIDERS)) {
  const el = $(k);
  el.addEventListener("input", () => {
    $(out).textContent = (+el.value).toFixed(k === "weight" ? 2 : (k === "scale" ? 3 : 1));
    dragging = k;
  });
  el.addEventListener("change", () => { api("set", {[k]: +el.value}); dragging = null; });
}
$("codec").onchange = () => msg("码流改动需要重启服务才能生效（MJPG/YUY2 差异见 docs/08）", "warn");
$("thumbsOn").onchange = e => {
  ["t-rgb","t-ir","t-diff"].forEach(id => {
    $(id).src = e.target.checked ? "/mjpg?view=" + id.slice(2) + "&q=55" : "";
  });
};
async function api(path, body){
  try{
    const r = await fetch("/api/" + path, {method:"POST",
      headers:{"Content-Type":"application/json"}, body: JSON.stringify(body || {})});
    const j = await r.json();
    if (!j.ok) { msg(j.error && j.error.message || "失败", "err"); return null; }
    return j.data;
  }catch(e){ msg("请求失败: " + e, "err"); return null; }
}
async function doAlign(){
  msg("标定中（需要 RGB 和 IR 都有画面）…");
  const d = await api("align");
  if (d) { msg(`标定完成 scale=${d.scale} tx=${d.tx} ty=${d.ty} 相关度=${d.score}`, "ok"); poll(); }
}
function resetAlign(){ api("set",{scale:1.30, tx:98, ty:18, rot:0}).then(poll); }
async function snap(){ const d = await api("snapshot");
  if (d) msg("已保存: " + Object.values(d).map(p=>p.split(/[\\/]/).pop()).join(", "), "ok"); }
async function darkCal(){
  if (!confirm("请先完全遮住镜头（含 IR 窗口），遮好后点确定开始校准。")) return;
  msg("采集暗场中…");
  const d = await api("dark", {target:"both", frames:30});
  if (d) msg("暗场校准完成：" + JSON.stringify(d).slice(0,300), "ok"); else poll0();
}
async function darkClear(){ const d = await api("dark/clear"); if (d) msg("已清除暗场", "ok"); }
async function wave(step){
  msg("测量中…");
  const d = await api("wavelength", {step});
  if (!d) return;
  if (step === "status" || d.ready) {
    $("verdict").innerHTML = d.ready
      ? `<span class="${d.reliable?'ok':'warn'}">${d.verdict}</span><br>
         <span class="sub">比值 ${d.ratio_filled_over_empty}（水/空）· 亮度漂移 ${d.brightness_drift}${d.reliable?"":" ⚠ 不可靠"}</span><br>
         <span class="sub">${d.reason||""} ${d.hint||""}</span>`
      : `<span class="warn">还没有足够数据</span><br><span class="sub">${d.hint||""}</span>`;
  } else {
    msg(`已记录 ${step}: ROI 均值 ${d.roi_mean}（整帧 ${d.frame_mean}）。` +
        (d.next ? " 下一步：" + d.next : ""), "ok");
  }
}
function msg(t, cls){ const m = $("msg"); m.className = cls || ""; m.textContent = t; }
let lastErr = "";
async function poll0(){ try{ const r = await fetch("/api/state"); const j = await r.json();
  if (j.ok) apply(j.data); }catch(e){} }
async function poll(){
  try{
    const j = await (await fetch("/api/state")).json();
    if (!j.ok) return;
    const p = j.data.params, s = j.data.stats;
    for (const [k, out] of Object.entries(SLIDERS)) {
      if (dragging === k) continue;
      const v = p[k];
      if (v !== undefined && Math.abs(+$(k).value - v) > 1e-6) {
        $(k).value = v;
        $(out).textContent = (+v).toFixed(k === "weight" ? 2 : (k === "scale" ? 3 : 1));
      }
    }
    $("status").textContent = j.data.status_text;
    if (s.rgb_error || s.ir_error) { const e = s.rgb_error || s.ir_error;
      if (e !== lastErr) { msg(e, "err"); lastErr = e; } }
  }catch(e){}
}
setMode("fuse");
setInterval(poll, 900); poll();
</script></body></html>
"""


class FrameCache:
    """合成 + 编码的短时缓存：多个客户端共用同一帧，避免 N 倍 CPU。"""

    def __init__(self, session: CameraSession, max_age: float = 0.04):
        self.s = session
        self.max_age = max_age
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, bytes]] = {}

    def jpeg(self, mode: str, quality: int = 80) -> bytes | None:
        key = f"{mode}:{quality}"
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < self.max_age:
                return hit[1]
        img = self.s.compose(mode)["view"]
        if img is None:
            return None
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
        if not ok:
            return None
        data = buf.tobytes()
        with self._lock:
            self._cache[key] = (now, data)
        return data


def _status_text(s: CameraSession) -> str:
    st = s.compose(s.params["mode"])["stats"]
    led = (f"LED 亮{st.get('led_on_mean')}/灭{st.get('led_off_mean')}"
           if st.get("led_on_mean") is not None else "LED -")
    return (f"RGB {st['rgb_fps']:.1f}fps({st['codec']}) mean={st['rgb_mean']} | "
            f"IR {st['ir_fps']:.1f}fps mean={st['ir_mean']} | {led}\n"
            f"暗场: RGB {'有' if st['dark_rgb'] else '无'} / IR {'有' if st['dark_ir'] else '无'} | "
            f"模式={st['mode']}")


class Handler(BaseHTTPRequestHandler):
    session: CameraSession = None
    cache: FrameCache = None
    server_version = "CameraWebUI/1.0"

    def log_message(self, fmt, *args):        # 静音访问日志
        pass

    # ---- 工具 ----
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data=None, error=None, code=200):
        obj = {"ok": error is None}
        if error is None:
            obj["data"] = data
        else:
            obj["error"] = error
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    # ---- GET ----
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if u.path == "/api/state":
            return self._json({"params": self.session.get_params(),
                               "stats": self.session.compose(self.session.params["mode"])["stats"],
                               "status_text": _status_text(self.session)})
        if u.path == "/snapshot.jpg":
            mode = (q.get("view") or ["fuse"])[0]
            data = self.cache.jpeg(mode, 95)
            if data is None:
                return self._json(error={"code": "NO_VIEW", "message": "还没有画面"}, code=503)
            return self._send(200, data, "image/jpeg")
        if u.path == "/mjpg":
            return self._mjpeg((q.get("view") or ["fuse"])[0], int((q.get("q") or [80])[0]),
                               float((q.get("fps") or [20])[0]))
        return self._json(error={"code": "NOT_FOUND", "message": u.path}, code=404)

    def _mjpeg(self, mode: str, quality: int, fps: float):
        boundary = b"--frame\r\n"
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        interval = 1.0 / max(1.0, min(60.0, fps))
        try:
            while True:
                t0 = time.time()
                data = self.cache.jpeg(mode, quality)
                if data is not None:
                    self.wfile.write(boundary)
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(b"Content-Length: %d\r\n\r\n" % len(data))
                    self.wfile.write(data)
                    self.wfile.write(b"\r\n")
                dt = time.time() - t0
                if dt < interval:
                    time.sleep(interval - dt)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    # ---- POST ----
    def do_POST(self):
        u = urlparse(self.path)
        body = self._body()
        try:
            if u.path == "/api/set":
                return self._json(self.session.set_params(**body))
            if u.path == "/api/align":
                return self._json(self.session.do_align())
            if u.path == "/api/snapshot":
                return self._json(self.session.save())
            if u.path == "/api/dark":
                target = body.get("target", "both")
                frames = int(body.get("frames", 30))
                try:
                    return self._json(self.session.calibrate_dark(target=target, frames=frames))
                except DarkNotCovered as e:
                    return self._json(error={"code": "NOT_COVERED", "message": str(e),
                                             "hint": "用不透光物体完全遮住镜头（IR 窗口也要遮）"},
                                      code=409)
            if u.path == "/api/dark/clear":
                self.session.clear_dark(body.get("target", "both"))
                return self._json({"cleared": True})
            if u.path == "/api/wavelength":
                step = body.get("step", "status")
                if step == "reset":
                    WAVE_FILE.unlink(missing_ok=True)
                    return self._json({"reset": True})
                state = json.loads(WAVE_FILE.read_text(encoding="utf-8")) if WAVE_FILE.exists() else {}
                if step == "status":
                    return self._json(wavelength_verdict(state))
                roi = body.get("roi")
                m = measure_illuminator(self.session.ir, roi=tuple(roi) if roi else None,
                                        frames=int(body.get("frames", 12)))
                state[step] = m
                WAVE_FILE.parent.mkdir(parents=True, exist_ok=True)
                WAVE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
                out = {"step": step, **m}
                out.update(wavelength_verdict(state))
                return self._json(out)
        except Exception as e:
            return self._json(error={"code": type(e).__name__, "message": str(e)}, code=500)
        return self._json(error={"code": "NOT_FOUND", "message": u.path}, code=404)


def make_server(host="127.0.0.1", port=8765, device="rgb", codec="auto",
                use_ir=True, out_dir="captures"):
    session = CameraSession(rgb_spec=device, codec=codec, use_ir=use_ir, out_dir=out_dir)
    Handler.session = session
    Handler.cache = FrameCache(session)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd, session


def serve(host="127.0.0.1", port=8765, device="rgb", codec="auto", use_ir=True,
          open_browser=False, out_dir="captures", wait_timeout=20.0) -> int:
    httpd, session = make_server(host, port, device, codec, use_ir, out_dir)
    url = f"http://{host}:{port}/"
    print(f"[camera_webui] 控制台: {url}   (Ctrl+C 退出)", flush=True)
    if not session.wait_ready(timeout=wait_timeout, need_ir=use_ir):
        errs = [e for e in (session.rgb.error, session.ir.error if session.ir else None) if e]
        print("[camera_webui] 警告: " + ("；".join(errs) or "等待画面超时"), file=sys.stderr, flush=True)
    if open_browser:
        threading.Thread(target=lambda: (time.sleep(1.0), webbrowser.open(url)), daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[camera_webui] 退出中…", flush=True)
    finally:
        httpd.shutdown()
        session.stop()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="RGB+IR 摄像头 Web 控制台")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--device", default="rgb")
    ap.add_argument("--codec", default="auto", choices=["auto", "yuy2", "mjpg"])
    ap.add_argument("--no-ir", action="store_true")
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--out-dir", default="captures")
    a = ap.parse_args(argv)
    return serve(a.host, a.port, a.device, a.codec, not a.no_ir, a.open, a.out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
