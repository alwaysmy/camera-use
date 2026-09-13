// camera.go — 原生相机会话：一台相机一个 goroutine（COM 套间绑线程）
//
// 外部（HTTP 处理函数）通过 `Do()` 把闭包投递到相机线程执行 —— COM 接口对象
// 只在创建它的线程上调用，避免跨套间崩溃。采集与命令共用同一个循环，
// 所以不会有并发访问同一个 IMFSourceReader 的问题。
package main

import (
	"context"
	"encoding/gob"
	"fmt"
	"image"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"time"
)

type ExposureState struct {
	Value     int32 `json:"value"`
	Flags     int32 `json:"flags"` // 1=Auto 2=Manual
	Min       int32 `json:"min"`
	Max       int32 `json:"max"`
	Step      int32 `json:"step"`
	Default   int32 `json:"default"`
	Caps      int32 `json:"caps"`
	Supported bool  `json:"supported"`
}

type CameraSession struct {
	Info  DeviceInfo
	IsIR  bool
	Kind  string

	mu      sync.RWMutex
	ycbcr   *image.YCbCr // RGB 帧（4:2:0）
	gray    []byte       // IR 补光灯亮帧
	grayDk  []byte       // IR 补光灯灭帧
	grayTS  time.Time    // 亮帧时间戳
	darkTS  time.Time    // 灭帧时间戳
	frameTS time.Time    // RGB 帧时间戳
	seq     atomic.Int64
	fps     float64
	lastErr error
	subtype string
	w, h    int
	frames  int64

	dark *DarkField
	lastRaw []byte // 最近一帧的**原始载荷**（调试用：验证编解码是否正确）
	lastRawSub string

	// wake —— "有新帧"信号，容量 1：满了就丢弃（丢的是重复通知，不是帧）
	// 采集线程只管往里投，渲染/测量方 select 等待，两边都不轮询。
	wake chan struct{}
	// OnFrame —— 每出一帧回调一次（引擎用它聚合出统一的唤醒源）
	OnFrame func()

	cmds chan func(*MFCamera)
	quit chan struct{}
}

func NewCameraSession(info DeviceInfo, isIR bool) *CameraSession {
	return &CameraSession{
		Info: info, IsIR: isIR, Kind: info.Kind,
		wake: make(chan struct{}, 1),
		cmds: make(chan func(*MFCamera), 8),
		quit: make(chan struct{}),
	}
}

// signal —— 非阻塞投递"有新帧"
func (c *CameraSession) signal() {
	select {
	case c.wake <- struct{}{}:
	default:
	}
	if c.OnFrame != nil {
		c.OnFrame()
	}
}

// WaitFrame —— 阻塞等到"帧号 != last"或 ctx 结束。返回 (当前帧号, 是否等到新帧)。
func (c *CameraSession) WaitFrame(ctx context.Context, last int64) (int64, bool) {
	for {
		if s := c.seq.Load(); s != last {
			return s, true
		}
		select {
		case <-c.wake:
		case <-ctx.Done():
			return c.seq.Load(), false
		}
	}
}

// Do —— 把 fn 投递到相机线程执行并等结果
func (c *CameraSession) Do(fn func(cam *MFCamera) error, timeout time.Duration) error {
	done := make(chan error, 1)
	select {
	case c.cmds <- func(cam *MFCamera) { done <- fn(cam) }:
	case <-c.quit:
		return fmt.Errorf("相机已关闭")
	case <-time.After(timeout):
		return fmt.Errorf("相机命令队列超时")
	}
	select {
	case err := <-done:
		return err
	case <-time.After(timeout):
		return fmt.Errorf("相机命令执行超时")
	}
}

func (c *CameraSession) Stop() { close(c.quit) }

