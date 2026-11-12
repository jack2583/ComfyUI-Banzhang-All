# -*- coding: utf-8 -*-
"""从 ComfyUI prompt 确定性编译段身份 Ticket（内部数据契约，**不是节点**）。

为什么从 prompt 编译
--------------------
execution.py:219-220 里 hidden.PROMPT 注入的是
``dynprompt.get_original_prompt()``，同一次执行里导演台与存档节点拿到的是
**同一份 prompt 字典**。两边各自算出同一张 Ticket，于是：

  * 不读全局 ``chain.json.current``（消除异步写 current 导致的错段）
  * 不查进程内短时表（导演台被 ComfyUI 缓存跳过也照样算得出来）
  * 不加端口、不改连线、旧工作流零改动

定位导演台优先走存档节点已有的「顺序锚」连线（它本来就接导演台输出 3），
所以这个原本「不参与计算」的口现在成了真正的身份链；多导演台也能对上。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

try:
    from . import csglide_ledger as ledger
except ImportError:
    import csglide_ledger as ledger

CAST_TYPES = ("CSGlideCastCS", "CSGlideCastLegacyCS")


class TicketError(ValueError):
    """段身份契约违规。一律硬失败，不再兜底新建段。"""


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def prompt_fingerprint(prompt: Any) -> str:
    if not isinstance(prompt, dict) or not prompt:
        raise TicketError("hidden.prompt 未注入或为空：请确认节点声明了 hidden PROMPT")
    return digest(prompt)


@dataclass(frozen=True)
class SegmentTicket:
    project_id: str
    clip_id: str
    shot: str
    id_kind: str
    length: int
    gross_frames: int
    trim_head: int
    net_frames: int
    fps: int
    fingerprint: str
    cast_node_id: str

    def ticket_hash(self):
        return digest(asdict(self))

    @property
    def label(self):
        return "%s / %s" % (self.project_id, self.clip_id)


def _locate_cast(prompt, node_id=None):
    """按 顺序锚 → 本节点自身 → 全图唯一 的优先级定位导演台。"""
    if node_id is not None:
        mine = prompt.get(str(node_id))
        if isinstance(mine, dict):
            anchor = (mine.get("inputs") or {}).get("顺序锚")
            if isinstance(anchor, (list, tuple)) and len(anchor) >= 2:
                src = prompt.get(str(anchor[0]))
                if isinstance(src, dict) and src.get("class_type") in CAST_TYPES:
                    return str(anchor[0])
            if mine.get("class_type") in CAST_TYPES:
                return str(node_id)
    found = [str(k) for k, v in prompt.items()
             if isinstance(v, dict) and v.get("class_type") in CAST_TYPES]
    if len(found) == 1:
        return found[0]
    if not found:
        raise TicketError("本次 prompt 里没有导演台节点（CSGlideCastCS）")
    raise TicketError("本次 prompt 有 %d 个导演台（%s），且顺序锚没能定位；"
                      "请把存档节点的「顺序锚」接到对应导演台的输出口"
                      % (len(found), ",".join(found)))


def resolve_clip_id(name, clip, shot, summary_fn=None):
    """按 _register_clip 同口径解析真实段 id（段身份的唯一入口）。

    两条提交路径，身份来源不同（20261107 后）：
      · 编译通道（导演台正常提交）：bz_prompt_build.js 在提交时写入本段真实
        cid，并有一处兜底再钉一次 —— 这里显式 clip 优先即可命中。
      · 裸提交（直接 Queue / 旧工作流 / 外部 POST /prompt）：
        csglide_cast.js 的 serializeValue 会把 widget 里的 chain.clip 抹成 ""
        （防止工作流跨机器分发时带着别的工程的 clip_XXXX 撞 _SegBindingLost），
        此时只能按 chain.shot 这个分段 uid 从档案反查。
    所以所有需要 cid 的下游（承接档定位、自动收片、视频合并归属、
    时间轴音频驱动）都必须走这里，不能再各自读 chain.clip。
    都拿不到返回空串；调用方必须打日志说明影响，不要静默退化。
    """
    cid = str(clip or "").strip()
    if cid:
        return cid
    shot = str(shot or "").strip()
    if not (name and shot and summary_fn):
        return ""
    try:
        for row in (summary_fn(name).get("clips") or []):
            if str(row.get("shot") or "") == shot:
                return str(row.get("id") or "")
    except Exception:
        return ""
    return ""


def ticket_from_prompt(prompt, node_id=None):
    """把一次提交的 prompt 编译成唯一段身份。找不到 / 不完整一律抛中文错误。"""
    fingerprint = prompt_fingerprint(prompt)
    cast_id = _locate_cast(prompt, node_id)
    spec = prompt[cast_id]
    inputs = spec.get("inputs") or {}
    override = inputs.get("h3_override")
    if isinstance(override, str) and override.strip():
        raw = override
    elif isinstance(override, (list, tuple)) and len(override) >= 2:
        # 中枢/无头编排可能把 h3_data 从上游节点接进来。能读到字面值就用它；
        # 读不到必须硬失败，绝不能退回去用面板里那份旧的 h3_data（会认错段）。
        src = prompt.get(str(override[0]))
        raw = None
        if isinstance(src, dict):
            upstream = src.get("inputs") or {}
            for key in ("h3_data", "value", "text", "string"):
                value = upstream.get(key)
                if isinstance(value, str) and value.strip():
                    raw = value
                    break
        if raw is None:
            raise TicketError(
                "导演台 h3_override 接了 #%s 的连线，但上游没有可直接读取的字面值；"
                "Ticket 不能拿面板里的旧 h3_data 顶替（会认错段）。"
                "请让上游直接输出 h3_data 字符串，或改用 h3_data 字面值提交。"
                % override[0])
    else:
        raw = inputs.get("h3_data")
    if isinstance(raw, (list, tuple)):
        raise TicketError("导演台 h3_data 是连线而非字面值；Ticket 需要字面 JSON")
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError) as exc:
        raise TicketError("导演台 h3_data 不是合法 JSON：%s" % exc)
    if not isinstance(data, dict):
        raise TicketError("导演台 h3_data 解析后不是对象")

    chain = data.get("chain") or {}
    name = str(chain.get("name") or "").strip()
    cid = str(chain.get("clip") or "").strip()
    shot = str(chain.get("shot") or "").strip()
    if not name:
        raise TicketError("导演台 h3_data.chain.name 为空：工程名没登记")
    if not cid and not shot:
        raise TicketError(
            "导演台 h3_data.chain 的 clip 与 shot 都为空 —— 既没有段 id 也没有"
            "分段 uid，无法认段。请检查提交前编译是否把段身份写进来。")
    # 正常画布 Queue 时 bz_prompt_build.js:937-940 会主动清掉 chain.clip，
    # 后端 _register_clip 按 chain.shot 认段（csglide_cast.py:2791-2801）。
    # Ticket 沿用同一口径：clip 为空就以 shot uid 作待解析身份，由存档节点
    # 按 shot 从档案反查真实 cid（不读全局 current）。
    id_kind = "clip" if cid else "shot"
    resolved = cid if cid else "@shot:" + shot

    length = int(data.get("length") or 124)
    cont = data.get("cont") or {}
    try:
        overlap = int(cont.get("frames") or 0)
    except (TypeError, ValueError):
        overlap = 0
    is_first = (cid == "clip_0001") or overlap <= 0
    frame = ledger.frame_spec_for(length, is_first=is_first, overlap=overlap)

    return SegmentTicket(
        project_id=name, clip_id=resolved, shot=shot, id_kind=id_kind,
        length=int(length), gross_frames=frame.gross_frames,
        trim_head=frame.trim_head, net_frames=frame.net_frames, fps=frame.fps,
        fingerprint=fingerprint, cast_node_id=cast_id,
    )
