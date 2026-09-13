// engine.go — 会话状态 + 渲染循环 + 各视图合成（全原生，无外部进程）
package main

import (
	"context"
	"fmt"
	"image"
	"image/jpeg"
	"math"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

type Params struct {
	Mode       string  `json:"mode"`
	Weight     float64 `json:"weight"`
	Gain       float64 `json:"gain"`
	Chroma     float64 `json:"chroma"`
	ChromaBlur int     `json:"chroma_blur"` // 色度平滑半径（色度平面像素，0=不模糊）
	ChromaGain float64 `json:"chroma_gain"` // YCbCr↔LAB 饱和度补偿系数
	WhiteBalance float64 `json:"white_balance"` // 0=保持原色偏 1=完全中性化
	Gamma        float64 `json:"gamma"`         // 1.0=不变；>1 提亮暗部
	ChromaDark   float64 `json:"chroma_dark"`   // 暗部去饱和强度 0..1（治暗处发紫/发绿）
	YoloFace     bool    `json:"yolo_face"`     // 画旁路 YOLO 人脸框
	YoloPose     bool    `json:"yolo_pose"`     // 画旁路 YOLO 姿态骨架
	YoloHand     bool    `json:"yolo_hand"`     // 画旁路 MediaPipe 手部骨架
	PersonDetect bool    `json:"person_detect"` // 启用人物存在/位置检测（IR差分+运动）
	PersonBox    bool    `json:"person_box"`    // 画人物框
	FaceDetect   bool    `json:"face_detect"`   // 启用人脸检测
	FaceWB       bool    `json:"face_wb"`       // 用**人脸区域**测色偏（比全帧准）
	FaceBox      bool    `json:"face_box"`      // 画人脸框
	MirrorH   bool    `json:"mirror_h"`    // 水平镜像
	MirrorV   bool    `json:"mirror_v"`    // 垂直镜像
	Scale      float64 `json:"scale"`
	Tx         float64 `json:"tx"`
	Ty         float64 `json:"ty"`
	Rot        float64 `json:"rot"`
	Stretch    float64 `json:"stretch"`
	Quality    int     `json:"quality"`
	Thumbs  bool    `json:"thumbs"`
	FPS     float64 `json:"fps"`
}

type Config struct {
	Codec string `json:"codec"`
	W     int    `json:"w"`
	H     int    `json:"h"`
	IR    bool   `json:"ir"`
}

type Stats struct {
	RGBFPS    float64 `json:"rgb_fps"`
	IRFPS     float64 `json:"ir_fps"`
	Subtype   string  `json:"subtype"`
	IRSubtype string  `json:"ir_subtype"`
	RGBMean   float64 `json:"rgb_mean"`
	IRMean    float64 `json:"ir_mean"`
	LEDOn     float64 `json:"led_on"`
	LEDOff    float64 `json:"led_off"`
	RenderMS  float64 `json:"render_ms"`
	StreamFPS float64 `json:"stream_fps"`
	Exposure  string  `json:"exposure"`
	Hint      string  `json:"hint"`
	RGBErr    string  `json:"rgb_error"`
	IRErr     string  `json:"ir_error"`
	DarkOn    bool    `json:"dark_on"`
	DarkRGB   bool    `json:"dark_rgb"`
	DarkIR    bool    `json:"dark_ir"`
	IRAgeMS   float64 `json:"ir_age_ms"`  // IR 亮帧距现在多久（移动伪影量化）
	RGBAgeMS  float64 `json:"rgb_age_ms"` // RGB 帧距现在多久
	SkewMS    float64 `json:"skew_ms"`    // 两路帧的时间错配
	FaceCount int     `json:"face_count"`
	FaceBoxes string  `json:"face_boxes"`
	Person    PersonState `json:"person"`
	VisionOK  bool    `json:"vision_ok"`   // 旁路是否在跑且结果新鲜
	VisionAge float64 `json:"vision_age_ms"`
	YoloFaces int     `json:"yolo_faces"`
	YoloPoses int     `json:"yolo_poses"`
	YoloHands int     `json:"yolo_hands"`
	Gestures  string  `json:"gestures"`
	YoloMs    float64 `json:"yolo_ms"`
	CastA     float64 `json:"cast_a"`     // 场景色偏（LAB a 中位数）
	CastB     float64 `json:"cast_b"`     // 场景色偏（LAB b 中位数）
}

type Engine struct {
	mu     sync.RWMutex
	rgb    *CameraSession
	ir     *CameraSession
	params Params
	cfg    Config
	stats  Stats

	rgbDev, irDev DeviceInfo

	// 事件驱动：相机每出一帧就往 wake 投一个信号（容量 1，重复信号自动合并）
	wake         chan struct{}
	lastRenderAt time.Time
	streamFrames int
	streamWindow time.Time

	// IR 色阶对齐参数的**时间平滑**状态。
	// 逐帧用百分位算参数 → 人一动直方图就变 → 整幅画面亮度跟着抽动（移动伪影之一）。
	// 一阶 IIR（EMA）把它稳住。
	irMu                       sync.Mutex
	irOK                       bool
	irP2, irP98, yP2, yP98, yM float64
	castMu              sync.Mutex
	castOK              bool
	castA, castB        float64
	darkOn              bool // 是否启用暗场补偿（可临时关掉，数据保留）
	// /frame.jpg 的短 TTL 缓存：旁路不限速时每轮都会来取图，
	// 每次都跑一遍完整融合渲染+JPEG 编码的话，CPU 会被吃光（实测过）。
	events    *eventLog // 事件流（底部时间线 / agent 查询用）
	lastPerson  bool
	lastGesture string
	lastFaceN   int
	frameMu   sync.Mutex
	frameBuf  []byte
	frameAt   time.Time
	frameView string
	vision              visionStore
	tracker             *personTracker
	personMu            sync.Mutex
	person              PersonState
	faceMu              sync.Mutex
	faces               []FaceRect
	cascade             *Cascade
	cascadeErr          error

	// 广播器：MJPEG 客户端用 sync.Cond 等新帧，不做 sleep 轮询
	bcast *broadcaster

	rendering atomic.Bool
	clients   atomic.Int64   // 当前 MJPEG 客户端数
}

// broadcaster —— 一写多读的"新帧"广播：写方 publish 后 Broadcast，
// 读方 wait(last) 阻塞到帧号变化。相比每个客户端各自 sleep 轮询：
// 延迟更低、空闲时零唤醒、客户端再多也只等一次。
type broadcaster struct {
	mu   sync.Mutex
	cond *sync.Cond
	seq  uint64
	data []byte
}

func newBroadcaster() *broadcaster {
	b := &broadcaster{}
	b.cond = sync.NewCond(&b.mu)
	return b
}

func (b *broadcaster) publish(d []byte) {
	b.mu.Lock()
	b.data = d
	b.seq++
	b.cond.Broadcast()
	b.mu.Unlock()
}

// wait —— 阻塞到出现比 last 新的帧；ctx 结束时返回 false。
// sync.Cond 本身不可取消，所以挂个看门狗 goroutine 在 ctx 结束时唤醒它。
func (b *broadcaster) wait(ctx context.Context, last uint64) (uint64, []byte, bool) {
	stop := make(chan struct{})
	go func() {
		select {
		case <-ctx.Done():
			b.mu.Lock()
			b.cond.Broadcast()
			b.mu.Unlock()
		case <-stop:
		}
	}()
	defer close(stop)

	b.mu.Lock()
	defer b.mu.Unlock()
	for b.seq == last && ctx.Err() == nil {
		b.cond.Wait()
	}
	if ctx.Err() != nil {
		return b.seq, nil, false
	}
	return b.seq, b.data, true
}

func (b *broadcaster) latest() []byte {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.data
}

func NewEngine() *Engine {
	return newEngineImpl()
}

func newEngineImpl() *Engine {
	darkOn := true // 暗场默认启用（启动时会自动加载 calib/dark_*.gob）
	return &Engine{
		darkOn: darkOn,
		events: newEventLog(300),
		params: Params{Mode: "fuse", Weight: 0.62, Gain: 1.0, Chroma: 1.25, ChromaBlur: 3, WhiteBalance: 0.6, Gamma: 1.0, ChromaDark: 0.6, ChromaGain: 1.0,
			Scale: 1.300, Tx: 98, Ty: 18, Rot: 0, Stretch: 1.0, Quality: 88,
			Thumbs: true, FPS: 30},
		wake:         make(chan struct{}, 1),
		streamWindow: time.Now(),
		bcast:        newBroadcaster(),
	}
}

// smoothTransfer —— 对 IR/RGB 的色阶统计量做 EMA 平滑。
// 逐帧直接用百分位 → 人一动直方图就变 → 画面亮度整片抽动，这是"移动伪影"的来源之一。
func (e *Engine) smoothTransfer(irP2, irP98, yP2, yP98, yMean float64) (float64, float64, float64, float64, float64) {
	const alpha = 0.12 // 越小越稳（拖影），越大越跟手（抽动）
	e.irMu.Lock()
	defer e.irMu.Unlock()
	if !e.irOK {
		e.irP2, e.irP98, e.yP2, e.yP98, e.yM = irP2, irP98, yP2, yP98, yMean
		e.irOK = true
	} else {
		e.irP2 = e.irP2*(1-alpha) + irP2*alpha
		e.irP98 = e.irP98*(1-alpha) + irP98*alpha
		e.yP2 = e.yP2*(1-alpha) + yP2*alpha
		e.yP98 = e.yP98*(1-alpha) + yP98*alpha
		e.yM = e.yM*(1-alpha) + yMean*alpha
	}
	return e.irP2, e.irP98, e.yP2, e.yP98, e.yM
}

// faceLoop —— 后台跑人脸检测（~5fps，1/2 分辨率，实测 7ms/次）
//
// 不在渲染循环里跑：全分辨率一次 36ms，会直接把 45fps 的渲染拖垮。
func (e *Engine) analysisLoop() {
tick := time.NewTicker(330 * time.Millisecond) // ~3fps：人脸/人物检测不需要更快
defer tick.Stop()
for range tick.C {
_, _, p, _ := e.Snapshot()
if !p.FaceDetect && !p.FaceWB && !p.FaceBox && !p.PersonDetect && !p.PersonBox {
continue
}
e.faceMu.Lock()
if e.cascade == nil && e.cascadeErr == nil {
e.cascade, e.cascadeErr = LoadFaceCascade()
if e.cascadeErr != nil {
fmt.Fprintln(os.Stderr, "[face] cascade 加载失败:", e.cascadeErr)
}
}
cas := e.cascade
e.faceMu.Unlock()
if cas == nil {
continue
}
rgbS, irS, _, _ := e.Snapshot()
if rgbS == nil {
continue
}
img, _, _, _, w, h, _ := rgbS.Snapshot()
if img == nil || w < 64 {
continue
}
// Y 平面抽出来 + 降到 1/2
y := make([]byte, w*h)
for row := 0; row < h; row++ {
copy(y[row*w:], img.Y[row*img.YStride:row*img.YStride+w])
}
ds, dw, dh := downscalePlane(y, w, h, 2)
// ── 人物检测（IR差分 + 运动）──
		if p.PersonDetect || p.PersonBox {
			if e.tracker == nil {
				e.tracker = newPersonTracker()
			}
			// IR 差分图：亮帧 - 灭帧，得到"只被红外照亮"的画面（人/近物反射强）
			var idf []byte
			var idw, idh int
			if irS != nil {
				_, gb, gd, _, iw2, ih2, _ := irS.Snapshot()
				if gb != nil && gd != nil && len(gb) == len(gd) {
					full := illumDiff(gb, gd)
					idf, idw, idh = downscalePlane(full, iw2, ih2, 4)
				}
			}
			// 运动用 RGB 的 Y 平面（1/4 分辨率）
			pg, pgw, pgh := downscalePlane(y, w, h, 4)
			st := e.tracker.update(pg, pgw, pgh, idf, idw, idh, w, h)
			e.personMu.Lock()
			e.person = st
			e.personMu.Unlock()
		}

// 旁路结果还新鲜且开了 YOLO 人脸 → 跳过原生 Viola-Jones。
// 旁路的 YOLO 更准（侧脸/遮挡都行），再跑一遍原生检测纯属白烧 CPU。
var faces []FaceRect
if _, _, _, ok := e.vision.get(); ok && e.vision.fresh(1200*time.Millisecond) && p.YoloFace {
	faces = nil
} else {
	faces = cas.Detect(ds, dw, dh, 40, 0, 1.08, 3)
}
for i := range faces {
faces[i].X *= 2
faces[i].Y *= 2
faces[i].W *= 2
faces[i].H *= 2
}
e.faceMu.Lock()
e.faces = faces
e.faceMu.Unlock()
		// ── 事件产生（只在状态**变化**时记，避免刷屏）──
		ps := e.Person()
		if ps.Present != e.lastPerson {
			if ps.Present {
				e.events.add("person", "有人进入画面（"+ps.Zone+"，覆盖 "+itoa(int(ps.Coverage*100))+"%）")
			} else {
				e.events.add("person", "画面里没人了")
			}
			e.lastPerson = ps.Present
		}
		if len(faces) != e.lastFaceN {
			switch {
			case e.lastFaceN == 0 && len(faces) > 0:
				e.events.add("face", "检出人脸 "+itoa(len(faces))+" 张")
			case len(faces) == 0:
				e.events.add("face", "人脸丢失")
			default:
				e.events.add("face", "人脸数变为 "+itoa(len(faces)))
			}
			e.lastFaceN = len(faces)
		}
		if vr2, _, _, ok2 := e.vision.get(); ok2 && e.vision.fresh(1200*time.Millisecond) && len(vr2.Hands) > 0 {
			g := ""
			for _, hd := range vr2.Hands {
				g += hd.Hand + ":" + hd.Gesture + " "
			}
			if g != e.lastGesture {
				e.events.add("gesture", "手势 "+g)
				e.lastGesture = g
			}
		}
}
}

func itoa(v int) string { return strconv.Itoa(v) }

func (e *Engine) Person() PersonState {
	e.personMu.Lock()
	defer e.personMu.Unlock()
	return e.person
}

func (e *Engine) Faces() []FaceRect {
e.faceMu.Lock()
defer e.faceMu.Unlock()
return append([]FaceRect(nil), e.faces...)
}

// MeasureCast —— 直接量当前 RGB 原始帧的色偏（LAB a/b 截尾均值）。
// 白平衡校准要靠它：不能拿融合图的色偏当依据（融合另有 IR 与色彩增强影响）。
func (e *Engine) MeasureCast() (float64, float64, bool) {
	rgbS, _, _, _ := e.Snapshot()
	if rgbS == nil {
		return 0, 0, false
	}
	img, _, _, _, w, h, _ := rgbS.Snapshot()
	if img == nil {
		return 0, 0, false
	}
	bgr := ycbcrToBGR(img)
	a, b := measureCast(bgrToLAB(bgr, w, h), w, h)
	return a, b, true
}

// AutoTuneWhiteBalance —— 扫描相机色温控制，取"最中性"的那一档。
//
// 依据：实测色温值越低画面越蓝、越高越黄，本机在 4000K 附近最中性。
// 每步都等**真正的新帧**再测（不 sleep 赌时间），并对同一档测两帧取平均抗噪。
func (e *Engine) AutoTuneWhiteBalance() (map[string]any, error) {
	rgbS, _, _, _ := e.Snapshot()
	if rgbS == nil {
		return nil, fmt.Errorf("RGB 相机不在线")
	}
	cr, ok := rgbS.WhiteBalanceRange()
	if !ok {
		return nil, fmt.Errorf("相机不支持白平衡控制")
	}
	// 两阶段：先粗搜 6 档，再在最优点附近精搜 7 档。
	// 单档 10K 太密没必要；色温是个 1 维旋钮，但 a/b 是两个目标，只能最小化 |a|+|b|。
	span := (cr.Max - cr.Min) / 5
	steps := []int32{cr.Min, cr.Min + span, cr.Min + 2*span, cr.Min + 3*span,
		cr.Min + 4*span, cr.Max}
	type row struct {
		K     int32     `json:"kelvin"`
		A     float64   `json:"a"`
		B     float64   `json:"b"`
		Score float64   `json:"score"`
	}
	var rows []row
	best := -1
	measure := func(v int32) bool {
		if err := rgbS.SetWhiteBalance(v, false); err != nil {
			return false
		}
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		last := rgbS.Seq()
		var sa, sb float64
		n := 0
		for n < 3 { // 等 3 帧新画面（白平衡生效要几帧），不 sleep 赌时间
			seq, okf := rgbS.WaitFrame(ctx, last)
			if !okf {
				break
			}
			last = seq
			if a, b, okm := e.MeasureCast(); okm {
				sa += a
				sb += b
				n++
			}
		}
		cancel()
		if n == 0 {
			return false
		}
		a, b := sa/float64(n), sb/float64(n)
		sc := math.Abs(a) + math.Abs(b)
		rows = append(rows, row{K: v, A: round2(a), B: round2(b), Score: round2(sc)})
		if best < 0 || sc < rows[best].Score {
			best = len(rows) - 1
		}
		return true
	}
	for _, v := range steps {
		measure(v)
	}
	// 第二阶段：围绕最优点细化
	if best >= 0 && best < len(rows) {
		center := rows[best].K
		for d := int32(-span/2); d <= span/2; d += span / 6 {
			v := center + d
			if v < cr.Min || v > cr.Max || v == center {
				continue
			}
			measure(v)
		}
	}
	res := map[string]any{"scan": rows, "capacity": map[string]any{"min": cr.Min, "max": cr.Max}}
	if best >= 0 && best < len(rows) {
		// 取最中性档左右再细插一档（步长 10K 太密没必要，直接用它）
		if err := rgbS.SetWhiteBalance(rows[best].K, false); err == nil {
			res["applied"] = rows[best].K
			res["applied_score"] = rows[best].Score
		}
	}
	return res, nil
}

// SetDarkEnabled —— 临时启用/停用暗场补偿（**不删数据**）。
// 灯光或曝光一变，旧的暗场就未必对，黑位会发灰发彩，这时先关掉排除一下。
func (e *Engine) SetDarkEnabled(on bool) {
	rgbS, irS, _, _ := e.Snapshot()
if rgbS == nil {
e.stats.RGBFPS, e.stats.Subtype, e.stats.RGBErr = 0, "", ""
e.stats.RGBMean, e.stats.DarkRGB = 0, false
}
if irS == nil {
e.stats.IRFPS, e.stats.IRSubtype, e.stats.IRErr = 0, "", ""
e.stats.IRMean, e.stats.LEDOn, e.stats.LEDOff, e.stats.DarkIR = 0, 0, 0, false
}
if rgbS != nil {
		if on {
			rgbS.SetDark(loadDark("rgb"))
		} else {
			rgbS.SetDark(nil)
		}
	}
	if irS != nil {
		if on {
			irS.SetDark(loadDark("ir"))
		} else {
			irS.SetDark(nil)
		}
	}
	e.mu.Lock()
	e.darkOn = on
	e.mu.Unlock()
}

// GammaInfo —— 软件 gamma + 相机 gamma（IAMVideoProcAmp[5]）
func (e *Engine) GammaInfo() map[string]any {
	out := map[string]any{"software": e.getGamma()}
	rgbS, _, _, _ := e.Snapshot()
	if rgbS != nil {
		if v, _, ok := rgbS.VideoProcAmpGet(5); ok {
			out["camera"] = v
			out["camera_default"] = 300
		}
	}
	return out
}

func (e *Engine) getGamma() float64 {
	e.mu.RLock()
	defer e.mu.RUnlock()
	return e.params.Gamma
}

// WhiteBalanceStatus —— 相机白平衡能力 + 当前值（给 UI 与 agent 用）
func (e *Engine) WhiteBalanceStatus() map[string]any {
	out := map[string]any{}
	rgbS, _, _, _ := e.Snapshot()
	if rgbS == nil {
		out["supported"] = false
		return out
	}
	cr, ok := rgbS.WhiteBalanceRange()
	if !ok {
		out["supported"] = false
		return out
	}
	v, f, ok2 := rgbS.WhiteBalance()
	out["supported"] = true
	out["min"], out["max"], out["step"], out["default"] = cr.Min, cr.Max, cr.Step, cr.Default
	out["caps"] = cr.Caps
	out["auto_capable"] = cr.Caps&1 != 0
	out["manual_capable"] = cr.Caps&2 != 0
	if ok2 {
		out["value"] = v
		out["flags"] = f
		out["auto"] = f == 1
	}
	return out
}

// SetWhiteBalance —— value<0 保留当前值只切模式
func (e *Engine) SetWhiteBalance(value int32, auto bool) error {
	rgbS, _, _, _ := e.Snapshot()
	if rgbS == nil {
		return fmt.Errorf("RGB 相机不在线")
	}
	return rgbS.SetWhiteBalance(value, auto)
}

// smoothCast —— 场景色偏参考值的时间平滑（避免整幅色温跳动）
func (e *Engine) smoothCast(a, b float64) (float64, float64) {
const alpha = 0.08
e.castMu.Lock()
defer e.castMu.Unlock()
if !e.castOK {
e.castA, e.castB = a, b
e.castOK = true
} else {
e.castA = e.castA*(1-alpha) + a*alpha
e.castB = e.castB*(1-alpha) + b*alpha
}
return e.castA, e.castB
}

func (e *Engine) castRef() (float64, float64) {
e.castMu.Lock()
defer e.castMu.Unlock()
return e.castA, e.castB
}

// notifyFrame —— 相机的"有新帧"回调：非阻塞投递信号（重复信号自动合并）
func (e *Engine) notifyFrame() {
	select {
	case e.wake <- struct{}{}:
	default:
	}
}

// seqOf —— 两路相机的帧号合成一个单调值，用来判断"有没有新东西可渲染"
func (e *Engine) seqOf() int64 {
	e.mu.RLock()
	rgbS, irS := e.rgb, e.ir
	e.mu.RUnlock()
	var s int64
	if rgbS != nil {
		s = rgbS.Seq()
	}
	if irS != nil {
		s += irS.Seq() * 1000003
	}
	return s
}

// preferFor —— 码流 → MF 子类型优先级
func preferFor(codec string, withIR bool) []string {
	switch codec {
	case "yuy2":
		return []string{"YUY2"}
	case "nv12":
		return []string{"NV12"}
	case "mjpg":
		return []string{"MJPG"}
	default: // auto
		if withIR {
			return []string{"MJPG"} // 未压缩会和 IR 抢 USB 带宽
		}
		return []string{"YUY2", "MJPG"} // 单路优先画质
	}
}

func (e *Engine) Start(codec string, w, h int, useIR bool) error {
	cams := ListCameras()
	if len(cams) == 0 {
		return fmt.Errorf("没有发现任何相机")
	}
	for _, d := range cams {
		if d.Kind == "rgb" && e.rgbDev.SymbolicLink == "" {
			e.rgbDev = d
		}
		if d.Kind == "ir" && e.irDev.SymbolicLink == "" {
			e.irDev = d
		}
	}
	if e.rgbDev.SymbolicLink == "" {
		return fmt.Errorf("没有找到 RGB 相机")
	}
	cfg, note, err := e.normalize(codec, w, h, useIR)
	if err != nil {
		return err
	}
	e.mu.Lock()
	e.cfg = cfg
	e.stats.Hint = note
	e.mu.Unlock()

	e.startRGB(cfg)
	if cfg.IR {
		e.startIR()
	}
	if visionSidecarOn {
startVisionSidecar(visionSidecarFPS)
}
go e.renderLoop()
	go e.analysisLoop()
	return nil
}

func (e *Engine) startRGB(cfg Config) {
	s := NewCameraSession(e.rgbDev, false)
	s.SetDark(loadDark("rgb"))
	s.OnFrame = e.notifyFrame
	e.mu.Lock()
	e.rgb = s
	e.mu.Unlock()
	go s.Run(preferFor(cfg.Codec, cfg.IR), cfg.W, cfg.H, cfg.Codec)
}

func (e *Engine) startIR() {
	if e.irDev.SymbolicLink == "" {
		return
	}
	s := NewCameraSession(e.irDev, true)
	s.SetDark(loadDark("ir"))
	s.OnFrame = e.notifyFrame
	e.mu.Lock()
	e.ir = s
	e.mu.Unlock()
	// IR 相机只有 YUY2 340x340（实测唯一一种原生格式）
	go s.Run([]string{"YUY2", "MJPG"}, 340, 340, "yuy2")
}

// forceBandwidth —— 诊断开关：临时绕过带宽规则（见 handleDiagDual）
var forceBandwidth bool

// visionSidecarFPS —— >0 时启动旁路视觉进程（由 main 的 -vision 设置）
var visionSidecarFPS float64

// visionSidecarOn —— 是否启用旁路（注意 fps=0 表示不限速，不能用 fps>0 当开关！）
var visionSidecarOn bool

// 实测的 USB 总线预算与各格式的像素位宽。
// 依据（都在本机实测）：
//   YUY2 640×480 @30 = 18.4 MB/s ；IR YUY2 340×340 @30 = 6.9 MB/s
//   两者同开 = 25.3 MB/s → RGB 直接掉到 0 fps（总线到顶）
//   NV12 640×480 @30 = 13.8 MB/s ；+IR = 20.7 MB/s → 稳定 14.9 + 29.8 fps ✓
// 所以预算取 21 MB/s 左右，别的一律按位宽换算。
const (
	busBudgetMBps = 21.0
	irMBps        = 6.9 // IR 通道固定开销（YUY2 340×340@30）
)

var codecBPP = map[string]float64{
	"yuy2": 2.0,  // YUV 4:2:2 打包，16 bit/px
	"nv12": 1.5,  // YUV 4:2:0 半平面，12 bit/px
	"mjpg": 0.12, // 实测 640×480 约 20~30 KB/帧
}

func bandwidthMBps(codec string, w, h int, fps float64) float64 {
	bpp, ok := codecBPP[codec]
	if !ok {
		bpp = 1.5
	}
	if fps <= 0 {
		fps = 30
	}
	return float64(w*h) * bpp / 1e6 * fps
}

// normalize —— 带宽规则：按实测总线预算判断"能不能同时开"
func (e *Engine) normalize(codec string, w, h int, ir bool) (Config, string, error) {
	if codec == "" || codec == "auto" {
		if ir {
			codec = "mjpg"
		} else {
			codec = "yuy2"
		}
	}
	if w == 0 || h == 0 {
		w, h = 640, 480
	}
	note := ""
	// YUY2 在相机侧最高只到 640×480（实测）。用户选了更高分辨率时**自动夹回去**并说明，
	// 而不是直接报错不改配置（那样 UI 上看着像"点了没反应"）。
	if codec == "yuy2" && (w > 640 || h > 480) {
		note += fmt.Sprintf("YUY2 最高只支持 640×480（相机能力），已自动改为 640×480。想要 720p/1080p 请用 NV12 或 MJPG。\n")
		w, h = 640, 480
	}
	needRGB := bandwidthMBps(codec, w, h, 30)
	if !forceBandwidth && needRGB > busBudgetMBps {
		note += fmt.Sprintf(
			"⚠ %s %d×%d 单独一路就要 %.1f MB/s，超过 USB 实测预算 %.0f MB/s："+
				"会出现掉帧/花屏/色块（不是色彩处理的问题）。建议改 MJPG（同分辨率只要约 %.1f MB/s）。\n",
			strings.ToUpper(codec), w, h, needRGB, busBudgetMBps, bandwidthMBps("mjpg", w, h, 30))
	}
	if ir && !forceBandwidth {
		need := needRGB + irMBps
		if need > busBudgetMBps {
			ir = false
			note += fmt.Sprintf(
				"带宽不够：%s %d×%d + IR 需要约 %.1f MB/s，超过 USB 实测预算 %.0f MB/s（会掉到 0~1.3fps），"+
					"已自动**只开一路**（关掉 IR）。\n"+
					"要 RGB+IR 同时跑：① 换 NV12（12bit/px，省 25%%，实测 640×480 能带得动 IR）② 或换 MJPG（有损但最省）。",
				strings.ToUpper(codec), w, h, need, busBudgetMBps)
		}
	}
	return Config{Codec: codec, W: w, H: h, IR: ir}, note, nil
}

// SetConfig —— 热切换（会重开采集；改码流/分辨率必须重开）
func (e *Engine) SetConfig(codec string, w, h int, ir bool) (Config, string, error) {
	cfg, note, err := e.normalize(codec, w, h, ir)
	if err != nil {
		return Config{}, "", err
	}
	e.mu.RLock()
	old := e.cfg
	e.mu.RUnlock()

	// 按**会话实际状态**决定启停，而不是看 old.IR 这个配置标志。
	// 两者一旦不一致（例如"超带宽自动关 IR"那条路径），IR 会一直跑着吃 USB 带宽 ——
	// 实测表现：config 说 ir=false，IR 却还在 29.7fps，把 YUY2 的预算挤爆。
	e.mu.RLock()
	hasIR := e.ir != nil
	e.mu.RUnlock()
	if hasIR && !cfg.IR {
		e.mu.Lock()
		if e.ir != nil {
			e.ir.Stop()
			e.ir = nil
		}
		e.mu.Unlock()
		time.Sleep(300 * time.Millisecond)
	}
	if !hasIR && cfg.IR {
		e.startIR()
		time.Sleep(300 * time.Millisecond)
	}
	if old.Codec != cfg.Codec || old.W != cfg.W || old.H != cfg.H || old.IR != cfg.IR {
		e.events.add("config", "切换采集 "+cfg.Codec+" "+itoa(cfg.W)+"x"+itoa(cfg.H)+" IR="+map[bool]string{true: "开", false: "关"}[cfg.IR])
		e.mu.Lock()
		oldRGB := e.rgb
		e.mu.Unlock()
		if oldRGB != nil {
			oldRGB.Stop()
			time.Sleep(400 * time.Millisecond) // 等设备真正释放
		}
		e.startRGB(cfg)
	}
	e.mu.Lock()
	e.cfg = cfg
	e.stats.Hint = note
	e.mu.Unlock()
	e.notifyFrame()
	return cfg, note, nil
}

func (e *Engine) Snapshot() (*CameraSession, *CameraSession, Params, Config) {
	e.mu.RLock()
	defer e.mu.RUnlock()
	return e.rgb, e.ir, e.params, e.cfg
}

func (e *Engine) Stats() (Stats, Params, Config) {
	e.mu.RLock()
	defer e.mu.RUnlock()
	return e.stats, e.params, e.cfg
}

func (e *Engine) SetParams(f func(p *Params)) Params {
	e.mu.Lock()
	f(&e.params)
	p := e.params
	e.mu.Unlock()
	e.notifyFrame()
	return p
}

// ───────────────────────── 视图 ─────────────────────────

const (
	thumbW, thumbH = 0, 120 // thumbW 按画布宽均分 4 列，见 renderMosaic
	gap            = 4
)

// renderCtx —— 一帧内**算一次、所有视图共用**的中间结果。
// 之前每个视图各做一次 warpGray，拼图（主图+3缩略图）就是 4 次 —— 白烧 3 倍 CPU。
type renderCtx struct {
	eng                  *Engine
	faceCastA, faceCastB float64
	faceCastOK           bool
	base                 *image.YCbCr
	baseBGR   []byte // 懒加载：只有走 LAB 的视图才需要
	irW, mask []byte
	diff      []byte
	rw, rh    int
	p         Params
}

// bgr —— 按需把 YCbCr 展开成 BGR（LAB 融合要用）
func (c *renderCtx) bgr() []byte {
	if c.baseBGR == nil {
		c.baseBGR = ycbcrToBGR(c.base)
	}
	return c.baseBGR
}

// chromaKsize —— 色度高斯核大小：ChromaBlur 是半径，ksize=2r+1。
// 默认 ChromaBlur=3 → ksize=7，与 OpenCV 参考实现的 int(3+2*chroma)|1 等价。
func (c *renderCtx) chromaKsize() int {
	if c.p.ChromaBlur <= 0 {
		return 0
	}
	return c.p.ChromaBlur*2 + 1
}

func (e *Engine) buildCtx() (*renderCtx, error) {
	rgbS, irS, p, _ := e.Snapshot()
	if rgbS == nil {
		return nil, fmt.Errorf("还没有 RGB 画面")
	}
	base, _, _, _, rw, rh, _ := rgbS.Snapshot()
	if base == nil {
		return nil, fmt.Errorf("RGB 还没有帧")
	}
	ctx := &renderCtx{eng: e, base: base, rw: rw, rh: rh, p: p}
	if irS != nil {
		_, gray, grayDk, _, iw, ih, _ := irS.Snapshot()
		if gray != nil && iw > 0 {
			rot := p.Rot * 3.14159265358979 / 180
			ctx.irW, ctx.mask = warpGray(gray, iw, ih, p.Scale, rot, p.Tx, p.Ty, rw, rh)
			// 把 IR 色阶对齐到 RGB 的亮度分布（不再"拉伸到满量程"），
			// 统计量过 EMA 平滑，避免移动时整幅画面亮度抽动
			a1, a2, b1, b2, bm := e.smoothTransfer(
				planePercentile(ctx.irW, rw, rw, rh, 2),
				planePercentile(ctx.irW, rw, rw, rh, 98),
				planePercentile(base.Y, base.YStride, rw, rh, 2),
				planePercentile(base.Y, base.YStride, rw, rh, 98),
				planeMean(base.Y[:rh*base.YStride]))
			blend := p.Stretch
			if blend <= 0 {
				blend = 1.0
			}
			transferIR(ctx.irW, rw, rw, rh, a1, a2, b1, b2, bm, p.Gain, blend)
			if grayDk != nil {
				raw := illumDiff(gray, grayDk)
				ctx.diff, _ = warpGray(raw, iw, ih, p.Scale, rot, p.Tx, p.Ty, rw, rh)
			}
		}
	}
	// 人脸区域白平衡：肤色比整帧更接近中性参考，比灰世界准得多
	if p.FaceWB {
		fs := e.Faces()
		if len(fs) > 0 {
			f := fs[0]
			for _, g := range fs { // 取最大的一张脸
				if g.W*g.H > f.W*f.H {
					f = g
				}
			}
			if roi, rw2, rh2, ok := cropBGR(ctx.bgr(), rw, rh, f); ok {
				ctx.faceCastA, ctx.faceCastB = measureCast(bgrToLAB(roi, rw2, rh2), rw2, rh2)
				ctx.faceCastOK = true
			}
		}
	}
	return ctx, nil
}

// viewFromCtx —— 从共用中间结果拼出某个视图（几乎零成本）
func (c *renderCtx) view(mode string) *image.YCbCr {
	switch mode {
	case "rgb":
		return c.base
	case "ir":
		if c.irW == nil {
			return c.base
		}
		return grayToYCbCr(c.irW, c.rw, c.rh)
	case "ircolor":
		if c.irW == nil {
			return c.base
		}
		return colorMapYCbCr(c.irW, c.rw, c.rh)
	case "diff":
		if c.diff == nil {
			return c.base
		}
		return colorMapYCbCr(c.diff, c.rw, c.rh)
	case "detail":
		if c.irW == nil {
			return c.base
		}
		bgr := detailLAB(c.bgr(), c.rw, c.rh, c.irW, c.mask, c.p.Weight*2.0)
		return bgrBytesTo420(bgr, c.rw, c.rh)
	case "edge", "edgefuse":
		// 边缘图：对亮度做梯度幅值。
		// 开了 IR 就把 IR 混进来一起求梯度 —— IR 在暗处的对比度远好于 RGB，
		// 融合后边缘会明显更完整（这就是"IR 开启时增强边缘"的做法）。
		w, h := c.rw, c.rh
		y := make([]byte, w*h)
		for row := 0; row < h; row++ {
			copy(y[row*w:], c.base.Y[row*c.base.YStride:row*c.base.YStride+w])
		}
		if c.irW != nil {
			irP2 := planePercentile(c.irW, w, w, h, 2)
			irP98 := planePercentile(c.irW, w, w, h, 98)
			sp := (irP98 - irP2)
			if sp < 1 {
				sp = 1
			}
			mix := 0.5
			if mode == "edgefuse" {
				mix = 0.75
			}
			// 按**配准掩码**混合：IR 只在覆盖区有效，羽化边界外是 0。整幅 50/50 混会在
			// 掩码边界留一条矩形假边，被梯度算子抓成框（用户报的"边缘图把叠加层算进去了"就是这个）。
			for i := range y {{
				iv := (float64(c.irW[i]) - irP2) * 255.0 / sp
				m := 1.0
				if c.mask != nil {{
					m = float64(c.mask[i]) / 255.0
				}}
				w := mix * m
				y[i] = clamp8(float64(y[i])*(1-w) + iv*w)
			}}
		}
		g := gradMag(y, w, h)
		var mx float32
		for _, v := range g {
			if v > mx {
				mx = v
			}
		}
		scale := float32(255.0)
		if mx > 1 {
			scale = 255.0 / mx
		}
		out := make([]byte, w*h)
		for i, v := range g {
			out[i] = clamp8(float64(v * scale))
		}
		return grayToYCbCr(out, w, h)
	case "ircolor2":
		// IR 伪彩（INFERNO）—— 与 ircolor 同源，留个名字给缩略图轮换用
		if c.irW == nil {
			return c.base
		}
		return colorMapYCbCr(c.irW, c.rw, c.rh)
case "darkmap":
// 暗场热点图：把暗场校准数据（每像素偏置/FPN）可视化。
// 热点亮的地方＝校准没盖住的固定图案噪声，用来判断"那块灰/彩是不是暗场残留"。
rgbS, irS, _, _ := c.eng.Snapshot()
var d *DarkField
if rgbS != nil {
d = rgbS.Dark()
}
if d == nil && irS != nil {
d = irS.Dark()
}
if d == nil || d.W == 0 || len(d.Gray) < d.W*d.H {
return c.base
}
rs := resizeYCbCrCover(grayToYCbCr(d.Gray, d.W, d.H), c.rw, c.rh)
out := make([]byte, c.rw*c.rh)
mx := float64(1)
for y := 0; y < c.rh; y++ {
for x := 0; x < c.rw; x++ {
v := float64(rs.Y[y*rs.YStride+x])
out[y*c.rw+x] = uint8(v)
if v > mx {
mx = v
}
}
}
sc := 255.0 / mx // 暗场值很小（个位数~几十），不拉伸是全黑
for i := range out {
out[i] = clamp8(float64(out[i]) * sc)
}
return grayToYCbCr(out, c.rw, c.rh)
	case "alignerr":
		// 配准误差图：|IR(已配准) - RGB 亮度| 的边缘。
		// 配准对了这里应该是"物体轮廓细线"；整片发亮说明没对上。
		if c.irW == nil {
			return c.base
		}
		w, h := c.rw, c.rh
		d := make([]byte, w*h)
		for row := 0; row < h; row++ {
			for x := 0; x < w; x++ {
				i := row*w + x
				a := int(c.base.Y[row*c.base.YStride+x])
				b := int(c.irW[i])
				v := a - b
				if v < 0 {
					v = -v
				}
				d[i] = uint8(v)
			}
		}
		// 反相 + 拉伸，让误差看得清
		g := gradMag(d, w, h)
		var mx float32
		for _, v := range g {
			if v > mx {
				mx = v
			}
		}
		sc := float32(255.0)
		if mx > 1 {
			sc = 255.0 / mx
		}
		out := make([]byte, w*h)
		for i, v := range g {
			out[i] = clamp8(float64(v * sc))
		}
		return grayToYCbCr(out, w, h)
	case "side":		if c.irW == nil {
			return c.base
		}
		out := image.NewYCbCr(image.Rect(0, 0, c.rw*2, c.rh), image.YCbCrSubsampleRatio420)
		blitYCbCr(out, c.base, 0, 0)
		blitYCbCr(out, grayToYCbCr(c.irW, c.rw, c.rh), c.rw, 0)
		return out
	default: // fuse —— 走 LAB，与 OpenCV 参考实现同款色彩数学
		if c.irW == nil {
			return c.base
		}
		ra, rb := c.eng.castRef()
		bgr, ca, cb := fuseLAB(c.bgr(), c.rw, c.rh, c.irW, c.mask,
			c.p.Weight, c.p.Chroma*c.p.ChromaGain, c.chromaKsize(), c.p.WhiteBalance, ra, rb,
			c.p.Gamma, c.p.ChromaDark)
		if c.p.FaceWB && c.faceCastOK {
			c.eng.smoothCast(c.faceCastA, c.faceCastB) // 人脸区域色偏做参考
		} else {
			c.eng.smoothCast(ca, cb) // 全帧灰世界
		}
		return bgrBytesTo420(bgr, c.rw, c.rh)
	}
}

// cropBGR —— 从 BGR 交错图裁一块 ROI（自动夹到边界内）
func cropBGR(bgr []byte, w, h int, r FaceRect) ([]byte, int, int, bool) {
	x0, y0 := r.X, r.Y
	x1, y1 := r.X+r.W, r.Y+r.H
	if x0 < 0 {
		x0 = 0
	}
	if y0 < 0 {
		y0 = 0
	}
	if x1 > w {
		x1 = w
	}
	if y1 > h {
		y1 = h
	}
	rw, rh := x1-x0, y1-y0
	if rw < 8 || rh < 8 {
		return nil, 0, 0, false
	}
	out := make([]byte, rw*rh*3)
	for y := 0; y < rh; y++ {
		copy(out[y*rw*3:(y+1)*rw*3], bgr[((y0+y)*w+x0)*3:((y0+y)*w+x0+rw)*3])
	}
	return out, rw, rh, true
}

// drawPersonBox —— 人物框：1 像素、Y=150（比人脸框暗），画成虚线好区分
func drawPersonBox(img *image.YCbCr, r FaceRect, mirrored bool) {
	w, h := img.Rect.Dx(), img.Rect.Dy()
	x0, y0 := r.X, r.Y
	x1, y1 := r.X+r.W, r.Y+r.H
	if mirrored {
		x0, x1 = w-1-x1, w-1-x0
	}
	put := func(x, y int, on bool) {
		if !on || x < 0 || y < 0 || x >= w || y >= h {
			return
		}
		img.Y[y*img.YStride+x] = 150
	}
	for x := x0; x <= x1; x++ {
		dash := ((x - x0) / 6 % 2) == 0
		put(x, y0, dash)
		put(x, y1, dash)
	}
	for y := y0; y <= y1; y++ {
		dash := ((y - y0) / 6 % 2) == 0
		put(x0, y, dash)
		put(x1, y, dash)
	}
}

// drawFaceBoxes —— 在 Y 平面上画人脸框（白色描边，2 像素粗）
func drawFaceBoxes(img *image.YCbCr, faces []FaceRect, mirrored bool) {
	w, h := img.Rect.Dx(), img.Rect.Dy()
	put := func(x, y int) {
		if x < 0 || y < 0 || x >= w || y >= h {
			return
		}
		img.Y[y*img.YStride+x] = 235
	}
	for _, f := range faces {
		x0, y0 := f.X, f.Y
		x1, y1 := f.X+f.W, f.Y+f.H
		if mirrored {
			x0, x1 = w-1-x1, w-1-x0
		}
		for x := x0; x <= x1; x++ {
			for t := 0; t < 2; t++ {
				put(x, y0+t)
				put(x, y1-t)
			}
		}
		for y := y0; y <= y1; y++ {
			for t := 0; t < 2; t++ {
				put(x0+t, y)
				put(x1-t, y)
			}
		}
	}
}

func (e *Engine) renderView(mode string) (*image.YCbCr, error) {
	ctx, err := e.buildCtx()
	if err != nil {
		return nil, err
	}
	return flipYCbCr(ctx.view(mode), ctx.p.MirrorH, ctx.p.MirrorV), nil
}

func (e *Engine) renderMosaic() (*image.YCbCr, error) {
	ctx, err := e.buildCtx()
	if err != nil {
		return nil, err
	}
	main := ctx.view(ctx.p.Mode)
	if !ctx.p.Thumbs {
		return flipYCbCr(main, ctx.p.MirrorH, ctx.p.MirrorV), nil
	}
	mw, mh := main.Rect.Dx(), main.Rect.Dy()
	canvas := image.NewYCbCr(image.Rect(0, 0, mw, mh+gap+thumbH), image.YCbCrSubsampleRatio420)
	for i := range canvas.Y {
		canvas.Y[i] = 36
	}
	for i := range canvas.Cb {
		canvas.Cb[i] = 128
		canvas.Cr[i] = 128
	}
	// 逐块翻转：镜像只翻画面内容，不翻缩略图的排版顺序
	mn := flipYCbCr(main, ctx.p.MirrorH, ctx.p.MirrorV)
	// 叠加层画在**副本**上：view("rgb") 等直通分支返回的就是 c.base 本体，
	// 直接画会污染底图，缩略图和边缘图就都把框/骨架算进去了。
	if ctx.p.FaceBox || ctx.p.PersonBox || ctx.p.YoloFace || ctx.p.YoloPose || ctx.p.YoloHand {
		mn = cloneYCbCr(mn)
	}
	if ctx.p.FaceBox {
		drawFaceBoxes(mn, ctx.eng.Faces(), ctx.p.MirrorH)
	}
	if ctx.p.PersonBox {
		ps := ctx.eng.Person()
		if ps.Box.W > 0 {
			drawPersonBox(mn, ps.Box, ctx.p.MirrorH)
		}
	}
	// 旁路视觉结果（YOLO 人脸 / 姿态）：结果按推理时的画面尺寸缩放过来
	if (ctx.p.YoloFace || ctx.p.YoloPose || ctx.p.YoloHand) && ctx.eng.vision.fresh(3*time.Second) {
		vr, vw, vh, _ := ctx.eng.vision.get()
		mv := mn
		if ctx.p.MirrorH {
			mv = flipYCbCr(mn, true, false) // 先按未镜像坐标画，再翻回去
		}
		drawVision(mv, vr, vw, vh, ctx.p.YoloFace, ctx.p.YoloPose, ctx.p.YoloHand)
		if ctx.p.MirrorH {
			copy(mn.Y, mv.Y)
			copy(mn.Cb, mv.Cb)
			copy(mn.Cr, mv.Cr)
		}
	}
	blitYCbCr(canvas, mn, 0, 0)
	// 4 列均分（原来 3×160 只占 496px，右边空 144px 很扎眼；顺便整排自然居中）
	tw := (mw - 5*gap) / 4
	for i, v := range []string{"rgb", "ir", "diff", "edge"} {
		th := resizeYCbCrCover(ctx.view(v), tw, thumbH) // cover：填满格子，不留黑边
		blitYCbCr(canvas, flipYCbCr(th, ctx.p.MirrorH, ctx.p.MirrorV),
			i*(tw+gap)+gap, mh+gap)
	}
	return canvas, nil
}

func (e *Engine) paramsMode() string {
	e.mu.RLock()
	defer e.mu.RUnlock()
	return e.params.Mode
}

func (e *Engine) renderLoop() {
	var lastRendered int64 = -1
	statTick := time.NewTicker(time.Second)
	defer statTick.Stop()

	for {
		// ① 没新帧就阻塞等待（新帧信号 / 统计节拍），不空转
		if e.seqOf() == lastRendered {
			select {
			case <-e.wake:
			case <-statTick.C:
				e.updateStats()
			}
			continue
		}
		// ② 有新帧就**尽快**渲染（帧一到就出图，不再固定等 40ms），
		//    但不超过目标帧率上限 —— 这是延迟最低的写法
		_, _, p, _ := e.Snapshot()
		fps := p.FPS
		if fps < 5 {
			fps = 5
		}
		if fps > 120 {
			fps = 120
		}
		if remain := time.Duration(float64(time.Second)/fps) - time.Since(e.lastRenderAt); remain > 0 {
			timer := time.NewTimer(remain)
			select {
			case <-timer.C:
			case <-statTick.C:
				e.updateStats()
			}
			timer.Stop()
		}
		e.lastRenderAt = time.Now()
		lastRendered = e.seqOf()
		// 没人在看就不渲染（空闲 CPU 归零；快照接口仍按需实时渲染）
		if e.clients.Load() == 0 {
			continue
		}
		t0 := time.Now()
		if img, err := e.renderMosaic(); err == nil {
			b := newByteWriter(1 << 20)
			if jpeg.Encode(b, img, &jpeg.Options{Quality: p.Quality}) == nil {
				e.bcast.publish(b.Bytes()) // 广播给所有 MJPEG 客户端
				e.setRenderMS(float64(time.Since(t0).Microseconds()) / 1000)
				e.mu.Lock()
				e.streamFrames++
				e.mu.Unlock()
			}
		}
		// 注意：这里**不能**再更新 lastRenderAt —— 节拍点在循环开头，
		// 渲染后更新会让周期变成"预算 + 渲染时间"（实测掉到 20fps）
		select {
		case <-statTick.C:
			e.updateStats()
		default:
		}
	}
}

func (e *Engine) setRenderMS(v float64) {
	e.mu.Lock()
	e.stats.RenderMS = v
	e.mu.Unlock()
}

func (e *Engine) updateStats() {
	rgbS, irS, _, cfg := e.Snapshot()
	e.mu.Lock()
	defer e.mu.Unlock()
	// 拼图流帧率：渲染循环累加，这里按时间窗换算
	e.stats.DarkOn = e.darkOn
	ca, cb := e.castRef()
	fs := e.faces
	e.stats.FaceCount = len(fs)
	var fb strings.Builder
	for _, f := range fs {
		fmt.Fprintf(&fb, "[%d,%d %dx%d] ", f.X, f.Y, f.W, f.H)
	}
	e.stats.FaceBoxes = fb.String()
	e.stats.Person = e.person
	vr, _, _, vok := e.vision.get()
	e.stats.VisionOK = vok && e.vision.fresh(3*time.Second)
	if vok {
		e.stats.VisionAge = float64(time.Since(vr.At).Milliseconds())
		e.stats.YoloFaces = len(vr.Faces)
		e.stats.YoloPoses = len(vr.Poses)
		e.stats.YoloHands = len(vr.Hands)
		var gb strings.Builder
		for _, hd := range vr.Hands {
			gb.WriteString(hd.Hand + ":" + hd.Gesture + " ")
		}
		e.stats.Gestures = gb.String()
		e.stats.YoloMs = vr.MsFace + vr.MsPose
	}
	e.stats.CastA, e.stats.CastB = ca, cb
	if d := time.Since(e.streamWindow); d >= 900*time.Millisecond {
		e.stats.StreamFPS = float64(e.streamFrames) / d.Seconds()
		e.streamFrames = 0
		e.streamWindow = time.Now()
	}
	if rgbS != nil {
		img, _, _, sub, _, _, fps := rgbS.Snapshot()
		e.stats.RGBFPS, e.stats.Subtype = fps, sub
		e.stats.RGBErr = errStr(rgbS.Err())
		e.stats.DarkRGB = rgbS.Dark() != nil
		if img != nil {
			e.stats.RGBMean = planeMean(img.Y[:img.Rect.Dy()*img.YStride])
		}
	}
	if irS != nil {
		_, gray, grayDk, sub, _, _, fps := irS.Snapshot()
		if rgbS != nil {
			rgbAge, irAgeA, _ := rgbS.FrameAge()
			_, irAgeB, _ := irS.FrameAge()
			if irAgeB > irAgeA {
				irAgeA = irAgeB
			}
			e.stats.RGBAgeMS = float64(rgbAge.Milliseconds())
			e.stats.IRAgeMS = float64(irAgeA.Milliseconds())
			skew := irAgeA - rgbAge
			if skew < 0 {
				skew = -skew
			}
			e.stats.SkewMS = float64(skew.Milliseconds())
		}

		e.stats.IRFPS, e.stats.IRSubtype = fps, sub
		e.stats.IRErr = errStr(irS.Err())
		e.stats.DarkIR = irS.Dark() != nil
		if gray != nil {
			e.stats.IRMean = meanOf(gray)
			e.stats.LEDOn = meanOf(gray)
		}
		if grayDk != nil {
			e.stats.LEDOff = meanOf(grayDk)
		}
	}
	// 带宽/占用预警
	if e.stats.RGBFPS > 0 && e.stats.RGBFPS < 5 && e.stats.RGBErr == "" {
		if cfg.IR && cfg.Codec != "mjpg" {
			e.stats.Hint = "⚠ USB 带宽不够：RGB 掉到 " + strconv.FormatFloat(e.stats.RGBFPS, 'f', 1, 64) +
				" fps。未压缩码流只能开一路 —— 切 MJPG 或关掉 IR。"
		} else {
			e.stats.Hint = "⚠ RGB 帧率异常低，可能被其它程序占用或 USB 带宽紧张。"
		}
	}
}

func errStr(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}

// ───────────────────────── 动作 ─────────────────────────

func (e *Engine) Align() (AlignResult, error) {
	rgbS, irS, p, _ := e.Snapshot()
	if rgbS == nil || irS == nil {
		return AlignResult{}, fmt.Errorf("需要 RGB 和 IR 同时在线")
	}
	rgbImg, _, _, _, rw, rh, _ := rgbS.Snapshot()
	_, gray, _, _, iw, ih, _ := irS.Snapshot()
	if rgbImg == nil || gray == nil {
		return AlignResult{}, fmt.Errorf("还没有帧")
	}
	// 取紧凑的 Y 平面
	y := make([]byte, rw*rh)
	for row := 0; row < rh; row++ {
		copy(y[row*rw:], rgbImg.Y[row*rgbImg.YStride:row*rgbImg.YStride+rw])
	}
	res := AutoAlign(y, rw, rh, gray, iw, ih, p.Scale, p.Tx, p.Ty)
	if !res.OK {
		return res, fmt.Errorf("自动标定失败（画面太暗或纹理不足）")
	}
	e.SetParams(func(pp *Params) {
		pp.Scale, pp.Tx, pp.Ty = res.Scale, res.Tx, res.Ty
	})
	return res, nil
}

func (e *Engine) CalibrateDark(target string, frames int) (map[string]any, error) {
	out := map[string]any{}
	rgbS, irS, _, _ := e.Snapshot()
	if target == "rgb" || target == "both" {
		if rgbS == nil {
			return nil, fmt.Errorf("RGB 相机不在线")
		}
		d, err := rgbS.CalibrateDark(frames, "rgb")
		if err != nil {
			return nil, err
		}
		rgbS.SetDark(d)
		e.events.add("calib", "暗场校准完成（RGB）")
			out["rgb"] = map[string]any{"mean": round2(d.Mean), "hot_pixels": d.Hot,
			"size": fmt.Sprintf("%dx%d", d.W, d.H), "saved": filepath.Join("calib", "dark_rgb.gob"),
			"enabled_now": true, "auto_load": true}
	}
	if target == "ir" || target == "both" {
		if irS == nil {
			return nil, fmt.Errorf("IR 相机不在线")
		}
		d, err := irS.CalibrateDark(frames, "ir")
		if err != nil {
			return nil, err
		}
		irS.SetDark(d)
		out["ir"] = map[string]any{"mean": round2(d.Mean), "hot_pixels": d.Hot,
			"size": fmt.Sprintf("%dx%d", d.W, d.H), "saved": filepath.Join("calib", "dark_ir.gob"),
			"enabled_now": true, "auto_load": true}
	}
	return out, nil
}

func (e *Engine) ClearDark() {
	rgbS, irS, _, _ := e.Snapshot()
	if rgbS != nil {
		rgbS.SetDark(nil)
	}
	if irS != nil {
		irS.SetDark(nil)
	}
	for _, tag := range []string{"rgb", "ir"} {
		_ = os.Remove(filepath.Join("calib", "dark_"+tag+".gob"))
	}
}

func (e *Engine) Save() map[string]string {
	out := map[string]string{}
	_ = os.MkdirAll("captures", 0o755)
	ts := time.Now().Format("20060102_150405")
	if img, err := e.renderMosaic(); err == nil {
		p := filepath.Join("captures", "view_"+ts+".jpg")
		if f, err := os.Create(p); err == nil {
			_ = jpeg.Encode(f, img, &jpeg.Options{Quality: 95})
			f.Close()
			out["view"] = p
		}
	}
	rgbS, irS, _, _ := e.Snapshot()
	if rgbS != nil {
		if img, _, _, _, _, _, _ := rgbS.Snapshot(); img != nil {
			p := filepath.Join("captures", "rgb_"+ts+".jpg")
			if f, err := os.Create(p); err == nil {
				_ = jpeg.Encode(f, img, &jpeg.Options{Quality: 95})
				f.Close()
				out["rgb"] = p
			}
		}
	}
	if irS != nil {
		if _, gray, _, _, w, h, _ := irS.Snapshot(); gray != nil {
			p := filepath.Join("captures", "ir_"+ts+".jpg")
			g := image.NewGray(image.Rect(0, 0, w, h))
			copy(g.Pix, gray)
			if f, err := os.Create(p); err == nil {
				_ = jpeg.Encode(f, g, &jpeg.Options{Quality: 95})
				f.Close()
				out["ir"] = p
			}
		}
	}
	return out
}

func round2(v float64) float64 { return float64(int(v*100+0.5)) / 100 }
