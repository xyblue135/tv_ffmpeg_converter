# -*- coding: utf-8 -*-
"""
老电视兼容模式视频转换器 —— 图形界面(CustomTkinter)

设计系统:间距 4/8/12/16/20/24,控件高 32/40,圆角 卡片12/按钮8,
色彩为中性灰 + 单一蓝色强调;深浅两套主题集中定义在 THEMES。

业务逻辑全部在 converter.py(本文件不生成任何 ffmpeg 命令)。
"""

import json
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

import converter
from converter import (
    DEFAULT_STYLE_KEY,
    MODE_MARUKO,
    MODE_TV,
    SPEED_PRESETS,
    SUB_STYLES,
    CancelledError,
    ConvertOptions,
    collect_videos,
    convert_file,
    default_tool_paths,
    output_path_for,
    preview_plan,
    probe_streams,
)

try:  # 拖拽支持(可选依赖,缺失时自动降级为仅按钮添加)
    import tkinterdnd2
    DND_OK = True
except Exception:
    DND_OK = False

SETTINGS_FILE = Path.home() / ".tv_converter_gui.json"

# ---------------- 设计令牌 ----------------

SP = {"xs": 4, "s": 8, "m": 12, "l": 16, "xl": 20, "xxl": 24}
H_CTRL, H_BTN, H_PRIMARY = 32, 34, 42
R_CARD, R_CTRL = 12, 8

THEMES = {
    "light": {
        "bg": "#f2f3f5", "sidebar": "#e9eaee", "card": "#ffffff", "field": "#f3f4f6",
        "border": "#d8dbe0", "hover": "#eceef1", "sel_tint": "#dbe7ff",
        "fg": "#1f2328", "muted": "#5f6673", "faint": "#9aa0a6",
        "accent": "#2563eb", "accent_hi": "#1d4ed8", "accent_soft": "#d6e4ff",
        "ok": "#15803d", "warn": "#b45309", "err": "#dc2626",
        "log_bg": "#fafbfc", "log_fg": "#3a3f45",
        "tree_sel_fg": "#ffffff", "stripe": "#f6f7f8",
    },
}

UI_FONT = ("Microsoft YaHei UI", 12)
SMALL_FONT = ("Microsoft YaHei UI", 11)
MONO_FONT = ("Consolas", 12)

MODE_SEG = {"老电视兼容": MODE_TV, "小丸压制": MODE_MARUKO}
MODE_SEG_REV = {v: k for k, v in MODE_SEG.items()}

ENCODER_CHOICES = [("x264 8bit(兼容性最好)", "x264"),
                   ("x264 10bit(渐变更优)", "x26410"),
                   ("x265 10bit(体积最小,较慢)", "x265")]
SUB_MODE_CHOICES = [("封装软字幕(推荐)", "soft"),
                    ("烧录进画面", "burn"),
                    ("不处理字幕", "none")]
AUDIO_CHOICES = [("原样复制(无损)", "copy"),
                 ("压制 AAC", "aac")]

TASK_PENDING, TASK_RUN, TASK_OK, TASK_ERR, TASK_SKIP, TASK_CANCEL = (
    "待转换", "转换中", "已完成", "失败", "已跳过", "已取消")


