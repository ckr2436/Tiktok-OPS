from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image


def _separator_scores(rgb: np.ndarray, *, axis: int) -> np.ndarray:
    """Score bright, dark, and consistently colored separator lines.

    Generated boards do not always use pure-white gutters. Some models render
    black dividers or JPEG-softened gray rules. A separator is therefore a
    line whose pixels are overwhelmingly bright/dark, or whose color is nearly
    constant end to end. The high uniformity gate avoids treating ordinary
    photographic rows/columns as dividers.
    """
    values = np.asarray(rgb, dtype=np.int16)
    minimum = np.min(values, axis=2)
    maximum = np.max(values, axis=2)
    bright = (minimum >= 205).mean(axis=axis)
    dark = (maximum <= 52).mean(axis=axis)

    median = np.median(values, axis=axis, keepdims=True)
    distance = np.max(np.abs(values - median), axis=2)
    uniform = (distance <= 14).mean(axis=axis)
    line_color = np.mean(values, axis=axis)
    line_count = int(line_color.shape[0])
    bilateral_contrast = np.zeros(line_count, dtype=np.float64)
    radii = sorted({
        max(3, round(line_count * ratio))
        for ratio in (0.006, 0.012, 0.025, 0.05)
    })
    for radius in radii:
        if radius * 2 >= line_count:
            continue
        left = np.roll(line_color, radius, axis=0)
        right = np.roll(line_color, -radius, axis=0)
        left_delta = np.max(np.abs(line_color - left), axis=1)
        right_delta = np.max(np.abs(line_color - right), axis=1)
        contrast = np.minimum(left_delta, right_delta)
        contrast[:radius] = 0
        contrast[-radius:] = 0
        bilateral_contrast = np.maximum(bilateral_contrast, contrast)

    # A line is a gutter only when the content on both sides differs. This
    # rejects broad flat-color panels while retaining white, black, gray, and
    # JPEG-softened separator bands.
    contrast_gate = bilateral_contrast >= 16
    uniform_gate = np.where((uniform >= 0.965) & contrast_gate, uniform, 0.0)
    bright_dark_gate = np.where(contrast_gate, np.maximum(bright, dark), 0.0)
    return np.maximum(bright_dark_gate, uniform_gate)


def _separator_runs(
    scores: np.ndarray,
    *,
    threshold: float = 0.90,
    minimum_width: int = 2,
) -> list[tuple[int, int]]:
    mask = np.asarray(scores >= threshold, dtype=bool)
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, enabled in enumerate(mask.tolist() + [False]):
        if enabled and start is None:
            start = index
        elif not enabled and start is not None:
            if index - start >= minimum_width:
                runs.append((start, index))
            start = None
    return runs


def _internal_runs(runs: list[tuple[int, int]], length: int) -> list[tuple[int, int]]:
    margin = max(2, round(length * 0.025))
    return [
        (start, end)
        for start, end in runs
        if start > margin and end < length - margin
    ]


def _merge_nearby_runs(
    runs: list[tuple[int, int]],
    length: int,
) -> list[tuple[int, int]]:
    """Merge one physical gutter split by a thin antialiased/photo line."""
    if not runs:
        return []
    maximum_gap = max(2, round(length * 0.01))
    merged: list[tuple[int, int]] = [runs[0]]
    for start, end in runs[1:]:
        previous_start, previous_end = merged[-1]
        if start - previous_end <= maximum_gap:
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def expected_row_columns(count: int) -> list[int]:
    """Return the only accepted board layout for a reference count."""
    layouts = {
        1: [1],
        2: [2],
        # Three portrait panels fit cleanly in one landscape row. The former
        # 2+1 portrait canvas encouraged image models to either stretch the
        # last panel across the full width or surround it with large white
        # margins, both of which made safe splitting unnecessarily fragile.
        3: [3],
        4: [2, 2],
        5: [3, 2],
        6: [3, 3],
        7: [4, 3],
        8: [4, 4],
        9: [3, 3, 3],
        10: [5, 5],
        11: [4, 4, 3],
        12: [4, 4, 4],
    }
    if count not in layouts:
        raise ValueError(f"Unsupported storyboard panel count: {count}")
    return layouts[count]


def _candidate_row_layouts(count: int) -> list[list[int]]:
    primary = expected_row_columns(count)
    candidates = [primary]
    # Accept already-rendered legacy three-panel boards during upgrades. New
    # prompts use a single 3-panel row, but a valid 2+1 board remains safe to
    # split when every extracted cell is portrait.
    if count == 3 and primary != [2, 1]:
        candidates.append([2, 1])
    if count == 6 and primary != [2, 2, 2]:
        candidates.append([2, 2, 2])
    # Portrait image generators commonly render seven panels on a 3x3
    # canvas and leave the final two cells blank. The last real panel is
    # still a normal portrait cell with explicit gutters, so this geometry is
    # just as safe to split as the preferred 4+3 board.
    if count == 7 and primary != [3, 3, 1]:
        candidates.append([3, 3, 1])
    return candidates


