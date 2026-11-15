#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""造一集 3 分钟的压测剧本 + 素材。

为什么要专门造这一集：短例子（3 场 8 句）证明不了排片在长剧本上还站得住。
这里给 6 场、60 句、5 个演员、14 段素材——素材数远少于镜头数，逼 ``story``
必须复用素材，正好用来量「复用是不是滥用」：同一段用了几次、有没有连着用。

    python examples/stress_long/make_episode.py

产物（都可随时删掉重建）：

    examples/stress_long/screenplay.json
    examples/stress_long/materials/*.mp4
    examples/stress_long/materials/bgm.mp3
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, "src"))

from cutwright import demo  # noqa: E402

MATERIALS = os.path.join(HERE, "materials")
SCREENPLAY = os.path.join(HERE, "screenplay.json")

# 文件名即标签：场次_地点_时间_景别_谁
# (文件名, 宽, 高, 时长, 说明) —— 与 demo.make_demo_media 的 CLIPS 同序
CLIPS = [
    ("S01_rooftop_dawn_wide.mp4", 1080, 1920, 6.0, "楼顶远景"),
    ("S01_rooftop_dawn_close_lin.mp4", 1080, 1920, 5.0, "楼顶近景 林岚"),
    ("S02_street_day_wide.mp4", 1920, 1080, 6.0, "街道远景（横屏）"),
    ("S02_street_day_close_zhou.mp4", 1080, 1920, 4.5, "街道近景 周野"),
    ("S03_office_night_wide.mp4", 1080, 1920, 6.0, "办公室远景"),
    ("S03_office_night_close_lin.mp4", 1080, 1920, 5.0, "办公室近景 林岚"),
    ("S03_office_night_detail.mp4", 1080, 1920, 3.5, "办公室特写"),
    ("S04_alley_rain_wide.mp4", 1080, 1920, 6.0, "雨巷远景"),
    ("S04_alley_rain_close_zhou.mp4", 1080, 1920, 4.0, "雨巷近景 周野"),
    ("S05_hospital_dawn_wide.mp4", 1080, 1920, 6.0, "医院远景"),
    ("S05_hospital_dawn_close_lin.mp4", 1080, 1920, 4.5, "医院近景 林岚"),
    ("S06_seaside_dusk_wide.mp4", 1920, 1080, 6.0, "海边远景（横屏）"),
    ("S06_seaside_dusk_close_zhou.mp4", 1080, 1920, 5.0, "海边近景 周野"),
    ("S06_seaside_dusk_detail.mp4", 1080, 1920, 3.0, "海边特写"),
]

CAST = {"lin": "林岚", "zhou": "周野", "chen": "陈叔", "tom": "汤姆", "xu": "徐医生"}

# 每场：id、地点标签、台词（who, type, text, 可选 duration）
SCENES = [
    ("S01", "rooftop dawn", [
        ("action", None, "天刚亮，楼顶的水塔还在滴水"),
        ("lin", "dialogue", "你昨晚没回家"),
        ("zhou", "dialogue", "回家了，只是没进屋"),
        ("lin", "dialogue", "周野，那份名单我看过了"),
        ("zhou", "voiceover", "她说话的时候，手一直按在栏杆上"),
        ("zhou", "dialogue", "所以呢，你也觉得我该停"),
        ("lin", "dialogue", "我觉得你该先睡一觉，再决定要不要停"),
        ("action", None, "风把她的外套吹起来，又落回去"),
        ("zhou", "dialogue", "名单上第三个人，是我爸的老同事"),
        ("lin", "dialogue", "我知道，我看名字就认出来了"),
    ]),
    ("S02", "street day", [
        ("action", None, "早高峰，人行道挤得像传送带"),
        ("zhou", "dialogue", "陈叔以前每周三都来这儿吃面"),
        ("lin", "dialogue", "那今天正好是周三"),
        ("zhou", "dialogue", "对，所以我在这儿站了四十分钟"),
        ("chen", "dialogue", "你们两个，跟我来后厨"),
        ("action", None, "面馆后厨的抽油烟机响得像下雨"),
        ("chen", "dialogue", "名单不是我给的，是有人塞进我店里的"),
        ("lin", "dialogue", "塞进来的时候，你有没有看见人"),
        ("chen", "dialogue", "看见一个背影，戴着医院的手环"),
        ("zhou", "voiceover", "医院。徐医生。这两个词在我脑子里撞了一下"),
    ]),
    ("S03", "office night", [
        ("action", None, "办公室只亮着半排灯"),
        ("lin", "dialogue", "徐医生的排班表，我在系统里查到了"),
        ("zhou", "dialogue", "她三号那晚不在医院"),
        ("lin", "dialogue", "可手环是她的编号"),
        ("tom", "dialogue", "有人在借用她的身份"),
        ("zhou", "dialogue", "汤姆，你为什么这么肯定"),
        ("tom", "dialogue", "因为我干过同样的事，五年前"),
        ("action", None, "窗外的霓虹闪了一下，随后安静"),
        ("lin", "dialogue", "那五年前那件事，最后怎么收的"),
        ("tom", "dialogue", "没收。所以现在还站在你们面前"),
    ]),
    ("S04", "alley rain", [
        ("action", None, "雨下大了，巷子里没有人"),
        ("zhou", "dialogue", "他知道我们会来"),
        ("lin", "dialogue", "他留了张纸条"),
        ("zhou", "voiceover", "纸上只有四个字：别查下去"),
        ("lin", "dialogue", "你怎么想"),
        ("zhou", "dialogue", "我想知道他是谁"),
        ("lin", "dialogue", "我更想知道他为什么怕"),
        ("action", None, "雨水顺着铁皮棚淌成一条线"),
        ("zhou", "dialogue", "怕，说明还活着"),
        ("lin", "dialogue", "活着，就还有下一次"),
    ]),
    ("S05", "hospital dawn", [
        ("action", None, "急诊楼的灯到天亮也没灭"),
        ("xu", "dialogue", "你们找我，是为了编号 0731 的手环"),
        ("lin", "dialogue", "看来您已经知道了"),
        ("xu", "dialogue", "三天前有人来借过，我没同意"),
        ("zhou", "dialogue", "那个人长什么样"),
        ("xu", "dialogue", "个子不高，右手小指少一截"),
        ("action", None, "林岚的手指在病历夹上停住"),
        ("lin", "dialogue", "陈叔，那个背影是不是也少一指"),
        ("action", None, "陈叔没说话，只是把烟按灭了"),
        ("zhou", "voiceover", "有些答案不用说出口"),
    ]),
    ("S06", "seaside dusk", [
        ("action", None, "傍晚的海边，退潮退到很远"),
        ("zhou", "dialogue", "名单还剩两个人"),
        ("lin", "dialogue", "我们已经查到第三个了"),
        ("zhou", "dialogue", "所以还剩两个"),
        ("lin", "dialogue", "不，是还剩两个愿意说话的"),
        ("action", None, "海风把纸吹得哗哗响"),
        ("zhou", "dialogue", "林岚，如果最后一个是她呢"),
        ("lin", "dialogue", "那也得查"),
        ("zhou", "dialogue", "好，那我陪你查到最后"),
        ("action", None, "两串脚印并排走向涨潮的地方"),
        ("zhou", "voiceover", "天黑了，但海岸线还看得见"),
    ]),
]


def build_screenplay() -> dict:
    scenes = []
    for scene_id, tags, lines in SCENES:
        items = []
        # 元组顺序是 (谁, 类型, 台词)；action 行没有 who。
        for who, kind, text in lines:
            if kind == "action":
                items.append({"type": "action", "text": text})
            elif kind == "voiceover":
                items.append({"type": "voiceover", "who": who, "text": text})
            else:
                items.append({"type": "dialogue", "who": who, "text": text})
        scenes.append({"id": scene_id, "tags": tags.split(), "lines": items})
    return {
        "name": "长夜名单 第1集",
        "canvas": {"width": 1080, "height": 1920},
        "fps": 30,
        "material_dir": "materials",
        "vo_dir": "materials",
        "cast": CAST,
        "music": {"file": "materials/bgm.mp3", "gain_db": -22},
        "scenes": scenes,
    }


def main() -> int:
    data = build_screenplay()
    with open(SCREENPLAY, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    lines = sum(len(s["lines"]) for s in data["scenes"])
    print("剧本: %s（%d 场 / %d 句）" % (SCREENPLAY, len(data["scenes"]), lines))

    ffmpeg = demo.find_ffmpeg(None)
    if not ffmpeg:
        print("找不到 ffmpeg：先装一个，或设 CUTWRIGHT_FFMPEG", file=sys.stderr)
        return 1

    # 素材总长按「最坏情况」给足：60 句 × 3s ≈ 180s，BGM 多给 10s。
    report = demo.make_demo_media(
        MATERIALS, clips=CLIPS, ffmpeg=ffmpeg, with_bgm=True, bgm_duration=190.0)
    print("素材: %d 新建 / %d 复用" % (len(report["created"]), len(report["skipped"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
