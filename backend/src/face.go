// face.go — 原生 Viola-Jones 人脸检测（零依赖）
//
// 为什么自己写而不是调 OpenCV：主程序要保持"离线可构建、零外部依赖"。
// 而 OpenCV 装的 haarcascade XML（908KB）其实就是一个**数据文件**，
// 把它拷进项目 + 用 Go 标准库的 encoding/xml 解析 + 自己实现积分图与级联判定，
// 就能得到同样的人脸检测能力（本机实测可用）。
//
// 用途：让"人脸区域"参与成像优化 —— 人脸区域测白平衡比全帧灰世界准得多。
package main

import (
	"encoding/xml"
	"math"
	"fmt"
	"os"
	"strconv"
	"strings"
	"sync"
)

// ── XML 解析（OpenCV haarcascade 格式：数字全塞在元素文本里，要自己拆）──

type FloatList []float64

func (l *FloatList) UnmarshalXML(d *xml.Decoder, start xml.StartElement) error {
	var s string
	if err := d.DecodeElement(&s, &start); err != nil {
		return err
	}
	for _, f := range strings.Fields(s) {
		v, err := strconv.ParseFloat(f, 64)
		if err == nil {
			*l = append(*l, v)
		}
	}
	return nil
}

type xmlWeak struct {
	InternalNodes FloatList `xml:"internalNodes"`
	LeafValues    FloatList `xml:"leafValues"`
}

type xmlStage struct {
	Threshold float64 `xml:"stageThreshold"`
	Weaks     []xmlWeak `xml:"weakClassifiers>_"`
}

type xmlFeature struct {
	Rects []FloatList `xml:"rects>_"`
}

type xmlCascade struct {
	XMLName xml.Name `xml:"opencv_storage"`
	Cascade struct {
		StageNum int          `xml:"stageNum"`
		Width    int          `xml:"width"`
		Height   int          `xml:"height"`
		Stages   []xmlStage   `xml:"stages>_"`
		Features []xmlFeature `xml:"features>_"`
	} `xml:"cascade"`
}

// ── 运行时结构 ──

type haarRect struct {
	x, y, w, h int
	weight     float64
}

type haarFeature struct {
	rects []haarRect
}

type stump struct {
	featureIdx int
	threshold  float64
	left       float64
	right      float64
}

type haarStage struct {
	threshold float64
	stumps    []stump
}

type Cascade struct {
	WinW, WinH int
	Stages     []haarStage
	Features   []haarFeature
}

type FaceRect struct {
	X int `json:"x"`
	Y int `json:"y"`
	W int `json:"w"`
	H int `json:"h"`
}

var (
	cascadeOnce sync.Once
	cascadeMain *Cascade
	cascadeErr  error
)

// LoadFaceCascade —— 默认加载 frontalface_default，失败再试 alt2
func LoadFaceCascade(paths ...string) (*Cascade, error) {
	if len(paths) == 0 {
		paths = []string{
			P("models", "haarcascade_frontalface_default.xml"),
			P("models", "haarcascade_frontalface_alt2.xml"),
		}
	}
	var lastErr error
	for _, p := range paths {
		c, err := parseCascade(p)
		if err == nil {
			return c, nil
		}
		lastErr = err
	}
	return nil, lastErr
}

func parseCascade(path string) (*Cascade, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	var doc xmlCascade
	if err := xml.NewDecoder(f).Decode(&doc); err != nil {
		return nil, fmt.Errorf("解析 %s 失败: %w", path, err)
	}
	c := &Cascade{WinW: doc.Cascade.Width, WinH: doc.Cascade.Height}
	if c.WinW == 0 || c.WinH == 0 {
		c.WinW, c.WinH = 24, 24
	}
	for _, xf := range doc.Cascade.Features {
		var feat haarFeature
		for _, r := range xf.Rects {
			if len(r) >= 5 {
				feat.rects = append(feat.rects, haarRect{
					x: int(r[0]), y: int(r[1]), w: int(r[2]), h: int(r[3]), weight: r[4]})
			}
		}
		c.Features = append(c.Features, feat)
	}
	for _, xs := range doc.Cascade.Stages {
		st := haarStage{threshold: xs.Threshold}
		for _, xw := range xs.Weaks {
			// internalNodes: [nodeIdx, leftChild, featureIdx, threshold]
			if len(xw.InternalNodes) < 4 || len(xw.LeafValues) < 2 {
				continue
			}
			st.stumps = append(st.stumps, stump{
				featureIdx: int(xw.InternalNodes[2]),
				threshold:  xw.InternalNodes[3],
				left:       xw.LeafValues[0],
				right:      xw.LeafValues[1],
			})
		}
		c.Stages = append(c.Stages, st)
	}
	if len(c.Stages) == 0 {
		return nil, fmt.Errorf("%s 里没有解析到 stage", path)
	}
	return c, nil
}

