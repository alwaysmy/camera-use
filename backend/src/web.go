// web.go — HTTP 服务 + 嵌入式前端（单条 MJPEG 流 + 可见性暂停 + 节流轮询）
package main

import (
	"context"
	"image"
	"encoding/json"
	"fmt"
	"image/jpeg"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"sync"
	"time"
)

var mimeB = []byte("--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ")

// handleFrame —— 给旁路进程喂的**干净帧**（不叠加任何框，免得它把框当人）
// ?view=rgb|fuse|ir|diff，默认 fuse（融合图更亮，暗光下检测更稳）；?q= 质量
func (e *Engine) handleFrame(w http.ResponseWriter, r *http.Request) {
	view := r.URL.Query().Get("view")
	if view == "" {
		view = "fuse"
	}
	q := 85
	if s := r.URL.Query().Get("q"); s != "" {
		if v, e2 := strconv.Atoi(s); e2 == nil && v >= 30 && v <= 95 {
			q = v
		}
	}
	// ★ 缓存必须在**渲染之前**查。
	// 之前写在渲染之后，等于每来一次请求照样先跑一遍完整融合渲染（buildCtx + fuseLAB），
	// 只省下 JPEG 编码那一点 —— 实测旁路不限速取图时，Go 进程被这一项吃到 1200%+ 单核。
	key := view + "|" + strconv.Itoa(q)
	e.frameMu.Lock()
	if len(e.frameBuf) > 0 && e.frameView == key && time.Since(e.frameAt) < 150*time.Millisecond {
		buf := e.frameBuf
		e.frameMu.Unlock()
		w.Header().Set("Content-Type", "image/jpeg")
		w.Header().Set("Cache-Control", "no-store")
		_, _ = w.Write(buf)
		return
	}
	e.frameMu.Unlock()

	var img *image.YCbCr
	var err error
	img, err = e.renderView(view)
	if err != nil || img == nil {
		http.Error(w, "没有画面", 503)
		return
	}
	w.Header().Set("Content-Type", "image/jpeg")
	w.Header().Set("Cache-Control", "no-store")
	b := newByteWriter(1 << 18)
	_ = jpeg.Encode(b, img, &jpeg.Options{Quality: q})
	out := b.Bytes()
	e.frameMu.Lock()
	e.frameBuf, e.frameAt, e.frameView = out, time.Now(), key
	e.frameMu.Unlock()
	_, _ = w.Write(out)
}

func (e *Engine) handleDarkEnable(w http.ResponseWriter, r *http.Request) {
var m struct {
On bool `json:"on"`
}
_ = json.NewDecoder(r.Body).Decode(&m)
e.SetDarkEnabled(m.On)
jsonOut(w, map[string]any{"dark_on": m.On}, nil)
}

func (e *Engine) handleGamma(w http.ResponseWriter, r *http.Request) {
if r.Method == http.MethodGet {
jsonOut(w, e.GammaInfo(), nil)
return
}
var m struct {
Software *float64 `json:"software"`
Camera   *float64 `json:"camera"`
}
_ = json.NewDecoder(r.Body).Decode(&m)
if m.Software != nil {
g := *m.Software
if g < 0.2 {
g = 0.2
}
if g > 4 {
g = 4
}
e.SetParams(func(pp *Params) { pp.Gamma = g })
}
if m.Camera != nil {
rgbS, _, _, _ := e.Snapshot()
if rgbS != nil {
if err := rgbS.VideoProcAmpSet(5, int32(*m.Camera), 2); err != nil {
jsonOut(w, nil, err)
return
}
}
}
jsonOut(w, e.GammaInfo(), nil)
}

func (e *Engine) handleEvents(w http.ResponseWriter, r *http.Request) {
	since := uint64(0)
	if v := r.URL.Query().Get("since"); v != "" {
		if n, err := strconv.ParseUint(v, 10, 64); err == nil {
			since = n
		}
	}
	ev := e.events.since(since, 100)
	last := since
	if len(ev) > 0 {
		last = ev[len(ev)-1].ID
	}
	jsonOut(w, map[string]any{"events": ev, "last": last}, nil)
}

func (e *Engine) handleVision(w http.ResponseWriter, r *http.Request) {
	// 不要再嵌一层匿名结构重复声明 frame_w/frame_h —— json tag 冲突会让解码报
	// "cannot unmarshal object into Go struct field .frame_h"（踩过）
	var vr VisionResult
	if err := json.NewDecoder(r.Body).Decode(&vr); err != nil {
		jsonOut(w, nil, err)
		return
	}
	e.vision.set(vr, vr.FrameW, vr.FrameH)
	jsonOut(w, map[string]any{"ok": true, "faces": len(vr.Faces),
		"poses": len(vr.Poses), "hands": len(vr.Hands)}, nil)
}

func (e *Engine) handleMJPEG(w http.ResponseWriter, r *http.Request) {
	fl, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "no flush", 500)
		return
	}
	w.Header().Set("Content-Type", "multipart/x-mixed-replace; boundary=frame")
	w.Header().Set("Cache-Control", "no-store, no-cache")

	// 事件驱动：等广播器通知"有新帧"，不再每个客户端各自 sleep 轮询。
	// N 个客户端也只等一次，空闲时零唤醒。
	e.clients.Add(1)
	defer e.clients.Add(-1)
	var last uint64
	ctx := r.Context()
	for {
		seq, data, ok := e.bcast.wait(ctx, last)
		if !ok {
			return
		}
		last = seq
		if len(data) == 0 {
			continue
		}
		if err := writeFrame(w, data); err != nil {
			return
		}
		fl.Flush()
	}
}

func writeFrame(w io.Writer, data []byte) error {
	if _, err := w.Write(mimeB); err != nil {
		return err
	}
	if _, err := w.Write([]byte(strconv.Itoa(len(data)))); err != nil {
		return err
	}
	if _, err := w.Write([]byte("\r\n\r\n")); err != nil {
		return err
	}
	if _, err := w.Write(data); err != nil {
		return err
	}
	_, err := w.Write([]byte("\r\n"))
	return err
}

func (e *Engine) handleSnapshotJPEG(w http.ResponseWriter, r *http.Request) {
	img, err := e.renderMosaic()
	if err != nil {
		http.Error(w, err.Error(), 503)
		return
	}
	w.Header().Set("Content-Type", "image/jpeg")
	_ = jpeg.Encode(w, img, &jpeg.Options{Quality: 95})
}

func jsonOut(w http.ResponseWriter, data any, err error) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	obj := map[string]any{"ok": err == nil}
	if err != nil {
		obj["error"] = map[string]string{"message": err.Error()}
	} else {
		obj["data"] = data
	}
	_ = json.NewEncoder(w).Encode(obj)
}

func (e *Engine) handleState(w http.ResponseWriter, r *http.Request) {
	st, p, cfg := e.Stats()
	jsonOut(w, map[string]any{"stats": st, "params": p, "config": cfg, "views": viewList()}, nil)
}

func viewList() []map[string]string {
	return []map[string]string{
		{"v": "fuse", "t": "融合"}, {"v": "detail", "t": "细节注入"},
		{"v": "rgb", "t": "RGB"}, {"v": "ir", "t": "IR"},
		{"v": "ircolor", "t": "IR 伪彩"}, {"v": "diff", "t": "补光差分"},
		{"v": "side", "t": "并排"},
	}
}

