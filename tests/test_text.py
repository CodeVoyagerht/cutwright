"""``cutwright.text`` 的单元测试：折行规则、版式定位、PNG 产物。

这里断言的都是**契约**，不是实现细节：折行按字数、``|`` 不显示、产物与 canvas
同尺寸、RGBA、关键词真的换了颜色、``invisible`` 真的全透明。换实现时这些断言
仍然应该成立，否则后端的 ``overlay=0:0`` 就不再安全。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
import uuid
from typing import List, Optional, Tuple

from PIL import Image

from cutwright.schema import TextStyle
from cutwright.text import FontNotFound, find_font, render_text_png, wrap_text

# 测试画布用 16:9 的小尺寸：渲染快，且非 9:16 能暴露「按宽度居中」写死的问题。
CANVAS: Tuple[int, int] = (640, 360)

RGBA = Tuple[int, int, int, int]


def _scratch_dir(prefix: str) -> str:
    """建一个可写、可删的临时目录。

    为什么不直接用 ``tempfile.mkdtemp``：它把目录建成 0o700，在「写权限按 ACL
    授予、且不覆盖新建私有目录」的受限沙箱里会被拒绝写入，甚至删不掉。用
    ``os.makedirs`` 建出来的目录继承父目录的 ACL，两边都能用。系统临时目录
    不可写时（容器 / 只读挂载）再退回仓库目录，测试不应该因为环境而报错。
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for base in (tempfile.gettempdir(), repo_root):
        path = os.path.join(base, prefix + uuid.uuid4().hex[:8])
        try:
            os.makedirs(path, exist_ok=False)
            probe = os.path.join(path, ".writable")
            with open(probe, "w", encoding="utf-8") as handle:
                handle.write("x")
            os.remove(probe)
            return path
        except OSError:
            continue
    raise unittest.SkipTest("没有可写的临时目录")


def _colors(image: Image.Image) -> List[Tuple[int, RGBA]]:
    """返回 (像素数, RGBA) 列表。

    用 ``getcolors`` 而不是逐像素遍历：判「有没有黄色」时只需要颜色直方图，
    一张 640x360 的图逐像素跑十几遍没必要。
    """
    found = image.convert("RGBA").getcolors(maxcolors=1 << 22)
    return list(found or [])


def _has_color(image: Image.Image, predicate, min_alpha: int = 200) -> bool:
    """是否存在足够不透明、且满足 ``predicate`` 的颜色。"""
    for count, color in _colors(image):
        if count <= 0 or color[3] < min_alpha:
            continue
        if predicate(color):
            return True
    return False


def _alpha_bbox(image: Image.Image) -> Optional[Tuple[int, int, int, int]]:
    """不透明区域的包围盒；全透明图返回 None。"""
    return image.convert("RGBA").getchannel("A").getbbox()


def _opaque_count(image: Image.Image, min_alpha: int = 128) -> int:
    total = 0
    for count, color in _colors(image):
        if color[3] >= min_alpha:
            total += count
    return total


def _is_white(color: RGBA) -> bool:
    return color[0] >= 235 and color[1] >= 235 and color[2] >= 235


def _is_keyword_yellow(color: RGBA) -> bool:
    """``#FFD54A``（默认关键词色）的判定：亮黄、蓝通道明显低。"""
    return color[0] >= 210 and 160 <= color[1] <= 240 and color[2] <= 170


class TextTestCase(unittest.TestCase):
    """所有渲染测试共用的临时目录与渲染帮手。"""

    def setUp(self) -> None:
        self.tmp = _scratch_dir("cw-text-")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def render(self, text: str, style: Optional[TextStyle] = None,
               canvas: Tuple[int, int] = CANVAS, scale: float = 1.0,
               name: str = "text.png") -> str:
        style = style if style is not None else TextStyle()
        return render_text_png(text, style, canvas, os.path.join(self.tmp, name), scale=scale)

    def load(self, path: str) -> Image.Image:
        image = Image.open(path)
        image.load()
        return image


