"""字幕 / 花字排版：把一段文字画成 canvas 同尺寸的透明 PNG。

为什么不交给 ffmpeg 的 ``drawtext``：

1. **关键词换色**。剪映那种「前半句白、后半句黄」的花字，一条 drawtext 只有
   一种颜色；要拆成多条滤镜就得自己算第二条的 x 偏移，等于在滤镜图里重写
   一遍排版器。
2. **中文折行**。折行按「字」数算（中文一字一格、英文一个字母一格），而
   drawtext 的自动换行按像素宽度，中英混排会折出参差不齐的断行。
3. **版式**。旋转、贴底、居中在 PIL 里一次算清；产物是 canvas 同尺寸的
   RGBA 图，后端拿过去只需要 ``overlay=0:0``，不必再算坐标——后端算坐标是
   bug 的温床，而且换后端就得重算一遍。

代价是多一个 Pillow 依赖，且文字是**位图**而非矢量。1080p 成片字号普遍在
80-120px，位图完全够用；改文案就重新渲染一次，比调滤镜便宜。
"""
from __future__ import annotations

import os
from typing import Dict, List, Sequence, Tuple

from PIL import Image, ImageColor, ImageDraw, ImageFont

from .schema import TextStyle

__all__ = ["FontNotFound", "find_font", "render_text_png", "wrap_text"]

# 一组 RGBA 颜色。Pillow 内部一律用 0-255 四通道，先转好再画，
# 省得每个字都解析一次 "＃RRGGBB"。
_RGBA = Tuple[int, int, int, int]

# 关键词分隔符。schema.TextSegment 也是按第一次出现的 "|" 切分，
# 两处必须一致，否则「显示的字」和「算颜色的字」会错位。
_SEPARATOR = "|"

# 找不到字体时的候选表。Windows 排在前面（本项目的绝大多数用户是剪映用户），
# 但表本身是跨平台的：``os.path.exists`` 判断一个不存在的路径只是一次失败的
# stat，比按平台分支更容易读，也不会出现「加了新平台忘了改分支」。
_WIN_FONT_DIR = os.environ.get("WINDIR") or r"C:\Windows"
_FONT_CANDIDATES: Tuple[str, ...] = (
    # Windows：微软雅黑粗 → 雅黑 → 黑体 → 宋体（由粗到细、由现代到传统）
    os.path.join(_WIN_FONT_DIR, "Fonts", "msyhbd.ttc"),
    os.path.join(_WIN_FONT_DIR, "Fonts", "msyh.ttc"),
    os.path.join(_WIN_FONT_DIR, "Fonts", "simhei.ttf"),
    os.path.join(_WIN_FONT_DIR, "Fonts", "simsun.ttc"),
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    # Linux：Noto CJK 是事实标准，wqy / DejaVu 是最后的兜底
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)

# find_font 的结果缓存：字体发现是一次次 os.path.exists，批量渲染几十条字幕时
# 每次都扫一遍候选表很浪费。
_FONT_PATH_CACHE: Dict[str, str] = {}

# ImageFont.truetype 每次都要读字体文件、解析字形表，是这个模块最贵的一步。
# 同一条时间线里同字号会重复出现几十次，所以按 (路径, 字号, 索引) 缓存。
_FONT_OBJ_CACHE: Dict[Tuple[str, int], ImageFont.FreeTypeFont] = {}


class FontNotFound(RuntimeError):
    """找不到任何可用字体。

    必须当场抛出，不能静默回退成一张空图：没有字体时「字幕没了」在成片里
    看不出来原因，用户会以为是自己文案写错了。
    """


# --------------------------------------------------------------------------- #
# 字体发现
# --------------------------------------------------------------------------- #
def find_font(prefer: str = "") -> str:
    """返回一个可用字体文件的绝对路径。

    顺序是「显式指定 > 环境变量 > 平台候选表」。``prefer`` 通常是
    :attr:`~cutwright.schema.TextStyle.font`，只在它**确实存在**时才生效：
    分镜表里写错一个字体名不该让整条片子渲染不出来，回退到候选表比炸掉
    更符合「先把片子渲出来」的预期。

    环境变量 ``CUTWRIGHT_FONT`` 是给无字体环境的逃生口（CI、容器）。
    """
    key = prefer or ""
    cached = _FONT_PATH_CACHE.get(key)
    if cached:
        return cached

    candidates: List[str] = []
    if prefer:
        candidates.append(prefer)
    env = os.environ.get("CUTWRIGHT_FONT")
    if env:
        candidates.append(env)
    candidates.extend(_FONT_CANDIDATES)

    for path in candidates:
        try:
            if path and os.path.isfile(path):
                resolved = os.path.abspath(path)
                _FONT_PATH_CACHE[key] = resolved
                return resolved
        except OSError:
            # 权限/路径非法都当成「这个候选不适用」，继续下一个
            continue

    raise FontNotFound(
        "找不到可用字体。装一个中文字体（如思源黑体 / 微软雅黑），"
        "或用环境变量 CUTWRIGHT_FONT 指向字体文件。已尝试: "
        + ", ".join(candidates[:4])
        + (" ..." if len(candidates) > 4 else "")
    )


