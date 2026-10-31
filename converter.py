# -*- coding: utf-8 -*-
"""
老电视兼容模式视频转换器 —— 核心转换逻辑(与界面解耦,便于日后移植到 Java)

流水线(即 GUI 里"老电视兼容模式"预设):
  1. 任意输入分辨率/比例 -> 归一方形像素 -> 等比缩进 1920x1080 -> 居中垫黑边(信箱/邮筒)
  2. 烧录字幕: 优先"简体中文"文本字幕轨 -> 提取为 SRT -> 按所选样式生成
     PlayRes=1920x1080 的 ASS(样式值即真实像素) -> libass 烧录
  3. 视频: H.264 High@4.1, 优先 NVIDIA 硬编(h264_nvenc),不可用自动回退 libx264
  4. 音频: 源自带 AAC 立体声则直接复制,否则转码为 AAC 192k 立体声(5.1 自动下混)
  5. 封装: MP4 + faststart(老电视友好)

命令行可独立使用:
  python converter.py 视频文件... [--style 样式名] [--bitrate 6000] [--no-sub] ...
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

# GUI(pythonw)下调用 ffmpeg/ffprobe 等控制台程序时不弹黑框
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

VIDEO_EXTS = {".mkv", ".mp4", ".ts", ".m2ts", ".avi", ".wmv", ".flv", ".mov", ".webm", ".mpg", ".mpeg"}

TEXT_SUB_CODECS = {"subrip", "ass", "ssa", "mov_text", "webvtt", "text"}
BITMAP_SUB_CODECS = {"hdmv_pgs_subtitle", "dvb_subtitle", "dvd_subtitle"}

TARGET_W, TARGET_H = 1920, 1080

# ---------------------------------------------------------------------------
# 字幕样式(PlayRes=1920x1080,字段值即屏幕像素)
# 全部由预览确认过的效果换算而来(288 空间 × 3.75),默认项与 P1a_1px细描边 完全一致
# ---------------------------------------------------------------------------

@dataclass
class SubStyle:
    key: str
    font: str = "Microsoft YaHei"
    size: int = 67
    primary: str = "&H00FFFFFF"        # 字体颜色 (ASS 为 &HAABBGGRR)
    outline_color: str = "&H00000000"  # 描边颜色;BorderStyle=3 时同时是衬底条颜色
    back: str = "&H96000000"           # 阴影颜色(柔和阴影样式用)
    bold: int = 0
    border_style: int = 1              # 1=描边 3=衬底条
    outline: float = 1.0               # 1.0 即 1 像素
    shadow: float = 0
    blur: float = 0                    # 边缘高斯模糊(柔和阴影样式用)
    margin_v: int = 131                # 距底部像素

    def ass_style_line(self) -> str:
        return (
            "Style: Sub,{font},{size},{primary},&H000000FF,{outline_color},{back},"
            "{bold},0,0,0,100,100,0,0,{border_style},{outline},{shadow},2,60,60,{margin_v},1,{blur}"
        ).format(
            font=self.font, size=self.size, primary=self.primary,
            outline_color=self.outline_color, back=self.back, bold=self.bold,
            border_style=self.border_style, outline=_fmt_num(self.outline),
            shadow=_fmt_num(self.shadow), margin_v=self.margin_v, blur=_fmt_num(self.blur),
        )


def _fmt_num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else ("%.2f" % v).rstrip("0").rstrip(".")


SUB_STYLES: Dict[str, SubStyle] = {s.key: s for s in [
    SubStyle("白字细描边 1px(推荐)"),
    SubStyle("白字粗描边(≈7px)", outline=7.5),
    SubStyle("大字加粗(老人友好)", size=86, bold=1, outline=9, shadow=4, margin_v=105),
    SubStyle("明黄字(老碟片风)", size=71, primary="&H0000FFFF", bold=1, outline=7.5),
    SubStyle("白字黑底条(流媒体风)", border_style=3, outline_color="&H66000000", outline=19),
    SubStyle("细描边柔和阴影", outline=4.5, shadow=7, blur=7.5),
    SubStyle("楷体(国产剧风)", font="KaiTi", size=75, outline=7.5, margin_v=120),
]}
DEFAULT_STYLE_KEY = "白字细描边 1px(推荐)"

# ---------------------------------------------------------------------------
# 两种模式
# ---------------------------------------------------------------------------

MODE_TV = "老电视兼容模式"      # ffmpeg 转码:1080p 信箱黑边 + 硬字幕 + MP4,兼容性优先
MODE_MARUKO = "小丸压制模式"    # x264/x265 CRF 恒定质量压制 + MKV 软字幕,画质/体积优先

SPEED_PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast",
                 "medium", "slow", "slower", "veryslow", "placebo"]


def _capability(ffmpeg: str, test_args: List[str]) -> bool:
    """试编一小段,探测当前 ffmpeg 是否支持某个编码特性(结果缓存)。"""
    try:
        r = subprocess.run(
            [ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=256x256:d=0.2", "-pix_fmt", "yuv420p"]
            + test_args + ["-f", "null", "-"],
            capture_output=True, timeout=120, creationflags=_NO_WINDOW,
        )
        return r.returncode == 0
    except Exception:
        return False


_CAP_CACHE: Dict[str, bool] = {}


def x264_10bit_ok(ffmpeg: str) -> bool:
    key = "x26410@" + os.path.abspath(ffmpeg)
    if key not in _CAP_CACHE:
        _CAP_CACHE[key] = _capability(ffmpeg, ["-c:v", "libx264", "-pix_fmt", "yuv420p10le"])
    return _CAP_CACHE[key]


# ---------------------------------------------------------------------------
# ffmpeg / ffprobe
# ---------------------------------------------------------------------------

def default_tool_paths() -> Tuple[str, str]:
    """定位 ffmpeg/ffprobe:PATH -> 常见安装位置。"""
    ffmpeg = shutil.which("ffmpeg") or ""
    ffprobe = shutil.which("ffprobe") or ""
    if ffmpeg and ffprobe:
        return ffmpeg, ffprobe
    candidates = [
        r"D:\4environment\ffmpeg-new\ffmpeg-master-latest-win64-gpl-shared\bin",
        r"C:\ffmpeg\bin",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "ffmpeg", "bin"),
    ]
    for d in candidates:
        f1, f2 = os.path.join(d, "ffmpeg.exe"), os.path.join(d, "ffprobe.exe")
        if os.path.isfile(f1) and os.path.isfile(f2):
            return f1, f2
    return ffmpeg, ffprobe


def probe_streams(ffprobe: str, path: str) -> dict:
    cmd = [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", path]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                         creationflags=_NO_WINDOW)
    if out.returncode != 0:
        raise RuntimeError("ffprobe 失败: %s" % out.stderr.strip()[:400])
    return json.loads(out.stdout)


def detect_nvenc(ffmpeg: str) -> bool:
    """真正试编一帧,确认 NVENC 可用(仅列出但无 GPU 时会失败)。"""
    try:
        r = subprocess.run(
            [ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=black:s=320x240:d=0.2",
             "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True, timeout=60, creationflags=_NO_WINDOW,
        )
        return r.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 轨道选择
# ---------------------------------------------------------------------------

def _tags(stream: dict) -> dict:
    return stream.get("tags") or {}


def pick_audio_track(streams: List[dict]) -> Tuple[Optional[int], str]:
    """优先 AAC 立体声直接复制;否则取第一条音轨转码为 AAC 立体声。"""
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    if not audios:
        return None, "no_audio"
    for s in audios:
        if s.get("codec_name") == "aac" and int(s.get("channels", 0) or 0) <= 2:
            return s["index"], "copy"
    return audios[0]["index"], "encode"


def pick_subtitle_track(streams: List[dict], allow_bitmap: bool = False) -> Tuple[Optional[int], str]:
    """简体中文优先,其次中文,再次任意文本字幕轨。返回 (绝对轨号, 说明)。

    allow_bitmap=True(软字幕封装场景)时位图字幕(PGS 等)也可选用,原样复制。
    """
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    if not subs:
        return None, "未找到字幕轨"

    def score(s: dict) -> int:
        lang = (_tags(s).get("language") or "").lower()
        title = _tags(s).get("title") or ""
        sc = 0
        if lang in ("chi", "zho", "zh"):
            sc += 4
        if "简" in title:
            sc += 4
        if "中" in title and "繁" not in title:
            sc += 1
        if "繁" in title:
            sc += 1
        if s.get("codec_name") in TEXT_SUB_CODECS:
            sc += 2
        return sc

    best = sorted(subs, key=score, reverse=True)[0]
    title = _tags(best).get("title") or best.get("codec_name", "")
    if best.get("codec_name") in BITMAP_SUB_CODECS:
        if allow_bitmap:
            return best["index"], "选用字幕轨 #%s %s(位图,原样封装)" % (best["index"], title)
        return None, "字幕轨是位图格式(%s),无法烧录成文字" % title
    if best.get("codec_name") not in TEXT_SUB_CODECS:
        return None, "字幕轨格式不支持(%s)" % best.get("codec_name")
    return best["index"], "选用字幕轨 #%s %s" % (best["index"], title)


def extract_srt(ffmpeg: str, src: str, sub_index: int, tmp_dir: str, log: Callable[[str], None]) -> Optional[str]:
    """把内嵌文本字幕轨转为 SRT(仅封包级操作,很快)。"""
    srt_path = os.path.join(tmp_dir, "subs.srt")
    cmd = [ffmpeg, "-y", "-v", "error", "-i", src, "-map", "0:%d" % sub_index, "-c:s", "srt", srt_path]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
                       creationflags=_NO_WINDOW)
    if r.returncode != 0 or not os.path.isfile(srt_path):
        log("  字幕提取失败:%s" % (r.stderr or "").strip()[:200])
        return None
    return srt_path


# ---------------------------------------------------------------------------
# SRT -> ASS(按所选样式,PlayRes=1920x1080)
# ---------------------------------------------------------------------------

_TS_RE = re.compile(r"(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})")
_TAG_RE = re.compile(r"\{[^}]*\}")
_HTML_RE = re.compile(r"</?[a-zA-Z][^>]*>")
_ENTITIES = [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " ")]


def _ts_to_ass(m: "re.Match[str]") -> str:
    h, mi, s, ms = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
    cs = (h * 3600 + mi * 60 + s) * 100 + (ms // 10)
    return "%d:%02d:%02d.%02d" % (cs // 360000, (cs // 6000) % 60, (cs // 100) % 60, cs % 100)


def _clean_line(t: str) -> str:
    t = _TAG_RE.sub("", t)
    t = _HTML_RE.sub("", t)
    for a, b in _ENTITIES:
        t = t.replace(a, b)
    return t.strip()


def parse_srt(text: str) -> List[Tuple[str, str, str]]:
    events = []
    for block in re.split(r"\r?\n\r?\n+", text.strip()):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        ts_i = 0
        if "-->" not in lines[0] and len(lines) > 1 and "-->" in lines[1]:
            ts_i = 1
        if "-->" not in lines[ts_i]:
            continue
        seg = lines[ts_i].split("-->")
        if len(seg) != 2:
            continue
        m1, m2 = _TS_RE.search(seg[0]), _TS_RE.search(seg[1])
        if not m1 or not m2:
            continue
        body = [_clean_line(ln) for ln in lines[ts_i + 1:]]
        body = [ln for ln in body if ln]
        if not body:
            continue
        events.append((_ts_to_ass(m1), _ts_to_ass(m2), "\\N".join(body)))
    return events


def srt_to_ass(srt_path: str, ass_path: str, style: SubStyle) -> int:
    with open(srt_path, "r", encoding="utf-8-sig", errors="replace") as f:
        text = f.read()
    events = parse_srt(text)
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: %d\n"
        "PlayResY: %d\n"
        "ScaledBorderAndShadow: yes\n"
        "WrapStyle: 0\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding, Blur\n"
        "%s\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    ) % (TARGET_W, TARGET_H, style.ass_style_line())
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header)
        for start, end, body in events:
            f.write("Dialogue: 0,%s,%s,Sub,,0,0,0,,%s\n" % (start, end, body))
    return len(events)


# ---------------------------------------------------------------------------
# 滤镜链与编码参数
# ---------------------------------------------------------------------------

def build_video_filter(ass_name: Optional[str]) -> str:
    """任意输入 -> 1920x1080 信箱黑边 -> 烧字幕 -> yuv420p

    ass_name 用相对文件名(如 "subs.ass"),配合 ffmpeg 的 cwd=临时目录,
    避开 Windows 绝对路径在滤镜里的冒号转义问题(libass 加载失败时会静默不出字)。
    """
    chain = (
        "scale=trunc(iw*max(sar\\,1)/2)*2:trunc(ih/2)*2,setsar=1,"
        "scale=%d:%d:force_original_aspect_ratio=decrease:flags=lanczos,"
        "scale=trunc(iw/2)*2:trunc(ih/2)*2,"
        "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:black"
    ) % (TARGET_W, TARGET_H, TARGET_W, TARGET_H)
    if ass_name:
        chain += ",subtitles=%s" % ass_name
    chain += ",format=yuv420p"
    return chain


_NVENC_CACHE: Dict[str, bool] = {}


def _nvenc_cached(ffmpeg: str) -> bool:
    key = os.path.abspath(ffmpeg)
    if key not in _NVENC_CACHE:
        _NVENC_CACHE[key] = detect_nvenc(ffmpeg)
    return _NVENC_CACHE[key]


def encoder_args(ffmpeg: str, bitrate_kbps: int) -> Tuple[List[str], str]:
    if _nvenc_cached(ffmpeg):
        return (
            ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr",
             "-b:v", "%dk" % bitrate_kbps, "-maxrate", "%dk" % int(bitrate_kbps * 1.5),
             "-bufsize", "%dk" % (bitrate_kbps * 2), "-profile:v", "high", "-level", "4.1"],
            "NVIDIA 硬编 (h264_nvenc)",
        )
    return (
        ["-c:v", "libx264", "-preset", "medium",
         "-b:v", "%dk" % bitrate_kbps, "-maxrate", "%dk" % int(bitrate_kbps * 1.5),
         "-bufsize", "%dk" % (bitrate_kbps * 2), "-profile:v", "high", "-level", "4.1",
         "-pix_fmt", "yuv420p"],
        "软件编码 (libx264)",
    )


# ---------------------------------------------------------------------------
# 单文件转换
# ---------------------------------------------------------------------------

class CancelledError(Exception):
    pass


def output_path_for(src: str, opts: ConvertOptions) -> Path:
    """按当前模式计算输出文件路径(与 convert_file 内部逻辑一致)。"""
    src_path = Path(src)
    if opts.mode == MODE_MARUKO:
        return src_path.parent / (src_path.stem + opts.suffix_m + ".mkv")
    return src_path.parent / (src_path.stem + opts.suffix + ".mp4")


@dataclass
class ConvertOptions:
    ffmpeg: str
    ffprobe: str
    mode: str = MODE_TV
    # ---- 老电视兼容模式 ----
    style: SubStyle = field(default_factory=lambda: SUB_STYLES[DEFAULT_STYLE_KEY])
    bitrate_kbps: int = 6000
    suffix: str = ".1080p.老电视"
    burn_sub: bool = True
    # ---- 小丸压制模式 ----
    enc: str = "x264"          # x264 | x26410 | x265
    crf: int = 18
    speed: str = "medium"      # x264/x265 的 preset
    keep_res: bool = True      # True=保持原分辨率 False=压到 1080p 信箱
    sub_mode: str = "soft"     # soft=封装软字幕 burn=烧录 none=不处理
    audio: str = "copy"        # copy=原样复制 aac=压制成 AAC
    aac_kbps: int = 192
    suffix_m: str = ".小丸压制"
    # ---- 共用 ----
    overwrite: bool = False


def maruko_encoder_args(opts: ConvertOptions) -> Tuple[List[str], str]:
    """小丸模式视频编码参数(ffmpeg 内置 libx264/libx265,即 x264/x265 内核)。"""
    if opts.enc == "x265":
        return (["-c:v", "libx265", "-preset", opts.speed, "-crf", str(opts.crf),
                 "-pix_fmt", "yuv420p10le", "-x265-params", "log-level=error"],
                "x265 10bit (HEVC)")
    pix = "yuv420p10le" if opts.enc == "x26410" else "yuv420p"
    name = "x264 10bit" if opts.enc == "x26410" else "x264 8bit"
    return (["-c:v", "libx264", "-preset", opts.speed, "-crf", str(opts.crf),
             "-pix_fmt", pix], name)


def _convert_maruko(src: str, opts: ConvertOptions, dst_path: Path,
                    streams: List[dict], duration: float,
                    audio_idx: Optional[int], audio_mode: str,
                    log: Callable[[str], None],
                    progress: Callable[[Optional[float], Optional[dict]], None],
                    cancel_event) -> None:
    """小丸压制模式:x264/x265 CRF 压制,MKV 封装,软字幕优先。"""
    enc_args, enc_name = maruko_encoder_args(opts)
    log("  视频:%s CRF%d preset=%s" % (enc_name, opts.crf, opts.speed))

    sub_idx, sub_note = None, "不处理字幕"
    need_ass = False
    if opts.sub_mode == "soft":
        sub_idx, sub_note = pick_subtitle_track(streams, allow_bitmap=True)
        log("  字幕:" + sub_note + "(软字幕封装)")
    elif opts.sub_mode == "burn":
        sub_idx, sub_note = pick_subtitle_track(streams)
        log("  字幕:" + sub_note + "(烧录)")
    else:
        log("  字幕:" + sub_note)

    audio_args: List[str] = []
    if audio_idx is None:
        log("  音频:未找到音轨,输出将无声")
        audio_args = ["-an"]
    elif opts.audio == "copy":
        log("  音频:原样复制")
        audio_args = ["-map", "0:%d" % audio_idx, "-c:a", "copy"]
    else:
        log("  音频:压制 AAC %dkbps 立体声" % opts.aac_kbps)
        audio_args = ["-map", "0:%d" % audio_idx, "-c:a", "aac",
                      "-b:a", "%dk" % opts.aac_kbps, "-ac", "2"]

    tmp_dir = tempfile.mkdtemp(prefix="maruko_")
    try:
        ass_name = None
        if sub_idx is not None and opts.sub_mode == "burn":
            srt_path = extract_srt(opts.ffmpeg, src, sub_idx, tmp_dir, log)
            if srt_path:
                ass_path = os.path.join(tmp_dir, "subs.ass")
                n = srt_to_ass(srt_path, ass_path, opts.style)
                ass_name = "subs.ass"
                log("  字幕:已按「%s」样式生成(%d 条)" % (opts.style.key, n))

        vf_parts: List[str] = []
        if not opts.keep_res:
            vf_parts += ["scale=trunc(iw*max(sar\\,1)/2)*2:trunc(ih/2)*2,setsar=1",
                         "scale=%d:%d:force_original_aspect_ratio=decrease:flags=lanczos" % (TARGET_W, TARGET_H),
                         "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                         "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:black" % (TARGET_W, TARGET_H)]
        pix = "yuv420p10le" if (opts.enc != "x264") else "yuv420p"
        if ass_name:
            vf_parts.append("subtitles=subs.ass")
        if vf_parts:
            vf_parts.append("format=" + pix)
            vf = ",".join(vf_parts)
        else:
            vf = None

        cmd = [opts.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
               "-nostats", "-progress", "pipe:1", "-i", str(src),
               "-map", "0:v:0"]
        cmd += audio_args
        if sub_idx is not None and opts.sub_mode == "soft":
            cmd += ["-map", "0:%d" % sub_idx, "-c:s", "copy", "-disposition:s:0", "default"]
            log("  轨道:字幕轨封装为默认轨(播放器可开关)")
        else:
            cmd += ["-sn"]
        cmd += ["-dn"]
        if vf:
            cmd += ["-vf", vf]
        cmd += ["-map_metadata", "0"] + enc_args + [str(dst_path)]

        log("  容器:MKV(保留章节/多轨)")
        _run_with_progress(cmd, duration, progress, cancel_event, cwd=tmp_dir if ass_name else None)
        log("  完成 -> %s" % dst_path.name)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _run_with_progress(cmd: List[str], duration: float,
                       progress: Callable[[Optional[float], Optional[dict]], None],
                       cancel_event, cwd: Optional[str] = None) -> None:
    """跑 ffmpeg 主命令,解析 -progress 输出汇报进度(附带真实的 speed/fps);取消/失败抛异常。"""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", cwd=cwd,
                            creationflags=_NO_WINDOW)
    err_tail: List[str] = []

    def _drain_err():
        for line in proc.stderr:
            err_tail.append(line.rstrip())
            if len(err_tail) > 30:
                err_tail.pop(0)

    import threading
    t_err = threading.Thread(target=_drain_err, daemon=True)
    t_err.start()

    info: dict = {}
    for line in proc.stdout:
        if cancel_event is not None and cancel_event.is_set():
            proc.terminate()
            raise CancelledError()
        if line.startswith("out_time_us="):
            try:
                secs = int(line.split("=", 1)[1]) / 1_000_000
                if duration > 0:
                    progress(min(1.0, max(0.0, secs / duration)), dict(info) if info else None)
            except ValueError:
                pass
        elif line.startswith("out_time_ms="):
            try:  # 旧版 ffmpeg 的 out_time_ms 实为微秒
                secs = int(line.split("=", 1)[1]) / 1_000_000
                if duration > 0:
                    progress(min(1.0, max(0.0, secs / duration)), dict(info) if info else None)
            except ValueError:
                pass
        elif line.startswith("speed="):
            info["speed"] = line.split("=", 1)[1].strip()
        elif line.startswith("fps="):
            info["fps"] = line.split("=", 1)[1].strip()
    t_err.join(timeout=5)
    proc.wait()
    if proc.returncode != 0:
        detail = "\n".join(err_tail[-8:])
        raise RuntimeError("ffmpeg 失败(代码 %d):\n%s" % (proc.returncode, detail))


def convert_file(src: str, opts: ConvertOptions,
                 log: Callable[[str], None] = print,
                 progress: Callable[[Optional[float], Optional[dict]], None] = lambda p, info=None: None,
                 cancel_event=None) -> str:
    """转换单个文件,返回输出路径。取消时抛 CancelledError,失败抛 RuntimeError。"""
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledError()
    src_path = Path(src)
    dst_path = output_path_for(src, opts)
    log("转换:%s" % src_path.name)

    if dst_path.exists() and not opts.overwrite:
        log("  跳过:输出文件已存在(%s)" % dst_path.name)
        progress(1.0, None)
        return str(dst_path)

    info = probe_streams(opts.ffprobe, str(src_path))
    streams = info.get("streams", [])
    duration = float((info.get("format") or {}).get("duration") or 0)

    audio_idx, audio_mode = pick_audio_track(streams)
    if audio_idx is None:
        log("  音频:未找到音轨,输出将无声")
    elif opts.mode == MODE_MARUKO:
        pass  # 小丸模式里按 sub/audio 选项自行记录
    elif audio_mode == "copy":
        log("  音频:AAC 立体声,直接复制")
    else:
        log("  音频:转码为 AAC 192k 立体声")

    if opts.mode == MODE_MARUKO:
        _convert_maruko(str(src_path), opts, dst_path, streams, duration,
                        audio_idx, audio_mode, log, progress, cancel_event)
        progress(1.0, None)
        return str(dst_path)

    sub_idx, sub_note = (None, "未启用硬字幕")
    if opts.burn_sub:
        sub_idx, sub_note = pick_subtitle_track(streams)
        log("  字幕:" + sub_note)

    tmp_dir = tempfile.mkdtemp(prefix="tvconv_")
    try:
        ass_path = None
        if sub_idx is not None:
            srt_path = extract_srt(opts.ffmpeg, str(src_path), sub_idx, tmp_dir, log)
            if srt_path:
                ass_path = os.path.join(tmp_dir, "subs.ass")
                n = srt_to_ass(srt_path, ass_path, opts.style)
                log("  字幕:已按「%s」样式生成(%d 条)" % (opts.style.key, n))
            else:
                log("  字幕:提取失败,改为不烧字幕")

        enc_args, enc_name = encoder_args(opts.ffmpeg, opts.bitrate_kbps)
        log("  视频:H.264 %s %dkbps" % (enc_name, opts.bitrate_kbps))

        cmd = [opts.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
               "-nostats", "-progress", "pipe:1", "-i", str(src_path),
               "-map", "0:v:0"]
        if audio_idx is not None:
            cmd += ["-map", "0:%d" % audio_idx]
            if audio_mode == "copy":
                cmd += ["-c:a", "copy"]
            else:
                cmd += ["-c:a", "aac", "-b:a", "192k", "-ac", "2"]
        else:
            cmd += ["-an"]
        cmd += ["-sn", "-dn", "-vf", build_video_filter("subs.ass" if ass_path else None),
                "-map_metadata", "0", "-movflags", "+faststart"]
        cmd += enc_args + [str(dst_path)]

        _run_with_progress(cmd, duration, progress, cancel_event, cwd=tmp_dir)
        progress(1.0, None)
        log("  完成 -> %s" % dst_path.name)
        return str(dst_path)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def collect_videos(inputs: List[str]) -> List[str]:
    """展开输入(文件或目录,目录取一层视频文件),去重保序。"""
    out: List[str] = []
    seen = set()
    for x in inputs:
        p = Path(x)
        if p.is_dir():
            for f in sorted(p.iterdir()):
                if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
                    fp = str(f)
                    if fp not in seen:
                        seen.add(fp)
                        out.append(fp)
        elif p.is_file():
            fp = str(p)
            if fp not in seen:
                seen.add(fp)
                out.append(fp)
    return out


# ---------------------------------------------------------------------------
# 预设命令预览与解释(界面"命令与解释"页 / README 使用)
# ---------------------------------------------------------------------------

VF_EXPLAIN = [
    ("scale=trunc(iw*max(sar,1)/2)*2:trunc(ih/2)*2,setsar=1",
     "非方形像素归一(防变形),宽高取偶"),
    ("scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos",
     "等比缩放到刚好装进 1080p(lanczos 高质量缩放算法)"),
    ("scale=trunc(iw/2)*2:trunc(ih/2)*2",
     "宽高取偶(H.264 色度抽样要求偶数)"),
    ("pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black",
     "居中垫黑边:宽银幕上下黑边、竖屏左右黑边,画面永不拉伸"),
    ("subtitles=subs.ass",
     "按所选样式烧录字幕(ASS 的 PlayRes=1920×1080,样式数值即屏幕真实像素)"),
    ("format=yuv420p",
     "像素格式,兼容性最广"),
]

FLAG_EXPLAIN = [
    ("-map 0:v:0", "取第一条视频轨"),
    ("-map 0:<音轨号>", "音频:源里是 AAC 立体声 → -c:a copy 直接复制零损失;否则 → -c:a aac -b:a 192k -ac 2(5.1 自动下混)"),
    ("-sn -dn", "字幕轨/数据轨不写入 MP4(字幕已经烧进画面)"),
    ("-c:v h264_nvenc", "NVIDIA 硬件编码;无 N 卡自动回退 -c:v libx264 软编"),
    ("-preset p5 -rc vbr", "p5 质量档 + 动态码率,速度画质均衡"),
    ("-b:v/-maxrate/-bufsize", "平均码率 / 峰值 / 缓冲,稳画质控体积(界面里可改)"),
    ("-profile:v high -level 4.1", "H.264 兼容档位,老电视时代的解码器都支持"),
    ("-movflags +faststart", "索引移到文件头:点开即播、支持边下边播"),
    ("-map_metadata 0", "保留源文件的标题等元数据"),
]


def style_desc(s: SubStyle) -> str:
    parts = [s.font, "字号%dpx" % s.size]
    if s.bold:
        parts.append("加粗")
    if s.border_style == 3:
        parts.append("半透明黑底条")
    else:
        parts.append("描边%spx" % _fmt_num(s.outline))
    if s.shadow:
        parts.append("阴影%spx" % _fmt_num(s.shadow))
    if s.blur:
        parts.append("边缘模糊")
    parts.append("距底%dpx(黑边内)" % s.margin_v)
    return "、".join(parts)


MARUKO_FLAG_EXPLAIN = [
    ("-map 0:v:0", "取第一条视频轨"),
    ("-crf <数值>", "恒定质量压制:数值越小画质越高、体积越大。18≈接近无损观感;23≈通用;小丸工具箱同款思路"),
    ("-preset <速度>", "编码速度与压缩比权衡:slow 压得更小但更慢,medium 折中"),
    ("-pix_fmt yuv420p10le", "10bit 高位深:渐变更平滑、暗部不易色带,同画质体积更小;老设备可能不兼容(8bit 则为 yuv420p)"),
    ("-map 0:<字幕轨号> -c:s copy -disposition:s default", "软字幕:字幕轨原样封装进 MKV 并设为默认轨,播放时可开关,体积几乎不增加(小丸思路:能封装不烧录)"),
    ("-c:a copy", "音频原样复制零损失;选压制则为 -c:a aac -b:a 192k -ac 2"),
    ("-map_metadata 0", "保留标题等元数据与章节"),
]


def _maruko_rows(opts: ConvertOptions) -> List[Tuple[str, str]]:
    rows: List[Tuple[str, str]] = []
    out_name = "视频名%s.mkv" % opts.suffix_m
    enc_args, enc_name = maruko_encoder_args(opts)

    rows.append(("h1", "预设「小丸压制模式」将执行以下流程"))
    rows.append(("exp", "目标:原分辨率(可选压到 1080p)x264/x265 CRF 恒定质量压制,MKV 封装,软字幕优先;"
                        "追求「体积小、画质好、可收藏」。注意:这不是老电视兼容取向,电视盒子多数放不了 MKV 软字幕。"))
    rows.append(("gap", ""))

    rows.append(("h1", "① 探测轨道"))
    rows.append(("cmd", 'ffprobe -v error -show_streams -show_format -of json "输入文件"'))
    rows.append(("exp", "· 字幕轨按 简体中文 > 中文 > 其他 自动选择;软字幕模式下位图字幕(PGS)也可原样封装"))
    rows.append(("gap", ""))

    if opts.sub_mode == "burn":
        rows.append(("h1", "② 提取并重排字幕(烧录时)"))
        rows.append(("cmd", 'ffmpeg -y -v error -i "输入文件" -map 0:<字幕轨号> -c:s srt subs.srt'))
        rows.append(("exp", "· 与老电视模式相同:抽成纯文本再按「%s」样式生成 ASS 烧进画面" % opts.style.key))
        rows.append(("gap", ""))

    rows.append(("h1", "③ 主转码"))
    vf_parts: List[str] = []
    if not opts.keep_res:
        vf_parts += ["scale=trunc(iw*max(sar,1)/2)*2:trunc(ih/2)*2,setsar=1",
                     "scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos",
                     "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                     "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black"]
    if opts.sub_mode == "burn":
        vf_parts.append("subtitles=subs.ass")
    vf_display = ",".join(vf_parts + ["format=%s" % ("yuv420p10le" if opts.enc != "x264" else "yuv420p")]) if vf_parts else ""
    vf_txt = '-vf "%s" ' % vf_display if vf_display else ""
    audio_txt = "-c:a copy" if opts.audio == "copy" else "-c:a aac -b:a %dk -ac 2" % opts.aac_kbps
    sub_txt = "-map 0:<字幕轨号> -c:s copy -disposition:s default" if opts.sub_mode == "soft" else "-sn"
    rows.append(("cmd", 'ffmpeg -y -i "输入文件" -map 0:v:0 -map 0:<音轨号> %s -dn %s%s '
                        '-map_metadata 0 %s "%s"' % (sub_txt, vf_txt, audio_txt, " ".join(enc_args), out_name)))
    if not vf_display:
        rows.append(("exp", "· 保持原分辨率且不烧字幕时,画面原样交给编码器,不经过任何滤镜(转码损耗最小)"))
    rows.append(("gap", ""))

    rows.append(("h1", "关键参数解释"))
    for flag, d in MARUKO_FLAG_EXPLAIN:
        rows.append(("exp", "  %s   %s" % (flag, d)))
    rows.append(("exp", "  编码器:%s;输出 MKV 容器(保留章节/多轨,不需要 faststart)" % enc_name))
    if opts.enc == "x26410":
        rows.append(("exp", "  提示:当前 ffmpeg %s 10bit x264 支持" % ("确认支持" if x264_10bit_ok(opts.ffmpeg) else "未确认支持,开始转换时如失败请改用 x264 8bit")))
    return rows


def preview_plan(opts: ConvertOptions, enc_note: Optional[str] = None) -> List[Tuple[str, str]]:
    """生成预设将执行的命令与逐项解释。返回 (类型, 文本) 列表:
    类型: h1=小标题 / cmd=命令 / exp=解释 / gap=空行
    """
    if opts.mode == MODE_MARUKO:
        return _maruko_rows(opts)

    rows: List[Tuple[str, str]] = []
    out_name = "视频名%s.mp4" % opts.suffix
    tools_ready = os.path.isfile(opts.ffmpeg)

    enc_args, enc_name = encoder_args(opts.ffmpeg, opts.bitrate_kbps)
    if not tools_ready:
        enc_name = "开始转换时自动检测(N 卡硬编优先)"

    rows.append(("h1", "预设「老电视兼容模式」将执行以下流程"))
    rows.append(("exp", "目标:任意输入 → 1920×1080 H.264/AAC MP4;画面不变形(非 16:9 自动垫黑边);"
                        "烧录简中字幕;老电视可硬解。输出保存在原视频同一目录。"))
    rows.append(("gap", ""))

    rows.append(("h1", "① 探测轨道"))
    rows.append(("cmd", 'ffprobe -v error -show_streams -show_format -of json "输入文件"'))
    rows.append(("exp", "· 读取轨道信息;字幕轨按 简体中文 > 中文 > 其他文本轨 自动选择,位图字幕(PGS 等)跳过并在日志提示"))
    rows.append(("gap", ""))

    if opts.burn_sub:
        rows.append(("h1", "② 提取并重排字幕"))
        rows.append(("cmd", 'ffmpeg -y -v error -i "输入文件" -map 0:<字幕轨号> -c:s srt subs.srt'))
        rows.append(("exp", "· 把内嵌字幕抽成纯文本 SRT:去掉字体/特效/\\pos 定位标签,避免烧出来位置错乱"))
        rows.append(("exp", "· 再按所选样式拼成 ASS:PlayRes=1920×1080,样式数值即屏幕真实像素"))
        rows.append(("exp", "· 当前样式「%s」:%s" % (opts.style.key, style_desc(opts.style))))
        rows.append(("gap", ""))

    rows.append(("h1", "③ 主转码"))
    rows.append(("cmd", 'ffmpeg -y -i "输入文件" -map 0:v:0 -map 0:<音轨号> -sn -dn '
                        '-vf "%s" -c:a <音频参数> -map_metadata 0 -movflags +faststart '
                        '%s "%s"' % (build_video_filter("subs.ass" if opts.burn_sub else None),
                                     " ".join(enc_args), out_name)))
    if not tools_ready:
        rows.append(("exp", "· 当前 ffmpeg 未就绪,以上编码器参数以 NVIDIA 硬编为例;开始转换时会按实际硬件自动确认"))
    rows.append(("gap", ""))

    rows.append(("h1", "滤镜链逐段解释 -vf"))
    for seg, d in VF_EXPLAIN:
        if seg.startswith("subtitles") and not opts.burn_sub:
            continue
        rows.append(("cmd", "  " + seg))
        rows.append(("exp", "      ← " + d))
    rows.append(("gap", ""))

    rows.append(("h1", "关键参数解释"))
    for flag, d in FLAG_EXPLAIN:
        rows.append(("exp", "  %s   %s" % (flag, d)))
    rows.append(("exp", "  编码器:%s" % enc_name))
    return rows


# ---------------------------------------------------------------------------
# 命令行入口(测试与批量用)
# ---------------------------------------------------------------------------

def main(argv: List[str]) -> int:
    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    import argparse
    ap = argparse.ArgumentParser(description="老电视兼容 / 小丸压制 双模式视频转换(核心模块)")
    ap.add_argument("inputs", nargs="+", help="视频文件或目录")
    ap.add_argument("--mode", default=MODE_TV, choices=[MODE_TV, MODE_MARUKO], help="转换模式")
    ap.add_argument("--style", default=DEFAULT_STYLE_KEY, help="[老电视] 字幕样式: %s" % " | ".join(SUB_STYLES))
    ap.add_argument("--bitrate", type=int, default=6000, help="[老电视] 视频码率 kbps,默认 6000")
    ap.add_argument("--suffix", default=".1080p.老电视", help="[老电视] 输出文件名后缀")
    ap.add_argument("--no-sub", action="store_true", help="[老电视] 不烧录字幕")
    ap.add_argument("--enc", default="x264", choices=["x264", "x26410", "x265"], help="[小丸] 编码器")
    ap.add_argument("--crf", type=int, default=18, help="[小丸] CRF 质量值(越小画质越高),默认 18")
    ap.add_argument("--speed", default="medium", choices=SPEED_PRESETS, help="[小丸] 编码速度档")
    ap.add_argument("--limit-1080p", action="store_true", help="[小丸] 压到 1080p 信箱(默认保持原分辨率)")
    ap.add_argument("--sub-mode", default="soft", choices=["soft", "burn", "none"], help="[小丸] 字幕处理:软字幕封装/烧录/不处理")
    ap.add_argument("--mk-audio", default="copy", choices=["copy", "aac"], help="[小丸] 音频:复制或压制成 AAC")
    ap.add_argument("--aac-kbps", type=int, default=192, help="[小丸] AAC 码率")
    ap.add_argument("--suffix-m", default=".小丸压制", help="[小丸] 输出文件名后缀")
    ap.add_argument("--overwrite", action="store_true", help="覆盖已存在的输出")
    ap.add_argument("--ffmpeg-dir", default="", help="ffmpeg 所在目录(默认自动探测)")
    args = ap.parse_args(argv)

    if args.ffmpeg_dir:
        ffmpeg = os.path.join(args.ffmpeg_dir, "ffmpeg.exe")
        ffprobe = os.path.join(args.ffmpeg_dir, "ffprobe.exe")
    else:
        ffmpeg, ffprobe = default_tool_paths()
    if not (os.path.isfile(ffmpeg) and os.path.isfile(ffprobe)):
        print("找不到 ffmpeg/ffprobe,请用 --ffmpeg-dir 指定")
        return 2

    style = SUB_STYLES.get(args.style)
    if style is None:
        print("未知字幕样式:%s" % args.style)
        return 2
    opts = ConvertOptions(ffmpeg=ffmpeg, ffprobe=ffprobe, mode=args.mode, style=style,
                          bitrate_kbps=args.bitrate, suffix=args.suffix,
                          burn_sub=not args.no_sub,
                          enc=args.enc, crf=args.crf, speed=args.speed,
                          keep_res=not args.limit_1080p, sub_mode=args.sub_mode,
                          audio=args.mk_audio, aac_kbps=args.aac_kbps,
                          suffix_m=args.suffix_m, overwrite=args.overwrite)

    files = collect_videos(args.inputs)
    if not files:
        print("没有找到视频文件")
        return 2
    ok = fail = 0
    for f in files:
        try:
            convert_file(f, opts)
            ok += 1
        except CancelledError:
            print("已取消")
            break
        except Exception as e:
            print("失败:%s" % e)
            fail += 1
    print("完成:成功 %d,失败 %d" % (ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
