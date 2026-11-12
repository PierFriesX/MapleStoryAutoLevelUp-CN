# -*- coding: utf-8 -*-
'''离线自检：挂机节奏（humanize）。不开游戏、不初始化按键驱动。

用法：
    PYTHONPATH=<项目根> python tools/verify_humanize.py
'''
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.humanize import (
    DEFAULTS, decide_attack, format_rest_block, is_edge_target,
    jitter_interval, match_antimacro, monster_span_ratio, params,
    read_rest, rest_remaining, session_span, turnaround_dwell,
    uniform_span, write_rest,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []


def check(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def test_params():
    off = params({})
    check("没有 humanize 段 → 关掉", off["enable"] is False)
    on = params({"humanize": {"enable": True, "attack_jitter": "nope"}})
    check("enable true 且坏数字回落到默认",
          on["enable"] is True and on["attack_jitter"] == DEFAULTS["attack_jitter"])
    check("字符串 false 能关掉",
          params({"humanize": {"enable": "false"}})["enable"] is False)


def test_jitter():
    rng = random.Random(1)
    vals = [jitter_interval(1.0, 0.25, rng) for _ in range(40)]
    check("抖动落在 ±25%", all(0.75 <= v <= 1.25 for v in vals))
    check("抖动不是常数", len(set(round(v, 5) for v in vals)) > 1)
    check("比例 0 保持原值", jitter_interval(0.9, 0, random.Random(0)) == 0.9)
    short = [jitter_interval(1.0, 0.5, random.Random(i), min_scale=0.9) for i in range(30)]
    check("跳跃最短不低于九成", all(v >= 0.9 for v in short))


def test_attack_gate():
    rng = random.Random(0)
    allow, phase, _ = decide_attack(
        "idle", 10.0, False, 0, False, 1, 1, 0.2, 0.5, rng)
    check("冷却没好 → 放行给原来的冷却判断", allow and phase == "idle")

    allow, phase, until = decide_attack(
        "idle", 10.0, True, 0, False, 0, 0, 0.2, 0.2, random.Random(0))
    check("必延迟 → 本帧不出招", (not allow) and phase == "wait" and until == 10.2)

    allow, phase, _ = decide_attack(
        "wait", 10.2, True, 10.2, False, 0, 0, 0.2, 0.2, random.Random(0))
    check("延迟到点 → 进入出招", allow and phase == "commit")

    allow, phase, _ = decide_attack(
        "commit", 11.0, True, 0, False, 1, 1, 1, 1, random.Random(0))
    check("已经在出招 → 不再重抽", allow and phase == "commit")

    allow, phase, until = decide_attack(
        "idle", 10.0, True, 0, False, 1, 0, 0, 0, random.Random(0), 1.2, 1.2)
    check("必放过 → 不出招，并且过一会会重新抽",
          (not allow) and phase == "skip" and until == 11.2)
    allow, phase, _ = decide_attack(
        "skip", 11.0, True, 11.2, False, 0, 0, 0, 0, random.Random(0))
    check("放过的时间还没到 → 继续不打", (not allow) and phase == "skip")
    allow, phase, _ = decide_attack(
        "skip", 11.2, True, 11.2, False, 0, 0, 0, 0, random.Random(0))
    check("放过时间到了 → 回到空闲，下一帧重新决定",
          (not allow) and phase == "idle")

    player = (100, 100)
    center = {"position": (90, 90), "size": (20, 20)}
    edge = {"position": (180, 90), "size": (20, 20)}
    check("贴着人不是边上", not is_edge_target([center], player, 100, 40, 0.82))
    check("靠近框边算边上", is_edge_target([edge], player, 100, 40, 0.82))
    check("最近的一只在框内就不算边上",
          not is_edge_target([center, edge], player, 100, 40, 0.82))
    check("没有怪没有比例", monster_span_ratio([], player, 100, 40) is None)


def test_session_and_rest():
    span = session_span(
        {"session_min_sec": 10, "session_max_sec": 20}, random.Random(2))
    check("会话时长落在区间里", 10 <= span <= 20)
    check("max<=0 表示不限时",
          session_span({"session_min_sec": 10, "session_max_sec": 0},
                       random.Random(0)) is None)
    check("休息剩余时间", rest_remaining(100, 40) == 60)
    check("休息过了就是 0", rest_remaining(100, 100) == 0)
    check("休息文案带分钟", "分钟" in format_rest_block(90))
    dur = [turnaround_dwell(1, 0.4, 0.4, random.Random(0))]
    check("掉头停留抽中就给出时长", dur[0] == 0.4)
    check("概率 0 不停留",
          turnaround_dwell(0, 1, 2, random.Random(0)) == 0.0)
    check("uniform 两端为 0 得到 0", uniform_span(0, 0, random.Random(0)) == 0.0)

    path = os.path.join(tempfile.mkdtemp(), "rest.json")
    write_rest(path, 1234.5, "session")
    check("休息时间能写能读", read_rest(path) == 1234.5)
    check("缺文件当成没在休息", read_rest(path + ".missing") == 0.0)


def test_antimacro():
    try:
        import cv2
        import numpy as np
    except ImportError:
        check("本机没有 cv2，跳过画面匹配", True, "未安装 opencv")
        return
    frame = np.zeros((40, 60), np.uint8)
    frame[10:16, 20:28] = 255
    tpl = frame[10:16, 20:28].copy()
    hit = match_antimacro(frame, [("dlg.png", tpl)], 0.18)
    check("裁下来的验证块能对上", hit is not None and hit[0] == "dlg.png" and hit[1] < 0.05)
    other = np.full((6, 8), 30, np.uint8)
    check("不像的图不对上", match_antimacro(frame, [("no.png", other)], 0.18) is None)
    check("没有模板就跳过", match_antimacro(frame, [], 0.18) is None)


def test_config_and_wiring():
    text = open(os.path.join(ROOT, "config", "config_default.yaml"),
                encoding="utf-8").read()
    block = text.split("humanize:", 1)[1].split("\nwatchdog:", 1)[0]
    check("默认配置打开节奏", "enable: true" in block)
    check("默认会限时", "session_max_sec: 2700" in block)
    check("默认会休息", "rest_min_sec: 480" in block)

    eng = open(os.path.join(ROOT, "src", "engine", "MapleStoryAutoLevelUp.py"),
               encoding="utf-8").read()
    kb = open(os.path.join(ROOT, "src", "input", "KeyBoardController.py"),
              encoding="utf-8").read()
    hunt = open(os.path.join(ROOT, "src", "states", "hunting.py"), encoding="utf-8").read()
    ui = open(os.path.join(ROOT, "src", "ui", "ui.py"), encoding="utf-8").read()
    check("出招走节奏闸门", "_human_attack_allowed" in eng)
    check("到点会结束本段", "_finish_session" in eng and "_session_due" in eng)
    check("会扫验证模板", "_scan_antimacro" in eng)
    check("跳跃冷却会重抽", "_roll_jump_cd" in kb and "self.t_last_jump = time.time()" in kb)
    check("增益冷却会重抽", "_roll_buff_cd" in kb)
    check("打怪帧会插入停留", "apply_human_pace" in hunt)
    check("界面认识休息拦截", "ret == -4" in ui)


def main():
    test_params()
    test_jitter()
    test_attack_gate()
    test_session_and_rest()
    test_antimacro()
    test_config_and_wiring()
    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：{', '.join(FAILS)}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