func (e *Engine) handleSet(w http.ResponseWriter, r *http.Request) {
	var m map[string]any
	if err := json.NewDecoder(r.Body).Decode(&m); err != nil {
		jsonOut(w, nil, err)
		return
	}
	p := e.SetParams(func(pp *Params) {
		if v, ok := m["mode"].(string); ok {
			pp.Mode = v
		}
		set := func(key string, dst *float64) {
			if v, ok := m[key].(float64); ok {
				*dst = v
			}
		}
		set("weight", &pp.Weight)
		set("gain", &pp.Gain)
		set("chroma", &pp.Chroma)
		set("scale", &pp.Scale)
		set("tx", &pp.Tx)
		set("ty", &pp.Ty)
		set("rot", &pp.Rot)
		set("stretch", &pp.Stretch)
		set("gamma", &pp.Gamma)
		set("chroma_dark", &pp.ChromaDark)
		set("fps", &pp.FPS)
		if v, ok := m["quality"].(float64); ok {
			pp.Quality = int(v)
		}
		if v, ok := m["chroma_blur"].(float64); ok {
			pp.ChromaBlur = int(v)
		}
		if v, ok := m["chroma_gain"].(float64); ok {
			pp.ChromaGain = v
		}
		for k, dst := range map[string]*bool{"face_detect": &pp.FaceDetect,
			"person_detect": &pp.PersonDetect, "person_box": &pp.PersonBox,
			"yolo_face": &pp.YoloFace, "yolo_pose": &pp.YoloPose, "yolo_hand": &pp.YoloHand,
			"face_wb": &pp.FaceWB, "face_box": &pp.FaceBox} {
			if v, ok := m[k].(bool); ok {
				*dst = v
			}
		}
		if v, ok := m["white_balance"].(float64); ok {
			pp.WhiteBalance = v
		}
		if v, ok := m["mirror_h"].(bool); ok {
			pp.MirrorH = v
		}
		if v, ok := m["mirror_v"].(bool); ok {
			pp.MirrorV = v
		}
		if v, ok := m["thumbs"].(bool); ok {
			pp.Thumbs = v
		}
	})
	jsonOut(w, p, nil)
}

func (e *Engine) handleConfig(w http.ResponseWriter, r *http.Request) {
	var m struct {
		Codec string `json:"codec"`
		W     int    `json:"w"`
		H     int    `json:"h"`
		IR    *bool  `json:"ir"`
	}
	if err := json.NewDecoder(r.Body).Decode(&m); err != nil {
		jsonOut(w, nil, err)
		return
	}
	_, _, cfg := e.Stats()
	ir := cfg.IR
	if m.IR != nil {
		ir = *m.IR
	}
	codec := m.Codec
	if codec == "" {
		codec = cfg.Codec
	}
	w2, h2 := m.W, m.H
	if w2 == 0 || h2 == 0 {
		w2, h2 = cfg.W, cfg.H
	}
	newCfg, note, err := e.SetConfig(codec, w2, h2, ir)
	if err != nil {
		jsonOut(w, nil, err)
		return
	}
	jsonOut(w, map[string]any{"config": newCfg, "note": note}, nil)
}

func (e *Engine) handleAlign(w http.ResponseWriter, r *http.Request) {
	res, err := e.Align()
	if err != nil {
		jsonOut(w, nil, err)
		return
	}
	jsonOut(w, res, nil)
}

// 曝光：GET 读 / POST 设（这是"根治画面发暗"的那把钥匙，现在原生可用了）
func (e *Engine) handleExposure(w http.ResponseWriter, r *http.Request) {
	rgbS, _, _, _ := e.Snapshot()
	if rgbS == nil {
		jsonOut(w, nil, fmt.Errorf("RGB 相机不在线"))
		return
	}
	if r.Method == http.MethodGet {
		st, err := rgbS.Exposure()
		if err != nil {
			jsonOut(w, nil, err)
			return
		}
		jsonOut(w, st, nil)
		return
	}
	var m struct {
		Value int32 `json:"value"`
		Auto  bool  `json:"auto"`
	}
	if err := json.NewDecoder(r.Body).Decode(&m); err != nil {
		jsonOut(w, nil, err)
		return
	}
	if err := rgbS.SetExposure(m.Value, m.Auto); err != nil {
		jsonOut(w, nil, err)
		return
	}
	time.Sleep(120 * time.Millisecond)
	st, _ := rgbS.Exposure()
	jsonOut(w, st, nil)
}

func (e *Engine) handleDark(w http.ResponseWriter, r *http.Request) {
	var m struct {
		Target string `json:"target"`
		Frames int    `json:"frames"`
	}
	_ = json.NewDecoder(r.Body).Decode(&m)
	if m.Target == "" {
		m.Target = "both"
	}
	if m.Frames == 0 {
		m.Frames = 30
	}
	out, err := e.CalibrateDark(m.Target, m.Frames)
	jsonOut(w, out, err)
}

func (e *Engine) handleDarkClear(w http.ResponseWriter, r *http.Request) {
	e.ClearDark()
	jsonOut(w, map[string]any{"cleared": true}, nil)
}

func (e *Engine) handleWBAutoTune(w http.ResponseWriter, r *http.Request) {
	res, err := e.AutoTuneWhiteBalance()
	jsonOut(w, res, err)
}

func (e *Engine) handleWB(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodGet {
		jsonOut(w, e.WhiteBalanceStatus(), nil)
		return
	}
	var m struct {
		Value *float64 `json:"value"`
		Auto  *bool    `json:"auto"`
	}
	_ = json.NewDecoder(r.Body).Decode(&m)
	val := int32(-1)
	if m.Value != nil {
		val = int32(*m.Value)
	}
	auto := false
	if m.Auto != nil {
		auto = *m.Auto
	}
	if val < 0 && m.Auto == nil {
		jsonOut(w, nil, fmt.Errorf("要带 value 或 auto"))
		return
	}
	if err := e.SetWhiteBalance(val, auto); err != nil {
		jsonOut(w, nil, err)
		return
	}
	time.Sleep(300 * time.Millisecond) // 等相机生效
	jsonOut(w, e.WhiteBalanceStatus(), nil)
}

func (e *Engine) handleDumpRaw(w http.ResponseWriter, r *http.Request) {
	rgbS, _, _, _ := e.Snapshot()
	if rgbS == nil {
		jsonOut(w, nil, fmt.Errorf("RGB 相机不在线"))
		return
	}
	data, sub, cw, chh := rgbS.RawPayload()
	if len(data) == 0 {
		jsonOut(w, nil, fmt.Errorf("还没有帧"))
		return
	}
	_ = os.MkdirAll("captures", 0o755)
	ts := time.Now().Format("20060102_150405")
	bin := filepath.Join("captures", "raw_"+ts+".bin")
	_ = os.WriteFile(bin, data, 0o644)
	meta := map[string]any{"subtype": sub, "w": cw, "h": chh, "bytes": len(data), "file": bin}
	jsonOut(w, meta, nil)
}

func (e *Engine) handleSave(w http.ResponseWriter, r *http.Request) {
	jsonOut(w, e.Save(), nil)
}

