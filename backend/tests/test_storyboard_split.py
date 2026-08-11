from __future__ import annotations

from PIL import Image, ImageDraw
import pytest

from app.services.hermes_agent.storyboard_split import (
    detect_preview_cells,
    expected_row_columns,
    validate_expected_layout,
)


def test_detects_row_specific_three_plus_two_gutters(tmp_path):
    path = tmp_path / "ragged-board.png"
    image = Image.new("RGB", (1200, 1200), "white")
    draw = ImageDraw.Draw(image)
    colors = ["#301040", "#102040", "#402010", "#201030", "#103020"]
    # Top row: three panels. Bottom row: two centered portrait panels.
    for index, box in enumerate(
        (
            (0, 0, 394, 594),
            (402, 0, 794, 594),
            (802, 0, 1199, 594),
            (195, 602, 584, 1199),
            (593, 602, 982, 1199),
        )
    ):
        draw.rectangle(box, fill=colors[index])
    image.save(path)

    cells, layout = detect_preview_cells(path, count=5)

    assert layout["method"] == "guided_bright_gutters"
    assert layout["row_columns"] == [3, 2]
    assert len(cells) == 5
    assert cells[0][:2] == (0, 0)
    assert cells[3][0] >= 190
    assert cells[4][0] > 590


def test_three_panel_primary_layout_is_one_landscape_row(tmp_path):
    path = tmp_path / "three-panel-row.png"
    image = Image.new("RGB", (1500, 900), "white")
    draw = ImageDraw.Draw(image)
    for color, box in zip(
        ("#302040", "#18384f", "#4d2b18"),
        ((0, 0, 492, 899), (504, 0, 996, 899), (1008, 0, 1499, 899)),
    ):
        draw.rectangle(box, fill=color)
    image.save(path)

    cells, layout = detect_preview_cells(path, count=3)

    assert expected_row_columns(3) == [3]
    assert layout["method"] == "guided_bright_gutters"
    assert layout["row_columns"] == [3]
    assert len(cells) == 3
    validate_expected_layout(layout, count=3)


def test_accepts_legacy_two_plus_one_when_last_panel_is_portrait(tmp_path):
    path = tmp_path / "legacy-three-panel.png"
    image = Image.new("RGB", (940, 1672), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 460, 818), fill="#302040")
    draw.rectangle((472, 0, 939, 818), fill="#18384f")
    draw.rectangle((230, 832, 709, 1671), fill="#4d2b18")
    # Photo-like straight lines must not become false gutters.
    draw.rectangle((80, 0, 86, 818), fill="#090909")
    draw.rectangle((760, 0, 767, 818), fill="#080808")
    image.save(path)

    cells, layout = detect_preview_cells(path, count=3)

    assert layout["method"] == "guided_bright_gutters"
    assert layout["row_columns"] == [2, 1]
    assert len(cells) == 3
    assert all(width / height <= 0.92 for _x, _y, width, height in cells)
    validate_expected_layout(layout, count=3)


def test_rejects_legacy_two_plus_one_with_landscape_bottom_panel(tmp_path):
    path = tmp_path / "invalid-three-panel.png"
    image = Image.new("RGB", (940, 1672), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 460, 818), fill="#302040")
    draw.rectangle((472, 0, 939, 818), fill="#18384f")
    draw.rectangle((0, 832, 939, 1671), fill="#4d2b18")
    image.save(path)

    with pytest.raises(ValueError, match="not safely detectable"):
        detect_preview_cells(path, count=3)


def test_does_not_guess_five_equal_slivers_without_gutters(tmp_path):
    path = tmp_path / "no-gutters.png"
    Image.new("RGB", (1200, 1200), "#302040").save(path)

    with pytest.raises(ValueError, match="not safely detectable"):
        detect_preview_cells(path, count=5)


def test_detects_dark_four_plus_three_gutters(tmp_path):
    path = tmp_path / "dark-ragged-board.png"
    image = Image.new("RGB", (1600, 1600), "#080808")
    draw = ImageDraw.Draw(image)
    colors = ["#512048", "#173c62", "#5b341c", "#204c3e", "#39285a", "#604020", "#244f58"]
    boxes = (
        (0, 0, 390, 790),
        (400, 0, 790, 790),
        (800, 0, 1190, 790),
        (1200, 0, 1599, 790),
        (0, 800, 520, 1599),
        (530, 800, 1050, 1599),
        (1060, 800, 1599, 1599),
    )
    for index, box in enumerate(boxes):
        draw.rectangle(box, fill=colors[index])
    image.save(path, quality=92)

    cells, layout = detect_preview_cells(path, count=7)

    assert layout["method"] == "detected_ragged_gutters"
    assert layout["row_columns"] == [4, 3]
    assert len(cells) == 7


