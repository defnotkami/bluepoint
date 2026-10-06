import copy

import math
import struct

import flet as ft

import flet.canvas as cv


def image_size(data):
    """Return (width, height) for common blueprint image formats without Pillow."""
    if not data:
        raise ValueError("Empty image data")
    # PNG
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    # GIF
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return struct.unpack("<HH", data[6:10])
    # BMP
    if data[:2] == b"BM" and len(data) >= 26:
        return struct.unpack("<ii", data[18:26])[:2]
    # JPEG
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            while i < len(data) and data[i] == 0xFF:
                i += 1
            if i >= len(data):
                break
            marker = data[i]
            i += 1
            if marker in (0xD8, 0xD9):
                continue
            if i + 2 > len(data):
                break
            length = struct.unpack(">H", data[i:i+2])[0]
            if length < 2 or i + length > len(data):
                break
            if marker in range(0xC0, 0xC4) or marker in range(0xC5, 0xC8) or marker in range(0xC9, 0xCC) or marker in range(0xCD, 0xD0):
                if length >= 7:
                    h, w = struct.unpack(">HH", data[i+3:i+7])
                    return w, h
            i += length
        raise ValueError("Could not determine JPEG dimensions")
    raise ValueError("Unsupported image format")


# Conversion factors to meters

UNIT_TO_METERS = {

    "millimeters": 0.001,

    "centimeters": 0.01,

    "meters": 1.0,

    "kilometers": 1000.0,

    "inches": 0.0254,

    "feet": 0.3048,

    "yards": 0.9144,

}

# key -> (label, icon)

TOOLS = {

    "calibrate": ("Calibrate", ft.Icons.STRAIGHTEN),

    "line": ("Line", ft.Icons.HORIZONTAL_RULE),

    "polyline": ("Polyline", ft.Icons.TIMELINE),

    "pan": ("Pan / View", ft.Icons.PAN_TOOL_ALT),

}

SEED_COLOR = ft.Colors.INDIGO

# Colors drawn on top of the blueprint image (independent of app theme)

CAL_COLOR = ft.Colors.RED_ACCENT_400

MEASURE_COLOR = ft.Colors.GREEN_ACCENT_700

PENDING_COLOR = ft.Colors.AMBER_700

LABEL_COLOR = ft.Colors.DEEP_ORANGE_ACCENT_400



def convert(value, from_unit, to_unit):

    return value * UNIT_TO_METERS[from_unit] / UNIT_TO_METERS[to_unit]



def bind_select(ctrl, handler):

    """Dropdown change event is `on_select` in new Flet, `on_change` in older."""

    if hasattr(ctrl, "on_select"):

        ctrl.on_select = handler

    else:

        ctrl.on_change = handler