def human_size(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "—"


def human_duration(sec) -> str:
    try:
        sec = int(float(sec))
    except (TypeError, ValueError):
        return "—"
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return "%d:%02d:%02d" % (h, m, s) if h else "%d:%02d" % (m, s)


def truncate_middle(text: str, maxlen: int = 72) -> str:
    if len(text) <= maxlen:
        return text
    half = (maxlen - 1) // 2
    return text[:half] + "…" + text[-half:]


# ---------------- 图标(统一线稿风格,Canvas 绘制) ----------------

def draw_icon(cv: tk.Canvas, kind: str, color: str, bg: str = ""):
    cv.delete("all")
    w = 2
    if kind == "app":
        cv.create_oval(2, 2, 26, 26, fill=color, outline="")
        cv.create_polygon(11, 8, 11, 20, 20, 14, fill="#ffffff", outline="")
    elif kind == "tv":
        cv.create_rectangle(3, 7, 23, 19, outline=color, width=w)
        cv.create_line(8, 3, 12, 7, fill=color, width=w)
        cv.create_line(18, 3, 14, 7, fill=color, width=w)
    elif kind == "maruko":  # 向内收拢的箭头 = 压制
        cv.create_line(3, 6, 9, 12, 3, 18, fill=color, width=w, joinstyle="round")
        cv.create_line(23, 6, 17, 12, 23, 18, fill=color, width=w, joinstyle="round")
        cv.create_line(11, 12, 15, 12, fill=color, width=w)
    elif kind == "film":
        cv.create_rectangle(6, 10, 58, 54, outline=color, width=w)
        for y in (16, 26, 36, 46):
            cv.create_rectangle(11, y, 17, y + 6, outline=color, width=1)
            cv.create_rectangle(47, y, 53, y + 6, outline=color, width=1)
    elif kind == "chevron":
        o = 8 if getattr(cv, "_open", False) else 12
        cv.create_line(6, o, 12, o + (-8 if cv._open else 8), 18, o, fill=color, width=w,
                       joinstyle="round")


class Tooltip:
    """轻量悬浮提示(悬停 0.6s 出现)。"""

    def __init__(self, widget, get_text):
        self.widget, self.get_text, self.tip, self.after_id = widget, get_text, None, None
        widget.bind("<Enter>", self._enter, add="+")
        widget.bind("<Leave>", self._leave, add="+")
        widget.bind("<ButtonPress>", self._leave, add="+")

    def _enter(self, _e):
        self.after_id = self.widget.after(600, self._show)

    def _leave(self, _e=None):
        if self.after_id:
            self.widget.after_cancel(self.after_id)
            self.after_id = None
        if self.tip:
            self.tip.destroy()
            self.tip = None

    def _show(self):
        text = self.get_text()
        if not text:
            return
        x = self.widget.winfo_rootx() + 14
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry("+%d+%d" % (x, y))
        tk.Label(self.tip, text=text, justify="left", bg="#111214", fg="#e8eaed",
                 font=SMALL_FONT, padx=8, pady=5, relief="solid", borderwidth=1).pack()


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.settings = self._load_settings()
        ctk.set_appearance_mode("light")   # 仅浅色主题
        ctk.set_default_color_theme("blue")
        self.T = THEMES["light"]

        self.title("视频转换器 — 老电视兼容 / 小丸压制")
        self.geometry("1160x880")
        self.minsize(1020, 760)

        self.queue: "queue.Queue[tuple]" = queue.Queue()
        self.media_q: "queue.Queue[tuple]" = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker = None
        self.enc_name = "探测中…"
        self._log_h = 160
        self._sort = None
        self.task_files: list = []          # 有序任务路径(运行顺序)
        self.task_meta: dict = {}           # path -> {"size":…,"res":…,"dur_s":…}
        self.task_states: dict = {}         # path -> 状态文本
        self._iid_by_path: dict = {}

        # ---- 所有变量(重建界面时保持) ----
        self.var_ffdir = tk.StringVar(value=self.settings.get("ffmpeg_dir", ""))
        self.var_mode = tk.StringVar(value=self.settings.get("mode", MODE_TV))
        if self.var_mode.get() not in (MODE_TV, MODE_MARUKO):
            self.var_mode.set(MODE_TV)
        self.var_style = tk.StringVar(value=self.settings.get("style", DEFAULT_STYLE_KEY))
        if self.var_style.get() not in SUB_STYLES:
            self.var_style.set(DEFAULT_STYLE_KEY)
        self.var_bitrate = tk.StringVar(value=str(self.settings.get("bitrate", 6000)))
        self.var_suffix = tk.StringVar(value=self.settings.get("suffix", ".1080p.老电视"))
        self.var_burn_sub = tk.BooleanVar(value=bool(self.settings.get("burn_sub", True)))
        self.var_enc = tk.StringVar(value=self.settings.get("enc_display", ENCODER_CHOICES[0][0]))
        self.var_crf = tk.StringVar(value=str(self.settings.get("crf", 18)))
        self.var_speed = tk.StringVar(value=self.settings.get("speed", "medium"))
        self.var_keep_res = tk.BooleanVar(value=bool(self.settings.get("keep_res", True)))
        self.var_sub_mode = tk.StringVar(value=self.settings.get("sub_mode_display", SUB_MODE_CHOICES[0][0]))
        self.var_audio = tk.StringVar(value=self.settings.get("audio_display", AUDIO_CHOICES[0][0]))
        self.var_aac_kbps = tk.StringVar(value=str(self.settings.get("aac_kbps", 192)))
        self.var_suffix_m = tk.StringVar(value=self.settings.get("suffix_m", ".小丸压制"))
        self.var_overwrite = tk.BooleanVar(value=bool(self.settings.get("overwrite", False)))
        self._enc_map = {d: v for d, v in ENCODER_CHOICES}
        self._submode_map = {d: v for d, v in SUB_MODE_CHOICES}
        self._audio_map = {d: v for d, v in AUDIO_CHOICES}

        self._build_ui()
        self._refresh_nav()
        self._switch_mode()
        self._reinsert_tasks()
        self._dnd_ok = False

        for v in (self.var_style, self.var_bitrate, self.var_suffix, self.var_burn_sub,
                  self.var_enc, self.var_crf, self.var_speed, self.var_keep_res,
                  self.var_sub_mode, self.var_audio, self.var_aac_kbps, self.var_suffix_m):
            v.trace_add("write", lambda *a: (self._validate_fields(), self._update_preview()))
        self.var_mode.trace_add("write", lambda *a: self._on_mode_change())

        self._bind_keys()
        self._setup_dnd()

        ffmpeg, _ = self._tools()
        if os.path.isfile(ffmpeg):
            threading.Thread(target=self._detect_encoder, daemon=True).start()
        threading.Thread(target=self._probe_worker, daemon=True).start()
        self.after(100, self._poll_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ================= 界面构建 =================

    def _build_ui(self) -> None:
        T = self.T
        for w in self.winfo_children():
            w.destroy()

        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Task.Treeview", background=T["card"], fieldbackground=T["card"],
                        foreground=T["fg"], rowheight=34, borderwidth=0, font=UI_FONT)
        style.configure("Task.Treeview.Heading", background=T["card"], foreground=T["muted"],
                        font=SMALL_FONT, relief="flat", padding=(8, 6))
        style.map("Task.Treeview.Heading", background=[("active", T["card"])])
        style.map("Task.Treeview", background=[("selected", T["accent"])],
                  foreground=[("selected", T["tree_sel_fg"])])

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # ---------- 左侧导航 ----------
        side = ctk.CTkFrame(self, width=228, corner_radius=0, fg_color=T["sidebar"])
        side.grid(row=0, column=0, sticky="nsw")
        side.grid_propagate(False)

        brand = ctk.CTkFrame(side, fg_color="transparent")
        brand.pack(fill="x", padx=18, pady=(18, 4))
        cv_app = tk.Canvas(brand, width=28, height=28, highlightthickness=0, bg=T["sidebar"])
        cv_app.pack(side="left")
        draw_icon(cv_app, "app", T["accent"])
        ctk.CTkLabel(brand, text="视频转换器", font=ctk.CTkFont(family="Microsoft YaHei UI", size=17, weight="bold"),
                     anchor="w").pack(side="left", padx=(10, 0))

        ctk.CTkLabel(side, text="转换模式", font=SMALL_FONT, text_color=T["muted"],
                     anchor="w").pack(fill="x", padx=20, pady=(22, 6))
        self._nav_canvases = {}
        self.nav_btns = {}
        for key, label, icon in ((MODE_TV, "老电视兼容模式", "tv"),
                                 (MODE_MARUKO, "小丸压制模式", "maruko")):
            btn = ctk.CTkButton(side, text="      " + label, anchor="w", height=40, corner_radius=R_CTRL,
                                font=UI_FONT, fg_color="transparent", hover_color=T["hover"],
                                text_color=T["fg"], command=lambda k=key: self.var_mode.set(k))
            btn.pack(fill="x", padx=12, pady=2)
            cv = tk.Canvas(btn, width=22, height=24, highlightthickness=0, bg=T["sidebar"])
            cv.place(x=14, rely=0.5, anchor="w")
            self._nav_canvases[key] = (cv, icon)
            self.nav_btns[key] = btn

        ctk.CTkFrame(side, fg_color="transparent").pack(fill="both", expand=True)

        self.lbl_enc = ctk.CTkLabel(side, text="编码器:探测中…", font=SMALL_FONT,
                                    text_color=T["muted"], anchor="w", justify="left")
        self.lbl_enc.pack(fill="x", padx=20, pady=(0, 8))

        # ---------- 主工作区 ----------
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(1, weight=1, minsize=210)
        main.bind("<Configure>", self._fit_log)
        pad = dict(padx=14, pady=(12, 0))

        # -- A. FFmpeg 环境 --
        env = ctk.CTkFrame(main, fg_color=T["card"], corner_radius=R_CARD)
        env.grid(row=0, column=0, sticky="ew", **pad)
        row = ctk.CTkFrame(env, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=10)
        self.env_dot = tk.Canvas(row, width=10, height=10, highlightthickness=0, bg=T["card"])
        self.env_dot.pack(side="left")
        self.lbl_env = ctk.CTkLabel(row, text="检测中…", font=SMALL_FONT, text_color=T["muted"])
        self.lbl_env.pack(side="left", padx=(8, 14))
        self.ent_ffdir = ctk.CTkEntry(row, textvariable=self.var_ffdir, height=H_CTRL,
                                      corner_radius=R_CTRL, font=SMALL_FONT)
        self.ent_ffdir.pack(side="left", fill="x", expand=True)
        Tooltip(self.ent_ffdir, lambda: "FFmpeg 与 ffprobe 必须在同一个目录")
        ctk.CTkButton(row, text="重新检测", width=88, height=30, corner_radius=R_CTRL, font=SMALL_FONT,
                      fg_color=T["field"], hover_color=T["hover"], text_color=T["fg"],
                      command=self._autodetect_tools).pack(side="left", padx=(10, 6))
        ctk.CTkButton(row, text="浏览…", width=76, height=30, corner_radius=R_CTRL, font=SMALL_FONT,
                      fg_color=T["field"], hover_color=T["hover"], text_color=T["fg"],
                      command=self._browse_ffdir).pack(side="left")

        # -- B. 任务列表 --
        box, body = self._card(main, None)
        box.grid(row=1, column=0, sticky="nsew", padx=14, pady=(12, 0))
        head = ctk.CTkFrame(body, fg_color="transparent")
        head.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(head, text="任务列表", font=ctk.CTkFont(family="Microsoft YaHei UI", size=13, weight="bold"),
                     anchor="w").pack(side="left")
        self.lbl_count = ctk.CTkLabel(head, text="0 个任务", font=SMALL_FONT, text_color=T["muted"])
        self.lbl_count.pack(side="left", padx=10)
        for text, cmd, w in (("清空", self._clear, 64), ("移除选中", self._remove_selected, 92),
                             ("添加文件夹", self._add_folder, 96), ("添加文件", self._add_files, 84)):
            btn = ctk.CTkButton(head, text=text, width=w, height=30, corner_radius=R_CTRL, font=SMALL_FONT,
                                fg_color=T["field"], hover_color=T["hover"], text_color=T["fg"],
                                command=cmd)
            btn.pack(side="right", padx=(6, 0))
            if text == "移除选中":
                self.btn_remove = btn

        tree_wrap = ctk.CTkFrame(body, fg_color=T["card"], corner_radius=R_CTRL)
        tree_wrap.pack(fill="both", expand=True)
        cols = ("name", "state", "res", "dur", "size", "fmt")
        self.tree = ttk.Treeview(tree_wrap, columns=cols, show="headings", selectmode="extended",
                                 style="Task.Treeview")
        for cid, text, width, anchor, stretch in (
                ("name", "文件", 420, "w", True), ("state", "状态", 90, "center", False),
                ("res", "分辨率", 110, "center", False), ("dur", "时长", 90, "center", False),
                ("size", "大小", 100, "e", False), ("fmt", "格式", 64, "center", False)):
            self.tree.heading(cid, text=text, command=lambda c=cid: self._sort_by(c))
            self.tree.column(cid, width=width, anchor=anchor, stretch=stretch)
        for tag, color in (("stripe", T["stripe"]), ("st_run", T["accent"]), ("st_ok", T["ok"]),
                           ("st_err", T["err"]), ("st_skip", T["muted"]), ("st_cancel", T["warn"]),
                           ("st_pending", T["fg"]), ("nostripe", T["card"])):
            if tag in ("stripe", "nostripe"):
                self.tree.tag_configure(tag, background=color)
            else:
                self.tree.tag_configure(tag, foreground=color)
        tree_sb = ctk.CTkScrollbar(tree_wrap, command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_sb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(2, 0), pady=2)
        tree_sb.pack(side="left", fill="y", padx=(4, 2), pady=2)
        self._tree_sb = tree_sb
        self.tree.bind("<Delete>", lambda e: self._remove_selected())
        self.tree.bind("<Double-1>", lambda e: "break")

        def _hover_path():
            iid = self.tree.identify_row(self.tree.winfo_pointery())
            return next((p for p, v in self._iid_by_path.items() if v == iid), "")
        self._tree_tooltip = Tooltip(self.tree, lambda: _hover_path() or "")

        # 空状态引导(无任务时替代表格显示,由 _sync_tasks 切换)
        self.empty_state = ctk.CTkFrame(tree_wrap, fg_color="transparent")
        film = tk.Canvas(self.empty_state, width=64, height=64, highlightthickness=0, bg=T["card"])
        film.pack(pady=(18, 8))
        draw_icon(film, "film", T["faint"])
        self.lbl_empty1 = ctk.CTkLabel(self.empty_state, text="还没有任务", font=self._f(14, "bold"))
        self.lbl_empty1.pack()
        self.lbl_empty2 = ctk.CTkLabel(
            self.empty_state,
            text=("把视频文件拖进窗口,或点击下方按钮添加" if self._dnd_active() else "点击下方按钮添加视频文件(未安装拖拽组件)"),
            font=SMALL_FONT, text_color=T["muted"])
        self.lbl_empty2.pack(pady=(2, 10))
        eb = ctk.CTkFrame(self.empty_state, fg_color="transparent")
        eb.pack()
        ctk.CTkButton(eb, text="添加文件", width=110, height=32, corner_radius=R_CTRL, font=UI_FONT,
                      fg_color=T["accent"], hover_color=T["accent_hi"], command=self._add_files).pack(side="left", padx=5)
        ctk.CTkButton(eb, text="添加文件夹", width=110, height=32, corner_radius=R_CTRL, font=UI_FONT,
                      fg_color=T["field"], hover_color=T["hover"], text_color=T["fg"],
                      command=self._add_folder).pack(side="left", padx=5)

        # -- C. 转换参数 --
        box, body = self._card(main, None)
        box.grid(row=2, column=0, sticky="ew", **pad)
        ctk.CTkLabel(body, text="转换参数", font=ctk.CTkFont(family="Microsoft YaHei UI", size=13, weight="bold"),
                     anchor="w").pack(fill="x", pady=(0, 8))

        def section_label(parent, text):
            ctk.CTkLabel(parent, text=text, font=SMALL_FONT, text_color=T["muted"], anchor="w").pack(
                fill="x", pady=(2, 4))

        self.tv_opts = ctk.CTkFrame(body, fg_color="transparent")
        row = ctk.CTkFrame(self.tv_opts, fg_color="transparent"); row.pack(fill="x")
        section_label(row, "字幕")
        ctk.CTkLabel(row, text="样式", font=UI_FONT).pack(side="left", padx=(16, 6))
        ctk.CTkOptionMenu(row, values=list(SUB_STYLES), variable=self.var_style, width=250,
                          height=H_CTRL, font=UI_FONT, dynamic_resizing=False).pack(side="left")
        ctk.CTkSwitch(row, text="烧录字幕(简中优先)", variable=self.var_burn_sub, font=UI_FONT,
                      progress_color=T["accent"]).pack(side="left", padx=(24, 0))
        row = ctk.CTkFrame(self.tv_opts, fg_color="transparent"); row.pack(fill="x", pady=(8, 0))
        section_label(row, "视频编码")
        ctk.CTkLabel(row, text="码率 kbps", font=UI_FONT).pack(side="left", padx=(16, 6))
        self.ent_bitrate = ctk.CTkEntry(row, textvariable=self.var_bitrate, width=90, height=H_CTRL,
                                        corner_radius=R_CTRL, justify="center")
        self.ent_bitrate.pack(side="left")
        self.err_bitrate = ctk.CTkLabel(row, text="", font=SMALL_FONT, text_color=T["err"])
        self.err_bitrate.pack(side="left", padx=(8, 16))
        self.lbl_enc_inline = ctk.CTkLabel(row, text="", font=SMALL_FONT, text_color=T["muted"])
        self.lbl_enc_inline.pack(side="right")
        self._out_tv = self._make_output_section(self.tv_opts, is_maruko=False)
        self._out_tv.pack(fill="x", pady=(8, 2))

        self.mk_opts = ctk.CTkFrame(body, fg_color="transparent")
        row = ctk.CTkFrame(self.mk_opts, fg_color="transparent"); row.pack(fill="x")
        section_label(row, "字幕")
        ctk.CTkLabel(row, text="处理方式", font=UI_FONT).pack(side="left", padx=(16, 6))
        ctk.CTkOptionMenu(row, values=[d for d, _ in SUB_MODE_CHOICES], variable=self.var_sub_mode,
                          width=170, height=H_CTRL, font=UI_FONT, dynamic_resizing=False).pack(side="left")
        ctk.CTkLabel(row, text="样式(烧录时生效)", font=UI_FONT).pack(side="left", padx=(24, 6))
        ctk.CTkOptionMenu(row, values=list(SUB_STYLES), variable=self.var_style, width=220,
                          height=H_CTRL, font=UI_FONT, dynamic_resizing=False).pack(side="left")
        row = ctk.CTkFrame(self.mk_opts, fg_color="transparent"); row.pack(fill="x", pady=(8, 0))
        section_label(row, "视频编码")
        ctk.CTkLabel(row, text="编码器", font=UI_FONT).pack(side="left", padx=(16, 6))
        ctk.CTkOptionMenu(row, values=[d for d, _ in ENCODER_CHOICES], variable=self.var_enc,
                          width=230, height=H_CTRL, font=UI_FONT, dynamic_resizing=False).pack(side="left")
        ctk.CTkLabel(row, text="CRF", font=UI_FONT).pack(side="left", padx=(20, 6))
        self.ent_crf = ctk.CTkEntry(row, textvariable=self.var_crf, width=64, height=H_CTRL,
                                    corner_radius=R_CTRL, justify="center")
        self.ent_crf.pack(side="left")
        self.err_crf = ctk.CTkLabel(row, text="", font=SMALL_FONT, text_color=T["err"])
        self.err_crf.pack(side="left", padx=(8, 16))
        ctk.CTkLabel(row, text="速度档", font=UI_FONT).pack(side="left", padx=(0, 6))
        ctk.CTkOptionMenu(row, values=SPEED_PRESETS, variable=self.var_speed, width=110,
                          height=H_CTRL, font=UI_FONT, dynamic_resizing=False).pack(side="left")
        ctk.CTkSwitch(row, text="保持原分辨率", variable=self.var_keep_res, font=UI_FONT,
                      progress_color=T["accent"]).pack(side="left", padx=(24, 0))
        self.lbl_enc_inline_mk = ctk.CTkLabel(self.mk_opts, text="", font=SMALL_FONT, text_color=T["muted"])
        self.lbl_enc_inline_mk.pack(anchor="w", padx=16, pady=(6, 0))
        row = ctk.CTkFrame(self.mk_opts, fg_color="transparent"); row.pack(fill="x", pady=(4, 0))
        section_label(row, "音频")
        ctk.CTkOptionMenu(row, values=[d for d, _ in AUDIO_CHOICES], variable=self.var_audio,
                          width=170, height=H_CTRL, font=UI_FONT, dynamic_resizing=False).pack(side="left", padx=(16, 10))
        ctk.CTkLabel(row, text="AAC kbps", font=UI_FONT).pack(side="left", padx=(6, 6))
        self.ent_aac = ctk.CTkEntry(row, textvariable=self.var_aac_kbps, width=64, height=H_CTRL,
                                    corner_radius=R_CTRL, justify="center")
        self.ent_aac.pack(side="left")
        self.err_aac = ctk.CTkLabel(row, text="", font=SMALL_FONT, text_color=T["err"])
        self.err_aac.pack(side="left", padx=8)
        self._out_mk = self._make_output_section(self.mk_opts, is_maruko=True)
        self._out_mk.pack(fill="x", pady=(8, 2))

        # -- D. 转换控制 --
        run = ctk.CTkFrame(main, fg_color="transparent")
        run.grid(row=3, column=0, sticky="ew", padx=14, pady=(12, 0))
        self.btn_start = ctk.CTkButton(run, text="开始转换", width=140, height=H_PRIMARY, corner_radius=9,
                                       font=ctk.CTkFont(family="Microsoft YaHei UI", size=14, weight="bold"),
                                       fg_color=T["accent"], hover_color=T["accent_hi"], command=self._start)
        self.btn_start.pack(side="left")
        self.btn_cancel = ctk.CTkButton(run, text="取消", width=84, height=H_PRIMARY, corner_radius=9,
                                        font=UI_FONT, fg_color=T["field"], hover_color=T["hover"],
                                        text_color=T["fg"], state="disabled", command=self._cancel)
        self.btn_cancel.pack(side="left", padx=10)
        self.run_dot = tk.Canvas(run, width=10, height=10, highlightthickness=0, bg=T["bg"])
        self.run_dot.pack(side="left", padx=(16, 6))
        self.var_status = tk.StringVar(value="就绪")
        ctk.CTkLabel(run, textvariable=self.var_status, font=SMALL_FONT,
                     text_color=T["muted"], anchor="w").pack(side="left", padx=(0, 16))
        self.lbl_speed = ctk.CTkLabel(run, text="", font=SMALL_FONT, text_color=T["muted"])
        self.lbl_speed.pack(side="right")
        self.progress = ctk.CTkProgressBar(run, height=10, corner_radius=6,
                                           progress_color=T["accent"], fg_color=T["field"])
        self.progress.pack(side="right", fill="x", expand=True, padx=(16, 14))
        self.progress.set(0)

        # -- E. 日志与命令(一体化面板:页签 + 操作,可折叠 / 可拖高) --
        self.log_handle = ctk.CTkFrame(main, height=7, corner_radius=2, fg_color=T["border"])
        self.log_handle.grid(row=4, column=0, sticky="ew", padx=14, pady=(12, 0))
        try:
            self.log_handle._canvas.configure(cursor="sb_v_double_arrow")
        except Exception:
            pass
        self.log_handle.bind("<Button-1>", self._log_drag_start, add="+")
        self.log_handle.bind("<B1-Motion>", self._log_drag, add="+")

        self.log_head = ctk.CTkFrame(main, fg_color="transparent")
        self.log_head.grid(row=5, column=0, sticky="ew", padx=14, pady=(10, 6))
        self.tab_log_btn = ctk.CTkButton(self.log_head, text="运行日志", width=104, height=30,
                                         corner_radius=R_CTRL, font=UI_FONT, command=lambda: self._show_tab("log"))
        self.tab_log_btn.pack(side="left")
        self.tab_cmd_btn = ctk.CTkButton(self.log_head, text="预设命令与解释", width=140, height=30,
                                         corner_radius=R_CTRL, font=UI_FONT,
                                         command=lambda: self._show_tab("cmd"))
        self.tab_cmd_btn.pack(side="left", padx=(8, 0))
        self.btn_fold = ctk.CTkButton(self.log_head, text="收起 ▴", width=72, height=30, corner_radius=R_CTRL,
                                      font=SMALL_FONT, fg_color=T["field"], hover_color=T["hover"],
                                      text_color=T["fg"], command=self._toggle_log)
        self.btn_fold.pack(side="right")
        self.btn_clear_log = ctk.CTkButton(self.log_head, text="清空日志", width=84, height=30,
                                           corner_radius=R_CTRL, font=SMALL_FONT,
                                           fg_color=T["field"], hover_color=T["hover"],
                                           text_color=T["fg"], command=self._clear_log)
        self.btn_clear_log.pack(side="right", padx=(0, 8))
        self.btn_copy = ctk.CTkButton(self.log_head, text="复制内容", width=84, height=30,
                                      corner_radius=R_CTRL, font=SMALL_FONT,
                                      fg_color=T["field"], hover_color=T["hover"],
                                      text_color=T["fg"], command=self._copy_active)
        self.btn_copy.pack(side="right", padx=(0, 8))

        self.log_wrap = ctk.CTkFrame(main, height=self._log_h, corner_radius=0, fg_color="transparent")
        self.log_wrap.grid(row=6, column=0, sticky="nsew", padx=14, pady=(0, 14))
        self.log_wrap.grid_propagate(False)
        self.log_wrap.grid_rowconfigure(0, weight=1)
        self.log_wrap.grid_columnconfigure(0, weight=1)
        panel = ctk.CTkFrame(self.log_wrap, fg_color=T["card"], corner_radius=R_CARD)
        panel.grid(row=0, column=0, sticky="nsew")
        panel.grid_rowconfigure(0, weight=1)
        panel.grid_columnconfigure(0, weight=1)
        content = ctk.CTkFrame(panel, fg_color=T["log_bg"], corner_radius=8)
        content.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(0, weight=1)

        self.log_text = ctk.CTkTextbox(content, font=ctk.CTkFont(family="Consolas", size=12),
                                       fg_color=T["log_bg"], text_color=T["log_fg"], wrap="word")
        log_inner = getattr(self.log_text, "_textbox", self.log_text)
        for tag, color in [("step", T["accent"]), ("ok", T["ok"]), ("err", T["err"]),
                           ("warn", T["warn"]), ("dim", T["muted"]), ("normal", T["log_fg"])]:
            log_inner.tag_config(tag, foreground=color)

        self.cmd_text = ctk.CTkTextbox(content, font=ctk.CTkFont(family="Consolas", size=12),
                                       fg_color=T["log_bg"], text_color=T["log_fg"], wrap="none")
        cmd_inner = getattr(self.cmd_text, "_textbox", self.cmd_text)
        cmd_inner.tag_config("h1", foreground=T["fg"], font=("Consolas", 12, "bold"))
        cmd_inner.tag_config("cmd", foreground=T["accent_hi"], lmargin1=14, lmargin2=14)
        cmd_inner.tag_config("exp", foreground=T["muted"], lmargin1=14, lmargin2=14)
        cmd_inner.tag_config("gap", font=MONO_FONT)

        self._show_tab("log")
        self._update_preview()
        self._sync_env_status()
        self._refresh_run_state()

    def _f(self, size, weight=None):
        return ctk.CTkFont(family="Microsoft YaHei UI", size=size, weight=weight)

    def _card(self, parent, title=None):
        card = ctk.CTkFrame(parent, fg_color=self.T["card"], corner_radius=R_CARD)
        if title:
            ctk.CTkLabel(card, text=title, font=self._f(13, "bold"), anchor="w").pack(
                fill="x", padx=14, pady=(10, 0))
        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=14, pady=(8, 12))
        return card, body

    def _make_output_section(self, parent, is_maruko: bool):
        """可折叠的「输出设置」:后缀 + 覆盖开关。返回折叠容器。"""
        T = self.T
        wrap = ctk.CTkFrame(parent, fg_color="transparent")
        head = ctk.CTkFrame(wrap, fg_color="transparent")
        head.pack(fill="x")
        cv = tk.Canvas(head, width=20, height=20, highlightthickness=0, bg=T["card"], cursor="hand2")
        cv._open = False
        cv.pack(side="left")
        lbl = ctk.CTkLabel(head, text="输出设置(后缀 · 覆盖)", font=UI_FONT, cursor="hand2")
        lbl.pack(side="left", padx=(2, 0))
        body = ctk.CTkFrame(wrap, fg_color="transparent")

        def toggle(_e=None):
            cv._open = not cv._open
            if cv._open:
                body.pack(fill="x", pady=(6, 0))
            else:
                body.pack_forget()
            self._draw_chevron(cv)
        for w in (cv, lbl, head):
            w.bind("<Button-1>", toggle)
        self._draw_chevron(cv)

        crow = ctk.CTkFrame(body, fg_color="transparent")
        crow.pack(fill="x", pady=2)
        suffix_var = self.var_suffix_m if is_maruko else self.var_suffix
        ctk.CTkLabel(crow, text="输出文件名后缀", font=UI_FONT).pack(side="left", padx=(16, 6))
        ctk.CTkEntry(crow, textvariable=suffix_var, width=170, height=H_CTRL,
                     corner_radius=R_CTRL).pack(side="left")
        ctk.CTkLabel(crow, text=("输出为 MKV,保存在原视频同一目录" if is_maruko
                                 else "输出为 MP4,保存在原视频同一目录"),
                     font=SMALL_FONT, text_color=T["muted"]).pack(side="left", padx=(14, 20))
        ctk.CTkSwitch(crow, text="覆盖已存在的输出文件", variable=self.var_overwrite, font=UI_FONT,
                      progress_color=T["accent"]).pack(side="right", padx=(0, 6))
        return wrap

    @staticmethod
    def _draw_chevron(cv):
        cv.delete("all")
        # 收起时箭头朝下(点击展开),展开时朝上(点击收起)
        y1, y2 = (13, 5) if cv._open else (5, 13)
        cv.create_line(6, y1, 12, y2, 18, y1, fill="#9aa0a6", width=2, joinstyle="round")

    # ================= 环境检测 =================

    def _tools(self):
        d = self.var_ffdir.get().strip()
        if d:
            return os.path.join(d, "ffmpeg.exe"), os.path.join(d, "ffprobe.exe")
        return default_tool_paths()

    def _sync_env_status(self):
        T = self.T
        ffmpeg, ffprobe = self._tools()
        ok = os.path.isfile(ffmpeg) and os.path.isfile(ffprobe)
        self.env_dot.delete("all")
        self.env_dot.create_oval(0, 0, 10, 10, fill=T["ok"] if ok else T["err"], outline="")
        self.lbl_env.configure(text="FFmpeg / FFprobe 就绪" if ok else "未找到 ffmpeg.exe / ffprobe.exe",
                               text_color=T["ok"] if ok else T["err"])

    def _browse_ffdir(self) -> None:
        d = filedialog.askdirectory(title="选择 ffmpeg 所在目录")
        if d:
            self.var_ffdir.set(d)
            self._detect_encoder_now()

    def _autodetect_tools(self) -> None:
        if not self.var_ffdir.get().strip():
            f, _ = default_tool_paths()
            if f:
                self.var_ffdir.set(str(Path(f).parent))
        self._detect_encoder_now()

    def _detect_encoder_now(self) -> None:
        self._sync_env_status()
        ffmpeg, _ = self._tools()
        if not os.path.isfile(ffmpeg):
            self.enc_name = "未就绪(未找到 ffmpeg)"
            self._refresh_enc_labels()
            return
        self.enc_name = "探测中…"
        self._refresh_enc_labels()
        threading.Thread(target=self._detect_encoder, daemon=True).start()

    def _detect_encoder(self) -> None:
        ffmpeg, _ = self._tools()
        try:
            ok = converter._nvenc_cached(ffmpeg)
        except Exception:
            ok = False
        self.enc_name = "NVIDIA 硬编 (h264_nvenc)" if ok else "软件编码 (libx264)"
        self.queue.put(("enc", None))

    def _refresh_enc_labels(self):
        T = self.T
        color = T["ok"] if "NVIDIA" in self.enc_name else (T["err"] if "未就绪" in self.enc_name else T["muted"])
        self.lbl_enc.configure(text="编码器:%s" % self.enc_name, text_color=color)
        self.lbl_enc_inline.configure(text="编码器:%s" % self.enc_name, text_color=color)
        self.lbl_enc_inline_mk.configure(text="编码器:%s" % self.enc_name, text_color=color)

    # ================= 任务列表 =================

    def _dnd_active(self) -> bool:
        return getattr(self, "_dnd_ok", False)

    def _setup_dnd(self) -> None:
        """CTk 不继承 tkinterdnd2 的混入类,把所需方法绑定到实例后注册拖放。"""
        self._dnd_ok = False
        if DND_OK:
            try:
                TD = tkinterdnd2.TkinterDnD
                TD._require(self)
                for name in ("_dnd_bind", "_substitute_dnd"):
                    setattr(self, name, getattr(TD.Tk, name).__get__(self))
                self._subst_format_str_dnd = TD.Tk._subst_format_str_dnd  # 类属性字符串
                TD.Tk.drop_target_register(self, "*")
                TD.Tk.dnd_bind(self, "<<Drop>>", self._on_drop)
                self._dnd_ok = True
            except Exception:
                self._dnd_ok = False
        if hasattr(self, "lbl_empty2"):
            self.lbl_empty2.configure(
                text=("把视频文件拖进窗口,或点击下方按钮添加" if self._dnd_ok
                      else "点击下方按钮添加视频文件(未安装拖拽组件)"))

    def _on_drop(self, event) -> None:
        data = getattr(event, "data", "")
        try:
            paths = list(data) if isinstance(data, (tuple, list)) else list(self.tk.splitlist(data))
        except Exception:
            return
        self._append_paths(paths)

    def _add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="选择视频文件",
            filetypes=[("视频文件", "*.mkv *.mp4 *.ts *.m2ts *.avi *.wmv *.flv *.mov *.webm *.mpg"),
                       ("所有文件", "*.*")])
        self._append_paths(paths)

    def _add_folder(self) -> None:
        d = filedialog.askdirectory(title="选择文件夹(取其中第一层的视频)")
        if d:
            self._append_paths([d])

    def _append_paths(self, paths) -> None:
        existing = set(self.task_files)
        added = 0
        for p in collect_videos(list(paths)):
            if p in existing:
                continue
            existing.add(p)
            self.task_files.append(p)
            self.task_states.setdefault(p, TASK_PENDING)
            meta = {"size": os.path.getsize(p) if os.path.exists(p) else None,
                    "res": "—", "dur_s": None, "probed": False}
            self.task_meta[p] = meta
            self._insert_task(p)
            self.media_q.put((p,))   # 后台探测真实媒体信息
            added += 1
        if added:
            self._sync_tasks()

    def _insert_task(self, path: str) -> str:
        meta = self.task_meta.get(path, {})
        state = self.task_states.get(path, TASK_PENDING)
        iid = self.tree.insert("", "end", values=(
            Path(path).name, state, meta.get("res", "—"),
            human_duration(meta.get("dur_s")), human_size(meta.get("size")),
            Path(path).suffix.lstrip(".").upper() or "—"),
            tags=(self._state_tag(state), "stripe" if len(self.task_files) % 2 else "nostripe"))
        self._iid_by_path[path] = iid
        return iid

    def _reinsert_tasks(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._iid_by_path = {}
        for i, p in enumerate(self.task_files):
            if i % 2:
                pass
            self._insert_task(p)
            self.tree.item(self._iid_by_path[p], tags=(self._state_tag(self.task_states.get(p, TASK_PENDING)),
                                                       "stripe" if i % 2 else "nostripe"))
        self._sync_tasks()

    def _probe_worker(self) -> None:
        while True:
            (path,) = self.media_q.get()
            meta = self.task_meta.get(path)
            if meta is None or meta.get("probed"):
                continue
            info = {"probed": True}
            try:
                ffmpeg, ffprobe = self._tools()
                data = probe_streams(ffprobe, path)
                v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
                if v:
                    info["res"] = "%s×%s" % (v.get("width", "?"), v.get("height", "?"))
                info["dur_s"] = float((data.get("format") or {}).get("duration") or 0) or None
            except Exception:
                pass  # 探测失败:保留大小,其余显示 —
            self.task_meta[path] = {**meta, **info}
            self.queue.put(("media", path))

    def _apply_media(self, path: str) -> None:
        iid = self._iid_by_path.get(path)
        if not iid or not self.tree.exists(iid):
            return
        meta = self.task_meta.get(path, {})
        cur = list(self.tree.item(iid, "values"))
        cur[2], cur[3] = meta.get("res", "—"), human_duration(meta.get("dur_s"))
        self.tree.item(iid, values=cur)

    def _state_tag(self, state: str) -> str:
        return {TASK_RUN: "st_run", TASK_OK: "st_ok", TASK_ERR: "st_err",
                TASK_SKIP: "st_skip", TASK_CANCEL: "st_cancel"}.get(state, "st_pending")

    def _set_task_state(self, path: str, state: str) -> None:
        self.task_states[path] = state
        iid = self._iid_by_path.get(path)
        if iid and self.tree.exists(iid):
            cur = list(self.tree.item(iid, "values"))
            cur[1] = state
            stripe = "stripe" if (self.tree.index(iid) % 2) else "nostripe"
            self.tree.item(iid, values=cur, tags=(self._state_tag(state), stripe))

    def _sync_tasks(self) -> None:
        n = len(self.task_files)
        self.lbl_count.configure(text="%d 个任务" % n)
        if n == 0:
            self.tree.pack_forget()
            self._tree_sb.pack_forget()
            self.empty_state.pack(fill="both", expand=True)
        else:
            self.empty_state.pack_forget()
            self.tree.pack(side="left", fill="both", expand=True, padx=(2, 0), pady=2)
            self._tree_sb.pack(side="left", fill="y", padx=(4, 2), pady=2)
        self._update_preview()

    def _remove_selected(self) -> None:
        for iid in self.tree.selection():
            vals = self.tree.item(iid, "values")
            path = next((p for p, v in self._iid_by_path.items() if v == iid), None)
            if path:
                self.task_files.remove(path)
                self.task_meta.pop(path, None)
                self.task_states.pop(path, None)
                del self._iid_by_path[path]
            self.tree.delete(iid)
        self._sync_tasks()

    def _clear(self) -> None:
        self.task_files.clear()
        self.task_meta.clear()
        self.task_states.clear()
        self._iid_by_path.clear()
        self.tree.delete(*self.tree.get_children())
        self._sync_tasks()

    def _sort_by(self, col: str) -> None:
        prev = self._sort
        desc = bool(prev and prev[0] == col and not prev[1])
        self._sort = (col, desc)

        def key(iid):
            vals = self.tree.item(iid, "values")
            path = next((p for p, v in self._iid_by_path.items() if v == iid), "")
            if col == "name":
                return vals[0].lower()
            if col == "state":
                order = [TASK_PENDING, TASK_RUN, TASK_OK, TASK_SKIP, TASK_CANCEL, TASK_ERR]
                return order.index(vals[1]) if vals[1] in order else 99
            if col == "res":
                try:
                    a, b = vals[2].replace("×", "x").split("x")
                    return int(a) * int(b)
                except Exception:
                    return 0
            if col == "dur":
                return self.task_meta.get(path, {}).get("dur_s") or 0
            if col == "size":
                return self.task_meta.get(path, {}).get("size") or 0
            return vals[5]
        items = sorted(self.tree.get_children(), key=key, reverse=desc)
        for pos, iid in enumerate(items):
            self.tree.move(iid, "", pos)
            self.tree.item(iid, tags=(self._state_tag(self.task_states.get(
                next((p for p, v in self._iid_by_path.items() if v == iid), ""), TASK_PENDING)),
                "stripe" if pos % 2 else "nostripe"))

    # ================= 模式切换 / 预览 =================

    def _on_mode_change(self) -> None:
        self._refresh_nav()
        self._switch_mode()

    def _refresh_nav(self) -> None:
        T = self.T
        for key, btn in self.nav_btns.items():
            selected = (key == self.var_mode.get())
            btn.configure(fg_color=T["sel_tint"] if selected else "transparent",
                          text_color=T["fg"] if selected else T["muted"],
                          hover_color=T["hover"] if not selected else T["sel_tint"])
            cv, icon = self._nav_canvases[key]
            cv.configure(bg=T["sidebar"])
            draw_icon(cv, icon, T["accent"] if selected else T["muted"])

    def _switch_mode(self) -> None:
        maruko = self.var_mode.get() == MODE_MARUKO
        self.tv_opts.pack_forget()
        self.mk_opts.pack_forget()
        self._out_tv.pack_forget()
        self._out_mk.pack_forget()
        if maruko:
            self.mk_opts.pack(fill="x")
            self._out_mk.pack(fill="x", pady=(8, 2))
        else:
            self.tv_opts.pack(fill="x")
            self._out_tv.pack(fill="x", pady=(8, 2))
        self._update_preview()

    def _current_opts(self) -> ConvertOptions:
        def _int(var, default):
            try:
                return int(var.get().strip())
            except ValueError:
                return default
        return ConvertOptions(
            ffmpeg=self._tools()[0], ffprobe=self._tools()[1],
            mode=self.var_mode.get(),
            style=SUB_STYLES.get(self.var_style.get(), SUB_STYLES[DEFAULT_STYLE_KEY]),
            bitrate_kbps=_int(self.var_bitrate, 6000),
            suffix=self.var_suffix.get().strip() or ".1080p.老电视",
            burn_sub=self.var_burn_sub.get(),
            enc=self._enc_map.get(self.var_enc.get(), "x264"),
            crf=_int(self.var_crf, 18),
            speed=self.var_speed.get(),
            keep_res=self.var_keep_res.get(),
            sub_mode=self._submode_map.get(self.var_sub_mode.get(), "soft"),
            audio=self._audio_map.get(self.var_audio.get(), "copy"),
            aac_kbps=_int(self.var_aac_kbps, 192),
            suffix_m=self.var_suffix_m.get().strip() or ".小丸压制",
            overwrite=self.var_overwrite.get(),
        )

    def _validate_fields(self) -> None:
        T = self.T

        def check(entry, label, var, lo, hi, msg):
            try:
                v = int(var.get().strip())
                ok = lo <= v <= hi
            except ValueError:
                ok, v = False, None
            label.configure(text="" if ok else msg)
            entry.configure(border_color=T["err"] if not ok else T["border"])
        check(self.ent_bitrate, self.err_bitrate, self.var_bitrate, 500, 50000, "需 500–50000")
        check(self.ent_crf, self.err_crf, self.var_crf, 0, 51, "需 0–51")
        check(self.ent_aac, self.err_aac, self.var_aac_kbps, 32, 512, "需 32–512")

    def _update_preview(self) -> None:
        if not hasattr(self, "cmd_text"):
            return
        rows = preview_plan(self._current_opts(), enc_note=self.enc_name)
        inner = getattr(self.cmd_text, "_textbox", self.cmd_text)
        inner.config(state="normal")
        inner.delete("1.0", "end")
        for kind, text in rows:
            inner.insert("end", text + "\n", kind if kind != "gap" else "gap")
        inner.config(state="disabled")
        n = len(self.task_files)
        self.lbl_count.configure(text="%d 个任务" % n)

    # ================= 转换控制 =================

    def _start(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        if not self.task_files:
            self.var_status.set("没有任务:请先添加视频")
            return
        ffmpeg, ffprobe = self._tools()
        if not (os.path.isfile(ffmpeg) and os.path.isfile(ffprobe)):
            messagebox.showwarning("提示", "未找到 ffmpeg.exe / ffprobe.exe\n%s" % ffmpeg)
            return
        opts = self._current_opts()
        if opts.mode == MODE_MARUKO:
            try:
                if not (0 <= int(self.var_crf.get().strip()) <= 51):
                    raise ValueError
            except ValueError:
                self.var_status.set("CRF 参数无效(0–51)")
                return
        else:
            try:
                if not (500 <= int(self.var_bitrate.get().strip()) <= 50000):
                    raise ValueError
            except ValueError:
                self.var_status.set("码率参数无效(500–50000)")
                return
        self._save_settings()
        self.cancel_event.clear()
        self.btn_start.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.progress.set(0)
        self.lbl_speed.configure(text="")
        self.var_status.set("准备中…")
        self.worker = threading.Thread(target=self._worker, args=(list(self.task_files), opts), daemon=True)
        self.worker.start()

    def _worker(self, files, opts: ConvertOptions) -> None:
        total = len(files)

        def log(msg):
            self.queue.put(("log", msg))

        def make_progress(idx):
            def cb(p, info=None):
                overall = (idx + p) / total if total else p
                self.queue.put(("progress", (overall, info)))
            return cb

        done = failed = 0
        for i, f in enumerate(files):
            if self.cancel_event.is_set():
                break
            idx = i
            self.queue.put(("file", (idx, i + 1, total, Path(f).name)))
            self.queue.put(("task", (f, TASK_RUN)))
            dst = output_path_for(f, opts)
            if dst.exists() and not opts.overwrite:
                log("跳过:输出文件已存在(%s)" % dst.name)
                self.queue.put(("task", (f, TASK_SKIP)))
                continue
            try:
                convert_file(f, opts, log=log, progress=make_progress(idx), cancel_event=self.cancel_event)
                done += 1
                self.queue.put(("task", (f, TASK_OK)))
            except CancelledError:
                self.queue.put(("task", (f, TASK_CANCEL)))
                log("已取消当前文件")
                break
            except Exception as e:
                failed += 1
                self.queue.put(("task", (f, TASK_ERR)))
                log("失败:%s" % e)
        self.queue.put(("done", (done, failed, total)))

    def _cancel(self) -> None:
        if self.worker is None or not self.worker.is_alive():
            return
        self.cancel_event.set()
        self.var_status.set("正在取消(等待 ffmpeg 退出)…")

    def _refresh_run_state(self) -> None:
        dot = self.run_dot
        dot.delete("all")
        running = self.worker is not None and self.worker.is_alive()
        dot.create_oval(0, 0, 10, 10, fill=self.T["accent"] if running else self.T["faint"], outline="")

    # ================= 日志 =================

    def _show_tab(self, name: str) -> None:
        T = self.T
        self._active_tab = name
        for btn, key in ((self.tab_log_btn, "log"), (self.tab_cmd_btn, "cmd")):
            sel = (key == name)
            btn.configure(fg_color=T["accent_soft"] if sel else "transparent",
                          text_color=T["accent"] if sel else T["muted"],
                          hover_color=T["accent_soft"] if sel else T["hover"])
        if name == "log":
            self.cmd_text.grid_remove()
            self.log_text.grid(row=0, column=0, sticky="nsew")
        else:
            self.log_text.grid_remove()
            self.cmd_text.grid(row=0, column=0, sticky="nsew")
        self.btn_clear_log.configure(state="normal" if name == "log" else "disabled")

    def _copy_active(self) -> None:
        tb = self.log_text if getattr(self, "_active_tab", "log") == "log" else self.cmd_text
        inner = getattr(tb, "_textbox", tb)
        text = inner.get("1.0", "end").strip()
        if not text:
            self.var_status.set("当前页没有可复制的内容")
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.var_status.set("已复制到剪贴板(%d 字符)" % len(text))

    def _clear_log(self) -> None:
        if getattr(self, "_active_tab", "log") != "log":
            return
        inner = getattr(self.log_text, "_textbox", self.log_text)
        inner.delete("1.0", "end")

    def _toggle_log(self) -> None:
        if self.log_wrap.winfo_ismapped():
            self.log_wrap.grid_remove()
            self.log_handle.grid_remove()
            self.btn_fold.configure(text="展开 ▾")
        else:
            self.log_wrap.grid()
            self.log_handle.grid()
            self.btn_fold.configure(text="收起 ▴")

    def _log_drag_start(self, e) -> None:
        self._drag_y = e.y_root
        self._drag_h = self._log_h

    def _log_drag(self, e) -> None:
        dy = self._drag_y - e.y_root
        max_h = max(140, int(self.winfo_height() * 0.6))
        self._log_h = max(120, min(self._drag_h + dy, max_h))
        self.log_wrap.configure(height=self._log_h)

    def _fit_log(self, event=None) -> None:
        """窗口变矮时压缩日志区(下限 130px),保证任务列表不被挤没。"""
        if not hasattr(self, "log_wrap"):
            return
        limit = max(130, int(self.winfo_height() * 0.38))
        self.log_wrap.configure(height=min(self._log_h, limit))

    def _log_line(self, tag: str, msg: str) -> None:
        inner = getattr(self.log_text, "_textbox", self.log_text)
        inner.insert("end", msg + "\n", tag)
        inner.see("end")

    def _log_classified(self, msg: str) -> None:
        if msg.startswith("转换:"):
            tag = "step"
        elif msg.startswith("失败"):
            tag = "err"
        elif msg.startswith("警告"):
            tag = "warn"
        elif msg.startswith("  完成") or msg.startswith("结束"):
            tag = "ok"
        elif msg.startswith("跳过") or msg.strip().startswith("·"):
            tag = "dim"
        else:
            tag = "normal"
        self._log_line(tag, msg)

    # ================= 轮询 =================

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "log":
                    self._log_classified(str(payload))
                elif kind == "progress":
                    pct, info = payload
                    self.progress.set(float(pct))
                    if info:
                        parts = []
                        if info.get("speed"):
                            parts.append("速度 %s" % info["speed"])
                        if info.get("fps"):
                            parts.append("%s fps" % info["fps"])
                        self.lbl_speed.configure(text=" · ".join(parts))
                elif kind == "file":
                    idx, i, total, name = payload
                    self.var_status.set("转换中 %d/%d · %s" % (i, total, name))
                elif kind == "task":
                    path, state = payload
                    self._set_task_state(path, state)
                elif kind == "media":
                    self._apply_media(payload)
                elif kind == "enc":
                    self._refresh_enc_labels()
                    self._update_preview()
                elif kind == "done":
                    done, failed, total = payload
                    self.progress.set(1.0)
                    if failed == 0:
                        self.var_status.set("完成:成功 %d / %d" % (done, total))
                    else:
                        self.var_status.set("结束:成功 %d,失败 %d(共 %d)" % (done, failed, total))
        except queue.Empty:
            pass
        if self.worker is not None and not self.worker.is_alive():
            self.worker = None
            self.btn_start.configure(state="normal")
            self.btn_cancel.configure(state="disabled")
            self.lbl_speed.configure(text="")
            self._refresh_run_state()
        self.after(100, self._poll_queue)

    # ================= 快捷键 / 拖拽 =================

    def _bind_keys(self) -> None:
        self.bind("<Control-o>", lambda e: self._add_files())
        self.bind("<Control-O>", lambda e: self._add_files())
        self.bind("<Control-Return>", lambda e: self._start())
        self.bind("<Escape>", lambda e: self._cancel())
        self.bind("<Control-Key-1>", lambda e: self.var_mode.set(MODE_TV))
        self.bind("<Control-Key-2>", lambda e: self.var_mode.set(MODE_MARUKO))

    # ================= 设置持久化 =================

    def _load_settings(self) -> dict:
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_settings(self) -> None:
        data = {
            "ffmpeg_dir": self.var_ffdir.get().strip(),
            "mode": self.var_mode.get(),
            "style": self.var_style.get(),
            "bitrate": self.var_bitrate.get().strip(),
            "suffix": self.var_suffix.get().strip(),
            "burn_sub": self.var_burn_sub.get(),
            "enc_display": self.var_enc.get(),
            "crf": self.var_crf.get().strip(),
            "speed": self.var_speed.get(),
            "keep_res": self.var_keep_res.get(),
            "sub_mode_display": self.var_sub_mode.get(),
            "audio_display": self.var_audio.get(),
            "aac_kbps": self.var_aac_kbps.get().strip(),
            "suffix_m": self.var_suffix_m.get().strip(),
            "overwrite": self.var_overwrite.get(),
        }
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _on_close(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            if not messagebox.askyesno("退出", "正在转换,确定退出吗?"):
                return
            self.cancel_event.set()
        self._save_settings()
        self.destroy()


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