def test_accepts_clear_four_plus_three_grid_on_provider_portrait_canvas(tmp_path):
    path = tmp_path / "provider-portrait-seven-panel.png"
    image = Image.new("RGB", (941, 1672), "white")
    draw = ImageDraw.Draw(image)
    colors = ["#512048", "#173c62", "#5b341c", "#204c3e", "#39285a", "#604020", "#244f58"]
    boxes = (
        (0, 0, 231, 823),
        (239, 0, 466, 823),
        (474, 0, 700, 823),
        (708, 0, 940, 823),
        (0, 832, 296, 1671),
        (305, 832, 622, 1671),
        (631, 832, 940, 1671),
    )
    for color, box in zip(colors, boxes):
        draw.rectangle(box, fill=color)
    image.save(path)

    cells, layout = detect_preview_cells(path, count=7)

    assert layout["method"] == "guided_bright_gutters"
    assert layout["row_columns"] == [4, 3]
    assert len(cells) == 7
    assert min(width / height for _x, _y, width, height in cells) >= 0.24
    validate_expected_layout(layout, count=7)


def test_accepts_three_plus_three_plus_one_seven_panel_portrait_board(tmp_path):
    path = tmp_path / "seven-panel-three-row-board.png"
    image = Image.new("RGB", (940, 1672), "white")
    draw = ImageDraw.Draw(image)
    colors = ["#512048", "#173c62", "#5b341c", "#204c3e", "#39285a", "#604020", "#244f58"]
    boxes = (
        (0, 0, 306, 548),
        (316, 0, 622, 548),
        (632, 0, 939, 548),
        (0, 558, 306, 1106),
        (316, 558, 622, 1106),
        (632, 558, 939, 1106),
        (0, 1116, 306, 1671),
    )
    for color, box in zip(colors, boxes):
        draw.rectangle(box, fill=color)
    image.save(path)

    cells, layout = detect_preview_cells(path, count=7)

    assert layout["method"] == "guided_bright_gutters"
    assert layout["row_columns"] == [3, 3, 1]
    assert len(cells) == 7
    assert cells[-1][0] == 0
    assert cells[-1][2] <= 310
    validate_expected_layout(layout, count=7)


def test_detects_uniform_gray_rectangular_gutters(tmp_path):
    path = tmp_path / "gray-board.png"
    image = Image.new("RGB", (1220, 1820), "#777777")
    draw = ImageDraw.Draw(image)
    colors = ["#401530", "#153050", "#503015", "#183d2d", "#342050", "#504020"]
    boxes = (
        (0, 0, 395, 900),
        (410, 0, 805, 900),
        (820, 0, 1219, 900),
        (0, 920, 395, 1819),
        (410, 920, 805, 1819),
        (820, 920, 1219, 1819),
    )
    for index, box in enumerate(boxes):
        draw.rectangle(box, fill=colors[index])
    image.save(path)

    cells, layout = detect_preview_cells(path, count=6)

    assert layout["method"] == "detected_ragged_gutters"
    assert layout["row_columns"] == [3, 3]
    assert len(cells) == 6


def test_merges_fragmented_separator_and_splits_actual_two_by_three_board(tmp_path):
    path = tmp_path / "fragmented-two-by-three.png"
    image = Image.new("RGB", (940, 1672), "white")
    draw = ImageDraw.Draw(image)
    colors = ["#352040", "#18384f", "#4d2b18", "#17402f", "#30214f", "#4a3820"]
    boxes = (
        (0, 0, 460, 551),
        (467, 0, 939, 551),
        (0, 558, 460, 1104),
        (467, 558, 939, 1104),
        (0, 1152, 460, 1671),
        (467, 1152, 939, 1671),
    )
    for color, box in zip(colors, boxes):
        draw.rectangle(box, fill=color)
    # Simulate one physical gutter broken into adjacent antialiased runs.
    draw.rectangle((0, 1105, 939, 1111), fill="#fefefe")
    draw.rectangle((0, 1112, 939, 1151), fill="#fafafa")
    image.save(path)

    cells, layout = detect_preview_cells(path, count=6)

    assert layout["row_columns"] == [2, 2, 2]
    assert len(cells) == 6
    assert cells[0] == (0, 0, 461, 552)
    assert cells[-1][0] == 467
    assert cells[-1][1] == 1152
