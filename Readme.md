# BluePoint

Measure real-world lengths and areas on blueprints, floor plans and drawings.

## Features

- **Opens almost anything**: PNG, JPEG, TIFF (including multi-page scans), WebP, HEIC/AVIF,
  BMP, GIF, PSD, TGA, ICO, PDF (any page), SVG and XPS. Phone photos are shown the right way up.
- **No distortion**: every page is shown at its exact pixel size and aspect ratio.
- **Two ways to set the scale**:
  - *Known length*: click both ends of a dimension you know. Change the number later and
    every measurement updates.
  - *Drawing scale*: type the scale printed on the sheet (e.g. `1:100`). For PDFs the
    resolution is known exactly, so no clicking is needed.
- **Tools**: line, path (multi-segment), area (with perimeter), plus pan/zoom.
- **Precision aids**: live length while you move the mouse, snapping to existing points,
  and 45° angle lock.
- **Units**: mm, cm, m, km, in, ft, yd, mi, and architectural feet & inches (`12' 3 1/2"`).
- Undo/redo, per-page measurements, export to CSV or clipboard, light/dark theme, phone layout.

## Shortcuts

| Key | Action |
| --- | --- |
| `C` `L` `P` `A` `V` | Calibrate, Line, Path, Area, Pan |
| `Enter` / right-click | Finish path or area |
| `Backspace` | Remove last point |
| `Esc` | Cancel shape / deselect |
| `Delete` | Delete selected measurement |
| `S` / `O` | Toggle snapping / 45° lock |
| `+` `-` `0` `1` | Zoom in, zoom out, fit, actual pixels |
| `Ctrl+Z` / `Ctrl+Y` | Undo / redo |
| `Ctrl+O` | Open a file |

## Tests

```bash
uv run pytest
```

### uv

Run as a desktop app:

```bash
uv run flet run
```

Run as a web app:

```bash
uv run flet run --web
```

For more details on running the app, refer to the [Getting Started Guide](https://flet.dev/docs/).

## Build the app

### Android

```bash
flet build apk -v
```

For more details on building and signing `.apk` or `.aab`, refer to the [Android Packaging Guide](https://flet.dev/docs/publish/android/).

### iOS

```bash
flet build ipa -v
```

For more details on building and signing `.ipa`, refer to the [iOS Packaging Guide](https://flet.dev/docs/publish/ios/).

### macOS

```bash
flet build macos -v
```

For more details on building macOS package, refer to the [macOS Packaging Guide](https://flet.dev/docs/publish/macos/).

### Linux

```bash
flet build linux -v
```

For more details on building Linux package, refer to the [Linux Packaging Guide](https://flet.dev/docs/publish/linux/).

### Windows

```bash
flet build windows -v
```

For more details on building Windows package, refer to the [Windows Packaging Guide](https://flet.dev/docs/publish/windows/).

### Web

```bash
flet build web -v
```

For more details on building Web app, refer to the [Web Packaging Guide](https://flet.dev/docs/publish/web/).