class BlueprintMeasurementApp:

    def __init__(self, page: ft.Page):

        self.page = page

        self.image_data = None

        self.img_w = 0

        self.img_h = 0

        # ---- undoable state ----

        self.tool = "calibrate"

        self.calibration_points = []

        self.pending = []  # in-progress line / polyline points

        self.measurements = []  # {"kind", "points", "px"}

        self.scale_factor = None  # pixels per calibration unit

        self.cal_unit = "meters"

        self.undo_stack = []

        self.redo_stack = []

        # ---- UI controls ----

        self.status_text = ft.Text("Load a blueprint to begin.", size=13)

        self.total_text = ft.Text(

            "Total: -",

            size=16,

            weight=ft.FontWeight.BOLD,

            color=ft.Colors.ON_PRIMARY_CONTAINER,

        )

        self.ref_field = ft.TextField(

            label="Reference distance", value="1.0", border_radius=10

        )

        self.cal_unit_dropdown = self.make_unit_dropdown("meters", "Reference unit")

        self.display_unit_dropdown = self.make_unit_dropdown("meters", "Display unit")

        bind_select(self.display_unit_dropdown, self.on_display_unit_change)

        self.measurement_list = ft.ListView(expand=True, spacing=4)

        self.undo_btn = ft.IconButton(

            icon=ft.Icons.UNDO, tooltip="Undo (Ctrl+Z)", on_click=self.undo

        )

        self.redo_btn = ft.IconButton(

            icon=ft.Icons.REDO, tooltip="Redo (Ctrl+Y)", on_click=self.redo

        )

        self.theme_btn = ft.IconButton(

            icon=ft.Icons.LIGHT_MODE,

            tooltip="Switch theme",

            on_click=self.toggle_theme,

        )

        self.tool_buttons = {}

        self.finish_btn = ft.Button(

            "Finish",

            icon=ft.Icons.CHECK,

            tooltip="Finish polyline (Enter)",

            on_click=self.finish_polyline,

        )

        self.cancel_btn = ft.Button(

            "Cancel",

            icon=ft.Icons.CLOSE,

            tooltip="Cancel current shape (Esc)",

            on_click=self.cancel_pending,

        )

        self.canvas = cv.Canvas(shapes=[])

        # Browser-safe file loading: the selected image is kept in memory.
        self.file_picker = ft.FilePicker(on_result=self.on_file_selected)
        self.page.services.append(self.file_picker)

        self.viewer = ft.InteractiveViewer(

            content=ft.Container(),

            min_scale=0.1,

            max_scale=10,

            constrained=False,

            boundary_margin=ft.Margin.all(200),

            expand=True,

        )

        # Empty-state shown until an image is loaded

        self.placeholder = ft.Container(

            alignment=ft.Alignment(0, 0),

            content=ft.Column(

                [

                    ft.Icon(

                        ft.Icons.ARCHITECTURE,

                        size=72,

                        color=ft.Colors.OUTLINE,

                    ),

                    ft.Text(

                        "No blueprint loaded",

                        size=20,

                        weight=ft.FontWeight.BOLD,

                    ),

                    ft.Text(

                        "Click “Load Blueprint” to open a plan image.",

                        color=ft.Colors.ON_SURFACE_VARIANT,

                    ),

                ],

                horizontal_alignment=ft.CrossAxisAlignment.CENTER,

                alignment=ft.MainAxisAlignment.CENTER,

                spacing=8,

            ),

        )

        self.stage = ft.Container(

            expand=True,

            content=self.placeholder,

            border_radius=16,

            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),

            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,

            clip_behavior=ft.ClipBehavior.HARD_EDGE,

        )

        self.page.on_keyboard_event = self.on_key

        self.build_ui()

        self.refresh()

    # ----------------------------------------------------------- Helpers

    def make_unit_dropdown(self, value, label):

        return ft.Dropdown(

            label=label,

            value=value,

            border_radius=10,

            options=[ft.DropdownOption(u) for u in UNIT_TO_METERS],

        )

    def card(self, icon, title, controls, expand=False):

        return ft.Container(

            expand=expand,

            padding=16,

            border_radius=16,

            bgcolor=ft.Colors.SURFACE_CONTAINER,

            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),

            content=ft.Column(

                [

                    ft.Row(

                        [

                            ft.Icon(icon, size=20, color=ft.Colors.PRIMARY),

                            ft.Text(title, size=16, weight=ft.FontWeight.BOLD),

                        ],

                        spacing=8,

                    ),

                    *controls,

                ],

                spacing=12,

                expand=expand,

            ),

        )

    # ---------------------------------------------------------------- UI

    def build_ui(self):

        header = ft.Container(

            padding=ft.Padding.symmetric(horizontal=16, vertical=8),

            border_radius=16,

            bgcolor=ft.Colors.SURFACE_CONTAINER,

            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),

            content=ft.Row(

                [



                    ft.Button(
                        "Load Blueprint",
                        icon=ft.Icons.IMAGE_OUTLINED,
                        action=ft.PickFiles(
                            self.file_picker,
                            allow_multiple=False,
                            file_type=ft.FilePickerFileType.CUSTOM,
                            allowed_extensions=["png", "jpg", "jpeg", "bmp", "gif", "tiff"],
                            with_data=True,
                        ),
                    ),

                    ft.VerticalDivider(width=12),

                    ft.Container(expand=True),

                    self.undo_btn,

                    self.redo_btn,

                    ft.IconButton(

                        icon=ft.Icons.RESTART_ALT,

                        tooltip="Reset calibration",

                        on_click=self.reset_calibration,

                    ),

                    ft.IconButton(

                        icon=ft.Icons.DELETE_SWEEP_OUTLINED,

                        tooltip="Clear all measurements",

                        on_click=self.clear_measurements,

                    ),

                    ft.VerticalDivider(width=12),

                    self.theme_btn,

                ],

                spacing=4,

                vertical_alignment=ft.CrossAxisAlignment.CENTER,

            ),

        )

        tool_controls = []

        for key, (label, icon) in TOOLS.items():

            btn = ft.Button(

                label, icon=icon, on_click=lambda e, k=key: self.set_tool(k)

            )

            self.tool_buttons[key] = btn

            tool_controls.append(btn)

        tool_controls += [

            ft.VerticalDivider(width=12),

            self.finish_btn,

            self.cancel_btn,

        ]

        tool_row = ft.Row(

            tool_controls,

            wrap=True,

            spacing=8,

            vertical_alignment=ft.CrossAxisAlignment.CENTER,

        )

        status_bar = ft.Container(

            padding=ft.Padding.symmetric(horizontal=14, vertical=8),

            border_radius=12,

            bgcolor=ft.Colors.SURFACE_CONTAINER,

            content=ft.Row(

                [

                    ft.Icon(

                        ft.Icons.INFO_OUTLINE,

                        size=18,

                        color=ft.Colors.PRIMARY,

                    ),

                    self.status_text,

                ],

                spacing=8,

            ),

        )

        calibration_card = self.card(

            ft.Icons.STRAIGHTEN,

            "Calibration",

            [

                ft.Text(

                    "Enter a known real distance, pick the Calibrate tool, "

                    "then click both ends of that length.",

                    size=12,

                    color=ft.Colors.ON_SURFACE_VARIANT,

                ),

                self.ref_field,

                self.cal_unit_dropdown,

            ],

        )

        units_card = self.card(

            ft.Icons.SWAP_HORIZ,

            "Display units",

            [self.display_unit_dropdown],

        )

        total_chip = ft.Container(

            padding=ft.Padding.symmetric(horizontal=14, vertical=10),

            border_radius=12,

            bgcolor=ft.Colors.PRIMARY_CONTAINER,

            content=ft.Row(

                [

                    ft.Icon(

                        ft.Icons.FUNCTIONS,

                        size=20,

                        color=ft.Colors.ON_PRIMARY_CONTAINER,

                    ),

                    self.total_text,

                ],

                spacing=8,

            ),

        )

        measurements_card = self.card(

            ft.Icons.RULE,

            "Measurements",

            [self.measurement_list, total_chip],

            expand=True,

        )

        right_panel = ft.Container(

            width=330,

            content=ft.Column(

                [calibration_card, units_card, measurements_card],

                expand=True,

                spacing=12,

            ),

        )

        self.page.add(

            ft.Column(

                [

                    header,

                    tool_row,

                    ft.Row(

                        [self.stage, right_panel],

                        expand=True,

                        spacing=12,

                        vertical_alignment=ft.CrossAxisAlignment.STRETCH,

                    ),

                    status_bar,

                ],

                expand=True,

                spacing=12,

            )

        )

    # -------------------------------------------------------------- Theme

    def toggle_theme(self, e):

        if self.page.theme_mode == ft.ThemeMode.DARK:

            self.page.theme_mode = ft.ThemeMode.LIGHT

        else:

            self.page.theme_mode = ft.ThemeMode.DARK

        self.update_theme_icon()

        self.page.update()

    def update_theme_icon(self):

        dark = self.page.theme_mode == ft.ThemeMode.DARK

        # Show the icon of the mode you will switch TO

        self.theme_btn.icon = ft.Icons.LIGHT_MODE if dark else ft.Icons.DARK_MODE

        self.theme_btn.tooltip = "Switch to light mode" if dark else "Switch to dark mode"

    # ------------------------------------------------------- Image loading
    def on_file_selected(self, e: ft.FilePickerResultEvent):
        """Load the selected blueprint from bytes; works in Flet Web and desktop."""
        if not e.files:
            return

        selected = e.files[0]
        if not selected.bytes:
            self.status_text.value = "Could not read the selected image."
            self.page.update()
            return

        try:
            self.img_w, self.img_h = image_size(selected.bytes)
        except Exception as ex:
            self.status_text.value = f"Could not open image: {ex}"
            self.page.update()
            return

        self.image_data = selected.bytes

        # Canvas overlay is the same size as the source image, so measurement
        # coordinates remain in image pixels even when the viewer is zoomed.
        self.canvas = cv.Canvas(
            shapes=[],
            width=self.img_w,
            height=self.img_h,
        )

        self.viewer.content = ft.GestureDetector(
            on_tap_down=self.on_image_click,
            content=ft.Stack(
                [
                    ft.Image(
                        src=self.image_data,
                        width=self.img_w,
                        height=self.img_h,
                        fit=ft.BoxFit.FILL,
                    ),
                    self.canvas,
                ],
                width=self.img_w,
                height=self.img_h,
            ),
        )

        self.stage.content = self.viewer
        self.page.update()

        # Fresh project: wipe state and history.
        self.tool = "calibrate"
        self.calibration_points = []
        self.pending = []
        self.measurements = []
        self.scale_factor = None
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.status_text.value = self.tool_hint()
        self.refresh()

    # ------------------------------------------------------------ History

    def snapshot(self):

        return copy.deepcopy(

            {

                "tool": self.tool,

                "calibration_points": self.calibration_points,

                "pending": self.pending,

                "measurements": self.measurements,

                "scale_factor": self.scale_factor,

                "cal_unit": self.cal_unit,

            }

        )

    def restore(self, snap):

        self.tool = snap["tool"]

        self.calibration_points = snap["calibration_points"]

        self.pending = snap["pending"]

        self.measurements = snap["measurements"]

        self.scale_factor = snap["scale_factor"]

        self.cal_unit = snap["cal_unit"]

    def push_history(self):

        """Call BEFORE changing any undoable state."""

        self.undo_stack.append(self.snapshot())

        if len(self.undo_stack) > 200:

            self.undo_stack.pop(0)

        self.redo_stack.clear()

    def undo(self, e=None):

        if not self.undo_stack:

            return

        self.redo_stack.append(self.snapshot())

        self.restore(self.undo_stack.pop())

        self.status_text.value = "Undo."

        self.refresh()

    def redo(self, e=None):

        if not self.redo_stack:

            return

        self.undo_stack.append(self.snapshot())

        self.restore(self.redo_stack.pop())

        self.status_text.value = "Redo."

        self.refresh()

    # ------------------------------------------------------------ Keyboard

    def on_key(self, e: ft.KeyboardEvent):

        key = (e.key or "").upper()

        if e.ctrl or e.meta:

            if key == "Z":

                if e.shift:

                    self.redo()

                else:

                    self.undo()

            elif key == "Y":

                self.redo()

        elif key == "ENTER":

            self.finish_polyline()

        elif key == "ESCAPE":

            self.cancel_pending()

    # -------------------------------------------------------------- Tools

    def set_tool(self, tool):

        if tool == self.tool:

            return

        self.pending = []  # discard unfinished shape, not undoable

        self.tool = tool

        self.status_text.value = self.tool_hint()

        self.refresh()

    def tool_hint(self):

        if self.image_data is None:

            return "Load a blueprint to begin."

        if self.tool == "calibrate":

            return "Calibrate: click the two ends of a known distance."

        if self.scale_factor is None and self.tool != "pan":

            return "Calibrate first (choose the Calibrate tool)."

        if self.tool == "line":

            return "Line: click two points to measure."

        if self.tool == "polyline":

            return "Polyline: click each point, then press Enter / Finish."

        return "Pan / View: drag to pan, scroll or pinch to zoom."

    def finish_polyline(self, e=None):

        if self.tool != "polyline" or len(self.pending) < 2:

            return

        self.push_history()

        pts = self.pending

        px = sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))

        self.measurements.append({"kind": "polyline", "points": pts, "px": px})

        self.pending = []

        self.status_text.value = "Polyline added. Click to start another."

        self.refresh()

    def cancel_pending(self, e=None):

        if not self.pending:

            return

        self.push_history()

        self.pending = []

        self.status_text.value = "Current shape cancelled."

        self.refresh()

    # ------------------------------------------------------------- Clicks

    def on_image_click(self, e):

        if self.tool == "pan":

            return

        pos = (e.local_position.x, e.local_position.y)

        if self.tool == "calibrate":

            self.handle_calibration_click(pos)

            return

        if self.scale_factor is None:

            self.status_text.value = "Calibrate first (choose the Calibrate tool)."

            self.page.update()

            return

        self.push_history()

        self.pending.append(pos)

        if self.tool == "line":

            if len(self.pending) == 2:

                p1, p2 = self.pending

                self.measurements.append(

                    {"kind": "line", "points": [p1, p2], "px": math.dist(p1, p2)}

                )

                self.pending = []

                self.status_text.value = "Line added. Click two points to measure."

            else:

                self.status_text.value = "Click the second point."

        else:  # polyline

            self.status_text.value = (

                f"{len(self.pending)} points. Keep clicking, "

                "then press Enter / Finish."

            )

        self.refresh()

    def handle_calibration_click(self, pos):

        try:

            ref = float(self.ref_field.value)

            if ref <= 0:

                raise ValueError

        except (TypeError, ValueError):

            self.status_text.value = "Enter a valid reference distance (> 0) first."

            self.page.update()

            return

        # A third click starts a new calibration

        starting_new = len(self.calibration_points) >= 2

        points = [] if starting_new else list(self.calibration_points)

        if len(points) == 1 and math.dist(points[0], pos) == 0:

            self.status_text.value = "Points are identical; click a different spot."

            self.page.update()

            return

        self.push_history()

        points.append(pos)

        self.calibration_points = points

        if len(points) == 2:

            self.cal_unit = self.cal_unit_dropdown.value or "meters"

            self.scale_factor = math.dist(points[0], points[1]) / ref

            self.tool = "line"

            self.status_text.value = (

                f"Calibrated: {self.scale_factor:.2f} px per {self.cal_unit}. "

                "Line tool selected, click two points to measure."

            )

        else:

            self.status_text.value = "Click the second calibration point."

        self.refresh()

    # ------------------------------------------------------------ Actions

    def reset_calibration(self, e):

        self.push_history()

        self.tool = "calibrate"

        self.scale_factor = None

        self.calibration_points = []

        self.pending = []

        self.measurements = []

        self.status_text.value = self.tool_hint()

        self.refresh()

    def clear_measurements(self, e):

        if not self.measurements and not self.pending:

            return

        self.push_history()

        self.measurements = []

        self.pending = []

        self.status_text.value = "Measurements cleared (Ctrl+Z to undo)."

        self.refresh()

    def delete_measurement(self, index):

        if 0 <= index < len(self.measurements):

            self.push_history()

            self.measurements.pop(index)

            self.status_text.value = f"Measurement #{index + 1} deleted."

            self.refresh()

    def on_display_unit_change(self, e):

        self.refresh()

    # --------------------------------------------------------- Rendering

    @property

    def display_unit(self):

        return self.display_unit_dropdown.value or "meters"

    def length_of(self, m):

        """Real-world length of a measurement in the display unit."""

        if not self.scale_factor:

            return 0.0

        in_cal_unit = m["px"] / self.scale_factor

        return convert(in_cal_unit, self.cal_unit, self.display_unit)

    def refresh(self):

        self.refresh_list()

        self.refresh_toolbar()

        self.redraw()

    def refresh_list(self):

        unit = self.display_unit

        self.measurement_list.controls.clear()

        total = 0.0

        for i, m in enumerate(self.measurements, start=1):

            length = self.length_of(m)

            total += length

            if m["kind"] == "polyline":

                sub = f"Polyline, {len(m['points'])} points"

                icon = ft.Icons.TIMELINE

            else:

                sub = "Line"

                icon = ft.Icons.HORIZONTAL_RULE

            badge = ft.Container(

                width=30,

                height=30,

                border_radius=15,

                alignment=ft.Alignment(0, 0),

                bgcolor=ft.Colors.PRIMARY_CONTAINER,

                content=ft.Text(

                    str(i),

                    size=12,

                    weight=ft.FontWeight.BOLD,

                    color=ft.Colors.ON_PRIMARY_CONTAINER,

                ),

            )

            self.measurement_list.controls.append(

                ft.ListTile(

                    dense=True,

                    leading=badge,

                    title=ft.Text(

                        f"{length:.2f} {unit}", weight=ft.FontWeight.W_600

                    ),

                    subtitle=ft.Row(

                        [ft.Icon(icon, size=14), ft.Text(sub, size=12)],

                        spacing=4,

                    ),

                    trailing=ft.IconButton(

                        icon=ft.Icons.DELETE_OUTLINE,

                        icon_size=20,

                        tooltip="Delete",

                        on_click=lambda e, idx=i - 1: self.delete_measurement(idx),

                    ),

                )

            )

        if not self.measurements:

            self.measurement_list.controls.append(

                ft.Container(

                    padding=16,

                    alignment=ft.Alignment(0, 0),

                    content=ft.Text(

                        "No measurements yet",

                        color=ft.Colors.ON_SURFACE_VARIANT,

                        italic=True,

                    ),

                )

            )

            self.total_text.value = "Total: -"

        else:

            self.total_text.value = (

                f"Total ({len(self.measurements)}): {total:.2f} {unit}"

            )

    def refresh_toolbar(self):

        for key, btn in self.tool_buttons.items():

            selected = key == self.tool

            btn.style = ft.ButtonStyle(

                bgcolor=(

                    ft.Colors.PRIMARY

                    if selected

                    else ft.Colors.SURFACE_CONTAINER_HIGHEST

                ),

                color=ft.Colors.ON_PRIMARY if selected else ft.Colors.ON_SURFACE,

                shape=ft.RoundedRectangleBorder(radius=10),

            )

        self.undo_btn.disabled = not self.undo_stack

        self.redo_btn.disabled = not self.redo_stack

        self.finish_btn.disabled = not (

            self.tool == "polyline" and len(self.pending) >= 2

        )

        self.cancel_btn.disabled = not self.pending

    def redraw(self):

        shapes = []

        red = ft.Paint(color=CAL_COLOR, stroke_width=2)

        green = ft.Paint(color=MEASURE_COLOR, stroke_width=2)

        amber = ft.Paint(color=PENDING_COLOR, stroke_width=2)

        label_style = ft.TextStyle(

            size=16, color=LABEL_COLOR, weight=ft.FontWeight.BOLD

        )

        for p in self.calibration_points:

            shapes.append(cv.Circle(p[0], p[1], 5, red))

        if len(self.calibration_points) == 2:

            (x1, y1), (x2, y2) = self.calibration_points

            shapes.append(cv.Line(x1, y1, x2, y2, red))

        for i, m in enumerate(self.measurements, start=1):

            pts = m["points"]

            for a, b in zip(pts, pts[1:]):

                shapes.append(cv.Line(a[0], a[1], b[0], b[1], green))

            for p in pts:

                shapes.append(cv.Circle(p[0], p[1], 4, green))

            shapes.append(

                cv.Text(

                    x=pts[0][0] + 6,

                    y=pts[0][1] + 6,

                    value=f"#{i}",

                    style=label_style,

                )

            )

        for a, b in zip(self.pending, self.pending[1:]):

            shapes.append(cv.Line(a[0], a[1], b[0], b[1], amber))

        for p in self.pending:

            shapes.append(cv.Circle(p[0], p[1], 4, amber))

        self.canvas.shapes = shapes

        # page.update() refreshes everything; never call canvas.update()

        # directly, since the canvas may not be mounted yet.

        self.page.update()



def main(page: ft.Page):

    page.title = "Bluey"

    if not page.web:
        page.window.width = 1500
        page.window.height = 900

    page.padding = 16

    page.theme = ft.Theme(color_scheme_seed=SEED_COLOR)

    page.dark_theme = ft.Theme(color_scheme_seed=SEED_COLOR)

    page.theme_mode = ft.ThemeMode.DARK

    app = BlueprintMeasurementApp(page)

    app.update_theme_icon()

    page.update()



if __name__ == "__main__":

    ft.run(main)