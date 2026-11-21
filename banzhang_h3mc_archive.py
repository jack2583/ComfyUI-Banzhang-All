# -*- coding: utf-8 -*-
"""💾 H3 续拍存档 · 中央档案统一编号（注册层包装）

设计
----
续拍存档不再使用 ComfyUI 保存计数器生成 ``h3_context/clip_00420_.safetensors``。
它以导演台当前段和当前 take 为唯一归属，直接写入中央工程目录：

    output/h3_chain/<片名>/clip_000N.t<take>.cont.safetensors

**这一段一版只有这一个 AV latent 文件。** 它同时是：下一段的承接窗口、
本段的钉帧/加长原稿、整片潜空间拼接的段落来源。

20261104 之前，本节点内部会先调用「📦 H3 段落存档」写一份
``clip_000N.t<take>.latent.safetensors``，再自己拷出 ``.cont.safetensors``。
两份内容完全相同、却由两条独立代码路径写入，任何一次写入失败都会留下
「只有一个文件」的半档；而下游按文件名分读（钉帧/整片潜空间读 .latent，
承接窗口读 .cont），于是出现加长被拒、收片缺档、整片潜空间静默丢段。
现在本节点自己完成登记 + 写盘，📦 节点与 .latent 文件一并删除。

顺带保留「顺序锚」输入口：它不参与计算，只保证导演台先登记当前段，本节点
随后落到同一段、同一版本上。
"""

import os
import time

_NODE_ID = "MiniMaxH3MotionContextSaveLatent"
_TAG = "[H3 续拍存档]"

_ANCHOR_SPEC = ("*", {
    "label": "顺序锚",
    "tooltip": "不参与计算，只保证本节点排在导演台之后执行。",
})

# 开关两档文字一并提供中文：ComfyUI 原生支持 BOOLEAN 的 label_on / label_off
# （comfy/comfy_types/node_typing.py），不写就回落成前端默认的英文 true/false。
_ENABLED_SPEC = ("BOOLEAN", {
    "default": True,
    "label": "启用存档",
    "label_on": "开",
    "label_off": "关",
    "tooltip": "关掉后本节点只透传潜空间，不写档、不推进 take、不登记段落。"
               "一采/二采/三采可各挂一个存档节点，只开想存档的那一段。",
})


def _set_last_saved(path):
    """把中央 cont 路径登记给 _h3mc；兼容旧代码读取 LAST_SAVED。"""
    try:
        from ._h3mc import nodes as mc_nodes
    except Exception:
        from _h3mc import nodes as mc_nodes
    mc_nodes.LAST_SAVED = {"path": path, "time": time.time()}


def _write_av_latent(latent, path):
    """把采样器输出的 AV latent 写成**唯一**那份 cont 文件。

    只复用 csglide_clip 里的容器工具函数（拆 NestedTensor / 安全落盘），
    不再调用任何存档节点类。Windows 上 safetensors 是内存映射的，
    _write_st 内部先写 .tmp 再 os.replace，避免覆盖正在被读的文件时炸。
    """
    from .csglide_clip import _streams, _write_st

    streams = _streams((latent or {}).get("samples"))
    if not streams:
        raise ValueError("续拍存档收到的潜空间是空的。")
    tensors = {}
    for i, stream in enumerate(streams):
        key = "video" if i == 0 else "audio" if i == 1 else "s%d" % i
        tensors[key] = stream.cpu().contiguous()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _write_st(path, tensors)
    return tensors


def _ticket_identity(prompt, unique_id):
    """从本次 prompt 编译 Ticket，返回 (工程名, 段 id, Ticket)。

    默认严格：编译失败一律抛错，绝不退回全局 current 猜段 —— 这正是
    「采样 6 分钟里 current 被改回旧段、第 3 段成果存进第 2 段」的根治点。
    应急回退仅供真机排障：设环境变量 BZ_TICKET_FALLBACK=1 后重启。
    """
    try:
        from .csglide_ticket import ticket_from_prompt
    except ImportError:
        from csglide_ticket import ticket_from_prompt

    fallback = os.environ.get("BZ_TICKET_FALLBACK", "").strip() == "1"
    try:
        ticket = ticket_from_prompt(prompt, unique_id)
        print("%s Ticket 认段：%s（指纹 %s）"
              % (_TAG, ticket.label, ticket.fingerprint[:12]))
        cid = ticket.clip_id
        if getattr(ticket, "id_kind", "clip") == "shot":
            # 与 _register_clip 完全同一口径（csglide_cast.py:2791-2801）：
            # 按稳定 shot uid 从档案反查真实 cid，绝不读全局 current。
            try:
                from . import csglide_chain as ch
            except ImportError:
                import csglide_chain as ch
            cid = ""
            for row in (ch.summary(ticket.project_id).get("clips") or []):
                if str(row.get("shot") or "") == ticket.shot:
                    cid = str(row.get("id") or "")
                    break
            if not cid:
                raise RuntimeError(
                    "%s 按分段 uid %s 没在档案里找到段落 —— 请先重跑上一段让它登记，"
                    "或在时间轴上认领该段。" % (_TAG, ticket.shot))
            print("%s 按 shot uid 认段：%s → %s" % (_TAG, ticket.shot, cid))
        return ticket.project_id, cid, ticket
    except Exception as exc:
        if not fallback:
            raise RuntimeError(
                "%s Ticket 认段失败：%s。本节点不再读全局 current 猜段；"
                "请用画布正常 Queue（由提交前编译注入 chain.clip）。"
                "应急回退：设环境变量 BZ_TICKET_FALLBACK=1 后重启。" % (_TAG, exc))
        print("⚠️ %s Ticket 认段失败，已按应急开关回退到旧 current 路径：%s"
              % (_TAG, exc))
        try:
            from . import csglide_chain as chain
        except ImportError:
            import csglide_chain as chain
        name = chain.last_name() or ""
        cid = ""
        if name:
            cid = ((chain.load_chain(name) or {}).get("current") or "").strip()
        return name, cid, None


