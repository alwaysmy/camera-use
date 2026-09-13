// person.go — 轻量"人物存在 / 位置"检测：IR 差分 + 运动检测（无模型、零依赖）
//
// 为什么不用模型：这一层的目标是"人在不在画面里、大概在哪块"，不需要骨架。
// 两个免费又很强的信号：
//
//	① IR 差分 = 补光灯亮帧 − 灭帧，得到"只被红外照亮的画面"。皮肤/近处物体反射强，
//	   所以人一靠近，差分图里人物的能量就明显高于背景。
//	② 运动检测 = 相邻帧灰度差。人在动就有大面积活跃像素，且能定位。
//
// 两者互补：静止的人靠 ①(IR)，动的人靠 ②(运动)，合起来比单用任何一个稳。
// 输出是**网格级**的（8×6 块），既能给"人在哪"，也足够便宜（1/4 分辨率，5fps）。
package main

import (
	"fmt"
	"math"
	"sync"
)

const (
	pGridX = 8 // 网格列数
	pGridY = 6 // 网格行数
)

// PersonState —— 检测结果（坐标是**原始帧**坐标，可直接画框）
type PersonState struct {
	Present  bool    `json:"present"`
	Score    float64 `json:"score"`    // 综合活跃度 0..1
	Coverage float64 `json:"coverage"` // 活跃格子占比 0..1
	Box      FaceRect `json:"box"`     // 人物包围盒（可能是多个格子拼出来的）
	Zone     string  `json:"zone"`     // 左 / 中 / 右
	Motion   float64 `json:"motion"`   // 运动分量
	IRDiff   float64 `json:"ir_diff"`  // IR 差分分量
	Cells    []int   `json:"-"`        // 活跃格子索引（调试用）
}

type personTracker struct {
	mu       sync.Mutex
	prev     []byte // 上一帧灰度（下采样后）
	ow, oh   int    // 原图尺寸（换算坐标用）
	w, h     int
	onCount  int // 连续"有"的帧数
	offCount int // 连续"无"的帧数
	state    PersonState
}

func newPersonTracker() *personTracker { return &personTracker{} }

const (
	pOnFrames  = 2  // 连续 2 帧判定"有人"（抗单帧噪声）
	pOffFrames = 8  // 连续 8 帧没动静才判"没人"（~1.6s，避免呼吸/微动就闪断）
	pMotionTh  = 10 // 单像素运动阈值（0..255）
	pCellActTh = 0.05
	pIRCellTh  = 0.40
)