def _load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    """按 (路径, 字号) 缓存字体对象。字体文件损坏时抛 FontNotFound。"""
    size = max(1, int(size))
    key = (path, size)
    font = _FONT_OBJ_CACHE.get(key)
    if font is not None:
        return font
    try:
        # ttc 是字体集合，index=0 是第一个字重；雅黑粗体的 0 号就是 Bold。
        font = ImageFont.truetype(path, size)
    except (OSError, ValueError) as exc:  # 文件不是字体 / 被截断
        raise FontNotFound(f"字体文件无法加载: {path} ({exc})") from exc
    _FONT_OBJ_CACHE[key] = font
    return font


# --------------------------------------------------------------------------- #
# 折行
# --------------------------------------------------------------------------- #
def wrap_text(text: str, max_chars: int) -> list[str]:
    """按字符数折行，返回每一行的文本。

    ``max_chars <= 0`` 表示不折行（整段一行）。``|`` 是关键词分隔符，不显示、
    也不占一格。英文单词不会被从中间劈开：剩下最后一格不足以放完整个单词时
    提前换行（中文没有词边界，按字切）。

    空文本返回空列表——调用方据此决定「要不要出这张图」。
    """
    chars = [(ch, "") for ch in _strip_separator(text)]
    return ["".join(ch for ch, _ in line) for line in _wrap_styled(chars, max_chars)]


def _strip_separator(text: str) -> str:
    """去掉第一个 ``|``（分隔符），保留后面可能出现的一切字符。"""
    head, sep, tail = (text or "").partition(_SEPARATOR)
    return head + tail if sep else head


def _is_word_char(ch: str) -> bool:
    """能不能把一个 ASCII 字母/数字当成「单词的一部分」。

    只对 ASCII 生效：中文、日文、标点都按「可以随便断」处理，这正是中文折行
    想要的行为。数字按词处理，避免把 "2024" 拆成 "20" + "24"。
    """
    return bool(ch) and ch.isascii() and (ch.isalnum() or ch in "'-_")


def _wrap_styled(chars: Sequence[Tuple[str, str]], max_chars: int) -> List[List[Tuple[str, str]]]:
    """折行的核心：输入 (字符, 颜色) 序列，输出多行。

    颜色随字符走，所以折行只搬移不重排——画的时候每行内部按颜色分段即可，
    不必再回头去原串里找位置。
    """
    if not chars:
        return []

    # 硬换行（"\n"）先切段：分镜表里的多行文案不该被折行逻辑揉成一行。
    paragraphs: List[List[Tuple[str, str]]] = [[]]
    for item in chars:
        if item[0] == "\n":
            paragraphs.append([])
        else:
            paragraphs[-1].append(item)

    limit = int(max_chars)
    lines: List[List[Tuple[str, str]]] = []
    for para in paragraphs:
        if not para:
            continue
        if limit <= 0:
            lines.append(_rstrip(para))
            continue
        rest: List[Tuple[str, str]] = list(para)
        while rest:
            if len(rest) <= limit:
                lines.append(_rstrip(rest))
                break
            cut = limit
            # 断点落在英文单词中间时回退到词首。整行就是一个超长单词时
            # 无处可退，只能硬切——否则会退化成每行一个字母。
            if _is_word_char(rest[cut - 1][0]) and _is_word_char(rest[cut][0]):
                back = cut
                while back > 0 and _is_word_char(rest[back - 1][0]):
                    back -= 1
                if back > 0:
                    cut = back
            lines.append(_rstrip(rest[:cut]))
            rest = _lstrip(rest[cut:])

    # 折行可能折出空行（原文里连续的空白），空行没有字形，丢掉更省版面。
    return [line for line in lines if line]