// Run —— 相机线程主循环
func (c *CameraSession) Run(prefer []string, w, h int, codec string) {
	if err := lockCOMThread(); err != nil {
		c.setErr(err)
		return
	}
	defer unlockCOMThread() // COM 套间必须清干净再还线程
	defer mfShutdown()

	cam, err := OpenCameraByLink(c.Info.SymbolicLink)
	if err != nil {
		c.setErr(fmt.Errorf("打开 %s 失败: %w", c.Info.Name, err))
		return
	}
	defer cam.Close()

	if _, err := cam.SetBestFormat(prefer, w, h); err != nil {
		c.setErr(err)
		return
	}
	c.mu.Lock()
	c.subtype, c.w, c.h = cam.Subtype, cam.W, cam.H
	c.mu.Unlock()

	// 默认切自动曝光（这就是"画面发暗"的根治手段），IR 相机不支持就算了
	if cc := cam.CameraControl(); cc != 0 {
		if _, ok := ctrlGetRange(cc, CtrlExposure); ok {
			_ = ctrlSet(cc, CtrlExposure, 0, CtrlFlagAuto)
		}
		comRelease(cc)
	}

	var irMeans []float64
	nWin, tWin := 0, time.Now()
	for {
		// 命令优先：非阻塞地取一条命令执行，然后立刻回去读帧
		select {
		case <-c.quit:
			return
		case fn := <-c.cmds:
			fn(cam)
			continue
		default:
		}

		data, err := cam.GrabFrame()
		if err != nil {
			c.setErr(err)
			// 出错后短暂退避，用定时器而非忙等
			t := time.NewTimer(80 * time.Millisecond)
			select {
			case <-t.C:
			case <-c.quit:
				t.Stop()
				return
			case fn := <-c.cmds:
				t.Stop()
				fn(cam)
			}
			continue
		}
		if data == nil {
			continue
		}
		c.mu.Lock()
		c.lastRaw = data
		c.lastRawSub = cam.Subtype
		c.mu.Unlock()
		if c.IsIR {
			g, err := decodeGray(data, cam.Subtype, cam.W, cam.H)
			if err != nil {
				c.setErr(err)
				continue
			}
			if c.dark != nil {
				g = c.dark.Apply(g)
			}
			// 相位判定 + **只保留最近**的亮/灭帧。
			//
			// 之前是从最近 8 帧里挑"均值最大"当亮帧 —— 最坏会拿到 266ms 前的帧，
			// 人一动融合图就是重影（用户实测反馈"移动有伪影"就是这个）。
			// 现在用滑动中位数判相位，每来一帧就就地更新，最坏延迟只有 2 帧（66ms）。
			m := meanOf(g)
			irMeans = append(irMeans, m)
			if len(irMeans) > 8 {
				irMeans = irMeans[1:]
			}
			med := medianOf(irMeans)
			now := time.Now()
			c.mu.Lock()
			if m >= med {
				c.gray = g
				c.grayTS = now
			} else {
				c.grayDk = g
				c.darkTS = now
			}
			if c.gray == nil {
				c.gray = g
				c.grayTS = now
			}
			if c.grayDk == nil {
				c.grayDk = g
				c.darkTS = now
			}
			c.mu.Unlock()
		} else {
			img, err := decodePayload(data, cam.Subtype, cam.W, cam.H)
			if err != nil {
				c.setErr(err)
				continue
			}
			if c.dark != nil {
				img = c.dark.ApplyYCbCr(img)
			}
			c.mu.Lock()
			c.ycbcr = img
			c.frameTS = time.Now()
			c.mu.Unlock()
		}
		c.frames++
		c.seq.Add(1)
		c.signal()
		nWin++
		if d := time.Since(tWin); d >= time.Second {
			c.mu.Lock()
			c.fps = float64(nWin) / d.Seconds()
			c.mu.Unlock()
			nWin, tWin = 0, time.Now()
		}
	}
}

// pickPhases —— 按亮度把最近几帧分成"补光灯亮/灭"两组。
// 注意：一定要拿**不同相位**的两帧做差分，取两张亮帧相减只会得到噪点/边缘。
func pickPhases(buf [][]byte) (bright, dark []byte) {
	if len(buf) == 0 {
		return nil, nil
	}
	bi, di := 0, 0
	for i := range buf {
		if meanOf(buf[i]) > meanOf(buf[bi]) {
			bi = i
		}
		if meanOf(buf[i]) < meanOf(buf[di]) {
			di = i
		}
	}
	bright, dark = buf[bi], buf[di]
	if meanOf(bright)-meanOf(dark) < 5 {
		return bright, nil // 没有明显相位差（可能不是交替补光的机型）
	}
	return bright, dark
}

func (c *CameraSession) setErr(err error) {
	c.mu.Lock()
	c.lastErr = err
	c.mu.Unlock()
}

func (c *CameraSession) Err() error {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.lastErr
}

// Snapshot —— 取当前帧（引用即取即用，帧是只读的）
func (c *CameraSession) Snapshot() (*image.YCbCr, []byte, []byte, string, int, int, float64) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.ycbcr, c.gray, c.grayDk, c.subtype, c.w, c.h, c.fps
}