class FontDiscoveryTest(unittest.TestCase):
    """字体发现：找不到字体必须显式失败，而不是画出一张空图。"""

    def test_default_font_exists_on_disk(self) -> None:
        path = find_font()
        self.assertTrue(os.path.isabs(path), f"字体路径应为绝对路径: {path}")
        self.assertTrue(os.path.isfile(path), f"字体文件不存在: {path}")

    def test_explicit_font_is_used(self) -> None:
        explicit = find_font()
        self.assertEqual(find_font(explicit), os.path.abspath(explicit))

    def test_unknown_prefer_falls_back(self) -> None:
        # 分镜表里写错字体名不该让整条片子渲不出来，回退到候选表是刻意的。
        path = find_font("Zzz-Not-Installed-Font.ttf")
        self.assertTrue(os.path.isfile(path))

    def test_font_not_found_is_a_runtime_error(self) -> None:
        self.assertTrue(issubclass(FontNotFound, RuntimeError))


class WrapTextTest(unittest.TestCase):
    """折行：按字数、中文按字切、英文按词回退、``|`` 不占格。"""

    def test_no_wrap_when_max_is_zero(self) -> None:
        self.assertEqual(wrap_text("这是一整句话不折行", 0), ["这是一整句话不折行"])

    def test_negative_max_also_means_no_wrap(self) -> None:
        self.assertEqual(wrap_text("abc def", -3), ["abc def"])

    def test_chinese_splits_per_character(self) -> None:
        self.assertEqual(wrap_text("一二三四五六", 2), ["一二", "三四", "五六"])

    def test_chinese_uneven_last_line(self) -> None:
        self.assertEqual(wrap_text("一二三四五", 2), ["一二", "三四", "五"])

    def test_english_word_boundary_backoff(self) -> None:
        # 在第 8 格处 "world" 会被劈开，必须整体挪到下一行。
        self.assertEqual(wrap_text("hello world", 8), ["hello", "world"])

    def test_english_exact_fit_is_not_split(self) -> None:
        self.assertEqual(wrap_text("hello world", 5), ["hello", "world"])

    def test_three_words(self) -> None:
        self.assertEqual(wrap_text("abc def ghi", 7), ["abc def", "ghi"])

    def test_overlong_word_is_hard_cut(self) -> None:
        # 单个超长单词无处可退，只能硬切，否则会退化成每行一个字母。
        self.assertEqual(wrap_text("abcdefghij", 4), ["abcd", "efgh", "ij"])

    def test_separator_is_not_rendered(self) -> None:
        self.assertEqual(wrap_text("你好|世界", 10), ["你好世界"])

    def test_separator_takes_no_width(self) -> None:
        # "abcd" 4 格正合适；若分隔符占格就会被挤成两行。
        self.assertEqual(wrap_text("ab|cd", 4), ["abcd"])

    def test_every_separator_is_invisible(self) -> None:
        # 全角/半角分隔符都只是「换色标记」，一个都不该出现在折行结果里。
        # schema.py 的 TextSegment 也是这么切的：偶数段正文、奇数段关键词，
        # 所以 "a|b|c" 里两根竖线都不显示，剩下的就是 "abc"。
        self.assertEqual(wrap_text("a|b|c", 10), ["abc"])
        self.assertEqual(wrap_text("自动裁剪｜变速｜补增益", 20), ["自动裁剪变速补增益"])

    def test_empty_text_returns_no_lines(self) -> None:
        self.assertEqual(wrap_text("", 10), [])
        self.assertEqual(wrap_text("   ", 10), [])

    def test_hard_newlines_are_kept(self) -> None:
        self.assertEqual(wrap_text("a\nb", 10), ["a", "b"])

    def test_wrapping_preserves_every_character(self) -> None:
        # 折行只搬移不丢字：拼回去必须等于去掉分隔符（与行尾空白）的原串。
        for text, limit in (("hello world", 8), ("一二三四五", 2), ("a|bc def", 3)):
            lines = wrap_text(text, limit)
            joined = "".join(lines)
            expected = text.replace("|", "").replace(" ", "")
            self.assertEqual(joined.replace(" ", ""), expected, f"{text!r} @ {limit}")
            self.assertTrue(all(len(line) <= limit for line in lines))