// ── 积分图 ──

type integral struct {
	sum  []int64 // (w+1)*(h+1)
	sq   []int64
	w, h int
}

func buildIntegral(gray []byte, w, h int) *integral {
	iw := w + 1
	it := &integral{sum: make([]int64, iw*(h+1)), sq: make([]int64, iw*(h+1)), w: w, h: h}
	for y := 0; y < h; y++ {
		var rowSum, rowSq int64
		for x := 0; x < w; x++ {
			v := int64(gray[y*w+x])
			rowSum += v
			rowSq += v * v
			it.sum[(y+1)*iw+x+1] = it.sum[y*iw+x+1] + rowSum
			it.sq[(y+1)*iw+x+1] = it.sq[y*iw+x+1] + rowSq
		}
	}
	return it
}

// rectSum —— [x, x+w) × [y, y+h) 的像素和。
// 积分图 I(x,y) = [0,x)×[0,y) 的累加和，所以区间和 = I(x2,y2)-I(x1,y2)-I(x2,y1)+I(x1,y1)
func (it *integral) rectSum(p []int64, x, y, w, h int) int64 {
	iw := it.w + 1
	if x < 0 || y < 0 || x+w > it.w || y+h > it.h {
		return 0
	}
	return p[(y+h)*iw+x+w] - p[y*iw+x+w] - p[(y+h)*iw+x] + p[y*iw+x]
}

// ── 检测 ──

// Detect —— 在灰度图里找人脸。minSize/maxSize 是窗口边长（像素）。
func (c *Cascade) Detect(gray []byte, w, h, minSize, maxSize int, scaleStep float64, minNeighbors int) []FaceRect {
	if minSize < c.WinW {
		minSize = c.WinW
	}
	if maxSize <= 0 || maxSize > min(w, h) {
		maxSize = min(w, h)
	}
	if scaleStep <= 1.0 {
		scaleStep = 1.15
	}
	it := buildIntegral(gray, w, h)
	var out []FaceRect
	for scale := float64(minSize); scale <= float64(maxSize); scale *= scaleStep {
		sw := int(scale)
		if sw < c.WinW || sw > w || sw > h {
			continue
		}
		sh := sw * c.WinH / c.WinW
		if sh > h {
			continue
		}
		step := sw / 12
		if step < 2 {
			step = 2
		}
		for y := 0; y+sh <= h; y += step {
			for x := 0; x+sw <= w; x += step {
				if c.evalWindow(it, gray, x, y, sw, sh) {
					out = append(out, FaceRect{X: x, Y: y, W: sw, H: sh})
				}
			}
		}
	}
	return groupFaces(out, minNeighbors)
}

// evalWindow —— 逐 stage 判定，任何一个不过就早退（这是 Viola-Jones 快的关键）
func (c *Cascade) evalWindow(it *integral, gray []byte, x, y, sw, sh int) bool {
	a := it.rectSum(it.sum, x, y, sw, sh)
	sq := it.rectSum(it.sq, x, y, sw, sh)
	n := float64(sw * sh)
	variance := float64(sq) - float64(a)*float64(a)/n
	if variance < 1 {
		return false // 死平区域（全黑/全白）直接跳过
	}
	// 归一化因子：训练时特征按 σ_pixel×N 归一，σ_pixel = sqrt(variance/N)，
	// 所以阈值要乘 sqrt(variance*N)。**少乘这个 sqrt(N) 会让阈值小 ~N^0.5 倍**，
	// 每个弱分类器的判定退化成常数 —— 实测表现：合成图乱检出 2720 张、真脸 0 张。
	norm := math.Sqrt(variance * n)
	invW := float64(sw) / float64(c.WinW)
	invH := float64(sh) / float64(c.WinH)

	for si := range c.Stages {
		st := &c.Stages[si]
		var sum float64
		for k := range st.stumps {
			sp := &st.stumps[k]
			var fv float64
			feat := &c.Features[sp.featureIdx]
			for _, r := range feat.rects {
				rx := x + int(float64(r.x)*invW)
				ry := y + int(float64(r.y)*invH)
				rw := int(float64(r.w) * invW)
				rh := int(float64(r.h) * invH)
				if rw < 1 {
					rw = 1
				}
				if rh < 1 {
					rh = 1
				}
				if rx+rw > it.w || ry+rh > it.h {
					continue
				}
				fv += float64(it.rectSum(it.sum, rx, ry, rw, rh)) * r.weight
			}
			if fv < sp.threshold*norm {
				sum += sp.left
			} else {
				sum += sp.right
			}
		}
		if sum < st.threshold {
			return false
		}
	}
	return true
}