func (c *CameraSession) Seq() int64 { return c.seq.Load() }

// RawPayload —— 最近一帧的原始字节与格式（调试用）
func (c *CameraSession) RawPayload() ([]byte, string, int, int) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.lastRaw, c.lastRawSub, c.w, c.h
}

// FrameAge —— 当前帧距现在多久（用来诊断 RGB/IR 的时间错配，也就是"移动伪影"的量化指标）
func (c *CameraSession) FrameAge() (rgb, bright, dark time.Duration) {
c.mu.RLock()
defer c.mu.RUnlock()
now := time.Now()
if !c.frameTS.IsZero() {
rgb = now.Sub(c.frameTS)
}
if !c.grayTS.IsZero() {
bright = now.Sub(c.grayTS)
}
if !c.darkTS.IsZero() {
dark = now.Sub(c.darkTS)
}
return
}

// medianOf —— 滑动中位数（判补光灯相位用）
func medianOf(v []float64) float64 {
if len(v) == 0 {
return 0
}
b := append([]float64(nil), v...)
for i := 1; i < len(b); i++ {
for j := i; j > 0 && b[j] < b[j-1]; j-- {
b[j], b[j-1] = b[j-1], b[j]
}
}
return b[len(b)/2]
}

// ───────────────────────── 曝光控制 ─────────────────────────

func (c *CameraSession) Exposure() (ExposureState, error) {
	var st ExposureState
	err := c.Do(func(cam *MFCamera) error {
		cc := cam.CameraControl()
		if cc == 0 {
			return nil
		}
		defer comRelease(cc)
		rng, ok := ctrlGetRange(cc, CtrlExposure)
		if !ok {
			return nil
		}
		st.Supported = true
		st.Min, st.Max, st.Step, st.Default, st.Caps = rng.Min, rng.Max, rng.Step, rng.Default, rng.Caps
		if v, f, ok := ctrlGet(cc, CtrlExposure); ok {
			st.Value, st.Flags = v, f
		}
		return nil
	}, 3*time.Second)
	return st, err
}

func (c *CameraSession) SetExposure(value int32, auto bool) error {
	return c.Do(func(cam *MFCamera) error {
		cc := cam.CameraControl()
		if cc == 0 {
			return fmt.Errorf("该相机不支持 IAMCameraControl")
		}
		defer comRelease(cc)
		if _, ok := ctrlGetRange(cc, CtrlExposure); !ok {
			return fmt.Errorf("该相机不支持曝光控制")
		}
		flags := int32(CtrlFlagManual)
		if auto {
			value, flags = 0, CtrlFlagAuto
		}
		return ctrlSet(cc, CtrlExposure, value, flags)
	}, 3*time.Second)
}

// SetVideoProcAmp —— 亮度/对比度/增益等（IAMVideoProcAmp）
func (c *CameraSession) SetVideoProcAmp(prop int32, value int32, auto bool) error {
	return c.Do(func(cam *MFCamera) error {
		vp := cam.VideoProcAmp()
		if vp == 0 {
			return fmt.Errorf("该相机不支持 IAMVideoProcAmp")
		}
		defer comRelease(vp)
		flags := int32(CtrlFlagManual)
		if auto {
			flags = CtrlFlagAuto
		}
		return ctrlSet(vp, prop, value, flags)
	}, 3*time.Second)
}

// ───────────────────────── 暗场校准 ─────────────────────────

// DarkField —— 遮住镜头拍出来的暗场：扣掉固定图案噪声(FPN)、读出偏置和热噪点
type DarkField struct {
	Gray []byte `json:"-"`
	W    int    `json:"w"`
	H    int    `json:"h"`
	Mean float64 `json:"mean"`
	Hot  int    `json:"hot_pixels"`
	TS   string `json:"ts"`
	IsIR bool   `json:"is_ir"`
}

const darkMaxMean = 32.0

// Apply —— 灰度帧扣暗场
func (d *DarkField) Apply(g []byte) []byte {
	if d == nil || len(g) != len(d.Gray) {
		return g
	}
	out := make([]byte, len(g))
	for i := range g {
		v := int(g[i]) - int(d.Gray[i])
		if v < 0 {
			v = 0
		}
		out[i] = uint8(v)
	}
	return out
}

