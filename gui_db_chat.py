import threading
import tkinter as tk
import re
import json
from datetime import datetime
from pathlib import Path
import time
import csv
import sqlite3
import os
from tkinter import filedialog, messagebox

try:
    import psycopg2
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "psycopg2 is not installed. Install it with: pip install psycopg2-binary"
    ) from exc

from config import build_psycopg2_dsn, load_db_config, CONFIG_PATH
from llm_pipeline import LLMPipeline


class MultiSeriesTimeChart(tk.Frame):
    """Reusable chart: one system view, many metric lines."""

    def __init__(
        self,
        parent: tk.Widget,
        title: str,
        series_colors: dict[str, str],
        show_legend: bool = True,
    ) -> None:
        super().__init__(parent, bd=1, relief=tk.GROOVE)
        self.series_colors = series_colors
        self.show_legend = show_legend
        self.title_label = tk.Label(self, text=title, anchor="w", font=("Segoe UI", 10, "bold"))
        self.title_label.pack(fill=tk.X, padx=8, pady=(6, 2))

        self.value_label = tk.Label(self, text="No data yet", anchor="w", font=("Consolas", 10))
        self.value_label.pack(fill=tk.X, padx=8, pady=(0, 4))

        body = tk.Frame(self)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        self.legend_frame = tk.Frame(body)
        if self.show_legend:
            self.legend_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8))

        self.canvas = tk.Canvas(body, bg="#FFFFFF", highlightthickness=0)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        if self.show_legend:
            self._render_legend()

        self.series_points: dict[str, list[float]] = {name: [] for name in series_colors}
        self.time_points: list[float] = []
        self.max_points = 120
        self.selection_overlay_ratio: tuple[float, float] | None = None
        self.canvas.bind("<Configure>", lambda _: self.redraw())

    def _render_legend(self) -> None:
        for metric_name, color in self.series_colors.items():
            row = tk.Frame(self.legend_frame)
            row.pack(fill=tk.X, pady=1)
            marker = tk.Canvas(row, width=14, height=10, highlightthickness=0)
            marker.create_line(1, 5, 13, 5, fill=color, width=2)
            marker.pack(side=tk.LEFT)
            tk.Label(row, text=metric_name, anchor="w", font=("Consolas", 9)).pack(side=tk.LEFT, padx=(4, 0))

    def set_series_points(
        self,
        series_points: dict[str, list[float]],
        time_points: list[float] | None = None,
    ) -> None:
        self.series_points = {
            name: series_points.get(name, [])[-self.max_points :]
            for name in self.series_colors
        }
        if time_points is None:
            max_len = max((len(points) for points in self.series_points.values()), default=0)
            self.time_points = list(range(max_len))
        else:
            self.time_points = time_points[-self.max_points :]
        self.redraw()

    def set_status_text(self, text: str) -> None:
        self.value_label.configure(text=text)

    def set_selection_overlay(self, left_ratio: float | None, right_ratio: float | None) -> None:
        if left_ratio is None or right_ratio is None:
            self.selection_overlay_ratio = None
        else:
            left = max(0.0, min(1.0, float(left_ratio)))
            right = max(0.0, min(1.0, float(right_ratio)))
            if right < left:
                left, right = right, left
            self.selection_overlay_ratio = (left, right)
        self.redraw()

    def redraw(self) -> None:
        self.canvas.delete("all")
        width = max(self.canvas.winfo_width(), 1)
        height = max(self.canvas.winfo_height(), 1)

        self.canvas.create_rectangle(1, 1, width - 1, height - 1, outline="#D0D0D0")
        max_len = max((len(points) for points in self.series_points.values()), default=0)
        if max_len < 2:
            self.canvas.create_text(
                width // 2,
                height // 2,
                text="Waiting for samples...",
                fill="#808080",
                font=("Segoe UI", 9),
            )
            return

        all_values = [value for points in self.series_points.values() for value in points]
        if not all_values:
            return
        raw_min = min(all_values)
        raw_max = max(all_values)
        y_min = min(0.0, raw_min)
        y_max = max(1.0, raw_max * 1.2)
        if y_max <= y_min:
            y_max = y_min + 1.0
        span = y_max - y_min

        plot_left = 56
        plot_right = width - 10
        plot_top = 10
        plot_bottom = height - 24
        x_span = max(plot_right - plot_left, 1)
        y_span = max(plot_bottom - plot_top, 1)

        if self.selection_overlay_ratio is not None:
            left_ratio, right_ratio = self.selection_overlay_ratio
            x_left = plot_left + left_ratio * x_span
            x_right = plot_left + right_ratio * x_span
            self.canvas.create_rectangle(
                x_left,
                plot_top,
                x_right,
                plot_bottom,
                fill="#000000",
                outline="",
                stipple="gray25",
            )
            self.canvas.create_line(x_left, plot_top, x_left, plot_bottom, fill="#000000", width=1)
            self.canvas.create_line(x_right, plot_top, x_right, plot_bottom, fill="#000000", width=1)

        tick_count = 5
        for i in range(tick_count + 1):
            ratio = i / tick_count
            y = plot_bottom - (ratio * y_span)
            tick_value = y_min + (ratio * span)
            self.canvas.create_line(plot_left, y, plot_right, y, fill="#F0F0F0")
            self.canvas.create_line(plot_left - 4, y, plot_left, y, fill="#808080")
            self.canvas.create_text(
                plot_left - 8,
                y,
                text=f"{tick_value:.0f}",
                anchor="e",
                fill="#666666",
                font=("Segoe UI", 8),
            )

        self.canvas.create_line(plot_left, plot_top, plot_left, plot_bottom, fill="#808080")

        for metric_name, points in self.series_points.items():
            if len(points) < 2:
                continue
            coords: list[float] = []
            for idx, value in enumerate(points):
                x = plot_left + (idx / (len(points) - 1)) * x_span
                norm = (value - y_min) / span
                y = plot_bottom - (norm * y_span)
                coords.extend([x, y])
            self.canvas.create_line(*coords, fill=self.series_colors[metric_name], width=2, smooth=True)

        for idx in range(0, max_len, 10):
            x = plot_left + (idx / (max_len - 1)) * x_span
            self.canvas.create_line(x, plot_bottom, x, plot_bottom + 4, fill="#808080")
            label = self._format_clock_time(self.time_points[idx] if idx < len(self.time_points) else 0.0)
            self.canvas.create_text(
                x,
                plot_bottom + 12,
                text=label,
                anchor="n",
                fill="#666666",
                font=("Segoe UI", 8),
            )

        if (max_len - 1) % 10 != 0:
            last_idx = max_len - 1
            x = plot_left + (last_idx / (max_len - 1)) * x_span
            self.canvas.create_line(x, plot_bottom, x, plot_bottom + 4, fill="#808080")
            label = self._format_clock_time(self.time_points[last_idx] if last_idx < len(self.time_points) else 0.0)
            self.canvas.create_text(
                x,
                plot_bottom + 12,
                text=label,
                anchor="n",
                fill="#666666",
                font=("Segoe UI", 8),
            )

        self.canvas.create_text(plot_left, height - 10, anchor="w", text=f"min {raw_min:.2f}", fill="#666666")
        self.canvas.create_text(plot_right, height - 10, anchor="e", text=f"max {raw_max:.2f}", fill="#666666")

    @staticmethod
    def _format_clock_time(unix_timestamp: float) -> str:
        return datetime.fromtimestamp(unix_timestamp).strftime("%H:%M:%S")