def _rstrip(chars: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    end = len(chars)
    while end > 0 and chars[end - 1][0].isspace():
        end -= 1
    return chars[:end]


def _lstrip(chars: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    start = 0
    while start < len(chars) and chars[start][0].isspace():
        start += 1
    return chars[start:]


# --------------------------------------------------------------------------- #
# 颜色
# --------------------------------------------------------------------------- #
def _parse_color(value: str, default: _RGBA) -> _RGBA:
    """``#RRGGBB`` / ``#RGB`` / ``#RRGGBBAA`` / 少数 CSS 名 → RGBA。

    解析失败返回 ``default``：一个笔误的颜色值不值得让整条片子渲不出来。
    """
    text = (value or "").strip()
    if not text:
        return default
    try:
        if text.startswith("#") and len(text) in (9,):  # #RRGGBBAA
            r = int(text[1:3], 16)
            g = int(text[3:5], 16)
            b = int(text[5:7], 16)
            a = int(text[7:9], 16)
            return (r, g, b, a)
        rgb = ImageColor.getrgb(text)
    except (ValueError, TypeError, IndexError):
        return default
    if len(rgb) == 4:
        return (rgb[0], rgb[1], rgb[2], rgb[3])
    return (rgb[0], rgb[1], rgb[2], 255)


def _styled_chars(text: str) -> List[Tuple[str, str]]:
    """把文案切成 (字符, 颜色标签) 序列；只有第一个 ``|`` 是分隔符。

    这里存的是标签（"body" / "kw"）而不是 RGBA 元组：折行只关心字符顺序，
    颜色在画的时候查表，序列本身保持轻量、可比较、出错时好打印。
    """
    head, sep, tail = (text or "").partition(_SEPARATOR)
    out: List[Tuple[str, str]] = [(ch, "body") for ch in head]
    if sep:
        out.extend((ch, "kw") for ch in tail)
    return out


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def render_text_png(text: str, style: TextStyle, canvas: tuple[int, int],
                    out_png: str, *, scale: float = 1.0) -> str:
    """把 ``text`` 按 ``style`` 画成 canvas 同尺寸的透明 PNG，返回绝对路径。

    输出尺寸**固定等于 canvas** 是刻意的契约：后端只写 ``overlay=0:0``，
    不需要知道文字落在哪、有多大。代价是每张字幕图都占 canvas 大小，
    但 PNG 的透明区域压缩率极高，实测一整条片子的字幕图也就几百 KB。

    ``scale`` 只用来整体缩放（预览图用 0.5 就很够），版式比例不变。
    ``style.invisible`` 时照样写一张全透明 PNG：有的后端要求「时间线上有这段
    素材」才能把旁白时长对齐，缺文件比缺像素更麻烦。
    """
    width = max(1, int(round(canvas[0] * scale))) if canvas else 1
    height = max(1, int(round(canvas[1] * scale))) if canvas else 1
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))

    if not style.invisible and (text or "").strip():
        body = _parse_color(style.color, (255, 255, 255, 255))
        keyword = _parse_color(style.keyword_color, (255, 213, 74, 255))
        stroke = _parse_color(style.stroke_color, (0, 0, 0, 255))
        lines = _wrap_styled(_styled_chars(text), style.max_chars_per_line)
        if lines:
            _draw_lines(image, lines, {"body": body, "kw": keyword}, stroke, style, scale)

    out_abs = os.path.abspath(out_png)
    parent = os.path.dirname(out_abs)
    if parent:
        os.makedirs(parent, exist_ok=True)
    image.save(out_abs, format="PNG")
    return out_abs