class RenderTextPngTest(TextTestCase):
    """渲染产物：尺寸、模式、颜色、版式。"""

    def test_output_size_equals_canvas(self) -> None:
        image = self.load(self.render("你好", TextStyle(font_height_px=60)))
        self.assertEqual(image.size, CANVAS)
        self.assertEqual(image.mode, "RGBA")

    def test_output_is_a_readable_png(self) -> None:
        path = self.render("有效 PNG", TextStyle(font_height_px=60))
        self.assertEqual(os.path.splitext(path)[1].lower(), ".png")
        with Image.open(path) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, CANVAS)

    def test_returns_absolute_path(self) -> None:
        path = self.render("路径", TextStyle(font_height_px=40))
        self.assertTrue(os.path.isabs(path))
        self.assertTrue(os.path.isfile(path))

    def test_nested_output_directory_is_created(self) -> None:
        out = os.path.join(self.tmp, "deep", "deeper", "a.png")
        path = render_text_png("嵌套", TextStyle(font_height_px=40), CANVAS, out)
        self.assertTrue(os.path.isfile(path))

    def test_empty_text_produces_transparent_png(self) -> None:
        image = self.load(self.render("", TextStyle(font_height_px=60)))
        self.assertEqual(image.size, CANVAS)
        self.assertIsNone(_alpha_bbox(image), "空文字应输出全透明图")

    def test_whitespace_only_text_produces_transparent_png(self) -> None:
        image = self.load(self.render("   ", TextStyle(font_height_px=60)))
        self.assertIsNone(_alpha_bbox(image))

    def test_plain_text_has_white_pixels(self) -> None:
        image = self.load(self.render("普通字幕", TextStyle(font_height_px=80)))
        self.assertIsNotNone(_alpha_bbox(image))
        self.assertTrue(_has_color(image, _is_white), "常规色文字应有白像素")
        self.assertFalse(_has_color(image, _is_keyword_yellow), "无分隔符时不该出现关键词色")

    def test_keyword_text_has_keyword_color_pixels(self) -> None:
        image = self.load(self.render("前半句|关键词", TextStyle(font_height_px=80)))
        self.assertTrue(_has_color(image, _is_white), "分隔符之前应是常规色")
        self.assertTrue(_has_color(image, _is_keyword_yellow), "分隔符之后应是关键词色")

    def test_keyword_color_follows_style(self) -> None:
        style = TextStyle(font_height_px=80, keyword_color="#00FF00")
        image = self.load(self.render("前|后", style))
        green = lambda color: color[1] >= 200 and color[0] <= 120 and color[2] <= 120
        self.assertTrue(_has_color(image, green), "keyword_color 应生效")

    def test_second_separator_returns_to_body_color(self) -> None:
        # schema.TextSegment 把文案切成「正文 + 关键词段列表」，即偶数段正文、
        # 奇数段关键词，所以两根竖线之间才高亮：甲(白) | 乙(黄) | 丙(白)。
        image = self.load(self.render("甲|乙|丙", TextStyle(font_height_px=80)))
        self.assertTrue(_has_color(image, _is_keyword_yellow))
        bbox = _alpha_bbox(image)
        self.assertIsNotNone(bbox)
        left, top, right, bottom = bbox
        tail_width = max(1, (right - left) // 5)
        tail = image.crop((right - tail_width, top, right, bottom))
        self.assertTrue(_has_color(tail, _is_white),
                        "第二个分隔符之后的文字应回到正文色，而不是整段刷黄")

    def test_stroke_produces_dark_pixels(self) -> None:
        style = TextStyle(font_height_px=80, stroke_width_px=8, stroke_color="#000000")
        image = self.load(self.render("描边", style))
        dark = lambda color: color[0] <= 40 and color[1] <= 40 and color[2] <= 40
        self.assertTrue(_has_color(image, dark), "描边应产生黑色像素")

    def test_invisible_still_writes_transparent_png(self) -> None:
        # 后端可能要「有素材但看不见」来对齐旁白时长，缺文件比缺像素更麻烦。
        path = self.render("看不见的字", TextStyle(font_height_px=80, invisible=True))
        self.assertTrue(os.path.isfile(path))
        image = self.load(path)
        self.assertEqual(image.size, CANVAS)
        self.assertEqual(image.mode, "RGBA")
        self.assertIsNone(_alpha_bbox(image), "invisible 应输出全透明图")

    def test_long_text_wraps_into_taller_block(self) -> None:
        style = TextStyle(font_height_px=40, max_chars_per_line=6)
        image = self.load(self.render("这是一段很长的字幕用来测试折行", style))
        box = _alpha_bbox(image)
        self.assertIsNotNone(box)
        _, top, _, bottom = box  # type: ignore[misc]
        # 10 个字 / 6 格 → 至少两行，高度应明显超过单行字高。
        self.assertGreater(bottom - top, 40 * 1.4)

    def test_rotation_90_turns_block_vertical(self) -> None:
        style = TextStyle(font_height_px=60, rotation_deg=90)
        rotated = self.load(self.render("ABCDE", style))
        box = _alpha_bbox(rotated)
        self.assertIsNotNone(box)
        left, top, right, bottom = box  # type: ignore[misc]
        self.assertGreater(bottom - top, right - left, "旋转 90 度后应变成竖排块")

    def test_rotation_does_not_lose_pixels(self) -> None:
        # 旋转后画布要够大：像素量应与未旋转时接近（重采样会有少量损失）。
        text = "旋转测试"
        flat = self.load(self.render(text, TextStyle(font_height_px=60), name="flat.png"))
        turned = self.load(self.render(text, TextStyle(font_height_px=60, rotation_deg=90),
                                       name="turned.png"))
        flat_count = _opaque_count(flat)
        turned_count = _opaque_count(turned)
        self.assertGreater(flat_count, 0)
        self.assertGreater(turned_count, flat_count * 0.8, "旋转后不应被裁掉大半")

    def test_bottom_margin_puts_text_near_bottom(self) -> None:
        style = TextStyle(font_height_px=60, bottom_margin_px=40)
        image = self.load(self.render("贴底", style))
        box = _alpha_bbox(image)
        self.assertIsNotNone(box)
        bottom = box[3]  # type: ignore[index]
        # 允许描边/内边距带来的偏差，只要求「明显在下半部分且离底不远」。
        self.assertLess(abs(bottom - (CANVAS[1] - 40)), 40, f"底边位置 {bottom}")
        self.assertGreater(bottom, CANVAS[1] // 2)

    def test_center_ratio_y_centers_block(self) -> None:
        style = TextStyle(font_height_px=60, center_ratio_y=0.25)
        image = self.load(self.render("居中", style))
        box = _alpha_bbox(image)
        self.assertIsNotNone(box)
        top, bottom = box[1], box[3]  # type: ignore[index]
        center = (top + bottom) / 2.0
        self.assertLess(abs(center - CANVAS[1] * 0.25), 25, f"中心位置 {center}")

    def test_default_is_vertically_centered(self) -> None:
        image = self.load(self.render("默认居中", TextStyle(font_height_px=60)))
        box = _alpha_bbox(image)
        self.assertIsNotNone(box)
        center = (box[1] + box[3]) / 2.0  # type: ignore[index]
        self.assertLess(abs(center - CANVAS[1] / 2.0), 25)

    def test_center_ratio_beats_bottom_margin(self) -> None:
        # 两个都给时 center_ratio_y 优先，否则「标题压在上三分之一」会失效。
        style = TextStyle(font_height_px=60, center_ratio_y=0.2, bottom_margin_px=10)
        image = self.load(self.render("优先", style))
        box = _alpha_bbox(image)
        self.assertIsNotNone(box)
        center = (box[1] + box[3]) / 2.0  # type: ignore[index]
        self.assertLess(abs(center - CANVAS[1] * 0.2), 25)

    def test_scale_scales_canvas_and_text(self) -> None:
        full = self.load(self.render("缩放", TextStyle(font_height_px=80), name="full.png"))
        half = self.load(self.render("缩放", TextStyle(font_height_px=80), scale=0.5,
                                     name="half.png"))
        self.assertEqual(half.size, (CANVAS[0] // 2, CANVAS[1] // 2))
        full_box, half_box = _alpha_bbox(full), _alpha_bbox(half)
        self.assertIsNotNone(full_box)
        self.assertIsNotNone(half_box)
        self.assertLess(half_box[3] - half_box[1], full_box[3] - full_box[1])  # type: ignore[index]

    def test_text_stays_inside_canvas(self) -> None:
        # 版式计算错误最典型的症状是文字贴边/出界，这里直接卡住。
        image = self.load(self.render("边界", TextStyle(font_height_px=60, bottom_margin_px=0)))
        box = _alpha_bbox(image)
        self.assertIsNotNone(box)
        self.assertGreaterEqual(box[0], 0)  # type: ignore[index]
        self.assertGreaterEqual(box[1], 0)  # type: ignore[index]
        self.assertLessEqual(box[2], CANVAS[0])  # type: ignore[index]
        self.assertLessEqual(box[3], CANVAS[1])  # type: ignore[index]


if __name__ == "__main__":
    unittest.main(verbosity=2)