def _select_guided_runs(
    scores: np.ndarray,
    *,
    length: int,
    divisions: int,
    threshold: float = 0.72,
) -> list[tuple[int, int]] | None:
    """Pick bright gutters near the expected equal-cell boundaries.

    The previous generic uniform-line detector also treated door frames,
    furniture edges, and other straight photographic details as gutters. The
    generation contract explicitly asks for white gutters, so use their known
    geometric positions first and retain the generic detector as fallback.
    """
    if divisions <= 1:
        return []
    runs = _merge_nearby_runs(
        _internal_runs(
            _separator_runs(scores, threshold=threshold),
            length,
        ),
        length,
    )
    if not runs:
        return None
    cell_span = length / divisions
    tolerance = max(12.0, cell_span * 0.28)
    selected: list[tuple[int, int]] = []
    used: set[int] = set()
    for boundary_index in range(1, divisions):
        target = cell_span * boundary_index
        ranked: list[tuple[float, float, int, tuple[int, int]]] = []
        for run_index, run in enumerate(runs):
            if run_index in used:
                continue
            start, end = run
            center = (start + end) / 2
            distance = abs(center - target)
            if distance > tolerance:
                continue
            strength = float(np.mean(scores[start:end])) if end > start else 0.0
            ranked.append((-strength, distance, run_index, run))
        if not ranked:
            return None
        _strength, _distance, run_index, run = min(ranked)
        used.add(run_index)
        selected.append(run)
    selected.sort()
    if any(selected[index][1] >= selected[index + 1][0] for index in range(len(selected) - 1)):
        return None
    return selected