class DBChatApp:
    MAX_PREVIEW_ROWS = 30
    MAX_CELL_WIDTH = 40
    MAX_TABLE_CACHE_SIZE = 5000
    MAX_COLUMN_CACHE_SIZE = 5000
    METRICS_DB_PATH = Path(__file__).with_name("metrics_history.sqlite3")
    EXPORT_FORMAT_TAG = "pg_diag_metrics_v1"
    DB_CONFIG_PATH = CONFIG_PATH
    PINNED_TOP_METRIC_VIEWS = ("pg_stat_activity (active sessions)",)
    ACTIVITY_VIEW_NAME = "pg_stat_activity (active sessions)"

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("PostgreSQL Chat")
        self.root.geometry("900x600")

        self.db_config = load_db_config()
        # Start in offline by default; runtime collection must be explicitly enabled.
        self.offline_mode = self._config_bool(self.db_config.get("offline_mode", True))
        self.db_config["offline_mode"] = self.offline_mode
        self.dsn = build_psycopg2_dsn(self.db_config)
        self.command_history: list[str] = []
        self.history_index: int | None = None
        self.history_current_input = ""
        self.suggestion_window: tk.Toplevel | None = None
        self.suggestion_listbox: tk.Listbox | None = None
        self.autocomplete_active = False
        self.autocomplete_context = ""
        self.autocomplete_base_prefix = ""
        self.cached_tables: list[str] = []
        self.cached_columns_by_table: dict[str, list[str]] = {}
        self.cached_columns_any: list[str] = []
        self.metrics_db_conn: sqlite3.Connection | None = None
        self.dialog_mode = tk.StringVar(value="DB")

        self.metric_configs = self._build_metric_configs()
        self.metric_states: dict[str, dict] = {}
        self.metric_charts: dict[str, MultiSeriesTimeChart] = {}
        self.metric_visibility_vars: dict[str, tk.BooleanVar] = {}
        self.metrics_debug_enabled = str(os.environ.get("RPG_PIPELINE_DEBUG", "false")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self.llm_pipeline = LLMPipeline(
            sqlite_path=self.METRICS_DB_PATH,
            dotenv_path=Path(__file__).with_name(".env"),
        )
        self._init_metrics_db()
        self._truncate_metrics_data()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close_app)

        paned = tk.PanedWindow(root, orient=tk.HORIZONTAL, sashrelief=tk.RAISED, sashwidth=6)
        paned.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 5))

        left_panel = tk.Frame(paned)
        left_panel.grid_rowconfigure(0, weight=0)
        left_panel.grid_rowconfigure(1, weight=1)
        left_panel.grid_rowconfigure(2, weight=0)
        left_panel.grid_columnconfigure(0, weight=1)

        right_panel = tk.Frame(paned, bd=1, relief=tk.GROOVE)
        right_panel.grid_rowconfigure(3, weight=1)
        right_panel.grid_columnconfigure(0, weight=1)

        paned.add(left_panel, minsize=360, stretch="always")
        paned.add(right_panel, minsize=300, stretch="always")
        self.root.update_idletasks()
        total_width = max(paned.winfo_width(), 1)
        paned.sash_place(0, int(total_width * 0.22), 0)

        toolbar_frame = tk.Frame(left_panel)
        toolbar_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=(0, 6))
        toolbar_frame.grid_columnconfigure(0, weight=1)
        self.settings_button = tk.Button(toolbar_frame, text="Settings", width=12, command=self.open_settings_window)
        self.settings_button.grid(row=0, column=1, sticky="e")
        self.connection_mode_button = tk.Button(toolbar_frame, width=14, command=self.toggle_offline_mode)
        self.connection_mode_button.grid(row=0, column=2, sticky="e", padx=(8, 0))
        self._sync_connection_mode_button()

        history_frame = tk.Frame(left_panel)
        history_frame.grid(row=1, column=0, sticky="nsew")

        y_scrollbar = tk.Scrollbar(history_frame, orient=tk.VERTICAL)
        x_scrollbar = tk.Scrollbar(history_frame, orient=tk.HORIZONTAL)

        self.chat_area = tk.Text(
            history_frame,
            wrap=tk.NONE,
            state=tk.DISABLED,
            font=("Consolas", 11),
            xscrollcommand=x_scrollbar.set,
            yscrollcommand=y_scrollbar.set,
        )
        self.chat_area.grid(row=0, column=0, sticky="nsew")

        y_scrollbar.configure(command=self.chat_area.yview)
        y_scrollbar.grid(row=0, column=1, sticky="ns")

        x_scrollbar.configure(command=self.chat_area.xview)
        x_scrollbar.grid(row=1, column=0, sticky="ew")

        history_frame.grid_rowconfigure(0, weight=1)
        history_frame.grid_columnconfigure(0, weight=1)
        self.chat_area.bind("<Control-c>", self._copy_chat_selection)
        self.chat_area.bind("<Control-C>", self._copy_chat_selection)

        input_frame = tk.Frame(left_panel)
        input_frame.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 10))
        input_frame.grid_columnconfigure(0, weight=1)
        input_frame.grid_rowconfigure(0, weight=1)
        input_frame.grid_rowconfigure(1, weight=0)

        self.input_box = tk.Text(input_frame, height=4, wrap=tk.WORD, font=("Consolas", 11))
        self.input_box.grid(row=0, column=0, sticky="nsew")
        self.input_box.bind("<Return>", self._on_enter)
        self.input_box.bind("<Shift-Return>", self._on_shift_enter)
        self.input_box.bind("<Control-v>", self._paste_into_input)
        self.input_box.bind("<Control-V>", self._paste_into_input)
        self.input_box.bind("<Up>", self._history_up)
        self.input_box.bind("<Down>", self._history_down)
        self.input_box.bind("<Tab>", self._on_tab_complete)
        self.input_box.bind("<Control-space>", self._on_ctrl_space_complete)
        self.input_box.bind("<Escape>", self._close_suggestions)
        self.input_box.bind("<KeyRelease>", self._on_input_key_release)
        self._configure_input_highlighting()

        self.buttons_frame = tk.Frame(input_frame)
        self.buttons_frame.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.button_wrap_padding = 8
        self.button_row_padding = 4
        self.button_max_width_ratio = 1.0
        self._buttons_layout_scheduled = False
        self._last_buttons_layout_signature: tuple | None = None

        self.mode_button = tk.Button(
            self.buttons_frame,
            text="Mode: БД",
            width=12,
            bg="#1E8449",
            fg="#FFFFFF",
            activebackground="#196F3D",
            activeforeground="#FFFFFF",
            command=self.toggle_dialog_mode,
        )
        self.llm_model_button = tk.Button(
            self.buttons_frame,
            text="LLM: chat",
            width=12,
            bg="#6C3483",
            fg="#FFFFFF",
            activebackground="#5B2C6F",
            activeforeground="#FFFFFF",
            command=self.toggle_llm_model,
        )
        self.send_button = tk.Button(self.buttons_frame, text="Send", width=12, command=self.send_query)
        self.refresh_cache_button = tk.Button(
            self.buttons_frame, text="Refresh cache", width=14, command=self.refresh_metadata_cache
        )
        self.export_button = tk.Button(self.buttons_frame, text="Export CSV", width=12, command=self.export_metrics_csv)
        self.import_button = tk.Button(self.buttons_frame, text="Import CSV", width=12, command=self.import_metrics_csv)
        self._request_layout_control_buttons()
        input_frame.bind("<Configure>", lambda _: self._request_layout_control_buttons())

        monitor_title = tk.Label(right_panel, text="Metrics", anchor="w", font=("Segoe UI", 10, "bold"))
        monitor_title.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))

        selector_frame = tk.Frame(right_panel)
        selector_frame.grid(row=1, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 6))
        selector_frame.grid_columnconfigure(0, weight=1)

        tk.Label(selector_frame, text="Show charts:", anchor="w", font=("Segoe UI", 9)).grid(
            row=0, column=0, sticky="w"
        )
        toggles_frame = tk.Frame(selector_frame)
        toggles_frame.grid(row=1, column=0, sticky="ew", pady=(2, 0))

        for idx, metric in enumerate(self.metric_configs):
            view_name = metric["view"]
            visible_var = tk.BooleanVar(value=True)
            self.metric_visibility_vars[view_name] = visible_var
            toggle = tk.Checkbutton(
                toggles_frame,
                text=view_name,
                variable=visible_var,
                command=lambda name=view_name: self._toggle_metric_chart_visibility(name),
                anchor="w",
            )
            toggle.grid(row=idx // 2, column=idx % 2, sticky="w", padx=(0, 16))

        timeline_frame = tk.Frame(right_panel)
        timeline_frame.grid(row=2, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 6))
        timeline_frame.grid_columnconfigure(0, weight=1)

        self.timeline_left_position = tk.IntVar(value=0)
        self.timeline_right_position = tk.IntVar(value=0)
        self.timeline_max_offset = 0
        self.timeline_default_span = 10
        self._timeline_range_initialized = False
        self._timeline_active_handle: str | None = None
        self._timeline_drag_start_x = 0
        self._timeline_drag_left_start = 0
        self._timeline_drag_right_start = 0
        self.timeline_label = tk.Label(timeline_frame, text="Timeline: latest", anchor="w")
        self.timeline_label.grid(row=0, column=0, sticky="w")

        self.timeline_canvas = tk.Canvas(timeline_frame, height=30, highlightthickness=0, bg="#FFFFFF")
        self.timeline_canvas.grid(row=1, column=0, sticky="ew")
        self.timeline_canvas.bind("<Configure>", self._on_timeline_canvas_configure)
        self.timeline_canvas.bind("<Button-1>", self._on_timeline_canvas_press)
        self.timeline_canvas.bind("<B1-Motion>", self._on_timeline_canvas_drag)
        self.timeline_canvas.bind("<ButtonRelease-1>", self._on_timeline_canvas_release)
        self.timeline_hint_label = tk.Label(
            timeline_frame,
            text="Drag left/right handles on one axis",
            anchor="w",
            font=("Segoe UI", 8),
        )
        self.timeline_hint_label.grid(row=2, column=0, sticky="w")

        metrics_canvas = tk.Canvas(right_panel, highlightthickness=0)
        metrics_canvas.grid(row=3, column=0, sticky="nsew", padx=(8, 0), pady=(0, 8))
        metrics_scroll = tk.Scrollbar(right_panel, orient=tk.VERTICAL, command=metrics_canvas.yview)
        metrics_scroll.grid(row=3, column=1, sticky="ns", pady=(0, 8))
        metrics_canvas.configure(yscrollcommand=metrics_scroll.set)

        self.metrics_frame = tk.Frame(metrics_canvas)
        metrics_window = metrics_canvas.create_window((0, 0), window=self.metrics_frame, anchor="nw")

        def _sync_metrics_scroll_region(_: tk.Event) -> None:
            metrics_canvas.configure(scrollregion=metrics_canvas.bbox("all"))

        def _sync_metrics_frame_width(_: tk.Event) -> None:
            metrics_canvas.itemconfigure(metrics_window, width=metrics_canvas.winfo_width())

        self.metrics_frame.bind("<Configure>", _sync_metrics_scroll_region)
        metrics_canvas.bind("<Configure>", _sync_metrics_frame_width)
        metrics_canvas.bind("<MouseWheel>", lambda event: self._on_metrics_mousewheel(event, metrics_canvas))
        metrics_canvas.bind("<Button-4>", lambda event: self._on_metrics_mousewheel(event, metrics_canvas))
        metrics_canvas.bind("<Button-5>", lambda event: self._on_metrics_mousewheel(event, metrics_canvas))
        self.metrics_frame.bind("<MouseWheel>", lambda event: self._on_metrics_mousewheel(event, metrics_canvas))
        self.metrics_frame.bind("<Button-4>", lambda event: self._on_metrics_mousewheel(event, metrics_canvas))
        self.metrics_frame.bind("<Button-5>", lambda event: self._on_metrics_mousewheel(event, metrics_canvas))

        for metric in self.metric_configs:
            chart = MultiSeriesTimeChart(
                self.metrics_frame,
                metric["view"],
                metric["colors"],
                show_legend=(metric["view"] != self.ACTIVITY_VIEW_NAME),
            )
            chart.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
            self.metric_charts[metric["view"]] = chart
            self.metric_states[metric["view"]] = {
                "prev": {},
                "available": True,
            }
        self._refresh_metric_chart_visibility()

        slider_frame = tk.Frame(right_panel)
        slider_frame.grid(row=4, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 10))
        slider_frame.grid_columnconfigure(0, weight=1)

        self.poll_interval_seconds = tk.IntVar(value=1)
        self.poll_label = tk.Label(slider_frame, text="Poll interval: 1 sec", anchor="w")
        self.poll_label.grid(row=0, column=0, sticky="w")

        self.poll_slider = tk.Scale(
            slider_frame,
            from_=1,
            to=1800,
            orient=tk.HORIZONTAL,
            resolution=1,
            showvalue=False,
            variable=self.poll_interval_seconds,
            command=self._on_poll_interval_changed,
        )
        self.poll_slider.grid(row=1, column=0, sticky="ew")

        self._append_server_message(
            "Connected config loaded. Enter SQL and press Enter to execute.\n"
            "Tip: Shift+Enter inserts a new line."
        )
        if self.offline_mode:
            self._append_server_message("Offline mode enabled: running without PostgreSQL connection.")
        self._capture_session_pg_settings()
        # Do not block UI startup with immediate synchronous polling.
        self.root.after(50, self._poll_all_metrics)

    def _on_shift_enter(self, event: tk.Event) -> str:
        return ""

    def _on_enter(self, event: tk.Event) -> str:
        if self.suggestion_window and self.suggestion_listbox and self.suggestion_listbox.size() > 0:
            self._accept_suggestion()
            return "break"
        self.send_query()
        return "break"

    def _on_ctrl_space_complete(self, event: tk.Event) -> str:
        return self._trigger_autocomplete()

    def _copy_chat_selection(self, event: tk.Event) -> str:
        try:
            selected = self.chat_area.get(tk.SEL_FIRST, tk.SEL_LAST)
        except tk.TclError:
            return "break"

        self.root.clipboard_clear()
        self.root.clipboard_append(selected)
        return "break"

    def _paste_into_input(self, event: tk.Event) -> str:
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            return "break"

        self.input_box.insert(tk.INSERT, text)
        self._apply_sql_highlighting()
        return "break"

    def _history_up(self, event: tk.Event) -> str:
        if self.suggestion_window and self.suggestion_listbox and self.suggestion_listbox.size() > 0:
            self._move_suggestion_selection(-1)
            return "break"

        if not self.command_history:
            return "break"

        if self.history_index is None:
            self.history_current_input = self.input_box.get("1.0", tk.END).rstrip("\n")
            self.history_index = len(self.command_history) - 1
        elif self.history_index > 0:
            self.history_index -= 1

        self._set_input_text(self.command_history[self.history_index])
        return "break"

    def _history_down(self, event: tk.Event) -> str:
        if self.suggestion_window and self.suggestion_listbox and self.suggestion_listbox.size() > 0:
            self._move_suggestion_selection(1)
            return "break"

        if self.history_index is None:
            return "break"

        if self.history_index < len(self.command_history) - 1:
            self.history_index += 1
            self._set_input_text(self.command_history[self.history_index])
        else:
            self.history_index = None
            self._set_input_text(self.history_current_input)
            self.history_current_input = ""
        return "break"

    def _set_input_text(self, text: str) -> None:
        self.input_box.delete("1.0", tk.END)
        self.input_box.insert("1.0", text)
        self.input_box.mark_set(tk.INSERT, tk.END)
        self._apply_sql_highlighting()

    def _on_tab_complete(self, event: tk.Event) -> str:
        return self._trigger_autocomplete()

    def _trigger_autocomplete(self) -> str:
        # If suggestions are already shown, Tab inserts the selected one.
        if self.suggestion_window and self.suggestion_listbox and self.suggestion_listbox.size() > 0:
            self._accept_suggestion()
            return "break"

        query = self.input_box.get("1.0", tk.END).rstrip("\n")
        cursor_idx = self.input_box.index(tk.INSERT)
        before_cursor = self.input_box.get("1.0", cursor_idx)

        suggestions = self._get_autocomplete_suggestions(query, before_cursor)
        if not suggestions:
            return "break"

        self.autocomplete_active = True
        if len(suggestions) == 1:
            self._replace_current_token(suggestions[0])
            self.autocomplete_active = False
            return "break"

        self._show_suggestions_popup(suggestions)
        return "break"

    def _on_input_key_release(self, event: tk.Event) -> None:
        self._apply_sql_highlighting()
        if not self.autocomplete_active or not self.suggestion_listbox:
            return
        if event.keysym in {"Up", "Down", "Return", "Tab", "Escape", "Shift_L", "Shift_R", "Control_L", "Control_R"}:
            return

        before_cursor = self.input_box.get("1.0", tk.INSERT)
        token_match = re.search(r"([A-Za-z_][A-Za-z0-9_]*)?$", before_cursor)
        current_prefix = (token_match.group(1) if token_match else "") or ""
        suggestions = self._suggestions_from_cache(current_prefix)
        if not suggestions:
            self._close_suggestions()
            return
        self._update_suggestions_popup(suggestions)

    def _get_autocomplete_suggestions(self, full_query: str, before_cursor: str) -> list[str]:
        match = re.search(r"([A-Za-z_][A-Za-z0-9_]*)?$", before_cursor)
        prefix = (match.group(1) if match else "") or ""
        context_kw = self._detect_context_clause(before_cursor, prefix)
        self.autocomplete_context = context_kw
        self.autocomplete_base_prefix = prefix

        keyword_suggestions = self._keyword_suggestions(prefix)

        # table.column style completion
        table_dot_match = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)?$", before_cursor)
        if table_dot_match:
            table_name = table_dot_match.group(1)
            col_prefix = table_dot_match.group(2) or ""
            columns = self._fetch_columns([table_name], "")
            self.cached_columns_by_table[table_name.lower()] = columns
            return self._filter_cached_values(columns, col_prefix)

        if context_kw in {"from", "join", "update", "into", "table"}:
            table_names = self._get_cached_tables()
            table_matches = self._filter_cached_values(table_names, prefix)
            return table_matches if table_matches else keyword_suggestions

        if context_kw in {"where", "and", "or", "set", "orderby"}:
            table_names = self._extract_tables_from_query(full_query)
            cols = self._get_cached_columns(table_names) if table_names else self._get_cached_columns_any()
            col_matches = self._filter_cached_values(cols, prefix)
            return col_matches if col_matches else keyword_suggestions

        # Generic fallback: commands + table names from schema.
        return self._merge_suggestions(keyword_suggestions, self._filter_cached_values(self._get_cached_tables(), prefix))

    @staticmethod
    def _detect_context_clause(before_cursor: str, prefix: str) -> str:
        # Analyze the text before the token being completed.
        search_text = before_cursor[:-len(prefix)] if prefix else before_cursor
        lower_text = search_text.lower()

        clause_patterns = [
            ("orderby", r"\border\s+by\b"),
            ("groupby", r"\bgroup\s+by\b"),
            ("insertinto", r"\binsert\s+into\b"),
            ("deletefrom", r"\bdelete\s+from\b"),
            ("from", r"\bfrom\b"),
            ("join", r"\bjoin\b"),
            ("update", r"\bupdate\b"),
            ("into", r"\binto\b"),
            ("table", r"\btable\b"),
            ("where", r"\bwhere\b"),
            ("and", r"\band\b"),
            ("or", r"\bor\b"),
            ("set", r"\bset\b"),
            ("select", r"\bselect\b"),
            ("values", r"\bvalues\b"),
        ]

        last_clause = ""
        last_pos = -1
        for clause, pattern in clause_patterns:
            for match in re.finditer(pattern, lower_text):
                if match.start() >= last_pos:
                    last_pos = match.start()
                    last_clause = clause
        return last_clause

    @staticmethod
    def _keyword_suggestions(prefix: str) -> list[str]:
        keywords = [
            "SELECT",
            "FROM",
            "WHERE",
            "JOIN",
            "LEFT JOIN",
            "RIGHT JOIN",
            "INNER JOIN",
            "GROUP BY",
            "ORDER BY",
            "LIMIT",
            "INSERT INTO",
            "UPDATE",
            "DELETE FROM",
            "CREATE TABLE",
            "ALTER TABLE",
            "DROP TABLE",
            "VALUES",
            "SET",
            "AND",
            "OR",
        ]
        if not prefix:
            return keywords[:8]
        upper_prefix = prefix.upper()
        return [kw for kw in keywords if kw.startswith(upper_prefix)]

    def _fetch_tables(self, prefix: str, limit: int = 50) -> list[str]:
        if self.offline_mode:
            return []
        sql = """
            SELECT c.relname
            FROM pg_catalog.pg_class AS c
            WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND pg_catalog.pg_table_is_visible(c.oid)
              AND (%s = '' OR c.relname ILIKE %s)
            ORDER BY c.relname
            LIMIT %s
        """
        like_prefix = f"{prefix}%"
        try:
            with psycopg2.connect(self.dsn) as conn:
                with conn.cursor() as cursor:
                    cursor.execute(sql, (prefix, like_prefix, limit))
                    return [row[0] for row in cursor.fetchall()]
        except Exception:
            return []

    def _fetch_columns(self, table_names: list[str], prefix: str, limit: int = 100) -> list[str]:
        if not table_names:
            return []
        if self.offline_mode:
            return []
        sql = """
            SELECT DISTINCT a.attname
            FROM pg_catalog.pg_attribute AS a
            JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
            WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND pg_catalog.pg_table_is_visible(c.oid)
              AND c.relname = ANY(%s)
              AND a.attnum > 0
              AND NOT a.attisdropped
              AND (%s = '' OR a.attname ILIKE %s)
            ORDER BY a.attname
            LIMIT %s
        """
        like_prefix = f"{prefix}%"
        try:
            with psycopg2.connect(self.dsn) as conn:
                with conn.cursor() as cursor:
                    cursor.execute(sql, (table_names, prefix, like_prefix, limit))
                    return [row[0] for row in cursor.fetchall()]
        except Exception:
            return []

    def _fetch_columns_any(self, prefix: str, limit: int = 100) -> list[str]:
        if self.offline_mode:
            return []
        sql = """
            SELECT DISTINCT a.attname
            FROM pg_catalog.pg_attribute AS a
            JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
            WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND pg_catalog.pg_table_is_visible(c.oid)
              AND a.attnum > 0
              AND NOT a.attisdropped
              AND (%s = '' OR a.attname ILIKE %s)
            ORDER BY a.attname
            LIMIT %s
        """
        like_prefix = f"{prefix}%"
        try:
            with psycopg2.connect(self.dsn) as conn:
                with conn.cursor() as cursor:
                    cursor.execute(sql, (prefix, like_prefix, limit))
                    return [row[0] for row in cursor.fetchall()]
        except Exception:
            return []

    def _get_cached_tables(self) -> list[str]:
        if not self.cached_tables:
            self.cached_tables = self._fetch_tables("", limit=self.MAX_TABLE_CACHE_SIZE)
        return self.cached_tables

    def _get_cached_columns(self, table_names: list[str]) -> list[str]:
        merged: list[str] = []
        for table_name in table_names:
            table_key = table_name.lower()
            if table_key not in self.cached_columns_by_table:
                self.cached_columns_by_table[table_key] = self._fetch_columns(
                    [table_name], "", limit=self.MAX_COLUMN_CACHE_SIZE
                )
            merged = self._merge_suggestions(merged, self.cached_columns_by_table[table_key])
        return merged

    def _get_cached_columns_any(self) -> list[str]:
        if not self.cached_columns_any:
            self.cached_columns_any = self._fetch_columns_any("", limit=self.MAX_COLUMN_CACHE_SIZE)
        return self.cached_columns_any

    @staticmethod
    def _filter_cached_values(values: list[str], prefix: str) -> list[str]:
        if not prefix:
            return values[:100]
        lower_prefix = prefix.lower()
        return [value for value in values if value.lower().startswith(lower_prefix)][:100]

    def _suggestions_from_cache(self, prefix: str) -> list[str]:
        if self.autocomplete_context in {"from", "join", "update", "into", "table"}:
            return self._filter_cached_values(self._get_cached_tables(), prefix)

        if self.autocomplete_context in {"where", "and", "or", "set", "orderby"}:
            query = self.input_box.get("1.0", tk.END).rstrip("\n")
            table_names = self._extract_tables_from_query(query)
            values = self._get_cached_columns(table_names) if table_names else self._get_cached_columns_any()
            return self._filter_cached_values(values, prefix)

        return self._filter_cached_values(self._get_cached_tables(), prefix)

    @staticmethod
    def _merge_suggestions(*groups: list[str]) -> list[str]:
        seen: set[str] = set()
        merged: list[str] = []
        for group in groups:
            for item in group:
                key = item.lower()
                if key not in seen:
                    seen.add(key)
                    merged.append(item)
        return merged

    @staticmethod
    def _extract_tables_from_query(query: str) -> list[str]:
        # Basic SQL table extraction for FROM/JOIN clauses.
        pattern = re.compile(
            r"(?:from|join|update|into)\s+([A-Za-z_][A-Za-z0-9_]*)",
            flags=re.IGNORECASE,
        )
        names = [m.group(1) for m in pattern.finditer(query)]
        seen: set[str] = set()
        unique: list[str] = []
        for name in names:
            key = name.lower()
            if key not in seen:
                seen.add(key)
                unique.append(name)
        return unique

    def _show_suggestions_popup(self, suggestions: list[str]) -> None:
        self._destroy_suggestions_window()

        popup = tk.Toplevel(self.root)
        popup.wm_overrideredirect(True)
        popup.attributes("-topmost", True)

        listbox = tk.Listbox(popup, height=min(8, len(suggestions)), width=48, font=("Consolas", 10))
        for item in suggestions:
            listbox.insert(tk.END, item)
        listbox.selection_set(0)
        listbox.activate(0)
        listbox.pack(fill=tk.BOTH, expand=True)

        listbox.bind("<Double-Button-1>", lambda _: self._accept_suggestion())

        bbox = self.input_box.bbox(tk.INSERT)
        if bbox:
            x, y, _, h = bbox
            screen_x = self.input_box.winfo_rootx() + x
            screen_y = self.input_box.winfo_rooty() + y + h + 2
        else:
            screen_x = self.input_box.winfo_rootx() + 10
            screen_y = self.input_box.winfo_rooty() + 10
        popup.geometry(f"+{screen_x}+{screen_y}")

        self.suggestion_window = popup
        self.suggestion_listbox = listbox
        self.input_box.focus_set()

    def _update_suggestions_popup(self, suggestions: list[str]) -> None:
        if not self.suggestion_listbox:
            return
        self.suggestion_listbox.delete(0, tk.END)
        for item in suggestions:
            self.suggestion_listbox.insert(tk.END, item)
        self.suggestion_listbox.selection_set(0)
        self.suggestion_listbox.activate(0)

    def _move_suggestion_selection(self, delta: int) -> None:
        if not self.suggestion_listbox:
            return
        size = self.suggestion_listbox.size()
        if size == 0:
            return
        current = self.suggestion_listbox.curselection()
        index = current[0] if current else 0
        next_index = max(0, min(size - 1, index + delta))
        self.suggestion_listbox.selection_clear(0, tk.END)
        self.suggestion_listbox.selection_set(next_index)
        self.suggestion_listbox.activate(next_index)
        self.suggestion_listbox.see(next_index)

    def _destroy_suggestions_window(self) -> None:
        if self.suggestion_window is not None:
            self.suggestion_window.destroy()
        self.suggestion_window = None
        self.suggestion_listbox = None

    def _accept_suggestion(self) -> None:
        if not self.suggestion_listbox:
            return
        selection = self.suggestion_listbox.curselection()
        if not selection:
            return
        suggestion = self.suggestion_listbox.get(selection[0])
        self._replace_current_token(suggestion)
        self._close_suggestions()
        self.input_box.focus_set()

    def _replace_current_token(self, suggestion: str) -> None:
        before_cursor = self.input_box.get("1.0", tk.INSERT)
        token_match = re.search(r"([A-Za-z_][A-Za-z0-9_]*)$", before_cursor)
        if token_match:
            token_len = len(token_match.group(1))
            start_idx = self.input_box.index(f"{tk.INSERT} - {token_len}c")
            self.input_box.delete(start_idx, tk.INSERT)
            self.input_box.insert(start_idx, suggestion)
        else:
            self.input_box.insert(tk.INSERT, suggestion)

    def _close_suggestions(self, event: tk.Event | None = None) -> str:
        self._destroy_suggestions_window()
        self.autocomplete_active = False
        self.autocomplete_context = ""
        self.autocomplete_base_prefix = ""
        return "break"

    def refresh_metadata_cache(self) -> None:
        self.cached_tables = []
        self.cached_columns_by_table = {}
        self.cached_columns_any = []
        self._append_server_message("Metadata cache refreshed.")

    def _on_poll_interval_changed(self, value: str) -> None:
        seconds = int(float(value))
        self.poll_label.configure(text=f"Poll interval: {seconds} sec")

    def _on_timeline_canvas_configure(self, _event: tk.Event) -> None:
        self._redraw_timeline_canvas()

    def _on_timeline_canvas_press(self, event: tk.Event) -> None:
        if self.timeline_max_offset <= 0:
            return
        left_x = self._timeline_position_to_x(self.timeline_left_position.get())
        right_x = self._timeline_position_to_x(self.timeline_right_position.get())
        axis_y = max(self.timeline_canvas.winfo_height(), 1) // 2
        min_x = min(left_x, right_x)
        max_x = max(left_x, right_x)
        dead_zone_half_height = 8

        # Dragging inside the middle "dead zone" moves the whole interval.
        if min_x + 8 <= event.x <= max_x - 8 and abs(event.y - axis_y) <= dead_zone_half_height:
            self._timeline_active_handle = "range"
            self._timeline_drag_start_x = event.x
            self._timeline_drag_left_start = self.timeline_left_position.get()
            self._timeline_drag_right_start = self.timeline_right_position.get()
            return

        self._timeline_active_handle = "left" if abs(event.x - left_x) <= abs(event.x - right_x) else "right"
        self._set_timeline_handle_from_x(self._timeline_active_handle, event.x)

    def _on_timeline_canvas_drag(self, event: tk.Event) -> None:
        if not self._timeline_active_handle:
            return
        if self._timeline_active_handle == "range":
            self._drag_timeline_range(event.x)
            return
        self._set_timeline_handle_from_x(self._timeline_active_handle, event.x)

    def _on_timeline_canvas_release(self, _event: tk.Event) -> None:
        self._timeline_active_handle = None

    def _set_timeline_handle_from_x(self, handle: str, x_coord: int) -> None:
        position = self._timeline_x_to_position(x_coord)
        if handle == "left":
            self.timeline_left_position.set(position)
            if self.timeline_left_position.get() > self.timeline_right_position.get():
                self.timeline_right_position.set(self.timeline_left_position.get())
        else:
            self.timeline_right_position.set(position)
            if self.timeline_right_position.get() < self.timeline_left_position.get():
                self.timeline_left_position.set(self.timeline_right_position.get())
        self._sync_timeline_label()
        self._refresh_metric_chart_data_windows()
        self._redraw_timeline_canvas()

    def _drag_timeline_range(self, x_coord: int) -> None:
        if self.timeline_max_offset <= 0:
            return
        dx_pixels = x_coord - self._timeline_drag_start_x
        axis_left, axis_right = self._timeline_axis_bounds()
        axis_span = max(1, axis_right - axis_left)
        if self.timeline_max_offset <= 0:
            step_delta = 0
        else:
            step_delta = int(round((dx_pixels / float(axis_span)) * self.timeline_max_offset))

        width = self._timeline_drag_right_start - self._timeline_drag_left_start
        new_left = self._timeline_drag_left_start + step_delta
        new_right = self._timeline_drag_right_start + step_delta

        if new_left < 0:
            new_right -= new_left
            new_left = 0
        if new_right > self.timeline_max_offset:
            overflow = new_right - self.timeline_max_offset
            new_left -= overflow
            new_right = self.timeline_max_offset
        new_left = max(0, new_left)
        new_right = min(self.timeline_max_offset, new_right)

        # Preserve interval width whenever possible.
        if new_right - new_left != width:
            new_right = min(self.timeline_max_offset, new_left + width)
            new_left = max(0, new_right - width)

        self.timeline_left_position.set(new_left)
        self.timeline_right_position.set(new_right)
        self._sync_timeline_label()
        self._refresh_metric_chart_data_windows()
        self._redraw_timeline_canvas()

    def _timeline_axis_bounds(self) -> tuple[int, int]:
        width = max(self.timeline_canvas.winfo_width(), 1)
        # Keep timeline axis geometry aligned with chart plotting area.
        axis_left = 56
        axis_right = max(axis_left, width - 10)
        return axis_left, axis_right

    def _timeline_position_to_x(self, position: int) -> float:
        axis_left, axis_right = self._timeline_axis_bounds()
        if self.timeline_max_offset <= 0:
            return float(axis_right)
        ratio = max(0.0, min(1.0, position / float(self.timeline_max_offset)))
        return axis_left + ratio * (axis_right - axis_left)

    def _timeline_x_to_position(self, x_coord: int) -> int:
        axis_left, axis_right = self._timeline_axis_bounds()
        if self.timeline_max_offset <= 0 or axis_right <= axis_left:
            return 0
        clamped_x = max(axis_left, min(axis_right, x_coord))
        ratio = (clamped_x - axis_left) / float(axis_right - axis_left)
        return int(round(ratio * self.timeline_max_offset))

    def _redraw_timeline_canvas(self) -> None:
        if not hasattr(self, "timeline_canvas"):
            return
        canvas = self.timeline_canvas
        canvas.delete("all")
        width = max(canvas.winfo_width(), 1)
        height = max(canvas.winfo_height(), 1)
        axis_left, axis_right = self._timeline_axis_bounds()
        axis_y = height // 2
        canvas.create_line(axis_left, axis_y, axis_right, axis_y, fill="#A0A0A0", width=2)

        left_x = self._timeline_position_to_x(self.timeline_left_position.get())
        right_x = self._timeline_position_to_x(self.timeline_right_position.get())
        if right_x < left_x:
            left_x, right_x = right_x, left_x
        canvas.create_line(left_x, axis_y, right_x, axis_y, fill="#2F6FDB", width=4)
        canvas.create_oval(left_x - 5, axis_y - 5, left_x + 5, axis_y + 5, fill="#2F6FDB", outline="")
        canvas.create_oval(right_x - 5, axis_y - 5, right_x + 5, axis_y + 5, fill="#E67E22", outline="")
        if right_x - left_x >= 16:
            mid_x = (left_x + right_x) / 2.0
            canvas.create_rectangle(mid_x - 10, axis_y - 4, mid_x + 10, axis_y + 4, fill="#000000", outline="")

        if self.timeline_max_offset <= 0:
            canvas.create_text(width - 8, axis_y - 10, text="latest", anchor="e", fill="#666666", font=("Segoe UI", 8))
        else:
            canvas.create_text(axis_left, axis_y - 10, text="oldest", anchor="w", fill="#666666", font=("Segoe UI", 8))
            canvas.create_text(axis_right, axis_y - 10, text="latest", anchor="e", fill="#666666", font=("Segoe UI", 8))

    def _toggle_metric_chart_visibility(self, _view_name: str) -> None:
        self._refresh_metric_chart_visibility()

    def _refresh_metric_chart_visibility(self) -> None:
        for chart in self.metric_charts.values():
            chart.pack_forget()
        view_order = {metric["view"]: idx for idx, metric in enumerate(self.metric_configs)}

        def display_priority(view_name: str) -> int:
            if view_name == "pg_stat_activity (active sessions)":
                return 0
            if view_name.startswith("pg_stat_bgwriter"):
                return 1
            if view_name.startswith("pg_stat_database"):
                return 2
            if view_name.startswith("pg_stat_wal"):
                return 3
            return 4

        ordered_metrics = sorted(
            self.metric_configs,
            key=lambda metric: (display_priority(metric["view"]), view_order.get(metric["view"], 10_000)),
        )
        for metric in ordered_metrics:
            view_name = metric["view"]
            visible_var = self.metric_visibility_vars.get(view_name)
            if visible_var is not None and visible_var.get():
                self.metric_charts[view_name].pack(fill=tk.BOTH, expand=True, pady=(0, 8))

    @staticmethod
    def _on_metrics_mousewheel(event: tk.Event, metrics_canvas: tk.Canvas) -> str:
        delta = 0
        if getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1
        elif getattr(event, "delta", 0):
            # On Windows and macOS, MouseWheel uses event.delta.
            delta = -int(event.delta / 120) if event.delta % 120 == 0 else (-1 if event.delta > 0 else 1)

        if delta != 0:
            metrics_canvas.yview_scroll(delta, "units")
        return "break"

    @staticmethod
    def _build_metric_configs() -> list[dict]:
        def unit_category(series_name: str) -> str:
            # Heuristic grouping by "unit-like" column name patterns.
            # The goal is to keep only the dominant category per view.
            s = series_name.lower()
            if "bytes" in s:
                return "bytes"
            if "time" in s:
                return "time"
            if s.startswith("xact_"):
                return "transactions"
            if "buffers" in s:
                return "buffers"
            if s.startswith("blks_") or " blks_" in s or "blks" in s:
                return "blocks"
            if "tup_" in s:
                return "tuples"
            if s in {"seq_scan", "idx_scan"} or s.endswith("_scan"):
                return "scans"
            return "counts"

        unit_priority = {
            "bytes": 0,
            "time": 1,
            "blocks": 2,
            "buffers": 3,
            "transactions": 4,
            "tuples": 5,
            "scans": 6,
            "counts": 7,
        }

        def filter_dominant_unit(series: list[str], colors: dict[str, str]) -> tuple[list[str], dict[str, str]]:
            cats = [unit_category(s) for s in series]
            counts: dict[str, int] = {}
            for c in cats:
                counts[c] = counts.get(c, 0) + 1
            max_count = max(counts.values(), default=0)
            candidates = [c for c, n in counts.items() if n == max_count]
            best_cat = sorted(candidates, key=lambda c: unit_priority.get(c, 999))[0] if candidates else "counts"
            kept_series = [s for s in series if unit_category(s) == best_cat]
            kept_colors = {s: colors[s] for s in kept_series if s in colors}
            return kept_series, kept_colors

        # Build candidate configs with expressions per series.
        candidates: list[dict] = [
            {
                "view": "pg_stat_bgwriter",
                "series": ["buffers_backend", "buffers_alloc", "buffers_checkpoint", "buffers_clean"],
                "colors": {
                    "buffers_backend": "#2F6FDB",
                    "buffers_alloc": "#E67E22",
                    "buffers_checkpoint": "#16A085",
                    "buffers_clean": "#C0392B",
                },
                "from_sql": "FROM pg_stat_bgwriter",
                "exprs": {
                    "buffers_backend": "buffers_backend",
                    "buffers_alloc": "buffers_alloc",
                    "buffers_checkpoint": "buffers_checkpoint",
                    "buffers_clean": "buffers_clean",
                },
            },
            {
                "view": "pg_stat_wal",
                "series": ["wal_records", "wal_fpi", "wal_bytes"],
                "colors": {"wal_records": "#8E44AD", "wal_fpi": "#16A085", "wal_bytes": "#D35400"},
                "from_sql": "FROM pg_stat_wal",
                "exprs": {"wal_records": "wal_records", "wal_fpi": "wal_fpi", "wal_bytes": "wal_bytes"},
            },
            {
                "view": "pg_stat_database (transactions)",
                "series": ["xact_commit", "xact_rollback"],
                "colors": {
                    "xact_commit": "#2E86C1",
                    "xact_rollback": "#C0392B",
                },
                "from_sql": "FROM pg_stat_database WHERE datname = current_database()",
                "exprs": {
                    "xact_commit": "xact_commit",
                    "xact_rollback": "xact_rollback",
                },
            },
            {
                "view": "pg_stat_database (tuples)",
                "series": [
                    "tup_returned",
                    "tup_fetched",
                    "tup_inserted",
                    "tup_updated",
                    "tup_deleted",
                ],
                "colors": {
                    "tup_returned": "#2E86C1",
                    "tup_fetched": "#117A65",
                    "tup_inserted": "#229954",
                    "tup_updated": "#D68910",
                    "tup_deleted": "#C0392B",
                },
                "from_sql": "FROM pg_stat_database WHERE datname = current_database()",
                "exprs": {
                    "tup_returned": "tup_returned",
                    "tup_fetched": "tup_fetched",
                    "tup_inserted": "tup_inserted",
                    "tup_updated": "tup_updated",
                    "tup_deleted": "tup_deleted",
                },
            },
            {
                "view": "pg_stat_database (blocks)",
                "series": ["blks_read", "blks_hit"],
                "colors": {
                    "blks_read": "#8E44AD",
                    "blks_hit": "#16A085",
                },
                "from_sql": "FROM pg_stat_database WHERE datname = current_database()",
                "exprs": {
                    "blks_read": "blks_read",
                    "blks_hit": "blks_hit",
                },
            },
            {
                "view": "pg_stat_archiver",
                "series": ["archived_count", "failed_count"],
                "colors": {"archived_count": "#229954", "failed_count": "#C0392B"},
                "from_sql": "FROM pg_stat_archiver",
                "exprs": {"archived_count": "archived_count", "failed_count": "failed_count"},
            },
            {
                "view": "pg_stat_slru",
                "series": ["blks_zeroed", "blks_hit", "blks_read", "blks_written", "flushes", "truncates"],
                "colors": {
                    "blks_zeroed": "#1F618D",
                    "blks_hit": "#1ABC9C",
                    "blks_read": "#7D3C98",
                    "blks_written": "#D35400",
                    "flushes": "#CA6F1E",
                    "truncates": "#566573",
                },
                "from_sql": "FROM pg_stat_slru",
                "exprs": {
                    "blks_zeroed": "COALESCE(SUM(blks_zeroed),0)",
                    "blks_hit": "COALESCE(SUM(blks_hit),0)",
                    "blks_read": "COALESCE(SUM(blks_read),0)",
                    "blks_written": "COALESCE(SUM(blks_written),0)",
                    "flushes": "COALESCE(SUM(flushes),0)",
                    "truncates": "COALESCE(SUM(truncates),0)",
                },
            },
            {
                "view": "pg_stat_user_tables",
                "series": ["seq_scan", "idx_scan", "n_tup_ins", "n_tup_upd", "n_tup_del"],
                "colors": {
                    "seq_scan": "#8E44AD",
                    "idx_scan": "#2471A3",
                    "n_tup_ins": "#229954",
                    "n_tup_upd": "#D68910",
                    "n_tup_del": "#C0392B",
                },
                "from_sql": "FROM pg_stat_user_tables",
                "exprs": {
                    "seq_scan": "COALESCE(SUM(seq_scan),0)",
                    "idx_scan": "COALESCE(SUM(idx_scan),0)",
                    "n_tup_ins": "COALESCE(SUM(n_tup_ins),0)",
                    "n_tup_upd": "COALESCE(SUM(n_tup_upd),0)",
                    "n_tup_del": "COALESCE(SUM(n_tup_del),0)",
                },
            },
            {
                "view": "pg_stat_user_indexes",
                "series": ["idx_scan", "idx_tup_read", "idx_tup_fetch"],
                "colors": {"idx_scan": "#1F618D", "idx_tup_read": "#17A589", "idx_tup_fetch": "#AF601A"},
                "from_sql": "FROM pg_stat_user_indexes",
                "exprs": {
                    "idx_scan": "COALESCE(SUM(idx_scan),0)",
                    "idx_tup_read": "COALESCE(SUM(idx_tup_read),0)",
                    "idx_tup_fetch": "COALESCE(SUM(idx_tup_fetch),0)",
                },
            },
            {
                "view": "pg_stat_activity (active sessions)",
                "series": ["non_idle_sessions"],
                "colors": {"non_idle_sessions": "#2E86C1"},
                "from_sql": "FROM pg_stat_activity",
                "exprs": {
                    "non_idle_sessions": "COALESCE(SUM(CASE WHEN state IS DISTINCT FROM 'idle' THEN 1 ELSE 0 END),0)",
                },
                "value_mode": "raw",
            },
        ]

        filtered_configs: list[dict] = []
        for c in candidates:
            kept_series, kept_colors = filter_dominant_unit(c["series"], c["colors"])
            if not kept_series:
                kept_series = c["series"]
                kept_colors = c["colors"]
            select_exprs = [c["exprs"][s] for s in kept_series]
            sql = f"SELECT {', '.join(select_exprs)} {c['from_sql']}"
            filtered_configs.append(
                {
                    "view": c["view"],
                    "series": kept_series,
                    "colors": kept_colors,
                    "sql": sql,
                    "value_mode": c.get("value_mode", "delta"),
                }
            )
        return filtered_configs

    def _init_metrics_db(self) -> None:
        self.metrics_db_conn = sqlite3.connect(self.METRICS_DB_PATH)
        self.metrics_db_conn.execute(
            """
            CREATE TABLE IF NOT EXISTS metric_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                view_name TEXT NOT NULL,
                series_name TEXT NOT NULL,
                raw_value REAL NOT NULL,
                delta_value REAL NOT NULL
            )
            """
        )
        self.metrics_db_conn.execute(
            """
            CREATE TABLE IF NOT EXISTS db_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self.metrics_db_conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_pg_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at TEXT NOT NULL,
                name TEXT NOT NULL,
                setting TEXT NOT NULL,
                unit TEXT NOT NULL,
                source TEXT NOT NULL,
                boot_val TEXT NOT NULL,
                reset_val TEXT NOT NULL
            )
            """
        )
        self.metrics_db_conn.execute(
            "INSERT OR REPLACE INTO db_meta(key, value) VALUES('export_format', ?)",
            (self.EXPORT_FORMAT_TAG,),
        )
        self.metrics_db_conn.commit()

    def _truncate_metrics_data(self) -> None:
        if not self.metrics_db_conn:
            return
        self.metrics_db_conn.execute("DELETE FROM metric_samples")
        self.metrics_db_conn.execute("DELETE FROM session_pg_settings")
        self.metrics_db_conn.commit()

    def _close_metrics_db(self) -> None:
        if self.metrics_db_conn is not None:
            self.metrics_db_conn.close()
            self.metrics_db_conn = None

    def _on_close_app(self) -> None:
        self._truncate_metrics_data()
        self._close_metrics_db()
        self.root.destroy()

    def _capture_session_pg_settings(self) -> None:
        if not self.metrics_db_conn:
            return
        if self.offline_mode:
            self._append_server_message("Offline mode: skipping pg_settings capture.")
            return
        captured_at = datetime.now().isoformat(timespec="seconds")
        query = """
            SELECT name, setting, COALESCE(unit, ''), COALESCE(source, ''), COALESCE(boot_val, ''), COALESCE(reset_val, '')
            FROM pg_settings
            ORDER BY name
        """
        rows: list[tuple[str, str, str, str, str, str]] = []
        try:
            with psycopg2.connect(self.dsn) as conn:
                with conn.cursor() as cursor:
                    cursor.execute(query)
                    rows = [
                        (
                            str(name),
                            str(setting),
                            str(unit),
                            str(source),
                            str(boot_val),
                            str(reset_val),
                        )
                        for (name, setting, unit, source, boot_val, reset_val) in cursor.fetchall()
                    ]
        except Exception as err:
            self._append_server_message(f"Failed to capture pg_settings: {err}")
            return

        payload = [(captured_at, *row) for row in rows]
        self.metrics_db_conn.executemany(
            """
            INSERT INTO session_pg_settings(captured_at, name, setting, unit, source, boot_val, reset_val)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            payload,
        )
        self.metrics_db_conn.commit()

    def _poll_all_metrics(self) -> None:
        poll_started = time.perf_counter()
        self._metrics_debug("[metrics] poll cycle started")
        for metric in self.metric_configs:
            metric_started = time.perf_counter()
            self._poll_metric_view(metric)
            self._metrics_debug(
                f"[metrics] view={metric['view']} done in {time.perf_counter() - metric_started:.3f}s"
            )
        self._metrics_debug(f"[metrics] poll cycle finished in {time.perf_counter() - poll_started:.3f}s")
        self._sync_timeline_slider_limits()
        self._refresh_metric_chart_data_windows()

        delay_ms = self.poll_interval_seconds.get() * 1000
        self.root.after(delay_ms, self._poll_all_metrics)

    def _poll_metric_view(self, metric: dict) -> None:
        view_name = metric["view"]
        state = self.metric_states[view_name]
        raw_values = self._fetch_metric_row(metric)

        if raw_values is None:
            return

        previous = state["prev"]
        delta_values: dict[str, float] = {}

        for series_name in metric["series"]:
            raw_value = float(raw_values[series_name])
            if series_name in previous:
                delta_value = raw_value - previous[series_name]
            else:
                delta_value = 0.0
            previous[series_name] = raw_value
            delta_values[series_name] = delta_value

        self._append_metric_sample(view_name, raw_values, delta_values)

    def _sync_timeline_slider_limits(self) -> None:
        max_points = 0
        if self.metrics_db_conn:
            row = self.metrics_db_conn.execute(
                """
                SELECT COALESCE(MAX(sample_count), 0)
                FROM (
                    SELECT COUNT(*) AS sample_count
                    FROM metric_samples
                    GROUP BY view_name, series_name
                )
                """
            ).fetchone()
            max_points = int(row[0]) if row and row[0] is not None else 0
        max_offset = max(0, max_points - 1)
        previous_max_offset = self.timeline_max_offset
        previous_left_back = max(0, previous_max_offset - self.timeline_left_position.get())
        previous_right_back = max(0, previous_max_offset - self.timeline_right_position.get())
        self.timeline_max_offset = max_offset

        if not self._timeline_range_initialized and self.timeline_max_offset > 0:
            span = min(self.timeline_default_span, self.timeline_max_offset)
            self.timeline_left_position.set(self.timeline_max_offset - span)
            self.timeline_right_position.set(self.timeline_max_offset)
            self._timeline_range_initialized = True
            self._sync_timeline_label()
            self._redraw_timeline_canvas()
            return

        next_left_position = max(0, self.timeline_max_offset - previous_left_back)
        next_right_position = max(0, self.timeline_max_offset - previous_right_back)
        next_left_position = min(next_left_position, self.timeline_max_offset)
        next_right_position = min(next_right_position, self.timeline_max_offset)
        if next_left_position > next_right_position:
            next_left_position, next_right_position = next_right_position, next_left_position

        self.timeline_left_position.set(next_left_position)
        self.timeline_right_position.set(next_right_position)
        self._sync_timeline_label()
        self._redraw_timeline_canvas()

    def _sync_timeline_label(self) -> None:
        older_steps_back, newer_steps_back = self._get_timeline_range_steps_back()
        if older_steps_back <= 0 and newer_steps_back <= 0:
            self.timeline_label.configure(text="Timeline: latest")
        elif older_steps_back == newer_steps_back:
            self.timeline_label.configure(text=f"Timeline: {older_steps_back} steps back")
        else:
            self.timeline_label.configure(
                text=f"Timeline: {older_steps_back}..{newer_steps_back} steps back"
            )

    def _refresh_metric_chart_data_windows(self) -> None:
        if not self.metrics_db_conn:
            return
        older_steps_back, newer_steps_back = self._get_timeline_range_steps_back()
        interval_count = older_steps_back - newer_steps_back + 1
        interval_count = max(1, interval_count)
        if self.timeline_max_offset > 0:
            left_ratio = self.timeline_left_position.get() / float(self.timeline_max_offset)
            right_ratio = self.timeline_right_position.get() / float(self.timeline_max_offset)
        else:
            left_ratio = None
            right_ratio = None
        for metric in self.metric_configs:
            view_name = metric["view"]
            chart = self.metric_charts.get(view_name)
            if not chart:
                continue
            value_mode = str(metric.get("value_mode", "delta")).lower()
            value_column = "raw_value" if value_mode == "raw" else "delta_value"
            per_series_values: dict[str, list[float]] = {}
            per_series_times: dict[str, list[float]] = {}
            use_fixed_activity_window = view_name == self.ACTIVITY_VIEW_NAME
            for series_name in metric["series"]:
                if use_fixed_activity_window:
                    query_limit = chart.max_points
                    query_offset = 0
                else:
                    query_limit = min(chart.max_points, interval_count)
                    query_offset = newer_steps_back
                rows = self.metrics_db_conn.execute(
                    f"""
                    SELECT timestamp, {value_column}
                    FROM metric_samples
                    WHERE view_name = ? AND series_name = ?
                    ORDER BY id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (view_name, series_name, query_limit, query_offset),
                ).fetchall()
                rows = list(reversed(rows))
                points: list[float] = []
                timestamps: list[float] = []
                for timestamp_text, metric_value in rows:
                    try:
                        timestamps.append(datetime.fromisoformat(str(timestamp_text)).timestamp())
                    except Exception:
                        continue
                    points.append(float(metric_value))
                per_series_values[series_name] = points
                per_series_times[series_name] = timestamps

            reference_times: list[float] = []
            for series_name in metric["series"]:
                if per_series_times.get(series_name):
                    reference_times = per_series_times[series_name]
                    break
            if not reference_times:
                chart.set_series_points({name: [] for name in metric["series"]}, [])
                chart.set_status_text("No stored samples yet.")
                chart.set_selection_overlay(None, None)
                continue

            chart.set_series_points(per_series_values, reference_times)
            if use_fixed_activity_window:
                chart.set_selection_overlay(left_ratio, right_ratio)
            else:
                chart.set_selection_overlay(None, None)
            preview_pairs = []
            for series_name in metric["series"][:3]:
                values = per_series_values.get(series_name, [])
                if values:
                    preview_pairs.append(f"{series_name}={values[-1]:.0f}")
            points_count = len(reference_times)
            prefix = "raw" if value_mode == "raw" else "delta"
            if preview_pairs:
                chart.set_status_text(f"{prefix}: {', '.join(preview_pairs)}  points={points_count}")
            else:
                chart.set_status_text("No stored samples yet.")

    def _get_timeline_range_steps_back(self) -> tuple[int, int]:
        left_back = max(0, self.timeline_max_offset - self.timeline_left_position.get())
        right_back = max(0, self.timeline_max_offset - self.timeline_right_position.get())
        older_steps_back = max(left_back, right_back)
        newer_steps_back = min(left_back, right_back)
        return older_steps_back, newer_steps_back

    def _get_selected_timeline_interval(self) -> dict[str, str] | None:
        if not self.metrics_db_conn:
            return None
        older_steps_back, newer_steps_back = self._get_timeline_range_steps_back()
        interval_count = max(1, older_steps_back - newer_steps_back + 1)
        newer_row = self.metrics_db_conn.execute(
            """
            SELECT timestamp
            FROM metric_samples
            ORDER BY id DESC
            LIMIT 1 OFFSET ?
            """,
            (newer_steps_back,),
        ).fetchone()
        older_row = self.metrics_db_conn.execute(
            """
            SELECT timestamp
            FROM metric_samples
            ORDER BY id DESC
            LIMIT 1 OFFSET ?
            """,
            (older_steps_back,),
        ).fetchone()
        if not newer_row or not older_row:
            return None
        newer_ts = str(newer_row[0])
        older_ts = str(older_row[0])
        from_ts = older_ts if older_ts <= newer_ts else newer_ts
        to_ts = newer_ts if newer_ts >= older_ts else older_ts
        return {
            "from_ts": from_ts,
            "to_ts": to_ts,
            "older_steps_back": str(older_steps_back),
            "newer_steps_back": str(newer_steps_back),
            "interval_count": str(interval_count),
        }

    def _fetch_metric_row(self, metric: dict) -> dict[str, float] | None:
        if self.offline_mode:
            return None
        try:
            connect_started = time.perf_counter()
            dsn = f"{self.dsn} connect_timeout=3"
            with psycopg2.connect(dsn) as conn:
                self._metrics_debug(
                    f"[metrics] connected for view={metric['view']} in {time.perf_counter() - connect_started:.3f}s"
                )
                with conn.cursor() as cursor:
                    sql_started = time.perf_counter()
                    cursor.execute(metric["sql"])
                    row = cursor.fetchone()
                    self._metrics_debug(
                        f"[metrics] sql executed for view={metric['view']} in {time.perf_counter() - sql_started:.3f}s"
                    )
                    if row is None:
                        return None
                    return {
                        metric["series"][idx]: float(row[idx])
                        for idx in range(len(metric["series"]))
                    }
        except Exception as err:
            self._metrics_debug(f"[metrics] fetch error view={metric['view']}: {err}")
            return None

    def _metrics_debug(self, message: str) -> None:
        if self.metrics_debug_enabled:
            print(message, flush=True)

    def _append_metric_sample(self, view_name: str, raw_values: dict[str, float], delta_values: dict[str, float]) -> None:
        if not self.metrics_db_conn:
            return
        timestamp = datetime.now().isoformat(timespec="seconds")
        rows = [
            (timestamp, view_name, series_name, float(raw_values[series_name]), float(delta_values[series_name]))
            for series_name in raw_values
        ]
        self.metrics_db_conn.executemany(
            """
            INSERT INTO metric_samples(timestamp, view_name, series_name, raw_value, delta_value)
            VALUES (?, ?, ?, ?, ?)
            """,
            rows,
        )
        self.metrics_db_conn.commit()

    def export_metrics_csv(self) -> None:
        if not self.metrics_db_conn:
            messagebox.showerror("Export CSV", "SQLite connection is not available.")
            return
        export_path = filedialog.asksaveasfilename(
            title="Export metrics to CSV",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not export_path:
            return

        metric_rows = self.metrics_db_conn.execute(
            """
            SELECT timestamp, view_name, series_name, raw_value, delta_value
            FROM metric_samples
            ORDER BY id
            """
        ).fetchall()
        settings_rows = self.metrics_db_conn.execute(
            """
            SELECT captured_at, name, setting, unit, source, boot_val, reset_val
            FROM session_pg_settings
            ORDER BY id
            """
        ).fetchall()

        with open(export_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["__format__", self.EXPORT_FORMAT_TAG])
            writer.writerow(["__block__", "settings"])
            writer.writerow(["captured_at", "name", "setting", "unit", "source", "boot_val", "reset_val"])
            writer.writerows(settings_rows)
            writer.writerow(["__block__", "metrics"])
            writer.writerow(["timestamp", "view_name", "series_name", "raw_value", "delta_value"])
            writer.writerows(metric_rows)

        self._append_server_message(f"Metrics exported: {export_path}")

    def import_metrics_csv(self) -> None:
        if not self.metrics_db_conn:
            messagebox.showerror("Import CSV", "SQLite connection is not available.")
            return
        import_path = filedialog.askopenfilename(
            title="Import metrics from CSV",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not import_path:
            return

        with open(import_path, "r", newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            try:
                format_row = next(reader)
            except StopIteration:
                messagebox.showerror("Import CSV", "File is empty or incomplete.")
                return

            if format_row != ["__format__", self.EXPORT_FORMAT_TAG]:
                messagebox.showerror("Import CSV", "Unsupported CSV format.")
                return

            block = ""
            settings_rows: list[tuple[str, str, str, str, str, str, str]] = []
            metric_rows: list[tuple[str, str, str, float, float]] = []

            for row in reader:
                if not row:
                    continue
                if row[:2] == ["__block__", "settings"]:
                    block = "settings_header"
                    continue
                if row[:2] == ["__block__", "metrics"]:
                    block = "metrics_header"
                    continue

                if block == "settings_header":
                    if row != ["captured_at", "name", "setting", "unit", "source", "boot_val", "reset_val"]:
                        messagebox.showerror("Import CSV", "Unexpected settings block header.")
                        return
                    block = "settings"
                    continue
                if block == "metrics_header":
                    if row != ["timestamp", "view_name", "series_name", "raw_value", "delta_value"]:
                        messagebox.showerror("Import CSV", "Unexpected metrics block header.")
                        return
                    block = "metrics"
                    continue

                if block == "settings":
                    if len(row) != 7:
                        continue
                    settings_rows.append((row[0], row[1], row[2], row[3], row[4], row[5], row[6]))
                elif block == "metrics":
                    if len(row) != 5:
                        continue
                    metric_rows.append((row[0], row[1], row[2], float(row[3]), float(row[4])))

        self._truncate_metrics_data()
        self.metrics_db_conn.executemany(
            """
            INSERT INTO session_pg_settings(captured_at, name, setting, unit, source, boot_val, reset_val)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            settings_rows,
        )
        self.metrics_db_conn.executemany(
            """
            INSERT INTO metric_samples(timestamp, view_name, series_name, raw_value, delta_value)
            VALUES (?, ?, ?, ?, ?)
            """,
            metric_rows,
        )
        self.metrics_db_conn.commit()
        self._append_server_message(
            f"Import complete: settings={len(settings_rows)} rows, metrics={len(metric_rows)} rows"
        )
        self._sync_timeline_slider_limits()
        self._refresh_metric_chart_data_windows()

    def _configure_input_highlighting(self) -> None:
        self.input_box.tag_configure("sql_keyword", foreground="#2F6FDB")
        self.input_box.tag_configure("sql_string", foreground="#0A7A3E")
        self.input_box.tag_configure("sql_number", foreground="#9C27B0")

    def _apply_sql_highlighting(self) -> None:
        for tag in ("sql_keyword", "sql_string", "sql_number"):
            self.input_box.tag_remove(tag, "1.0", tk.END)

        text = self.input_box.get("1.0", tk.END).rstrip("\n")
        if not text:
            return

        keyword_pattern = re.compile(
            r"\b(select|from|where|join|left|right|inner|outer|on|group|by|order|limit|insert|into|update|delete|create|alter|drop|table|values|set|and|or|not|null|as|distinct)\b",
            flags=re.IGNORECASE,
        )
        string_pattern = re.compile(r"'([^']|'')*'")
        number_pattern = re.compile(r"\b\d+(?:\.\d+)?\b")

        self._highlight_matches(keyword_pattern, text, "sql_keyword")
        self._highlight_matches(string_pattern, text, "sql_string")
        self._highlight_matches(number_pattern, text, "sql_number")

    def _highlight_matches(self, pattern: re.Pattern[str], text: str, tag_name: str) -> None:
        for match in pattern.finditer(text):
            start_idx = f"1.0 + {match.start()}c"
            end_idx = f"1.0 + {match.end()}c"
            self.input_box.tag_add(tag_name, start_idx, end_idx)

    def _append_message(self, speaker: str, text: str) -> None:
        self.chat_area.configure(state=tk.NORMAL)
        self.chat_area.insert(tk.END, f"{speaker}: {text}\n\n")
        self.chat_area.see(tk.END)
        self.chat_area.configure(state=tk.DISABLED)

    def _append_user_message(self, text: str) -> None:
        self._append_message("You", text)

    def _append_server_message(self, text: str) -> None:
        speaker = "DB" if self.dialog_mode.get() == "DB" else "LLM"
        self._append_message(speaker, text)

    def _sync_chat_wrap_mode(self) -> None:
        if self.dialog_mode.get() == "LLM":
            self.chat_area.configure(wrap=tk.WORD)
        else:
            self.chat_area.configure(wrap=tk.NONE)

    def toggle_dialog_mode(self) -> None:
        if self.dialog_mode.get() == "DB":
            self.dialog_mode.set("LLM")
            self.mode_button.configure(
                text="Mode: LLM",
                bg="#B03A2E",
                activebackground="#922B21",
            )
            self._sync_llm_model_button_label()
            self._request_layout_control_buttons()
            self._sync_chat_wrap_mode()
            self._append_server_message("LLM mode enabled.")
        else:
            self.dialog_mode.set("DB")
            self.mode_button.configure(
                text="Mode: БД",
                bg="#1E8449",
                activebackground="#196F3D",
            )
            self._request_layout_control_buttons()
            self._sync_chat_wrap_mode()
            self._append_server_message("DB mode enabled.")

    def _sync_llm_model_button_label(self) -> None:
        model = self.llm_pipeline.get_model().strip().lower()
        label = "reasoner" if "reasoner" in model else "chat"
        self.llm_model_button.configure(text=f"LLM: {label}")
        self._request_layout_control_buttons()

    def _request_layout_control_buttons(self) -> None:
        if self._buttons_layout_scheduled:
            return
        self._buttons_layout_scheduled = True
        self.root.after_idle(self._layout_control_buttons)

    def _layout_control_buttons(self) -> None:
        self._buttons_layout_scheduled = False
        ordered_buttons = [self.mode_button]
        if self.dialog_mode.get() == "LLM":
            ordered_buttons.append(self.llm_model_button)
        ordered_buttons.extend(
            [
                self.send_button,
                self.refresh_cache_button,
                self.export_button,
                self.import_button,
            ]
        )

        for child in self.buttons_frame.winfo_children():
            child.grid_forget()

        if not ordered_buttons:
            return

        container_width = self.buttons_frame.master.winfo_width()
        if container_width <= 1:
            self._request_layout_control_buttons()
            return
        max_row_width = max(int(container_width * self.button_max_width_ratio), 220)

        layout_signature = (
            tuple(button.cget("text") for button in ordered_buttons),
            max_row_width,
        )
        if layout_signature == self._last_buttons_layout_signature:
            return
        self._last_buttons_layout_signature = layout_signature

        row = 0
        col = 0
        used_width = 0
        for button in ordered_buttons:
            button_width = button.winfo_reqwidth()
            next_width = button_width if col == 0 else used_width + self.button_wrap_padding + button_width
            if col > 0 and next_width > max_row_width:
                row += 1
                col = 0
                used_width = 0
                next_width = button_width
            button.grid(row=row, column=col, padx=(0, self.button_wrap_padding), pady=(0, self.button_row_padding), sticky="w")
            used_width = next_width
            col += 1

    def toggle_llm_model(self) -> None:
        current = self.llm_pipeline.get_model().strip().lower()
        if "reasoner" in current:
            self.llm_pipeline.set_model("deepseek-chat")
            self._append_server_message("LLM model switched to deepseek-chat.")
        else:
            self.llm_pipeline.set_model("deepseek-reasoner")
            self._append_server_message("LLM model switched to deepseek-reasoner.")
        self._sync_llm_model_button_label()

    def send_query(self) -> None:
        self._close_suggestions()
        query = self.input_box.get("1.0", tk.END).strip()
        if not query:
            return

        if not self.command_history or self.command_history[-1] != query:
            self.command_history.append(query)
        self.history_index = None
        self.history_current_input = ""

        self.input_box.delete("1.0", tk.END)
        self._append_user_message(query)

        if self.dialog_mode.get() == "LLM":
            self.send_button.configure(state=tk.DISABLED)
            self._append_server_message("LLM pipeline: collecting context and generating answer...")
            selected_interval = self._get_selected_timeline_interval()
            worker = threading.Thread(
                target=self._execute_llm_thread,
                args=(query, selected_interval),
                daemon=True,
            )
            worker.start()
            return

        self.send_button.configure(state=tk.DISABLED)
        self._append_server_message("Executing...")

        worker = threading.Thread(target=self._execute_query_thread, args=(query,), daemon=True)
        worker.start()

    def _execute_query_thread(self, query: str) -> None:
        try:
            response = self._execute_query(query)
        except Exception as err:
            response = f"Error: {err}"
        self.root.after(0, self._finish_query, response)

    def _execute_llm_thread(self, query: str, selected_interval: dict[str, str] | None = None) -> None:
        try:
            response = self.llm_pipeline.run(query, selected_interval=selected_interval)
        except Exception as err:
            response = f"LLM error: {err}"
        self.root.after(0, self._finish_query, response)

    def _finish_query(self, response: str) -> None:
        self._append_server_message(response)
        self.send_button.configure(state=tk.NORMAL)

    def _execute_query(self, query: str) -> str:
        if self.offline_mode:
            return "Offline mode is enabled. Live SQL execution is unavailable."
        with psycopg2.connect(self.dsn) as conn:
            with conn.cursor() as cursor:
                cursor.execute(query)

                if cursor.description is None:
                    conn.commit()
                    return f"OK. Rows affected: {cursor.rowcount}"

                rows = cursor.fetchmany(self.MAX_PREVIEW_ROWS + 1)
                truncated = len(rows) > self.MAX_PREVIEW_ROWS
                if truncated:
                    rows = rows[: self.MAX_PREVIEW_ROWS]
                columns = [desc[0] for desc in cursor.description]
                return self._format_table(columns, rows, truncated=truncated)

    @classmethod
    def _format_table(cls, columns: list[str], rows: list[tuple], truncated: bool) -> str:
        if not rows:
            return "Query executed successfully. No rows returned."

        normalized_rows = [
            [cls._normalize_cell(value) for value in row]
            for row in rows
        ]

        widths = [min(len(str(col)), cls.MAX_CELL_WIDTH) for col in columns]
        for row in normalized_rows:
            for idx, value in enumerate(row):
                widths[idx] = max(widths[idx], len(str(value)))
                widths[idx] = min(widths[idx], cls.MAX_CELL_WIDTH)

        header_cells = [
            cls._clip_cell_text(str(col), widths[i]).ljust(widths[i]) for i, col in enumerate(columns)
        ]
        header = "| " + " | ".join(header_cells) + " |"
        separator = "+-" + "-+-".join("-" * widths[i] for i in range(len(columns))) + "-+"
        body_lines = [
            "| "
            + " | ".join(cls._clip_cell_text(str(value), widths[i]).ljust(widths[i]) for i, value in enumerate(row))
            + " |"
            for row in normalized_rows
        ]

        table = "\n".join([separator, header, separator, *body_lines, separator])
        table += f"\nRows shown: {len(rows)}"
        if truncated:
            table += f" (first {cls.MAX_PREVIEW_ROWS})"
        return table

    @classmethod
    def _normalize_cell(cls, value: object) -> str:
        if value is None:
            return "NULL"
        text = str(value)
        return text.replace("\n", "\\n")

    @staticmethod
    def _clip_cell_text(text: str, width: int) -> str:
        if len(text) <= width:
            return text
        if width <= 3:
            return text[:width]
        return text[: width - 3] + "..."

    @staticmethod
    def _config_bool(value: object) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        return text in {"1", "true", "yes", "on"}

    def _sync_connection_mode_button(self) -> None:
        if self.offline_mode:
            self.connection_mode_button.configure(
                text="Mode: Offline",
                bg="#B03A2E",
                fg="#FFFFFF",
                activebackground="#922B21",
                activeforeground="#FFFFFF",
            )
        else:
            self.connection_mode_button.configure(
                text="Mode: Online",
                bg="#1E8449",
                fg="#FFFFFF",
                activebackground="#196F3D",
                activeforeground="#FFFFFF",
            )

    def _persist_db_config(self) -> bool:
        payload = dict(self.db_config)
        payload["offline_mode"] = self.offline_mode
        try:
            self.DB_CONFIG_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return True
        except Exception as err:
            messagebox.showerror("Settings", f"Failed to save config:\n{err}")
            return False

    def toggle_offline_mode(self) -> None:
        self.offline_mode = not self.offline_mode
        self.db_config["offline_mode"] = self.offline_mode
        if not self._persist_db_config():
            self.offline_mode = not self.offline_mode
            self.db_config["offline_mode"] = self.offline_mode
            self._sync_connection_mode_button()
            return
        self._sync_connection_mode_button()
        mode_text = "Offline mode enabled." if self.offline_mode else "Offline mode disabled."
        self._append_server_message(mode_text)

    def open_settings_window(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("DB Settings")
        window.transient(self.root)
        window.grab_set()
        window.resizable(False, False)

        body = tk.Frame(window, padx=12, pady=10)
        body.pack(fill=tk.BOTH, expand=True)

        fields = [
            ("host", "Host"),
            ("port", "Port"),
            ("user", "User"),
            ("database", "Database"),
            ("password", "Password"),
        ]
        entries: dict[str, tk.Entry] = {}
        for row_idx, (key, label) in enumerate(fields):
            tk.Label(body, text=label, anchor="w", width=10).grid(row=row_idx, column=0, sticky="w", pady=3)
            entry = tk.Entry(body, width=36, show="*" if key == "password" else "")
            entry.insert(0, str(self.db_config.get(key, "")))
            entry.grid(row=row_idx, column=1, sticky="ew", pady=3)
            entries[key] = entry

        body.grid_columnconfigure(1, weight=1)

        actions = tk.Frame(body)
        actions.grid(row=len(fields), column=0, columnspan=2, sticky="e", pady=(10, 0))

        def on_save() -> None:
            updated = {key: entries[key].get().strip() for key, _ in fields}
            if not all(updated.values()):
                messagebox.showerror("Settings", "All fields are required.")
                return
            if not updated["port"].isdigit():
                messagebox.showerror("Settings", "Port must be a number.")
                return
            new_dsn = build_psycopg2_dsn(updated)
            if not self.offline_mode:
                try:
                    test_dsn = f"{new_dsn} connect_timeout=3"
                    with psycopg2.connect(test_dsn):
                        pass
                except Exception as err:
                    messagebox.showerror("Settings", f"Connection test failed:\n{err}")
                    return

            try:
                self.DB_CONFIG_PATH.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as err:
                messagebox.showerror("Settings", f"Failed to save config:\n{err}")
                return

            self.db_config = updated
            self.db_config["offline_mode"] = self.offline_mode
            self.dsn = new_dsn
            if not self._persist_db_config():
                return
            self._append_server_message("DB settings updated and applied.")
            window.destroy()

        tk.Button(actions, text="Cancel", width=10, command=window.destroy).pack(side=tk.RIGHT, padx=(8, 0))
        tk.Button(actions, text="Save", width=10, command=on_save).pack(side=tk.RIGHT)


def main() -> None:
    root = tk.Tk()
    app = DBChatApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