// ApplyYCbCr —— 只扣 Y 平面（色度不动）
func (d *DarkField) ApplyYCbCr(img *image.YCbCr) *image.YCbCr {
	if d == nil || img == nil {
		return img
	}
	w, h := img.Rect.Dx(), img.Rect.Dy()
	if w != d.W || h != d.H {
		return img
	}
	out := cloneYCbCr(img)
	for y := 0; y < h; y++ {
		yo := y * out.YStride
		for x := 0; x < w; x++ {
			v := int(out.Y[yo+x]) - int(d.Gray[y*w+x])
			if v < 0 {
				v = 0
			}
			out.Y[yo+x] = uint8(v)
		}
	}
	return out
}

func (d *DarkField) save(tag string) error {
	_ = os.MkdirAll("calib", 0o755)
	f, err := os.Create(filepath.Join("calib", "dark_"+tag+".gob"))
	if err != nil {
		return err
	}
	defer f.Close()
	return gob.NewEncoder(f).Encode(d)
}

func loadDark(tag string) *DarkField {
	f, err := os.Open(filepath.Join("calib", "dark_"+tag+".gob"))
	if err != nil {
		return nil
	}
	defer f.Close()
	var d DarkField
	if gob.NewDecoder(f).Decode(&d) != nil {
		return nil
	}
	return &d
}

// CalibrateDark —— 采 frames 帧求像素中值当暗场。镜头没遮住就直接报错。
func (c *CameraSession) CalibrateDark(frames int, tag string) (*DarkField, error) {
	if frames <= 0 {
		frames = 30
	}
	var samples [][]byte
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	lastSeq := int64(-1)
	for len(samples) < frames {
		seq, ok := c.WaitFrame(ctx, lastSeq)
		if !ok {
			break // 超时或相机停了
		}
		lastSeq = seq
		img, gray, _, _, _, _, _ := c.Snapshot()
		var g []byte
		if c.IsIR {
			g = gray
		} else if img != nil {
			w, h := img.Rect.Dx(), img.Rect.Dy()
			compact := make([]byte, w*h)
			for y := 0; y < h; y++ {
				copy(compact[y*w:], img.Y[y*img.YStride:y*img.YStride+w])
			}
			g = compact
		}
		if g != nil {
			samples = append(samples, g)
		}
	}
	if len(samples) < 3 {
		return nil, fmt.Errorf("没采到帧（相机未出图？）")
	}
	n := len(samples[0])
	median := make([]byte, n)
	buf := make([]byte, len(samples))
	var sum float64
	for i := 0; i < n; i++ {
		for j, s := range samples {
			if i < len(s) {
				buf[j] = s[i]
			}
		}
		// 插入排序求中值（样本数很少，够用）
		m := insertionMedian(buf[:len(samples)])
		median[i] = m
		sum += float64(m)
	}
	mean := sum / float64(n)
	limit := darkMaxMean
	if c.IsIR {
		limit = 45 // IR 暗帧基线本来就偏高
	}
	if mean > limit {
		return nil, fmt.Errorf("画面平均亮度 %.1f > %.0f，镜头似乎没遮住 —— 请用不透光物体完全盖住镜头再校准", mean, limit)
	}
	img, _, _, _, w, h, _ := c.Snapshot()
	if img != nil {
		w, h = img.Rect.Dx(), img.Rect.Dy()
	}
	hot := 0
	var vs float64
	for _, v := range median {
		vs += (float64(v) - mean) * (float64(v) - mean)
	}
	std := 0.0
	if n > 1 {
		std = sqrt(vs / float64(n))
	}
	for _, v := range median {
		if float64(v) > mean+6*maxf(1, std) {
			hot++
		}
	}
	df := &DarkField{Gray: median, W: w, H: h, Mean: mean, Hot: hot,
		TS: time.Now().Format("2006-01-02 15:04:05"), IsIR: c.IsIR}
	if err := df.save(tag); err != nil {
		return df, fmt.Errorf("暗场已生成但保存失败: %w", err)
	}
	return df, nil
}

// VideoProcAmpGet —— 读任意画质控制：
// 0亮度 1对比度 2色相 3饱和度 4锐度 5Gamma 6彩色开关 7白平衡 8背光补偿 9增益
func (c *CameraSession) VideoProcAmpGet(prop int32) (int32, int32, bool) {
	var v, f int32
	var ok_ bool
	_ = c.Do(func(cam *MFCamera) error {
		iface := cam.VideoProcAmp()
		if iface == 0 {
			return nil
		}
		defer comRelease(iface)
		vv, ff, o := ctrlGet(iface, prop)
		v, f, ok_ = vv, ff, o
		return nil
	}, 3*time.Second)
	return v, f, ok_
}