// handleDiagDual —— 诊断：强制允许"未压缩码流 + IR 同开"，实测到底能不能跑。
// 平时的硬规则（未压缩只能开一路）在这条路径上被临时绕过，测完自动恢复 MJPG。
// 结论要看数据：如果 RGB 掉到个位数 fps，就是 USB 总线带宽到顶了，跟 CPU/语言无关。
func (e *Engine) handleDiagDual(w http.ResponseWriter, r *http.Request) {
	var m struct {
		Codec  string `json:"codec"`
		Res    string `json:"res"`
		Second int    `json:"seconds"`
	}
	_ = json.NewDecoder(r.Body).Decode(&m)
	if m.Codec == "" {
		m.Codec = "yuy2"
	}
	if m.Second == 0 {
		m.Second = 8
	}
	w2, h2 := 640, 480
	if m.Res == "1280x720" {
		w2, h2 = 1280, 720
	}
	if m.Res == "1920x1080" {
		w2, h2 = 1920, 1080
	}

	forceBandwidth = true
	cfg, _, err := e.SetConfig(m.Codec, w2, h2, true)
	forceBandwidth = false
	if err != nil {
		jsonOut(w, nil, err)
		return
	}
	time.Sleep(time.Duration(m.Second) * time.Second)

	_, irS, _, _ := e.Snapshot()
	st, _, _ := e.Stats()
	res := map[string]any{
		"codec": cfg.Codec, "size": fmt.Sprintf("%dx%d", cfg.W, cfg.H), "ir": cfg.IR,
		"rgb_fps": round2(st.RGBFPS), "ir_fps": round2(st.IRFPS),
		"rgb_subtype": st.Subtype, "ir_subtype": st.IRSubtype,
		"rgb_error": st.RGBErr,
	}
	if irS != nil {
		res["ir_online"] = true
	}
	if st.RGBFPS < 5 {
		res["verdict"] = "跑不动：RGB 掉到 " + strconv.FormatFloat(st.RGBFPS, 'f', 1, 64) +
			" fps —— 这是 USB 总线带宽上限（不是 CPU/语言问题），必须二选一"
	} else {
		res["verdict"] = "跑得动：" + strconv.FormatFloat(st.RGBFPS, 'f', 1, 64) + " fps RGB + " +
			strconv.FormatFloat(st.IRFPS, 'f', 1, 64) + " fps IR"
	}
	// 恢复 MJPG + IR
	_, _, _ = e.SetConfig("mjpg", cfg.W, cfg.H, true)
	jsonOut(w, res, nil)
}

// ───────────────────────── 水吸收测波段 ─────────────────────────

var waveMu sync.Mutex

func (e *Engine) handleWavelength(w http.ResponseWriter, r *http.Request) {
	var m struct {
		Step string `json:"step"`
	}
	_ = json.NewDecoder(r.Body).Decode(&m)
	waveMu.Lock()
	defer waveMu.Unlock()

	path := filepath.Join("calib", "wavelength.json")
	state := map[string]map[string]float64{}
	if b, err := os.ReadFile(path); err == nil {
		_ = json.Unmarshal(b, &state)
	}
	if m.Step == "reset" {
		_ = os.Remove(path)
		jsonOut(w, map[string]any{"reset": true}, nil)
		return
	}
	if m.Step == "status" {
		jsonOut(w, verdict(state), nil)
		return
	}
	if m.Step != "empty" && m.Step != "filled" {
		jsonOut(w, nil, fmt.Errorf("step 需为 empty / filled / status / reset"))
		return
	}
	_, irS, _, _ := e.Snapshot()
	if irS == nil {
		jsonOut(w, nil, fmt.Errorf("IR 相机不在线（未压缩码流下会被自动关闭）"))
		return
	}
	var roiVals, allVals []float64
	lastSeq := int64(-1)
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	for len(roiVals) < 12 {
		seq, ok := irS.WaitFrame(ctx, lastSeq)
		if !ok {
			break
		}
		lastSeq = seq
		_, gray, grayDk, _, w, h, _ := irS.Snapshot()
		if gray == nil || grayDk == nil || len(gray) != len(grayDk) {
			continue
		}
		// 用**未归一化**的 |亮-灭|，保证多次测量之间可比
		var sumAll, sumROI float64
		n := 0
		for y := 0; y < h; y++ {
			for x := 0; x < w; x++ {
				d := int(gray[y*w+x]) - int(grayDk[y*w+x])
				if d < 0 {
					d = -d
				}
				sumAll += float64(d)
				if x >= w*4/10 && x < w*6/10 && y >= h*4/10 && y < h*6/10 {
					sumROI += float64(d)
					n++
				}
			}
		}
		if n > 0 {
			roiVals = append(roiVals, sumROI/float64(n))
			allVals = append(allVals, sumAll/float64(w*h))
		}
	}
	if len(roiVals) == 0 {
		jsonOut(w, nil, fmt.Errorf("拿不到 IR 帧"))
		return
	}
	state[m.Step] = map[string]float64{"roi_mean": round2(avg(roiVals)), "frame_mean": round2(avg(allVals))}
	_ = os.MkdirAll("calib", 0o755)
	b, _ := json.MarshalIndent(state, "", "  ")
	_ = os.WriteFile(path, b, 0o644)
	res := map[string]any{"step": m.Step, "roi_mean": state[m.Step]["roi_mean"],
		"frame_mean": state[m.Step]["frame_mean"], "samples": len(roiVals)}
	for k, v := range verdict(state) {
		res[k] = v
	}
	jsonOut(w, res, nil)
}

func avg(v []float64) float64 {
	if len(v) == 0 {
		return 0
	}
	var s float64
	for _, x := range v {
		s += x
	}
	return s / float64(len(v))
}

func verdict(state map[string]map[string]float64) map[string]any {
	em, ok1 := state["empty"]
	fm, ok2 := state["filled"]
	if !ok1 || !ok2 {
		have := []string{}
		for k := range state {
			have = append(have, k)
		}
		return map[string]any{"ready": false, "have": have,
			"hint": "需要先测 empty（空容器）再测 filled（装满水）"}
	}
	ratio := fm["roi_mean"] / maxF(1e-6, em["roi_mean"])
	drift := fm["frame_mean"] / maxF(1e-6, em["frame_mean"])
	reliable := drift >= 0.7 && drift <= 1.4
	var v, why string
	switch {
	case ratio >= 0.45:
		v, why = "850nm 可能性大", "水对 850nm 吸收弱，光能穿过去"
	case ratio <= 0.18:
		v, why = "940nm 可能性大", "水对 940nm 吸收强，光基本被吃掉"
	default:
		v, why = "不确定（过渡区）", "建议换更粗的容器（更长水层）重测"
	}
	hint := ""
	if !reliable {
		hint = "两次测量间整体亮度变化 >30%（自动曝光在漂），建议重测"
	}
	return map[string]any{"ready": true, "ratio_filled_over_empty": round3(ratio),
		"brightness_drift": round3(drift), "reliable": reliable,
		"verdict": v, "reason": why, "hint": hint}
}

func round3(v float64) float64 { return float64(int(v*1000+0.5)) / 1000 }
func maxF(a, b float64) float64 {
	if a > b {
		return a
	}
	return b
}

// ───────────────────────── 前端 ─────────────────────────