// update —— 每 tick 调一次。
//
//	gray: 当前 RGB 的 Y 平面（下采样后的下采样图），gw×gh
//	irdf: IR 差分图（亮-灭，下采样后），iw×ih；没有 IR 时可传 nil
func (p *personTracker) update(gray []byte, gw, gh int, irdf []byte, iw, ih, ow, oh int) PersonState {
	p.mu.Lock()
	defer p.mu.Unlock()

	cellsX, cellsY := pGridX, pGridY
	// 每格覆盖的像素范围
	gcw, gch := float64(gw)/float64(cellsX), float64(gh)/float64(cellsY)
	icw, ich := float64(iw)/float64(cellsX), float64(ih)/float64(cellsY)

	motionCell := make([]float64, cellsX*cellsY)
	totalMotion := 0.0
	totalPix := 0.0
	if p.prev != nil && len(p.prev) == len(gray) {
		for y := 0; y < gh; y++ {
			cy := y * cellsY / gh
			for x := 0; x < gw; x++ {
				d := int(gray[y*gw+x]) - int(p.prev[y*gw+x])
				if d < 0 {
					d = -d
				}
				if d > pMotionTh {
					motionCell[cy*cellsX+x*cellsX/gw]++
					totalMotion++
				}
				totalPix++
			}
		}
	}
	// 归一化成"该格内活跃像素占比"
	for cy := 0; cy < cellsY; cy++ {
		for cx := 0; cx < cellsX; cx++ {
			if gcw > 0 && gch > 0 {
				motionCell[cy*cellsX+cx] /= gcw * gch
			}
		}
	}

	irCell := make([]float64, cellsX*cellsY)
	irMean := 0.0
	if len(irdf) > 0 && iw > 0 && ih > 0 {
		// IR 差分图里"接近"的部分远高于背景 → 用均值×系数做自适应阈值
		s := 0.0
		for _, v := range irdf {
			s += float64(v)
		}
		irMean = s / float64(len(irdf))
		th := irMean * 1.8
		if th < 12 {
			th = 12
		}
		for y := 0; y < ih; y++ {
			cy := y * cellsY / ih
			for x := 0; x < iw; x++ {
				if float64(irdf[y*iw+x]) > th {
					irCell[cy*cellsX+x*cellsX/iw]++
				}
			}
		}
		for cy := 0; cy < cellsY; cy++ {
			for cx := 0; cx < cellsX; cx++ {
				if icw > 0 && ich > 0 {
					irCell[cy*cellsX+cx] /= icw * ich
				}
			}
		}
	}

	// 合并：任一信号够强就算活跃
	active := make([]int, 0, cellsX*cellsY)
	var sx, sy, n int
	for cy := 0; cy < cellsY; cy++ {
		for cx := 0; cx < cellsX; cx++ {
			i := cy*cellsX + cx
			if motionCell[i] >= pCellActTh || irCell[i] >= pIRCellTh {
				active = append(active, i)
				sx += cx
				sy += cy
				n++
			}
		}
	}

	st := PersonState{Cells: active}
	if totalPix > 0 {
		st.Motion = totalMotion / totalPix
	}
	if irMean > 0 {
		st.IRDiff = irMean / 255.0
	}
	st.Coverage = float64(n) / float64(cellsX*cellsY)
	// 综合分：运动占比(放大)与 IR 能量(放大)取大者，再叠加覆盖度
	ms := st.Motion * 12
	if ms > 1 {
		ms = 1
	}
	irs := st.IRDiff * 8
	if irs > 1 {
		irs = 1
	}
	st.Score = math.Max(ms, irs)*0.6 + st.Coverage*0.4

	// 判定 + 去抖
	if n >= 2 && st.Score > 0.12 {
		p.onCount++
		p.offCount = 0
	} else {
		p.offCount++
		p.onCount = 0
	}
	st.Present = p.onCount >= pOnFrames || (p.state.Present && p.offCount < pOffFrames)

	// 包围盒：只取活跃格子的极值（避免把整幅算进去）
	if n > 0 {
		minX, maxX := cellsX, 0
		minY, maxY := cellsY, 0
		for _, i := range active {
			cx, cy := i%cellsX, i/cellsX
			if cx < minX {
				minX = cx
			}
			if cx > maxX {
				maxX = cx
			}
			if cy < minY {
				minY = cy
			}
			if cy > maxY {
				maxY = cy
			}
		}
		// 格坐标 → 原图坐标
		st.Box = FaceRect{
			X: minX * ow / cellsX,
			Y: minY * oh / cellsY,
			W: (maxX - minX + 1) * ow / cellsX,
			H: (maxY - minY + 1) * oh / cellsY,
		}
		cxm := float64(sx) / float64(n)
		switch {
		case cxm < float64(cellsX)/3:
			st.Zone = "左"
		case cxm > float64(cellsX)*2/3:
			st.Zone = "右"
		default:
			st.Zone = "中"
		}
	}

	p.prev = append(p.prev[:0], gray...)
	p.w, p.h = gw, gh
	p.ow, p.oh = ow, oh
	p.state = st
	return st
}

func (s PersonState) String() string {
	return fmt.Sprintf("person[p=%v score=%.2f cov=%.2f zone=%s motion=%.3f irdiff=%.3f]",
		s.Present, s.Score, s.Coverage, s.Zone, s.Motion, s.IRDiff)
}
