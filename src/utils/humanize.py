# -*- coding: utf-8 -*-
'''挂机节奏（2026-11-11）。

把「到点就按、每圈同一条像素、一次开到天亮」拆开。全部是纯函数，
引擎和键盘线程只负责记账。配置段缺了 → enable 为 False，旧行为不变。

不碰的两件事：
  · 跳跃指令不会被抽掉。窄平台上少跳一次就会掉下去。
  · 不识别、不点击反挂机验证。只在画面对上用户自己放进 antimacro/ 的模板时停手。
'''
from __future__ import annotations

import json
import os

DEFAULTS = {
    "enable": True,
    "attack_jitter": 0.25,
    "buff_jitter": 0.20,
    "jump_jitter": 0.10,
    "attack_skip_prob": 0.12,
    "edge_skip_prob": 0.45,
    "skip_min_sec": 1.2,
    "skip_max_sec": 3.5,
    "edge_ratio": 0.82,
    "react_min_sec": 0.18,
    "react_max_sec": 0.65,
    "session_min_sec": 1200,
    "session_max_sec": 2700,
    "rest_min_sec": 480,
    "rest_max_sec": 1080,
    "dwell_every_min_sec": 25,
    "dwell_every_max_sec": 70,
    "dwell_prob": 0.40,
    "dwell_min_sec": 0.5,
    "dwell_max_sec": 1.6,
    "turnaround_dwell_prob": 0.55,
    "turnaround_dwell_min_sec": 0.4,
    "turnaround_dwell_max_sec": 1.3,
    "antimacro_thres": 0.18,
    "antimacro_every": 8,
}

_FLOAT_KEYS = tuple(k for k in DEFAULTS if k != "enable")
_INT_KEYS = ("antimacro_every",)


def _as_float(value, fallback):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(fallback)


def _as_bool(value, fallback):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off"):
            return False
    return bool(fallback)


def params(cfg) -> dict:
    '''合并 humanize 段。没有这段 → enable False（离线桩配置不改行为）。'''
    raw = (cfg or {}).get("humanize") if isinstance(cfg, dict) else None
    if not isinstance(raw, dict):
        out = dict(DEFAULTS)
        out["enable"] = False
        return out
    out = dict(DEFAULTS)
    out["enable"] = _as_bool(raw.get("enable", True), True)
    for key in _FLOAT_KEYS:
        if key in raw:
            out[key] = _as_float(raw.get(key), DEFAULTS[key])
    if "antimacro_every" in raw:
        try:
            out["antimacro_every"] = max(1, int(raw.get("antimacro_every")))
        except (TypeError, ValueError):
            out["antimacro_every"] = int(DEFAULTS["antimacro_every"])
    return out


def jitter_interval(base, ratio, rng, min_scale=0.0) -> float:
    '''base × uniform(max(min_scale, 1-ratio), 1+ratio)。ratio<=0 或 base<=0 时原样返回。'''
    try:
        base = float(base)
        ratio = float(ratio)
        min_scale = float(min_scale)
    except (TypeError, ValueError):
        return 0.0
    if base <= 0 or ratio <= 0:
        return base
    lo = max(min_scale, 1.0 - ratio)
    hi = 1.0 + ratio
    if lo > hi:
        lo = hi
    if lo == hi:
        return base * lo
    return base * rng.uniform(lo, hi)


def uniform_span(lo, hi, rng) -> float:
    '''闭区间随机长度。两端都 <=0 时返回 0。'''
    lo = _as_float(lo, 0.0)
    hi = _as_float(hi, 0.0)
    if hi < lo:
        lo, hi = hi, lo
    if hi <= 0:
        return 0.0
    if lo < 0:
        lo = 0.0
    if hi == lo:
        return lo
    return rng.uniform(lo, hi)


def session_span(p, rng):
    '''本段挂机秒数。session_max_sec<=0 → None（不限时）。'''
    hi = _as_float(p.get("session_max_sec"), 0.0)
    if hi <= 0:
        return None
    return uniform_span(p.get("session_min_sec"), hi, rng)


