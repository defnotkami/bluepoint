"""BluePoint: measure real-world distances and areas on blueprints and plans."""

import asyncio
import copy
import csv
import io
import math
import os

import flet as ft
import flet.canvas as cv

from geometry import (
    DISPLAY_UNITS,
    UNIT_ABBR,
    UNIT_TO_METERS,
    centroid,
    format_area,
    format_length,
    nearest_point,
    parse_positive,
    polygon_area,
    polygon_perimeter,
    polyline_length,
    snap_angle,
)
from loader import LoadError, open_document

APP_NAME = "BluePoint"
SEED_COLOR = ft.Colors.INDIGO

# key -> (label, icon, keyboard shortcut, hint)
TOOLS = {
    "calibrate": (
        "Calibrate",
        ft.Icons.STRAIGHTEN,
        "C",
        "Click both ends of a distance you know.",
    ),
    "line": ("Line", ft.Icons.HORIZONTAL_RULE, "L", "Click two points to measure."),
    "polyline": (
        "Path",
        ft.Icons.TIMELINE,
        "P",
        "Click each point. Enter or right-click to finish.",
    ),
    "area": (
        "Area",
        ft.Icons.PENTAGON_OUTLINED,
        "A",
        "Click the corners. Click the first point, Enter or right-click to close.",
    ),
    "pan": ("Pan", ft.Icons.PAN_TOOL_OUTLINED, "V", "Drag to move, scroll or pinch to zoom."),
}

# Overlay colours. Drawn on the plan itself, so independent of the app theme.
KIND_COLORS = {
    "line": "#2962FF",
    "polyline": "#00A152",
    "area": "#AA00FF",
}
CAL_COLOR = "#FF1744"
PENDING_COLOR = "#FF6D00"
SELECT_COLOR = "#FFD600"
HALO_COLOR = ft.Colors.with_opacity(0.85, ft.Colors.WHITE)

KIND_LABELS = {"line": "Line", "polyline": "Path", "area": "Area"}
KIND_ICONS = {
    "line": ft.Icons.HORIZONTAL_RULE,
    "polyline": ft.Icons.TIMELINE,
    "area": ft.Icons.PENTAGON_OUTLINED,
}

SUPPORTED_TEXT = "PNG, JPEG, TIFF, WebP, HEIC, BMP, GIF, PDF, SVG and more"

MAX_ZOOM = 16.0
SNAP_RADIUS_SCREEN = 12  # px on screen