def _trim_bright_outer_margins(
    rgb: np.ndarray,
    cell: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    """Remove white centering margins without cropping photographic content."""
    x, y, width, height = cell
    crop = rgb[y:y + height, x:x + width, :]
    if crop.size == 0:
        return cell
    content_fraction = (np.min(crop, axis=2) < 225).mean(axis=0)
    active = np.flatnonzero(content_fraction >= 0.04)
    if active.size == 0:
        return cell
    left = max(0, int(active[0]) - 1)
    right = min(width, int(active[-1]) + 2)
    trimmed_width = right - left
    if trimmed_width < 160:
        return cell
    # Ignore tiny antialiasing trims. Meaningful white margins are typically
    # generated around the centered final cell of a ragged board.
    if left < max(2, round(width * 0.01)) and width - right < max(2, round(width * 0.01)):
        return cell
    return (x + left, y, trimmed_width, height)


def _detect_guided_bright_gutters(
    rgb: np.ndarray,
    *,
    count: int,
) -> tuple[list[tuple[int, int, int, int]], dict[str, Any]] | None:
    height, width = rgb.shape[:2]
    bright = np.min(rgb, axis=2) >= 225
    for row_columns in _candidate_row_layouts(count):
        row_count = len(row_columns)
        horizontal = _select_guided_runs(
            bright.mean(axis=1),
            length=height,
            divisions=row_count,
        )
        if horizontal is None:
            continue
        row_intervals = _intervals(height, horizontal)
        if len(row_intervals) != row_count:
            continue
        cells: list[tuple[int, int, int, int]] = []
        vertical_by_row: list[list[tuple[int, int]]] = []
        failed = False
        for (y_start, y_end), columns_in_row in zip(row_intervals, row_columns):
            sample_start = min(y_end, y_start + 2)
            sample_end = max(sample_start + 1, y_end - 2)
            row_bright_scores = bright[sample_start:sample_end, :].mean(axis=0)
            vertical = _select_guided_runs(
                row_bright_scores,
                length=width,
                divisions=columns_in_row,
            )
            if vertical is None:
                failed = True
                break
            vertical_by_row.append(vertical)
            for x_start, x_end in _intervals(width, vertical):
                cell = (x_start, y_start, x_end - x_start, y_end - y_start)
                cells.append(_trim_bright_outer_margins(rgb, cell))
        if failed:
            continue
        if _cells_are_usable(cells, count, max_aspect=0.92):
            return cells, {
                "method": "guided_bright_gutters",
                "width": width,
                "height": height,
                "columns": max(row_columns),
                "rows": row_count,
                "row_columns": row_columns,
                "vertical_gutters_by_row": vertical_by_row,
                "horizontal_gutters": horizontal,
            }
    return None


def validate_expected_layout(layout: dict[str, Any], *, count: int) -> None:
    actual = [int(value) for value in list(layout.get("row_columns") or [])]
    accepted = _candidate_row_layouts(count)
    if actual not in accepted:
        raise ValueError(
            "Storyboard layout does not match the required panel geometry: "
            f"expected one of {accepted}, detected {actual or 'unknown'}"
        )


def _intervals(length: int, separators: list[tuple[int, int]]) -> list[tuple[int, int]]:
    starts = [0] + [end for _start, end in separators]
    ends = [start for start, _end in separators] + [length]
    return [(start, end) for start, end in zip(starts, ends) if end > start]


def _cells_are_usable(
    cells: list[tuple[int, int, int, int]],
    count: int,
    *,
    min_aspect: float = 0.24,
    max_aspect: float = 0.92,
) -> bool:
    return (
        len(cells) == count
        and all(width >= 160 and height >= 160 for _x, _y, width, height in cells)
        and all(
            min_aspect <= width / max(1, height) <= max_aspect
            for _x, _y, width, height in cells
        )
    )


def detect_preview_cells(
    source: str | Path,
    *,
    count: int,
) -> tuple[list[tuple[int, int, int, int]], dict[str, Any]]:
    """Detect regular or ragged storyboard grids using their separator lines.

    Image models often arrange five panels as 3+2 or seven panels as 4+3.
    Their vertical gutters do not span the whole canvas, so global column
    projection cannot see them. Detect horizontal rows first, then find the
    vertical gutters independently inside every row.
    """
    source_path = Path(source)
    with Image.open(source_path) as image:
        rgb = np.asarray(image.convert("RGB"))
    height, width = rgb.shape[:2]
    if count <= 0:
        raise ValueError("Storyboard panel count must be positive")

    guided = _detect_guided_bright_gutters(rgb, count=count)
    if guided is not None:
        return guided

    horizontal = _merge_nearby_runs(
        _internal_runs(
            _separator_runs(_separator_scores(rgb, axis=1), threshold=0.82),
            height,
        ),
        height,
    )
    row_intervals = _intervals(height, horizontal)

    ragged_cells: list[tuple[int, int, int, int]] = []
    row_columns: list[int] = []
    row_vertical_gutters: list[list[tuple[int, int]]] = []
    if len(row_intervals) > 1:
        for y_start, y_end in row_intervals:
            # Ignore the row's outer two pixels so the horizontal border does
            # not inflate otherwise unrelated bright columns.
            sample_start = min(y_end, y_start + 2)
            sample_end = max(sample_start + 1, y_end - 2)
            row_rgb = rgb[sample_start:sample_end, :, :]
            vertical = _merge_nearby_runs(
                _internal_runs(
                    _separator_runs(_separator_scores(row_rgb, axis=0), threshold=0.88),
                    width,
                ),
                width,
            )
            columns = _intervals(width, vertical)
            row_columns.append(len(columns))
            row_vertical_gutters.append(vertical)
            ragged_cells.extend(
                (x_start, y_start, x_end - x_start, y_end - y_start)
                for x_start, x_end in columns
            )
        if _cells_are_usable(ragged_cells, count, max_aspect=0.92):
            return ragged_cells, {
                "method": "detected_ragged_gutters",
                "width": width,
                "height": height,
                "columns": max(row_columns),
                "rows": len(row_intervals),
                "row_columns": row_columns,
                "vertical_gutters_by_row": row_vertical_gutters,
                "horizontal_gutters": horizontal,
            }

    # Rectangular boards still benefit from full-canvas gutter detection.
    vertical = _merge_nearby_runs(
        _internal_runs(
            _separator_runs(_separator_scores(rgb, axis=0), threshold=0.82),
            width,
        ),
        width,
    )
    detected_columns = len(vertical) + 1
    detected_rows = len(horizontal) + 1
    if detected_columns * detected_rows == count:
        x_intervals = _intervals(width, vertical)
        y_intervals = _intervals(height, horizontal)
        cells = [
            (x_start, y_start, x_end - x_start, y_end - y_start)
            for y_start, y_end in y_intervals
            for x_start, x_end in x_intervals
        ]
        if _cells_are_usable(cells, count, max_aspect=0.92):
            return cells, {
                "method": "detected_rectangular_gutters",
                "width": width,
                "height": height,
                "columns": detected_columns,
                "rows": detected_rows,
                "row_columns": [detected_columns] * detected_rows,
                "vertical_gutters": vertical,
                "horizontal_gutters": horizontal,
            }

    # Never guess a grid. A wrong guess can silently crop across two panels
    # and poison every downstream video reference. The visual stage must
    # regenerate a board with detectable separators instead.
    raise ValueError(
        f"Storyboard separators are not safely detectable: {width}x{height}, count={count}"
    )


__all__ = ["detect_preview_cells", "expected_row_columns", "validate_expected_layout"]
