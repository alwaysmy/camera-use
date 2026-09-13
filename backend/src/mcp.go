// mcp.go — MCP 服务器（JSON-RPC 2.0 over stdio）：给 agent 物理世界观察能力
//
// 为什么要它：agent 平时只能读文件、跑命令，**看不见物理世界**。
// 这个 MCP 把已经做好的相机能力（原生采集 + IR 融合 + 人脸/骨架/手部检测 + 白平衡/曝光控制）
// 直接暴露成工具，agent 就能"看一眼房间""谁在不在""手比了什么手势""当时多亮"。
//
// 传输：MCP 的 stdio 传输 = 一行一个 JSON-RPC 消息（换行分隔）。
// 用 `camera_backend.exe -mcp` 启动（同时跑采集；HTTP 也照常提供）。
package main

import (
	"bufio"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"image/jpeg"
	"os"
	"strconv"
	"strings"
	"time"
)

type mcpReq struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      any             `json:"id,omitempty"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
}

type mcpContent struct {
	Type     string `json:"type"`
	Text     string `json:"text,omitempty"`
	Data     string `json:"data,omitempty"`
	MimeType string `json:"mimeType,omitempty"`
}

type mcpResult struct {
	Content []mcpContent `json:"content"`
	IsError bool         `json:"isError,omitempty"`
}

func mcpWrite(v any) {
	b, _ := json.Marshal(v)
	os.Stdout.Write(b)
	os.Stdout.Write([]byte("\n"))
}

func mcpReply(id any, result any) {
	mcpWrite(map[string]any{"jsonrpc": "2.0", "id": id, "result": result})
}

func mcpErr(id any, code int, msg string) {
	mcpWrite(map[string]any{"jsonrpc": "2.0", "id": id,
		"error": map[string]any{"code": code, "message": msg}})
}

// RunMCP —— 阻塞在 stdin 上服务 MCP 请求
func RunMCP(e *Engine) {
	// 打印相机真实状态：MCP 模式**不预开相机**（首次工具调用才懒加载）。
	// 这行既是诊断，也是"有没有误开相机"的判据 ——
	// 注意别用"另一个进程还能不能打开相机"来判断：实测本机相机允许多进程共享打开。
	rgbS, irS, _, _ := e.Snapshot()
	camState := "未打开（首次工具调用时懒加载，空闲 60s 自动释放）"
	if rgbS != nil || irS != nil {
		camState = "**已打开**（不该出现在 MCP 启动时 —— 检查懒加载是否漏了路径）"
	}
	fmt.Fprintf(os.Stderr, "[mcp] 已就绪（stdio）｜相机 %s｜把本进程接进 agent 的 MCP 配置即可\n", camState)

	// 调用日志：记录「谁、什么时候、调了什么」——
	// 没有它时只能看到副作用（摄像头被打开、进程被拉起），查不到是谁在调。
	lg := newMCPLogger()
	defer lg.close()
	fmt.Fprintf(os.Stderr, "[mcp] 调用日志: %s\n", lg.LogPath())

	sc := bufio.NewScanner(os.Stdin)
	sc.Buffer(make([]byte, 1<<20), 1<<24)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" {
			continue
		}
		var req mcpReq
		if err := json.Unmarshal([]byte(line), &req); err != nil {
			mcpErr(nil, -32700, "parse error")
			continue
		}
		switch req.Method {
		case "initialize":
			// 客户端自报身份（"被谁调用"的第一来源）
			var ci struct {
				ClientInfo struct {
					Name    string `json:"name"`
					Version string `json:"version"`
				} `json:"clientInfo"`
			}
			_ = json.Unmarshal(req.Params, &ci)
			if ci.ClientInfo.Name != "" {
				lg.client = ci.ClientInfo.Name + "/" + ci.ClientInfo.Version
			}
			lg.add("initialize", map[string]any{"client_info": ci.ClientInfo})
			mcpReply(req.ID, map[string]any{
				"protocolVersion": "2024-11-05",
				"capabilities":    map[string]any{"tools": map[string]any{}},
				"serverInfo": map[string]any{
					"name": "camera-use", "version": "1.0.0",
					"title": "物理世界观察（RGB+IR 摄像头）",
				},
				"instructions": "用 camera_look 看画面，camera_observe 拿一句话描述，" +
					"camera_detect 拿结构化检测（人脸/人形/骨架/手部手势），camera_control 调相机。",
			})
		case "notifications/initialized", "notifications/cancelled":
			// 通知无需回复
		case "ping":
			mcpReply(req.ID, map[string]any{})
		case "tools/list":
			lg.add("tools/list", nil)
			mcpReply(req.ID, map[string]any{"tools": mcpTools()})
		case "tools/call":
			var p struct {
				Name      string         `json:"name"`
				Arguments map[string]any `json:"arguments"`
			}
			json.Unmarshal(req.Params, &p)
			lg.add("tools/call", map[string]any{"tool": p.Name, "args": p.Arguments})
			res, err := mcpCall(e, p.Name, p.Arguments)
			if err != nil {
				mcpReply(req.ID, mcpResult{IsError: true,
					Content: []mcpContent{{Type: "text", Text: "错误: " + err.Error()}}})
				continue
			}
			mcpReply(req.ID, res)
		default:
			mcpErr(req.ID, -32601, "method not found: "+req.Method)
		}
	}
}

func mcpTools() []map[string]any {
	obj := func(props map[string]any, req ...string) map[string]any {
		return map[string]any{"type": "object", "properties": props, "required": req}
	}
	enum := func(vals ...string) map[string]any {
		return map[string]any{"type": "string", "enum": vals}
	}
	return []map[string]any{
		{
			"name": "camera_look",
			"description": "看眼前的画面（返回 JPEG，可直接作为视觉输入）。" +
				"view 选通道：fuse=RGB+IR融合(默认，暗光下最清楚)、rgb=可见光原图、ir=红外黑白、" +
				"edge=边缘图、diff=红外补光差分(只显示被红外照亮的东西)、ircolor=红外伪彩、" +
				"darkmap=暗场热点图、alignerr=配准误差图。",
			"inputSchema": obj(map[string]any{
				"view":      enum("fuse", "rgb", "ir", "edge", "diff", "ircolor", "darkmap", "alignerr"),
				"quality":   map[string]any{"type": "integer", "minimum": 40, "maximum": 95, "description": "JPEG 质量，默认 85"},
				"max_width": map[string]any{"type": "integer", "description": "最大宽度，过大时等比缩小，默认 800"},
			}, "view"),
		},
		{
			"name": "camera_observe",
			"description": "观察物理世界（不返回图片）。what 参数决定看哪部分：" +
				"summary=一句话概述（有没有人/在哪/多亮）；person=人物存在与位置（红外差分+运动）；" +
				"face=人脸框（原生 Viola-Jones + 旁路 YOLO，含坐标与置信度）；" +
				"pose=人形与 17 点骨架；hand=手部 21 点与五指手势；" +
				"state=相机与处理链路状态（分辨率/码流/帧率/曝光/白平衡/融合参数/暗场/推理延迟）；" +
				"events=最近的事件流（谁进来了/谁走了/手势变化/人脸增减/采集切换/暗场校准，带时间戳）；" +
				"all=以上全部（默认）。坐标单位一律为像素。",
			"inputSchema": obj(map[string]any{
				"what":   enum("all", "summary", "person", "face", "pose", "hand", "state", "events"),
				"format": enum("json", "text"),
			}),
		},
		{
			"name": "camera_control",
			"description": "控制相机与画面处理（会改变物理设备状态）。" +
				"config=分辨率/码流/IR 开关；exposure=曝光（手动值或自动）；" +
				"camera_wb=相机色温白平衡；params=融合与画质参数" +
				"（mode/weight/gain/chroma/chroma_blur/white_balance/gamma/chroma_dark/fps/quality/thumbs/mirror_h/mirror_v/face_box/person_box/yolo_face/yolo_pose/yolo_hand）。",
			"inputSchema": obj(map[string]any{
				"config":    map[string]any{"type": "object", "description": `{"codec":"nv12|mjpg|yuy2","w":640,"h":480,"ir":true}`},
				"exposure":  map[string]any{"type": "object", "description": `{"value":-3,"auto":false}`},
				"camera_wb": map[string]any{"type": "object", "description": `{"value":4600,"auto":false}`},
				"params":    map[string]any{"type": "object", "description": `{"mode":"fuse","weight":0.62,"chroma":1.25}`},
			}),
		},
	}
}

func mcpCall(e *Engine, name string, args map[string]any) (mcpResult, error) {
	switch name {
	case "camera_look":
		view, _ := args["view"].(string)
		if view == "" {
			view = "fuse"
		}
		q := 85
		if v, ok := args["quality"].(float64); ok {
			q = int(v)
		}
		img, err := e.renderView(view)
		if err != nil || img == nil {
			return mcpResult{}, fmt.Errorf("取不到画面（相机可能在切换）")
		}
		if mw, ok := args["max_width"].(float64); ok && int(mw) > 0 && img.Rect.Dx() > int(mw) {
			f := float64(img.Rect.Dx()) / mw
			img = resizeYCbCr(img, int(mw), int(float64(img.Rect.Dy())/f))
		}
		b := newByteWriter(1 << 19)
		if err := jpeg.Encode(b, img, &jpeg.Options{Quality: q}); err != nil {
			return mcpResult{}, err
		}
		st, _, cfg := e.Stats()
		txt := fmt.Sprintf("画面 %s %dx%d（%s），%s", view, img.Rect.Dx(), img.Rect.Dy(), cfg.Codec, e.mcpSummary(st))
		return mcpResult{Content: []mcpContent{
			{Type: "text", Text: txt},
			{Type: "image", Data: base64.StdEncoding.EncodeToString(b.Bytes()), MimeType: "image/jpeg"},
		}}, nil
	case "camera_observe":
		what, _ := args["what"].(string)
		format, _ := args["format"].(string)
		if what == "" {
			what = "all"
		}
		// summary / text 直接给一句话
		if what == "summary" || format == "text" {
			return mcpResult{Content: []mcpContent{{Type: "text", Text: e.mcpObserve()}}}, nil
		}
		st, p, cfg := e.Stats()
		out := map[string]any{}
		if what == "all" || what == "state" {
			out["state"] = map[string]any{
				"config": cfg, "params": p, "stats": st,
				"white_balance": e.WhiteBalanceStatus(),
			}
		}
		if what == "all" || what == "person" {
			out["person"] = e.Person()
		}
		if what == "all" || what == "face" {
			out["faces_native"] = e.Faces()
			if vr, vw, vh, ok := e.vision.get(); ok && e.vision.fresh(3*time.Second) {
				out["faces_yolo"] = vr.Faces
				out["vision_frame"] = map[string]any{"w": vw, "h": vh}
				out["vision_age_ms"] = time.Since(vr.At).Milliseconds()
			}
		}
		if what == "pose" || what == "hand" || what == "all" {
			if vr, _, _, ok := e.vision.get(); ok && e.vision.fresh(3*time.Second) {
				if what == "pose" || what == "all" {
					out["poses"] = vr.Poses
				}
				if what == "hand" || what == "all" {
					out["hands"] = vr.Hands
				}
				out["vision_ok"] = true
				out["vision_ms"] = map[string]any{"face": vr.MsFace, "pose": vr.MsPose, "hand": vr.MsHand}
			} else {
				out["vision_ok"] = false
				out["vision_hint"] = "旁路未运行：启动时加 -vision（或 python tools/vision_sidecar.py），" +
					"人脸仍可用原生的 faces_native"
			}
		}
		if what == "all" {
			out["summary"] = e.mcpObserve()
		}
		b, _ := json.MarshalIndent(out, "", " ")
		return mcpResult{Content: []mcpContent{{Type: "text", Text: string(b)}}}, nil
	case "camera_control":
		rgbS, _, _, _ := e.Snapshot()
		var done []string
		if m, ok := args["config"].(map[string]any); ok {
			codec := str(m["codec"])
			w, h := intOf(m["w"], 0), intOf(m["h"], 0)
			ir, _ := m["ir"].(bool)
			_, note, err := e.SetConfig(codec, w, h, ir)
			if err != nil {
				return mcpResult{}, err
			}
			done = append(done, "config"+note)
		}
		if m, ok := args["exposure"].(map[string]any); ok {
			if auto, ok2 := m["auto"].(bool); ok2 && auto {
				rgbS, _, _, _ := e.Snapshot()
			if rgbS == nil {
				return mcpResult{}, fmt.Errorf("RGB 相机不在线")
			}
			if err := rgbS.SetExposure(0, true); err != nil {
					return mcpResult{}, err
				}
				done = append(done, "曝光=自动")
			} else {
				if err := rgbS.SetExposure(int32(intOf(m["value"], -3)), false); err != nil {
					return mcpResult{}, err
				}
				done = append(done, "曝光=手动"+strconv.Itoa(intOf(m["value"], -3)))
			}
		}
		if m, ok := args["camera_wb"].(map[string]any); ok {
			auto, _ := m["auto"].(bool)
			v := int32(-1)
			if x, ok2 := m["value"].(float64); ok2 {
				v = int32(x)
			}
			if err := e.SetWhiteBalance(v, auto); err != nil {
				return mcpResult{}, err
			}
			done = append(done, "相机白平衡="+func() string {
				if auto {
					return "自动"
				}
				return strconv.Itoa(int(v)) + "K"
			}())
		}
		if m, ok := args["params"].(map[string]any); ok {
			keys := make([]string, 0, len(m))
			for k := range m {
				keys = append(keys, k)
			}
			e.SetParams(func(pp *Params) {
				applyParamMap(pp, m)
			})
			done = append(done, "参数:"+strings.Join(keys, ","))
		}
		if len(done) == 0 {
			return mcpResult{}, fmt.Errorf("没给要改的东西（config/exposure/camera_wb/params）")
		}
		time.Sleep(400 * time.Millisecond)
		
		st, _, _ := e.Stats()
		return mcpResult{Content: []mcpContent{
			{Type: "text", Text: "已应用 —— " + strings.Join(done, "；") + "\n" + e.mcpSummary(st)},
		}}, nil
	}
	return mcpResult{}, fmt.Errorf("未知工具: " + name)
}

func str(v any) string {
	s, _ := v.(string)
	return s
}

func intOf(v any, def int) int {
	if f, ok := v.(float64); ok {
		return int(f)
	}
	return def
}

// applyParamMap —— 把 JSON 参数写进 Params（白名单，避免乱塞）
func applyParamMap(pp *Params, m map[string]any) {
	sets := map[string]*float64{
		"weight": &pp.Weight, "gain": &pp.Gain, "chroma": &pp.Chroma,
		"white_balance": &pp.WhiteBalance, "gamma": &pp.Gamma, "chroma_dark": &pp.ChromaDark,
		"stretch": &pp.Stretch, "fps": &pp.FPS, "scale": &pp.Scale,
		"tx": &pp.Tx, "ty": &pp.Ty, "rot": &pp.Rot,
	}
	for k, dst := range sets {
		if v, ok := m[k].(float64); ok {
			*dst = v
		}
	}
	if v, ok := m["chroma_blur"].(float64); ok {
		pp.ChromaBlur = int(v)
	}
	if v, ok := m["quality"].(float64); ok {
		pp.Quality = int(v)
	}
	if v, ok := m["mode"].(string); ok {
		pp.Mode = v
	}
	for k, dst := range map[string]*bool{"thumbs": &pp.Thumbs, "mirror_h": &pp.MirrorH,
		"mirror_v": &pp.MirrorV, "face_box": &pp.FaceBox, "person_box": &pp.PersonBox,
		"yolo_face": &pp.YoloFace, "yolo_pose": &pp.YoloPose, "yolo_hand": &pp.YoloHand} {
		if v, ok := m[k].(bool); ok {
			*dst = v
		}
	}
}

// mcpSummary —— 一句话概述（给 camera_look / camera_control 附带的文字）
func (e *Engine) mcpSummary(st Stats) string {
	return fmt.Sprintf("RGB %.1ffps / IR %.1ffps，画面亮度 %.0f，%s",
		st.RGBFPS, st.IRFPS, st.RGBMean, e.personWord())
}

func (e *Engine) personWord() string {
	p := e.Person()
	if !p.Present {
		return "画面里没有人"
	}
	return fmt.Sprintf("有人在画面%s（覆盖 %.0f%%）", p.Zone, p.Coverage*100)
}

// mcpObserve —— camera_observe 的实现：把各模块状态拼成一段人话
func (e *Engine) mcpObserve() string {
	st, p, cfg := e.Stats()
	var b strings.Builder
	ps := e.Person()
	if ps.Present {
		fmt.Fprintf(&b, "有人：位置 %s，覆盖 %.0f%%，运动 %.1f%%，IR 差分 %.0f%%。",
			ps.Zone, ps.Coverage*100, ps.Motion*100, ps.IRDiff*100)
	} else {
		fmt.Fprintf(&b, "画面里没有人（运动 %.1f%%，IR 差分 %.0f%%）。", ps.Motion*100, ps.IRDiff*100)
	}
	faces := e.Faces()
	fmt.Fprintf(&b, " 原生人脸 %d 张。", len(faces))
	if vr, _, _, ok := e.vision.get(); ok && e.vision.fresh(3*time.Second) {
		fmt.Fprintf(&b, " 旁路检出 YOLO 人脸 %d、人形 %d、手 %d", len(vr.Faces), len(vr.Poses), len(vr.Hands))
		for _, h := range vr.Hands {
			fmt.Fprintf(&b, "（%s %s）", h.Hand, h.Gesture)
		}
		b.WriteString("。")
	} else {
		b.WriteString(" 旁路未运行（无 YOLO/骨架/手势）。")
	}
	fmt.Fprintf(&b, " 画质：%s %dx%d，亮度 %.0f，IR %s",
		cfg.Codec, cfg.W, cfg.H, st.RGBMean, map[bool]string{true: "开", false: "关"}[cfg.IR])
	if st.IRFPS > 0 {
		fmt.Fprintf(&b, "（%.1ffps，补光灯亮/灭 %.0f/%.0f", st.IRFPS, st.LEDOn, st.LEDOff)
		if st.LEDOn-st.LEDOff < 12 {
			b.WriteString("，差值偏小说明环境光很亮、IR 差分不可靠")
		}
		b.WriteString("）")
	}
	fmt.Fprintf(&b, "。暗场校准 %s。融合权重 %.2f，色彩增强 %.2f，gamma %.2f，暗部去彩 %.2f。",
		map[bool]string{true: "已启用", false: "未启用"}[st.DarkOn], p.Weight, p.Chroma, p.Gamma, p.ChromaDark)
	if st.RGBFPS < 1 {
		b.WriteString(" ⚠ RGB 相机当前没出帧。")
	}
	return b.String()
}