class BlueprintApp:
    def __init__(self, page: ft.Page):
        self.page = page

        # ---- document ----
        self.doc = None
        self.file_name = ""
        self.page_index = 0
        self.page_states = {}  # page index -> saved state (multi-page files)
        self.img_w = 0
        self.img_h = 0
        self.page_dpi = None

        # ---- undoable state ----
        self.tool = "calibrate"
        self.calibration = None
        self.pending = []
        self.measurements = []  # {"kind": "line"|"polyline"|"area", "points": [...]}
        self.undo_stack = []
        self.redo_stack = []

        # ---- view state ----
        self.zoom = 1.0  # best estimate of the viewer's current scale
        self.drawn_zoom = 1.0  # zoom the overlay strokes were last sized for
        self.min_zoom = 0.05
        self.zoom_base = 1.0
        self.stage_size = (900.0, 600.0)
        self.auto_fit = True
        self.view_lock = asyncio.Lock()
        self.fitting = False
        self.hover = None
        self.hover_snapped = False
        self.selected = None
        self.snap_enabled = True
        self.ortho_enabled = False
        self.typing = False
        self.narrow = None
        self.busy = False

        self.build_controls()
        self.page.on_keyboard_event = self.on_key
        self.page.on_resize = self.on_page_resize
        self.apply_layout()
        self.refresh()

    # ================================================================ build
    def build_controls(self):
        self.file_picker = ft.FilePicker()
        self.clipboard = ft.Clipboard()
        self.page.services.extend([self.file_picker, self.clipboard])

        # ---- header ----
        self.file_label = ft.Text(
            "No file open",
            size=13,
            color=ft.Colors.ON_SURFACE_VARIANT,
            overflow=ft.TextOverflow.ELLIPSIS,
            max_lines=1,
            expand=True,
        )
        self.page_dropdown = ft.Dropdown(
            width=124,
            dense=True,
            visible=False,
            border_radius=10,
            label="Page",
        )
        self.page_dropdown.on_select = self.on_page_select
        self.undo_btn = ft.IconButton(ft.Icons.UNDO, tooltip="Undo (Ctrl+Z)", on_click=self.undo)
        self.redo_btn = ft.IconButton(ft.Icons.REDO, tooltip="Redo (Ctrl+Y)", on_click=self.redo)
        self.zoom_text = ft.Text("100%", size=12, width=46, text_align=ft.TextAlign.CENTER)
        self.zoom_group = ft.Container(
            border_radius=20,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            padding=ft.Padding.symmetric(horizontal=4),
            content=ft.Row(
                [
                    ft.IconButton(ft.Icons.REMOVE, icon_size=18, tooltip="Zoom out (-)",
                                  on_click=lambda e: self.zoom_by(1 / 1.25)),
                    self.zoom_text,
                    ft.IconButton(ft.Icons.ADD, icon_size=18, tooltip="Zoom in (+)",
                                  on_click=lambda e: self.zoom_by(1.25)),
                    ft.IconButton(ft.Icons.FIT_SCREEN, icon_size=18, tooltip="Fit to screen (0)",
                                  on_click=self.fit_view),
                    ft.IconButton(ft.Icons.ONE_X_MOBILEDATA, icon_size=18,
                                  tooltip="Actual pixels (1)",
                                  on_click=lambda e: self.page.run_task(self.set_view, 1.0)),
                ],
                spacing=0,
                tight=True,
            ),
        )
        self.export_btn = ft.PopupMenuButton(
            icon=ft.Icons.IOS_SHARE,
            tooltip="Export measurements",
            items=[
                ft.PopupMenuItem(content="Copy as text", icon=ft.Icons.CONTENT_COPY,
                                 on_click=self.copy_measurements),
                ft.PopupMenuItem(content="Save as CSV", icon=ft.Icons.TABLE_VIEW,
                                 on_click=self.save_csv),
            ],
        )
        self.clear_btn = ft.IconButton(
            ft.Icons.DELETE_SWEEP_OUTLINED,
            tooltip="Clear all measurements",
            on_click=self.clear_measurements,
        )
        self.theme_btn = ft.IconButton(ft.Icons.LIGHT_MODE, on_click=self.toggle_theme)
        self.open_btn = ft.FilledButton(
            "Open",
            icon=ft.Icons.FOLDER_OPEN,
            tooltip=f"Open a plan ({SUPPORTED_TEXT})",
            on_click=self.pick_file,
        )
        self.progress = ft.ProgressRing(width=18, height=18, stroke_width=2, visible=False)

        # ---- tools ----
        self.tool_buttons = {}
        for key, (label, icon, shortcut, _) in TOOLS.items():
            self.tool_buttons[key] = ft.Container(
                content=ft.Column(
                    [ft.Icon(icon, size=22), ft.Text(label, size=11)],
                    spacing=2,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    tight=True,
                ),
                width=64,
                padding=ft.Padding.symmetric(vertical=8),
                border_radius=12,
                tooltip=f"{label} ({shortcut})",
                on_click=lambda e, k=key: self.set_tool(k),
                ink=True,
            )
        self.snap_btn = ft.IconButton(
            ft.Icons.MY_LOCATION,
            selected_icon=ft.Icons.MY_LOCATION,
            tooltip="Snap to existing points (S)",
            on_click=self.toggle_snap,
        )
        self.ortho_btn = ft.IconButton(
            ft.Icons.SQUARE_FOOT,
            selected_icon=ft.Icons.SQUARE_FOOT,
            tooltip="Lock angles to 45° steps (O)",
            on_click=self.toggle_ortho,
        )

        # ---- stage ----
        self.image = ft.Image(src="", gapless_playback=True, visible=False)
        self.canvas = cv.Canvas(shapes=[])
        self.live_canvas = cv.Canvas(shapes=[])
        self.paper = ft.Container(bgcolor=ft.Colors.WHITE)
        self.gestures = ft.GestureDetector(
            on_tap_up=self.on_tap,
            on_secondary_tap_up=self.on_secondary_tap,
            on_hover=self.on_hover,
            on_exit=self.on_exit,
            hover_interval=30,
            mouse_cursor=ft.MouseCursor.PRECISE,
            content=ft.Stack([self.paper, self.image, self.canvas, self.live_canvas]),
        )
        self.viewer = ft.InteractiveViewer(
            content=self.gestures,
            min_scale=0.05,
            max_scale=MAX_ZOOM,
            constrained=False,
            boundary_margin=ft.Margin.all(2000),
            interaction_update_interval=0,
            on_interaction_start=self.on_view_start,
            on_interaction_update=self.on_view_update,
            on_interaction_end=self.on_view_end,
            expand=True,
        )
        self.empty_state = ft.Container(
            alignment=ft.Alignment.CENTER,
            expand=True,
            content=ft.Column(
                [
                    ft.Container(
                        width=96,
                        height=96,
                        border_radius=48,
                        bgcolor=ft.Colors.PRIMARY_CONTAINER,
                        alignment=ft.Alignment.CENTER,
                        content=ft.Icon(ft.Icons.ARCHITECTURE, size=52,
                                        color=ft.Colors.ON_PRIMARY_CONTAINER),
                    ),
                    ft.Text("Open a blueprint to start measuring", size=20,
                            weight=ft.FontWeight.BOLD, text_align=ft.TextAlign.CENTER),
                    ft.Text(SUPPORTED_TEXT, color=ft.Colors.ON_SURFACE_VARIANT,
                            text_align=ft.TextAlign.CENTER),
                    ft.Container(height=4),
                    ft.FilledButton("Open file", icon=ft.Icons.FOLDER_OPEN,
                                    on_click=self.pick_file),
                    ft.Container(height=12),
                    ft.Text(
                        "1. Calibrate on a known length or drawing scale\n"
                        "2. Measure lines, paths and areas\n"
                        "3. Export the results",
                        size=13,
                        color=ft.Colors.ON_SURFACE_VARIANT,
                    ),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=8,
                tight=True,
            ),
        )
        self.hint_text = ft.Text("", size=13, color=ft.Colors.ON_INVERSE_SURFACE)
        self.live_text = ft.Text("", size=13, weight=ft.FontWeight.BOLD,
                                 color=ft.Colors.INVERSE_PRIMARY)
        self.hint_pill = ft.Container(
            bottom=12,
            left=12,
            right=12,
            alignment=ft.Alignment.BOTTOM_CENTER,
            content=ft.Container(
                padding=ft.Padding.symmetric(horizontal=14, vertical=8),
                border_radius=20,
                bgcolor=ft.Colors.with_opacity(0.92, ft.Colors.INVERSE_SURFACE),
                content=ft.Row([self.hint_text, self.live_text], spacing=12, tight=True,
                               wrap=True),
            ),
        )
        self.finish_btn = ft.FilledButton("Finish", icon=ft.Icons.CHECK,
                                          tooltip="Finish shape (Enter)",
                                          on_click=self.finish_shape)
        self.undo_point_btn = ft.OutlinedButton("Undo point", icon=ft.Icons.BACKSPACE_OUTLINED,
                                                tooltip="Remove last point (Backspace)",
                                                on_click=self.remove_last_point)
        self.cancel_btn = ft.OutlinedButton("Cancel", icon=ft.Icons.CLOSE,
                                            tooltip="Cancel shape (Esc)",
                                            on_click=self.cancel_pending)
        self.shape_bar = ft.Container(
            top=12,
            left=12,
            right=12,
            alignment=ft.Alignment.TOP_CENTER,
            visible=False,
            content=ft.Container(
                padding=6,
                border_radius=24,
                bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
                shadow=ft.BoxShadow(blur_radius=12, color=ft.Colors.with_opacity(0.25, ft.Colors.BLACK)),
                content=ft.Row([self.finish_btn, self.undo_point_btn, self.cancel_btn],
                               spacing=6, tight=True),
            ),
        )
        # The viewer stays mounted all the time so zoom calls always reach it;
        # the welcome screen simply covers it until a file is open.
        self.empty_state.bgcolor = ft.Colors.SURFACE_CONTAINER_LOW
        self.stage_body = ft.Stack([self.viewer, self.empty_state], expand=True)
        self.stage = ft.Container(
            expand=True,
            border_radius=16,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            on_size_change=self.on_stage_resize,
            size_change_interval=100,
            content=ft.Stack(
                [self.stage_body, self.shape_bar, self.hint_pill],
                expand=True,
            ),
        )

        # ---- calibration panel ----
        self.cal_mode = ft.SegmentedButton(
            selected=["points"],
            show_selected_icon=False,
            segments=[
                ft.Segment(value="points", label="Known length", icon=ft.Icon(ft.Icons.STRAIGHTEN)),
                ft.Segment(value="scale", label="Drawing scale", icon=ft.Icon(ft.Icons.ASPECT_RATIO)),
            ],
            on_change=self.on_cal_mode_change,
        )
        self.ref_field = ft.TextField(
            label="Known distance",
            value="1",
            dense=True,
            border_radius=10,
            expand=True,
            keyboard_type=ft.KeyboardType.NUMBER,
            on_change=self.on_ref_change,
            on_focus=self.on_field_focus,
            on_blur=self.on_field_blur,
        )
        self.ref_unit = ft.Dropdown(
            value="meters",
            dense=True,
            width=130,
            border_radius=10,
            options=[ft.DropdownOption(u) for u in UNIT_TO_METERS],
        )
        self.ref_unit.on_select = self.on_ref_change
        self.points_box = ft.Column(
            [
                ft.Text("Pick Calibrate, then click both ends of a dimension "
                        "you know (a door, a dimension line...).",
                        size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                ft.Row([self.ref_field, self.ref_unit], spacing=8),
                ft.FilledTonalButton("Click two points", icon=ft.Icons.ADS_CLICK,
                                     on_click=lambda e: self.set_tool("calibrate")),
            ],
            spacing=10,
        )
        self.ratio_field = ft.TextField(
            label="Scale  1 :",
            value="100",
            dense=True,
            border_radius=10,
            expand=True,
            keyboard_type=ft.KeyboardType.NUMBER,
            on_submit=self.apply_scale,
            on_focus=self.on_field_focus,
            on_blur=self.on_field_blur,
        )
        self.dpi_field = ft.TextField(
            label="Resolution (DPI)",
            dense=True,
            border_radius=10,
            expand=True,
            keyboard_type=ft.KeyboardType.NUMBER,
            on_submit=self.apply_scale,
            on_focus=self.on_field_focus,
            on_blur=self.on_field_blur,
        )
        self.dpi_note = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT)
        self.scale_box = ft.Column(
            [
                ft.Text("Use the scale printed on the drawing, e.g. 1:100 "
                        "(1/8\" = 1' is 1:96).",
                        size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                ft.Row([self.ratio_field, self.dpi_field], spacing=8),
                self.dpi_note,
                ft.FilledTonalButton("Apply scale", icon=ft.Icons.CHECK, on_click=self.apply_scale),
            ],
            spacing=10,
            visible=False,
        )
        self.cal_status_icon = ft.Icon(ft.Icons.ERROR_OUTLINE, size=18)
        self.cal_status_text = ft.Text("", size=13, weight=ft.FontWeight.W_600, expand=True)
        self.cal_status = ft.Container(
            padding=ft.Padding.symmetric(horizontal=12, vertical=8),
            border_radius=10,
            content=ft.Row([self.cal_status_icon, self.cal_status_text], spacing=8),
        )

        # ---- measurements panel ----
        self.display_unit = ft.Dropdown(
            value="meters",
            dense=True,
            width=140,
            border_radius=10,
            options=[
                ft.DropdownOption(key=u, text="ft & in" if u == "feet & inches" else u)
                for u in DISPLAY_UNITS
            ],
        )
        self.display_unit.on_select = lambda e: self.refresh()
        self.measurement_list = ft.ListView(expand=True, spacing=6)
        self.total_length_text = ft.Text("-", size=15, weight=ft.FontWeight.BOLD,
                                         color=ft.Colors.ON_PRIMARY_CONTAINER)
        self.total_area_text = ft.Text("-", size=15, weight=ft.FontWeight.BOLD,
                                       color=ft.Colors.ON_PRIMARY_CONTAINER)

    def card(self, icon, title, controls, expand=False, trailing=None):
        header = [ft.Icon(icon, size=20, color=ft.Colors.PRIMARY),
                  ft.Text(title, size=15, weight=ft.FontWeight.BOLD, expand=True)]
        if trailing is not None:
            header.append(trailing)
        return ft.Container(
            expand=expand,
            padding=14,
            border_radius=16,
            bgcolor=ft.Colors.SURFACE_CONTAINER,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            content=ft.Column(
                [ft.Row(header, spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                 *controls],
                spacing=12,
                expand=expand,
            ),
        )

    def apply_layout(self):
        width = self.page.width or 1400
        narrow = width < 900
        if narrow == self.narrow:
            return
        self.narrow = narrow

        left = [
            ft.Icon(ft.Icons.ARCHITECTURE, color=ft.Colors.PRIMARY),
            ft.Text(APP_NAME, size=18, weight=ft.FontWeight.BOLD, visible=not narrow),
            ft.Container(width=4),
            self.open_btn,
            self.page_dropdown,
            self.progress,
        ]
        actions = [self.undo_btn, self.redo_btn, self.export_btn, self.clear_btn, self.theme_btn]
        center = ft.CrossAxisAlignment.CENTER
        if narrow:
            content = ft.Column(
                [
                    ft.Row(left + actions, spacing=2, scroll=ft.ScrollMode.AUTO,
                           vertical_alignment=center),
                    ft.Row([self.zoom_group, self.file_label], spacing=8,
                           vertical_alignment=center),
                ],
                spacing=4,
            )
        else:
            content = ft.Row(left + [self.file_label, self.zoom_group] + actions,
                             spacing=6, vertical_alignment=center)
        header = ft.Container(
            padding=ft.Padding.symmetric(horizontal=12, vertical=6),
            border_radius=16,
            bgcolor=ft.Colors.SURFACE_CONTAINER,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            content=content,
        )

        tools = list(self.tool_buttons.values())
        toggles = [self.snap_btn, self.ortho_btn]
        if narrow:
            tool_bar = ft.Row(tools + [ft.VerticalDivider(width=8)] + toggles,
                              scroll=ft.ScrollMode.AUTO, spacing=4)
        else:
            tool_bar = ft.Container(
                width=80,
                padding=ft.Padding.symmetric(vertical=8),
                border_radius=16,
                bgcolor=ft.Colors.SURFACE_CONTAINER,
                border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
                content=ft.Column(
                    tools + [ft.Divider(height=12)] + toggles,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=4,
                    scroll=ft.ScrollMode.AUTO,
                ),
            )

        calibration_card = self.card(
            ft.Icons.STRAIGHTEN, "Scale",
            [self.cal_status, self.cal_mode, self.points_box, self.scale_box],
        )
        totals = ft.Container(
            padding=ft.Padding.symmetric(horizontal=12, vertical=10),
            border_radius=12,
            bgcolor=ft.Colors.PRIMARY_CONTAINER,
            content=ft.Column(
                [
                    ft.Row([ft.Icon(ft.Icons.FUNCTIONS, size=18, color=ft.Colors.ON_PRIMARY_CONTAINER),
                            ft.Text("Length", size=13, color=ft.Colors.ON_PRIMARY_CONTAINER, expand=True),
                            self.total_length_text]),
                    ft.Row([ft.Icon(ft.Icons.SQUARE_FOOT, size=18, color=ft.Colors.ON_PRIMARY_CONTAINER),
                            ft.Text("Area", size=13, color=ft.Colors.ON_PRIMARY_CONTAINER, expand=True),
                            self.total_area_text]),
                ],
                spacing=4,
            ),
        )
        measurements_card = self.card(
            ft.Icons.RULE, "Measurements",
            [self.measurement_list, totals],
            expand=True,
            trailing=self.display_unit,
        )

        if narrow:
            measurements_card.height = 340
            measurements_card.expand = False
            side = ft.Column([calibration_card, measurements_card], spacing=12)
            self.stage.height = max(320, (self.page.height or 700) * 0.6)
            self.stage.expand = False
            body = ft.Column(
                [header, tool_bar, self.stage, side],
                spacing=10,
                expand=True,
                scroll=ft.ScrollMode.AUTO,
            )
        else:
            self.stage.height = None
            self.stage.expand = True
            side = ft.Container(
                width=340,
                content=ft.Column([calibration_card, measurements_card], spacing=12, expand=True),
            )
            body = ft.Column(
                [
                    header,
                    ft.Row([tool_bar, self.stage, side], expand=True, spacing=12,
                           vertical_alignment=ft.CrossAxisAlignment.STRETCH),
                ],
                spacing=12,
                expand=True,
            )
        self.page.controls.clear()
        self.page.add(body)

    def on_page_resize(self, e):
        before = self.narrow
        self.apply_layout()
        if before != self.narrow:
            self.refresh()
            if self.doc is not None:
                # The viewer was rebuilt, which resets its zoom: fit again
                # once the new layout has reported its size.
                self.page.run_task(self.refit_later)

    async def refit_later(self):
        await asyncio.sleep(0.3)
        await self.fit_view()

    # ================================================================ theme
    def toggle_theme(self, e=None):
        dark = self.page.theme_mode == ft.ThemeMode.DARK
        self.page.theme_mode = ft.ThemeMode.LIGHT if dark else ft.ThemeMode.DARK
        self.update_theme_icon()
        self.page.update()

    def update_theme_icon(self):
        dark = self.page.theme_mode == ft.ThemeMode.DARK
        self.theme_btn.icon = ft.Icons.LIGHT_MODE if dark else ft.Icons.DARK_MODE
        self.theme_btn.tooltip = "Switch to light mode" if dark else "Switch to dark mode"

    def notify(self, message, error=False):
        self.page.show_dialog(
            ft.SnackBar(
                ft.Text(message),
                bgcolor=ft.Colors.ERROR_CONTAINER if error else None,
                behavior=ft.SnackBarBehavior.FLOATING,
                show_close_icon=True,
            )
        )

    # ============================================================== loading
    async def pick_file(self, e=None):
        if self.busy:
            return
        try:
            files = await self.file_picker.pick_files(
                dialog_title="Open a blueprint",
                file_type=ft.FilePickerFileType.ANY,
                allow_multiple=False,
                with_data=True,
            )
        except Exception as ex:
            self.notify(f"Could not open the file picker: {ex}", error=True)
            return
        if not files:
            return
        f = files[0]
        data = f.bytes
        if not data and f.path:
            try:
                with open(f.path, "rb") as fh:
                    data = fh.read()
            except OSError as ex:
                self.notify(f"Could not read {f.name}: {ex}", error=True)
                return
        if not data:
            self.notify("Could not read the selected file.", error=True)
            return
        await self.load_bytes(data, f.name or os.path.basename(f.path or "") or "file")

    async def set_busy(self, busy, message=None):
        self.busy = busy
        self.progress.visible = busy
        self.open_btn.disabled = busy
        self.page_dropdown.disabled = busy
        if message:
            self.hint_text.value = message
            self.live_text.value = ""
            self.hint_pill.visible = True
        self.page.update()
        if busy:
            await asyncio.sleep(0.05)  # let the spinner paint before heavy work

    async def load_bytes(self, data, name):
        await self.set_busy(True, f"Opening {name}…")
        try:
            doc = open_document(data, name)
            rendered = doc.render(0)
        except LoadError as ex:
            await self.set_busy(False)
            self.notify(str(ex), error=True)
            self.refresh()
            return
        except Exception as ex:  # unexpected decoder problems
            await self.set_busy(False)
            self.notify(f"Could not open {name}: {ex}", error=True)
            self.refresh()
            return

        self.doc = doc
        self.file_name = name
        self.page_states = {}
        self.page_index = 0
        self.page_dropdown.options = [
            ft.DropdownOption(key=str(i), text=f"Page {i + 1}") for i in range(doc.page_count)
        ]
        self.page_dropdown.value = "0"
        self.page_dropdown.visible = doc.page_count > 1
        self.reset_state()
        await self.show_page(rendered)
        await self.set_busy(False)
        self.refresh()

    def reset_state(self):
        self.tool = "calibrate"
        self.calibration = None
        self.pending = []
        self.measurements = []
        self.undo_stack = []
        self.redo_stack = []
        self.selected = None
        self.hover = None

    async def show_page(self, rendered):
        self.img_w, self.img_h = rendered.width, rendered.height
        self.page_dpi = rendered.dpi
        size = dict(width=self.img_w, height=self.img_h)
        # Every layer has the exact pixel size of the page, so the plan is never
        # stretched and click positions are always true image pixels.
        self.image.src = rendered.data
        self.image.fit = ft.BoxFit.FILL
        self.image.visible = True
        for ctrl in (self.paper, self.image, self.canvas, self.live_canvas, self.gestures.content):
            ctrl.width, ctrl.height = size["width"], size["height"]
        margin = max(self.img_w, self.img_h, 2000)
        self.viewer.boundary_margin = ft.Margin.all(margin)
        self.empty_state.visible = False

        label = self.file_name
        if self.doc.page_count > 1:
            label += f"  ·  page {self.page_index + 1} of {self.doc.page_count}"
        label += f"  ·  {self.img_w}×{self.img_h} px"
        self.file_label.value = label
        self.file_label.tooltip = label

        if self.page_dpi:
            self.dpi_field.value = f"{self.page_dpi:.4g}"
            exact = self.doc.kind == "document"
            self.dpi_note.value = (
                "Exact resolution from the document." if exact
                else "Resolution from the image file. Check it matches the scan."
            )
        else:
            self.dpi_field.value = ""
            self.dpi_note.value = "Enter the scan resolution (DPI) of this image."
        self.sync_fields_from_calibration()
        self.page.update()
        await asyncio.sleep(0.05)  # let the new page lay out before fitting
        await self.fit_view()

    async def on_page_select(self, e):
        if self.doc is None or self.busy:
            return
        index = int(self.page_dropdown.value or 0)
        if index == self.page_index:
            return
        self.page_states[self.page_index] = self.save_page_state()
        await self.set_busy(True, f"Rendering page {index + 1}…")
        try:
            rendered = self.doc.render(index)
        except Exception as ex:
            await self.set_busy(False)
            self.page_dropdown.value = str(self.page_index)
            self.notify(f"Could not render page {index + 1}: {ex}", error=True)
            self.refresh()
            return
        self.page_index = index
        state = self.page_states.get(index)
        if state:
            self.load_page_state(state)
        else:
            self.reset_state()
        await self.show_page(rendered)
        await self.set_busy(False)
        self.refresh()

    def save_page_state(self):
        return {
            "snapshot": self.snapshot(),
            "undo": self.undo_stack,
            "redo": self.redo_stack,
        }

    def load_page_state(self, state):
        self.restore(state["snapshot"])
        self.undo_stack = state["undo"]
        self.redo_stack = state["redo"]
        self.selected = None

    # =============================================================== history
    def snapshot(self):
        return copy.deepcopy({
            "tool": self.tool,
            "calibration": self.calibration,
            "pending": self.pending,
            "measurements": self.measurements,
        })

    def restore(self, snap):
        self.tool = snap["tool"]
        self.calibration = snap["calibration"]
        self.pending = snap["pending"]
        self.measurements = snap["measurements"]
        self.sync_fields_from_calibration()

    def push_history(self):
        """Call BEFORE changing any undoable state."""
        self.undo_stack.append(self.snapshot())
        if len(self.undo_stack) > 300:
            self.undo_stack.pop(0)
        self.redo_stack.clear()

    def undo(self, e=None):
        if not self.undo_stack:
            return
        self.redo_stack.append(self.snapshot())
        self.restore(self.undo_stack.pop())
        self.selected = None
        self.refresh()

    def redo(self, e=None):
        if not self.redo_stack:
            return
        self.undo_stack.append(self.snapshot())
        self.restore(self.redo_stack.pop())
        self.selected = None
        self.refresh()

    # ============================================================== keyboard
    def on_field_focus(self, e):
        self.typing = True

    def on_field_blur(self, e):
        self.typing = False

    async def on_key(self, e: ft.KeyboardEvent):
        key = (e.key or "").upper()
        if e.ctrl or e.meta:
            if key == "Z":
                self.redo() if e.shift else self.undo()
            elif key == "Y":
                self.redo()
            elif key == "O":
                await self.pick_file()
            return
        if self.typing:
            return  # don't steal keys while the user types in a field
        if key == "ENTER":
            self.finish_shape()
        elif key == "ESCAPE":
            if self.pending:
                self.cancel_pending()
            elif self.selected is not None:
                self.select(None)
        elif key == "BACKSPACE":
            self.remove_last_point()
        elif key == "DELETE" and self.selected is not None:
            self.delete_measurement(self.selected)
        elif key in ("+", "=", "NUMPAD ADD"):
            await self.zoom_by(1.25)
        elif key in ("-", "NUMPAD SUBTRACT"):
            await self.zoom_by(1 / 1.25)
        elif key == "0":
            await self.fit_view()
        elif key == "1":
            await self.set_view(1.0)
        elif key == "S":
            self.toggle_snap()
        elif key == "O":
            self.toggle_ortho()
        else:
            for tool, (_, _, shortcut, _) in TOOLS.items():
                if key == shortcut:
                    self.set_tool(tool)
                    break

    # ================================================================== view
    def on_stage_resize(self, e: ft.LayoutSizeChangeEvent):
        changed = (e.width, e.height) != self.stage_size
        self.stage_size = (e.width, e.height)
        # Keep the page fitted while the window resizes, until the user zooms.
        if changed and (self.auto_fit or self.fitting) and self.doc is not None:
            self.page.run_task(self.fit_view)

    def fit_scale(self):
        sw, sh = self.stage_size
        if not self.img_w or not self.img_h:
            return 1.0
        return max(0.01, min((sw - 24) / self.img_w, (sh - 24) / self.img_h))

    async def set_view(self, scale):
        """Show the page at `scale`, centred in the stage."""
        if self.doc is None:
            return
        async with self.view_lock:  # zoom() is relative, so never interleave
            await self._set_view(scale)

    async def _set_view(self, scale):
        fit = self.fit_scale()
        self.min_zoom = min(0.5, fit / 2)
        self.viewer.min_scale = self.min_zoom
        self.viewer.max_scale = max(MAX_ZOOM, fit * 4)
        self.safe_update(self.viewer)
        scale = max(self.min_zoom, min(self.viewer.max_scale, scale))
        sw, sh = self.stage_size
        # pan() moves in page pixels, so divide the screen offset by scale.
        dx = (sw - self.img_w * scale) / 2 / scale
        dy = (sh - self.img_h * scale) / 2 / scale
        for attempt in range(3):
            try:
                await asyncio.wait_for(self.viewer.reset(), 1.5)
                await asyncio.wait_for(self.viewer.zoom(scale), 1.5)
                await asyncio.wait_for(self.viewer.pan(dx, dy), 1.5)
                break
            except Exception:  # viewer still being rebuilt: try again
                await asyncio.sleep(0.3)
        self.zoom = scale
        self.auto_fit = False
        self.after_zoom()

    async def fit_view(self, e=None):
        self.fitting = True
        try:
            await self.set_view(self.fit_scale())
        finally:
            self.fitting = False
        self.auto_fit = True

    async def zoom_by(self, factor):
        if self.doc is None:
            return
        new = max(self.min_zoom, min(self.viewer.max_scale, self.zoom * factor))
        if new == self.zoom:
            return
        try:
            await self.viewer.zoom(new / self.zoom)
        except Exception:
            return
        self.zoom = new
        self.auto_fit = False
        self.after_zoom()

    def on_view_start(self, e):
        self.zoom_base = self.zoom
        self.auto_fit = False

    def on_view_update(self, e: ft.ScaleUpdateEvent):
        if e.scale and e.scale != 1:
            self.zoom = max(self.min_zoom, min(self.viewer.max_scale, self.zoom_base * e.scale))
            self.zoom_text.value = f"{self.zoom * 100:.0f}%"
            self.safe_update(self.zoom_text)

    def on_view_end(self, e):
        self.after_zoom()

    def after_zoom(self):
        self.zoom_text.value = f"{self.zoom * 100:.0f}%"
        # Overlay strokes are sized in screen pixels; redraw them only when
        # the zoom changed enough to notice.
        if abs(math.log(self.zoom / self.drawn_zoom)) > 0.12:
            self.redraw()
            self.draw_live()
        self.page.update()

    # ================================================================= tools
    def set_tool(self, tool):
        if tool == self.tool:
            return
        self.pending = []  # unfinished shapes are discarded when switching
        self.tool = tool
        self.refresh()

    def toggle_snap(self, e=None):
        self.snap_enabled = not self.snap_enabled
        self.refresh()

    def toggle_ortho(self, e=None):
        self.ortho_enabled = not self.ortho_enabled
        self.refresh()

    # ============================================================ calibration
    @property
    def px_per_meter(self):
        c = self.calibration
        if not c:
            return None
        if c["mode"] == "points":
            px = math.dist(*c["points"])
            meters = c["ref"] * UNIT_TO_METERS[c["unit"]]
            return px / meters if meters > 0 and px > 0 else None
        # Drawing scale: one real meter is (1/ratio) m on paper, i.e.
        # dpi / (ratio * 0.0254) pixels.
        return c["dpi"] / (c["ratio"] * 0.0254)

    def sync_fields_from_calibration(self):
        c = self.calibration
        if not c:
            return
        if c["mode"] == "points":
            self.ref_field.value = f"{c['ref']:g}"
            self.ref_unit.value = c["unit"]
            self.cal_mode.selected = ["points"]
        else:
            self.ratio_field.value = f"{c['ratio']:g}"
            self.dpi_field.value = f"{c['dpi']:g}"
            self.cal_mode.selected = ["scale"]

    def on_cal_mode_change(self, e):
        self.refresh()

    def on_ref_change(self, e):
        """Changing the known distance re-scales every measurement instantly."""
        ref = parse_positive(self.ref_field.value)
        self.ref_field.error = None if ref else "Enter a number > 0"
        c = self.calibration
        if ref and c and c["mode"] == "points":
            c["ref"] = ref
            c["unit"] = self.ref_unit.value or "meters"
        self.refresh()

    def apply_scale(self, e=None):
        ratio = parse_positive(self.ratio_field.value)
        dpi = parse_positive(self.dpi_field.value)
        self.ratio_field.error = None if ratio else "e.g. 100 or 1:100"
        self.dpi_field.error = None if dpi else "Required"
        if not (ratio and dpi):
            self.refresh()
            return
        if self.doc is None:
            self.notify("Open a file first.")
            return
        self.push_history()
        self.calibration = {"mode": "scale", "ratio": ratio, "dpi": dpi}
        self.pending = [] if self.tool == "calibrate" else self.pending
        if self.tool == "calibrate":
            self.tool = "line"
        self.refresh()
        self.notify(f"Scale set to 1:{ratio:g} at {dpi:g} DPI.")

    def calibration_summary(self):
        c, ppm = self.calibration, self.px_per_meter
        if not c or not ppm:
            return None
        unit = self.display_unit.value or "meters"
        if c["mode"] == "points":
            how = f"{c['ref']:g} {c['unit']} reference"
        else:
            how = f"1:{c['ratio']:g} at {c['dpi']:g} DPI"
        one = format_length(100 / ppm, unit, 3)
        return f"{how} · 100 px = {one}"

    # ================================================================ clicks
    def snap_candidates(self):
        pts = [p for m in self.measurements for p in m["points"]]
        if self.calibration and self.calibration["mode"] == "points":
            pts += self.calibration["points"]
        if self.tool == "area" and len(self.pending) >= 3:
            pts.append(self.pending[0])
        return pts

    def resolve_point(self, x, y):
        """Apply snapping/angle lock and clamp to the page. Returns (point, snapped)."""
        p = (min(max(x, 0.0), float(self.img_w)), min(max(y, 0.0), float(self.img_h)))
        if self.snap_enabled:
            hit = nearest_point(p, self.snap_candidates(), SNAP_RADIUS_SCREEN / self.zoom)
            if hit is not None:
                return tuple(hit), True
        if self.ortho_enabled and self.pending:
            return snap_angle(self.pending[-1], p), False
        return p, False

    def on_tap(self, e: ft.TapEvent):
        # on_tap_up only fires for real taps, never at the start of a drag, so
        # panning the plan no longer drops stray points.
        if self.doc is None or self.tool == "pan" or e.local_position is None:
            return
        pos, snapped = self.resolve_point(e.local_position.x, e.local_position.y)
        self.selected = None

        if self.pending and math.dist(pos, self.pending[-1]) < 1e-6:
            return  # double click on the same spot

        if self.tool == "area" and len(self.pending) >= 3 and snapped and pos == tuple(self.pending[0]):
            self.finish_shape()
            return

        self.push_history()
        self.pending.append(pos)

        if self.tool == "calibrate" and len(self.pending) == 2:
            self.finish_calibration()
        elif self.tool == "line" and len(self.pending) == 2:
            self.measurements.append({"kind": "line", "points": self.pending})
            self.pending = []
        self.refresh()

    def finish_calibration(self):
        ref = parse_positive(self.ref_field.value)
        if not ref:
            self.pending = []
            self.ref_field.error = "Enter a number > 0"
            self.notify("Enter the known distance first, then click the two points again.",
                        error=True)
            return
        self.calibration = {
            "mode": "points",
            "points": self.pending,
            "ref": ref,
            "unit": self.ref_unit.value or "meters",
        }
        self.cal_mode.selected = ["points"]
        self.pending = []
        self.tool = "line"
        self.notify("Calibrated. Line tool selected.")

    def on_secondary_tap(self, e):
        if self.pending:
            if not self.finish_shape():
                self.cancel_pending()

    def on_hover(self, e: ft.HoverEvent):
        if self.doc is None or self.tool == "pan" or e.local_position is None:
            if self.hover is not None:
                self.on_exit()
            return
        self.hover, self.hover_snapped = self.resolve_point(e.local_position.x, e.local_position.y)
        self.draw_live(update=True)

    def on_exit(self, e=None):
        self.hover = None
        self.draw_live(update=True)

    def finish_shape(self, e=None):
        """Finish the current path/area. Returns True when a shape was added."""
        if self.tool == "polyline" and len(self.pending) >= 2:
            kind = "polyline"
        elif self.tool == "area" and len(self.pending) >= 3:
            kind = "area"
        else:
            return False
        self.push_history()
        self.measurements.append({"kind": kind, "points": self.pending})
        self.pending = []
        self.refresh()
        return True

    def remove_last_point(self, e=None):
        if not self.pending:
            return
        self.push_history()
        self.pending = self.pending[:-1]
        self.refresh()

    def cancel_pending(self, e=None):
        if not self.pending:
            return
        self.push_history()
        self.pending = []
        self.refresh()

    # =============================================================== actions
    def clear_measurements(self, e=None):
        if not self.measurements and not self.pending:
            return
        self.push_history()
        self.measurements = []
        self.pending = []
        self.selected = None
        self.refresh()
        self.notify("Measurements cleared. Ctrl+Z to undo.")

    def delete_measurement(self, index):
        if 0 <= index < len(self.measurements):
            self.push_history()
            self.measurements.pop(index)
            self.selected = None
            self.refresh()

    def select(self, index):
        self.selected = None if index == self.selected else index
        self.refresh()

    # ============================================================ values
    @property
    def unit(self):
        return self.display_unit.value or "meters"

    def value_of(self, m):
        """(text, meters or m², kind) for one measurement."""
        ppm = self.px_per_meter
        pts = m["points"]
        if m["kind"] == "area":
            px2 = polygon_area(pts)
            if not ppm:
                return f"{px2:,.0f} px²", None
            sq = px2 / ppm ** 2
            return format_area(sq, self.unit), sq
        px = polyline_length(pts)
        if not ppm:
            return f"{px:,.1f} px", None
        meters = px / ppm
        return format_length(meters, self.unit), meters

    def detail_of(self, m):
        ppm = self.px_per_meter
        pts = m["points"]
        if m["kind"] == "area":
            per = polygon_perimeter(pts)
            per_text = format_length(per / ppm, self.unit) if ppm else f"{per:,.1f} px"
            return f"Area · {len(pts)} corners · perimeter {per_text}"
        if m["kind"] == "polyline":
            return f"Path · {len(pts) - 1} segments"
        return "Line"

    def pending_value(self, points, closed=False):
        ppm = self.px_per_meter
        if closed:
            px2 = polygon_area(points)
            return format_area(px2 / ppm ** 2, self.unit) if ppm else f"{px2:,.0f} px²"
        px = polyline_length(points)
        return format_length(px / ppm, self.unit) if ppm else f"{px:,.1f} px"

    # ============================================================== render
    def refresh(self):
        self.refresh_panels()
        self.refresh_toolbar()
        self.redraw()
        self.draw_live()
        self.page.update()

    def refresh_panels(self):
        # calibration status
        summary = self.calibration_summary()
        if summary:
            self.cal_status.bgcolor = ft.Colors.with_opacity(0.15, ft.Colors.GREEN)
            self.cal_status_icon.name = ft.Icons.CHECK_CIRCLE
            self.cal_status_icon.color = ft.Colors.GREEN
            self.cal_status_text.value = summary
        else:
            self.cal_status.bgcolor = ft.Colors.with_opacity(0.15, ft.Colors.AMBER)
            self.cal_status_icon.name = ft.Icons.WARNING_AMBER_ROUNDED
            self.cal_status_icon.color = ft.Colors.AMBER
            self.cal_status_text.value = "Not calibrated. Values show in pixels."
        mode = (list(self.cal_mode.selected or ["points"]) or ["points"])[0]
        self.points_box.visible = mode == "points"
        self.scale_box.visible = mode == "scale"

        # measurement list
        controls = []
        total_len = total_area = 0.0
        has_len = has_area = False
        for i, m in enumerate(self.measurements):
            text, real = self.value_of(m)
            if real is not None:
                if m["kind"] == "area":
                    total_area += real
                    has_area = True
                else:
                    total_len += real
                    has_len = True
            color = KIND_COLORS[m["kind"]]
            selected = i == self.selected
            controls.append(
                ft.Container(
                    padding=ft.Padding.only(left=10, right=2, top=6, bottom=6),
                    border_radius=12,
                    bgcolor=ft.Colors.SECONDARY_CONTAINER if selected else ft.Colors.SURFACE_CONTAINER_HIGH,
                    border=ft.Border.all(2 if selected else 1,
                                         ft.Colors.PRIMARY if selected else ft.Colors.TRANSPARENT),
                    on_click=lambda e, idx=i: self.select(idx),
                    ink=True,
                    content=ft.Row(
                        [
                            ft.Container(
                                width=30, height=30, border_radius=15, bgcolor=color,
                                alignment=ft.Alignment.CENTER,
                                content=ft.Text(str(i + 1), size=12, weight=ft.FontWeight.BOLD,
                                                color=ft.Colors.WHITE),
                            ),
                            ft.Column(
                                [ft.Text(text, size=15, weight=ft.FontWeight.W_600, selectable=True),
                                 ft.Row([ft.Icon(KIND_ICONS[m["kind"]], size=13,
                                                 color=ft.Colors.ON_SURFACE_VARIANT),
                                         ft.Text(self.detail_of(m), size=11,
                                                 color=ft.Colors.ON_SURFACE_VARIANT,
                                                 expand=True)],
                                        spacing=4)],
                                spacing=0,
                                expand=True,
                                tight=True,
                            ),
                            ft.IconButton(ft.Icons.DELETE_OUTLINE, icon_size=18, tooltip="Delete",
                                          on_click=lambda e, idx=i: self.delete_measurement(idx)),
                        ],
                        spacing=10,
                    ),
                )
            )
        if not controls:
            controls.append(
                ft.Container(
                    padding=20,
                    alignment=ft.Alignment.CENTER,
                    content=ft.Column(
                        [ft.Icon(ft.Icons.STRAIGHTEN, color=ft.Colors.OUTLINE),
                         ft.Text("No measurements yet", italic=True,
                                 color=ft.Colors.ON_SURFACE_VARIANT)],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        tight=True,
                    ),
                )
            )
        self.measurement_list.controls = controls
        self.total_length_text.value = format_length(total_len, self.unit) if has_len else "-"
        self.total_area_text.value = format_area(total_area, self.unit) if has_area else "-"

        # hint pill
        self.hint_pill.visible = self.doc is not None
        if self.doc is not None:
            hint = TOOLS[self.tool][3]
            if self.tool == "polyline" and self.pending:
                hint = f"{len(self.pending)} points · Enter or right-click to finish."
            elif self.tool == "area" and self.pending:
                hint = f"{len(self.pending)} corners · click the first point or press Enter to close."
            elif self.tool == "calibrate" and len(self.pending) == 1:
                hint = "Now click the other end of the known distance."
            elif self.tool == "line" and len(self.pending) == 1:
                hint = "Click the second point."
            elif self.tool in ("line", "polyline", "area") and not self.px_per_meter:
                hint += " (Not calibrated: values in pixels.)"
            self.hint_text.value = hint

    def refresh_toolbar(self):
        has_doc = self.doc is not None
        for key, btn in self.tool_buttons.items():
            sel = key == self.tool and has_doc
            btn.bgcolor = ft.Colors.PRIMARY_CONTAINER if sel else None
            color = ft.Colors.ON_PRIMARY_CONTAINER if sel else ft.Colors.ON_SURFACE_VARIANT
            icon, label = btn.content.controls
            icon.color = color
            label.color = color
            label.weight = ft.FontWeight.BOLD if sel else None
            btn.disabled = not has_doc
            btn.opacity = 1 if has_doc else 0.5
        self.snap_btn.selected = self.snap_enabled
        self.snap_btn.icon_color = ft.Colors.PRIMARY if self.snap_enabled else ft.Colors.OUTLINE
        self.ortho_btn.selected = self.ortho_enabled
        self.ortho_btn.icon_color = ft.Colors.PRIMARY if self.ortho_enabled else ft.Colors.OUTLINE
        self.undo_btn.disabled = not self.undo_stack
        self.redo_btn.disabled = not self.redo_stack
        self.clear_btn.disabled = not (self.measurements or self.pending)
        self.export_btn.disabled = not self.measurements
        self.zoom_group.disabled = not has_doc
        self.zoom_text.value = f"{self.zoom * 100:.0f}%" if has_doc else "–"
        multi = self.tool in ("polyline", "area")
        self.shape_bar.visible = bool(self.pending)
        self.finish_btn.visible = multi
        self.finish_btn.disabled = not (
            (self.tool == "polyline" and len(self.pending) >= 2)
            or (self.tool == "area" and len(self.pending) >= 3)
        )
        self.undo_point_btn.visible = multi
        self.gestures.mouse_cursor = (
            ft.MouseCursor.GRAB if self.tool == "pan" else ft.MouseCursor.PRECISE
        )

    # ---------------------------------------------------------------- canvas
    def paints(self, color, width, dash=None, fill=False):
        z = self.zoom
        return ft.Paint(
            color=color,
            stroke_width=width / z,
            stroke_cap=ft.StrokeCap.ROUND,
            stroke_join=ft.StrokeJoin.ROUND,
            style=ft.PaintingStyle.FILL if fill else ft.PaintingStyle.STROKE,
            stroke_dash_pattern=[d / z for d in dash] if dash else None,
        )

    def path_shapes(self, points, color, closed=False, width=2.5, dash=None, halo=HALO_COLOR,
                    fill=None):
        """Coloured polyline with a light halo so it reads on any background."""
        if len(points) < 2:
            return []
        els = [cv.Path.MoveTo(*points[0])] + [cv.Path.LineTo(*p) for p in points[1:]]
        if closed:
            els.append(cv.Path.Close())
        shapes = []
        if fill:
            shapes.append(cv.Path(list(els), paint=ft.Paint(color=fill, style=ft.PaintingStyle.FILL)))
        shapes.append(cv.Path(list(els), paint=self.paints(halo, width + 3)))
        shapes.append(cv.Path(list(els), paint=self.paints(color, width, dash)))
        return shapes

    def vertex_shapes(self, points, color, radius=4):
        z = self.zoom
        shapes = []
        for x, y in points:
            shapes.append(cv.Circle(x, y, (radius + 1.5) / z, self.paints(ft.Colors.WHITE, 0, fill=True)))
            shapes.append(cv.Circle(x, y, radius / z, self.paints(color, 0, fill=True)))
        return shapes

    def label_shapes(self, x, y, text, color):
        z = self.zoom
        size = 12 / z
        w = (len(text) * 7.2 + 14) / z
        h = 20 / z
        return [
            cv.Rect(x - w / 2, y - h / 2, w, h, border_radius=h / 2,
                    paint=ft.Paint(color=color, style=ft.PaintingStyle.FILL)),
            cv.Text(x, y, text, alignment=ft.Alignment.CENTER,
                    style=ft.TextStyle(size=size, color=ft.Colors.WHITE,
                                       weight=ft.FontWeight.BOLD)),
        ]

    def label_anchor(self, m):
        pts = m["points"]
        if m["kind"] == "area":
            return centroid(pts)
        # middle of the longest segment keeps the label on the line
        a, b = max(zip(pts, pts[1:]), key=lambda s: math.dist(*s))
        return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)

    def redraw(self):
        self.drawn_zoom = self.zoom
        if self.doc is None:
            self.canvas.shapes = []
            return
        shapes = []
        labels = []
        c = self.calibration
        if c and c["mode"] == "points":
            shapes += self.path_shapes(c["points"], CAL_COLOR, dash=[8, 5])
            shapes += self.vertex_shapes(c["points"], CAL_COLOR)
            (x1, y1), (x2, y2) = c["points"]
            labels += self.label_shapes(
                (x1 + x2) / 2, (y1 + y2) / 2, f"ref {c['ref']:g} {UNIT_ABBR[c['unit']]}", CAL_COLOR
            )
        for i, m in enumerate(self.measurements):
            color = KIND_COLORS[m["kind"]]
            closed = m["kind"] == "area"
            halo = SELECT_COLOR if i == self.selected else HALO_COLOR
            fill = ft.Colors.with_opacity(0.18, color) if closed else None
            shapes += self.path_shapes(m["points"], color, closed=closed, halo=halo,
                                       width=3.5 if i == self.selected else 2.5, fill=fill)
            shapes += self.vertex_shapes(m["points"], color, radius=3.5)
            text, _ = self.value_of(m)
            ax, ay = self.label_anchor(m)
            labels += self.label_shapes(ax, ay, f"{i + 1}  {text}", color)
        self.canvas.shapes = shapes + labels  # labels on top

    def draw_live(self, update=False):
        """In-progress shape, rubber band to the cursor and snap marker."""
        shapes = []
        live = ""
        if self.doc is not None and self.tool != "pan":
            color = CAL_COLOR if self.tool == "calibrate" else PENDING_COLOR
            pts = list(self.pending)
            preview = pts + [self.hover] if (self.hover and pts) else pts
            closed = self.tool == "area" and len(preview) >= 3
            if len(preview) >= 2:
                fill = ft.Colors.with_opacity(0.15, color) if closed else None
                shapes += self.path_shapes(preview, color, closed=closed, dash=[6, 4], fill=fill)
            shapes += self.vertex_shapes(pts, color)
            if self.hover:
                hx, hy = self.hover
                z = self.zoom
                if self.hover_snapped:
                    shapes.append(cv.Circle(hx, hy, 9 / z, self.paints(SELECT_COLOR, 2.5)))
                shapes.append(cv.Circle(hx, hy, 3 / z, self.paints(color, 0, fill=True)))
                if len(preview) >= 2:
                    if self.tool == "calibrate":
                        live = f"{math.dist(*preview[-2:]):,.1f} px"
                    else:
                        seg = self.pending_value(preview[-2:])
                        live = seg
                        if self.tool == "polyline" and len(preview) > 2:
                            live = f"{seg} · total {self.pending_value(preview)}"
                        elif closed:
                            live = f"{self.pending_value(preview, closed=True)}"
                    shapes += self.label_shapes(hx + 0, hy - 22 / z, live, color)
        self.live_canvas.shapes = shapes
        self.live_text.value = live
        if update:
            self.safe_update(self.live_canvas, self.live_text)

    @staticmethod
    def safe_update(*controls):
        """Update only these controls (cheap), skipping any not on screen."""
        for c in controls:
            try:
                c.update()
            except Exception:
                pass

    # ================================================================ export
    def export_rows(self):
        rows = [["#", "type", "value", "detail"]]
        for i, m in enumerate(self.measurements, start=1):
            text, _ = self.value_of(m)
            rows.append([i, KIND_LABELS[m["kind"]], text, self.detail_of(m)])
        return rows

    async def copy_measurements(self, e=None):
        lines = [f"{APP_NAME} · {self.file_name}"]
        summary = self.calibration_summary()
        if summary:
            lines.append(f"Scale: {summary}")
        for i, m in enumerate(self.measurements, start=1):
            text, _ = self.value_of(m)
            lines.append(f"{i}. {KIND_LABELS[m['kind']]}: {text}")
        lines.append(f"Total length: {self.total_length_text.value}")
        lines.append(f"Total area: {self.total_area_text.value}")
        await self.clipboard.set("\n".join(lines))
        self.notify("Measurements copied to clipboard.")

    async def save_csv(self, e=None):
        buf = io.StringIO()
        csv.writer(buf).writerows(self.export_rows())
        data = buf.getvalue().encode("utf-8-sig")
        base = os.path.splitext(self.file_name or "blueprint")[0]
        try:
            path = await self.file_picker.save_file(
                dialog_title="Save measurements",
                file_name=f"{base}-measurements.csv",
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=["csv"],
                src_bytes=data,
            )
        except Exception as ex:
            self.notify(f"Could not save: {ex}", error=True)
            return
        if path and not self.page.web:
            try:
                with open(path, "wb") as fh:
                    fh.write(data)
            except OSError as ex:
                self.notify(f"Could not save: {ex}", error=True)
                return
        if path or self.page.web:
            self.notify("Measurements saved.")


def main(page: ft.Page):
    page.title = APP_NAME
    if not page.web:
        page.window.width = 1500
        page.window.height = 920
        page.window.min_width = 420
        page.window.min_height = 500
    page.padding = 12
    page.theme = ft.Theme(color_scheme_seed=SEED_COLOR)
    page.dark_theme = ft.Theme(color_scheme_seed=SEED_COLOR)
    page.theme_mode = ft.ThemeMode.DARK
    app = BlueprintApp(page)
    app.update_theme_icon()
    page.update()
    return app


if __name__ == "__main__":
    ft.run(main)