// DebugWindow —— 对指定窗口逐级打印判定过程（诊断用）
func (c *Cascade) DebugWindow(gray []byte, w, h, x, y, sw, sh int) {
it := buildIntegral(gray, w, h)
a := it.rectSum(it.sum, x, y, sw, sh)
sq := it.rectSum(it.sq, x, y, sw, sh)
n := float64(sw * sh)
variance := float64(sq) - float64(a)*float64(a)/n
norm := math.Sqrt(variance * n)
fmt.Printf("窗口 (%d,%d %dx%d) 像素和=%d 方差=%.1f norm=%.2f\n", x, y, sw, sh, a, variance, norm)
invW := float64(sw) / float64(c.WinW)
invH := float64(sh) / float64(c.WinH)
for si := 0; si < len(c.Stages); si++ {
st := &c.Stages[si]
var sum float64
for k := range st.stumps {
sp := &st.stumps[k]
var fv float64
feat := &c.Features[sp.featureIdx]
for _, r := range feat.rects {
rx := x + int(float64(r.x)*invW)
ry := y + int(float64(r.y)*invH)
rw := maxInt(1, int(float64(r.w)*invW))
rh := maxInt(1, int(float64(r.h)*invH))
fv += float64(it.rectSum(it.sum, rx, ry, rw, rh)) * r.weight
}
if fv < sp.threshold*norm {
sum += sp.left
} else {
sum += sp.right
}
if si == 0 && k < 3 {
fmt.Printf("    stump[%d] feat=%d 原始特征=%.1f 阈值*norm=%.2f → %s (%.2f)\n",
k, sp.featureIdx, fv, sp.threshold*norm,
map[bool]string{true: "left", false: "right"}[fv < sp.threshold*norm],
map[bool]float64{true: sp.left, false: sp.right}[fv < sp.threshold*norm])
}
}
fmt.Printf("  stage[%d] sum=%.3f 阈值=%.3f → %s\n", si, sum, st.threshold,
map[bool]string{true: "过", false: "拒"}[sum >= st.threshold])
if sum < st.threshold {
return
}
}
}

// groupFaces —— 跨尺度投票分组（等价于 OpenCV 的 minNeighbors）
//
// 光合并重叠框不够：同一张脸在不同尺度上会被检出多次，而误检往往只出现一两次。
// 按"中心距离 + 尺寸比例"聚类，票数 >= minNeighbors 才认。
func groupFaces(in []FaceRect, minNeighbors int) []FaceRect {
	if len(in) == 0 {
		return nil
	}
	type cand struct {
		r FaceRect
		n int
	}
	var gs []cand
	for _, f := range in {
		cx, cy := f.X+f.W/2, f.Y+f.H/2
		placed := false
		for i := range gs {
			g := &gs[i]
			gx, gy := g.r.X+g.r.W/2, g.r.Y+g.r.H/2
			dx, dy := float64(cx-gx), float64(cy-gy)
			dist := math.Sqrt(dx*dx + dy*dy)
			sz := float64(maxInt(g.r.W, f.W))
			ratio := float64(f.W) / float64(g.r.W)
			if dist < 0.35*sz && ratio > 0.75 && ratio < 1.35 {
				n := float64(g.n)
				g.r.X = int((float64(g.r.X)*n + float64(f.X)) / (n + 1))
				g.r.Y = int((float64(g.r.Y)*n + float64(f.Y)) / (n + 1))
				g.r.W = int((float64(g.r.W)*n + float64(f.W)) / (n + 1))
				g.r.H = int((float64(g.r.H)*n + float64(f.H)) / (n + 1))
				g.n++
				placed = true
				break
			}
		}
		if !placed {
			gs = append(gs, cand{r: f, n: 1})
		}
	}
	if minNeighbors < 1 {
		minNeighbors = 1
	}
	var out []FaceRect
	for _, g := range gs {
		if g.n >= minNeighbors {
			out = append(out, g.r)
		}
	}
	return out
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}