def mirror_to_central(latent, anchor, prompt=None, unique_id=None):
    """把当前采样 latent 归到 Ticket 指定的段，写唯一一份统一编号的 cont 文件。

    认段不再依赖 chain.json.current / last_name()：导演台与存档节点各自从
    同一份 hidden.prompt 算出同一张 Ticket（见 csglide_ticket.py）。
    take 策略维持既有行为：每次执行都另存为新版本，不覆盖当前版。
    """
    try:
        from . import csglide_chain as chain
    except ImportError:
        import csglide_chain as chain

    name, cid, ticket = _ticket_identity(prompt, unique_id)
    if not name or not cid:
        print("%s Ticket 没能给出工程名 / 段 id，本次不能使用统一编号存档。" % _TAG)
        return None
    if chain.load_clip(name, cid) is None:
        print("%s 段 %s/%s 在档案里不存在，本次不能使用统一编号存档。"
              % (_TAG, name, cid))
        return None

    meta = chain.load_clip(name, cid) or {}
    # 与旧「另存为版本=True」逐字等价：current_take + 1，下限 1。
    take = max(1, int(meta.get("current_take") or 0) + 1)
    path = chain.clip_cont(name, cid, take)

    tensors = _write_av_latent(latent, path)

    takes = list(meta.get("takes") or [])
    if take not in takes:
        takes.append(take)
    chain.update_clip(name, cid, {
        "takes": takes, "current_take": take,
        "has_latent": True, "pending": None,
    })
    _set_last_saved(path)

    names = ", ".join("%s(%s)" % (k, tuple(v.shape)) for k, v in tensors.items())
    print("%s 已统一归档：%s / %s / take %d，cont=%s；流：%s"
          % (_TAG, name, cid, take, os.path.basename(path), names))
    return {
        "name": name,
        "cid": cid,
        "take": take,
        "cont_path": path,
        "ticket": ticket.ticket_hash() if ticket is not None else None,
    }


def apply_archive(cls_map):
    """给续拍存档节点补「顺序锚」口、撤掉保存计数控件，并接入 Ticket 认段。"""
    cls = cls_map.get(_NODE_ID)
    if cls is None:
        return False
    try:
        # 取未绑定的原函数，避免 classmethod 包装后多传一次 cls。
        orig_input = cls.INPUT_TYPES.__func__

        def _patched_input(cls, *a, **k):
            data = orig_input(cls, *a, **k)
            required = data.get("required") or {}
            # 保存路径和本片段编号不再交给用户；统一由 Ticket/take 决定。
            required.pop("filename_prefix", None)
            required.pop("clip_index", None)
            # 开关设为可选（默认开）：老工作流/老模板没有这个控件也不会被判缺字段，
            # 且可选控件同样会随工作流提交、带默认值，中央选片能照常读到状态。
            data.setdefault("optional", {})["archive_enabled"] = _ENABLED_SPEC
            data.setdefault("optional", {}).setdefault("顺序锚", _ANCHOR_SPEC)
            # hidden 只用于读本次 prompt 与节点 id，不渲染控件、不改连线。
            # execution.py:216-228 确认 PROMPT 给的是完整原始 prompt 字典。
            hidden = data.setdefault("hidden", {})
            hidden["prompt"] = "PROMPT"
            hidden["unique_id"] = "UNIQUE_ID"
            return data

        cls.INPUT_TYPES = classmethod(_patched_input)

        def _patched(self, *a, **kw):
            latent = kw.pop("latent", None)
            if latent is None and a:
                latent = a[0]
            anchor = kw.pop("顺序锚", None)
            prompt = kw.pop("prompt", None)
            node_id = kw.pop("unique_id", None)

            # 开关关闭：纯透传，不写档、不推进 take、不登记段落（20261017）。
            if not kw.pop("archive_enabled", True):
                return (latent,)

            stale = []
            for key in ("filename_prefix", "clip_index"):
                if key in kw:
                    value = kw.pop(key)
                    if value not in (None, "", 0):
                        stale.append("%s=%r" % (key, value))
            if stale:
                print("%s 旧参数 %s 已失效；现在按 Ticket/中央段落/take 统一编号。"
                      % (_TAG, "、".join(stale)))

            info = mirror_to_central(latent, anchor, prompt, node_id)
            if info is None:
                raise RuntimeError(
                    "H3 续拍存档：Ticket 没能给出工程名 / 段 id，或该段在档案里"
                    "不存在；本节点不再读全局 current 猜段、也不使用保存计数。")
            # 输出仍为 latent 直通，供后续 VAE 解码和二采链使用。
            return (latent,)

        cls.save = _patched
        return True
    except Exception as e:
        print("⚠️ [班长全家桶] 续拍存档统一编号未挂载: %s" % e)
        return False