def monster_span_ratio(monsters, player, dx, dy):
    '''离角色最近那只怪，在攻击框里的相对位置。0 = 贴着人，1 = 框边。

    size 按攻击逻辑的解包方式当 (宽, 高)。没有怪、或框退化时返回 None。
    '''
    try:
        dx = float(dx)
        dy = float(dy)
        px, py = float(player[0]), float(player[1])
    except (TypeError, ValueError, IndexError):
        return None
    if dx <= 0 or dy <= 0:
        return None
    best = None
    for monster in monsters or []:
        try:
            mx, my = monster["position"]
            mw, mh = monster["size"]
        except (KeyError, TypeError, ValueError):
            continue
        cx = float(mx) + float(mw) / 2.0
        cy = float(my) + float(mh) / 2.0
        ratio = max(abs(cx - px) / dx, abs(cy - py) / dy)
        if best is None or ratio < best:
            best = ratio
    return best


def is_edge_target(monsters, player, dx, dy, edge_ratio) -> bool:
    ratio = monster_span_ratio(monsters, player, dx, dy)
    if ratio is None:
        return False
    return ratio >= _as_float(edge_ratio, 1.0)


def decide_attack(phase, now, cooled, react_until, is_edge,
                  skip_prob, edge_skip_prob, react_min, react_max, rng,
                  skip_min=1.2, skip_max=3.5):
    '''这一帧进不进原来的出招状态机。

    返回 (allow, phase, react_until)。
      allow True  → 调用方照旧跑转身/冷却判断
      allow False → 这一帧不出招（在等反应，或决定放过这拨怪）
    phase: idle / wait / skip / commit

    放过只维持 skip_min~skip_max 秒。小平台上怪可能一直在攻击框里，
    不能把「这拨不打」理解成永远不打。
    '''
    phase = phase or "idle"
    now = _as_float(now, 0.0)
    react_until = _as_float(react_until, 0.0)
    if phase == "commit":
        return True, "commit", react_until
    if not cooled:
        return True, "idle", 0.0
    if phase == "skip":
        if now < react_until:
            return False, "skip", react_until
        return False, "idle", 0.0
    if phase == "wait":
        if now < react_until:
            return False, "wait", react_until
        return True, "commit", 0.0
    prob = edge_skip_prob if is_edge else skip_prob
    prob = min(1.0, max(0.0, _as_float(prob, 0.0)))
    if rng.random() < prob:
        hold = uniform_span(skip_min, skip_max, rng)
        if hold <= 0:
            hold = 1.0
        return False, "skip", now + hold
    delay = uniform_span(react_min, react_max, rng)
    if delay > 0:
        return False, "wait", now + delay
    return True, "commit", 0.0


def turnaround_dwell(prob, lo, hi, rng) -> float:
    '''掉头/切段时要站多久。没抽中返回 0。'''
    prob = min(1.0, max(0.0, _as_float(prob, 0.0)))
    if rng.random() >= prob:
        return 0.0
    return uniform_span(lo, hi, rng)


def rest_remaining(until, now) -> float:
    try:
        left = float(until) - float(now)
    except (TypeError, ValueError):
        return 0.0
    return left if left > 0 else 0.0


def format_rest_block(left_sec) -> str:
    mins = max(1, int(round(float(left_sec) / 60.0)))
    return (
        f"上一段挂机刚结束，还要休息约 {mins} 分钟再开始。\n"
        "这段空档是故意留的。想马上继续，把 humanize.enable 改成 false。"
    )


def read_rest(path) -> float:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return float(data.get("rest_until") or 0)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0.0


def write_rest(path, until, reason) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    payload = {"rest_until": float(until), "reason": str(reason)}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, path)


def match_antimacro(frame_gray, templates, thres):
    '''在灰度画面里找验证模板。命中返回 (名字, 分数)，否则 None。

    分数是 TM_SQDIFF_NORMED，越低越像。模板比画面还大时跳过。
    '''
    import cv2
    import numpy as np

    if frame_gray is None or not templates:
        return None
    thres = _as_float(thres, 0.18)
    best = None
    for name, tpl in templates:
        if tpl is None:
            continue
        if tpl.shape[0] > frame_gray.shape[0] or tpl.shape[1] > frame_gray.shape[1]:
            continue
        res = cv2.matchTemplate(frame_gray, tpl, cv2.TM_SQDIFF_NORMED)
        res = np.nan_to_num(res, nan=1.0, posinf=1.0, neginf=1.0)
        score = float(cv2.minMaxLoc(res)[0])
        if best is None or score < best[1]:
            best = (str(name), score)
    if best is not None and best[1] < thres:
        return best
    return None