def _draw_lines(image: Image.Image, lines: List[List[Tuple[str, str]]],
                palette: Dict[str, _RGBA], stroke: _RGBA, style: TextStyle,
                scale: float) -> None:
    """把折好的行画成一块，再按 style 的版式贴到 canvas 上。"""
    font_path = find_font(style.font)
    font_size = max(1, int(round(float(style.font_height_px) * scale)))
    font = _load_font(font_path, font_size)
    stroke_w = max(0, int(round(float(style.stroke_width_px) * scale)))

    ascent, descent = font.getmetrics()
    text_h = ascent + descent
    # 行距比字身高一点点，中文密排才不会「上下粘连」。
    line_h = max(text_h, 1) + max(1, int(round(font_size * 0.18)))

    # 每行拆成「同色连续段」并量宽。用 font.getlength 而不是 getbbox：
    # 后者会丢掉行尾空格，多段拼接时累计偏移会偏。
    measured: List[List[Tuple[str, _RGBA, float]]] = []
    for line in lines:
        runs: List[Tuple[str, _RGBA, float]] = []
        for label, group in _group_by_label(line):
            runs.append((group, palette.get(label, palette["body"]),
                         float(font.getlength(group))))
        measured.append(runs)

    content_w = max((sum(r[2] for r in runs) for runs in measured), default=0.0)
    # 描边是向字形**外侧**扩的，两侧都要留出 stroke_w，否则贴到画布边缘会缺角。
    pad = stroke_w + max(2, int(round(2 * scale)))
    block_w = max(1, int(round(content_w)) + pad * 2)
    block_h = max(1, line_h * len(measured) + pad * 2)

    block = Image.new("RGBA", (block_w, block_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(block)

    align = (style.align or "center").strip().lower()
    for index, runs in enumerate(measured):
        line_w = sum(r[2] for r in runs)
        if align in ("left", "start"):
            x = float(pad)
        elif align in ("right", "end"):
            x = float(block_w - pad) - line_w
        else:
            x = (block_w - line_w) / 2.0
        y = pad + index * line_h + max(0, (line_h - text_h) // 2)
        for run_text, color, run_w in runs:
            # 每个同色段单独画。相邻段的描边会互相覆盖，但颜色相同，
            # 所以接缝处看不出来——比逐字画快，也不会出现字间缝隙。
            draw.text((x, y), run_text, font=font, fill=color,
                      stroke_width=stroke_w, stroke_fill=stroke)
            x += run_w

    _paste_block(image, block, style, scale)


def _group_by_label(line: Sequence[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """把 (字符, 标签) 压成 (标签, 文本)，相邻同标签合并。"""
    out: List[Tuple[str, str]] = []
    for ch, label in line:
        if out and out[-1][0] == label:
            out[-1] = (label, out[-1][1] + ch)
        else:
            out.append((label, ch))
    return out


def _paste_block(image: Image.Image, block: Image.Image, style: TextStyle,
                 scale: float) -> None:
    """按 style 的垂直规则定位，必要时旋转，再把块贴进 canvas。"""
    width, height = image.size

    # 垂直：center_ratio_y 优先（「把标题压在画面上三分之一」这种诉求最常用），
    # 其次是贴底（字幕的安全区由用户按平台要求给），都没有就居中。
    if style.center_ratio_y is not None:
        cy = height * float(style.center_ratio_y)
        top = cy - block.height / 2.0
    elif style.bottom_margin_px is not None:
        bottom_margin = float(style.bottom_margin_px) * scale
        top = height - bottom_margin - block.height
    else:
        top = (height - block.height) / 2.0

    align = (style.align or "center").strip().lower()
    if align in ("left", "start"):
        left = 0.0
    elif align in ("right", "end"):
        left = float(width - block.width)
    else:
        left = (width - block.width) / 2.0

    rotation = float(style.rotation_deg or 0.0)
    if abs(rotation) > 1e-6:
        # 先记下块心，旋转后仍以它为心——否则转一下文字就跑到别处去了。
        center_x = left + block.width / 2.0
        center_y = top + block.height / 2.0
        # Pillow 的 rotate 正角是逆时针；分镜表里说的「旋转 15 度」是顺时针
        # （视觉上往右下压），所以取负号。expand=True 保证四角不被裁。
        rotated = block.rotate(-rotation, resample=Image.BICUBIC, expand=True,
                               fillcolor=(0, 0, 0, 0))
        left = center_x - rotated.width / 2.0
        top = center_y - rotated.height / 2.0
        block = rotated

    _composite_clipped(image, block, int(round(left)), int(round(top)))


def _composite_clipped(image: Image.Image, block: Image.Image, x: int, y: int) -> None:
    """把 block 贴到 (x, y)，超出画布的部分裁掉。

    ``Image.alpha_composite`` 不接受负偏移，而「旋转后的块比画布还大」是正常
    情况，所以在这里先算交集再贴——不能让一次越界变成异常。
    """
    left = max(0, x)
    top = max(0, y)
    right = min(image.width, x + block.width)
    bottom = min(image.height, y + block.height)
    if right <= left or bottom <= top:
        return
    crop = block.crop((left - x, top - y, right - x, bottom - y))
    image.alpha_composite(crop, (left, top))