// VideoProcAmpSet —— 写任意画质控制（flags: 1=Auto 2=Manual）
func (c *CameraSession) VideoProcAmpSet(prop, val, flags int32) error {
	return c.Do(func(cam *MFCamera) error {
		iface := cam.VideoProcAmp()
		if iface == 0 {
			return fmt.Errorf("相机不支持 IAMVideoProcAmp")
		}
		defer comRelease(iface)
		return ctrlSet(iface, prop, val, flags)
	}, 3*time.Second)
}

// VideoProcAmpRange —— 读取值范围
func (c *CameraSession) VideoProcAmpRange(prop int32) (ControlRange, bool) {
	var cr ControlRange
	var ok_ bool
	_ = c.Do(func(cam *MFCamera) error {
		iface := cam.VideoProcAmp()
		if iface == 0 {
			return nil
		}
		defer comRelease(iface)
		r, o := ctrlGetRange(iface, prop)
		cr, ok_ = r, o
		return nil
	}, 3*time.Second)
	return cr, ok_
}

// WhiteBalanceRange —— 读相机白平衡能力（色温 Kelvin，caps 含 Auto/Manual）
func (c *CameraSession) WhiteBalanceRange() (ControlRange, bool) {
	var cr ControlRange
	var ok_ bool
	_ = c.Do(func(cam *MFCamera) error {
		iface := cam.VideoProcAmp()
		if iface == 0 {
			return nil
		}
		defer comRelease(iface)
		r, o := ctrlGetRange(iface, 7) // 7 = VideoProcAmp_WhiteBalance
		cr, ok_ = r, o
		return nil
	}, 3*time.Second)
	return cr, ok_
}

// WhiteBalance —— 读当前值、标志（1=自动 2=手动）
func (c *CameraSession) WhiteBalance() (int32, int32, bool) {
	var v, f int32
	var ok_ bool
	_ = c.Do(func(cam *MFCamera) error {
		iface := cam.VideoProcAmp()
		if iface == 0 {
			return nil
		}
		defer comRelease(iface)
		vv, ff, o := ctrlGet(iface, 7)
		v, f, ok_ = vv, ff, o
		return nil
	}, 3*time.Second)
	return v, f, ok_
}

// SetWhiteBalance —— 直接命令相机白平衡（value<0 且 auto=true 时只切模式）
func (c *CameraSession) SetWhiteBalance(value int32, auto bool) error {
	return c.Do(func(cam *MFCamera) error {
		iface := cam.VideoProcAmp()
		if iface == 0 {
			return fmt.Errorf("相机不支持 IAMVideoProcAmp")
		}
		defer comRelease(iface)
		flags := int32(2)
		if auto {
			flags = 1
		}
		if value >= 0 {
			if err := ctrlSet(iface, 7, value, flags); err != nil {
				return fmt.Errorf("设置白平衡 %dK 失败: %w", value, err)
			}
		} else {
			v, _, ok := ctrlGet(iface, 7)
			if !ok {
				return fmt.Errorf("读白平衡失败")
			}
			if err := ctrlSet(iface, 7, v, flags); err != nil {
				return fmt.Errorf("切换白平衡模式失败: %w", err)
			}
		}
		return nil
	}, 3*time.Second)
}

func (c *CameraSession) SetDark(d *DarkField) {
	c.mu.Lock()
	c.dark = d
	c.mu.Unlock()
}

func (c *CameraSession) Dark() *DarkField {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.dark
}

func insertionMedian(b []byte) byte {
	for i := 1; i < len(b); i++ {
		for j := i; j > 0 && b[j] < b[j-1]; j-- {
			b[j], b[j-1] = b[j-1], b[j]
		}
	}
	if len(b) == 0 {
		return 0
	}
	return b[len(b)/2]
}

func sqrt(x float64) float64 {
	if x <= 0 {
		return 0
	}
	z := x
	for i := 0; i < 32; i++ {
		z = (z + x/z) / 2
	}
	return z
}

func maxf(a, b float64) float64 {
	if a > b {
		return a
	}
	return b
}