const page = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>RGB + IR 摄像头控制台 (原生 Go)</title><style>
:root{--bg:#12141a;--pane:#1c1f27;--fg:#e7e9ee;--dim:#8b93a5;--acc:#4da3ff;--ok:#3ecf8e;--warn:#ffb454;--err:#ff6b6b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:13px/1.5 "Segoe UI",system-ui,sans-serif}
header{padding:9px 16px;background:var(--pane);border-bottom:1px solid #2b3040;display:flex;gap:12px;align-items:baseline}
header h1{font-size:15px;margin:0}header .sub{color:var(--dim);font-size:12px}
.wrap{display:grid;grid-template-columns:minmax(210px,250px) minmax(0,1fr) minmax(280px,330px);
  gap:12px;padding:12px;box-sizing:border-box;width:100%;align-items:start}

@media(max-width:1180px){.wrap{grid-template-columns:minmax(0,1fr)}}
@media(max-width:760px){.bb-grid{grid-template-columns:minmax(0,1fr)}}
aside.side{background:var(--pane);border-radius:8px;padding:12px}
/* 不再给边栏固定高度+内部滚动：内容长了就撑开页面，硬塞一页会把文字裁掉 */
.side.left{order:-1}
.viewer{min-width:0}
#main{width:100%;max-width:760px;background:#000;border-radius:8px;display:block}

fieldset{border:1px solid #2b3040;border-radius:6px;margin:0 0 10px;padding:9px}
legend{color:var(--acc);font-size:12px;padding:0 6px}
label{display:flex;align-items:center;gap:8px;margin:6px 0}
label span.k{flex:0 0 88px;color:var(--dim)}input[type=range]{flex:1}
output{flex:0 0 50px;text-align:right;color:var(--acc);font-variant-numeric:tabular-nums}
.modes{display:flex;flex-wrap:wrap;gap:6px}
button{background:#262b36;color:var(--fg);border:1px solid #333a4a;border-radius:6px;padding:6px 11px;cursor:pointer;font-size:12px}
button:hover{background:#2f3644}.modes button.on{background:var(--acc);border-color:var(--acc);color:#04121f;font-weight:600}
button.pri{background:var(--acc);color:#04121f;border-color:var(--acc);font-weight:600}
.row{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
select{background:#262b36;color:var(--fg);border:1px solid #333a4a;border-radius:5px;padding:4px}
#status{font-family:ui-monospace,Consolas,monospace;font-size:11.5px;color:var(--dim);padding:8px;background:#0e1015;border-radius:6px;white-space:pre-wrap;word-break:break-all}
#msg{margin-top:8px;font-size:12px;min-height:17px}.ok{color:var(--ok)}.warn{color:var(--warn)}.err{color:var(--err)}
#verdict{font-size:14px;font-weight:600;margin-top:6px}.small{font-size:11px;color:var(--dim)}

  aside{display:flex;flex-direction:column;gap:8px;overflow-y:auto;padding-right:4px}
  .hintbar{font-size:11px;color:#7a8494;line-height:1.6;padding:2px 0 4px}
  /* 画面居中：主图与拼图在可视区里水平居中，别贴着左边 */
  #viewer,#main{display:block;margin:0 auto}
  .viewer{display:flex;flex-direction:column;align-items:center;justify-content:flex-start;padding:10px 0}
  .viewer img{max-width:100%;height:auto;border-radius:8px;background:#0b0d11}
  section.sec{background:#161a21;border:1px solid #232833;border-radius:8px;overflow:hidden}
  .sec-h{cursor:pointer;list-style:none;padding:9px 11px;font-size:13px;font-weight:600;
    color:#cfd6e2;background:#1b202a;border-bottom:1px solid #232833;display:flex;
    align-items:center;justify-content:space-between}
  .sec-h::-webkit-details-marker{display:none}
  .sec-h::after{content:"▸";color:#5b6577;font-size:11px}
  section.sec[open]>summary::after{content:"▾"}
  .sec-h:hover{background:#202633}
  section.sec>div,section.sec>label,section.sec>button,section.sec>.row2,section.sec>.row3{padding:0 11px}
  section.sec>*:not(summary):first-of-type{margin-top:10px}
  section.sec>*:last-child{margin-bottom:11px}
  .tag{font-size:10px;font-weight:400;color:#6d7789;background:#101419;border:1px solid #232833;
    border-radius:4px;padding:1px 5px;margin-left:6px}
  label.sl{display:grid;grid-template-columns:66px 1fr 42px;align-items:center;gap:8px;
    font-size:12px;margin:7px 0;color:#98a2b3}
  label.sl .k{color:#8b95a7}
  label.sl select{width:100%}
  label.sl output{text-align:right;font-variant-numeric:tabular-nums;color:#cfd6e2}
  label.sl input[type=range]{width:100%}
  .row2{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin:7px 0}
  .row3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:4px;margin:7px 0}
  .row2 button,.row3 button{width:100%}
  label.chk{display:flex;align-items:center;gap:6px;font-size:12px;color:#98a2b3;margin:6px 0}
  label.chk input{accent-color:#4a8cff}
  .modes{display:grid;grid-template-columns:repeat(3,1fr);gap:5px;margin:9px 0 6px}
  .small{font-size:11px;line-height:1.65;color:#79839a;margin:6px 0}
  .small b{color:#a8b3c5}
  section.sec button{margin:6px 0}

  /* ── 整齐化：统一"标签 | 控件 | 数值"三列网格 + 段落节奏 ────────── */
  /* 标签列给够宽度（原来 66px 会把"IR 权重""色度平滑"截断），超长才省略号 */
  label.sl{display:grid;grid-template-columns:78px minmax(0,1fr) 48px;align-items:center;
    gap:8px;font-size:12px;margin:6px 0;color:#98a2b3;line-height:1.5}
  label.sl .k{color:#8b95a7;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  label.sl output{text-align:right;font-variant-numeric:tabular-nums;color:#cfd6e2;font-size:11.5px}
  label.sl input[type=range]{width:100%;margin:0}
  label.sl select{width:100%;min-width:0}
  /* 段落内部节奏统一：所有分组内容左右内边距一致、上下留白一致 */
  section.sec>.small{padding:0}
  section.sec>*:not(.sec-h){margin-left:11px;margin-right:11px}
  section.sec>.row2,section.sec>.row3{margin-top:7px;margin-bottom:7px}
  section.sec>button{margin-top:7px;margin-bottom:7px}
  section.sec>.small:first-of-type{margin-top:8px}
  section.sec>.small:last-child{margin-bottom:11px}
  /* 视图按钮：等高、字号一致，别再忽大忽小 */
  .modes{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:5px;margin:8px 0 4px}
  .modes button{padding:7px 2px;font-size:12px;line-height:1.2;white-space:nowrap;
    overflow:hidden;text-overflow:ellipsis}
  /* 按钮统一高度 */
  .row2 button,.row3 button,section.sec>button{min-height:30px;font-size:12px}
  /* 勾选项行高统一（图标 + 文字对齐） */
  label.chk{display:flex;align-items:center;gap:6px;font-size:12px;color:#98a2b3;
    margin:6px 0;line-height:1.5}
  label.chk input{margin:0;accent-color:#4a8cff;flex:0 0 auto}
  /* 分组标题：左侧竖线 + 常开（不再折叠） */
  .sec-h{margin:0;padding:9px 11px 8px;font-size:13px;font-weight:600;color:#cfd6e2;
    background:#1b202a;border-bottom:1px solid #232833;border-left:2px solid #3a4a66;
    display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:1}
.bottombar{width:100%;margin-top:12px;background:var(--pane);border-radius:8px;padding:10px 12px}
  .statusline{font-size:12px;color:#c8d2e0;line-height:1.95;word-break:break-word}
  .statusline b{color:#fff}
  .statusline .warn{color:#ffb454}
  .bb-grid{display:grid;grid-template-columns:minmax(240px,1fr) minmax(0,1.1fr);gap:12px;margin-top:8px}
  .bb-box{background:#12151b;border:1px solid #232833;border-radius:6px;padding:8px 10px;min-width:0}
  .bb-h{font-size:12px;font-weight:600;color:#9fb0c8;margin-bottom:6px}
  .evlist{height:132px;overflow-y:auto;font-size:11.5px;line-height:1.85;color:#93a0b4;
    font-variant-numeric:tabular-nums}
  .evlist div{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .evlist .t{color:#5f6b7e;margin-right:6px}
  .evlist .k-person{color:#7fd17f}.evlist .k-gesture{color:#d9a7ff}
  .evlist .k-face{color:#7fb8ff}.evlist .k-calib{color:#ffd479}.evlist .k-config{color:#8b95a7}
  #spark{width:100%;height:120px;display:block}
  .sparklegend{font-size:10.5px;color:#6d7789;margin-top:4px}
  .sparklegend .c1{color:#4a8cff}.sparklegend .c2{color:#ffb454}.sparklegend .c3{color:#7fd17f}
</style></head><body>
<header><h1>RGB + 红外(IR) 摄像头控制台</h1><span class="sub">原生 Go 后端 · Media Foundation 采集 · 单流拼图 · 暗场校准 · 波段判别</span></header>
<div class="wrap">
 <aside class="side left">
  <div class="hintbarL">原生 Go 后端 · Media Foundation 采集 · 单流拼图 · 暗场校准 · 波段判别</div>

  <section class="sec"><h3 class="sec-h">视图</h3>
    <div class="modes" id="modes"></div>
    <label class="chk"><input type="checkbox" id="thumbs" onchange="api('set',{thumbs:this.checked}).then(refresh)"> 缩略图拼进同一张图（单流）</label>
  </section>

  <section class="sec"><h3 class="sec-h">曝光 <span class="tag">OpenCV 做不到的那件事</span></h3>
    <div class="row2">
      <button id="exp-auto" onclick="setExp(0,true)">自动</button>
      <button onclick="setExp(0,false)">手动</button>
    </div>
    <label class="sl"><span class="k">曝光值</span><input type="range" id="expval" min="-10" max="0" step="1"><output id="o-expval"></output></label>
    <div id="expinfo" class="small">读取中…</div>
  </section>

  <section class="sec"><h3 class="sec-h">采集 <span class="tag" id="bwtag"></span></h3>
    <label class="sl"><span class="k">码流</span>
      <select id="codec" onchange="applyCfg()">
        <option value="mjpg">MJPG · 有损但最省（可 1080p）</option>
        <option value="yuy2">YUY2 · 4:2:2 未压缩（画质最好；选高分辨率会自动夹到 480p）</option>
        <option value="nv12">NV12 · 4:2:0 未压缩（可 1080p）</option>
      </select></label>
    <label class="sl"><span class="k">分辨率</span>
      <select id="res" onchange="applyCfg()">
        <option value="640x480">640×480</option>
        <option value="1280x720">1280×720</option>
        <option value="1920x1080">1920×1080</option>
      </select></label>
    <label class="chk"><input type="checkbox" id="ir" onchange="applyCfg()"> 启用 IR 通道（Windows Hello 相机）</label>
    <div class="small">USB 预算约 21 MB/s：<b>NV12 480p 可带 IR</b>（实测 14.9+29.8fps）、
      YUY2 不行（RGB 掉到 0）、720p 未压缩必超。超预算会自动只开一路并说明。</div>
  </section>

  </aside>
 <div class="viewer">
   <img id="main" src="/mjpg" alt="live">
   <div class="bottombar">
     <div class="statusline" id="statusline">—</div>
     <div class="bb-grid">
       <div class="bb-box">
         <div class="bb-h">事件 <span class="small" style="display:inline">最近 100 条</span></div>
         <div class="evlist" id="evlist"></div>
       </div>
       <div class="bb-box">
         <div class="bb-h">趋势 <span class="small" style="display:inline">最近 5 分钟</span></div>
         <canvas id="spark" width="440" height="120"></canvas>
         <div class="sparklegend"><b class="c1">■</b> 流 fps　<b class="c2">■</b> 亮度　<b class="c3">■</b> 人物覆盖度</div>
       </div>
     </div>
   </div>
 </div>
 <aside class="side right"><section class="sec"><h3 class="sec-h">相机白平衡 <span class="tag" id="wbtag">直控相机</span></h3>
    <label class="sl"><span class="k">色温</span><input type="range" id="camwb" min="2800" max="6500" step="10" oninput="wbApply()"><output id="o-camwb"></output></label>
    <div class="row2">
      <label class="chk"><input type="checkbox" id="wbauto" onchange="wbApply()"> 自动</label>
      <button onclick="wbTune()">自动校准</button>
    </div>
    <div class="small" id="wbmsg">拖动色温即时生效（约 0.2s 防抖）。值越低画面越蓝、越高越黄。相机做粗调、软件白平衡做细调，叠加使用。
      校准会扫两轮（粗 6 档 + 精 7 档，每档等 3 帧新画面），约 6~10 秒。</div>
  </section>
<section class="sec"><h3 class="sec-h">融合</h3>
    <label class="sl"><span class="k">IR 权重</span><input type="range" id="weight" min="0" max="1" step="0.02"><output id="o-weight"></output></label>
    <label class="sl"><span class="k">IR 增益</span><input type="range" id="gain" min="0.5" max="3" step="0.05"><output id="o-gain"></output></label>
    <label class="sl"><span class="k">色彩增强</span><input type="range" id="chroma" min="1" max="4" step="0.05"><output id="o-chroma"></output></label>
    <label class="sl"><span class="k">色度平滑</span><input type="range" id="chroma_blur" min="0" max="12" step="1"><output id="o-chroma_blur"></output></label>
    <label class="sl"><span class="k">暗部去彩</span><input type="range" id="chroma_dark" min="0" max="1" step="0.05"><output id="o-chroma_dark"></output></label>
    <div class="small">暗部去彩：暗处 RGB 色度信噪比极差，放大后会变成一块块紫/绿。
      这个值把<b>越暗的区域越往灰色收缩</b>（亮部保留颜色）。<b>0.6 是默认</b>；
      若发现"某块发光但发紫"（典型是强反红外的物体在 RGB 里很暗），加大它。</div>
    <label class="sl"><span class="k">IR 色阶对齐</span><input type="range" id="stretch" min="0" max="1" step="0.05"><output id="o-stretch"></output></label>
    <label class="sl"><span class="k">软件白平衡</span><input type="range" id="white_balance" min="0" max="1" step="0.05"><output id="o-white_balance"></output></label>
    <label class="sl"><span class="k">Gamma</span><input type="range" id="gamma" min="0.4" max="2.5" step="0.05"><output id="o-gamma"></output></label>
    <div class="small">Gamma：<b>1.0 = 不变</b>；&gt;1 提亮暗部（暗光下看清楚），&lt;1 压暗。融合后作用在亮度通道上。</div>
    <div class="small">色彩增强 1.25 ≈ 接近原始观感（1.6 是旧 OpenCV 版的味道）。<br>
      软件白平衡：0=保留现场色偏（不放大）· 1=完全中性化。实测场景色偏 <b id="cast">—</b></div>
  </section>

  
  <section class="sec"><h3 class="sec-h">人脸 <span class="tag">原生 Viola-Jones</span></h3>
    <div class="row3">
      <label class="chk"><input type="checkbox" id="fd" onchange="api('set',{face_detect:this.checked})"> 检测</label>
      <label class="chk"><input type="checkbox" id="fwb" onchange="api('set',{face_wb:this.checked})"> 人脸白平衡</label>
      <label class="chk"><input type="checkbox" id="fbx" onchange="api('set',{face_box:this.checked})"> 画框</label>
    </div>
    <div class="small">肤色比整幅场景更接近中性参考，所以人脸区域测色偏比灰世界准。
      后台 1/2 分辨率 ~5fps（19ms/次），不占渲染帧率。</div>

    <div class="hr"></div>
    <div class="row3">
      <label class="chk"><input type="checkbox" id="pd" onchange="api('set',{person_detect:this.checked})"> 人物检测</label>
      <label class="chk"><input type="checkbox" id="pbx" onchange="api('set',{person_box:this.checked})"> 画人物框</label>
      <span></span>
    </div>
    <div class="small">无模型的轻量检测：<b>IR 差分</b>（皮肤/近物反射红外强）+ <b>运动检测</b>，
      8×6 网格输出"人在不在、在哪一块"。静止的人靠 IR 差分、动的人靠运动，互补。
      结果在状态栏与 <code>/api/state → person</code>。</div>
    <div class="small" id="personinfo">—</div>

    <div class="hr"></div>
    <div class="row3">
      <label class="chk"><input type="checkbox" id="yf" onchange="api('set',{yolo_face:this.checked})"> YOLO 人脸</label>
      <label class="chk"><input type="checkbox" id="yp" onchange="api('set',{yolo_pose:this.checked})"> YOLO 骨架</label>
      <label class="chk"><input type="checkbox" id="yh" onchange="api('set',{yolo_hand:this.checked})"> 手部</label>
    </div>
    <div class="small" id="yolomsg">旁路状态：未知</div>
  </section>

  <section class="sec"><h3 class="sec-h">配准对齐</h3>
    <label class="sl"><span class="k">scale</span><input type="range" id="scale" min="0.8" max="1.8" step="0.005"><output id="o-scale"></output></label>
    <label class="sl"><span class="k">tx</span><input type="range" id="tx" min="-200" max="300" step="1"><output id="o-tx"></output></label>
    <label class="sl"><span class="k">ty</span><input type="range" id="ty" min="-200" max="300" step="1"><output id="o-ty"></output></label>
    <label class="sl"><span class="k">rot</span><input type="range" id="rot" min="-10" max="10" step="0.1"><output id="o-rot"></output></label>
    <button onclick="api('align',{}).then(d=>d&&msg('标定完成 scale='+d.scale.toFixed(3)+' tx='+d.tx+' ty='+d.ty,'ok'))">自动标定</button>
  </section>

  <section class="sec"><h3 class="sec-h">暗场 / 波段</h3>
    <div class="row3">
      <button onclick="darkCal()">暗场校准</button>
      <button onclick="darkClear()">清数据</button>
      <label class="chk"><input type="checkbox" id="darkon" onchange="api('dark/enable',{on:this.checked}).then(refresh)"> 启用校正</label>
    </div>
    <div class="small">校完自动生效并保存，下次启动自动加载。「启用校正」可临时关掉补偿（数据保留），
      灯光/曝光变了导致黑位发灰发彩时，先关掉试试。</div>
    <div class="row2" style="margin-top:6px">
      <button onclick="wave('empty')">① 空容器</button>
      <button onclick="wave('filled')">② 装满水</button>
    </div>
    <div id="verdict" class="small">用水的吸收比判别 850 / 940nm（需要能盖住镜头的容器）</div>
  </section>

  <section class="sec"><h3 class="sec-h">其他</h3>
    <div class="row3">
      <label class="chk"><input type="checkbox" id="mh" onchange="api('set',{mirror_h:this.checked})"> 水平镜像</label>
      <label class="chk"><input type="checkbox" id="mv" onchange="api('set',{mirror_v:this.checked})"> 垂直镜像</label>
    </div>
    <div class="row2">
      <button onclick="api('snapshot',{}).then(d=>d&&msg('已存图: '+d.view,'ok'))">存图</button>
      <button onclick="location.reload()">刷新页面</button>
    </div>
    <label class="sl"><span class="k">渲染帧率</span><input type="range" id="fps" min="5" max="60" step="1"><output id="o-fps"></output></label>
    <label class="sl"><span class="k">JPEG 质量</span><input type="range" id="quality" min="50" max="95" step="1"><output id="o-quality"></output></label>
  </section>

  <div id="msg" class="small"></div>
</aside></div>
<script>
const $=i=>document.getElementById(i); let mode="fuse", dragging=null, cfg={};
async function api(p,b){try{const r=await fetch("/api/"+p,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b||{})});
 const j=await r.json(); if(!j.ok){msg(j.error&&j.error.message||"失败","err");return null;} return j.data;}
 catch(e){msg("请求失败 "+e,"err");return null;}}
function msg(t,c){const m=$("msg");m.className=c||"";m.textContent=t;}
async function init(){const j=await(await fetch("/api/state")).json(); if(!j.ok)return;
 (j.data.views||[]).forEach(v=>{const b=document.createElement("button");b.textContent=v.t;b.dataset.v=v.v;
  b.onclick=()=>setMode(v.v);$("modes").appendChild(b);});
 setMode("fuse"); cfg=j.data.config||{};
 $("res").value=cfg.w+"x"+cfg.h; $("codec").value=cfg.codec; $("ir").checked=!!cfg.ir;
 applyState(j.data); readExp();}
function setMode(v){mode=v;[...$("modes").children].forEach(b=>b.classList.toggle("on",b.dataset.v===v));
 api("set",{mode:v}).then(()=>{});}
const SL={weight:"o-weight",gain:"o-gain",chroma:"o-chroma",chroma_blur:"o-chroma_blur",white_balance:"o-white_balance",gamma:"o-gamma",chroma_dark:"o-chroma_dark",stretch:"o-stretch",fps:"o-fps",
          scale:"o-scale",tx:"o-tx",ty:"o-ty",rot:"o-rot"};
for(const[k,o]of Object.entries(SL)){const el=$(k);
 el.addEventListener("input",()=>{$(o).textContent=(+el.value).toFixed(k==="weight"||k==="stretch"?2:(k==="scale"?3:1));dragging=k;});
 el.addEventListener("change",()=>{api("set",{[k]:+el.value});dragging=null;});}
$("thumbs").onchange=e=>api("set",{thumbs:e.target.checked});
$("expval").addEventListener("change",()=>setExp(+$("expval").value,false));
async function readExp(){const r=await fetch("/api/exposure");const j=await r.json();
 if(!j.ok){$("expinfo").textContent="曝光接口不可用（IR 相机不支持）";return;}
 const d=j.data; $("expval").min=d.min; $("expval").max=d.max||0;
 if(document.activeElement!==$("expval")){$("expval").value=d.value;$("o-expval").textContent=d.value;}
 $("expinfo").innerHTML=(d.supported?("范围 "+d.min+" ~ "+d.max+"（步长 "+d.step+"，默认 "+d.default+"），当前 <b>"+d.value+"</b>，模式 "+(d.flags===1?"Auto":"Manual")):"相机不支持曝光控制")+"<br>OpenCV 只会用 Manual 标志，这就是画面发暗的根因。";}
function setExp(v,auto){api("exposure",{value:v,auto:auto}).then(d=>{if(d){msg(auto?"已切 Auto 曝光":"已设手动曝光 "+v,"ok");readExp();}});}
function applyCfg(){const[W,H]=$("res").value.split("x").map(Number);
 api("config",{codec:$("codec").value,w:W,h:H,ir:$("ir").checked}).then(d=>{
  if(d){if(d.note)msg(d.note,"warn");else msg("已切换："+JSON.stringify(d.config),"ok");
   cfg=d.config;$("ir").checked=!!d.config.ir;setTimeout(refresh,1200);}});}
function align(){msg("标定中（Go 原生梯度搜索）…");api("align",{}).then(d=>{if(d)msg("标定完成 scale="+d.scale+" tx="+d.tx+" ty="+d.ty+" 相关度="+d.score.toFixed(3),"ok");});}
function resetAlign(){api("set",{scale:1.30,tx:98,ty:18,rot:0}).then(refresh);}
function snap(){api("snapshot",{}).then(d=>{if(d)msg("已保存: "+Object.values(d).map(p=>p.split(/[\\/]/).pop()).join(", "),"ok");});}
function darkCal(){
 if(!confirm("请先完全遮住镜头（RGB 和 IR 窗口都要遮住，别漏光），点确定开始采集暗场。\n\n采集 30 帧取平均，得到的是：固定图案噪声(FPN) + 读出偏置 + 热噪点。\n校完会存到 calib/dark_*.gob，下次启动自动加载启用。"))return;
 const t0=Date.now();
 msg("正在采集暗场…（别动，约 2 秒）");
 api("dark",{target:"both",frames:30}).then(d=>{
   if(!d)return;
   const secs=((Date.now()-t0)/1000).toFixed(1);
   const rgb=d.rgb||{}, ir=d.ir||{};
   const parts=[];
   if(rgb.saved||rgb.mean!==undefined)parts.push("RGB: 均值 "+(rgb.mean!==undefined?rgb.mean.toFixed(1):"?")+"，已存 "+rgb.saved);
   if(ir.saved||ir.mean!==undefined)parts.push("IR: 均值 "+(ir.mean!==undefined?ir.mean.toFixed(1):"?")+"，已存 "+ir.saved);
   const bar=document.createElement("div");
   bar.className="ok";
   bar.style.cssText="margin:6px 0;padding:8px 10px;border-radius:6px;background:#17381f;border:1px solid #2e7d32;line-height:1.6";
   bar.innerHTML="<b>✅ 暗场校准完成</b>（用时 "+secs+"s）<br>"+parts.join("<br>")+
     "<br>状态：<b>已保存并即刻生效</b>；下次启动自动加载（状态栏显示 暗场 RGB✓/IR✓）。"+
     "<br>想撤销点「清暗场」。";
   const host=document.getElementById("msg");
   if(host){host.innerHTML="";host.appendChild(bar);}
   else alert("暗场校准完成："+parts.join(" / "));
   load();
 });
}
function darkClear(){
 if(!confirm("清除已保存的暗场数据？清除后当前与下次启动都不再补偿。"))return;
 api("dark/clear",{}).then(d=>{if(d){msg("已清除暗场（当前已停用）","ok");load();}});
}
function darkClear(){api("dark/clear",{}).then(d=>{if(d)msg("已清除暗场","ok");});}
let _wbT=null;
function wbApply(){const v=+$("camwb").value;const auto=$("wbauto").checked;
 $("o-camwb").value=auto?"自动":v+"K";
 if(auto){doWB(v,true);return;}
 clearTimeout(_wbT);_wbT=setTimeout(()=>doWB(v,false),220);}
function doWB(v,auto){
 api("wb",auto?{auto:true}:{value:v,auto:false}).then(d=>{if(d){$("wbmsg").innerHTML=
  "已设置："+(d.auto?"自动":d.value+"K")+"（范围 "+d.min+"~"+d.max+"K）";}});}
function wbTune(){$("wbmsg").textContent="扫描中，请保持画面稳定…";
 api("wb/auto-tune",{}).then(d=>{if(!d)return;
  let t="<b>校准完成，已应用 "+d.applied+"K</b>（评分 "+d.applied_score+"，越小越中性）<br>";
  t+="<table class='small' style='width:100%'><tr><th>色温</th><th>色偏a</th><th>色偏b</th><th>评分</th></tr>";
  d.scan.forEach(r=>{t+="<tr"+(r.kelvin===d.applied?" style='color:#7fd17f'":"")+"><td>"+r.kelvin+"K</td><td>"+r.a+"</td><td>"+r.b+"</td><td>"+r.score+"</td></tr>";});
  t+="</table>";$("wbmsg").innerHTML=t;
  $("camwb").value=d.applied;$("wbauto").checked=false;load();});}

// ── 底部栏：状态条 / 事件流 / 趋势图 ──
let evLast = 0; const trend = {fps:[],lum:[],cov:[]}; const TRENDMAX = 150;
function pushTrend(s){
  trend.fps.push(+s.stream_fps||0);
  trend.lum.push(+s.rgb_mean||0);
  trend.cov.push(+(s.person&&s.person.coverage||0)*100);
  for(const k in trend) if(trend[k].length>TRENDMAX) trend[k].shift();
  drawSpark();
}
function drawSpark(){
  const c=$("spark"); if(!c) return; const g=c.getContext("2d");
  const W=c.width,H=c.height; g.clearRect(0,0,W,H);
  g.strokeStyle="#1d222c"; g.lineWidth=1;
  for(let i=1;i<4;i++){const y=H*i/4; g.beginPath(); g.moveTo(0,y); g.lineTo(W,y); g.stroke();}
  const draw=(arr,mx,col)=>{
    if(arr.length<2) return;
    g.strokeStyle=col; g.lineWidth=1.6; g.beginPath();
    for(let i=0;i<arr.length;i++){
      const x=W*i/(TRENDMAX-1), y=H-2-(Math.min(arr[i],mx)/mx)*(H-6);
      i?g.lineTo(x,y):g.moveTo(x,y);
    } g.stroke();
  };
  const mx1=Math.max(35,...trend.fps), mx2=Math.max(180,...trend.lum), mx3=Math.max(60,...trend.cov);
  draw(trend.fps,mx1,"#4a8cff"); draw(trend.lum,mx2,"#ffb454"); draw(trend.cov,mx3,"#7fd17f");
  g.fillStyle="#5f6b7e"; g.font="10px monospace";
  g.fillText("fps≤"+mx1.toFixed(0),2,10); g.fillText("亮度≤"+mx2.toFixed(0),2,22);
}
function renderStatus(s){
  const p=s.person||{}, g=s.gestures||"";
  const led = (s.led_on&&s.led_off)?(s.led_on-s.led_off):0;
  const ledw = (s.ir_fps>0 && led<12) ? ' <span class="warn">(环境光太亮，IR差分不可靠)</span>' : '';
  $("statusline").innerHTML =
    '<b>RGB</b> '+(s.rgb_fps||0).toFixed(1)+'fps('+(s.subtype||'-')+')　'+
    '<b>IR</b> '+(s.ir_fps||0).toFixed(1)+'fps　'+
    '<b>流</b> '+(s.stream_fps||0).toFixed(1)+'fps/'+(s.render_ms||0).toFixed(1)+'ms　'+
    '<b>亮度</b> '+(s.rgb_mean||0).toFixed(0)+'　'+
    '<b>LED差</b> '+led+ledw+'　'+
    '<b>人物</b> '+(p.present?('有·'+p.zone+' 覆盖 '+((p.coverage||0)*100).toFixed(0)+'%'):'无')+'　'+
    '<b>人脸</b> '+(s.face_count||0)+(s.yolo_faces?(' /YOLO '+s.yolo_faces):'')+'　'+
    '<b>手</b> '+(s.yolo_hands||0)+(g?(' ['+g.trim()+']'):'')+'　'+
    '<b>旁路</b> '+(s.vision_ok?((s.yolo_ms||0).toFixed(0)+'ms 延迟'+(s.vision_age_ms||0).toFixed(0)+'ms'):'<span class="warn">未运行</span>')+'　'+
    '<b>暗场</b> '+(s.dark_on?'开':'关')+
    (s.rgb_error?('<br><span class="warn">RGB 错误: '+s.rgb_error+'</span>'):'')+
    (s.hint?('<br><span class="warn">'+s.hint+'</span>'):'');
}
async function pollEvents(){
  try{
    const j = await (await fetch("/api/events?since="+evLast)).json();
    if(!j.ok) return;
    const list=$("evlist");
    for(const e of j.data.events){
      const d=document.createElement("div");
      d.innerHTML='<span class="t">'+e.at+'</span><span class="k-'+e.kind+'">'+e.text+'</span>';
      list.appendChild(d); evLast=e.id;
    }
    while(list.children.length>100) list.removeChild(list.firstChild);
    if(j.data.events.length) list.scrollTop=list.scrollHeight;
  }catch(err){}
}

function wave(step){msg("测量中…");api("wavelength",{step}).then(d=>{if(!d)return;
 if(step==="status"||d.ready){$("verdict").innerHTML=d.ready?
   '<span class="'+(d.reliable?"ok":"warn")+'">'+d.verdict+'</span><br><span class="small">比值 '+
   d.ratio_filled_over_empty+'（水/空）· 亮度漂移 '+d.brightness_drift+(d.reliable?"":" ⚠ 不可靠")+'</span><br><span class="small">'+
   (d.reason||"")+" "+(d.hint||"")+"</span>":'<span class="warn">数据不足</span><br><span class="small">'+(d.hint||"")+'</span>';
  msg("已取结论","ok");}
 else msg("已记录 "+step+": ROI 均值 "+d.roi_mean+"（整帧 "+d.frame_mean+"）","ok");});}
/* 前端性能：切后台就断开 MJPEG（省浏览器解码 + 省服务器带宽），回来再接上 */
document.addEventListener("visibilitychange",()=>{
 if(document.hidden){$("main").src="";}else{$("main").src="/mjpg?_="+Date.now();refresh();}});
let timer=null;
function loop(){if(timer)clearInterval(timer);timer=setInterval(()=>{if(!document.hidden)refresh();},1200);}
async function refresh(){try{const j=await(await fetch("/api/state")).json();if(!j.ok)return;
 applyState(j.data); cfg=j.data.config||{}; readExp();}catch(e){}}
function applyState(d){const p=d.params||{},s=d.stats||{};
 for(const[k,o]of Object.entries(SL)){if(dragging===k||p[k]===undefined)continue;
  if(Math.abs(+$(k).value-p[k])>1e-6){$(k).value=p[k];$(o).textContent=(+p[k]).toFixed(k==="weight"||k==="stretch"?2:(k==="scale"?3:1));}}
 const dark=(s.dark_rgb?"RGB✓":"RGB✗")+" "+(s.dark_ir?"IR✓":"IR✗");
 $("status").textContent=
  "RGB "+s.rgb_fps.toFixed(1)+"fps("+(s.subtype||"?")+") mean="+s.rgb_mean.toFixed(0)+
  " | IR "+s.ir_fps.toFixed(1)+"fps("+(s.ir_subtype||"-")+") mean="+s.ir_mean.toFixed(0)+
  (s.led_on?" | LED "+s.led_on.toFixed(0)+"/"+s.led_off.toFixed(0):"")+
  "\n渲染 "+s.stream_fps.toFixed(1)+"fps 拼图("+s.render_ms.toFixed(0)+"ms/帧) | 暗场 "+dark+
  (s.rgb_error?"\n❌ "+s.rgb_error:"")+(s.ir_error?"\n❌ "+s.ir_error:"")+
  (s.hint?"\n"+s.hint:"")+(s.face_count?"\n人脸 "+s.face_count+" 张: "+s.face_boxes:"");
 const ce=document.getElementById("cast"); if(ce) ce.textContent="a="+(s.cast_a||0).toFixed(1)+" b="+(s.cast_b||0).toFixed(1);
 const yi=document.getElementById("yolomsg"); if(yi) yi.innerHTML = s.vision_ok ? ("✅ 旁路运行中 · 人脸 "+s.yolo_faces+" · 骨架 "+s.yolo_poses+" · 手 "+s.yolo_hands+(s.gestures?(" ["+s.gestures+"]"):"")+" · "+(s.yolo_ms||0).toFixed(0)+"ms · 延迟 "+(s.vision_age_ms||0).toFixed(0)+"ms") : "⭕ 旁路未运行（启动 backend 时加 -vision，或手动跑 tools/vision_sidecar.py）";
 const pi=document.getElementById("personinfo"); renderStatus(s); pushTrend(s); pollEvents();
 const dk=document.getElementById("darkon"); if(dk) dk.checked=!!s.dark_on;
 if(s.cam_wb){const cw=document.getElementById("camwb"),wa=document.getElementById("wbauto"),ow=document.getElementById("o-camwb");
  if(cw){cw.min=s.cam_wb.min;cw.max=s.cam_wb.max;if(document.activeElement!==cw)cw.value=s.cam_wb.value;ow.value=s.cam_wb.auto?"自动":s.cam_wb.value+"K";}
  if(wa) wa.checked=!!s.cam_wb.auto;}
 if(pi&&s.person){const q=s.person; pi.innerHTML=q.present?("✅ <b>画面里有人</b> · 位置 "+q.zone+" · 覆盖度 "+(q.coverage*100).toFixed(0)+"% · 运动 "+(q.motion*100).toFixed(1)+"% · IR "+(q.ir_diff*100).toFixed(0)+"%"):"⭕ 没检测到人（运动 "+(q.motion*100).toFixed(1)+"% · IR "+(q.ir_diff*100).toFixed(0)+"%）";}}
init();loop();
</script></body></html>`

func (e *Engine) Routes() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = io.WriteString(w, page)
	})
	mux.HandleFunc("/mjpg", e.handleMJPEG)
	mux.HandleFunc("/snapshot.jpg", e.handleSnapshotJPEG)
	mux.HandleFunc("/api/state", e.handleState)
	mux.HandleFunc("/api/set", e.handleSet)
	mux.HandleFunc("/api/config", e.handleConfig)
	mux.HandleFunc("/api/align", e.handleAlign)
	mux.HandleFunc("/api/exposure", e.handleExposure)
	mux.HandleFunc("/api/dark", e.handleDark)
	mux.HandleFunc("/api/dark/clear", e.handleDarkClear)
	mux.HandleFunc("/api/snapshot", e.handleSave)
	mux.HandleFunc("/api/dump-raw", e.handleDumpRaw)
	mux.HandleFunc("/api/wb", e.handleWB)
	mux.HandleFunc("/frame.jpg", e.handleFrame)
	mux.HandleFunc("/api/vision", e.handleVision)
	mux.HandleFunc("/api/events", e.handleEvents)
	mux.HandleFunc("/api/dark/enable", e.handleDarkEnable)
	mux.HandleFunc("/api/gamma", e.handleGamma)
	mux.HandleFunc("/api/wb/auto-tune", e.handleWBAutoTune)
	mux.HandleFunc("/api/wavelength", e.handleWavelength)
	mux.HandleFunc("/api/diag-dual", e.handleDiagDual)
	return mux
}
